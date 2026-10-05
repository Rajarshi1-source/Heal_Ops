"""Closed-loop verification.

`judge()` is a pure function, so every outcome the system can reach is testable here without a
cluster, a database or a clock. That matters because this is the file that decides whether HealOps
tells the truth about itself.

The tests are grouped by the three questions verification asks, plus the `inconclusive` paths that
exist specifically so a missing signal can never read as recovery.
"""

from __future__ import annotations

import pytest

from plan import (
    ApiCall,
    BlastRadius,
    Durability,
    ExpectedSignal,
    RemediationPlan,
    Reversibility,
)
from runbooks.base import SignalWindow
from verifier import (
    OOM_TOLERANCE,
    PSI_FULL_CEILING,
    RESTART_TOLERANCE,
    THROTTLE_RATIO_CEILING,
    judge,
)


def plan(**overrides: object) -> RemediationPlan:
    call = ApiCall(verb="patch", resource="apps/v1/Deployment", name="api", namespace="shop")
    base: dict[str, object] = {
        "runbook": "crashloop_rollback",
        "incident_id": 1,
        "target_kind": "Deployment",
        "target_name": "api",
        "target_namespace": "shop",
        "actions": (call,),
        "compensation": (call,),
        "reversibility": Reversibility.REVERSIBLE,
        "durability": Durability.EPHEMERAL,
        "ttl_seconds": 7200,
        "blast_radius": BlastRadius(pods=3),
        "confidence": 0.94,
        "summary": "roll back shop/api",
        "expected_signal": ExpectedSignal(
            alert_should_clear="KubePodCrashLooping", window_seconds=300
        ),
    }
    return RemediationPlan(**(base | overrides))  # type: ignore[arg-type]


def healthy(**overrides: object) -> SignalWindow:
    """A window in which everything was measured and everything is fine."""
    base: dict[str, object] = {
        "alert_firing": False,
        "restarts": 0,
        "oom_events": 0.0,
        "throttle_ratio": 0.01,
        "psi_full": 0.0,
        "memory_usage_ratio": 0.4,
        "new_alerts": (),
    }
    return SignalWindow(**(base | overrides))  # type: ignore[arg-type]


def sick(**overrides: object) -> SignalWindow:
    base: dict[str, object] = {
        "alert_firing": True,
        "restarts": 7,
        "oom_events": 2.0,
        "throttle_ratio": 0.4,
        "psi_full": 0.2,
        "memory_usage_ratio": 0.99,
        "new_alerts": ("KubePodCrashLooping",),
    }
    return SignalWindow(**(base | overrides))  # type: ignore[arg-type]


# =================================================================================================
# The success path -- and how narrow it is
# =================================================================================================


def test_all_three_questions_answered_yes_is_remediated() -> None:
    """The only route to success: alert cleared, signals recovered, no new alerts."""
    verdict = judge(plan(), before=sick(), after=healthy())

    assert verdict.outcome == "remediated"
    assert verdict.recovered is True
    assert verdict.alert_cleared is True
    assert verdict.needs_compensation is False


def test_success_records_why_it_succeeded() -> None:
    # The Slack card quotes these, so "fixed" is never posted without evidence.
    verdict = judge(plan(), before=sick(), after=healthy())
    assert verdict.reasons
    assert "alert cleared" in verdict.reasons[0]


def test_signals_before_and_after_are_both_recorded() -> None:
    # What the dashboard diffs, and what makes a verdict auditable after the fact.
    verdict = judge(plan(), before=sick(), after=healthy())
    assert verdict.signals_before["restarts"] == 7.0
    assert verdict.signals_after["restarts"] == 0.0


# =================================================================================================
# Question 1 -- did the alert clear?
# =================================================================================================


def test_alert_still_firing_is_not_recovered() -> None:
    verdict = judge(plan(), before=sick(), after=healthy(alert_firing=True))

    assert verdict.outcome == "not_recovered"
    assert verdict.needs_compensation is True
    assert "still firing" in verdict.reasons[0]


def test_unreadable_alert_state_is_inconclusive_not_success() -> None:
    """The assertion that keeps the scorecard honest.

    If an unreachable Alertmanager read as "cleared", an observability outage would be recorded as a
    successful remediation.
    """
    verdict = judge(plan(), before=sick(), after=healthy(alert_firing=None))

    assert verdict.outcome == "inconclusive"
    assert verdict.recovered is False
    assert verdict.alert_cleared is None


def test_inconclusive_triggers_compensation() -> None:
    """A change that could not be shown to work is not left in production on trust.

    The plan routes `inconclusive` to `compensate()` alongside `not_recovered`: the watchdog makes
    the same call when verification never ran at all, and the two must not disagree.
    """
    verdict = judge(plan(), before=sick(), after=healthy(alert_firing=None))
    assert verdict.needs_compensation is True


def test_alert_state_is_not_required_when_the_plan_expects_none() -> None:
    # A plan with no expected_signal makes no claim about an alert, so unknown alert state is not
    # itself a reason for doubt.
    verdict = judge(plan(expected_signal=None), before=sick(), after=healthy(alert_firing=None))
    assert verdict.outcome == "remediated"


# =================================================================================================
# Question 2 -- did the golden signals recover?
# =================================================================================================


def test_continued_restarts_fail_even_with_the_alert_cleared() -> None:
    """The case Rev 1 recorded as a success.

    A rollback onto an equally broken revision clears the alert briefly while the container keeps
    dying. The restart counter is what catches it.
    """
    verdict = judge(plan(), before=sick(), after=healthy(restarts=4))

    assert verdict.outcome == "not_recovered"
    assert any("still restarting" in r for r in verdict.reasons)


def test_restart_tolerance_is_zero() -> None:
    # A rollback that halves the crash rate is still a crash loop.
    assert RESTART_TOLERANCE == 0
    assert judge(plan(), before=sick(), after=healthy(restarts=1)).outcome == "not_recovered"
    assert judge(plan(), before=sick(), after=healthy(restarts=0)).outcome == "remediated"


def test_any_oom_event_after_the_fix_fails() -> None:
    assert OOM_TOLERANCE == 0.0
    verdict = judge(plan(), before=sick(), after=healthy(oom_events=1.0))

    assert verdict.outcome == "not_recovered"
    assert any("OOM" in r for r in verdict.reasons)


def test_throttle_ratio_above_the_ceiling_fails() -> None:
    verdict = judge(
        plan(), before=sick(), after=healthy(throttle_ratio=THROTTLE_RATIO_CEILING + 0.01)
    )
    assert verdict.outcome == "not_recovered"
    assert any("throttle ratio" in r for r in verdict.reasons)


def test_throttle_ratio_at_the_ceiling_passes() -> None:
    # The comparison is `>`, so exactly at the ceiling is acceptable.
    verdict = judge(plan(), before=sick(), after=healthy(throttle_ratio=THROTTLE_RATIO_CEILING))
    assert verdict.outcome == "remediated"


def test_memory_pressure_above_the_ceiling_fails() -> None:
    """PSI answers "did the workload actually recover?" with pressure, not with alert state."""
    verdict = judge(plan(), before=sick(), after=healthy(psi_full=PSI_FULL_CEILING + 0.01))

    assert verdict.outcome == "not_recovered"
    assert any("stalled" in r for r in verdict.reasons)


def test_every_failing_signal_is_reported_not_just_the_first() -> None:
    # An operator reading the escalation needs the whole picture, not the first thing that tripped.
    verdict = judge(
        plan(),
        before=sick(),
        after=healthy(restarts=3, oom_events=2.0, throttle_ratio=0.5, psi_full=0.3),
    )
    assert len(verdict.reasons) >= 4


def test_one_unreadable_signal_does_not_block_a_verdict() -> None:
    # PSI is unavailable on some nodes. As long as something else was measured and nothing is wrong,
    # that is enough for a positive verdict.
    verdict = judge(plan(), before=sick(), after=healthy(psi_full=None))
    assert verdict.outcome == "remediated"


def test_no_signal_readable_at_all_is_inconclusive() -> None:
    """The alert cleared but nothing could be measured, so recovery is unproven.

    This is the difference between "shown to be healthy" and "not shown to be broken".
    """
    verdict = judge(
        plan(),
        before=sick(),
        after=SignalWindow(
            alert_firing=False,
            restarts=None,
            oom_events=None,
            throttle_ratio=None,
            psi_full=None,
        ),
    )

    assert verdict.outcome == "inconclusive"
    assert "no golden signal could be read" in verdict.reasons[0]


# =================================================================================================
# Question 3 -- did the action cause something else?
# =================================================================================================


def test_a_new_alert_the_action_could_have_caused_fails() -> None:
    """Scaling up fixes CPU and can start an OOM cascade on a memory-tight node.

    Only a whole-target check sees that, which is the entire reason this question exists.
    """
    verdict = judge(
        plan(),
        before=healthy(new_alerts=()),
        after=healthy(new_alerts=("KubePodOOMKilled",)),
    )

    assert verdict.outcome == "not_recovered"
    assert any("could have caused" in r for r in verdict.reasons)
    assert verdict.new_alerts == ("KubePodOOMKilled",)


def test_a_pre_existing_alert_is_not_blamed_on_the_action() -> None:
    # It was already firing before we acted, so it is not evidence that we broke something.
    verdict = judge(
        plan(),
        before=healthy(new_alerts=("NodeDiskPressure",)),
        after=healthy(new_alerts=("NodeDiskPressure",)),
    )
    assert verdict.outcome == "remediated"


def test_an_alert_already_pending_before_acting_is_not_blamed_on_the_action() -> None:
    """Regression, found on the kind cluster: a working scale-out was reverted for
    CPUThrottlingHigh -- a `for: 15m` alert the CPU fault itself had started, pending when the
    engine acted and firing mid-window. The condition predates the action; so does the blame."""
    verdict = judge(
        plan(),
        before=healthy(new_alerts=(), pending_alerts=("CPUThrottlingHigh",)),
        after=healthy(new_alerts=("CPUThrottlingHigh",)),
    )
    assert verdict.outcome == "remediated"


def test_an_alert_that_starts_pending_only_after_acting_still_counts() -> None:
    # Pending AFTER the action is no excuse: only what was already under way before is forgiven.
    verdict = judge(
        plan(),
        before=healthy(new_alerts=()),
        after=healthy(new_alerts=("KubePodOOMKilled",), pending_alerts=("KubePodOOMKilled",)),
    )
    assert verdict.outcome == "not_recovered"


def test_the_triggering_alert_is_not_counted_as_a_new_alert() -> None:
    # It is the subject of question 1; counting it again here would double-report one problem.
    verdict = judge(
        plan(),
        before=sick(),
        after=healthy(new_alerts=("KubePodCrashLooping",)),
    )
    assert verdict.outcome == "remediated"


# =================================================================================================
# The runbook's own probe
# =================================================================================================


def test_extra_verification_failure_fails_the_verdict() -> None:
    """A runbook can assert things the golden signals cannot.

    The OOM runbook checks that `restartCount` genuinely did not move, which is the specific claim
    "in-place resize" makes.
    """
    verdict = judge(
        plan(),
        before=sick(),
        after=healthy(),
        extra_failure="container restarted during the in-place resize",
    )

    assert verdict.outcome == "not_recovered"
    assert "container restarted" in " ".join(verdict.reasons)


def test_no_extra_failure_leaves_the_verdict_alone() -> None:
    assert judge(plan(), before=sick(), after=healthy(), extra_failure=None).outcome == (
        "remediated"
    )


# =================================================================================================
# Serialisation
# =================================================================================================


def test_verdict_serialises_for_the_ledger_and_the_dashboard() -> None:
    payload = judge(plan(), before=sick(), after=healthy(restarts=2)).to_dict()

    assert payload["outcome"] == "not_recovered"
    assert isinstance(payload["reasons"], list)
    assert "signals_before" in payload
    assert "signals_after" in payload


def test_only_a_proven_recovery_avoids_compensation() -> None:
    # Success is the one outcome that keeps the change. The other two both undo it, and stay
    # distinct in the verdict so the scorecard can tell "it failed" from "we could not tell".
    assert judge(plan(), before=sick(), after=healthy()).needs_compensation is False
    assert judge(plan(), before=sick(), after=healthy(restarts=3)).needs_compensation is True
    assert judge(plan(), before=sick(), after=healthy(alert_firing=None)).needs_compensation is True


@pytest.mark.parametrize(
    ("constant", "expected"),
    [
        (THROTTLE_RATIO_CEILING, 0.05),
        (PSI_FULL_CEILING, 0.05),
        (RESTART_TOLERANCE, 0),
        (OOM_TOLERANCE, 0.0),
    ],
)
def test_thresholds_match_the_specification(constant: float, expected: float) -> None:
    # These numbers are the verdict. If someone loosens one, it should be a visible change.
    assert constant == expected

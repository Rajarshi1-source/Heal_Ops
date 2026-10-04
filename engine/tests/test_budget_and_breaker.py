"""Bounded automation: the token-bucket budget and the global breaker.

The trade-off these encode is deliberate -- in a mass event HealOps fixes *less* and pages *sooner*.
These tests pin the numbers that make that true, and the transitions that let automation resume.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import breaker
import budget
import ledger
from db.connection import db_now
from db.models import RemediationBudget, Verification
from tests.factories import ledger_events, make_execution

pytestmark = pytest.mark.db

TRIAL = str(uuid4())
OTHER = str(uuid4())


# --- budget ---------------------------------------------------------------------------------------


def test_the_specified_limits() -> None:
    assert budget.limit_for("global") == 20
    assert budget.limit_for("ns:payments") == 3
    assert budget.limit_for("ns:shop") == 8
    assert budget.limit_for("ns:chaos") == 50
    assert budget.limit_for("ns:somewhere-new") == 5


async def test_consume_draws_from_the_namespace_and_the_global_bucket(
    db_session: AsyncSession,
) -> None:
    await budget.consume(db_session, "payments")
    buckets = {b.scope: b for b in await budget.buckets(db_session, "payments")}
    assert buckets["global"].used == 1
    assert buckets["ns:payments"].used == 1


async def test_the_snapshot_reports_the_tighter_bucket(db_session: AsyncSession) -> None:
    """One `used >= max` rule then covers both limits."""
    for _ in range(3):
        await budget.consume(db_session, "payments")

    snap = await budget.snapshot(db_session, "payments")

    assert snap.scope == "ns:payments"
    assert (snap.used_actions, snap.max_actions) == (3, 3)


async def test_exhaustion_is_detected_at_the_moment_of_acting(db_session: AsyncSession) -> None:
    assert await budget.has_room(db_session, "payments") is None
    for _ in range(3):
        await budget.consume(db_session, "payments")

    exhausted = await budget.has_room(db_session, "payments")
    assert exhausted is not None and exhausted.scope == "ns:payments"


async def test_node_actions_draw_only_on_the_global_bucket(db_session: AsyncSession) -> None:
    await budget.consume(db_session, None)
    assert [b.scope for b in await budget.buckets(db_session, None)] == ["global"]


async def test_tokens_refill_continuously_not_at_the_top_of_the_hour(
    db_session: AsyncSession,
) -> None:
    """A true token bucket: 3/hour means one token back every 20 minutes, not all three at once."""
    for _ in range(3):
        await budget.consume(db_session, "payments")
    row = await db_session.get(RemediationBudget, "ns:payments")
    assert row is not None
    row.window_start = await db_now(db_session) - timedelta(minutes=25)
    await db_session.flush()

    buckets = {b.scope: b for b in await budget.buckets(db_session, "payments")}
    assert buckets["ns:payments"].used == 2, "one token refilled after 25 of 60 minutes"

    row.window_start = await db_now(db_session) - timedelta(hours=2)
    await db_session.flush()
    buckets = {b.scope: b for b in await budget.buckets(db_session, "payments")}
    assert buckets["ns:payments"].used == 0, "never refills past empty"


async def test_a_tightened_limit_takes_effect_without_a_migration(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config import get_settings

    await budget.consume(db_session, "shop")
    monkeypatch.setitem(get_settings().budget_namespace_max, "shop", 1)
    snap = await budget.snapshot(db_session, "shop")
    assert (snap.used_actions, snap.max_actions) == (1, 1)


# --- breaker: state machine -----------------------------------------------------------------------


async def test_a_new_breaker_is_closed(db_session: AsyncSession) -> None:
    assert (await breaker.view(db_session)).state == "closed"
    assert (await breaker.admit(db_session, OTHER))[0] is True


async def test_trip_opens_for_the_cool_down_and_is_audited(db_session: AsyncSession) -> None:
    await breaker.trip(db_session, "testing")
    current = await breaker.view(db_session)

    assert current.state == "open" and current.reason == "testing"
    assert current.until is not None
    allowed, why = await breaker.admit(db_session, OTHER)
    assert not allowed and "testing" in why
    assert "breaker" in await ledger_events(db_session, None)  # type: ignore[arg-type]


async def test_open_becomes_half_open_after_the_cool_down(db_session: AsyncSession) -> None:
    await breaker.trip(db_session, "testing")
    row = await db_session.get(RemediationBudget, "global")
    assert row is not None
    row.breaker_until = await db_now(db_session) - timedelta(seconds=1)
    await db_session.flush()

    assert (await breaker.view(db_session)).state == "half_open"


async def half_open(session: AsyncSession) -> None:
    await breaker.trip(session, "testing")
    row = await session.get(RemediationBudget, "global")
    assert row is not None
    row.breaker_until = await db_now(session) - timedelta(seconds=1)
    await session.flush()


async def test_half_open_lets_exactly_one_plan_through(db_session: AsyncSession) -> None:
    await half_open(db_session)

    assert (await breaker.admit(db_session, TRIAL))[0] is True
    assert (await breaker.admit(db_session, TRIAL))[0] is True, "the trial may re-enter"
    second, why = await breaker.admit(db_session, OTHER)
    assert second is False and "single trial" in why
    # Once the slot is taken, the gate sees the breaker as open for everyone else.
    assert (await breaker.view(db_session)).for_policy() == "open"


async def test_a_successful_trial_closes_the_breaker(db_session: AsyncSession) -> None:
    await half_open(db_session)
    await breaker.admit(db_session, TRIAL)

    await breaker.record_outcome(db_session, TRIAL, success=True)

    assert (await breaker.view(db_session)).state == "closed"


async def test_a_failed_trial_reopens_it(db_session: AsyncSession) -> None:
    await half_open(db_session)
    await breaker.admit(db_session, TRIAL)

    await breaker.record_outcome(db_session, TRIAL, success=False)

    current = await breaker.view(db_session)
    assert current.state == "open" and "trial" in (current.reason or "")


async def test_other_plans_outcomes_do_not_settle_the_trial(db_session: AsyncSession) -> None:
    await half_open(db_session)
    await breaker.admit(db_session, TRIAL)
    await breaker.record_outcome(db_session, OTHER, success=True)
    assert (await breaker.view(db_session)).state == "half_open"


async def test_a_freeze_has_no_expiry_and_names_its_owner(db_session: AsyncSession) -> None:
    await breaker.freeze(db_session, reason="release in progress", owner="alice")
    current = await breaker.view(db_session)

    assert current.state == "open" and current.frozen
    assert current.until is None, "a release ends when the release does, not on a timer"
    assert (current.owner, current.reason) == ("alice", "release in progress")

    await breaker.unfreeze(db_session, owner="alice")
    assert (await breaker.view(db_session)).state == "closed"


async def test_the_freeze_is_a_ledger_entry_with_a_reason_and_an_owner(
    db_session: AsyncSession,
) -> None:
    from sqlalchemy import select

    from db.models import AuditLedger

    await breaker.freeze(db_session, reason="release in progress", owner="alice")
    entry = (
        (
            await db_session.execute(
                select(AuditLedger)
                .where(AuditLedger.event_type == "freeze")
                .order_by(AuditLedger.seq.desc())
            )
        )
        .scalars()
        .first()
    )
    assert entry is not None
    assert entry.actor == "alice"
    assert entry.payload["reason"] == "release in progress"


# --- breaker: trip conditions ---------------------------------------------------------------------


async def compensations(session: AsyncSession, n: int, trigger: str = "verification") -> None:
    for _ in range(n):
        await ledger.append(session, "compensate", {"trigger": trigger})


async def test_more_than_three_compensations_in_fifteen_minutes_trips(
    db_session: AsyncSession,
) -> None:
    await compensations(db_session, 3)
    assert await breaker.evaluate(db_session) is None, "three is the limit, not over it"

    await compensations(db_session, 1, trigger="watchdog")
    reason = await breaker.evaluate(db_session)

    assert reason is not None and "4 compensations" in reason
    assert (await breaker.view(db_session)).state == "open"


async def test_expiries_are_not_counted_as_the_system_getting_it_wrong(
    db_session: AsyncSession,
) -> None:
    await compensations(db_session, 6, trigger="expiry")
    assert await breaker.evaluate(db_session) is None


async def verdicts(session: AsyncSession, *outcomes: str) -> None:
    for outcome in outcomes:
        _, execution, _ = await make_execution(session)
        session.add(
            Verification(
                execution_id=execution.id,
                window_seconds=300,
                signals_before={},
                signals_after={},
                outcome=outcome,
            )
        )
        await session.flush()


async def test_more_than_two_consecutive_failures_for_one_runbook_trips(
    db_session: AsyncSession,
) -> None:
    await verdicts(db_session, "not_recovered", "inconclusive")
    assert await breaker.evaluate(db_session) is None

    await verdicts(db_session, "not_recovered")
    reason = await breaker.evaluate(db_session)
    assert reason is not None and "3 consecutive" in reason


async def test_a_success_ends_the_streak(db_session: AsyncSession) -> None:
    # All rows share the test transaction's frozen `now()`, so ordering is by insertion here: the
    # point is only that a success anywhere among the latest verdicts breaks the run.
    await verdicts(db_session, "not_recovered", "remediated")
    assert await breaker._consecutive_failures(db_session) is None


async def test_evidence_before_the_last_transition_is_not_reused(
    db_session: AsyncSession,
) -> None:
    """The failures that tripped the breaker must not re-trip it the moment a trial closes it."""
    await compensations(db_session, 5)
    await breaker.evaluate(db_session)
    await half_open(db_session)
    await breaker.admit(db_session, TRIAL)
    await breaker.record_outcome(db_session, TRIAL, success=True)

    assert await breaker._too_many_compensations(db_session) is None


def test_a_high_kubernetes_error_rate_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    totals = iter([(0.0, 0.0), (100.0, 30.0)])
    monkeypatch.setattr(breaker, "_k8s_totals", lambda: next(totals))
    breaker._samples.clear()

    assert breaker._k8s_error_rate() is None, "one sample has no rate"
    reason = breaker._k8s_error_rate()
    assert reason is not None and "30%" in reason


def test_a_few_failures_out_of_a_few_calls_is_noise(monkeypatch: pytest.MonkeyPatch) -> None:
    totals = iter([(0.0, 0.0), (5.0, 4.0)])
    monkeypatch.setattr(breaker, "_k8s_totals", lambda: next(totals))
    breaker._samples.clear()
    breaker._k8s_error_rate()
    assert breaker._k8s_error_rate() is None


def test_only_server_side_failures_count_as_api_errors() -> None:
    """A 404 Lease lookup is the API server working; counting it would trip on a healthy cluster."""
    from kubernetes.client.rest import ApiException

    import metrics
    from k8s.client import _count_failure

    def errors() -> float:
        return breaker._k8s_totals()[1]

    start = errors()
    _count_failure("probe", ApiException(status=404))
    _count_failure("probe", ApiException(status=409))
    assert errors() == start
    _count_failure("probe", ApiException(status=503))
    _count_failure("probe", ApiException(status=429))
    assert errors() == start + 2
    assert metrics.k8s_api_errors is not None


async def test_evaluate_waits_out_an_open_breaker(db_session: AsyncSession) -> None:
    await breaker.trip(db_session, "testing")
    await compensations(db_session, 9)
    assert await breaker.evaluate(db_session) is None

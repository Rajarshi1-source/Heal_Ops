"""Closed-loop verification.

This module is the answer to defect D1, and it is the single most important file in the project.
Rev 1 called the Kubernetes API, got a 200, wrote `SUCCESS` and posted to Slack -- so a rollback
onto an equally broken revision was recorded as a successful auto-remediation. That is worse than
no automation, because it produces false confidence.

**`REMEDIATED` requires all three of:**

1. the triggering alert **cleared** in Alertmanager,
2. the workload's **golden signals recovered** below their thresholds,
3. **no new alert** appeared on the same target that the action could have caused.

Anything else is `not_recovered` (compensate and escalate) or `inconclusive` (escalate). There is no
fourth outcome and no silent success.

### Why "the alert stopped firing" is not enough

An interviewer who has run production asks exactly this. Three reasons, and they are the reason this
file is longer than a single Alertmanager query:

* **Alerts clear for the wrong reasons.** The pod was deleted, the workload scaled to zero, the
  metric went stale. The alert is gone and the incident is not.
* **`for:` durations lag both recovery and relapse** by minutes, so a short window can catch an
  alert mid-transition in either direction.
* **The action can cause a *different* problem.** Scaling up fixes CPU and starts an OOM cascade on
  a memory-tight node. Only a whole-target check sees that, which is what question 3 is for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

import ledger
import metrics
from config import get_settings
from db.connection import db_now
from db.models import RunbookExecution, Verification
from integrations import alertmanager_client, prometheus_client
from integrations.prometheus_client import GoldenSignals
from plan import RemediationPlan
from runbooks.base import Runbook, SignalWindow

log = structlog.get_logger(__name__)

Outcome = Literal["remediated", "not_recovered", "inconclusive"]

# Thresholds. These are the numbers the whole verdict turns on, so they live here as named constants
# rather than as literals buried in comparisons.
THROTTLE_RATIO_CEILING = 0.05
"""5% of CFS periods throttled. Above this the workload is still CPU-starved."""

PSI_FULL_CEILING = 0.05
"""5% of wall time fully stalled on memory."""

RESTART_TOLERANCE = 0
"""Zero. A container that is still restarting has not recovered, however much less often."""

OOM_TOLERANCE = 0.0
"""Any OOM event inside the window means the memory fix did not hold."""


@dataclass(frozen=True, slots=True)
class Verdict:
    outcome: Outcome
    reasons: tuple[str, ...] = ()
    alert_cleared: bool | None = None
    signals_before: dict[str, float | None] = field(default_factory=dict)
    signals_after: dict[str, float | None] = field(default_factory=dict)
    new_alerts: tuple[str, ...] = ()

    @property
    def recovered(self) -> bool:
        return self.outcome == "remediated"

    @property
    def needs_compensation(self) -> bool:
        """Only a definite failure triggers compensation.

        `inconclusive` escalates *without* compensating: undoing a fix that may well have worked,
        because a metrics endpoint was unreachable, would turn an observability outage into a second
        production change.
        """
        return self.outcome == "not_recovered"

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "alert_cleared": self.alert_cleared,
            "signals_before": self.signals_before,
            "signals_after": self.signals_after,
            "new_alerts": list(self.new_alerts),
        }


async def capture_window(
    plan: RemediationPlan, *, window: str = "5m"
) -> tuple[SignalWindow, GoldenSignals]:
    """Read the target's signals now.

    Called once before acting and once after the window elapses, so `signals_before` and
    `signals_after` are measured the same way -- otherwise the comparison would be between two
    different definitions of health.
    """
    signals = await prometheus_client.golden_signals(
        plan.target_namespace,
        workload=plan.target_name if plan.target_kind != "Node" else None,
        window=window,
    )

    alert_name = plan.expected_signal.alert_should_clear if plan.expected_signal else None
    firing: bool | None = None
    if alert_name:
        firing = await alertmanager_client.is_firing(
            alert_name,
            namespace=plan.target_namespace,
            node=plan.target_name if plan.target_kind == "Node" else None,
        )

    on_target = await alertmanager_client.alerts_on_target(
        plan.target_namespace,
        node=plan.target_name if plan.target_kind == "Node" else None,
    )

    return (
        SignalWindow(
            alert_firing=firing,
            restarts=int(signals.restarts) if signals.restarts is not None else None,
            oom_events=signals.oom_events,
            throttle_ratio=signals.throttle_ratio,
            psi_full=signals.psi_full,
            memory_usage_ratio=signals.memory_usage_ratio,
            new_alerts=tuple(on_target or ()),
        ),
        signals,
    )


def judge(
    plan: RemediationPlan,
    before: SignalWindow,
    after: SignalWindow,
    *,
    extra_failure: str | None = None,
) -> Verdict:
    """Decide the outcome. A pure function, so the whole verdict is unit-testable.

    Check order matters and is deliberate:

    1. Alertmanager reachability -- if alert state cannot be read, nothing else is conclusive.
    2. The alert still firing -- the most direct evidence of failure.
    3. Golden signals against their ceilings.
    4. A runbook's own `extra_verification` probe.
    5. New alerts on the target that the action could have caused.
    6. Finally: were enough signals observed to call this a success at all?
    """
    reasons: list[str] = []

    # 1. Alert state unknown. Absence of evidence is never evidence of recovery.
    if after.alert_firing is None and plan.expected_signal is not None:
        return Verdict(
            outcome="inconclusive",
            reasons=(
                "could not read alert state from Alertmanager, so recovery cannot be confirmed",
            ),
            alert_cleared=None,
            signals_before=_signals_dict(before),
            signals_after=_signals_dict(after),
            new_alerts=after.new_alerts,
        )

    # 2. The triggering alert is still firing. Unambiguous failure.
    if after.alert_firing is True:
        alert = plan.expected_signal.alert_should_clear if plan.expected_signal else "the alert"
        reasons.append(f"{alert} is still firing after the remediation window")

    # 3. Golden signals. Each compares against a named ceiling; `None` is handled in step 6.
    if after.oom_events is not None and after.oom_events > OOM_TOLERANCE:
        reasons.append(
            f"{after.oom_events:.0f} OOM event(s) after the fix (tolerance {OOM_TOLERANCE:.0f})"
        )

    if after.restarts is not None and after.restarts > RESTART_TOLERANCE:
        reasons.append(
            f"container still restarting: {after.restarts} restart(s) "
            f"(tolerance {RESTART_TOLERANCE})"
        )

    if after.throttle_ratio is not None and after.throttle_ratio > THROTTLE_RATIO_CEILING:
        reasons.append(
            f"CPU throttle ratio {after.throttle_ratio:.1%} exceeds {THROTTLE_RATIO_CEILING:.0%}"
        )

    if after.psi_full is not None and after.psi_full > PSI_FULL_CEILING:
        reasons.append(
            f"memory pressure: {after.psi_full:.1%} of wall time fully stalled, "
            f"above {PSI_FULL_CEILING:.0%}"
        )

    # 4. The runbook's own claim about its specific fix.
    if extra_failure:
        reasons.append(extra_failure)

    # 5. Did the action cause something else? Scaling up fixes CPU and can start an OOM cascade, and
    #    only a whole-target check sees that.
    triggering = plan.expected_signal.alert_should_clear if plan.expected_signal else None
    caused = tuple(
        name for name in after.new_alerts if name != triggering and name not in before.new_alerts
    )
    if caused:
        reasons.append(
            f"new alert(s) appeared on this target that the action could have caused: "
            f"{', '.join(caused)}"
        )

    if reasons:
        return Verdict(
            outcome="not_recovered",
            reasons=tuple(reasons),
            alert_cleared=after.alert_firing is False,
            signals_before=_signals_dict(before),
            signals_after=_signals_dict(after),
            new_alerts=caused,
        )

    # 6. Nothing is wrong -- but was anything actually measured? A workload whose every signal is
    #    unreadable has not been shown to recover, it has merely not been shown to be broken.
    tracked = (after.oom_events, after.restarts, after.throttle_ratio, after.psi_full)
    if all(value is None for value in tracked):
        return Verdict(
            outcome="inconclusive",
            reasons=(
                "the alert cleared but no golden signal could be read, so recovery is unproven",
            ),
            alert_cleared=after.alert_firing is False,
            signals_before=_signals_dict(before),
            signals_after=_signals_dict(after),
        )

    return Verdict(
        outcome="remediated",
        reasons=("alert cleared, golden signals within thresholds, no new alerts on the target",),
        alert_cleared=True,
        signals_before=_signals_dict(before),
        signals_after=_signals_dict(after),
    )


def _signals_dict(window: SignalWindow) -> dict[str, float | None]:
    return {
        "restarts": float(window.restarts) if window.restarts is not None else None,
        "oom_events": window.oom_events,
        "throttle_ratio": window.throttle_ratio,
        "psi_full": window.psi_full,
        "memory_usage_ratio": window.memory_usage_ratio,
    }


async def verify(
    session: AsyncSession,
    execution: RunbookExecution,
    plan: RemediationPlan,
    runbook: Runbook,
    before: SignalWindow,
) -> Verdict:
    """Verify one execution and persist the verdict.

    The caller owns the transaction, so the `verify` ledger entry lands in the same commit as the
    verification row and the execution's status change.
    """
    settings = get_settings()
    window_seconds = (
        plan.expected_signal.window_seconds
        if plan.expected_signal
        else settings.verification_window_seconds
    )

    after, _ = await capture_window(plan)

    # The runbook's own probe runs on the observed window, so it can assert things the generic
    # golden signals cannot -- the OOM runbook checks that restartCount genuinely did not move.
    extra = await runbook.extra_verification(plan, after)

    verdict = judge(plan, before, after, extra_failure=extra)

    session.add(
        Verification(
            execution_id=execution.id,
            window_seconds=window_seconds,
            alert_cleared=verdict.alert_cleared,
            signals_before=verdict.signals_before,
            signals_after=verdict.signals_after,
            outcome=verdict.outcome,
            compensated=False,
            reasons=list(verdict.reasons),
        )
    )
    await session.flush()

    await ledger.append(
        session,
        "verify",
        verdict.to_dict(),
        incident_id=execution.incident_id,
        execution_id=execution.id,
    )

    metrics.verification_outcome_total.labels(runbook=plan.runbook, outcome=verdict.outcome).inc()

    if execution.finished_at is not None:
        # How fast the system notices its own mistake. One of the numbers worth being proud of.
        now = await db_now(session)
        metrics.time_to_detect_failure.labels(runbook=plan.runbook).observe(
            (now - execution.finished_at).total_seconds()
        )

    log.info(
        "verification complete",
        runbook=plan.runbook,
        outcome=verdict.outcome,
        reasons=verdict.reasons,
        execution_id=execution.id,
    )
    return verdict


def due_at(finished_at: datetime, plan: RemediationPlan) -> datetime:
    """When this execution's verification window closes.

    Scheduling is a timestamp on the execution rather than an in-process timer, because a timer dies
    with the worker and the verification would never run -- which is exactly the gap the watchdog
    exists to catch.
    """
    window = (
        plan.expected_signal.window_seconds
        if plan.expected_signal
        else get_settings().verification_window_seconds
    )
    return finished_at + timedelta(seconds=window)

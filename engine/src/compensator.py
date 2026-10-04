"""Compensation -- two paths, because one is not enough.

This is the **saga pattern with a persisted compensating transaction**, and naming it that way is
worth the two hours it takes to build.

    path 1: verification decided it did not work   -> on_verification()
    path 2: verification never completed at all    -> watchdog.sweep() -> force_revert()

Path 2 is the one that matters. A compensation owned by the process that can crash **is not a
compensation**: if the engine dies between `execute()` and `verify()`, path 1 never runs and the
cluster is left hand-edited with nobody planning to undo it. So the undo is persisted on the plan
row before the action runs, and a separate CronJob can find and execute it with no knowledge of the
process that created it.

Both paths call the same `runbook.compensate(plan)`, which is why that method must be idempotent.

A third caller, `expire()`, is not a failure path at all: an ephemeral mitigation's TTL elapsed and
the workload returns to its declared state. It is kept apart in the record -- execution status
`expired`, not `reverted` -- because the false-remediation rate counts reverts, and a mitigation
that did its job and then stood down is not a false remediation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

import ledger
import metrics
from core.incident_state import assert_transition, is_terminal
from db.models import Incident, IncidentState, RunbookExecution, Verification
from plan import RemediationPlan
from runbooks.base import ExecutionResult, Runbook

if TYPE_CHECKING:
    # Imported for typing only: `verifier` imports nothing from here, but keeping the runtime
    # import out avoids coupling the two modules' import order.
    from verifier import Verdict

log = structlog.get_logger(__name__)

Trigger = Literal["verification", "watchdog", "expiry"]

#: Terminal statuses for an execution, after compensation has been considered.
CLOSED_REMEDIATED = "remediated"
CLOSED_REVERTED = "reverted"
CLOSED_REVERT_FAILED = "revert_failed"
CLOSED_EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class CompensationOutcome:
    status: str
    reverted: bool
    detail: str
    escalate: bool
    trigger: Trigger | None = None
    ledger_hash: str | None = None
    """The `compensate` entry's hash, quoted in the escalation so evidence is one lookup away."""

    @property
    def failed_to_revert(self) -> bool:
        """The worst state the system can be in: the fix did not work AND the undo did not work.

        Always escalates loudly. There is nothing automated left to try, and a human needs to know
        the cluster is in a state neither the plan nor its compensation describes.
        """
        return self.status == CLOSED_REVERT_FAILED


async def on_verification(
    session: AsyncSession,
    incident: Incident,
    execution: RunbookExecution,
    plan: RemediationPlan,
    runbook: Runbook,
    verdict: Verdict,
) -> CompensationOutcome:
    """Path 1: the verifier reached a conclusion.

    Only `remediated` is a success. `not_recovered` and `inconclusive` both compensate and escalate;
    the verdict row keeps them apart, so "it failed" and "we could not tell" remain distinguishable
    on the scorecard even though both end in the same revert.
    """
    if verdict.outcome == "remediated":
        execution.status = CLOSED_REMEDIATED
        incident.state = assert_transition(
            incident.id, incident.state, IncidentState.REMEDIATED
        ).value
        incident.outcome = "remediated"
        await session.flush()

        # A durable plan's pull request is opened from here once GitOps support lands (Phase 6):
        # only after verification passes, so a PR never carries a fix that did not work.
        log.info("incident remediated", runbook=plan.runbook, execution_id=execution.id)
        return CompensationOutcome(
            status=CLOSED_REMEDIATED,
            reverted=False,
            detail="verified recovered",
            escalate=False,
            trigger="verification",
        )

    prefix = "could not confirm recovery" if verdict.outcome == "inconclusive" else "did not work"
    return await _revert(
        session,
        incident,
        execution,
        plan,
        runbook,
        trigger="verification",
        reason=f"{prefix}: " + "; ".join(verdict.reasons),
    )


async def force_revert(
    session: AsyncSession,
    incident: Incident,
    execution: RunbookExecution,
    plan: RemediationPlan,
    runbook: Runbook,
    *,
    reason: str,
) -> CompensationOutcome:
    """Path 2: verification never completed, so assume the worst and undo.

    Called by the watchdog. Assuming failure is the right default here -- an unverified change that
    actually worked costs one unnecessary revert, whereas an unverified change that did not work and
    is left in place costs an ongoing incident nobody is watching.
    """
    log.warning(
        "force-reverting an unverified execution",
        runbook=plan.runbook,
        execution_id=execution.id,
        reason=reason,
    )
    return await _revert(
        session, incident, execution, plan, runbook, trigger="watchdog", reason=reason
    )


async def expire(
    session: AsyncSession,
    incident: Incident,
    execution: RunbookExecution,
    plan: RemediationPlan,
    runbook: Runbook,
) -> CompensationOutcome:
    """The TTL on an ephemeral fix elapsed, so remove the imperative change.

    This is not a failure. The mitigation did its job and its window is over, and the workload goes
    back to its declared state. Automation that quietly leaves permanent hand-edits in a cluster is
    worse than no automation -- this is the mechanism that guarantees it does not.
    """
    log.info(
        "ephemeral fix expired; restoring declared state",
        runbook=plan.runbook,
        execution_id=execution.id,
    )
    return await _revert(
        session,
        incident,
        execution,
        plan,
        runbook,
        trigger="expiry",
        reason=f"ephemeral mitigation expired after {plan.ttl_seconds}s",
    )


async def _revert(
    session: AsyncSession,
    incident: Incident,
    execution: RunbookExecution,
    plan: RemediationPlan,
    runbook: Runbook,
    *,
    trigger: Trigger,
    reason: str,
) -> CompensationOutcome:
    """Run the persisted compensation and record what happened.

    The compensation comes from `plan.compensation`, decided before the action ran. It is never
    re-derived from current cluster state, because by the time this runs that state *is* the thing
    being undone.
    """
    try:
        result = await runbook.compensate(plan)
    except Exception as exc:  # noqa: BLE001
        # A raising compensation is the worst case and must still be recorded. Swallowing it would
        # leave the cluster changed with no audit trail of the failed undo.
        result = ExecutionResult(ok=False, detail=f"compensation raised: {exc}")
        log.error(
            "compensation raised",
            runbook=plan.runbook,
            execution_id=execution.id,
            error=str(exc),
        )

    digest = await ledger.append(
        session,
        "compensate",
        {
            "trigger": trigger,
            "reason": reason,
            "plan_id": execution.plan_id,
            **result.to_dict(),
        },
        incident_id=incident.id,
        execution_id=execution.id,
    )

    metrics.compensations_total.labels(runbook=plan.runbook, trigger=trigger).inc()

    execution.compensated = True
    await _mark_verification_compensated(session, execution)

    if not result.ok:
        execution.status = CLOSED_REVERT_FAILED
        execution.detail = f"{reason} -> COMPENSATION FAILED: {result.detail}"
        _close_incident(incident, IncidentState.ESCALATED, "revert_failed")
        await session.flush()

        log.error(
            "compensation failed; cluster is in an undescribed state",
            runbook=plan.runbook,
            execution_id=execution.id,
            detail=result.detail,
        )
        return CompensationOutcome(
            status=CLOSED_REVERT_FAILED,
            reverted=False,
            detail=result.detail,
            escalate=True,
            trigger=trigger,
            ledger_hash=digest,
        )

    execution.detail = f"{reason} -> {result.detail}"
    if trigger == "expiry":
        execution.status = CLOSED_EXPIRED
        # Usually the incident closed as remediated long ago, and terminal states are terminal: the
        # expiry is a fact about the mitigation, not a new verdict on the incident.
        if not is_terminal(incident.state):
            _close_incident(incident, IncidentState.REVERTED, "expired")
        await session.flush()
        return CompensationOutcome(
            status=CLOSED_EXPIRED,
            reverted=True,
            detail=result.detail,
            escalate=False,
            trigger=trigger,
            ledger_hash=digest,
        )

    # The system caught its own mistake. `reverted` is the scorecard's bucket for exactly this, and
    # it still escalates: the original problem is back, and a human needs the evidence.
    execution.status = CLOSED_REVERTED
    _close_incident(incident, IncidentState.REVERTED, "reverted")
    await session.flush()
    return CompensationOutcome(
        status=CLOSED_REVERTED,
        reverted=True,
        detail=result.detail,
        escalate=True,
        trigger=trigger,
        ledger_hash=digest,
    )


def _close_incident(incident: Incident, state: IncidentState, outcome: str) -> None:
    incident.state = assert_transition(incident.id, incident.state, state).value
    incident.outcome = outcome


async def _mark_verification_compensated(
    session: AsyncSession, execution: RunbookExecution
) -> None:
    """Flag the verification row, so the false-remediation rate can be computed from one table.

    `reverted_after_verification / executed` is the trust metric nobody else reports, and it needs
    the compensation flag to sit next to the verdict that caused it.

    An UPDATE rather than a walk of the session's identity map: the map holds clean objects only
    weakly, so the verification row flushed a moment earlier may already be gone from it, and the
    flag would silently never be set.
    """
    await session.execute(
        update(Verification)
        .where(Verification.execution_id == execution.id)
        .values(compensated=True)
        .execution_options(synchronize_session="fetch")
    )

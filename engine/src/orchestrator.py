"""The orchestrator.

Every incident flows through `handle()`, and that single funnel is what makes safety **structural**
rather than a matter of each runbook remembering to be careful. A runbook cannot skip the policy
gate, cannot act without registering its compensation first, and cannot mark itself successful --
because it is never the thing calling those steps.

The stage order is fixed:

    plan -> policy gate -> [shadow | approval | execute] -> schedule verification

**Every branch is audited, including the ones that do nothing.** Refusals, denials and shadow
records each get a ledger entry, because "the system correctly decided not to act" is a data point
the efficacy scorecard needs, and a silent no-op is indistinguishable from a bug.

One ordering detail carries most of the safety weight: `register_compensation()` runs *before*
`execute()`. Deriving the undo afterwards would mean reconstructing the prior state from a cluster
that has already changed, and a compensation owned by a process that can crash is not a compensation
at all -- which is why it is persisted rather than held in memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

import ledger
import metrics
import policy as policy_module
from bundle import IncidentBundle, Target
from cache import locks
from config import get_settings
from core.incident_state import IncidentState, assert_transition
from db.models import Incident, RemediationPlanRow, RunbookExecution
from plan import RemediationPlan
from policy import BudgetSnapshot, PolicyDecision, PromotionSnapshot, TargetView
from runbooks.base import ExecutionResult, Runbook

log = structlog.get_logger(__name__)

Outcome = Literal[
    "refused", "denied", "shadow", "awaiting_approval", "executed", "execute_failed", "blocked"
]


@dataclass(frozen=True, slots=True)
class HandleResult:
    """What the orchestrator did, for the caller and for the metrics."""

    outcome: Outcome
    plan: RemediationPlan
    decision: PolicyDecision | None = None
    execution_id: int | None = None
    plan_id: str | None = None
    detail: str = ""

    @property
    def acted(self) -> bool:
        return self.outcome == "executed"


async def handle(
    session: AsyncSession,
    incident: Incident,
    bundle: IncidentBundle,
    runbook: Runbook,
    target: Target,
    *,
    budget: BudgetSnapshot | None = None,
    promotion: PromotionSnapshot | None = None,
) -> HandleResult:
    """Run one incident through the pipeline.

    The caller owns the transaction, so every ledger append here lands in the same commit as the
    state change it records.
    """
    settings = get_settings()

    await ledger.append(
        session,
        "plan_requested",
        {"runbook": runbook.name, "target": target.key, "alertname": bundle.cause_alert.alertname},
        incident_id=incident.id,
    )
    incident.state = assert_transition(incident.id, incident.state, IncidentState.PLANNING).value

    # --- stage 1: plan ---------------------------------------------------------------------------
    # A pure function of the bundle. No cluster reads happen inside it, which is what keeps the
    # decision eval hermetic and the determinism digest stable.
    plan = runbook.plan(bundle, target)
    plan = plan.with_plan_id(plan.plan_id or "")

    await ledger.append(
        session, "plan", plan.to_dict(), incident_id=incident.id
    )

    if plan.is_refusal:
        # A refusal is a first-class outcome, not an error. It is recorded, counted and reported.
        row = await _persist_plan(session, incident, plan, PolicyDecision(effect="deny"))
        metrics.plans_total.labels(runbook=runbook.name, decision="refuse").inc()
        metrics.incidents_total.labels(outcome="refused").inc()
        incident.state = assert_transition(
            incident.id, incident.state, IncidentState.ESCALATED
        ).value
        incident.outcome = "refused"
        log.info(
            "runbook refused", runbook=runbook.name, reason=plan.refusal_reason, target=target.key
        )
        return HandleResult(
            outcome="refused", plan=plan, plan_id=row.id, detail=plan.refusal_reason or ""
        )

    # --- stage 2: the policy gate ----------------------------------------------------------------
    decision = policy_module.evaluate(
        plan,
        _target_view(target),
        budget or BudgetSnapshot(),
        promotion or PromotionSnapshot(),
    )
    await ledger.append(
        session, "policy", decision.to_dict(), incident_id=incident.id
    )
    plan_row = await _persist_plan(session, incident, plan, decision)
    metrics.plans_total.labels(runbook=runbook.name, decision=decision.effect).inc()

    if decision.denied:
        metrics.incidents_total.labels(outcome="denied").inc()
        incident.state = assert_transition(
            incident.id, incident.state, IncidentState.ESCALATED
        ).value
        incident.outcome = "denied"
        log.info("policy denied", runbook=runbook.name, reasons=decision.reasons)
        return HandleResult(
            outcome="denied",
            plan=plan,
            decision=decision,
            plan_id=plan_row.id,
            detail="; ".join(decision.messages),
        )

    # --- stage 3: shadow mode --------------------------------------------------------------------
    mode = (promotion or PromotionSnapshot()).mode
    if mode == "shadow":
        # Records the plan it WOULD have run, and touches nothing. The agreement rate between these
        # and what a human actually did is the drift monitor.
        execution = RunbookExecution(
            incident_id=incident.id,
            plan_id=plan_row.id,
            runbook=runbook.name,
            status="shadow",
            mode="shadow",
            state_before={},
            state_after={},
            detail=plan.summary,
            durability=str(plan.durability),
        )
        session.add(execution)
        await session.flush()

        await ledger.append(
            session,
            "shadow",
            {"plan_id": plan_row.id, "summary": plan.summary, "would_have": plan.describe_actions()},
            incident_id=incident.id,
            execution_id=execution.id,
        )
        metrics.incidents_total.labels(outcome="shadow").inc()
        # Shadow closes the incident without acting: an honest "nothing was done".
        incident.state = assert_transition(
            incident.id, incident.state, IncidentState.REMEDIATED
        ).value
        incident.outcome = "shadow"
        log.info("shadow plan recorded", runbook=runbook.name, summary=plan.summary)
        return HandleResult(
            outcome="shadow",
            plan=plan,
            decision=decision,
            execution_id=execution.id,
            plan_id=plan_row.id,
        )

    # --- stage 4: approval ------------------------------------------------------------------------
    execution = RunbookExecution(
        incident_id=incident.id,
        plan_id=plan_row.id,
        runbook=runbook.name,
        status="queued",
        mode="approval" if decision.needs_approval else "auto",
        state_before={},
        state_after={},
        durability=str(plan.durability),
        gitops_managed=plan.gitops_managed,
    )
    session.add(execution)
    await session.flush()

    if decision.needs_approval:
        # The approval row is created by `approvals.request()` in the same transaction, and
        # PostgreSQL owns the deadline. Nothing executes here; expiry escalates rather than acting.
        incident.state = assert_transition(
            incident.id, incident.state, IncidentState.AWAITING_APPROVAL
        ).value
        execution.status = "awaiting_approval"
        await ledger.append(
            session,
            "approve",
            {
                "stage": "requested",
                "plan_id": plan_row.id,
                "reasons": list(decision.reasons),
                "messages": list(decision.messages),
            },
            incident_id=incident.id,
            execution_id=execution.id,
        )
        log.info(
            "approval required", runbook=runbook.name, reasons=decision.reasons, target=target.key
        )
        return HandleResult(
            outcome="awaiting_approval",
            plan=plan,
            decision=decision,
            execution_id=execution.id,
            plan_id=plan_row.id,
            detail="; ".join(decision.messages),
        )

    # --- stage 5: execute -------------------------------------------------------------------------
    return await execute_approved(
        session, incident, plan, runbook, execution, plan_row.id, decision
    )


async def execute_approved(
    session: AsyncSession,
    incident: Incident,
    plan: RemediationPlan,
    runbook: Runbook,
    execution: RunbookExecution,
    plan_id: str,
    decision: PolicyDecision,
) -> HandleResult:
    """The only mutating window in the system.

    Reached either directly on the auto path or from the Slack approval callback. Both routes go
    through here so the lock, the compensation registration and the ledger entry cannot be skipped
    by taking a different path in.
    """
    settings = get_settings()

    incident.state = assert_transition(
        incident.id, incident.state, IncidentState.EXECUTING
    ).value
    execution.status = "executing"
    await session.flush()

    try:
        async with locks.per_resource(
            plan.target_kind,
            plan.target_namespace,
            plan.target_name,
            token=plan_id,
            ttl_seconds=settings.lock_ttl_seconds,
        ):
            # Persisted BEFORE the action. This is the saga's compensating transaction, and it is
            # already durable by the time anything changes in the cluster.
            await _register_compensation(session, execution, plan)

            result = await runbook.execute(plan)

            await ledger.append(
                session,
                "execute",
                {"plan_id": plan_id, **result.to_dict()},
                incident_id=incident.id,
                execution_id=execution.id,
            )
    except locks.LockNotAcquired as exc:
        # Another plan holds this resource. Not an error: the other incident is probably fixing the
        # same thing, and forcing our way in is how two runbooks produce an unpredictable state.
        execution.status = "blocked"
        await ledger.append(
            session,
            "execute",
            {"plan_id": plan_id, "ok": False, "detail": str(exc), "blocked": True},
            incident_id=incident.id,
            execution_id=execution.id,
        )
        incident.state = assert_transition(
            incident.id, incident.state, IncidentState.ESCALATED
        ).value
        incident.outcome = "blocked"
        return HandleResult(
            outcome="blocked",
            plan=plan,
            decision=decision,
            execution_id=execution.id,
            plan_id=plan_id,
            detail=str(exc),
        )

    execution.state_before = result.state_before
    execution.state_after = result.state_after
    execution.detail = result.detail

    if not result.ok:
        # The action did not apply. That is a failure of *execution*, distinct from a fix that
        # applied but did not work -- which only verification can tell us.
        execution.status = "failed"
        incident.state = assert_transition(
            incident.id, incident.state, IncidentState.ESCALATED
        ).value
        incident.outcome = "escalated"
        metrics.incidents_total.labels(outcome="escalated").inc()
        log.warning("execution failed", runbook=runbook.name, detail=result.detail)
        return HandleResult(
            outcome="execute_failed",
            plan=plan,
            decision=decision,
            execution_id=execution.id,
            plan_id=plan_id,
            detail=result.detail,
        )

    # Applied. NOT successful -- success means the incident is over, and that is the verifier's call.
    execution.status = "awaiting_verification"
    incident.state = assert_transition(
        incident.id, incident.state, IncidentState.VERIFYING
    ).value
    await session.flush()

    log.info(
        "executed, awaiting verification",
        runbook=runbook.name,
        detail=result.detail,
        window_seconds=(plan.expected_signal.window_seconds if plan.expected_signal else None),
    )
    return HandleResult(
        outcome="executed",
        plan=plan,
        decision=decision,
        execution_id=execution.id,
        plan_id=plan_id,
        detail=result.detail,
    )


# --- helpers ---------------------------------------------------------------------------------------


def _target_view(target: Target) -> TargetView:
    """Project the bundle's target onto the narrow view policy rules may read."""
    return TargetView(
        kind=target.kind,
        name=target.name,
        namespace=target.namespace,
        replicas=target.replicas,
        labels=dict(target.labels),
        annotations=dict(target.annotations),
    )


async def _persist_plan(
    session: AsyncSession,
    incident: Incident,
    plan: RemediationPlan,
    decision: PolicyDecision,
) -> RemediationPlanRow:
    """Write the plan and the decision that gated it, together.

    A plan without its decision, or a decision without its plan, would leave the audit trail unable
    to answer "why was this allowed?".
    """
    row = RemediationPlanRow(
        incident_id=incident.id,
        runbook=plan.runbook,
        planner_version=plan.planner_version or get_settings().planner_version,
        target_kind=plan.target_kind,
        target_namespace=plan.target_namespace,
        target_name=plan.target_name,
        actions=[_call_to_dict(c) for c in plan.actions],
        compensation=[_call_to_dict(c) for c in plan.compensation],
        reversibility=str(plan.reversibility),
        blast_radius={
            "pods": plan.blast_radius.pods,
            "nodes": plan.blast_radius.nodes,
            "namespaces": list(plan.blast_radius.namespaces),
            "user_facing": plan.blast_radius.user_facing,
            "score": plan.blast_radius.score(),
        },
        durability=str(plan.durability),
        ttl_seconds=plan.ttl_seconds,
        expected_signal=(
            {
                "alert_should_clear": plan.expected_signal.alert_should_clear,
                "queries": dict(plan.expected_signal.queries),
                "thresholds": dict(plan.expected_signal.thresholds),
                "window_seconds": plan.expected_signal.window_seconds,
            }
            if plan.expected_signal
            else {}
        ),
        confidence=plan.confidence,
        summary=plan.summary,
        cost_delta_inr_month=plan.cost_delta_inr_month,
        refusal_reason=plan.refusal_reason,
        policy_decision=decision.effect,
        policy_reasons=list(decision.reasons),
    )
    session.add(row)
    await session.flush()
    return row


def _call_to_dict(call: Any) -> dict[str, Any]:
    return {
        "verb": call.verb,
        "resource": call.resource,
        "name": call.name,
        "namespace": call.namespace,
        "body": call.body,
        "subresource": call.subresource,
    }


async def _register_compensation(
    session: AsyncSession, execution: RunbookExecution, plan: RemediationPlan
) -> None:
    """Make the undo durable before the action runs.

    The compensation already lives on the plan row; recording the expiry here is what lets the
    watchdog find and reverse this execution without needing the process that created it to be alive.
    """
    from datetime import timedelta

    from db.connection import db_now

    if plan.ttl_seconds:
        now = await db_now(session)
        execution.expires_at = now + timedelta(seconds=plan.ttl_seconds)
    await session.flush()


def summarise_for_slack(result: HandleResult) -> str:
    """One line per outcome, written for a human reading it on a phone."""
    plan = result.plan
    match result.outcome:
        case "refused":
            return f"REFUSED {plan.runbook} on {plan.target_key}: {result.detail}"
        case "denied":
            return f"DENIED by policy: {plan.target_key} -- {result.detail}"
        case "shadow":
            return f"SHADOW: would have {plan.summary}"
        case "awaiting_approval":
            return f"APPROVAL NEEDED: {plan.summary} ({result.detail})"
        case "executed":
            return f"ACTED: {plan.summary} -- verifying recovery now"
        case "execute_failed":
            return f"FAILED to apply: {plan.summary} -- {result.detail}"
        case "blocked":
            return f"BLOCKED: {result.detail}"
    return plan.summary

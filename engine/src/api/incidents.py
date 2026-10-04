"""Read API for the dashboard: incidents, their full evidence trail, and the efficacy scorecard.

Every endpoint here is a GET and none of them can change anything, which is what lets the public
demo point the dashboard at a read-only seeded engine. The dashboard's Route Handlers proxy these;
nothing in the front end talks to PostgreSQL.

Two things are computed here rather than in the browser, so there is one definition of each:

* **The trail** -- correlate, plan, policy, gate, execute, verify, outcome -- with every stage's own
  outcome, including the stages that did nothing. Refused, denied and shadow are first-class.
* **The outcome vocabulary.** The engine records finer-grained outcomes than a badge needs; they
  are normalised to the ten the dashboard's `StatusBadge` union declares, with the raw value kept
  alongside so nothing is hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.connection import session_scope
from db.models import (
    AlertEvent,
    Approval,
    AuditLedger,
    Incident,
    IncidentState,
    RemediationPlanRow,
    RunbookExecution,
    Verification,
)

router = APIRouter(tags=["incidents"])

Outcome = Literal[
    "remediated",
    "not_recovered",
    "inconclusive",
    "reverted",
    "revert_failed",
    "escalated",
    "refused",
    "denied",
    "shadow",
    "pending",
]

#: Raw incident outcomes the engine writes, mapped to the badge vocabulary. Anything not listed is
#: an escalation -- the system handing the incident to a human -- which is never styled a failure.
_OUTCOMES: dict[str, Outcome] = {
    "remediated": "remediated",
    "reverted": "reverted",
    "expired": "reverted",
    "revert_failed": "revert_failed",
    "refused": "refused",
    "denied": "denied",
    "shadow": "shadow",
}

FILTER_SCAN = 1000

StageState = Literal["passed", "stopped", "corrected", "failed", "pending", "skipped"]


@dataclass(frozen=True, slots=True)
class Stage:
    stage: str
    state: StageState
    label: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "stage": self.stage,
            "state": self.state,
            "label": self.label,
            "detail": self.detail,
        }


def outcome_of(incident: Incident) -> Outcome:
    if incident.state not in {
        s.value for s in (IncidentState.REMEDIATED, IncidentState.REVERTED, IncidentState.ESCALATED)
    }:
        return "pending"
    return _OUTCOMES.get(incident.outcome or "", "escalated")


def trail(
    incident: Incident,
    plan: RemediationPlanRow | None,
    execution: RunbookExecution | None,
    approval: Approval | None,
    verification: Verification | None,
) -> list[Stage]:
    """The pipeline as it ran for this incident, stage by stage."""
    stages = [
        Stage(
            "correlate",
            "passed",
            f"{incident.alert_count} alert(s)",
            incident.correlation_reason,
        )
    ]
    outcome = outcome_of(incident)

    if plan is None:
        state: StageState = "pending" if outcome == "pending" else "stopped"
        stages.append(Stage("plan", state, "no plan", "escalated without a runbook"))
        return [*stages, Stage("outcome", _final(outcome), outcome)]

    if plan.refusal_reason:
        stages.append(Stage("plan", "stopped", "refused", plan.refusal_reason))
        return [*stages, Stage("outcome", "stopped", "refused")]
    stages.append(Stage("plan", "passed", plan.runbook, plan.summary))

    decision = plan.policy_decision
    if decision == "deny":
        stages.append(Stage("policy", "stopped", "denied", ", ".join(plan.policy_reasons)))
        return [*stages, Stage("outcome", "stopped", "denied")]
    stages.append(
        Stage(
            "policy",
            "passed",
            "approval required" if decision == "require_approval" else "allowed",
            ", ".join(plan.policy_reasons),
        )
    )

    if execution is not None and execution.mode == "shadow":
        stages.append(Stage("gate", "stopped", "shadow", "recorded what it would have done"))
        return [*stages, Stage("outcome", "stopped", "shadow")]

    if approval is not None:
        gate = {
            "approved": Stage("gate", "passed", "approved", approval.decided_by or ""),
            "rejected": Stage("gate", "stopped", "rejected", approval.decided_by or ""),
            "expired": Stage("gate", "stopped", "expired", "nobody answered; escalated"),
        }.get(approval.status, Stage("gate", "pending", "awaiting approval", ""))
        stages.append(gate)
        if gate.state == "pending":
            return stages  # still in progress: no outcome to show yet
        if gate.state != "passed":
            return [*stages, Stage("outcome", _final(outcome), outcome)]
    else:
        stages.append(Stage("gate", "passed", "auto", "within policy, no human needed"))

    status = execution.status if execution is not None else None
    if status in (None, "queued", "awaiting_approval", "executing"):
        return [*stages, Stage("execute", "pending", status or "queued")]
    if status in ("failed", "blocked"):
        detail = (execution.detail or "") if execution else ""
        stages.append(Stage("execute", "failed", status, detail))
        return [*stages, Stage("outcome", "stopped", outcome)]
    stages.append(
        Stage("execute", "passed", "applied", (execution.detail or "") if execution else "")
    )

    if verification is None:
        stages.append(Stage("verify", "pending", "verifying", ""))
        return stages
    stages.append(
        Stage(
            "verify",
            "passed" if verification.outcome == "remediated" else "failed",
            verification.outcome,
            "; ".join(verification.reasons),
        )
    )
    if execution is not None and execution.compensated and status != "expired":
        stages.append(
            Stage("compensate", "corrected" if status == "reverted" else "failed", status or "")
        )
    return [*stages, Stage("outcome", _final(outcome), outcome)]


def _final(outcome: Outcome) -> StageState:
    return {
        "remediated": "passed",
        "reverted": "corrected",
        "revert_failed": "failed",
        "pending": "pending",
    }.get(outcome, "stopped")  # type: ignore[return-value]


# --- list -----------------------------------------------------------------------------------------


@router.get("/api/incidents", summary="Incidents, newest first, with their pipeline trail")
async def list_incidents(
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    namespace: str | None = None,
    outcome: Outcome | None = None,
) -> dict[str, Any]:
    async with session_scope() as session:
        stmt = select(Incident).order_by(Incident.opened_at.desc(), Incident.id.desc())
        if namespace:
            stmt = stmt.where(Incident.namespace == namespace)

        if outcome is None:
            total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
            page = list((await session.execute(stmt.limit(limit).offset(offset))).scalars())
        else:
            # The outcome is normalised in Python, so the filter is too. Bounded: this is a
            # dashboard view of recent incidents, not a reporting query.
            matching = [
                i
                for i in (await session.execute(stmt.limit(FILTER_SCAN))).scalars()
                if outcome_of(i) == outcome
            ]
            total, page = len(matching), matching[offset : offset + limit]

        items = [await _summary(session, incident) for incident in page]
        return {"items": items, "total": int(total or 0)}


async def _latest(session: AsyncSession, incident_id: int) -> tuple[Any, Any, Any, Any]:
    plan = (
        await session.execute(
            select(RemediationPlanRow)
            .where(RemediationPlanRow.incident_id == incident_id)
            .order_by(RemediationPlanRow.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    execution = (
        await session.execute(
            select(RunbookExecution)
            .where(RunbookExecution.incident_id == incident_id)
            .order_by(RunbookExecution.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    approval = verification = None
    if execution is not None:
        approval = (
            await session.execute(select(Approval).where(Approval.execution_id == execution.id))
        ).scalar_one_or_none()
        verification = (
            await session.execute(
                select(Verification)
                .where(Verification.execution_id == execution.id)
                .order_by(Verification.checked_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return plan, execution, approval, verification


async def _summary(session: AsyncSession, incident: Incident) -> dict[str, Any]:
    plan, execution, approval, verification = await _latest(session, incident.id)
    return {
        "id": incident.id,
        "state": incident.state,
        "outcome": outcome_of(incident),
        "outcome_detail": incident.outcome,
        "cause_alertname": incident.cause_alertname,
        "cause_scope": incident.cause_scope,
        "target_key": incident.target_key,
        "namespace": incident.namespace,
        "opened_at": incident.opened_at,
        "alert_count": incident.alert_count,
        "correlation_reason": incident.correlation_reason,
        "runbook": plan.runbook if plan else None,
        "summary": plan.summary if plan else None,
        "mode": execution.mode if execution else None,
        "trail": [s.to_dict() for s in trail(incident, plan, execution, approval, verification)],
    }


# --- detail ---------------------------------------------------------------------------------------


@router.get("/api/incidents/{incident_id}", summary="One incident with all of its evidence")
async def get_incident(incident_id: int) -> dict[str, Any]:
    async with session_scope() as session:
        incident = await session.get(Incident, incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail=f"no incident {incident_id}")

        alerts = (
            await session.execute(
                select(AlertEvent)
                .where(AlertEvent.incident_id == incident_id)
                .order_by(AlertEvent.received_at)
            )
        ).scalars()
        plans = (
            await session.execute(
                select(RemediationPlanRow)
                .where(RemediationPlanRow.incident_id == incident_id)
                .order_by(RemediationPlanRow.created_at)
            )
        ).scalars()
        executions = list(
            (
                await session.execute(
                    select(RunbookExecution)
                    .where(RunbookExecution.incident_id == incident_id)
                    .order_by(RunbookExecution.id)
                )
            ).scalars()
        )
        ids = [e.id for e in executions]
        approvals = (
            (await session.execute(select(Approval).where(Approval.execution_id.in_(ids))))
            .scalars()
            .all()
            if ids
            else []
        )
        verifications = (
            (
                await session.execute(
                    select(Verification)
                    .where(Verification.execution_id.in_(ids))
                    .order_by(Verification.checked_at)
                )
            )
            .scalars()
            .all()
            if ids
            else []
        )
        ledger_rows = (
            await session.execute(
                select(AuditLedger)
                .where(AuditLedger.incident_id == incident_id)
                .order_by(AuditLedger.seq)
            )
        ).scalars()

        summary = await _summary(session, incident)
        return {
            **summary,
            "closed_at": incident.closed_at,
            "alerts": [
                {
                    "id": a.id,
                    "alertname": a.alertname,
                    "status": a.status,
                    "role": a.role,
                    "severity": a.severity,
                    "fingerprint": a.fingerprint,
                    "namespace": a.namespace,
                    "pod": a.pod_name,
                    "node": a.node_name,
                    "starts_at": a.starts_at,
                    "received_at": a.received_at,
                }
                for a in alerts
            ],
            "plans": [
                {
                    "id": p.id,
                    "runbook": p.runbook,
                    "planner_version": p.planner_version,
                    "summary": p.summary,
                    "target_key": f"{p.target_namespace or '-'}/{p.target_kind}/{p.target_name}",
                    "actions": p.actions,
                    "compensation": p.compensation,
                    "reversibility": p.reversibility,
                    "durability": p.durability,
                    "ttl_seconds": p.ttl_seconds,
                    "blast_radius": p.blast_radius,
                    "expected_signal": p.expected_signal,
                    "confidence": float(p.confidence),
                    "cost_delta_inr_month": float(p.cost_delta_inr_month),
                    "refusal_reason": p.refusal_reason,
                    "policy_decision": p.policy_decision,
                    "policy_reasons": list(p.policy_reasons),
                    "created_at": p.created_at,
                }
                for p in plans
            ],
            "executions": [
                {
                    "id": e.id,
                    "plan_id": e.plan_id,
                    "runbook": e.runbook,
                    "status": e.status,
                    "mode": e.mode,
                    "detail": e.detail,
                    "state_before": e.state_before,
                    "state_after": e.state_after,
                    "signals_before": e.signals_before,
                    "started_at": e.started_at,
                    "finished_at": e.finished_at,
                    "expires_at": e.expires_at,
                    "durability": e.durability,
                    "gitops_managed": e.gitops_managed,
                    "gitops_paused": e.gitops_paused,
                    "compensated": e.compensated,
                    "pr_url": e.pr_url,
                }
                for e in executions
            ],
            "approvals": [_approval(a) for a in approvals],
            "verifications": [
                {
                    "execution_id": v.execution_id,
                    "outcome": v.outcome,
                    "alert_cleared": v.alert_cleared,
                    "signals_before": v.signals_before,
                    "signals_after": v.signals_after,
                    "reasons": list(v.reasons),
                    "compensated": v.compensated,
                    "window_seconds": v.window_seconds,
                    "checked_at": v.checked_at,
                }
                for v in verifications
            ],
            "ledger": [
                {
                    "seq": row.seq,
                    "occurred_at": row.occurred_at,
                    "event_type": row.event_type,
                    "actor": row.actor,
                    "execution_id": row.execution_id,
                    "payload": row.payload,
                    "prev_hash": row.prev_hash,
                    "hash": row.hash,
                }
                for row in ledger_rows
            ],
        }


def _approval(a: Approval) -> dict[str, Any]:
    return {
        "execution_id": a.execution_id,
        "incident_id": a.incident_id,
        "status": a.status,
        "requested_at": a.requested_at,
        "expires_at": a.expires_at,
        "decided_by": a.decided_by,
        "decided_at": a.decided_at,
        "reason": a.reason,
    }


# --- the efficacy scorecard -----------------------------------------------------------------------


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    """A rate with its honest denominator, so the dashboard can show both."""
    return {
        "value": (numerator / denominator) if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
    }


@router.get("/api/stats", summary="The efficacy scorecard, with denominators")
async def stats() -> dict[str, Any]:
    async with session_scope() as session:
        incidents = list((await session.execute(select(Incident))).scalars())
        total = len(incidents)

        latest_mode: dict[int, str] = {}
        for incident_id, mode in (
            await session.execute(
                select(RunbookExecution.incident_id, RunbookExecution.mode).order_by(
                    RunbookExecution.id
                )
            )
        ).all():
            latest_mode[incident_id] = mode

        remediated = [i for i in incidents if i.outcome == "remediated"]
        auto = [i for i in remediated if latest_mode.get(i.id) == "auto"]
        assisted = [i for i in remediated if latest_mode.get(i.id) == "approval"]
        escalated = [i for i in incidents if i.state == IncidentState.ESCALATED.value]

        executed = await session.scalar(
            select(func.count())
            .select_from(RunbookExecution)
            .where(RunbookExecution.finished_at.isnot(None), RunbookExecution.mode != "shadow")
        )
        reverted = await session.scalar(
            select(func.count(func.distinct(Verification.execution_id))).where(
                Verification.compensated.is_(True)
            )
        )
        plans_total = await session.scalar(select(func.count()).select_from(RemediationPlanRow))
        refused = await session.scalar(
            select(func.count())
            .select_from(RemediationPlanRow)
            .where(
                (RemediationPlanRow.refusal_reason.isnot(None))
                | (RemediationPlanRow.policy_decision == "deny")
            )
        )

        verdicts = (
            await session.execute(
                select(
                    Verification.outcome,
                    Verification.checked_at,
                    RunbookExecution.finished_at,
                    RunbookExecution.mode,
                    Incident.opened_at,
                )
                .join(RunbookExecution, RunbookExecution.id == Verification.execution_id)
                .join(Incident, Incident.id == RunbookExecution.incident_id)
            )
        ).all()

    def seconds(pairs: list[tuple[datetime, datetime]]) -> float | None:
        values = [(end - start).total_seconds() for start, end in pairs if start and end]
        return median(values) if values else None

    return {
        "incidents": total,
        "auto_resolution": _ratio(len(auto), total),
        "assisted_resolution": _ratio(len(assisted), total),
        "escalation": _ratio(len(escalated), total),
        # The trust metric: of the changes HealOps made, how many it had to take back.
        "false_remediation": _ratio(int(reverted or 0), int(executed or 0)),
        "refusal": _ratio(int(refused or 0), int(plans_total or 0)),
        "mttr_auto_seconds": seconds(
            [(o, c) for out, c, _f, mode, o in verdicts if out == "remediated" and mode == "auto"]
        ),
        "mttr_assisted_seconds": seconds(
            [
                (o, c)
                for out, c, _f, mode, o in verdicts
                if out == "remediated" and mode == "approval"
            ]
        ),
        "time_to_detect_failure_seconds": seconds(
            [(f, c) for out, c, f, _mode, _o in verdicts if out != "remediated"]
        ),
    }

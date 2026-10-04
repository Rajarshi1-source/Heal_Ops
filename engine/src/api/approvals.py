"""`POST /api/approvals/{execution_id}` -- the approval decision endpoint.

Serves the dashboard's approve path; the Slack button reaches the same `approval_manager.resolve()`
through the Bolt adapter. One place records a human decision, with one transaction boundary and one
ledger entry format.

**Authenticated, and fail-closed when unconfigured.** The decision body names who decided, and
a name in a request body is a claim, not an identity. So the endpoint requires a bearer token that
only the dashboard's server-side Route Handler holds; with no token configured it refuses every
decision, and Slack -- whose requests Bolt verifies against the signing secret -- is the only way to
approve.

**Idempotent by construction.** A double click, or a retried request, is one decision:
`resolve()` returns the original outcome rather than overwriting it, and only the call that moved
the execution out of `awaiting_approval` enqueues it.

Granting approval does **not** execute anything here. It records the decision and enqueues the
execution, so the act of approving cannot block on a Kubernetes call and a slow cluster cannot make
the click look like it failed.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Body, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from cache import streams
from config import get_settings
from core import approval_manager
from db.connection import transaction
from db.models import RunbookExecution

log = structlog.get_logger(__name__)
router = APIRouter(tags=["approvals"])


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "reject"]
    decided_by: str = Field(description="The dashboard's authenticated identity.")
    slack_message_ts: str | None = None
    slack_channel: str | None = None


class ApprovalResult(BaseModel):
    execution_id: int
    status: str
    allowed: bool
    detail: str
    enqueued: bool


def require_approver(authorization: str | None) -> None:
    """Constant-time bearer check. No configured token means no caller is trusted."""
    settings = get_settings()
    if settings.read_only:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="this deployment is read-only")

    expected = settings.approvals_api_token
    if expected is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="approvals via the API are disabled; decide from the Slack card",
        )

    scheme, _, token = (authorization or "").partition(" ")
    presented = token if scheme.lower() == "bearer" else ""
    if not hmac.compare_digest(presented, expected.get_secret_value()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid or missing token")


@router.post(
    "/api/approvals/{execution_id}",
    summary="Record a human approval decision",
    response_model=ApprovalResult,
)
async def decide(
    execution_id: int,
    body: Annotated[ApprovalDecision, Body()],
    authorization: Annotated[str | None, Header()] = None,
) -> ApprovalResult:
    require_approver(authorization)
    approved = body.decision == "approve"

    async with transaction() as session:
        execution = await session.get(RunbookExecution, execution_id, with_for_update=True)
        if execution is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no execution {execution_id}",
            )

        resolution = await approval_manager.resolve(
            session,
            execution,
            approved=approved,
            decided_by=body.decided_by,
            slack_message_ts=body.slack_message_ts,
            slack_channel=body.slack_channel,
        )
        incident_id = execution.incident_id

    if resolution.enqueue:
        # After commit: a worker that picks this up must find the `queued` status already durable.
        await streams.enqueue(
            "execute_approved", {"execution_id": execution_id, "incident_id": incident_id}
        )

    log.info(
        "approval recorded",
        execution_id=execution_id,
        decision=body.decision,
        decided_by=body.decided_by,
        allowed=resolution.authorisation.allowed,
        enqueued=resolution.enqueue,
    )

    return ApprovalResult(
        execution_id=execution_id,
        status=resolution.execution_status,
        allowed=resolution.authorisation.allowed,
        detail=resolution.authorisation.reason,
        enqueued=resolution.enqueue,
    )


@router.get("/api/approvals", summary="Approvals, soonest deadline first, with their plans")
async def list_approvals(status_filter: str = "pending", limit: int = 50) -> dict[str, object]:
    """What the dashboard's approval page shows: the same facts as the Slack card.

    `expires_at` comes from PostgreSQL. The dashboard renders its countdown from this value and
    never computes a deadline itself, because the database owns expiry.
    """
    import plan_store
    from db.connection import session_scope
    from db.models import Approval, RemediationPlanRow

    async with session_scope() as session:
        stmt = select(Approval).order_by(Approval.expires_at).limit(min(limit, 200))
        if status_filter != "all":
            stmt = stmt.where(Approval.status == status_filter)
        items = []
        for approval in (await session.execute(stmt)).scalars():
            execution = await session.get(RunbookExecution, approval.execution_id)
            row = (
                await session.get(RemediationPlanRow, execution.plan_id)
                if execution is not None and execution.plan_id
                else None
            )
            plan = plan_store.rehydrate(row, execution) if row is not None else None
            items.append(
                {
                    "execution_id": approval.execution_id,
                    "incident_id": approval.incident_id,
                    "status": approval.status,
                    "requested_at": approval.requested_at,
                    "expires_at": approval.expires_at,
                    "decided_by": approval.decided_by,
                    "reason": approval.reason,
                    "plan": _card(plan) if plan is not None else None,
                }
            )
    return {"items": items}


def _card(plan: Any) -> dict[str, object]:
    signal = plan.expected_signal
    return {
        "runbook": plan.runbook,
        "summary": plan.summary,
        "target_key": plan.target_key,
        "actions": plan.describe_actions(),
        "compensation": [c.describe() for c in plan.compensation],
        "reversibility": str(plan.reversibility),
        "blast_radius_score": plan.blast_radius.score(),
        "blast_radius": {
            "pods": plan.blast_radius.pods,
            "nodes": plan.blast_radius.nodes,
            "user_facing": plan.blast_radius.user_facing,
        },
        "cost_delta_inr_month": plan.cost_delta_inr_month,
        "confidence": plan.confidence,
        "expected_alert": signal.alert_should_clear if signal else None,
        "window_seconds": signal.window_seconds if signal else None,
    }


@router.get(
    "/api/approvals/{execution_id}",
    summary="Read the authoritative approval state",
)
async def read(execution_id: int) -> dict[str, object]:
    """Report whether this execution may proceed.

    Reads PostgreSQL, not the cache. The dashboard renders the countdown from `expires_at` in the
    database rather than computing it client-side from a duration, because the database owns expiry
    and a browser clock would disagree with it.
    """
    async with transaction() as session:
        authorisation = await approval_manager.can_execute(session, execution_id)

    return {
        "execution_id": execution_id,
        "allowed": authorisation.allowed,
        "reason": authorisation.reason,
        "decided_by": authorisation.decided_by,
    }

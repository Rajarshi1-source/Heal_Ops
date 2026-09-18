"""`POST /api/approvals/{execution_id}` -- the approval decision endpoint.

Serves two callers: the Slack interactivity handler and the dashboard's approve path. Both land here
so there is exactly one place where a human decision is recorded, one transaction boundary, and one
ledger entry format.

**Idempotent by construction.** A double click in Slack, or a retried dashboard request, is one
decision -- `approval_manager.decide()` returns the original outcome rather than overwriting it.
That matters more than it looks: without it, two people could race and whoever clicked last would
win a question the first had already answered.

Granting approval does **not** execute anything here. It records the decision and enqueues the
execution, so the act of approving cannot block on a Kubernetes call and a slow cluster cannot make
Slack think the click failed.
"""

from __future__ import annotations

from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Body, HTTPException, status
from pydantic import BaseModel, Field

from cache import streams
from core import approval_manager
from db.connection import transaction
from db.models import RunbookExecution

log = structlog.get_logger(__name__)
router = APIRouter(tags=["approvals"])


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "reject"]
    decided_by: str = Field(description="Slack user id, or the dashboard's authenticated identity.")
    slack_message_ts: str | None = None
    slack_channel: str | None = None


class ApprovalResult(BaseModel):
    execution_id: int
    status: str
    allowed: bool
    detail: str
    enqueued: bool


@router.post(
    "/api/approvals/{execution_id}",
    summary="Record a human approval decision",
    response_model=ApprovalResult,
)
async def decide(
    execution_id: int,
    body: Annotated[ApprovalDecision, Body()],
) -> ApprovalResult:
    approved = body.decision == "approve"

    async with transaction() as session:
        execution = await session.get(RunbookExecution, execution_id)
        if execution is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no execution {execution_id}",
            )

        authorisation = await approval_manager.decide(
            session,
            execution_id,
            approved=approved,
            decided_by=body.decided_by,
            slack_message_ts=body.slack_message_ts,
            slack_channel=body.slack_channel,
        )

        if not authorisation.allowed:
            # Rejected, expired, or already decided. The execution is closed out rather than left
            # sitting in `awaiting_approval` forever.
            if execution.status == "awaiting_approval":
                execution.status = "rejected" if not approved else "escalated"
            enqueued = False
        else:
            # Approved. Hand it to a worker rather than executing inline, so the click returns
            # immediately and a slow cluster cannot look like a failed button.
            execution.status = "queued"
            enqueued = True

    if enqueued:
        await streams.enqueue(
            "execute_approved",
            {"execution_id": execution_id, "incident_id": execution.incident_id},
        )

    log.info(
        "approval recorded",
        execution_id=execution_id,
        decision=body.decision,
        decided_by=body.decided_by,
        allowed=authorisation.allowed,
    )

    return ApprovalResult(
        execution_id=execution_id,
        status="approved" if authorisation.allowed else body.decision,
        allowed=authorisation.allowed,
        detail=authorisation.reason,
        enqueued=enqueued,
    )


@router.get(
    "/api/approvals/{execution_id}",
    summary="Read the authoritative approval state",
)
async def read(execution_id: int) -> dict[str, object]:
    """Report whether this execution may proceed.

    Reads PostgreSQL, not the cache. The dashboard renders the countdown from `expires_at` here
    rather than computing it client-side from a duration, because the database owns expiry and a
    browser clock would disagree with it.
    """
    async with transaction() as session:
        authorisation = await approval_manager.can_execute(session, execution_id)

    return {
        "execution_id": execution_id,
        "allowed": authorisation.allowed,
        "reason": authorisation.reason,
        "decided_by": authorisation.decided_by,
    }

"""Approvals -- a safety control, so PostgreSQL owns it.

This module is the fix for defect D3, and the reasoning is worth stating precisely because the naive
version looks perfectly reasonable.

Rev 1 stored approval state in Redis under a 600-second TTL and read "key missing" as "timed out".
Restart Redis, or let it evict under memory pressure, and every pending approval vanished. The
executor then could not distinguish three completely different situations:

* *expired*      -- nobody answered in time, so escalate;
* *never existed* -- a bug or a forged callback, so refuse;
* *lost*          -- the cache restarted, so the answer is unknown.

Collapsing those into one meant the system either dropped incidents silently or, worse, executed a
disruptive action nobody approved.

So: **PostgreSQL is the source of truth.** `expires_at` is a column, not a TTL -- expiry transitions
the row rather than deleting it, which keeps the evidence. Redis is a read-through cache and a
notifier, nothing more. And `can_execute()` **fails closed**: any state it cannot positively read as
an in-window approval returns `False`.

> *"A safety control cannot live in a cache."*
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import ledger
from cache import approval_store
from cache.approval_store import CachedApproval
from config import get_settings
from core.incident_state import assert_transition
from db.connection import db_now
from db.models import Approval, Incident, IncidentState, RunbookExecution

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Authorisation:
    """The answer to "may this execution proceed?"."""

    allowed: bool
    reason: str
    decided_by: str | None = None

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True, slots=True)
class Resolution:
    """What one decision did to the execution it was about."""

    authorisation: Authorisation
    execution_status: str
    enqueue: bool
    """True only for the single call that moved `awaiting_approval -> queued`.

    A second "Approve" click, or a retried dashboard request, gets the original authorisation back
    but `enqueue=False` -- otherwise a double click would queue the same execution twice.
    """


#: Execution statuses for an approval that ended without a grant.
REJECTED = "rejected"
APPROVAL_EXPIRED = "approval_expired"


async def request(
    session: AsyncSession,
    execution: RunbookExecution,
    *,
    reasons: tuple[str, ...] = (),
) -> Approval:
    """Create the approval record. The deadline is set here, in the database.

    Idempotent per execution: the unique index on `execution_id` means a retried job finds the
    existing row instead of creating a second one, so a double-delivered message cannot produce two
    approval requests for one decision.
    """
    settings = get_settings()
    now = await db_now(session)

    existing = await _fetch(session, execution.id)
    if existing is not None:
        return existing

    approval = Approval(
        execution_id=execution.id,
        incident_id=execution.incident_id,
        status="pending",
        requested_at=now,
        expires_at=now + timedelta(seconds=settings.approval_timeout_seconds),
        reason="; ".join(reasons) if reasons else None,
    )
    session.add(approval)
    await session.flush()

    log.info(
        "approval requested",
        execution_id=execution.id,
        expires_at=approval.expires_at.isoformat(),
    )
    return approval


async def can_execute(session: AsyncSession, execution_id: int) -> Authorisation:
    """**Fail closed.** An unknown or unreadable approval is a refusal, never a permission.

    Every path that is not "approved, decided inside its window" returns `False`, including the
    error path -- a database that cannot be read is not evidence that a human said yes.
    """
    try:
        row = await _fetch(session, execution_id)
    except Exception as exc:  # noqa: BLE001
        # Cannot read the approval state at all. Refusing is the only safe answer.
        log.error("approval state unreadable; refusing", execution_id=execution_id, error=str(exc))
        return Authorisation(False, "approval state unreadable -- refusing (fail closed)")

    if row is None:
        return Authorisation(False, "no approval record -- refusing (fail closed)")

    now = await db_now(session)

    if row.status == "approved":
        if row.decided_at is None:
            # Approved with no decision timestamp should be impossible. Treat the contradiction as
            # untrustworthy rather than assuming the happy case.
            return Authorisation(False, "approval record is inconsistent -- refusing")
        if row.decided_at > row.expires_at:
            # Decided, but after the window closed. The human answered a question that had already
            # expired, and acting on it now would surprise them.
            return Authorisation(
                False, f"approved at {row.decided_at.isoformat()}, after the window closed"
            )
        return Authorisation(True, f"approved by {row.decided_by}", decided_by=row.decided_by)

    if row.status == "pending":
        if now > row.expires_at:
            # Expiry ESCALATES. It never auto-executes -- silence is not consent.
            await _mark_expired(session, execution_id)
            return Authorisation(False, "approval window expired -- escalating")
        return Authorisation(False, "still awaiting a human decision")

    return Authorisation(False, f"approval status={row.status}")


async def decide(
    session: AsyncSession,
    execution_id: int,
    *,
    approved: bool,
    decided_by: str,
    slack_message_ts: str | None = None,
    slack_channel: str | None = None,
) -> Authorisation:
    """Record a human's decision.

    Idempotent: a double click in Slack is **one** decision. The second call returns the first
    outcome rather than overwriting it, so two people cannot race and have the later click win.
    """
    row = await _fetch(session, execution_id)
    if row is None:
        return Authorisation(False, "no approval record for this execution")

    now = await db_now(session)

    if row.status != "pending":
        # Already decided. Report what was decided rather than replacing it.
        log.info(
            "approval already decided; ignoring the repeat",
            execution_id=execution_id,
            status=row.status,
            original_decider=row.decided_by,
        )
        return Authorisation(
            row.status == "approved",
            f"already {row.status} by {row.decided_by}",
            decided_by=row.decided_by,
        )

    if now > row.expires_at:
        # The click arrived after the deadline. Record the expiry rather than honouring it, so the
        # audit trail shows the window closed before anyone answered.
        await _mark_expired(session, execution_id)
        await ledger.append(
            session,
            "approve",
            {
                "stage": "expired",
                "execution_id": execution_id,
                "attempted_by": decided_by,
                "detail": "decision arrived after the window closed",
            },
            actor=decided_by,
            incident_id=row.incident_id,
            execution_id=execution_id,
        )
        return Authorisation(False, "approval window had already expired")

    row.status = "approved" if approved else "rejected"
    row.decided_by = decided_by
    row.decided_at = now
    if slack_message_ts:
        row.slack_message_ts = slack_message_ts
    if slack_channel:
        row.slack_channel = slack_channel
    await session.flush()

    # The approver identity is the change record's signature, so it goes in the ledger with the
    # actor set to the human rather than to `healops`.
    await ledger.append(
        session,
        "approve",
        {
            "stage": "granted" if approved else "rejected",
            "execution_id": execution_id,
            "decided_by": decided_by,
        },
        actor=decided_by,
        incident_id=row.incident_id,
        execution_id=execution_id,
    )

    await _best_effort(
        approval_store.mirror(
            CachedApproval(
                execution_id=execution_id,
                status=row.status,  # type: ignore[arg-type]
                expires_at=row.expires_at.isoformat(),
                decided_by=decided_by,
                slack_message_ts=row.slack_message_ts,
            )
        )
    )

    log.info(
        "approval decided",
        execution_id=execution_id,
        approved=approved,
        decided_by=decided_by,
    )
    return Authorisation(approved, f"{row.status} by {decided_by}", decided_by=decided_by)


async def resolve(
    session: AsyncSession,
    execution: RunbookExecution,
    *,
    approved: bool,
    decided_by: str,
    slack_message_ts: str | None = None,
    slack_channel: str | None = None,
) -> Resolution:
    """Record a decision and settle the execution and incident it was about.

    The one entry point for both the Slack button and the dashboard, so the two cannot drift in
    what an approval *does*. Caller owns the transaction; the `approve` ledger entry written by
    `decide()` lands in the same commit as the state change here.
    """
    authorisation = await decide(
        session,
        execution.id,
        approved=approved,
        decided_by=decided_by,
        slack_message_ts=slack_message_ts,
        slack_channel=slack_channel,
    )

    if execution.status != "awaiting_approval":
        # Settled by an earlier click, the reaper, or a worker that already ran it. Report the
        # original decision and change nothing -- overwriting the status of an execution that is
        # already running would hand it to a second worker.
        return Resolution(authorisation, execution.status, enqueue=False)

    if authorisation.allowed:
        execution.status = "queued"
        await session.flush()
        return Resolution(authorisation, execution.status, enqueue=True)

    row = await _fetch(session, execution.id)
    status = REJECTED if row is not None and row.status == "rejected" else APPROVAL_EXPIRED
    await _escalate(session, execution, status)
    return Resolution(authorisation, execution.status, enqueue=False)


async def reap_expired(session: AsyncSession) -> list[Approval]:
    """Transition `pending -> expired` for everything past its deadline, and escalate.

    Runs on a short loop so the state machine is driven by durable data rather than by whoever
    happens to read the row next. Expired approvals **escalate**; nothing here executes anything.
    Silence is not consent.
    """
    now = await db_now(session)

    rows = list(
        (
            await session.execute(
                select(Approval).where(Approval.status == "pending", Approval.expires_at < now)
            )
        ).scalars()
    )
    if not rows:
        return []

    await session.execute(
        update(Approval)
        .where(Approval.status == "pending", Approval.expires_at < now)
        .values(status="expired")
    )

    for row in rows:
        await ledger.append(
            session,
            "approve",
            {
                "stage": "expired",
                "execution_id": row.execution_id,
                "detail": "nobody answered within the approval window; escalating",
            },
            incident_id=row.incident_id,
            execution_id=row.execution_id,
        )
        execution = await session.get(RunbookExecution, row.execution_id)
        if execution is not None and execution.status == "awaiting_approval":
            await _escalate(session, execution, APPROVAL_EXPIRED)
        await _best_effort(approval_store.invalidate(row.execution_id))

    log.info("reaped expired approvals", count=len(rows))
    return rows


async def _fetch(session: AsyncSession, execution_id: int) -> Approval | None:
    return (
        await session.execute(select(Approval).where(Approval.execution_id == execution_id))
    ).scalar_one_or_none()


async def _mark_expired(session: AsyncSession, execution_id: int) -> None:
    await session.execute(
        update(Approval)
        .where(Approval.execution_id == execution_id, Approval.status == "pending")
        .values(status="expired")
    )
    await session.flush()
    await _best_effort(approval_store.invalidate(execution_id))


async def _escalate(session: AsyncSession, execution: RunbookExecution, status: str) -> None:
    """An approval that ended without a grant hands the incident to a human, explicitly.

    Leaving the incident in `awaiting_approval` would show it on the dashboard as still waiting for
    a decision that has already been made.
    """
    execution.status = status
    incident = await session.get(Incident, execution.incident_id)
    if incident is not None and incident.state == IncidentState.AWAITING_APPROVAL.value:
        incident.state = assert_transition(
            incident.id, incident.state, IncidentState.ESCALATED
        ).value
        incident.outcome = status
    await session.flush()


async def _best_effort(operation: Awaitable[object]) -> None:
    """Cache writes never decide anything, so a cache outage must not roll back a decision.

    Without this, Redis being down would turn a human's recorded approval into an exception inside
    the very transaction that records it -- the cache would have become a dependency of the safety
    control it exists only to speed up.
    """
    try:
        await operation
    except Exception as exc:  # noqa: BLE001
        log.warning("approval cache write failed; PostgreSQL remains authoritative", error=str(exc))

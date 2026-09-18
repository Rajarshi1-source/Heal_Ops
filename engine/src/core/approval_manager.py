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

from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import ledger
from cache import approval_store
from cache.approval_store import CachedApproval
from config import get_settings
from db.connection import db_now
from db.models import Approval, RunbookExecution

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Authorisation:
    """The answer to "may this execution proceed?"."""

    allowed: bool
    reason: str
    decided_by: str | None = None

    def __bool__(self) -> bool:
        return self.allowed


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

    await approval_store.mirror(
        CachedApproval(
            execution_id=execution_id,
            status=row.status,  # type: ignore[arg-type]
            expires_at=row.expires_at.isoformat(),
            decided_by=decided_by,
            slack_message_ts=row.slack_message_ts,
        )
    )

    log.info(
        "approval decided",
        execution_id=execution_id,
        approved=approved,
        decided_by=decided_by,
    )
    return Authorisation(approved, f"{row.status} by {decided_by}", decided_by=decided_by)


async def reap_expired(session: AsyncSession) -> list[Approval]:
    """Transition `pending -> expired` for everything past its deadline.

    Runs on a short loop so the state machine is driven by durable data rather than by whoever
    happens to read the row next. Expired approvals **escalate**; nothing here executes anything.
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
        await approval_store.invalidate(row.execution_id)

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
    await approval_store.invalidate(execution_id)

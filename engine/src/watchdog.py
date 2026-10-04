"""The watchdog -- a dead-man's switch for remediation.

Runs as a CronJob every minute, deliberately **outside** the engine process. That separation is the
entire point: the failure it exists to catch is the engine dying between `execute()` and `verify()`,
and a sweeper living inside that same process would die with it.

Three jobs:

1. **Force-revert executions whose verification never completed** past the deadline. An unverified
   change left in place is an incident nobody is watching.
2. **Expire TTL'd ephemeral fixes.** The mitigation's window is over, so the workload returns to its
   declared state and GitOps reconciliation resumes.
3. **Export `healops_watchdog_actions_total`**, which alerts above zero. The watchdog acting at all
   means something upstream failed, so it should never be silently routine.

Everything here is idempotent. The sweep may run twice, overlap with itself, or race the verifier.
Candidate rows are locked `FOR UPDATE SKIP LOCKED`, so whichever of the watchdog and the verify loop
reaches an execution first owns it and the other passes over it; `compensate()` being idempotent
covers whatever that leaves.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import breaker
import compensator
import notifier
import plan_store
from compensator import CompensationOutcome
from config import get_settings
from db.connection import db_now, transaction
from db.models import AuditLedger, Incident, RemediationPlanRow, RunbookExecution
from plan import Durability, RemediationPlan
from runbooks import load_all

log = structlog.get_logger(__name__)

#: Statuses that mean "acted, outcome not yet decided". Only these are candidates for a sweep.
AWAITING_VERIFICATION = "awaiting_verification"


@dataclass(frozen=True, slots=True)
class SweepReport:
    force_reverted: int = 0
    expired: int = 0
    skipped: int = 0
    failures: int = 0

    @property
    def total_actions(self) -> int:
        return self.force_reverted + self.expired

    def to_dict(self) -> dict[str, int]:
        return {
            "force_reverted": self.force_reverted,
            "expired": self.expired,
            "skipped": self.skipped,
            "failures": self.failures,
        }


async def find_unverified(
    session: AsyncSession, now: datetime, deadline_seconds: int
) -> list[RunbookExecution]:
    """Executions that acted and were never judged.

    Selected on `finished_at`, not on when the row was created: an execution that legitimately took
    four minutes to apply should get its full verification window afterwards, not have that time
    counted against it.
    """
    cutoff = now - timedelta(seconds=deadline_seconds)
    rows = await session.execute(
        select(RunbookExecution)
        .where(
            RunbookExecution.status == AWAITING_VERIFICATION,
            RunbookExecution.compensated.is_(False),
            RunbookExecution.finished_at.isnot(None),
            RunbookExecution.finished_at < cutoff,
        )
        .order_by(RunbookExecution.finished_at)
        .limit(50)
        .with_for_update(skip_locked=True)
    )
    return list(rows.scalars())


async def find_expired_ephemeral(session: AsyncSession, now: datetime) -> list[RunbookExecution]:
    """Ephemeral fixes whose TTL has elapsed.

    Only successful, verified executions are candidates. A fix that already failed and was reverted
    has nothing left to expire, and re-running its compensation would be noise in the ledger.
    """
    rows = await session.execute(
        select(RunbookExecution)
        .where(
            RunbookExecution.durability == str(Durability.EPHEMERAL),
            RunbookExecution.compensated.is_(False),
            RunbookExecution.expires_at.isnot(None),
            RunbookExecution.expires_at < now,
            RunbookExecution.status.in_([compensator.CLOSED_REMEDIATED, AWAITING_VERIFICATION]),
        )
        .order_by(RunbookExecution.expires_at)
        .limit(50)
        .with_for_update(skip_locked=True)
    )
    return list(rows.scalars())


async def _handle(
    session: AsyncSession,
    execution: RunbookExecution,
    *,
    expired: bool,
    deadline_seconds: int,
) -> tuple[RemediationPlan, CompensationOutcome] | None:
    """Compensate one execution. Returns what was done, or None when nothing safe could be."""
    registry = load_all()

    if execution.plan_id is None:
        # No plan means no persisted undo, so there is nothing safe to do automatically. Better to
        # leave it and say so than to guess at a compensation.
        log.error("execution has no plan; cannot compensate", execution_id=execution.id)
        return None

    plan_row = await session.get(RemediationPlanRow, execution.plan_id)
    if plan_row is None:
        log.error("plan row missing; cannot compensate", execution_id=execution.id)
        return None

    runbook = registry.get(plan_row.runbook)
    if runbook is None:
        # The runbook was removed or renamed while this fix was outstanding. Compensating with a
        # different runbook's logic would be worse than escalating.
        log.error(
            "runbook is no longer registered; cannot compensate",
            runbook=plan_row.runbook,
            execution_id=execution.id,
        )
        return None

    incident = await session.get(Incident, execution.incident_id)
    if incident is None:
        log.error("incident missing; cannot compensate", execution_id=execution.id)
        return None

    plan = plan_store.rehydrate(plan_row)

    if expired:
        outcome = await compensator.expire(session, incident, execution, plan, runbook)
    else:
        outcome = await compensator.force_revert(
            session,
            incident,
            execution,
            plan,
            runbook,
            reason=(
                f"verification never completed within {deadline_seconds}s; "
                "assuming the fix did not work"
            ),
        )
    if not expired:
        # A stranded half-open trial counts as a failed one: it was never shown to work.
        await breaker.record_outcome(session, execution.plan_id, success=False)
    return plan, outcome


async def sweep() -> SweepReport:
    """One pass. Safe to run concurrently with itself and with the verifier."""
    settings = get_settings()
    deadline = settings.verification_deadline_seconds
    counts = {"force_reverted": 0, "expired": 0, "skipped": 0, "failures": 0}
    acted: list[tuple[RemediationPlan, CompensationOutcome]] = []

    async with transaction() as session:
        now = await db_now(session)
        batches = (
            ("force_reverted", False, await find_unverified(session, now, deadline)),
            ("expired", True, None),
        )
        for bucket, is_expiry, preloaded in batches:
            # The expiry query runs after the force-reverts have flushed, so an execution that was
            # both stranded and past its TTL is compensated once, not twice.
            candidates = (
                preloaded if preloaded is not None else await find_expired_ephemeral(session, now)
            )
            for execution in candidates:
                try:
                    # A savepoint per execution: one failure rolls back only its own partial
                    # writes, and the rest of the sweep still lands.
                    async with session.begin_nested():
                        done = await _handle(
                            session, execution, expired=is_expiry, deadline_seconds=deadline
                        )
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "watchdog could not compensate",
                        execution_id=execution.id,
                        expiry=is_expiry,
                        error=str(exc),
                    )
                    counts["failures"] += 1
                    continue
                if done is None:
                    counts["skipped"] += 1
                else:
                    counts[bucket] += 1
                    acted.append(done)

    # After commit: the ledger is the record, the message is only news about it.
    for plan, outcome in acted:
        await notifier.watchdog_acted(plan, outcome)

    report = SweepReport(**counts)
    if report.total_actions or report.failures:
        # The watchdog acting means something upstream failed. Never log this at debug.
        log.warning("watchdog acted", **report.to_dict())
    return report


async def actions_recorded(session: AsyncSession) -> int:
    """Every compensation the watchdog has ever run, counted from the ledger.

    The engine exports this as `healops_watchdog_actions_total` on each scrape. The ledger is the
    one record the CronJob and the engine share, and it cannot be rewritten, so the count cannot go
    backwards and cannot miss an action the watchdog took while nobody was scraping.
    """
    count = await session.scalar(
        select(func.count())
        .select_from(AuditLedger)
        .where(
            AuditLedger.event_type == "compensate",
            AuditLedger.payload["trigger"].astext.in_(("watchdog", "expiry")),
        )
    )
    return int(count or 0)


async def main() -> int:
    """CronJob entrypoint. Exits non-zero only on sweep failures, so the Job surfaces them."""
    from db import connection

    try:
        report = await sweep()
    finally:
        await connection.dispose()

    print(  # noqa: T201 - CronJob logs are read with kubectl logs
        f"watchdog: force_reverted={report.force_reverted} expired={report.expired} "
        f"skipped={report.skipped} failures={report.failures}"
    )
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

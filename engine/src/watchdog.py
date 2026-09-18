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

Everything here is idempotent. The sweep may run twice, overlap with itself, or race the verifier,
and `compensate()` being idempotent is what makes that safe.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import compensator
from config import get_settings
from db.connection import db_now, transaction
from db.models import Incident, RemediationPlanRow, RunbookExecution
from plan import (
    ApiCall,
    BlastRadius,
    Durability,
    ExpectedSignal,
    RemediationPlan,
    Reversibility,
)
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
    )
    return list(rows.scalars())


def rehydrate_plan(row: RemediationPlanRow) -> RemediationPlan:  # noqa: C901
    """Rebuild the plan object from its persisted row.

    The watchdog has no memory of the process that created the plan; that is the whole design. So it
    reconstructs the plan, and crucially its **compensation**, from the database. This is why
    `compensation` is a column rather than something derived at revert time.
    """
    signal = row.expected_signal or {}
    radius = row.blast_radius or {}

    return RemediationPlan(
        runbook=row.runbook,
        incident_id=row.incident_id,
        target_kind=row.target_kind,
        target_name=row.target_name,
        target_namespace=row.target_namespace,
        actions=tuple(_to_call(c) for c in row.actions or []),
        compensation=tuple(_to_call(c) for c in row.compensation or []),
        reversibility=Reversibility(row.reversibility),
        durability=Durability(row.durability),
        ttl_seconds=row.ttl_seconds,
        blast_radius=BlastRadius(
            pods=int(radius.get("pods", 0)),
            nodes=int(radius.get("nodes", 0)),
            namespaces=tuple(radius.get("namespaces", ())),
            user_facing=bool(radius.get("user_facing", False)),
        ),
        expected_signal=None if not signal else _to_signal(signal),
        confidence=float(row.confidence),
        summary=row.summary or "",
        cost_delta_inr_month=float(row.cost_delta_inr_month or 0),
        refusal_reason=row.refusal_reason,
        planner_version=row.planner_version,
        plan_id=row.id,
    )


def _to_call(raw: dict[str, Any]) -> ApiCall:
    namespace = raw.get("namespace")
    subresource = raw.get("subresource")
    return ApiCall(
        verb=str(raw.get("verb", "")),
        resource=str(raw.get("resource", "")),
        name=str(raw.get("name", "")),
        namespace=str(namespace) if namespace is not None else None,
        body=raw.get("body") or {},
        subresource=str(subresource) if subresource is not None else None,
    )


def _to_signal(raw: dict[str, Any]) -> ExpectedSignal:
    return ExpectedSignal(
        alert_should_clear=str(raw.get("alert_should_clear", "")),
        queries=raw.get("queries") or {},
        thresholds=raw.get("thresholds") or {},
        window_seconds=int(raw.get("window_seconds") or 300),
    )


async def _handle(
    session: AsyncSession,
    execution: RunbookExecution,
    *,
    expired: bool,
    deadline_seconds: int,
) -> bool:
    """Compensate one execution. Returns whether an action was taken."""
    registry = load_all()

    if execution.plan_id is None:
        # No plan means no persisted undo, so there is nothing safe to do automatically. Better to
        # leave it and say so than to guess at a compensation.
        log.error("execution has no plan; cannot compensate", execution_id=execution.id)
        return False

    plan_row = await session.get(RemediationPlanRow, execution.plan_id)
    if plan_row is None:
        log.error("plan row missing; cannot compensate", execution_id=execution.id)
        return False

    runbook = registry.get(plan_row.runbook)
    if runbook is None:
        # The runbook was removed or renamed while this fix was outstanding. Compensating with a
        # different runbook's logic would be worse than escalating.
        log.error(
            "runbook is no longer registered; cannot compensate",
            runbook=plan_row.runbook,
            execution_id=execution.id,
        )
        return False

    incident = await session.get(Incident, execution.incident_id)
    if incident is None:
        log.error("incident missing; cannot compensate", execution_id=execution.id)
        return False

    plan = rehydrate_plan(plan_row)

    if expired:
        await compensator.expire(session, incident, execution, plan, runbook)
    else:
        await compensator.force_revert(
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
    return True


async def sweep() -> SweepReport:
    """One pass. Safe to run concurrently with itself and with the verifier."""
    settings = get_settings()
    force_reverted = expired_count = skipped = failures = 0

    async with transaction() as session:
        now = await db_now(session)

        for execution in await find_unverified(
            session, now, settings.verification_deadline_seconds
        ):
            try:
                acted = await _handle(
                    session,
                    execution,
                    expired=False,
                    deadline_seconds=settings.verification_deadline_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                # One bad execution must not abort the sweep; the rest still need attention.
                log.error("force-revert failed", execution_id=execution.id, error=str(exc))
                failures += 1
                continue
            if acted:
                force_reverted += 1
            else:
                skipped += 1

        for execution in await find_expired_ephemeral(session, now):
            try:
                acted = await _handle(
                    session,
                    execution,
                    expired=True,
                    deadline_seconds=settings.verification_deadline_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                log.error("expiry failed", execution_id=execution.id, error=str(exc))
                failures += 1
                continue
            if acted:
                expired_count += 1
            else:
                skipped += 1

    report = SweepReport(
        force_reverted=force_reverted,
        expired=expired_count,
        skipped=skipped,
        failures=failures,
    )

    if report.total_actions or report.failures:
        # The watchdog acting means something upstream failed. Never log this at debug.
        log.warning("watchdog acted", **report.to_dict())
    return report


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

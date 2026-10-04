"""The anti-masking guard -- automation that hides a bug files a bug.

The criticism every self-healing system deserves and almost none answer: **if you automatically fix
the same failure every day, you have converted a bug into a chore and hidden it.**

So every execution is counted per (runbook, target) over a rolling seven days. At the fourth:

1. **demote** that (runbook, namespace) to approval mode -- it stops fixing this unattended;
2. **open a GitHub issue** with the rendered report: timeline, plans, verdicts, ledger hashes;
3. attach the issue URL to the recurrence row, and **post to Slack**.

Steps 1 and the report happen inside the transaction that recorded the execution, so the demotion
cannot be lost; 2 and 3 are external calls made after commit by the worker, and their failure costs
the issue, not the guard.

The target is the *incident's* key -- the workload -- not the plan's. The OOM runbook acts on a pod,
and every restart is a new pod name; counting by plan target would see four different targets and
the guard would never fire on exactly the defect it exists for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import ledger
import metrics
from config import get_settings
from db.connection import db_now
from db.models import (
    AuditLedger,
    Incident,
    RemediationPlanRow,
    RemediationRecurrence,
    RunbookExecution,
    Verification,
)
from plan import RemediationPlan

log = structlog.get_logger(__name__)

#: Marks a window whose guard has fired, before the issue URL is known. Dedup keys off it, so a
#: fifth occurrence in the same window does not file a second issue.
FILING = "(filing)"
LABELS = ["healops", "recurring-incident", "needs-root-cause"]


@dataclass(frozen=True, slots=True)
class MaskingEvent:
    """The guard fired. Carries everything the post-commit steps need."""

    runbook: str
    target_key: str
    namespace: str | None
    occurrences: int
    title: str
    body: str


async def record(
    session: AsyncSession, plan: RemediationPlan, incident: Incident
) -> MaskingEvent | None:
    """Count this execution; demote and return a `MaskingEvent` when the threshold is crossed."""
    settings = get_settings()
    now = await db_now(session)
    today = now.date()
    key = incident.target_key

    row = await session.get(RemediationRecurrence, (plan.runbook, key, today), with_for_update=True)
    if row is None:
        session.add(
            RemediationRecurrence(
                runbook=plan.runbook, target_key=key, window_start=today, occurrences=1
            )
        )
    else:
        row.occurrences += 1
        row.last_seen_at = now
    await session.flush()

    window = _window(today, settings.recurrence_window_days)
    occurrences, already_filed = await _count(session, plan.runbook, key, window)
    metrics.recurrence_count.labels(runbook=plan.runbook, target=key).set(occurrences)

    if occurrences < settings.recurrence_threshold or already_filed:
        return None

    await _mark_filing(session, plan.runbook, key, today)
    await _demote(session, plan.runbook, plan.target_namespace, occurrences, key)
    await ledger.append(
        session,
        "masking_guard",
        {
            "runbook": plan.runbook,
            "target": key,
            "occurrences": occurrences,
            "window_days": settings.recurrence_window_days,
            "action": "demoted to approval; filing an issue",
        },
        incident_id=incident.id,
    )
    log.warning("masking guard fired", runbook=plan.runbook, target=key, occurrences=occurrences)

    return MaskingEvent(
        runbook=plan.runbook,
        target_key=key,
        namespace=plan.target_namespace,
        occurrences=occurrences,
        title=(
            f"[HealOps] {plan.runbook} has remediated {key} {occurrences}x in "
            f"{settings.recurrence_window_days} days"
        ),
        body=await render_report(session, plan.runbook, key, window),
    )


async def attach_issue(session: AsyncSession, event: MaskingEvent, url: str | None) -> None:
    """Replace the filing marker with the issue URL, or with why there is none."""
    window = _window((await db_now(session)).date(), get_settings().recurrence_window_days)
    rows = (
        await session.execute(
            select(RemediationRecurrence).where(
                RemediationRecurrence.runbook == event.runbook,
                RemediationRecurrence.target_key == event.target_key,
                RemediationRecurrence.window_start >= window,
                RemediationRecurrence.issue_url == FILING,
            )
        )
    ).scalars()
    for row in rows:
        row.issue_url = url or "(not filed: github unavailable or unconfigured)"
    await session.flush()


async def render_report(session: AsyncSession, runbook: str, key: str, since: date) -> str:
    """The masking report: what was fixed, how often, and the evidence for each time."""
    rows = (
        await session.execute(
            select(RunbookExecution, RemediationPlanRow, Incident)
            .join(RemediationPlanRow, RemediationPlanRow.id == RunbookExecution.plan_id)
            .join(Incident, Incident.id == RunbookExecution.incident_id)
            .where(
                RunbookExecution.runbook == runbook,
                Incident.target_key == key,
                Incident.opened_at >= since,
                RunbookExecution.mode != "shadow",
            )
            .order_by(Incident.opened_at)
        )
    ).all()

    lines = [
        f"HealOps has run `{runbook}` against `{key}` {len(rows)} time(s) since {since}.",
        "",
        "That is no longer an incident; it is a defect being hidden by automation. The runbook has",
        "been **demoted to approval mode** for this namespace and will not fix this unattended",
        "again until someone promotes it back.",
        "",
        "| Opened | Incident | Plan | Execution | Verdict | Ledger |",
        "|---|---|---|---|---|---|",
    ]
    for execution, plan_row, incident in rows:
        verdict = await session.scalar(
            select(Verification.outcome)
            .where(Verification.execution_id == execution.id)
            .order_by(Verification.checked_at.desc())
            .limit(1)
        )
        digest = await session.scalar(
            select(AuditLedger.hash)
            .where(AuditLedger.execution_id == execution.id, AuditLedger.event_type == "execute")
            .limit(1)
        )
        lines.append(
            f"| {incident.opened_at:%Y-%m-%d %H:%M} | #{incident.id} | {plan_row.summary} | "
            f"{execution.status} | {verdict or 'pending'} | `{(digest or '')[:16]}` |"
        )
    lines += [
        "",
        "Verify any row offline with `healopsctl audit --verify-chain`; each hash is a link in "
        "the tamper-evident ledger.",
    ]
    return "\n".join(lines)


def _window(today: date, days: int) -> date:
    return today - timedelta(days=days - 1)


async def _count(session: AsyncSession, runbook: str, key: str, since: date) -> tuple[int, bool]:
    result = (
        await session.execute(
            select(
                func.coalesce(func.sum(RemediationRecurrence.occurrences), 0),
                func.count(RemediationRecurrence.issue_url),
            ).where(
                RemediationRecurrence.runbook == runbook,
                RemediationRecurrence.target_key == key,
                RemediationRecurrence.window_start >= since,
            )
        )
    ).one()
    return int(result[0]), int(result[1]) > 0


async def _mark_filing(session: AsyncSession, runbook: str, key: str, today: date) -> None:
    row = await session.get(RemediationRecurrence, (runbook, key, today))
    if row is not None:
        row.issue_url = FILING
    await session.flush()


async def _demote(
    session: AsyncSession, runbook: str, namespace: str | None, occurrences: int, key: str
) -> None:
    """Authority is earned per namespace and lost per namespace, on one strike."""
    import promotion

    await promotion.demote(
        session,
        runbook,
        namespace,
        reason=f"masking guard: {occurrences} remediations of {key} in 7 days",
    )

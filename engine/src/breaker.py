"""The global remediation circuit breaker -- a breaker over *policy*, not transport.

Transport breakers on the K8s, Slack and S3 clients stop one sick dependency turning every runbook
into a timeout. This one stops something worse: **automating into an outage.** It trips on any of

* more than 3 compensations in 15 minutes        -- the system keeps getting it wrong
* more than 2 consecutive verification failures  -- that runbook is broken
  for the same runbook
* Kubernetes API error rate above 20%             -- the cluster is sick; acting into it is reckless
* a manual `healopsctl freeze --reason ...`       -- a human said stop

and while open, the policy gate denies every plan. After 30 minutes it goes `open -> half_open`,
where **exactly one plan** is let through; that plan's outcome decides `closed` or `open` again.
A freeze has no expiry: only `unfreeze` lifts it, because "release in progress" ends when the
release does, not when a timer says so.

The trip conditions read the ledger and the verification rows, never in-process counters, except for
the API error rate, which is by nature a property of this process's view of the API server. That is
what lets a compensation run by the watchdog CronJob -- another process, minutes earlier -- count
towards tripping the breaker in the engine.

State lives on the `global` row of `remediation_budget`, and every transition is a ledger entry with
a reason and an owner.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import ledger
import metrics
from budget import GLOBAL, limit_for
from config import get_settings
from db.connection import db_now
from db.models import AuditLedger, RemediationBudget, RunbookExecution, Verification

log = structlog.get_logger(__name__)

#: `breaker_owner` for trips the system made itself. Anything else is a human freeze.
SYSTEM: Final[str] = "healops"


@dataclass(frozen=True, slots=True)
class BreakerView:
    state: str
    reason: str | None = None
    owner: str | None = None
    until: datetime | None = None
    trial_plan_id: str | None = None

    @property
    def frozen(self) -> bool:
        return self.state == "open" and self.until is None and self.owner not in (None, SYSTEM)

    def for_policy(self) -> str:
        """What the gate should see. A half-open breaker whose single trial is taken is closed to
        everyone else, so it reads as open."""
        if self.state == "half_open" and self.trial_plan_id is not None:
            return "open"
        return self.state


async def _row(session: AsyncSession, *, for_update: bool) -> RemediationBudget:
    row = await session.get(RemediationBudget, GLOBAL, with_for_update=for_update)
    if row is None:
        row = RemediationBudget(
            scope=GLOBAL,
            window_start=await db_now(session),
            max_actions=limit_for(GLOBAL),
            used_actions=0,
            breaker_state="closed",
        )
        session.add(row)
        await session.flush()
    return row


async def view(session: AsyncSession, *, for_update: bool = False) -> BreakerView:
    """Current state, moving `open -> half_open` if the cool-down has elapsed."""
    row = await _row(session, for_update=for_update)
    now = await db_now(session)
    if row.breaker_state == "open" and row.breaker_until is not None and now >= row.breaker_until:
        row.breaker_state = "half_open"
        row.half_open_plan_id = None
        await _record(session, "breaker", "half_open", row.breaker_reason, SYSTEM)
    _export(row.breaker_state)
    return BreakerView(
        state=row.breaker_state,
        reason=row.breaker_reason,
        owner=row.breaker_owner,
        until=row.breaker_until,
        trial_plan_id=row.half_open_plan_id,
    )


async def trip(session: AsyncSession, reason: str) -> None:
    """Open the breaker for the cool-down period. A no-op if it is already open."""
    row = await _row(session, for_update=True)
    if row.breaker_state == "open":
        return
    now = await db_now(session)
    row.breaker_state = "open"
    row.breaker_reason = reason
    row.breaker_owner = SYSTEM
    row.breaker_until = now + timedelta(seconds=get_settings().breaker_half_open_after_seconds)
    row.half_open_plan_id = None
    await _record(session, "breaker", "open", reason, SYSTEM)
    log.warning("remediation breaker tripped", reason=reason)


async def freeze(session: AsyncSession, *, reason: str, owner: str) -> None:
    """`healopsctl freeze`: all automation off, with no expiry, on the record."""
    row = await _row(session, for_update=True)
    row.breaker_state = "open"
    row.breaker_reason = reason
    row.breaker_owner = owner
    row.breaker_until = None
    row.half_open_plan_id = None
    await _record(session, "freeze", "open", reason, owner)


async def unfreeze(session: AsyncSession, *, owner: str, reason: str = "unfrozen") -> None:
    row = await _row(session, for_update=True)
    row.breaker_state = "closed"
    row.breaker_reason = None
    row.breaker_owner = None
    row.breaker_until = None
    row.half_open_plan_id = None
    await _record(session, "unfreeze", "closed", reason, owner)


async def admit(session: AsyncSession, plan_id: str) -> tuple[bool, str]:
    """May this plan act *now*? Checked inside the mutating window, not only at planning time.

    The gate saw the breaker when the plan was made; an approval can sit for ten minutes, and the
    breaker may have tripped since. Half-open admits exactly one plan: the first to get here claims
    the trial slot atomically, and every other plan is refused until its outcome is known.
    """
    current = await view(session, for_update=True)
    if current.state == "closed":
        return True, "breaker closed"
    if current.state == "open":
        return False, f"breaker open: {current.reason}"
    if current.trial_plan_id in (None, plan_id):
        await session.execute(
            update(RemediationBudget)
            .where(RemediationBudget.scope == GLOBAL)
            .values(half_open_plan_id=plan_id)
        )
        await session.flush()
        return True, "half-open trial"
    return False, "breaker half-open and its single trial is already in progress"


async def record_outcome(session: AsyncSession, plan_id: str | None, *, success: bool) -> None:
    """Settle a half-open trial. Outcomes of other plans do not move the breaker here."""
    if plan_id is None:
        return
    row = await _row(session, for_update=True)
    if row.breaker_state != "half_open" or row.half_open_plan_id != plan_id:
        return
    if success:
        row.breaker_state = "closed"
        row.breaker_reason = None
        row.breaker_owner = None
        row.breaker_until = None
        row.half_open_plan_id = None
        await _record(session, "breaker", "closed", "half-open trial remediated", SYSTEM)
        log.info("remediation breaker closed after a successful trial")
    else:
        row.breaker_state = "closed"  # let trip() take the normal open path
        await trip(session, "half-open trial did not remediate")


# --- trip conditions ------------------------------------------------------------------------------


async def evaluate(session: AsyncSession) -> str | None:
    """Check every automatic trip condition; trip and return the reason if one holds."""
    current = await view(session)
    if current.state != "closed":
        return None  # open waits out its cool-down; half-open is decided by its trial

    reason = (
        await _too_many_compensations(session)
        or await _consecutive_failures(session)
        or _k8s_error_rate()
    )
    if reason:
        await trip(session, reason)
    return reason


async def _since_last_change(session: AsyncSession) -> datetime | None:
    """Evidence older than the breaker's last transition has already been acted on.

    Without this bound, the failures that tripped the breaker would trip it again the moment a
    trial closed it.
    """
    changed: datetime | None = await session.scalar(
        select(func.max(AuditLedger.occurred_at)).where(
            AuditLedger.event_type.in_(("breaker", "freeze", "unfreeze"))
        )
    )
    return changed


async def _too_many_compensations(session: AsyncSession) -> str | None:
    settings = get_settings()
    now = await db_now(session)
    since = now - timedelta(seconds=settings.breaker_compensation_window_seconds)
    last_change = await _since_last_change(session)
    if last_change is not None and last_change > since:
        since = last_change

    count = await session.scalar(
        select(func.count())
        .select_from(AuditLedger)
        .where(
            AuditLedger.event_type == "compensate",
            AuditLedger.occurred_at > since,
            # Expiry is a mitigation standing down on schedule, not the system getting it wrong.
            AuditLedger.payload["trigger"].astext.in_(("verification", "watchdog")),
        )
    )
    if (count or 0) > settings.breaker_compensation_threshold:
        minutes = settings.breaker_compensation_window_seconds // 60
        return f"{count} compensations in {minutes} minutes -- the system keeps getting it wrong"
    return None


async def _consecutive_failures(session: AsyncSession) -> str | None:
    """Any runbook whose most recent verdicts are all failures, beyond the threshold."""
    threshold = get_settings().breaker_consecutive_failure_threshold
    last_change = await _since_last_change(session)

    stmt = (
        select(RunbookExecution.runbook, Verification.outcome)
        .join(Verification, Verification.execution_id == RunbookExecution.id)
        .order_by(RunbookExecution.runbook, Verification.checked_at.desc())
    )
    if last_change is not None:
        stmt = stmt.where(Verification.checked_at > last_change)

    streaks: dict[str, int] = {}
    broken: set[str] = set()
    for runbook, outcome in (await session.execute(stmt)).all():
        if runbook in broken:
            continue
        if outcome == "remediated":
            broken.add(runbook)  # the streak ends at the most recent success
            continue
        streaks[runbook] = streaks.get(runbook, 0) + 1
        if streaks[runbook] > threshold:
            return (
                f"{streaks[runbook]} consecutive verification failures for {runbook} -- "
                "that runbook is broken"
            )
    return None


#: (monotonic time, calls, server-side errors) samples of this process's K8s client counters.
_samples: deque[tuple[float, float, float]] = deque(maxlen=120)


def _k8s_totals() -> tuple[float, float]:
    def total(counter: object) -> float:
        return sum(
            sample.value
            for family in counter.collect()  # type: ignore[attr-defined]
            for sample in family.samples
            if sample.name.endswith("_total")
        )

    return total(metrics.k8s_api_calls), total(metrics.k8s_api_errors)


def _k8s_error_rate() -> str | None:
    settings = get_settings()
    now = time.monotonic()
    calls, errors = _k8s_totals()
    _samples.append((now, calls, errors))

    horizon = now - settings.breaker_k8s_error_window_seconds
    oldest = next((s for s in _samples if s[0] >= horizon), None)
    if oldest is None:
        return None
    window_calls, window_errors = calls - oldest[1], errors - oldest[2]
    if window_calls < settings.breaker_k8s_min_calls:
        return None
    rate = window_errors / window_calls
    if rate > settings.breaker_k8s_error_rate:
        return f"Kubernetes API error rate {rate:.0%} -- the cluster is sick; not acting into it"
    return None


async def _record(
    session: AsyncSession, event: str, state: str, reason: str | None, owner: str
) -> None:
    await ledger.append(
        session, event, {"state": state, "reason": reason, "owner": owner}, actor=owner
    )
    _export(state)


def _export(state: str) -> None:
    metrics.breaker_state.labels(scope=GLOBAL).set(metrics.BREAKER_STATE_VALUES[state])

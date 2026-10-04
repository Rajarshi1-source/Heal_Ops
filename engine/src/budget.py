"""The remediation budget: a token bucket over actions on production.

An error budget for automation. **If HealOps needs to act more than 20 times an hour, the cluster
has a systemic problem and a human should be looking at it** -- so exhaustion escalates rather than
queues, and the policy gate denies the plan with a message saying exactly that.

    global       20 actions per rolling hour
    ns:payments   3    tighter where a mistake costs more
    ns:shop       8
    ns:chaos     50    the demo namespace can be noisy

A true token bucket, not a fixed window: `used_actions` drains continuously at `max / window` per
second, so an hour that started with a burst does not reset all at once at the top of the hour, and
there is no boundary at which twice the allowance can be spent back to back. The existing columns
carry it exactly: `window_start` is the time the bucket was last refilled, and it only advances by
the time that whole tokens took to come back, so integer storage loses nothing.

Rate-limiting HTTP calls is what most projects do. This rate-limits *changes to production*, which
is the thing that actually causes self-inflicted incidents.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

import metrics
from config import get_settings
from db.connection import db_now
from db.models import RemediationBudget
from policy import BudgetSnapshot

GLOBAL = "global"


def scope_for(namespace: str | None) -> str:
    return f"ns:{namespace}" if namespace else GLOBAL


def limit_for(scope: str) -> int:
    settings = get_settings()
    if scope == GLOBAL:
        return settings.budget_global_max
    namespace = scope.removeprefix("ns:")
    return settings.budget_namespace_max.get(namespace, settings.budget_default_namespace_max)


@dataclass(frozen=True, slots=True)
class Bucket:
    scope: str
    used: int
    max_actions: int

    @property
    def remaining(self) -> int:
        return max(self.max_actions - self.used, 0)


async def _bucket(
    session: AsyncSession, scope: str, now: datetime, *, for_update: bool
) -> RemediationBudget:
    """Load (or create) a scope's row and refill it to `now`."""
    settings = get_settings()
    row = await session.get(RemediationBudget, scope, with_for_update=for_update)
    if row is None:
        row = RemediationBudget(
            scope=scope,
            window_start=now,
            window_seconds=settings.budget_window_seconds,
            max_actions=limit_for(scope),
            used_actions=0,
            breaker_state="closed",
        )
        session.add(row)
        await session.flush()

    # Configuration is the source of the limit. A row created under an older limit follows the
    # current one, so tightening a namespace's budget is a config change, not a migration.
    row.max_actions = limit_for(scope)
    row.window_seconds = settings.budget_window_seconds

    if row.used_actions > 0 and row.max_actions > 0:
        per_token = row.window_seconds / row.max_actions
        refilled = int((now - row.window_start).total_seconds() // per_token)
        if refilled > 0:
            row.used_actions = max(row.used_actions - refilled, 0)
            row.window_start = (
                now
                if row.used_actions == 0
                else row.window_start + timedelta(seconds=refilled * per_token)
            )
    else:
        row.window_start = now
    await session.flush()
    return row


async def buckets(
    session: AsyncSession, namespace: str | None, *, for_update: bool = False
) -> list[Bucket]:
    """The scopes an action in this namespace draws from: its own, and the global one."""
    now = await db_now(session)
    scopes = [GLOBAL] if namespace is None else [GLOBAL, scope_for(namespace)]
    result = []
    for scope in scopes:
        row = await _bucket(session, scope, now, for_update=for_update)
        result.append(Bucket(scope=scope, used=row.used_actions, max_actions=row.max_actions))
    return result


async def snapshot(
    session: AsyncSession, namespace: str | None, *, breaker_state: str = "closed"
) -> BudgetSnapshot:
    """What the policy gate sees: whichever scope has the least room left.

    Reporting the tighter bucket is what lets one rule -- `used >= max` -- cover both the global and
    the per-namespace limit, so the policy file stays a list of readable sentences.
    """
    tightest = min(await buckets(session, namespace), key=lambda b: (b.remaining, b.max_actions))
    return BudgetSnapshot(
        scope=tightest.scope,
        used_actions=tightest.used,
        max_actions=tightest.max_actions,
        breaker_state=breaker_state,
    )


async def has_room(session: AsyncSession, namespace: str | None) -> Bucket | None:
    """The exhausted bucket, if any. Re-checked at the moment of acting, not only at planning.

    An approval can sit for ten minutes. The budget the human saw on the card is not necessarily
    the budget left when they click, and spending past the limit because the plan was gated earlier
    would make the limit a suggestion.
    """
    for bucket in await buckets(session, namespace, for_update=True):
        if bucket.remaining <= 0:
            return bucket
    return None


async def consume(session: AsyncSession, namespace: str | None) -> None:
    """Spend one action from every scope it draws on. Caller owns the transaction."""
    now = await db_now(session)
    scopes = [GLOBAL] if namespace is None else [GLOBAL, scope_for(namespace)]
    for scope in scopes:
        row = await _bucket(session, scope, now, for_update=True)
        row.used_actions += 1
        metrics.budget_used_ratio.labels(namespace=scope).set(
            row.used_actions / row.max_actions if row.max_actions else 1.0
        )
    await session.flush()

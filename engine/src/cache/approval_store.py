"""Approval cache -- a cache, and nothing more.

This module is deliberately thin, because the interesting decision is what it is *not* allowed to
do. Rev 1 kept approval state in Redis under a 600-second TTL and treated "key missing" as
"timed out". Restart Redis and every pending approval vanished, leaving the executor unable to
distinguish *expired* from *never existed* from *lost* -- so it would either drop incidents silently
or execute a disruptive action nobody approved.

PostgreSQL owns the decision. `expires_at` is a column, not a TTL. Redis exists here only so the
Slack callback path is fast, and every read is verified against the database before it authorises
anything. A safety control cannot live in a cache.

There is no `set_pending` that creates state. Cache entries only ever mirror a row that already
exists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from cache.redis_client import get_client

ApprovalStatus = Literal["pending", "approved", "rejected", "expired"]

# Short by design. The cache is an optimisation for the Slack round trip, so a stale entry costs a
# database read rather than a wrong decision.
_TTL_SECONDS = 60


def cache_key(execution_id: int | str) -> str:
    return f"approval:{execution_id}"


@dataclass(frozen=True, slots=True)
class CachedApproval:
    """A mirror of an `approvals` row. Never authoritative."""

    execution_id: int
    status: ApprovalStatus
    expires_at: str
    decided_by: str | None = None
    slack_message_ts: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "execution_id": self.execution_id,
                "status": self.status,
                "expires_at": self.expires_at,
                "decided_by": self.decided_by,
                "slack_message_ts": self.slack_message_ts,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> CachedApproval | None:
        try:
            data: dict[str, Any] = json.loads(raw)
            return cls(
                execution_id=int(data["execution_id"]),
                status=data["status"],
                expires_at=data["expires_at"],
                decided_by=data.get("decided_by"),
                slack_message_ts=data.get("slack_message_ts"),
            )
        except ValueError, KeyError, TypeError:
            # An unreadable cache entry is treated as absent, never as permission.
            return None


async def mirror(approval: CachedApproval) -> None:
    """Write through after the database row has been committed."""
    await get_client().set(cache_key(approval.execution_id), approval.to_json(), ex=_TTL_SECONDS)


async def peek(execution_id: int | str) -> CachedApproval | None:
    """Read the cached mirror.

    A hit is a hint for rendering, never an authorisation. `can_execute()` reads PostgreSQL.
    Returns None on a miss *and* on any error -- an unreachable cache must not become a decision.
    """
    try:
        raw = await get_client().get(cache_key(execution_id))
    except Exception:  # noqa: BLE001 - a cache failure degrades, never authorises
        return None
    return CachedApproval.from_json(raw) if raw else None


async def invalidate(execution_id: int | str) -> None:
    """Drop the mirror after a decision, so the next read comes from the source of truth."""
    await get_client().delete(cache_key(execution_id))


async def mark_notified(execution_id: int | str, ttl_seconds: int = 3600) -> bool:
    """Claim the right to post the Slack approval card exactly once.

    Returns True for the first caller. Without this, a retried job would post a second identical
    approval request and two humans could approve what is really one decision.
    """
    claimed = await get_client().set(
        f"approval:notified:{execution_id}", "1", nx=True, ex=ttl_seconds
    )
    return bool(claimed)

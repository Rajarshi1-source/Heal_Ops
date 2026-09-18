"""The approval cache.

Every test here exists to pin down what the cache is *not* allowed to do. Rev 1's defect D3 was
keeping approval state in Redis under a TTL and reading "key missing" as "timed out"; a cache
restart then made *expired*, *never existed* and *lost* indistinguishable.

So: an unreadable, corrupt or absent entry returns `None`, and `None` never authorises anything.
PostgreSQL owns the decision.
"""

from __future__ import annotations

from typing import Any

import pytest

from cache import approval_store
from cache.approval_store import CachedApproval

pytestmark = [pytest.mark.redis, pytest.mark.asyncio]


def approval(execution_id: int = 7, status: str = "pending") -> CachedApproval:
    return CachedApproval(
        execution_id=execution_id,
        status=status,  # type: ignore[arg-type]
        expires_at="2026-08-22T02:24:07+00:00",
        decided_by=None,
        slack_message_ts="1755830047.123456",
    )


async def test_mirror_then_peek_round_trips(redis_client_fixture: Any) -> None:
    await approval_store.mirror(approval())
    cached = await approval_store.peek(7)

    assert cached is not None
    assert cached.execution_id == 7
    assert cached.status == "pending"
    assert cached.slack_message_ts == "1755830047.123456"


async def test_peek_returns_none_on_a_miss(redis_client_fixture: Any) -> None:
    assert await approval_store.peek(999) is None


async def test_corrupt_entry_reads_as_absent(redis_client_fixture: Any) -> None:
    """Malformed cache content must not be interpreted, and must never be treated as permission."""
    await redis_client_fixture.set(approval_store.cache_key(7), "not-json{{{")
    assert await approval_store.peek(7) is None


async def test_entry_missing_required_fields_reads_as_absent(
    redis_client_fixture: Any,
) -> None:
    await redis_client_fixture.set(approval_store.cache_key(7), '{"status":"approved"}')
    assert await approval_store.peek(7) is None, (
        "a partial entry must not be usable; an approval with no execution id authorises nothing"
    )


async def test_invalidate_forces_the_next_read_to_the_source_of_truth(
    redis_client_fixture: Any,
) -> None:
    await approval_store.mirror(approval())
    await approval_store.invalidate(7)
    assert await approval_store.peek(7) is None


async def test_cache_entry_expires(redis_client_fixture: Any) -> None:
    """The TTL bounds staleness; it does not represent the approval deadline.

    The authoritative deadline is `approvals.expires_at`, a column. A short cache TTL means a stale
    entry costs a database read, never a wrong decision.
    """
    await approval_store.mirror(approval())
    ttl = await redis_client_fixture.ttl(approval_store.cache_key(7))
    assert 0 < ttl <= 60


async def test_there_is_no_way_to_create_state_through_the_cache() -> None:
    """The cache can only mirror a row that already exists.

    If this module grew a `create_pending()`, approval state could originate in a cache -- which is
    exactly the defect this design corrects. Guarding it as a test makes the omission deliberate
    rather than accidental.
    """
    exported = {name for name in dir(approval_store) if not name.startswith("_")}
    assert "mirror" in exported
    for forbidden in ("create", "create_pending", "set_pending", "approve", "grant"):
        assert forbidden not in exported


async def test_notification_claim_succeeds_once(redis_client_fixture: Any) -> None:
    """A retried job must not post a second approval card.

    Two identical cards mean two humans could each approve what is really one decision.
    """
    assert await approval_store.mark_notified(7) is True
    assert await approval_store.mark_notified(7) is False


async def test_notification_claims_are_per_execution(redis_client_fixture: Any) -> None:
    assert await approval_store.mark_notified(7) is True
    assert await approval_store.mark_notified(8) is True


async def test_decided_approval_carries_the_decider(redis_client_fixture: Any) -> None:
    decided = CachedApproval(
        execution_id=9,
        status="approved",
        expires_at="2026-08-22T02:24:07+00:00",
        decided_by="U024BE7LH",
        slack_message_ts="1755830047.123456",
    )
    await approval_store.mirror(decided)
    cached = await approval_store.peek(9)

    assert cached is not None
    assert cached.status == "approved"
    # Who approved is audit data, so it travels with the mirror for rendering -- but the ledger
    # entry, not this, is the record.
    assert cached.decided_by == "U024BE7LH"


async def test_from_json_rejects_a_non_object() -> None:
    assert CachedApproval.from_json("[]") is None
    assert CachedApproval.from_json("null") is None
    assert CachedApproval.from_json('"string"') is None


async def test_to_json_is_compact() -> None:
    text = approval().to_json()
    assert ", " not in text
    assert '": ' not in text

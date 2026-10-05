"""Locks, dedup and the stream, against a real Valkey.

The fencing test is the one that earns its place. A bare `SET NX` with a TTL can be released by the
wrong holder -- worker A stalls past the TTL, the lock expires, worker B acquires it, then A wakes
up and deletes B's lock. Storing the plan id as the value and comparing it inside a Lua script
makes that impossible, and the difference only shows up under exactly this scenario.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cache import dedup_store, locks, streams

pytestmark = [pytest.mark.redis, pytest.mark.asyncio]


# --- per-resource locking -----------------------------------------------------------------------


async def test_lock_is_exclusive(redis_client_fixture: Any) -> None:
    first = await locks.acquire("Deployment", "shop", "api", token="plan-1")
    assert first is not None

    # A second plan targeting the same object must not get in. One rolling back while the other
    # raises a memory limit leaves a final state nobody predicted.
    second = await locks.acquire("Deployment", "shop", "api", token="plan-2")
    assert second is None


async def test_lock_is_released_and_reacquirable(redis_client_fixture: Any) -> None:
    lock = await locks.acquire("Deployment", "shop", "api", token="plan-1")
    assert lock is not None
    assert await lock.release() is True

    again = await locks.acquire("Deployment", "shop", "api", token="plan-2")
    assert again is not None


async def test_release_is_fenced_by_plan_id(redis_client_fixture: Any) -> None:
    """The scenario a bare SET NX gets wrong.

    Plan 1 holds the lock, its TTL expires, plan 2 acquires it, and plan 1 then tries to clean up.
    Plan 1 must not be able to release a lock it no longer owns.
    """
    stale = await locks.acquire("Deployment", "shop", "api", token="plan-1", ttl_seconds=1)
    assert stale is not None

    await asyncio.sleep(1.2)  # let it expire naturally

    successor = await locks.acquire("Deployment", "shop", "api", token="plan-2")
    assert successor is not None

    # The stale holder's release is a no-op, and the successor still holds the lock.
    assert await stale.release() is False
    assert await locks.holder("Deployment", "shop", "api") == "plan-2"


async def test_extend_only_works_for_the_holder(redis_client_fixture: Any) -> None:
    lock = await locks.acquire("Deployment", "shop", "api", token="plan-1")
    assert lock is not None
    assert await lock.extend(120) is True

    impostor = locks.ResourceLock(key=lock.key, token="plan-2")
    assert await impostor.extend(120) is False


async def test_lock_keys_are_scoped_per_resource(redis_client_fixture: Any) -> None:
    # Different objects must never serialise against each other; two unrelated incidents in
    # different namespaces should proceed in parallel.
    a = await locks.acquire("Deployment", "shop", "api", token="p1")
    b = await locks.acquire("Deployment", "payments", "api", token="p2")
    c = await locks.acquire("Node", None, "worker-1", token="p3")
    assert all(x is not None for x in (a, b, c))

    assert locks.lock_key("Deployment", "shop", "api") == "lock:Deployment:shop/api"
    assert locks.lock_key("Node", None, "worker-1") == "lock:Node:-/worker-1"


async def test_context_manager_raises_when_contended(redis_client_fixture: Any) -> None:
    held = await locks.acquire("Deployment", "shop", "api", token="plan-1")
    assert held is not None

    with pytest.raises(locks.LockNotAcquired, match="already being remediated"):
        async with locks.per_resource("Deployment", "shop", "api", token="plan-2"):
            pass


async def test_context_manager_releases_on_exception(redis_client_fixture: Any) -> None:
    # An execution that raises must not leave the resource locked for the TTL; the next incident
    # on that object would be blocked for two minutes for no reason.
    with pytest.raises(ValueError, match="boom"):
        async with locks.per_resource("Deployment", "shop", "api", token="plan-1"):
            raise ValueError("boom")

    assert await locks.holder("Deployment", "shop", "api") is None


# --- dedup and idempotency ----------------------------------------------------------------------


async def test_first_delivery_is_not_deduplicated(redis_client_fixture: Any) -> None:
    assert await dedup_store.seen_recently("fp-1") is False
    assert await dedup_store.seen_recently("fp-1") is True


async def test_a_flapping_alert_gets_a_plan_for_its_new_incident(redis_client_fixture: Any) -> None:
    """Regression, found on the kind cluster: a fingerprint that fires, resolves and fires again
    opens a second incident -- and dedup on the fingerprint alone gave it no plan job, so it sat
    `open` forever. Redelivery into the SAME incident is still collapsed."""
    assert await dedup_store.seen_recently("fp-flap", incident_id=1) is False
    assert await dedup_store.seen_recently("fp-flap", incident_id=1) is True
    assert await dedup_store.seen_recently("fp-flap", incident_id=2) is False


async def test_dedup_is_per_fingerprint(redis_client_fixture: Any) -> None:
    """Why dedup alone cannot handle a storm.

    Forty pods produce forty distinct fingerprints, so nothing deduplicates. That was defect D5,
    and it is why correlation exists rather than a longer dedup window.
    """
    for i in range(40):
        assert await dedup_store.seen_recently(f"fp-pod-{i}") is False


async def test_idempotency_claim_succeeds_once(redis_client_fixture: Any) -> None:
    assert await dedup_store.claim("fp-1", "crashloop_rollback", 7) is True
    assert await dedup_store.claim("fp-1", "crashloop_rollback", 7) is False

    # A different runbook or incident is a different claim.
    assert await dedup_store.claim("fp-1", "cpu_scale_up", 7) is True
    assert await dedup_store.claim("fp-1", "crashloop_rollback", 8) is True


async def test_released_claim_can_be_retried(redis_client_fixture: Any) -> None:
    # If the work never actually started, a retry must not be silently swallowed.
    assert await dedup_store.claim("fp-1", "crashloop_rollback", 7) is True
    await dedup_store.release_claim("fp-1", "crashloop_rollback", 7)
    assert await dedup_store.claim("fp-1", "crashloop_rollback", 7) is True


# --- the stream ---------------------------------------------------------------------------------


async def test_consumer_groups_are_separate(redis_client_fixture: Any) -> None:
    """The bulkhead.

    `exec` and `verify` each get their own copy of every message. Sharing a group would let a
    verification backlog -- five minutes per execution -- block new incident handling, which is the
    failure mode where the system stops responding because it is busy responding.
    """
    await streams.ensure_groups()
    await streams.enqueue("plan", {"incident_id": 1})

    exec_jobs = await streams.read("exec", "worker-1", block_ms=100)
    verify_jobs = await streams.read("verify", "worker-1", block_ms=100)

    assert len(exec_jobs) == 1
    assert len(verify_jobs) == 1
    assert exec_jobs[0].payload["incident_id"] == 1
    assert verify_jobs[0].payload["incident_id"] == 1


async def test_ensure_groups_is_idempotent(redis_client_fixture: Any) -> None:
    # Called on every startup, including on a stream that already exists.
    await streams.ensure_groups()
    await streams.ensure_groups()


async def test_unacked_message_stays_pending(redis_client_fixture: Any) -> None:
    await streams.ensure_groups()
    await streams.enqueue("plan", {"incident_id": 1})

    jobs = await streams.read("exec", "worker-1", block_ms=100)
    assert len(jobs) == 1

    # Not acked, so a second read sees nothing new but the message is not lost either.
    assert await streams.read("exec", "worker-1", block_ms=100) == []

    reclaimed = await streams.claim_stale("exec", "worker-2", min_idle_ms=0)
    assert [j.payload for j in reclaimed] == [{"incident_id": 1}]


async def test_ack_removes_the_message_from_pending(redis_client_fixture: Any) -> None:
    await streams.ensure_groups()
    await streams.enqueue("plan", {"incident_id": 1})

    jobs = await streams.read("exec", "worker-1", block_ms=100)
    await streams.ack("exec", jobs[0].message_id)

    assert await streams.claim_stale("exec", "worker-2", min_idle_ms=0) == []


async def test_claim_stale_reports_the_delivery_count(redis_client_fixture: Any) -> None:
    # The delivery count drives the retry budget; without it a poison message loops forever.
    await streams.ensure_groups()
    await streams.enqueue("plan", {"incident_id": 1})

    await streams.read("exec", "worker-1", block_ms=100)
    first = await streams.claim_stale("exec", "worker-2", min_idle_ms=0)
    assert first[0].delivery_count >= 2
    assert await streams.should_retry(first[0]) is True


async def test_exhausted_retries_go_to_the_dlq(redis_client_fixture: Any) -> None:
    """Silent message loss is the failure the DLQ exists to prevent."""
    await streams.ensure_groups()
    await streams.enqueue("plan", {"incident_id": 99})

    jobs = await streams.read("exec", "worker-1", block_ms=100)
    job = jobs[0]

    await streams.to_dlq("exec", job, reason="max retries exceeded")

    assert await streams.dlq_depth() == 1
    # And it is acked off the main stream, so it stops being redelivered.
    assert await streams.claim_stale("exec", "worker-2", min_idle_ms=0) == []


async def test_batch_enqueue_preserves_every_job(redis_client_fixture: Any) -> None:
    await streams.ensure_groups()
    ids = await streams.enqueue_batch([("plan", {"incident_id": i}) for i in range(5)])
    assert len(ids) == 5

    jobs = await streams.read("exec", "worker-1", count=10, block_ms=100)
    assert sorted(j.payload["incident_id"] for j in jobs) == [0, 1, 2, 3, 4]


async def test_depth_reports_zero_for_a_missing_stream(redis_client_fixture: Any) -> None:
    # Readiness and the /metrics endpoint both call this before anything has been queued.
    assert await streams.dlq_depth() == 0
    assert await streams.depth() == 0


async def test_a_long_poll_never_blocks_past_the_socket_timeout(
    redis_client_fixture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 15 s verification poll used to block for 15 s on a client whose socket gives up at 5 s.
    from cache import redis_client

    await streams.ensure_groups()
    seen: list[int] = []
    original = redis_client_fixture.xreadgroup

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["block"])
        return await original(*args, **{**kwargs, "block": 10})

    monkeypatch.setattr(redis_client_fixture, "xreadgroup", spy)
    await streams.read("verify", "worker-1", block_ms=15_000)

    assert seen == [streams.MAX_BLOCK_MS]
    assert streams.MAX_BLOCK_MS < redis_client.SOCKET_TIMEOUT_SECONDS * 1000

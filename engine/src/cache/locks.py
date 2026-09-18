"""Per-resource distributed locks, fenced by plan id.

Two runbooks must never mutate the same object concurrently: one rolling back while the other
raises a memory limit leaves a final state nobody predicted. The lock serialises them.

The **fencing token** is the part that is easy to omit and expensive to omit. A bare `SET NX` with
a TTL can be released by the wrong holder: worker A stalls past the TTL, the lock expires, worker B
acquires it, then A wakes up and deletes B's lock. Storing the plan id as the value and checking it
in a Lua script on release makes that impossible -- a holder can only release the lock it owns.

Note what this does and does not solve. Locking prevents *conflicts*; it does nothing about
*volume*. Forty queued remediations still execute, just politely one at a time. Volume is the
remediation budget's problem.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from cache.redis_client import decoded, get_client
from config import get_settings

# Release only if we still hold it. Compare-and-delete must be atomic, so it is a script.
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

# Extend only if we still hold it, for an execution that legitimately runs long.
_EXTEND = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


class LockNotAcquired(RuntimeError):
    """Another plan is already mutating this resource."""


def lock_key(kind: str, namespace: str | None, name: str) -> str:
    return f"lock:{kind}:{namespace or '-'}/{name}"


@dataclass(frozen=True, slots=True)
class ResourceLock:
    key: str
    token: str

    async def extend(self, seconds: int | None = None) -> bool:
        ttl = seconds or get_settings().lock_ttl_seconds
        result = await get_client().eval(_EXTEND, 1, self.key, self.token, str(ttl))
        return bool(result)

    async def release(self) -> bool:
        result = await get_client().eval(_RELEASE, 1, self.key, self.token)
        return bool(result)


async def acquire(
    kind: str,
    namespace: str | None,
    name: str,
    token: str,
    ttl_seconds: int | None = None,
) -> ResourceLock | None:
    """Try once. Returns None if another plan holds the lock.

    Deliberately non-blocking: an incident that has to queue behind another mutation of the same
    object is usually already handled by the time the lock frees, and waiting would hold a worker
    for the full TTL.
    """
    ttl = ttl_seconds or get_settings().lock_ttl_seconds
    key = lock_key(kind, namespace, name)
    acquired = await get_client().set(key, token, nx=True, ex=ttl)
    return ResourceLock(key=key, token=token) if acquired else None


async def holder(kind: str, namespace: str | None, name: str) -> str | None:
    """Which plan currently holds this lock, if any. For diagnostics and the dashboard."""
    return decoded(await get_client().get(lock_key(kind, namespace, name)))


@asynccontextmanager
async def per_resource(
    kind: str,
    namespace: str | None,
    name: str,
    token: str,
    ttl_seconds: int | None = None,
) -> AsyncIterator[ResourceLock]:
    """The only sanctioned way to open a mutating window.

    `token` must be the plan id, so the ledger, the lock and the execution all name the same
    thing when something goes wrong.
    """
    lock = await acquire(kind, namespace, name, token, ttl_seconds)
    if lock is None:
        current = await holder(kind, namespace, name)
        raise LockNotAcquired(
            f"{kind} {namespace or '-'}/{name} is already being remediated by plan {current}"
        )
    try:
        yield lock
    finally:
        await lock.release()

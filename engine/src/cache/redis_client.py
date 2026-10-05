"""Valkey/Redis connection.

Everything in here is **disposable**. Say this out loud in review: if Valkey is wiped, HealOps
loses in-flight queue position and cold caches. It does not lose approvals, plans, executions, or
the audit chain -- those live in PostgreSQL, which is why a cache restart is an inconvenience
rather than a safety incident.
"""

from __future__ import annotations

from typing import Any, cast

import redis.asyncio as redis

from config import get_settings

SOCKET_TIMEOUT_SECONDS = 5

_client: redis.Redis | None = None


def decoded(value: Any) -> str | None:
    """Narrow a Redis reply to `str`.

    The client is created with `decode_responses=True`, so every reply is already a `str`. The type
    stubs describe the general `bytes | str` case because they cannot see that flag, so this helper
    puts the narrowing in one place instead of scattering casts through the callers.
    """
    return cast(str | None, value)


def get_client() -> redis.Redis:
    global _client  # noqa: PLW0603
    if _client is None:
        settings = get_settings()
        _client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=3,
            health_check_interval=30,
            retry_on_timeout=True,
        )
    return _client


async def ping() -> bool:
    try:
        return bool(await get_client().ping())
    except Exception:  # noqa: BLE001 - readiness must report, never raise
        return False


async def close() -> None:
    global _client  # noqa: PLW0603
    if _client is not None:
        await _client.aclose()
    _client = None

"""Shared fixtures.

Tests marked `db` need a real PostgreSQL 18: `uuidv7()`, range partitioning, and the grant model
that makes the ledger append-only are all server behaviour, and mocking them would test the mock.
CI provides the server as a service container; locally, `make dev` or the test container does.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "HEALOPS_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:55432/healops",
)


#: A server found unreachable once is skipped for the rest of the session. Re-probing per test cost
#: a refused connection each -- seconds apiece on Windows -- and turned a missing container into a
#: suite that looked hung.
_unreachable: dict[str, str] = {}


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """A session against the test database, rolled back afterwards.

    The ledger cannot be cleaned up with DELETE -- that is the point of it -- so each test works
    inside a transaction that is rolled back, and the advisory lock the ledger takes is
    transaction-scoped so it is released with it.
    """
    if "postgres" in _unreachable:
        pytest.skip(_unreachable["postgres"])
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        _unreachable["postgres"] = f"PostgreSQL not reachable at {TEST_DATABASE_URL}: {exc}"
        pytest.skip(_unreachable["postgres"])

    # Mirror the production sessionmaker exactly, `autoflush=False` included. With autoflush on,
    # a module that forgets to flush its own writes still passes its tests and then misbehaves in
    # production -- which is precisely how the ledger's chain-linking bug hid.
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with engine.connect() as connection:
        trans = await connection.begin()
        session = maker(bind=connection)
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()
    await engine.dispose()


TEST_REDIS_URL = os.environ.get("HEALOPS_TEST_REDIS_URL", "redis://localhost:56379/0")


@pytest_asyncio.fixture
async def redis_client_fixture() -> AsyncIterator[Any]:
    """A real Valkey/Redis, flushed between tests.

    Locks, streams and dedup are all built out of server-side semantics -- `SET NX`, Lua
    compare-and-delete, consumer-group bookkeeping. A fake would test the fake.
    """
    import redis.asyncio as aioredis

    from cache import redis_client as module

    if "redis" in _unreachable:
        pytest.skip(_unreachable["redis"])
    client = aioredis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        _unreachable["redis"] = f"Redis/Valkey not reachable at {TEST_REDIS_URL}: {exc}"
        pytest.skip(_unreachable["redis"])

    await client.flushdb()

    # Point the module-level singleton at the test server for the duration of the test.
    previous = module._client
    module._client = client
    try:
        yield client
    finally:
        module._client = previous
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"

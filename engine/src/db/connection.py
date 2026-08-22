"""Async engine and session management.

The rule this module exists to enforce: **one transaction per state transition, with the ledger
append inside it.** State and audit cannot diverge, so `transaction()` is the only sanctioned way
to write, and the ledger's append runs on the same session.

If PostgreSQL is unreachable the engine stops acting. That is deliberate -- the ledger is a
precondition for action, not a side effect of it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


class DatabaseUnavailable(RuntimeError):
    """Raised where a caller must fail closed rather than proceed without durable state."""


def get_engine() -> AsyncEngine:
    global _engine  # noqa: PLW0603
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=10,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=1800,
            echo=False,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker  # noqa: PLW0603
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), expire_on_commit=False, autoflush=False
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Read-only or best-effort work. Does not commit."""
    async with get_sessionmaker()() as session:
        yield session


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]:
    """One state transition, committed atomically with its ledger entry.

    Rolls back on any exception. Callers must not commit inside the block -- the whole point is
    that the ledger append and the state change share a single commit boundary.
    """
    async with get_sessionmaker()() as session, session.begin():
        yield session


async def db_now(session: AsyncSession) -> datetime:
    """The database clock is the single source of time for the ledger.

    Two engine replicas with skewed clocks would otherwise produce a hash chain whose timestamps
    go backwards, and a chain that disagrees with itself about ordering is not evidence.
    """
    result = await session.execute(text("SELECT now()"))
    return result.scalar_one()


async def ping() -> bool:
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - readiness must report, never raise
        return False


async def dispose() -> None:
    global _engine, _sessionmaker  # noqa: PLW0603
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None

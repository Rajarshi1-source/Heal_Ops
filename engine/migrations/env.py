"""Alembic environment.

Migrations run as the *owner* role, not as `healops_app`. That separation is what lets migration
002 revoke UPDATE and DELETE on the ledger from the application while still being able to create
partitions.
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from db.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The migration user may differ from the runtime user; HEALOPS_MIGRATION_DATABASE_URL wins if set.
database_url = (
    os.environ.get("HEALOPS_MIGRATION_DATABASE_URL")
    or os.environ.get("HEALOPS_DATABASE_URL")
    or "postgresql+asyncpg://postgres:postgres@localhost:5432/healops"
)
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def _include_object(obj: object, name: str | None, type_: str, *_: object) -> bool:
    # Ledger partitions are created by a CronJob and by migration 002, never by autogenerate.
    return not (type_ == "table" and name is not None and name.startswith("audit_ledger_"))


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=_include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())

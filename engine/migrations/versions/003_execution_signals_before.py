"""Persist the pre-action signal window on the execution.

Verification compares the target after the action with the target immediately before it, mostly to
tell an alert the action *caused* from one that was already firing. That "before" was held in the
memory of the process that acted -- so a leader that died mid-window took it with it, and its
successor could only verify against nothing. Same reasoning as the compensation column: anything
verification depends on must outlive the process that started it.

Additive only (expand step of expand-contract): existing rows get `{}`, which the verifier reads as
"not observed" rather than as healthy.

Revision ID: 003
Revises: 002
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runbook_executions",
        sa.Column(
            "signals_before",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("runbook_executions", "signals_before")

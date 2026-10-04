"""Count shadow plans a human actually judged.

Shadow agreement is "of the shadow plans a human compared with what they did, how many matched".
`shadow_plans` counts every plan recorded; a plan nobody looked at is neither an agreement nor a
disagreement, so dividing by it would let silence drag a good runbook's agreement rate down -- or,
read the other way, let an unexamined runbook look as if it had a track record. The ladder promotes
on `shadow_agreements / shadow_judged`.

Additive only (expand step).

Revision ID: 004
Revises: 003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runbook_promotion",
        sa.Column("shadow_judged", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("runbook_promotion", "shadow_judged")

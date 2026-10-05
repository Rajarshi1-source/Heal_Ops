"""Record when an incident closed -- in the database, from the database clock.

`incidents.closed_at` existed from the first migration and nothing ever wrote it, so every incident
read as never closed (found while measuring the chaos run's scorecard). A trigger, not application
code: every path that ends an incident -- the verifier, the watchdog, the approval reaper, a human
in psql -- gets it, and the time is PostgreSQL's, the same single clock the ledger uses. Set once:
the first terminal state is when it closed.

Backfills the incidents already terminal, from their last ledger entry.

Additive only (expand step).

Revision ID: 005
Revises: 004
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TERMINAL = "('remediated', 'reverted', 'escalated')"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION incident_closed_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.state IN {TERMINAL} AND NEW.closed_at IS NULL THEN
                NEW.closed_at := now();
            END IF;
            RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER incidents_closed_at BEFORE INSERT OR UPDATE OF state ON incidents "
        "FOR EACH ROW EXECUTE FUNCTION incident_closed_at();"
    )
    op.execute(
        f"""
        UPDATE incidents i SET closed_at = l.last
        FROM (SELECT incident_id, max(occurred_at) AS last FROM audit_ledger GROUP BY incident_id) l
        WHERE l.incident_id = i.id AND i.state IN {TERMINAL} AND i.closed_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS incidents_closed_at ON incidents;")
    op.execute("DROP FUNCTION IF EXISTS incident_closed_at();")

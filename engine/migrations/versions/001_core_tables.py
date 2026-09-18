"""Core tables: alerts, incidents, executions, approvals.

This is the Rev 1 base. Migration 002 adds the Rev 2 safety tables on top of it, and keeping the
two separate is deliberate -- the Rev 1 -> Rev 2 boundary is exactly where the five defects were
fixed, and the migration history should show that rather than hide it.

Revision ID: 001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # uuidv7() is built into PostgreSQL 18. Fail here rather than at the first ledger insert.
    op.execute(
        """
        DO $$
        BEGIN
            IF current_setting('server_version_num')::int < 180000 THEN
                RAISE EXCEPTION
                    'HealOps requires PostgreSQL 18+ for uuidv7() (found %)',
                    current_setting('server_version');
            END IF;
        END
        $$;
        """
    )

    op.create_table(
        "incidents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="open"),
        sa.Column("cause_scope", sa.String(16), nullable=False),
        sa.Column("cause_target", sa.Text(), nullable=False),
        sa.Column("cause_alertname", sa.String(200), nullable=False),
        sa.Column("target_key", sa.Text(), nullable=False),
        sa.Column("namespace", sa.String(200), nullable=True),
        sa.Column("precedence", sa.Integer(), nullable=False, server_default="9"),
        sa.Column("correlation_reason", sa.Text(), nullable=False, server_default="new_incident"),
        sa.Column("alert_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "cause_scope IN ('node','workload','pod')", name="ck_incidents_cause_scope"
        ),
    )
    op.create_index("ix_incidents_open_target", "incidents", ["target_key", "state"])
    op.create_index("ix_incidents_opened_at", "incidents", ["opened_at"])

    op.create_table(
        "alert_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("alertname", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("severity", sa.String(20), nullable=True),
        sa.Column("namespace", sa.String(200), nullable=True),
        sa.Column("pod_name", sa.String(200), nullable=True),
        sa.Column("node_name", sa.String(200), nullable=True),
        sa.Column("labels", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("annotations", postgresql.JSONB(), nullable=False, server_default="{}"),
        # The complete v2 payload, verbatim.
        sa.Column("full_payload", postgresql.JSONB(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("incident_id", sa.BigInteger(), nullable=True),
        sa.Column("role", sa.String(16), nullable=False, server_default="cause"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="SET NULL"),
        sa.CheckConstraint("role IN ('cause','symptom')", name="ck_alert_events_role"),
    )
    op.create_index("ix_alert_events_fingerprint", "alert_events", ["fingerprint", "received_at"])
    op.create_index(
        "ix_alert_events_ns_name", "alert_events", ["namespace", "alertname", "received_at"]
    )
    op.create_index("ix_alert_events_incident", "alert_events", ["incident_id"])

    op.create_table(
        "runbook_executions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("incident_id", sa.BigInteger(), nullable=False),
        sa.Column("runbook", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("state_before", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("state_after", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"]),
    )
    op.create_index("ix_executions_incident", "runbook_executions", ["incident_id"])
    op.create_index("ix_executions_status", "runbook_executions", ["status"])

    # Approvals: PostgreSQL is the source of truth and `expires_at` is the authoritative deadline.
    # Defect D3 in Rev 1 was keeping this only in Redis with a TTL, which meant a cache restart
    # made "expired" and "lost" indistinguishable.
    op.create_table(
        "approvals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            nullable=False,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("execution_id", sa.BigInteger(), nullable=False),
        sa.Column("incident_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("slack_message_ts", sa.Text(), nullable=True),
        sa.Column("slack_channel", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["execution_id"], ["runbook_executions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"]),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','expired')", name="ck_approvals_status"
        ),
    )
    # One approval per execution: a double click in Slack is one decision, not two.
    op.create_index("ix_approvals_execution", "approvals", ["execution_id"], unique=True)
    op.create_index("ix_approvals_pending_expiry", "approvals", ["status", "expires_at"])


def downgrade() -> None:
    op.drop_table("approvals")
    op.drop_table("runbook_executions")
    op.drop_table("alert_events")
    op.drop_table("incidents")

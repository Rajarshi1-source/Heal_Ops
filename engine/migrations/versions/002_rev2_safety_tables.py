"""Rev 2 safety tables: plans, verifications, the hash-chained ledger, promotion, budget, recurrence.

Three choices here are worth being able to defend on sight:

* `uuidv7()` for ledger and plan IDs -- timestamp-ordered, so index locality is good and the chain
  reads chronologically. This is the specific reason the project requires PostgreSQL 18.
* **Monthly partitions on the ledger** -- an append-only table that only ever grows should be
  partitioned before it needs to be. A missing partition is an insert failure, and under the
  fail-closed rule that means no remediation, so `ensure_ledger_partition` is called by a CronJob.
* **`REVOKE UPDATE, DELETE`** -- enforcement at the grant level, not by convention. An audit table
  the application can rewrite is not an audit table.

Revision ID: 002
Revises: 001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "healops_app"


def upgrade() -> None:
    # --- 1. RemediationPlan: what we intend to do, before we do it ------------------------------
    op.create_table(
        "remediation_plans",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            nullable=False,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("incident_id", sa.BigInteger(), nullable=False),
        sa.Column("runbook", sa.Text(), nullable=False),
        sa.Column("planner_version", sa.Text(), nullable=False, server_default="0"),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_namespace", sa.Text(), nullable=True),
        sa.Column("target_name", sa.Text(), nullable=False),
        sa.Column("actions", postgresql.JSONB(), nullable=False),
        sa.Column("compensation", postgresql.JSONB(), nullable=False),
        sa.Column("reversibility", sa.Text(), nullable=False),
        sa.Column("blast_radius", postgresql.JSONB(), nullable=False),
        sa.Column("durability", sa.Text(), nullable=False),
        sa.Column("ttl_seconds", sa.Integer(), nullable=True),
        sa.Column("expected_signal", postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "cost_delta_inr_month", sa.Numeric(12, 2), nullable=False, server_default="0"
        ),
        sa.Column("refusal_reason", sa.Text(), nullable=True),
        sa.Column("policy_decision", sa.Text(), nullable=False),
        sa.Column(
            "policy_reasons", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"]),
        sa.CheckConstraint(
            "reversibility IN ('reversible','partially_reversible','irreversible')",
            name="ck_plans_reversibility",
        ),
        sa.CheckConstraint("durability IN ('ephemeral','durable')", name="ck_plans_durability"),
        sa.CheckConstraint(
            "policy_decision IN ('allow','require_approval','deny')", name="ck_plans_decision"
        ),
        # An ephemeral fix with no TTL is a permanent hand-edit nobody is tracking.
        sa.CheckConstraint(
            "durability <> 'ephemeral' OR ttl_seconds IS NOT NULL",
            name="ck_plans_ephemeral_needs_ttl",
        ),
    )
    op.create_index("ix_plans_incident", "remediation_plans", ["incident_id", "created_at"])

    # --- 2. Verification: did it actually work? -------------------------------------------------
    op.create_table(
        "verifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            nullable=False,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("execution_id", sa.BigInteger(), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        # Nullable on purpose: NULL is "not observed", which must stay distinguishable from False.
        sa.Column("alert_cleared", sa.Boolean(), nullable=True),
        sa.Column("signals_before", postgresql.JSONB(), nullable=False),
        sa.Column("signals_after", postgresql.JSONB(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("compensated", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("reasons", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["runbook_executions.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "outcome IN ('remediated','not_recovered','inconclusive')",
            name="ck_verifications_outcome",
        ),
    )
    op.create_index("ix_verifications_execution", "verifications", ["execution_id"])

    # --- 3. The tamper-evident ledger -----------------------------------------------------------
    # Created with raw SQL: Alembic's create_table cannot express PARTITION BY, and the composite
    # primary key must include the partition key.
    op.execute(
        """
        CREATE TABLE audit_ledger (
            seq             BIGSERIAL,
            id              UUID NOT NULL DEFAULT uuidv7(),
            occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            actor           TEXT NOT NULL,
            event_type      TEXT NOT NULL,
            incident_id     BIGINT,
            execution_id    BIGINT,
            payload         JSONB NOT NULL,
            prev_hash       CHAR(64) NOT NULL,
            hash            CHAR(64) NOT NULL,
            bundle_sha256   CHAR(64),
            PRIMARY KEY (seq, occurred_at)
        ) PARTITION BY RANGE (occurred_at);
        """
    )
    op.execute("CREATE UNIQUE INDEX ix_ledger_hash ON audit_ledger (hash, occurred_at);")
    op.execute("CREATE INDEX ix_ledger_incident ON audit_ledger (incident_id);")
    op.execute("CREATE INDEX ix_ledger_occurred_at ON audit_ledger (occurred_at);")

    # A helper the partition CronJob calls, so creating next month's partition is one statement
    # rather than a templated migration. A missing partition means inserts fail, which under
    # fail-closed means no remediation -- so this path is tested, not assumed.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ensure_ledger_partition(for_month DATE)
        RETURNS TEXT AS $$
        DECLARE
            start_date DATE := date_trunc('month', for_month)::DATE;
            end_date   DATE := (date_trunc('month', for_month) + INTERVAL '1 month')::DATE;
            part_name  TEXT := 'audit_ledger_' || to_char(start_date, 'YYYY_MM');
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = part_name) THEN
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF audit_ledger FOR VALUES FROM (%L) TO (%L)',
                    part_name, start_date, end_date
                );
                EXECUTE format('GRANT SELECT, INSERT ON %I TO %I', part_name, '""" + APP_ROLE + """');
            END IF;
            RETURN part_name;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    # Current month plus the next two, so a partition gap cannot halt remediation while the
    # CronJob is being wired up.
    op.execute("SELECT ensure_ledger_partition(CURRENT_DATE);")
    op.execute("SELECT ensure_ledger_partition((CURRENT_DATE + INTERVAL '1 month')::DATE);")
    op.execute("SELECT ensure_ledger_partition((CURRENT_DATE + INTERVAL '2 months')::DATE);")

    # Append-only enforcement at the grant level. A later migration must never grant these back.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                GRANT SELECT, INSERT ON audit_ledger TO {APP_ROLE};
                REVOKE UPDATE, DELETE, TRUNCATE ON audit_ledger FROM {APP_ROLE};
                GRANT USAGE, SELECT ON SEQUENCE audit_ledger_seq_seq TO {APP_ROLE};
            END IF;
        END
        $$;
        """
    )

    # --- 4. Promotion ladder --------------------------------------------------------------------
    op.create_table(
        "runbook_promotion",
        sa.Column("runbook", sa.Text(), nullable=False),
        sa.Column("namespace", sa.Text(), nullable=False),
        # New (runbook, namespace) pairs start in shadow: authority is earned, never assumed.
        sa.Column("mode", sa.Text(), nullable=False, server_default="shadow"),
        sa.Column("consecutive_good", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_executions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_reverted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shadow_plans", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shadow_agreements", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_outcome", sa.Text(), nullable=True),
        sa.Column(
            "last_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("changed_reason", sa.Text(), nullable=True),
        sa.Column("code_fingerprint", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("runbook", "namespace"),
        sa.CheckConstraint("mode IN ('shadow','approval','auto')", name="ck_promotion_mode"),
    )

    # --- 5. Remediation budget and the global breaker --------------------------------------------
    op.create_table(
        "remediation_budget",
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column(
            "window_start",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("window_seconds", sa.Integer(), nullable=False, server_default="3600"),
        sa.Column("max_actions", sa.Integer(), nullable=False),
        sa.Column("used_actions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("breaker_state", sa.Text(), nullable=False, server_default="closed"),
        sa.Column("breaker_reason", sa.Text(), nullable=True),
        sa.Column("breaker_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("breaker_owner", sa.Text(), nullable=True),
        sa.Column("half_open_plan_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.PrimaryKeyConstraint("scope"),
        sa.CheckConstraint(
            "breaker_state IN ('closed','open','half_open')", name="ck_budget_breaker_state"
        ),
    )
    # An error budget for automation: if HealOps needs to act more than this in an hour, the
    # cluster has a systemic problem and a human should be looking at it.
    op.execute(
        """
        INSERT INTO remediation_budget (scope, max_actions) VALUES
            ('global', 20),
            ('ns:payments', 3),
            ('ns:shop', 8),
            ('ns:chaos', 50)
        ON CONFLICT (scope) DO NOTHING;
        """
    )

    # --- 6. Recurrence tracking for the anti-masking guard ---------------------------------------
    op.create_table(
        "remediation_recurrence",
        sa.Column("runbook", sa.Text(), nullable=False),
        sa.Column("target_key", sa.Text(), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("issue_url", sa.Text(), nullable=True),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("runbook", "target_key", "window_start"),
    )

    # --- 7. Executions gain the Rev 2 fields ----------------------------------------------------
    op.add_column(
        "runbook_executions",
        sa.Column("plan_id", postgresql.UUID(as_uuid=False), nullable=True),
    )
    op.add_column(
        "runbook_executions",
        sa.Column("mode", sa.Text(), nullable=False, server_default="auto"),
    )
    op.add_column("runbook_executions", sa.Column("durability", sa.Text(), nullable=True))
    op.add_column(
        "runbook_executions",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("runbook_executions", sa.Column("pr_url", sa.Text(), nullable=True))
    op.add_column(
        "runbook_executions",
        sa.Column("gitops_managed", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "runbook_executions",
        sa.Column("gitops_paused", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "runbook_executions",
        sa.Column("compensated", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_foreign_key(
        "fk_executions_plan", "runbook_executions", "remediation_plans", ["plan_id"], ["id"]
    )
    op.create_check_constraint(
        "ck_executions_mode", "runbook_executions", "mode IN ('shadow','approval','auto')"
    )
    # The watchdog's two sweep queries.
    op.create_index(
        "ix_executions_awaiting_verification",
        "runbook_executions",
        ["status", "finished_at"],
    )
    op.create_index("ix_executions_expiry", "runbook_executions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_executions_expiry", table_name="runbook_executions")
    op.drop_index("ix_executions_awaiting_verification", table_name="runbook_executions")
    op.drop_constraint("ck_executions_mode", "runbook_executions", type_="check")
    op.drop_constraint("fk_executions_plan", "runbook_executions", type_="foreignkey")
    for column in (
        "compensated",
        "gitops_paused",
        "gitops_managed",
        "pr_url",
        "expires_at",
        "durability",
        "mode",
        "plan_id",
    ):
        op.drop_column("runbook_executions", column)

    op.drop_table("remediation_recurrence")
    op.drop_table("remediation_budget")
    op.drop_table("runbook_promotion")
    op.execute("DROP FUNCTION IF EXISTS ensure_ledger_partition(DATE);")
    op.execute("DROP TABLE IF EXISTS audit_ledger CASCADE;")
    op.drop_table("verifications")
    op.drop_table("remediation_plans")

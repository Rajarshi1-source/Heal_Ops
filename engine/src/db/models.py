"""SQLAlchemy models.

Shape notes that are easy to get wrong and expensive to fix later:

* `audit_ledger` is append-only and hash-chained. The application role has INSERT and SELECT only;
  the migration revokes UPDATE and DELETE. It is mapped here for reads and inserts, never updates.
* `approvals.expires_at` is the authoritative deadline. It is a column, not a Redis TTL, because a
  TTL that deletes the row destroys the evidence and leaves the executor unable to tell "expired"
  from "lost".
* The incident state machine is monotonic and enforced in `incident.py`, not by a CHECK, because
  an illegal transition must raise *and* append a ledger entry.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# PostgreSQL 18 built-in. Timestamp-ordered UUIDs give good index locality and let the hash chain
# read chronologically -- the specific reason this project requires PG 18.
UUIDV7 = text("uuidv7()")
NOW = text("now()")


class IncidentState(enum.StrEnum):
    OPEN = "open"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    REMEDIATED = "remediated"
    REVERTED = "reverted"
    ESCALATED = "escalated"


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    alertname: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(20))
    namespace: Mapped[str | None] = mapped_column(String(200))
    pod_name: Mapped[str | None] = mapped_column(String(200))
    node_name: Mapped[str | None] = mapped_column(String(200))
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    annotations: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    # The complete v2 payload, verbatim. Reconstructing an incident later needs the raw thing.
    full_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    incident_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("incidents.id", ondelete="SET NULL")
    )
    # 'cause' selects the runbook; 'symptom' attaches as evidence and never triggers anything.
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="cause")

    __table_args__ = (
        Index("ix_alert_events_fingerprint", "fingerprint", "received_at"),
        Index("ix_alert_events_ns_name", "namespace", "alertname", "received_at"),
        Index("ix_alert_events_incident", "incident_id"),
        CheckConstraint("role IN ('cause','symptom')", name="ck_alert_events_role"),
    )


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default="open")
    # Cause-first correlation: scope and target decide which alert owns the incident.
    cause_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    cause_target: Mapped[str] = mapped_column(Text, nullable=False)
    cause_alertname: Mapped[str] = mapped_column(String(200), nullable=False)
    target_key: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str | None] = mapped_column(String(200))
    precedence: Mapped[int] = mapped_column(Integer, nullable=False, server_default="9")
    correlation_reason: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="new_incident"
    )
    alert_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(32))

    executions: Mapped[list[RunbookExecution]] = relationship(back_populates="incident")

    __table_args__ = (
        Index("ix_incidents_open_target", "target_key", "state"),
        Index("ix_incidents_opened_at", "opened_at"),
        CheckConstraint(
            "cause_scope IN ('node','workload','pod')", name="ck_incidents_cause_scope"
        ),
    )


class RemediationPlanRow(Base):
    __tablename__ = "remediation_plans"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=UUIDV7)
    incident_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("incidents.id"), nullable=False)
    runbook: Mapped[str] = mapped_column(Text, nullable=False)
    planner_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="0")
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_namespace: Mapped[str | None] = mapped_column(Text)
    target_name: Mapped[str] = mapped_column(Text, nullable=False)
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    # Decided BEFORE acting, never derived afterwards. This is the saga's compensating transaction.
    compensation: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    reversibility: Mapped[str] = mapped_column(Text, nullable=False)
    blast_radius: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    durability: Mapped[str] = mapped_column(Text, nullable=False)
    ttl_seconds: Mapped[int | None] = mapped_column(Integer)
    expected_signal: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    cost_delta_inr_month: Mapped[float] = mapped_column(
        Numeric(12, 2), nullable=False, server_default="0"
    )
    refusal_reason: Mapped[str | None] = mapped_column(Text)
    policy_decision: Mapped[str] = mapped_column(Text, nullable=False)
    policy_reasons: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )

    __table_args__ = (
        Index("ix_plans_incident", "incident_id", "created_at"),
        CheckConstraint(
            "reversibility IN ('reversible','partially_reversible','irreversible')",
            name="ck_plans_reversibility",
        ),
        CheckConstraint("durability IN ('ephemeral','durable')", name="ck_plans_durability"),
        CheckConstraint(
            "policy_decision IN ('allow','require_approval','deny')", name="ck_plans_decision"
        ),
        # ttl_seconds is REQUIRED for ephemeral fixes: an imperative change with no expiry is how
        # clusters drift away from their manifests.
        CheckConstraint(
            "durability <> 'ephemeral' OR ttl_seconds IS NOT NULL",
            name="ck_plans_ephemeral_needs_ttl",
        ),
    )


class RunbookExecution(Base):
    __tablename__ = "runbook_executions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("incidents.id"), nullable=False)
    plan_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("remediation_plans.id")
    )
    runbook: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="auto")
    state_before: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    state_after: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    # Rev 2 fields.
    durability: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pr_url: Mapped[str | None] = mapped_column(Text)
    gitops_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    gitops_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    compensated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # The target as observed immediately before acting. Durable for the same reason compensation
    # is: a successor leader has to be able to finish verifying what its predecessor started.
    signals_before: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    incident: Mapped[Incident] = relationship(back_populates="executions")

    __table_args__ = (
        Index("ix_executions_incident", "incident_id"),
        Index("ix_executions_status", "status"),
        # The watchdog's sweep query; both branches of it are index-driven.
        Index("ix_executions_awaiting_verification", "status", "finished_at"),
        Index("ix_executions_expiry", "expires_at"),
        CheckConstraint("mode IN ('shadow','approval','auto')", name="ck_executions_mode"),
    )


class Approval(Base):
    """PostgreSQL is the source of truth for approvals. Redis is a cache and a notifier.

    A safety control cannot live only in a cache: restart the cache and either the system drops
    incidents silently or, worse, executes something nobody approved.
    """

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=UUIDV7)
    execution_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runbook_executions.id", ondelete="CASCADE"), nullable=False
    )
    incident_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("incidents.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    # Authoritative deadline. Not a TTL.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    slack_message_ts: Mapped[str | None] = mapped_column(Text)
    slack_channel: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_approvals_execution", "execution_id", unique=True),
        Index("ix_approvals_pending_expiry", "status", "expires_at"),
        CheckConstraint(
            "status IN ('pending','approved','rejected','expired')", name="ck_approvals_status"
        ),
    )


class Verification(Base):
    __tablename__ = "verifications"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=UUIDV7)
    execution_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runbook_executions.id", ondelete="CASCADE"), nullable=False
    )
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL means "not observed", which is different from False and must stay distinguishable:
    # a missing signal is never a healthy signal.
    alert_cleared: Mapped[bool | None] = mapped_column(Boolean)
    signals_before: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    signals_after: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    compensated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )

    __table_args__ = (
        Index("ix_verifications_execution", "execution_id"),
        CheckConstraint(
            "outcome IN ('remediated','not_recovered','inconclusive')",
            name="ck_verifications_outcome",
        ),
    )


class AuditLedger(Base):
    """Append-only, hash-chained. Partitioned monthly, and the app role cannot UPDATE or DELETE."""

    __tablename__ = "audit_ledger"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=NOW
    )
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False, server_default=UUIDV7)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    incident_id: Mapped[int | None] = mapped_column(BigInteger)
    execution_id: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_sha256: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_ledger_incident", "incident_id"),
        Index("ix_ledger_occurred_at", "occurred_at"),
        {"postgresql_partition_by": "RANGE (occurred_at)"},
    )


class RunbookPromotion(Base):
    __tablename__ = "runbook_promotion"

    runbook: Mapped[str] = mapped_column(Text, primary_key=True)
    namespace: Mapped[str] = mapped_column(Text, primary_key=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="shadow")
    consecutive_good: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_executions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_reverted: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    shadow_plans: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    shadow_agreements: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    shadow_judged: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_outcome: Mapped[str | None] = mapped_column(Text)
    last_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    changed_reason: Mapped[str | None] = mapped_column(Text)
    # Any change to the runbook's code or the policy file resets authority to 'approval'.
    code_fingerprint: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint("mode IN ('shadow','approval','auto')", name="ck_promotion_mode"),
    )


class RemediationBudget(Base):
    __tablename__ = "remediation_budget"

    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3600")
    max_actions: Mapped[int] = mapped_column(Integer, nullable=False)
    used_actions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    breaker_state: Mapped[str] = mapped_column(Text, nullable=False, server_default="closed")
    breaker_reason: Mapped[str | None] = mapped_column(Text)
    breaker_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    breaker_owner: Mapped[str | None] = mapped_column(Text)
    # Set while a half-open breaker has released its single trial plan.
    half_open_plan_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))

    __table_args__ = (
        CheckConstraint(
            "breaker_state IN ('closed','open','half_open')", name="ck_budget_breaker_state"
        ),
    )


class RemediationRecurrence(Base):
    """Feeds the anti-masking guard: automation that keeps papering over a defect files a bug."""

    __tablename__ = "remediation_recurrence"

    runbook: Mapped[str] = mapped_column(Text, primary_key=True)
    target_key: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(Date, primary_key=True)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    issue_url: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )

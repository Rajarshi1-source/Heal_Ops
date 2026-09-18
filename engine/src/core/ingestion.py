"""Alert ingestion: persist, correlate, enqueue.

This is the "durable-then-ack" half of the webhook contract. The ordering is not negotiable:

1. Persist the alert and its incident to PostgreSQL, with the ledger append **in the same
   transaction** as the state change.
2. Only then add to the Redis stream.
3. Only then return 202.

Reversing 1 and 2 produces the worst available failure: a queued job whose alert was never
recorded, so a remediation runs with no audit trail. Under the fail-closed rule that must be
impossible, which is why the ledger is a precondition rather than a side effect.

Nothing here decides whether to act. The engine ingests and persists; the safety pipeline decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import ledger
import metrics
from core.alert_parser import AlertmanagerAlert, AlertmanagerV2Payload, fingerprint_of
from core.incident_correlator import (
    SAME_TARGET_WINDOW_SECONDS,
    AlertFacts,
    OpenIncident,
    correlate,
)
from core.incident_state import TERMINAL
from db.connection import db_now
from db.models import AlertEvent, Incident, IncidentState


@dataclass(frozen=True, slots=True)
class IngestedAlert:
    alert_event_id: int
    incident_id: int
    fingerprint: str
    alertname: str
    role: str
    correlation_reason: str
    created_incident: bool

    @property
    def is_cause(self) -> bool:
        """Only a cause alert selects a runbook. A symptom is evidence and nothing more."""
        return self.role == "cause"


async def _open_incidents(session: AsyncSession, now: datetime) -> list[OpenIncident]:
    """Incidents still accepting alerts, inside the correlation window."""
    cutoff = now.timestamp() - SAME_TARGET_WINDOW_SECONDS
    rows = (
        await session.execute(
            select(Incident)
            .where(Incident.state.notin_([s.value for s in TERMINAL]))
            .order_by(Incident.opened_at.desc())
            .limit(200)
        )
    ).scalars()

    result: list[OpenIncident] = []
    for row in rows:
        if row.opened_at.timestamp() < cutoff:
            continue
        node = row.cause_target if row.cause_scope == "node" else None
        workload = row.cause_target if row.cause_scope == "workload" else None
        result.append(
            OpenIncident(
                id=row.id,
                cause_alertname=row.cause_alertname,
                cause_scope=row.cause_scope,  # type: ignore[arg-type]
                cause_target=row.cause_target,
                target_key=row.target_key,
                namespace=row.namespace,
                precedence=row.precedence,
                opened_at=row.opened_at,
                node=node,
                workload=workload,
            )
        )
    return result


def _cause_target(facts: AlertFacts) -> str:
    if facts.scope == "node":
        return facts.node or "unknown"
    return facts.workload or facts.pod or "unknown"


async def ingest_alert(
    session: AsyncSession, alert: AlertmanagerAlert, raw_payload: dict[str, object]
) -> IngestedAlert:
    """Persist one alert and attach it to an incident. Caller owns the transaction."""
    now = await db_now(session)
    facts = alert.facts()
    fingerprint = fingerprint_of(alert)

    decision = correlate(facts, await _open_incidents(session, now), now)

    created = False
    if decision.action == "create":
        incident = Incident(
            state=IncidentState.OPEN.value,
            cause_scope=facts.scope,
            cause_target=_cause_target(facts),
            cause_alertname=facts.alertname,
            target_key=facts.target_key,
            namespace=facts.namespace,
            precedence=facts.precedence,
            correlation_reason=decision.reason,
            alert_count=1,
            opened_at=now,
        )
        session.add(incident)
        await session.flush()
        created = True
        incident_id = incident.id
    else:
        if decision.incident_id is None:
            raise RuntimeError(
                f"correlator returned action={decision.action!r} with no incident id; "
                "attaching an alert to nothing would lose it"
            )
        existing = await session.get(Incident, decision.incident_id)
        if existing is None:
            # The incident was open when the correlator looked and is gone now. Losing the alert is
            # worse than a duplicate incident, so fail loudly and let the retry re-correlate.
            raise RuntimeError(
                f"incident {decision.incident_id} disappeared between correlation and persistence"
            )
        incident = existing
        incident_id = incident.id
        incident.alert_count += 1

        if decision.action == "promote_cause":
            # The cause landed after its own symptoms, which is the ordering that actually
            # happens: pods crash-loop before the node's Ready condition flips. The incident takes
            # on the new cause rather than treating it as a symptom of what it caused.
            incident.cause_alertname = facts.alertname
            incident.cause_scope = facts.scope
            incident.cause_target = _cause_target(facts)
            incident.target_key = facts.target_key
            incident.precedence = facts.precedence
            incident.correlation_reason = decision.reason

    event = AlertEvent(
        fingerprint=fingerprint,
        alertname=facts.alertname,
        status=alert.status,
        severity=alert.severity,
        namespace=facts.namespace,
        pod_name=facts.pod,
        node_name=facts.node,
        labels=alert.labels,
        annotations=alert.annotations,
        full_payload=raw_payload,
        starts_at=alert.startsAt or now,
        ends_at=alert.endsAt,
        received_at=now,
        incident_id=incident_id,
        role=decision.role,
    )
    session.add(event)
    await session.flush()

    # The ledger append is inside the caller's transaction, alongside the state change above.
    await ledger.append(
        session,
        "state_transition" if not created else "plan_requested",
        {
            "stage": "ingest",
            "alertname": facts.alertname,
            "fingerprint": fingerprint,
            "target_key": facts.target_key,
            "role": decision.role,
            "correlation_reason": decision.reason,
            "created_incident": created,
        },
        incident_id=incident_id,
    )

    metrics.webhook_alerts_received.labels(status=alert.status).inc()
    if not created:
        metrics.alerts_correlated.labels(reason=decision.reason).inc()

    return IngestedAlert(
        alert_event_id=event.id,
        incident_id=incident_id,
        fingerprint=fingerprint,
        alertname=facts.alertname,
        role=decision.role,
        correlation_reason=decision.reason,
        created_incident=created,
    )


async def resolve_alert(
    session: AsyncSession, alert: AlertmanagerAlert, raw_payload: dict[str, object]
) -> None:
    """Record a `resolved` delivery.

    A resolution is evidence, not an action. If an incident's alerts all clear on their own the
    honest report is "1 incident, 0 remediations, self-resolved" -- which is exactly what the
    storm scenario produces when a node recovers by itself.
    """
    now = await db_now(session)
    fingerprint = fingerprint_of(alert)
    facts = alert.facts()

    session.add(
        AlertEvent(
            fingerprint=fingerprint,
            alertname=facts.alertname,
            status="resolved",
            severity=alert.severity,
            namespace=facts.namespace,
            pod_name=facts.pod,
            node_name=facts.node,
            labels=alert.labels,
            annotations=alert.annotations,
            full_payload=raw_payload,
            starts_at=alert.startsAt or now,
            ends_at=alert.endsAt or now,
            received_at=now,
            role="symptom",
        )
    )
    # Sessions run with `autoflush=False`, so flush here rather than leaving this row's visibility
    # to depend on whether a later alert in the same batch happens to trigger a flush.
    await session.flush()
    metrics.webhook_alerts_received.labels(status="resolved").inc()


async def ingest_payload(
    session: AsyncSession, payload: AlertmanagerV2Payload
) -> list[IngestedAlert]:
    """Persist a whole Alertmanager batch. Caller owns the transaction."""
    raw = payload.model_dump(mode="json")
    ingested: list[IngestedAlert] = []

    for alert in payload.alerts:
        if alert.is_firing:
            ingested.append(await ingest_alert(session, alert, raw))
        else:
            await resolve_alert(session, alert, raw)

    return ingested


def utcnow() -> datetime:
    """Only for code paths with no session. The ledger always uses the database clock."""
    return datetime.now(UTC)

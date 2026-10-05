"""Ingestion against a real PostgreSQL 18.

This is where the pieces meet: an Alertmanager batch becomes incidents and alert rows, correlation
decides cause from symptom, and the ledger append lands **in the same transaction** as the state
change. The storm test is the headline -- four alerts, one incident, one job.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.alert_parser import AlertmanagerV2Payload
from core.ingestion import ingest_payload
from db.models import AlertEvent, AuditLedger, Incident, IncidentState

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

FIXTURES = Path(__file__).parent / "fixtures" / "alert_payloads"


def load(name: str) -> AlertmanagerV2Payload:
    return AlertmanagerV2Payload.model_validate(
        json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    )


def synthetic(alerts: list[dict[str, Any]]) -> AlertmanagerV2Payload:
    return AlertmanagerV2Payload.model_validate(
        {"version": "4", "status": "firing", "alerts": alerts}
    )


def firing(alertname: str, fingerprint: str, **labels: str) -> dict[str, Any]:
    return {
        "status": "firing",
        "labels": {"alertname": alertname, "severity": "critical", **labels},
        "annotations": {},
        "startsAt": "2026-08-22T02:14:07Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": fingerprint,
    }


# --- a single alert -----------------------------------------------------------------------------


async def test_single_alert_creates_one_incident(db_session: AsyncSession) -> None:
    ingested = await ingest_payload(db_session, load("crashloop_firing.json"))

    assert len(ingested) == 1
    item = ingested[0]
    assert item.created_incident is True
    assert item.role == "cause"
    assert item.is_cause is True

    incident = await db_session.get(Incident, item.incident_id)
    assert incident is not None
    assert incident.state == IncidentState.OPEN.value
    assert incident.cause_alertname == "KubePodCrashLooping"
    assert incident.cause_scope == "workload"
    assert incident.target_key == "chaos/Deployment/crashy-api"
    assert incident.alert_count == 1


async def test_raw_payload_is_stored_verbatim(db_session: AsyncSession) -> None:
    """Parsed fields are enough until the first field nobody thought to parse."""
    ingested = await ingest_payload(db_session, load("crashloop_firing.json"))
    event = await db_session.get(AlertEvent, ingested[0].alert_event_id)

    assert event is not None
    assert event.full_payload["groupKey"].startswith("{}/{severity")
    assert event.full_payload["alerts"][0]["labels"]["reason"] == "CrashLoopBackOff"
    assert event.labels["node"] == "healops-worker2"
    assert event.node_name == "healops-worker2"


async def test_ledger_entry_lands_in_the_same_transaction(db_session: AsyncSession) -> None:
    """State and audit cannot diverge.

    The rows are visible on the same uncommitted transaction, which is the proof that no separate
    commit boundary exists between the state change and its audit record.
    """
    before = (await db_session.execute(select(func.count()).select_from(AuditLedger))).scalar_one()

    ingested = await ingest_payload(db_session, load("crashloop_firing.json"))

    after = (await db_session.execute(select(func.count()).select_from(AuditLedger))).scalar_one()
    assert after == before + 1

    entry = (
        await db_session.execute(
            select(AuditLedger).where(AuditLedger.incident_id == ingested[0].incident_id)
        )
    ).scalar_one()
    assert entry.payload["stage"] == "ingest"
    assert entry.payload["alertname"] == "KubePodCrashLooping"
    assert len(entry.hash) == 64
    assert len(entry.prev_hash) == 64


# --- the storm ----------------------------------------------------------------------------------


async def test_storm_becomes_one_incident(db_session: AsyncSession) -> None:
    """One node failure, four alerts, ONE incident.

    Without cause-first correlation this batch would produce four incidents and three unnecessary
    remediations of workloads whose only problem was living on a dead node.
    """
    ingested = await ingest_payload(db_session, load("node_storm_firing.json"))

    assert len(ingested) == 4
    assert len({i.incident_id for i in ingested}) == 1, "four alerts must share one incident"

    causes = [i for i in ingested if i.is_cause]
    symptoms = [i for i in ingested if not i.is_cause]
    assert len(causes) == 1
    assert causes[0].alertname == "KubeNodeNotReady"
    assert len(symptoms) == 3
    assert {s.correlation_reason for s in symptoms} == {"symptom_of_node_cause"}


async def test_symptoms_are_persisted_as_evidence(db_session: AsyncSession) -> None:
    ingested = await ingest_payload(db_session, load("node_storm_firing.json"))
    incident_id = ingested[0].incident_id

    rows = (
        (await db_session.execute(select(AlertEvent).where(AlertEvent.incident_id == incident_id)))
        .scalars()
        .all()
    )

    assert len(rows) == 4
    assert sum(1 for r in rows if r.role == "cause") == 1
    assert sum(1 for r in rows if r.role == "symptom") == 3

    incident = await db_session.get(Incident, incident_id)
    assert incident is not None
    assert incident.alert_count == 4
    assert incident.cause_scope == "node"
    assert incident.cause_target == "healops-worker3"


async def test_forty_symptoms_still_make_one_incident(db_session: AsyncSession) -> None:
    payload = synthetic(
        [firing("KubeNodeNotReady", "node-fp", node="worker-9")]
        + [
            firing(
                "KubePodCrashLooping",
                f"pod-fp-{i}",
                namespace="shop",
                node="worker-9",
                pod=f"svc{i % 7}-6b8c9d7f5-ab{i:03d}",
                container="app",
            )
            for i in range(40)
        ]
    )

    ingested = await ingest_payload(db_session, payload)

    assert len(ingested) == 41
    assert len({i.incident_id for i in ingested}) == 1
    # Exactly one job would be enqueued for this batch.
    assert sum(1 for i in ingested if i.is_cause) == 1


# --- correlation boundaries ---------------------------------------------------------------------


async def test_unrelated_alerts_stay_separate(db_session: AsyncSession) -> None:
    # A genuine second failure must not be swallowed into the first incident.
    payload = synthetic(
        [
            firing(
                "KubePodCrashLooping",
                "fp-a",
                namespace="shop",
                node="worker-1",
                pod="api-6b8c9d7f5-aaa11",
                container="api",
            ),
            firing(
                "KubePodCrashLooping",
                "fp-b",
                namespace="payments",
                node="worker-2",
                pod="ledger-6b8c9d7f5-bbb22",
                container="ledger",
            ),
        ]
    )
    ingested = await ingest_payload(db_session, payload)

    assert len({i.incident_id for i in ingested}) == 2
    assert all(i.is_cause for i in ingested)


async def test_same_workload_twice_joins_one_incident(db_session: AsyncSession) -> None:
    payload = synthetic(
        [
            firing(
                "KubePodCrashLooping",
                "fp-1",
                namespace="shop",
                node="worker-1",
                pod="api-6b8c9d7f5-aaa11",
                container="api",
            ),
            firing(
                "KubePodCrashLooping",
                "fp-2",
                namespace="shop",
                node="worker-1",
                pod="api-6b8c9d7f5-bbb22",
                container="api",
            ),
        ]
    )
    ingested = await ingest_payload(db_session, payload)
    assert len({i.incident_id for i in ingested}) == 1


# --- resolved deliveries ------------------------------------------------------------------------


async def test_resolved_alert_is_recorded_without_an_incident(db_session: AsyncSession) -> None:
    """A resolution is evidence, not an action.

    It produces no incident and no job, which is what lets a self-recovering node be reported
    honestly as "0 remediations, self-resolved".
    """
    incidents_before = (
        await db_session.execute(select(func.count()).select_from(Incident))
    ).scalar_one()

    ingested = await ingest_payload(db_session, load("crashloop_resolved.json"))

    assert ingested == []
    incidents_after = (
        await db_session.execute(select(func.count()).select_from(Incident))
    ).scalar_one()
    assert incidents_after == incidents_before

    resolved = (
        (await db_session.execute(select(AlertEvent).where(AlertEvent.status == "resolved")))
        .scalars()
        .all()
    )
    assert len(resolved) == 1
    assert resolved[0].ends_at is not None


async def test_mixed_batch_handles_both(db_session: AsyncSession) -> None:
    payload = AlertmanagerV2Payload.model_validate(
        {
            "version": "4",
            "status": "firing",
            "alerts": [
                firing(
                    "HighPodCPU",
                    "fp-firing",
                    namespace="shop",
                    node="worker-1",
                    pod="api-6b8c9d7f5-aaa11",
                    container="api",
                ),
                {
                    "status": "resolved",
                    "labels": {"alertname": "HighPodCPU", "namespace": "shop"},
                    "annotations": {},
                    "startsAt": "2026-08-22T02:00:00Z",
                    "endsAt": "2026-08-22T02:10:00Z",
                    "fingerprint": "fp-resolved",
                },
            ],
        }
    )
    ingested = await ingest_payload(db_session, payload)
    assert len(ingested) == 1
    assert ingested[0].alertname == "HighPodCPU"


# --- ledger chain integrity across many appends -------------------------------------------------


async def test_chain_stays_valid_across_a_whole_batch(db_session: AsyncSession) -> None:
    import ledger

    await ingest_payload(db_session, load("node_storm_firing.json"))
    await db_session.flush()

    status = await ledger.verify_chain(db_session)
    assert status.valid is True, status.detail
    assert status.entries >= 4


async def test_concurrent_batches_correlate_one_after_the_other() -> None:
    """Regression, found on the kind cluster: one Deployment's pods on two nodes arrive as two
    Alertmanager groups, 0.3ms apart. Correlated concurrently, neither batch saw the other's new
    incident -- two incidents, two rollbacks of one Deployment. The second batch must wait.

    Two real connections, both rolled back: nothing is committed to the test database.
    """
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from tests.conftest import TEST_DATABASE_URL, _unreachable

    if "postgres" in _unreachable:
        pytest.skip(_unreachable["postgres"])
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    try:
        async with engine.connect() as probe:
            await probe.execute(select(1))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"PostgreSQL not reachable: {exc}")

    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    first_batch = synthetic(
        [
            firing(
                "KubePodCrashLooping",
                "fp-node-a",
                namespace="shop",
                pod="api-7d9f8b6c5d-aaaaa",
                node="worker-1",
            )
        ]
    )
    second_batch = synthetic(
        [
            firing(
                "KubePodCrashLooping",
                "fp-node-b",
                namespace="shop",
                pod="api-7d9f8b6c5d-bbbbb",
                node="worker-2",
            )
        ]
    )

    async with engine.connect() as c1, engine.connect() as c2:
        t1, t2 = await c1.begin(), await c2.begin()
        s1, s2 = maker(bind=c1), maker(bind=c2)
        try:
            await ingest_payload(s1, first_batch)  # holds the correlation lock until t1 ends
            second = asyncio.create_task(ingest_payload(s2, second_batch))
            await asyncio.sleep(0.5)
            assert not second.done(), "the second batch correlated while the first was undecided"
            # And it waits at the CORRELATION lock -- before reading open incidents. The ledger's
            # own lock would also make it wait, but only after it had already correlated: the race.
            async with engine.connect() as observer:
                waiting = await observer.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                        " AND objid::bigint"
                        " = (hashtext('healops_correlation')::bigint & 4294967295)"
                    )
                )
            assert waiting == 1
            await t1.rollback()
            await asyncio.wait_for(second, timeout=10)
        finally:
            await s1.close()
            await s2.close()
            if t2.is_active:
                await t2.rollback()
    await engine.dispose()

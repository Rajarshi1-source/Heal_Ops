"""The Alertmanager webhook receiver.

The properties under test are the contract Alertmanager depends on:

* 202 only after the alert is durable;
* **persist before enqueue**, because a queued job whose alert was never recorded would remediate
  with no audit trail;
* constant-time secret comparison, and 401 without it;
* only *cause* alerts are enqueued -- this is where forty rollbacks become one remediation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

FIXTURES = Path(__file__).parent / "fixtures" / "alert_payloads"
SECRET = "test-shared-secret"  # noqa: S105


class FakeIngestion:
    """Records the order in which the receiver touches durable storage and the queue."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.ingested: list[Any] = []
        self.enqueued: list[tuple[str, dict[str, Any]]] = []
        self.deduplicated: set[str] = set()
        self.fail_persist = False


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeIngestion:
    """Replace PostgreSQL and Redis with recorders.

    The receiver's ordering guarantee is the unit under test, so the dependencies are stubbed
    rather than started. Their real behaviour is covered by the `db` tests.
    """
    import api.webhooks as wh
    from core.ingestion import IngestedAlert

    state = FakeIngestion()

    class FakeTransaction:
        async def __aenter__(self) -> object:
            state.calls.append("transaction_begin")
            return object()

        async def __aexit__(self, *exc: object) -> bool:
            state.calls.append("transaction_commit")
            return False

    def fake_transaction() -> FakeTransaction:
        return FakeTransaction()

    async def fake_ingest_payload(_session: object, payload: Any) -> list[IngestedAlert]:
        if state.fail_persist:
            raise RuntimeError("postgres unreachable")
        state.calls.append("persist")
        result: list[IngestedAlert] = []
        # The first firing alert is the cause; the rest are symptoms on the same node. That mirrors
        # what the correlator does and keeps this test focused on the receiver.
        for index, alert in enumerate(payload.firing):
            result.append(
                IngestedAlert(
                    alert_event_id=index + 1,
                    incident_id=1,
                    fingerprint=alert.fingerprint,
                    alertname=alert.alertname,
                    role="cause" if index == 0 else "symptom",
                    correlation_reason="new_incident" if index == 0 else "symptom_of_node_cause",
                    created_incident=index == 0,
                )
            )
        state.ingested = result
        return result

    async def fake_seen_recently(fingerprint: str, *_: object, **__: object) -> bool:
        return fingerprint in state.deduplicated

    async def fake_enqueue_batch(jobs: list[tuple[str, dict[str, Any]]]) -> list[str]:
        state.calls.append("enqueue")
        state.enqueued.extend(jobs)
        return [f"1-{i}" for i in range(len(jobs))]

    monkeypatch.setattr(wh, "transaction", fake_transaction)
    monkeypatch.setattr(wh, "ingest_payload", fake_ingest_payload)
    monkeypatch.setattr(wh.dedup_store, "seen_recently", fake_seen_recently)
    monkeypatch.setattr(wh.streams, "enqueue_batch", fake_enqueue_batch)
    return state


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, fake: FakeIngestion) -> TestClient:
    monkeypatch.setenv("HEALOPS_WEBHOOK_SHARED_SECRET", SECRET)

    from config import get_settings

    get_settings.cache_clear()

    from fastapi import FastAPI

    import api.webhooks as wh

    app = FastAPI()
    app.include_router(wh.router)
    try:
        yield TestClient(app)
    finally:
        get_settings.cache_clear()


def payload(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def post(client: TestClient, body: dict[str, Any], token: str | None = SECRET) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/api/webhooks/alertmanager", json=body, headers=headers)


# --- authentication -----------------------------------------------------------------------------


def test_missing_secret_is_rejected(client: TestClient) -> None:
    response = post(client, payload("crashloop_firing.json"), token=None)
    assert response.status_code == 401


def test_wrong_secret_is_rejected(client: TestClient) -> None:
    response = post(client, payload("crashloop_firing.json"), token="wrong")
    assert response.status_code == 401


def test_secret_prefix_is_not_accepted(client: TestClient) -> None:
    # A `startswith`-style comparison would accept this. compare_digest does not.
    response = post(client, payload("crashloop_firing.json"), token=SECRET[:-1])
    assert response.status_code == 401


def test_valid_secret_is_accepted(client: TestClient) -> None:
    response = post(client, payload("crashloop_firing.json"))
    assert response.status_code == 202


# --- the durable-then-ack contract --------------------------------------------------------------


def test_returns_202_not_200(client: TestClient) -> None:
    """202 is the honest code: the work is accepted, not completed.

    Alertmanager only needs to know the alert will not be lost.
    """
    assert post(client, payload("crashloop_firing.json")).status_code == 202


def test_persists_before_enqueueing(client: TestClient, fake: FakeIngestion) -> None:
    """The ordering that makes the audit trail trustworthy.

    Enqueueing first would allow a remediation to run for an alert that was never recorded -- an
    action with no ledger entry, which the fail-closed rule forbids.
    """
    post(client, payload("crashloop_firing.json"))

    assert "persist" in fake.calls
    assert "enqueue" in fake.calls
    assert fake.calls.index("persist") < fake.calls.index("enqueue")
    # And the transaction must have committed before anything was queued.
    assert fake.calls.index("transaction_commit") < fake.calls.index("enqueue")


def test_nothing_is_enqueued_when_persistence_fails(
    client: TestClient, fake: FakeIngestion
) -> None:
    # No ledger write means no action. A 500 makes Alertmanager retry, which is the correct
    # outcome: a duplicated alert is recoverable, a silently dropped one is not.
    fake.fail_persist = True
    with pytest.raises(RuntimeError, match="postgres unreachable"):
        post(client, payload("crashloop_firing.json"))
    assert "enqueue" not in fake.calls
    assert fake.enqueued == []


# --- correlation is what stops the storm --------------------------------------------------------


def test_only_cause_alerts_are_enqueued(client: TestClient, fake: FakeIngestion) -> None:
    """Four alerts arrive, one job is queued.

    The three symptoms are persisted as evidence on the incident. This single assertion is the
    difference between one remediation and four.
    """
    response = post(client, payload("node_storm_firing.json"))

    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] == 4
    assert body["enqueued"] == 1
    assert len(fake.enqueued) == 1

    kind, job = fake.enqueued[0]
    assert kind == "plan"
    assert job["incident_id"] == 1
    assert job["alertname"] == "KubeNodeNotReady"


def test_response_reports_the_incidents_touched(client: TestClient) -> None:
    body = post(client, payload("node_storm_firing.json")).json()
    assert body["incidents"] == [1]


# --- dedup --------------------------------------------------------------------------------------


def test_duplicate_delivery_is_not_enqueued_twice(client: TestClient, fake: FakeIngestion) -> None:
    # Alertmanager's HA gossip cluster can deliver the same alert twice.
    fake.deduplicated.add("aa11bb22cc33dd44")

    body = post(client, payload("node_storm_firing.json")).json()
    assert body["enqueued"] == 0
    assert body["deduplicated"] == 1
    assert fake.enqueued == []


def test_duplicate_alerts_are_still_persisted(client: TestClient, fake: FakeIngestion) -> None:
    # Dedup suppresses the ACTION, never the record. The second delivery is evidence that the
    # alert is still firing, which the verifier and the recurrence counter both care about.
    fake.deduplicated.add("aa11bb22cc33dd44")
    body = post(client, payload("node_storm_firing.json")).json()
    assert body["accepted"] == 4
    assert "persist" in fake.calls


# --- resolved deliveries ------------------------------------------------------------------------


def test_resolved_only_batch_enqueues_nothing(client: TestClient, fake: FakeIngestion) -> None:
    """A resolution is evidence, not a trigger.

    When a node recovers on its own the honest report is "1 incident, 0 remediations,
    self-resolved" -- and that requires resolved deliveries to do exactly nothing.
    """
    body = post(client, payload("crashloop_resolved.json")).json()
    assert body["enqueued"] == 0
    assert fake.enqueued == []


# --- malformed input ----------------------------------------------------------------------------


def test_unknown_fields_are_tolerated(client: TestClient) -> None:
    body = payload("crashloop_firing.json")
    body["somethingNewInAlertmanager035"] = {"nested": True}
    assert post(client, body).status_code == 202


def test_empty_alert_list_is_accepted(client: TestClient) -> None:
    # Alertmanager can send an empty batch. Rejecting it would trigger pointless retries.
    response = post(client, {"version": "4", "status": "firing", "alerts": []})
    assert response.status_code == 202
    assert response.json()["accepted"] == 0

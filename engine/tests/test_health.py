"""Readiness semantics.

The distinction under test is which dependencies are *required* and which merely *degrade*. Getting
this wrong is expensive in both directions:

* Too strict -- if Slack being down made the pod unready, a Slack outage would take HealOps out of
  rotation and Alertmanager would pile up retries against a service that was perfectly capable of
  recording alerts.
* Too lax -- if PostgreSQL being down left the pod ready, Alertmanager would POST alerts that could
  not be persisted, and an action with no ledger entry is exactly what fail-closed forbids.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import health
from config import get_settings


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """All dependencies healthy by default; individual tests knock one out."""
    state = {"postgres": True, "redis": True, "kubernetes": True, "alertmanager": True}

    async def ping_postgres() -> bool:
        return state["postgres"]

    async def ping_redis() -> bool:
        return state["redis"]

    async def ping_k8s() -> bool:
        return state["kubernetes"]

    async def ping_am() -> bool:
        return state["alertmanager"]

    monkeypatch.setattr(health.connection, "ping", ping_postgres)
    monkeypatch.setattr(health.redis_client, "ping", ping_redis)
    monkeypatch.setattr(health.k8s, "ping", ping_k8s)
    monkeypatch.setattr(health.alertmanager_client, "ping", ping_am)

    class FakeElector:
        is_leader = False

    monkeypatch.setattr(health, "get_elector", lambda: FakeElector())
    return state


@pytest.fixture
def client(probes: dict[str, bool]) -> TestClient:
    app = FastAPI()
    app.include_router(health.router)
    get_settings.cache_clear()
    try:
        yield TestClient(app)
    finally:
        get_settings.cache_clear()


def body(client: TestClient) -> dict[str, Any]:
    return client.get("/healthz").json()


# --- liveness -----------------------------------------------------------------------------------


def test_livez_depends_on_nothing(client: TestClient, probes: dict[str, bool]) -> None:
    """Restarting the pod does not fix PostgreSQL, so a dependency outage must not kill it."""
    probes.update(dict.fromkeys(probes, False))
    response = client.get("/livez")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"


# --- readiness ----------------------------------------------------------------------------------


def test_ready_when_everything_is_up(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["degraded"] == []


def test_not_ready_without_postgres(client: TestClient, probes: dict[str, bool]) -> None:
    # No ledger write means no action, so a replica that cannot reach PostgreSQL must not be sent
    # alerts it would have to drop.
    probes["postgres"] = False
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_not_ready_without_redis(client: TestClient, probes: dict[str, bool]) -> None:
    # Redis is disposable for *state*, but the webhook cannot enqueue without it, so accepting an
    # alert would not actually make it durable end to end.
    probes["redis"] = False
    assert client.get("/healthz").status_code == 503


def test_still_ready_without_alertmanager(client: TestClient, probes: dict[str, bool]) -> None:
    """Degradation with explicit semantics.

    An unreachable Alertmanager means verification returns `inconclusive` and escalates. That is a
    documented behaviour, not a reason to stop accepting alerts.
    """
    probes["alertmanager"] = False
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert "alertmanager" in response.json()["degraded"]


def test_still_ready_without_the_kubernetes_api(
    client: TestClient, probes: dict[str, bool]
) -> None:
    # Alerts can still be recorded and correlated; only execution is blocked, and the transport
    # breaker plus the policy breaker handle that.
    probes["kubernetes"] = False
    response = client.get("/healthz")
    assert response.status_code == 200
    assert "kubernetes" in response.json()["degraded"]


def test_checks_are_reported_individually(client: TestClient, probes: dict[str, bool]) -> None:
    probes["alertmanager"] = False
    checks = body(client)["checks"]
    assert checks == {
        "postgres": True,
        "redis": True,
        "kubernetes": True,
        "alertmanager": False,
    }


def test_required_dependencies_are_the_durability_ones(client: TestClient) -> None:
    assert set(health.REQUIRED) == {"postgres", "redis"}


# --- operational context ------------------------------------------------------------------------


def test_reports_leadership(client: TestClient) -> None:
    # Both replicas serve webhooks; only one acts. Surfacing which is which makes a "why did
    # nothing execute?" question answerable in one request.
    assert body(client)["is_leader"] is False


def test_reports_planner_version(client: TestClient) -> None:
    # Every plan records planner_version, so an incident can be correlated with the engine that
    # handled it. Exposing it here makes a running pod self-describing.
    assert body(client)["planner_version"]


def test_flags_an_unreplaced_default_secret(client: TestClient) -> None:
    """A deployment still using the placeholder secret should be visible, not quietly insecure."""
    assert body(client)["webhook_secret_is_default"] is True


def test_reports_slack_disabled_by_default(client: TestClient) -> None:
    # With Slack absent, approvals are held rather than executed. Worth being able to see.
    assert body(client)["slack_enabled"] is False


def test_api_health_alias_matches_healthz(client: TestClient) -> None:
    # The dashboard's route handler proxies /api/health; it must not drift from /healthz.
    assert client.get("/api/health").json() == body(client)

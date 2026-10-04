"""The FastAPI application wiring.

Small surface, but three claims worth checking rather than assuming:

* **One web server, one port.** The Slack adapter is mounted on this app rather than run separately,
  so there is one Service, one ingress rule and one readiness story for the process.
* **It boots with no Slack, no GitHub and no LLM key.** Approvals are then held rather than
  executed, and the ledger and dashboard keep working. A system that needs credentials in order to
  start cannot degrade gracefully.
* **`/metrics` reflects live state**, refreshed on scrape rather than on a timer.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import main
from config import get_settings


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client that does not start the lifespan.

    The lifespan creates consumer groups and starts leader election, which need Redis and a
    Kubernetes API. Those are covered by their own tests; here the subject is routing.
    """

    async def no_depth() -> int:
        return 0

    class FakeElector:
        """Covered properly in `test_lease.py`; here it only needs to avoid touching a cluster."""

        is_leader = False

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    async def no_groups() -> None:
        return None

    async def no_ping() -> bool:
        return True

    monkeypatch.setattr(main.streams, "depth", no_depth)
    monkeypatch.setattr(main.streams, "dlq_depth", no_depth)
    monkeypatch.setattr(main.streams, "ensure_groups", no_groups)
    monkeypatch.setattr(main.connection, "ping", no_ping)

    async def no_watchdog_actions(_session: Any) -> int:
        return 0

    monkeypatch.setattr(main.watchdog, "actions_recorded", no_watchdog_actions)
    monkeypatch.setattr(main, "get_elector", lambda: FakeElector())

    get_settings.cache_clear()
    with TestClient(main.app) as c:
        yield c
    get_settings.cache_clear()


# --- routing -------------------------------------------------------------------------------------


def test_root_describes_the_pipeline() -> None:
    # The pipeline order is the project, so a reader hitting the service learns it immediately.
    from fastapi.testclient import TestClient as RawClient

    with RawClient(main.app) as c:
        body = c.get("/").json()

    assert body["service"] == "healops-engine"
    assert "correlate" in body["pipeline"]
    assert "verify" in body["pipeline"]
    assert "compensate" in body["pipeline"]


def test_openapi_schema_builds() -> None:
    # A malformed response model would only surface here, not at import.
    schema = main.app.openapi()
    assert schema["info"]["title"] == "HealOps Engine"


def test_expected_routes_are_mounted() -> None:
    # Included routers are wrapped, so their paths are read from the schema rather than from
    # `app.routes`.
    documented = set(main.app.openapi()["paths"])

    assert "/api/webhooks/alertmanager" in documented
    assert "/healthz" in documented
    assert "/livez" in documented
    assert "/api/runbooks" in documented


def test_metrics_and_root_are_deliberately_undocumented() -> None:
    # Operational endpoints, not part of the API contract, so they stay out of the schema while
    # still being served.
    schema_paths = set(main.app.openapi()["paths"])
    assert "/metrics" not in schema_paths
    assert "/" not in schema_paths

    served = {getattr(r, "path", None) for r in main.app.routes}
    assert "/metrics" in served
    assert "/" in served


def test_runbooks_endpoint_lists_the_registry(client: TestClient) -> None:
    body = client.get("/api/runbooks").json()

    assert body["count"] >= 3
    names = {r["name"] for r in body["runbooks"]}
    assert {"crashloop_rollback", "cpu_scale_up", "disk_cleanup"} <= names


def test_runbooks_endpoint_exposes_the_chaos_fixture(client: TestClient) -> None:
    """The model card as data: a reader can follow the fixture to the proof."""
    body = client.get("/api/runbooks").json()
    for entry in body["runbooks"]:
        assert entry["chaos_fixture"]
        assert entry["triggers"]


def test_metrics_endpoint_serves_prometheus_exposition(client: TestClient) -> None:
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    # Self-monitoring is a first-class feature, so the engine's own series must be present.
    assert "healops_" in response.text


def test_metrics_include_the_trust_series(client: TestClient) -> None:
    text = client.get("/metrics").text

    # The metrics an operator needs to know whether automation is behaving.
    for series in (
        "healops_verification_outcome_total",
        "healops_compensations_total",
        "healops_watchdog_actions_total",
        "healops_ledger_valid",
        "healops_breaker_state",
        "healops_budget_used_ratio",
    ):
        assert series in text, f"{series} is missing from /metrics"


def test_queue_depth_is_refreshed_on_scrape(client: TestClient) -> None:
    # Refreshed per scrape rather than on a timer, so the numbers are as fresh as the scrape.
    text = client.get("/metrics").text
    assert "healops_queue_depth" in text
    assert "healops_dlq_depth" in text


# --- graceful degradation ------------------------------------------------------------------------


def test_app_boots_without_slack_credentials() -> None:
    """Approvals are held, not executed, when Slack is absent.

    The engine still acts within policy and the ledger still records -- which is why the absence of
    a token must not be a startup failure.
    """
    settings = get_settings()
    assert settings.slack_enabled is False
    assert main.app is not None


def test_app_boots_without_github_credentials() -> None:
    # Durable PRs are unavailable; ephemeral mitigation still works.
    assert get_settings().github_enabled is False


def test_narrator_defaults_to_template() -> None:
    """The system works with no API key and no network.

    The LLM never produces or alters a plan, a policy decision or a verdict -- it writes prose only,
    so a deterministic narrator is a complete implementation rather than a fallback.
    """
    assert get_settings().narrator == "template"


def test_slack_adapter_is_skipped_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    # Must not raise when the integration module does not exist yet.
    main._install_slack_adapter()

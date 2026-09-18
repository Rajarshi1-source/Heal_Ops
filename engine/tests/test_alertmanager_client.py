"""The Alertmanager v2 client.

The whole point of this file is the three-valued return. `is_firing()` gives `True`, `False`, or
`None`, and `None` means "I could not tell". Collapsing `None` to `False` would turn "Alertmanager
is down" into "the alert cleared", which is the single substitution the verification design exists
to prevent -- absence of evidence is never evidence of recovery.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from config import get_settings
from integrations import alertmanager_client as am

pytestmark = pytest.mark.asyncio

BASE = "http://localhost:9093"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEALOPS_ALERTMANAGER_URL", BASE)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def alert(alertname: str, **labels: str) -> dict[str, object]:
    return {
        "labels": {"alertname": alertname, **labels},
        "annotations": {},
        "startsAt": "2026-08-22T02:14:07Z",
        "fingerprint": "abc123",
        "status": {"state": "active"},
    }


# --- reachability -------------------------------------------------------------------------------


@respx.mock
async def test_ping_succeeds_on_v2_status() -> None:
    route = respx.get(f"{BASE}/api/v2/status").mock(
        return_value=httpx.Response(200, json={"cluster": {"status": "ready"}})
    )
    assert await am.ping() is True
    assert route.called


@respx.mock
async def test_ping_fails_when_unreachable() -> None:
    respx.get(f"{BASE}/api/v2/status").mock(side_effect=httpx.ConnectError("refused"))
    assert await am.ping() is False


@respx.mock
async def test_client_targets_api_v2_only() -> None:
    """v1 was removed in Alertmanager 0.27, so a v1 path would 404 forever."""
    route = respx.get(url__startswith=f"{BASE}/api/v2/alerts").mock(
        return_value=httpx.Response(200, json=[])
    )
    await am.active_alerts()
    assert route.called
    assert "/api/v2/" in str(route.calls[0].request.url)


# --- is_firing: the three-valued answer ---------------------------------------------------------


@respx.mock
async def test_is_firing_true_when_the_alert_is_present() -> None:
    respx.get(url__startswith=f"{BASE}/api/v2/alerts").mock(
        return_value=httpx.Response(
            200, json=[alert("KubePodCrashLooping", namespace="chaos", pod="crashy-api-x")]
        )
    )
    assert await am.is_firing("KubePodCrashLooping", namespace="chaos") is True


@respx.mock
async def test_is_firing_false_when_nothing_matches() -> None:
    respx.get(url__startswith=f"{BASE}/api/v2/alerts").mock(
        return_value=httpx.Response(200, json=[])
    )
    assert await am.is_firing("KubePodCrashLooping", namespace="chaos") is False


@respx.mock
async def test_is_firing_none_when_alertmanager_is_unreachable() -> None:
    """The assertion that keeps the scorecard honest.

    If this ever returned False, an Alertmanager outage would be indistinguishable from a
    successful remediation, and the verifier would record `remediated` for an incident that is
    still burning.
    """
    respx.get(url__startswith=f"{BASE}/api/v2/alerts").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    assert await am.is_firing("KubePodCrashLooping", namespace="chaos") is None


@respx.mock
async def test_is_firing_none_on_a_server_error() -> None:
    respx.get(url__startswith=f"{BASE}/api/v2/alerts").mock(
        return_value=httpx.Response(503, text="unavailable")
    )
    assert await am.is_firing("KubePodCrashLooping") is None


@respx.mock
async def test_is_firing_none_on_a_timeout() -> None:
    respx.get(url__startswith=f"{BASE}/api/v2/alerts").mock(
        side_effect=httpx.ReadTimeout("too slow")
    )
    assert await am.is_firing("KubePodCrashLooping") is None


# --- filters ------------------------------------------------------------------------------------


@respx.mock
async def test_matching_is_by_label_not_fingerprint() -> None:
    """After a remediation the pod is often a new one, so the fingerprint changes.

    Filtering on fingerprint would report "cleared" purely because the pod was replaced.
    """
    route = respx.get(url__startswith=f"{BASE}/api/v2/alerts").mock(
        return_value=httpx.Response(200, json=[])
    )
    await am.is_firing("HighPodCPU", namespace="shop", pod="checkout-1", node="worker-2")

    query = str(route.calls[0].request.url)
    assert "alertname%3DHighPodCPU" in query or "alertname=HighPodCPU" in query
    assert "namespace" in query
    assert "fingerprint" not in query


@respx.mock
async def test_inhibited_alerts_are_excluded_by_default() -> None:
    """Inhibited symptoms must not count as "still firing".

    A pod alert suppressed because its node is down would otherwise keep an incident open forever.
    """
    route = respx.get(url__startswith=f"{BASE}/api/v2/alerts").mock(
        return_value=httpx.Response(200, json=[])
    )
    await am.active_alerts()
    query = str(route.calls[0].request.url)
    assert "inhibited=false" in query
    assert "silenced=false" in query
    assert "active=true" in query


@respx.mock
async def test_inhibited_can_be_requested_explicitly() -> None:
    route = respx.get(url__startswith=f"{BASE}/api/v2/alerts").mock(
        return_value=httpx.Response(200, json=[])
    )
    await am.active_alerts(include_inhibited=True)
    assert "inhibited=true" in str(route.calls[0].request.url)


# --- alerts_on_target: the third verification question -------------------------------------------


@respx.mock
async def test_alerts_on_target_lists_every_alertname() -> None:
    """ "Did my action cause a DIFFERENT problem?"

    Scaling up fixes CPU and can start an OOM cascade on a memory-tight node. Only a whole-target
    check catches that, which is why this returns every alertname rather than checking one.
    """
    respx.get(url__startswith=f"{BASE}/api/v2/alerts").mock(
        return_value=httpx.Response(
            200,
            json=[
                alert("KubePodOOMKilled", namespace="shop", pod="checkout-2"),
                alert("HighPodCPU", namespace="shop", pod="checkout-2"),
                alert("HighPodCPU", namespace="shop", pod="checkout-3"),
            ],
        )
    )
    names = await am.alerts_on_target("shop", pod="checkout-2")
    assert names == ["HighPodCPU", "KubePodOOMKilled"]


@respx.mock
async def test_alerts_on_target_none_when_unreachable() -> None:
    respx.get(url__startswith=f"{BASE}/api/v2/alerts").mock(
        side_effect=httpx.ConnectError("refused")
    )
    assert await am.alerts_on_target("shop", pod="checkout-2") is None


@respx.mock
async def test_alerts_on_target_ignores_blank_alertnames() -> None:
    respx.get(url__startswith=f"{BASE}/api/v2/alerts").mock(
        return_value=httpx.Response(200, json=[{"labels": {}}, alert("HighPodCPU")])
    )
    assert await am.alerts_on_target("shop") == ["HighPodCPU"]


@respx.mock
async def test_unexpected_payload_shape_returns_none() -> None:
    # A dict where a list was expected must not be silently treated as "no alerts".
    respx.get(url__startswith=f"{BASE}/api/v2/alerts").mock(
        return_value=httpx.Response(200, json={"error": "nope"})
    )
    assert await am.active_alerts() is None

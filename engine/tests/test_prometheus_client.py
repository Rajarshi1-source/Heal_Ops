"""The Prometheus client.

One contract, tested from every angle: **`None` means "not observed", never zero.** If any failure
path here returned 0.0, the verifier would read "Prometheus is down" as "the workload is perfectly
healthy" and record `remediated` for an incident that is still burning.

So every one of these is asserted to return `None`: unreachable server, connection refused, timeout,
HTTP 500, Prometheus-level error, empty result set, `NaN`, `+Inf`, and an unparseable value.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from config import get_settings
from integrations import prometheus_client as prom
from integrations.prometheus_client import GoldenSignals

pytestmark = pytest.mark.asyncio

BASE = "http://localhost:9090"
QUERY_URL = f"{BASE}/api/v1/query"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEALOPS_PROMETHEUS_URL", BASE)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def result(*values: str) -> dict[str, object]:
    return {
        "status": "success",
        "data": {
            "result": [
                {"metric": {"pod": f"p{i}"}, "value": [1755830047.0, v]}
                for i, v in enumerate(values)
            ]
        },
    }


# --- the happy path -------------------------------------------------------------------------------


@respx.mock
async def test_reads_a_single_value() -> None:
    respx.get(QUERY_URL).mock(return_value=httpx.Response(200, json=result("0.042")))
    signals = await prom.golden_signals("shop", workload="api")

    assert signals.throttle_ratio == pytest.approx(0.042)


@respx.mock
async def test_takes_the_worst_series_not_the_average() -> None:
    """For "did anything go wrong?", the worst replica is the answer.

    Averaging would let one healthy pod mask a sick one, which is exactly how a partial failure
    gets recorded as a success.
    """
    respx.get(QUERY_URL).mock(return_value=httpx.Response(200, json=result("0.01", "0.90", "0.02")))
    signals = await prom.golden_signals("shop", workload="api")

    assert signals.throttle_ratio == pytest.approx(0.90)


@respx.mock
async def test_reads_every_golden_signal() -> None:
    respx.get(QUERY_URL).mock(return_value=httpx.Response(200, json=result("1.0")))
    signals = await prom.golden_signals("shop", workload="api")

    assert signals.observed() == 5
    assert signals.missing() == ()


# --- every failure path returns None --------------------------------------------------------------


@respx.mock
async def test_connection_refused_is_none() -> None:
    respx.get(QUERY_URL).mock(side_effect=httpx.ConnectError("refused"))
    signals = await prom.golden_signals("shop", workload="api")

    assert signals.throttle_ratio is None
    assert signals.observed() == 0
    assert len(signals.missing()) == 5


@respx.mock
async def test_timeout_is_none() -> None:
    respx.get(QUERY_URL).mock(side_effect=httpx.ReadTimeout("too slow"))
    assert (await prom.golden_signals("shop", workload="api")).restarts is None


@respx.mock
async def test_http_error_is_none() -> None:
    respx.get(QUERY_URL).mock(return_value=httpx.Response(500, text="boom"))
    assert (await prom.golden_signals("shop", workload="api")).restarts is None


@respx.mock
async def test_prometheus_level_error_is_none() -> None:
    # HTTP 200 with a Prometheus error body -- a bad query, say. Still not a reading.
    respx.get(QUERY_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "error", "errorType": "bad_data", "error": "parse error"}
        )
    )
    assert (await prom.golden_signals("shop", workload="api")).restarts is None


@respx.mock
async def test_empty_result_set_is_none_not_zero() -> None:
    """The most dangerous case, because it looks like a successful query.

    A missing series and a genuinely-zero metric are indistinguishable in shape, so an empty result
    must never be reduced to 0.0.
    """
    respx.get(QUERY_URL).mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {"result": []}})
    )
    signals = await prom.golden_signals("shop", workload="api")

    assert signals.restarts is None
    assert signals.oom_events is None


@respx.mock
async def test_nan_is_none() -> None:
    # PromQL produces NaN for things like a division by zero. It is not a measurement.
    respx.get(QUERY_URL).mock(return_value=httpx.Response(200, json=result("NaN")))
    assert (await prom.golden_signals("shop", workload="api")).throttle_ratio is None


@respx.mock
async def test_infinity_is_none() -> None:
    respx.get(QUERY_URL).mock(return_value=httpx.Response(200, json=result("+Inf")))
    assert (await prom.golden_signals("shop", workload="api")).throttle_ratio is None


@respx.mock
async def test_unparseable_value_is_none() -> None:
    respx.get(QUERY_URL).mock(return_value=httpx.Response(200, json=result("not-a-number")))
    assert (await prom.golden_signals("shop", workload="api")).throttle_ratio is None


@respx.mock
async def test_one_bad_series_among_good_ones_is_ignored() -> None:
    # A single malformed series should not discard the readings that are valid.
    respx.get(QUERY_URL).mock(return_value=httpx.Response(200, json=result("bad", "0.30")))
    assert (await prom.golden_signals("shop", workload="api")).throttle_ratio == pytest.approx(0.30)


@respx.mock
async def test_malformed_json_body_is_none() -> None:
    respx.get(QUERY_URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    assert (await prom.golden_signals("shop", workload="api")).restarts is None


# --- queries reference the recording rules --------------------------------------------------------


@respx.mock
async def test_queries_use_the_shared_recording_rules() -> None:
    """The alert and the verifier must judge by the same expression.

    Re-writing the PromQL here is how the definition an alert fired on and the definition recovery
    is measured by drift apart.
    """
    route = respx.get(QUERY_URL).mock(return_value=httpx.Response(200, json=result("0.01")))
    await prom.golden_signals("shop", workload="api")

    queries = [str(call.request.url) for call in route.calls]
    joined = " ".join(queries)

    assert "healops%3Acontainer_cpu_throttle_ratio" in joined or (
        "healops:container_cpu_throttle_ratio" in joined
    )
    assert "container_oom_events" in joined
    assert "psi_full" in joined or "memory_psi_full" in joined


@respx.mock
async def test_oom_uses_the_counter_rule_not_the_flapping_gauge() -> None:
    route = respx.get(QUERY_URL).mock(return_value=httpx.Response(200, json=result("0")))
    await prom.golden_signals("shop", workload="api")

    joined = " ".join(str(c.request.url) for c in route.calls)
    assert "last_terminated_reason" not in joined


@respx.mock
async def test_pod_selector_matches_by_prefix() -> None:
    """After a rollout the pod name changes.

    An exact match would return nothing and read as "not observed", so the verifier would go
    inconclusive on every successful rollback.
    """
    route = respx.get(QUERY_URL).mock(return_value=httpx.Response(200, json=result("0")))
    await prom.golden_signals("shop", workload="api")

    joined = " ".join(str(c.request.url) for c in route.calls)
    assert "pod%3D~" in joined or 'pod=~"api' in joined


@respx.mock
async def test_node_scoped_call_omits_the_workload_selector() -> None:
    route = respx.get(QUERY_URL).mock(return_value=httpx.Response(200, json=result("0")))
    await prom.golden_signals("shop", workload=None)

    joined = " ".join(str(c.request.url) for c in route.calls)
    assert "pod%3D~" not in joined and "pod=~" not in joined


# --- plan-specific queries ------------------------------------------------------------------------


@respx.mock
async def test_plan_queries_share_the_none_contract() -> None:
    respx.get(QUERY_URL).mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {"result": []}})
    )
    values = await prom.query_thresholds({"custom": "up == 0"})
    assert values == {"custom": None}


# --- reachability ---------------------------------------------------------------------------------


@respx.mock
async def test_ping_reports_reachability() -> None:
    respx.get(f"{BASE}/-/ready").mock(return_value=httpx.Response(200, text="Ready"))
    assert await prom.ping() is True


@respx.mock
async def test_ping_false_when_unreachable() -> None:
    respx.get(f"{BASE}/-/ready").mock(side_effect=httpx.ConnectError("refused"))
    assert await prom.ping() is False


# --- the GoldenSignals container ------------------------------------------------------------------


async def test_observed_counts_only_real_readings() -> None:
    signals = GoldenSignals(restarts=0.0, oom_events=None, throttle_ratio=0.01)

    # Zero IS a reading. None is not. Conflating them is the bug this whole module guards against.
    assert signals.observed() == 2
    assert "oom_events" in signals.missing()
    assert "restarts" not in signals.missing()


async def test_all_signals_default_to_unobserved() -> None:
    # The safe default: a freshly constructed window has measured nothing.
    signals = GoldenSignals()
    assert signals.observed() == 0
    assert len(signals.missing()) == 5


@respx.mock
async def test_event_counts_use_the_post_action_window() -> None:
    """The ten-minute OOM rule still holds the OOM that triggered the alert five minutes after a
    successful fix, so counts must be taken over the window that starts after the action."""
    route = respx.get(QUERY_URL).mock(return_value=httpx.Response(200, json=result("0")))
    await prom.golden_signals("shop", workload="api", window="270s")

    queries = [c.request.url.params["query"] for c in route.calls]
    oom = next(q for q in queries if "container_oom_events_total" in q)
    restarts = next(q for q in queries if "restarts_total" in q)
    assert "[270s]" in oom and "[270s]" in restarts
    assert "increase10m" not in " ".join(queries)


@respx.mock
async def test_the_pod_prefix_does_not_match_a_longer_workload_name() -> None:
    route = respx.get(QUERY_URL).mock(return_value=httpx.Response(200, json=result("0")))
    await prom.golden_signals("shop", workload="api")
    assert 'pod=~"api-.*"' in route.calls[0].request.url.params["query"]

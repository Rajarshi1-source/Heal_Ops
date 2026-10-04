"""Prometheus query client for the golden signals.

Every function here returns `float | None`, and **`None` means "not observed"** -- never zero. That
distinction is the most important thing in this module. If an unavailable metric returned 0.0, then
"Prometheus is down" would be indistinguishable from "the workload is perfectly healthy", and the
verifier would record `remediated` for an incident that is still burning.

So a missing series, an unreachable server, a query error and a `NaN` all collapse to `None`, and
the verifier turns `None` into `inconclusive` rather than into success.

The ratio queries are the recording rules from `charts/alerting-rules`, referenced by name rather
than re-written here. Duplicating PromQL between the alert rules and the verifier is how the two
drift apart until an alert fires on one definition and recovery is judged by another. The event
counts are the exception, for the reason given on `golden_signals`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from config import get_settings

log = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(10.0, connect=3.0)


@dataclass(frozen=True, slots=True)
class GoldenSignals:
    """A workload's observable health over one window.

    Each field is `None` when the signal could not be read. `observed()` reports how many were
    actually available, which is what lets the verifier distinguish "everything looks fine" from
    "nothing could be measured".
    """

    restarts: float | None = None
    oom_events: float | None = None
    throttle_ratio: float | None = None
    psi_full: float | None = None
    memory_usage_ratio: float | None = None

    def observed(self) -> int:
        return sum(1 for value in self.to_dict().values() if value is not None)

    def missing(self) -> tuple[str, ...]:
        return tuple(name for name, value in self.to_dict().items() if value is None)

    def to_dict(self) -> dict[str, float | None]:
        return {
            "restarts": self.restarts,
            "oom_events": self.oom_events,
            "throttle_ratio": self.throttle_ratio,
            "psi_full": self.psi_full,
            "memory_usage_ratio": self.memory_usage_ratio,
        }


async def _instant_query(query: str) -> float | None:
    """Run one instant query and reduce it to a single number.

    Returns `None` on every failure path: unreachable server, HTTP error, Prometheus-level error,
    empty result, or a non-finite value. A caller must never be able to mistake a failure for a
    healthy reading.
    """
    settings = get_settings()
    url = f"{settings.prometheus_url.rstrip('/')}/api/v1/query"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            response = await http.get(url, params={"query": query})
            response.raise_for_status()
            body: dict[str, Any] = response.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("prometheus query failed", query=query, error=str(exc))
        return None

    if body.get("status") != "success":
        log.warning("prometheus returned an error", query=query, body=body.get("error"))
        return None

    result = body.get("data", {}).get("result", [])
    if not result:
        # An empty result set is genuinely ambiguous: the series may not exist, or the workload may
        # have no data yet. Either way it is not evidence of recovery.
        return None

    # Take the maximum across series. For "did anything go wrong?" the worst replica is the
    # answer -- averaging would let one healthy pod mask a sick one.
    values: list[float] = []
    for series in result:
        raw = series.get("value", [None, None])[1]
        try:
            value = float(raw)
        except TypeError, ValueError:
            continue
        if math.isfinite(value):
            values.append(value)

    return max(values) if values else None


async def ping() -> bool:
    settings = get_settings()
    url = f"{settings.prometheus_url.rstrip('/')}/-/ready"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            return (await http.get(url)).status_code == 200
    except Exception:  # noqa: BLE001 - readiness must report, never raise
        return False


def _selector(
    namespace: str | None,
    pod: str | None = None,
    container: str | None = None,
    *,
    exact_pod: str | None = None,
) -> str:
    parts: list[str] = []
    if namespace:
        parts.append(f'namespace="{namespace}"')
    if exact_pod:
        # A plan that acts on one pod is judged on that pod, not on its siblings.
        parts.append(f'pod="{exact_pod}"')
    elif pod:
        # Regex-match the pod prefix: after a rollout the pod name changes, so an exact match would
        # silently return nothing and read as "not observed". The hyphen stops `api` from also
        # matching `api-gateway`'s pods.
        parts.append(f'pod=~"{pod}-.*"')
    if container:
        parts.append(f'container="{container}"')
    return "{" + ",".join(parts) + "}" if parts else ""


async def golden_signals(
    namespace: str | None,
    *,
    workload: str | None = None,
    container: str | None = None,
    window: str = "5m",
    pod: str | None = None,
) -> GoldenSignals:
    """Read every golden signal for a workload.

    The ratio signals reference the recording rules defined alongside the alert rules, so the
    definition the alert fired on and the definition recovery is judged by are the same expression.

    The two *event counts* cannot: they must be counted over a window that starts after the action.
    The OOM recording rule looks back a fixed ten minutes, so five minutes after a successful memory
    fix it still contains the OOM that triggered the alert -- and every fix would read as failed.
    `window` is therefore the post-action window, and it applies to restarts and OOMs only.
    """
    selector = _selector(namespace, workload, container, exact_pod=pod)

    return GoldenSignals(
        # Restarts over the window. A rollback that halves the crash rate is still a crash loop.
        restarts=await _instant_query(
            f"max(increase(kube_pod_container_status_restarts_total{selector}[{window}]))"
        ),
        # OOM by COUNTER, not the flapping last_terminated_reason gauge.
        oom_events=await _instant_query(
            f"max(increase(container_oom_events_total{selector}[{window}]))"
        ),
        # The dimensionless ratio. Raw throttled-period counters move with replica count and window
        # length, so a raw before/after comparison is meaningless -- especially for a runbook that
        # changes the replica count.
        throttle_ratio=await _instant_query(
            f"max(healops:container_cpu_throttle_ratio:rate5m{selector})"
        ),
        # Share of wall time fully stalled on memory. Beta in 1.34, GA in 1.36.
        psi_full=await _instant_query(f"max(healops:container_memory_psi_full:rate5m{selector})"),
        memory_usage_ratio=await _instant_query(
            f"max(healops:container_memory_usage_ratio:current{selector})"
        ),
    )


async def query_thresholds(queries: dict[str, str]) -> dict[str, float | None]:
    """Evaluate a plan's own `expected_signal.queries`.

    A runbook can name extra signals beyond the golden set, and this keeps them on the same
    "None means unobserved" contract as everything else.
    """
    return {name: await _instant_query(query) for name, query in queries.items()}


async def memory_peak_mib(
    namespace: str, workload: str, container: str, *, lookback: str = "1h"
) -> float | None:
    """The container's highest working set over the lookback, in MiB -- what a new limit must clear.

    Read during target building, before planning, so `plan()` stays a pure function of the bundle.
    `None` when unobserved, which the OOM runbook turns into lower confidence rather than a guess
    presented as a measurement.
    """
    selector = _selector(namespace, workload, container)
    peak = await _instant_query(
        f"max(max_over_time(container_memory_working_set_bytes{selector}[{lookback}]))"
    )
    return peak / (1024 * 1024) if peak is not None else None

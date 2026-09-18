"""Alertmanager **API v2** client.

v1 was removed in 0.27, so every path here is `/api/v2/...`. Two callers depend on it:

* the verifier, asking "did the triggering alert actually clear?" -- and the answer `None` is
  meaningful, because an unreachable Alertmanager must produce `inconclusive`, never a silent
  success;
* the correlator's belt-and-braces check on what is currently firing.

`is_firing()` returns `bool | None` on purpose. Collapsing the unreachable case to `False` would
convert "I cannot tell" into "it recovered", which is the exact substitution the whole verification
design exists to prevent.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from config import get_settings

log = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


async def _get(path: str, params: dict[str, Any] | None = None) -> Any | None:
    url = f"{get_settings().alertmanager_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            response = await http.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("alertmanager unreachable", path=path, error=str(exc))
        return None


async def ping() -> bool:
    return await _get("/api/v2/status") is not None


async def active_alerts(
    filters: list[str] | None = None, *, include_inhibited: bool = False
) -> list[dict[str, Any]] | None:
    """Currently active alerts. `None` means Alertmanager could not be reached."""
    params: dict[str, Any] = {
        "active": "true",
        "silenced": "false",
        "inhibited": "true" if include_inhibited else "false",
    }
    if filters:
        params["filter"] = filters
    result = await _get("/api/v2/alerts", params)
    return result if isinstance(result, list) else None


async def is_firing(
    alertname: str,
    *,
    namespace: str | None = None,
    pod: str | None = None,
    node: str | None = None,
) -> bool | None:
    """Is this alert still firing? `None` means unknown, which must escalate.

    Matching is by label rather than by fingerprint: after a remediation the pod may be a different
    one, so the fingerprint changes even though the alert is conceptually the same.
    """
    filters = [f"alertname={alertname}"]
    if namespace:
        filters.append(f"namespace={namespace}")
    if pod:
        filters.append(f"pod={pod}")
    if node:
        filters.append(f"node={node}")

    alerts = await active_alerts(filters)
    if alerts is None:
        return None
    return len(alerts) > 0


async def alerts_on_target(
    namespace: str | None, pod: str | None = None, node: str | None = None
) -> list[str] | None:
    """Every alertname currently firing on a target.

    The verifier uses this for the third question: did a NEW alert appear on this target that the
    action could have caused? Scaling up fixes CPU and can start an OOM cascade on a
    memory-tight node, and only a whole-target check catches that.
    """
    filters: list[str] = []
    if namespace:
        filters.append(f"namespace={namespace}")
    if pod:
        filters.append(f"pod={pod}")
    if node:
        filters.append(f"node={node}")

    alerts = await active_alerts(filters or None)
    if alerts is None:
        return None
    return sorted({a.get("labels", {}).get("alertname", "") for a in alerts} - {""})

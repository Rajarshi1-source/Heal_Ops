"""Health and readiness.

`/healthz` is the readiness probe and it checks PostgreSQL, Redis, the Kubernetes API and
Alertmanager reachability. The deployment uses `maxUnavailable: 0`, so an honest readiness signal is
what keeps the webhook reachable through a rolling update -- if the probe lied, Alertmanager would
POST into a pod that cannot persist and start retrying.

The distinction between the two endpoints matters:

* **`/livez`** answers "is this process wedged?" and depends on nothing external. A dependency
  outage must not get the pod killed and restarted -- restarting does not fix PostgreSQL.
* **`/healthz`** answers "should traffic come here?" and requires the dependencies that make
  accepting an alert *durable*. PostgreSQL is required; Slack is not.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

import metrics
from cache import redis_client
from config import get_settings
from db import connection
from integrations import alertmanager_client
from k8s import client as k8s
from k8s.lease import get_elector

router = APIRouter(tags=["health"])

# Dependencies without which accepting an alert would not be durable.
REQUIRED = ("postgres", "redis")


async def _probe() -> dict[str, bool]:
    postgres = await connection.ping()
    redis_up = await redis_client.ping()
    kubernetes = await k8s.ping()
    am = await alertmanager_client.ping()

    results = {
        "postgres": postgres,
        "redis": redis_up,
        "kubernetes": kubernetes,
        "alertmanager": am,
    }
    for name, up in results.items():
        metrics.dependency_up.labels(dependency=name).set(1 if up else 0)
    return results


@router.get("/livez", summary="Process liveness; depends on nothing external")
async def livez() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/healthz", summary="Readiness: can this replica durably accept an alert?")
async def healthz(response: Response) -> dict[str, Any]:
    checks = await _probe()
    settings = get_settings()
    ready = all(checks[name] for name in REQUIRED)

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
        # Degradation is explicit rather than implied. No Prometheus means verification returns
        # `inconclusive` and escalates; no Slack means approvals are held, not executed.
        "degraded": [name for name, up in checks.items() if not up and name not in REQUIRED],
        "is_leader": get_elector().is_leader,
        "environment": settings.environment,
        "planner_version": settings.planner_version,
        "read_only": settings.read_only,
        "slack_enabled": settings.slack_enabled,
        # Surfaced so a deployment that never replaced the default secret is visible rather than
        # quietly insecure.
        "webhook_secret_is_default": (
            settings.webhook_shared_secret.get_secret_value() == "change-me"
        ),
    }


@router.get("/api/health", summary="Alias used by the dashboard's route handler")
async def api_health(response: Response) -> dict[str, Any]:
    return await healthz(response)

"""FastAPI entrypoint.

**One web server, one port.** The Slack Bolt `AsyncApp` is mounted on this app via its adapter
rather than run as a second server -- two ports would mean two Services, two ingress rules and two
readiness stories for one process.

Startup order matters: the stream's consumer groups must exist before any worker reads, and leader
election starts last so a replica cannot believe it leads before it can serve.

Shutdown is a real drain, not a `sys.exit`: stop consuming, finish in-flight work, release the
Lease. An engine killed mid-execution is what the watchdog exists for, but it should not happen on
a routine deploy.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

import metrics
from api import health, runbooks, webhooks
from cache import redis_client, streams
from config import get_settings
from db import connection
from k8s.lease import get_elector

logging.basicConfig(level=logging.INFO, format="%(message)s")
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log.info(
        "healops engine starting",
        environment=settings.environment,
        planner_version=settings.planner_version,
        read_only=settings.read_only,
    )

    # Consumer groups first: a worker that reads before its group exists gets an error, not an
    # empty batch.
    try:
        await streams.ensure_groups()
    except Exception as exc:  # noqa: BLE001
        # Not fatal. The readiness probe reports Redis down, so this replica takes no traffic
        # until it recovers -- which is better than crash-looping the pod.
        log.error("could not create consumer groups", error=str(exc))

    if not await connection.ping():
        # Also not fatal, and for the same reason. Fail-closed applies to *acting*, not to booting:
        # a pod that will not start cannot report why.
        log.error("postgres unreachable at startup; refusing to act until it returns")

    await get_elector().start()

    try:
        yield
    finally:
        log.info("healops engine draining")
        await get_elector().stop()
        await redis_client.close()
        await connection.dispose()
        log.info("healops engine stopped")


app = FastAPI(
    title="HealOps Engine",
    description=(
        "Self-healing Kubernetes remediation: correlate, plan, gate, act, verify, compensate. "
        "The engine ingests and persists; it never decides whether an action is allowed."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(webhooks.router)
app.include_router(runbooks.router)


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> PlainTextResponse:
    settings = get_settings()
    # Refreshed on scrape rather than on a timer, so the numbers are as fresh as the scrape.
    metrics.queue_depth.set(await streams.depth())
    metrics.dlq_depth.set(await streams.dlq_depth())
    metrics.is_leader.set(1 if get_elector().is_leader else 0)
    metrics.promotion_mode.labels(runbook="_engine", namespace=settings.self_namespace).set(0)
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "service": "healops-engine",
        "pipeline": "alert -> correlate -> plan -> policy -> [shadow|approval|execute] "
        "-> verify -> compensate?",
        "docs": "/docs",
    }


def _install_slack_adapter() -> None:
    """Mount the Slack AsyncApp if it is configured.

    Wired here rather than at import time so the engine runs with no Slack credentials at all:
    approvals are then held rather than executed, and the ledger and dashboard keep working.
    """
    settings = get_settings()
    if not settings.slack_enabled:
        log.info("slack not configured; approvals will be held, never auto-executed")
        return
    try:
        from integrations.slack_app import mount_slack

        mount_slack(app)
        log.info("slack adapter mounted on the FastAPI app")
    except ImportError:
        log.info("slack integration not present yet")


_install_slack_adapter()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104

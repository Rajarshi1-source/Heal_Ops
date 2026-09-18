"""The Alertmanager webhook receiver.

Alertmanager must never block on HealOps, so the receiver does the minimum and returns **202**:
persist durably, enqueue, acknowledge. Everything expensive -- planning, the policy gate,
execution, verification -- happens on a worker.

Three properties to preserve when editing:

* **Durable-then-ack.** PostgreSQL first, stream second, 202 third. Alertmanager retries on
  non-2xx, so a lost alert is worse than a duplicated one, and duplicates are handled by the
  idempotency key.
* **Idempotency key is `(fingerprint, runbook, incident_id)`.** Alertmanager runs an HA gossip
  cluster whose own dedup can still deliver twice.
* **Only cause alerts are enqueued.** Symptoms are persisted as evidence and go no further. This is
  where forty rollbacks become one remediation.
"""

from __future__ import annotations

import hmac

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, status

import metrics
from cache import dedup_store, streams
from config import get_settings
from core.alert_parser import AlertmanagerV2Payload
from core.ingestion import ingest_payload
from db.connection import transaction

log = structlog.get_logger(__name__)
router = APIRouter(tags=["webhooks"])


def verify_shared_secret(authorization: str | None) -> None:
    """Constant-time comparison.

    A `!=` on a secret leaks its prefix through response timing. The cost of `compare_digest` is
    nil and the alternative is a finding in any review.
    """
    expected = get_settings().webhook_shared_secret.get_secret_value()
    presented = ""
    if authorization:
        scheme, _, token = authorization.partition(" ")
        presented = token if scheme.lower() == "bearer" else authorization

    if not hmac.compare_digest(presented, expected):
        metrics.webhook_rejections.labels(reason="bad_secret").inc()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing webhook secret",
        )


@router.post(
    "/api/webhooks/alertmanager",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive an Alertmanager v2 alert batch",
)
async def receive_alertmanager(
    payload: AlertmanagerV2Payload,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    verify_shared_secret(authorization)

    settings = get_settings()
    if settings.read_only:
        # The public demo build refuses every write path regardless of policy. An endpoint that can
        # trigger remediation is never exposed to the internet.
        metrics.webhook_rejections.labels(reason="read_only").inc()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this deployment is read-only",
        )

    log.info(
        "alertmanager batch received",
        group_key=payload.groupKey,
        firing=len(payload.firing),
        resolved=len(payload.resolved),
        truncated=payload.truncatedAlerts,
    )

    # Step 1: durable. The ledger append happens inside this transaction, so if it fails nothing
    # is recorded and nothing is queued -- no ledger write, no action.
    async with transaction() as session:
        ingested = await ingest_payload(session, payload)

    # Step 2: enqueue. Only cause alerts get a job; symptoms stay as evidence on the incident.
    jobs: list[tuple[str, dict[str, object]]] = []
    deduplicated = 0
    for item in ingested:
        if not item.is_cause:
            continue
        if await dedup_store.seen_recently(item.fingerprint):
            deduplicated += 1
            metrics.alerts_deduplicated.inc()
            continue
        jobs.append(
            (
                "plan",
                {
                    "incident_id": item.incident_id,
                    "alert_event_id": item.alert_event_id,
                    "fingerprint": item.fingerprint,
                    "alertname": item.alertname,
                },
            )
        )

    if jobs:
        await streams.enqueue_batch(jobs)

    # Step 3: acknowledge.
    return {
        "accepted": len(ingested),
        "enqueued": len(jobs),
        "deduplicated": deduplicated,
        "incidents": sorted({i.incident_id for i in ingested}),
    }

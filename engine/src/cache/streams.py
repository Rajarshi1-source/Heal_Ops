"""Redis Streams: the remediation queue.

Two consumer groups on one stream, `exec` and `verify`, and that separation is a **bulkhead**, not
a tidiness preference. Verification waits out a five-minute window per execution. Sharing a group
with execution would let a verification backlog block new incident handling -- the failure mode
where the system stops responding precisely because it is busy responding.

`XACK` happens only after the ledger write lands. Alertmanager retries on non-2xx and a lost alert
is worse than a duplicated one, so the pipeline is idempotent and acknowledges late.

Messages that exhaust their retries go to `remediation-jobs-dlq` with an alert on depth, because
silent message loss is the failure this queue exists to prevent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from redis.exceptions import ResponseError

from cache.redis_client import get_client
from config import get_settings


@dataclass(frozen=True, slots=True)
class QueuedJob:
    message_id: str
    kind: str
    payload: dict[str, Any]
    delivery_count: int

    @property
    def incident_id(self) -> int | None:
        raw = self.payload.get("incident_id")
        return int(raw) if raw is not None else None


async def ensure_groups() -> None:
    """Create the stream and both consumer groups. Safe to call on every startup."""
    settings = get_settings()
    client = get_client()
    for group in (settings.consumer_group_exec, settings.consumer_group_verify):
        try:
            await client.xgroup_create(settings.stream_name, group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise


async def enqueue(kind: str, payload: dict[str, Any]) -> str:
    """Add a job. Called after the alert is durable in PostgreSQL, never before."""
    settings = get_settings()
    message_id = await get_client().xadd(
        settings.stream_name,
        {"kind": kind, "payload": json.dumps(payload, separators=(",", ":"))},
    )
    return str(message_id)


async def enqueue_batch(jobs: list[tuple[str, dict[str, Any]]]) -> list[str]:
    settings = get_settings()
    client = get_client()
    pipe = client.pipeline()
    for kind, payload in jobs:
        pipe.xadd(
            settings.stream_name,
            {"kind": kind, "payload": json.dumps(payload, separators=(",", ":"))},
        )
    return [str(message_id) for message_id in await pipe.execute()]


def _parse(message_id: str, fields: dict[str, str], delivery_count: int) -> QueuedJob:
    return QueuedJob(
        message_id=message_id,
        kind=fields.get("kind", "unknown"),
        payload=json.loads(fields.get("payload", "{}")),
        delivery_count=delivery_count,
    )


async def read(group: str, consumer: str, count: int = 10, block_ms: int = 5000) -> list[QueuedJob]:
    """Read new messages for this consumer group."""
    settings = get_settings()
    response = cast(
        list[tuple[str, list[tuple[str, dict[str, str]]]]],
        await get_client().xreadgroup(
            group, consumer, {settings.stream_name: ">"}, count=count, block=block_ms
        ),
    )
    jobs: list[QueuedJob] = []
    for _stream, messages in response or []:
        jobs.extend(_parse(mid, fields, 1) for mid, fields in messages)
    return jobs


async def claim_stale(
    group: str, consumer: str, min_idle_ms: int = 120_000, count: int = 10
) -> list[QueuedJob]:
    """Reclaim messages a dead consumer left pending.

    Without this, a worker that dies mid-job leaves its message pending forever: never acked, never
    redelivered, and invisible. The watchdog handles the *cluster* consequences of a crash; this
    handles the *queue* consequences.
    """
    settings = get_settings()
    client = get_client()
    result = cast(
        "tuple[str, list[tuple[str, dict[str, str]]], list[str]]",
        await client.xautoclaim(
            settings.stream_name, group, consumer, min_idle_time=min_idle_ms, count=count
        ),
    )
    messages = result[1] if len(result) > 1 else []

    jobs: list[QueuedJob] = []
    for mid, fields in messages:
        if not fields:
            # Tombstone for a message deleted from the stream; drop the pending entry.
            await client.xack(settings.stream_name, group, mid)
            continue
        pending = await client.xpending_range(
            settings.stream_name, group, min=mid, max=mid, count=1
        )
        delivery_count = int(pending[0]["times_delivered"]) if pending else 1
        jobs.append(_parse(mid, fields, delivery_count))
    return jobs


async def ack(group: str, message_id: str) -> None:
    """Acknowledge. Call this ONLY after the ledger write has landed."""
    await get_client().xack(get_settings().stream_name, group, message_id)


async def to_dlq(group: str, job: QueuedJob, reason: str) -> str:
    """Park a job that exhausted its retries, then ack it off the main stream."""
    settings = get_settings()
    client = get_client()
    message_id = await client.xadd(
        settings.dlq_name,
        {
            "kind": job.kind,
            "payload": json.dumps(job.payload, separators=(",", ":")),
            "reason": reason,
            "original_id": job.message_id,
            "delivery_count": str(job.delivery_count),
        },
    )
    await client.xack(settings.stream_name, group, job.message_id)
    return str(message_id)


async def dlq_depth() -> int:
    """Exported as `healops_dlq_depth`, alerting above zero."""
    try:
        return int(await get_client().xlen(get_settings().dlq_name))
    except ResponseError:
        return 0


async def should_retry(job: QueuedJob) -> bool:
    return job.delivery_count < get_settings().max_delivery_attempts


async def depth() -> int:
    try:
        return int(await get_client().xlen(get_settings().stream_name))
    except ResponseError:
        return 0

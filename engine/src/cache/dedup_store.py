"""Alert deduplication and the idempotency key.

Two different problems, deliberately kept apart:

* **Dedup** collapses the same alert re-delivered inside a short window. It is a cheap optimisation
  and losing it costs an extra plan, not correctness.
* **The idempotency key** `(fingerprint, runbook, incident_id)` is the correctness mechanism.
  Alertmanager runs an HA gossip cluster whose own dedup can still deliver twice, and a re-queued
  stream message can be redelivered, so the pipeline must be idempotent rather than assume it
  won't happen.

Neither is a substitute for correlation. Dedup is per-fingerprint, and in a storm every
fingerprint differs -- forty pods, forty fingerprints, nothing deduplicated. That was defect D5.
"""

from __future__ import annotations

from cache.redis_client import get_client
from config import get_settings


def dedup_key(fingerprint: str) -> str:
    return f"dedup:{fingerprint}"


def idempotency_key(fingerprint: str, runbook: str, incident_id: int | str) -> str:
    return f"idem:{fingerprint}:{runbook}:{incident_id}"


async def seen_recently(fingerprint: str, ttl_seconds: int | None = None) -> bool:
    """True if this exact alert was already accepted inside the dedup window.

    Sets the key when absent, so the first caller wins and the rest are told to stand down.
    """
    ttl = ttl_seconds or get_settings().dedup_ttl_seconds
    first = await get_client().set(dedup_key(fingerprint), "1", nx=True, ex=ttl)
    return not first


async def claim(
    fingerprint: str, runbook: str, incident_id: int | str, ttl_seconds: int = 3600
) -> bool:
    """Claim the right to act on this (alert, runbook, incident) exactly once.

    Returns True for the first caller and False for every redelivery. The TTL is long relative to
    the dedup window because a redelivery can arrive well after the original -- the point is to
    outlive the retry horizon, not the alert.
    """
    key = idempotency_key(fingerprint, runbook, incident_id)
    claimed = await get_client().set(key, "1", nx=True, ex=ttl_seconds)
    return bool(claimed)


async def release_claim(fingerprint: str, runbook: str, incident_id: int | str) -> None:
    """Undo a claim when the work did not actually start, so a retry is not silently swallowed."""
    await get_client().delete(idempotency_key(fingerprint, runbook, incident_id))

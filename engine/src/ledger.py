"""Append-only, hash-chained audit ledger.

Each entry commits to its predecessor, so altering or deleting any historical entry breaks every
hash after it and is detectable in a single pass:

    hash_n = sha256( hash_{n-1} || canonical_json(entry_n) )

Four details that are easy to break, and expensive to break silently:

1. **Canonical JSON is a frozen contract.** `sort_keys=True`, `separators=(",", ":")`. Any change
   to serialisation invalidates every historical hash. If it must ever change, version the chain
   rather than rewriting it.
2. **The database clock is the single source of time.** Never the application's.
3. **The chain is sequential**, so appends serialise. That is an accepted trade-off at under 100
   alerts/hour. If throughput ever mattered the answer is per-namespace chains with a periodic
   root, not abandoning the chain.
4. **`verify_chain` runs hourly in-cluster and in CI**, exported as `healops_ledger_valid`. A
   tamper-evidence mechanism nobody checks is decoration.

The application role holds INSERT and SELECT only; the migration revokes UPDATE and DELETE.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.connection import db_now
from db.models import AuditLedger

GENESIS: Final[str] = "0" * 64

# The vocabulary of auditable events. Refusals and denials are in here on purpose: "the system
# correctly decided not to act" is part of the record, not an absence from it.
EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "plan_requested",
        "plan",
        "policy",
        "shadow",
        "approve",
        "execute",
        "verify",
        "compensate",
        "promote",
        "freeze",
        "unfreeze",
        "gitops_pause",
        "gitops_resume",
        "state_transition",
        "illegal_transition",
        "masking_guard",
        "budget_exhausted",
        "breaker",
    }
)


def canonical(obj: Any) -> str:
    """The frozen serialisation contract. Do not "improve" this function."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: str, entry: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical(entry)).encode("utf-8")).hexdigest()


def build_entry(
    event_type: str,
    payload: dict[str, Any],
    occurred_at: datetime,
    actor: str,
    incident_id: int | None,
    execution_id: int | None,
) -> dict[str, Any]:
    """The exact dict that gets hashed.

    Only these fields participate. `seq` deliberately does not: it is assigned by the database and
    including it would make the hash depend on insertion order rather than on content.
    """
    return {
        "event_type": event_type,
        "payload": payload,
        "ids": {"incident_id": incident_id, "execution_id": execution_id},
        "actor": actor,
        "occurred_at": occurred_at.isoformat(),
    }


@dataclass(frozen=True, slots=True)
class ChainStatus:
    valid: bool
    entries: int
    broken_at_seq: int | None = None
    detail: str | None = None


async def last_hash(session: AsyncSession) -> str:
    row = await session.execute(select(AuditLedger.hash).order_by(AuditLedger.seq.desc()).limit(1))
    return row.scalar_one_or_none() or GENESIS


async def append(
    session: AsyncSession,
    event_type: str,
    payload: dict[str, Any],
    *,
    actor: str = "healops",
    incident_id: int | None = None,
    execution_id: int | None = None,
    bundle_sha256: str | None = None,
) -> str:
    """Append one entry. MUST run inside the same transaction as the state change it records.

    Returns the new head hash.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown ledger event_type {event_type!r}")

    # Serialise appends against concurrent writers. Without this two transactions could read the
    # same head and produce two entries claiming the same predecessor, which reads as a broken
    # chain forever afterwards.
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('healops_ledger'))"))

    prev = await last_hash(session)
    occurred_at = await db_now(session)
    entry = build_entry(event_type, payload, occurred_at, actor, incident_id, execution_id)
    digest = compute_hash(prev, entry)

    session.add(
        AuditLedger(
            occurred_at=occurred_at,
            actor=actor,
            event_type=event_type,
            incident_id=incident_id,
            execution_id=execution_id,
            payload=payload,
            prev_hash=prev,
            hash=digest,
            bundle_sha256=bundle_sha256,
        )
    )
    # Flush explicitly rather than relying on autoflush. Sessions are created with
    # `autoflush=False`, so without this a second append in the same transaction would not see
    # the first one's pending row: both would read the same head and claim the same predecessor,
    # and the chain would be permanently broken from that point on.
    await session.flush()
    return digest


async def verify_chain(session: AsyncSession, since: datetime | None = None) -> ChainStatus:
    """Walk the chain and recompute every hash.

    Exported as `healops_ledger_valid` (0/1) with an alert on 0, and run by
    `healopsctl audit --verify-chain`.
    """
    stmt = select(AuditLedger).order_by(AuditLedger.seq)
    if since is not None:
        stmt = stmt.where(AuditLedger.occurred_at >= since)

    prev = GENESIS
    count = 0
    first = True

    for row in (await session.execute(stmt)).scalars():
        # A partial walk starts from whatever the first row claims its predecessor was; a full
        # walk must start at GENESIS.
        if first and since is not None:
            prev = row.prev_hash
        first = False

        if row.prev_hash != prev:
            return ChainStatus(
                valid=False,
                entries=count,
                broken_at_seq=row.seq,
                detail=(
                    f"prev_hash mismatch at seq={row.seq}: "
                    f"stored {row.prev_hash[:12]}..., expected {prev[:12]}..."
                ),
            )

        entry = build_entry(
            row.event_type,
            row.payload,
            row.occurred_at,
            row.actor,
            row.incident_id,
            row.execution_id,
        )
        expected = compute_hash(prev, entry)
        if expected != row.hash:
            return ChainStatus(
                valid=False,
                entries=count,
                broken_at_seq=row.seq,
                detail=(
                    f"chain broken at seq={row.seq}: content does not match its hash "
                    f"(stored {row.hash[:12]}..., recomputed {expected[:12]}...)"
                ),
            )

        prev = row.hash
        count += 1

    return ChainStatus(valid=True, entries=count)


def verify_entries(entries: list[dict[str, Any]]) -> ChainStatus:
    """Offline verification for an evidence bundle -- no database, no network.

    `healopsctl replay <sha>` and the CI ledger gate both use this.
    """
    prev = GENESIS
    for i, row in enumerate(entries):
        entry = build_entry(
            row["event_type"],
            row["payload"],
            datetime.fromisoformat(row["occurred_at"]),
            row.get("actor", "healops"),
            row.get("incident_id"),
            row.get("execution_id"),
        )
        if i == 0:
            prev = row["prev_hash"]
        elif row["prev_hash"] != prev:
            return ChainStatus(
                valid=False,
                entries=i,
                broken_at_seq=row.get("seq"),
                detail=f"prev_hash mismatch at index {i}",
            )
        expected = compute_hash(prev, entry)
        if expected != row["hash"]:
            return ChainStatus(
                valid=False,
                entries=i,
                broken_at_seq=row.get("seq"),
                detail=f"hash mismatch at index {i}",
            )
        prev = row["hash"]
    return ChainStatus(valid=True, entries=len(entries))

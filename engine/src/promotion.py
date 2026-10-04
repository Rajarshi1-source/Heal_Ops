"""The promotion ladder: how automation earns authority, per runbook, per namespace.

    SHADOW    plans for real incidents but cannot act; a human judges each plan against what they
              did. Promote at >= 10 judged plans with >= 90% agreement.
    APPROVAL  acts only after a human approves. Promote at 20 consecutive verified-good outcomes.
    AUTO      acts within policy, unattended.

    demote (one strike) on: a failed verification that compensated, a "was this correct? -> No",
    the masking guard, or any change to the runbook's code or the policy file (back to APPROVAL).

**Promotion takes twenty; demotion takes one.** That asymmetry is the safety property. It is also
per namespace: a runbook can be fully automatic in `chaos` and approval-only in `payments`, which is
what makes the blast-radius argument concrete rather than rhetorical.

The ladder becomes *enforcement* through the policy file: `approve-not-promoted` requires approval
for anything not on `auto`, and the orchestrator's shadow branch refuses to act at all on `shadow`.
Shipping new engine code must never silently grant more power, so the runbook's source and the
policy file are fingerprinted; a changed fingerprint drops an `auto` runbook back to `approval`.
Promotion of the *software* is git -> CI -> Helm; promotion of the *authority* is this. Two
pipelines, deliberately.

Every transition is a ledger entry, so an auditor can see when a bot was granted authority and on
what evidence. The decision layer stays deterministic rules -- this module counts outcomes, it does
not learn anything.
"""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

import ledger
import metrics
from config import get_settings
from db.connection import db_now
from db.models import RunbookPromotion
from policy import PromotionSnapshot

log = structlog.get_logger(__name__)

Mode = Literal["shadow", "approval", "auto"]

SHADOW_MIN_PLANS: Final[int] = 10
SHADOW_MIN_AGREEMENT: Final[float] = 0.90
APPROVAL_MIN_CONSECUTIVE_GOOD: Final[int] = 20

#: The namespace key for node-scoped runbooks, which have none.
CLUSTER: Final[str] = "-"


@dataclass(frozen=True, slots=True)
class Transition:
    """A rung changed. The worker posts it to Slack after the transaction commits."""

    runbook: str
    namespace: str
    from_mode: str
    to_mode: str
    reason: str


def scope(namespace: str | None) -> str:
    return namespace or CLUSTER


# --- fingerprint ----------------------------------------------------------------------------------


@lru_cache(maxsize=32)
def fingerprint(runbook: str) -> str:
    """sha256 over the runbook's source and the policy file.

    Either changing means the evidence that earned `auto` was gathered on different logic, so it no
    longer counts. Cached per process: the code cannot change under a running engine.
    """
    from runbooks import load_all

    digest = hashlib.sha256()
    found = load_all().get(runbook)
    if found is not None:
        source = inspect.getsourcefile(type(found))
        if source:
            digest.update(Path(source).read_bytes())
    policy_file = Path(get_settings().policy_file)
    if policy_file.is_file():
        digest.update(policy_file.read_bytes())
    return digest.hexdigest()


# --- reading the rung -----------------------------------------------------------------------------


async def get(session: AsyncSession, runbook: str, namespace: str | None) -> PromotionSnapshot:
    """The rung the gate reads. No row means no authority yet: shadow."""
    row = await _row(session, runbook, namespace, create=True)
    current = fingerprint(runbook)
    if row.code_fingerprint is None:
        # First sight of this logic: it becomes the baseline. Changes are detected from here on.
        row.code_fingerprint = current
    elif row.code_fingerprint != current:
        row.code_fingerprint = current
        if row.mode == "auto":
            await _move(session, row, "approval", "the runbook's code or the policy file changed")
        row.consecutive_good = 0
    await session.flush()
    _export(row)
    return PromotionSnapshot(mode=row.mode, consecutive_good=row.consecutive_good)


async def _row(
    session: AsyncSession, runbook: str, namespace: str | None, *, create: bool
) -> RunbookPromotion:
    key = (runbook, scope(namespace))
    row = await session.get(RunbookPromotion, key, with_for_update=True)
    if row is None and create:
        row = RunbookPromotion(
            runbook=runbook,
            namespace=key[1],
            mode="shadow",
            consecutive_good=0,
            total_executions=0,
            total_reverted=0,
            shadow_plans=0,
            shadow_agreements=0,
            shadow_judged=0,
            last_changed_at=await db_now(session),
            changed_reason="new runbook: starts in shadow",
        )
        session.add(row)
        await session.flush()
    if row is None:
        raise LookupError(key)
    return row


# --- shadow ---------------------------------------------------------------------------------------


async def on_shadow_plan(session: AsyncSession, runbook: str, namespace: str | None) -> None:
    row = await _row(session, runbook, namespace, create=True)
    row.shadow_plans += 1
    await session.flush()


async def on_shadow_feedback(
    session: AsyncSession, runbook: str, namespace: str | None, *, agreed: bool, by: str
) -> Transition | None:
    """A human compared a shadow plan with what they actually did."""
    row = await _row(session, runbook, namespace, create=True)
    row.shadow_judged += 1
    if agreed:
        row.shadow_agreements += 1
    rate = row.shadow_agreements / row.shadow_judged
    metrics.shadow_agreement_ratio.labels(runbook=runbook, namespace=row.namespace).set(rate)
    await ledger.append(
        session,
        "promote",
        {
            "stage": "shadow_feedback",
            "runbook": runbook,
            "namespace": row.namespace,
            "agreed": agreed,
            "agreement": round(rate, 3),
            "judged": row.shadow_judged,
        },
        actor=by,
    )

    if (
        row.mode == "shadow"
        and row.shadow_judged >= SHADOW_MIN_PLANS
        and rate >= SHADOW_MIN_AGREEMENT
    ):
        return await _move(
            session,
            row,
            "approval",
            f"agreed with humans on {row.shadow_agreements} of {row.shadow_judged} shadow plans "
            f"({rate:.0%})",
        )
    await session.flush()
    return None


# --- outcomes -------------------------------------------------------------------------------------


async def on_outcome(
    session: AsyncSession, runbook: str, namespace: str | None, *, good: bool, why: str
) -> Transition | None:
    """A verified outcome of an execution that actually acted (approval or auto mode)."""
    row = await _row(session, runbook, namespace, create=True)
    row.total_executions += 1
    row.last_outcome = "good" if good else why

    if not good:
        row.total_reverted += 1
        row.consecutive_good = 0
        if row.mode == "auto":
            return await _move(session, row, "approval", f"one strike: {why}")
        await session.flush()
        return None

    row.consecutive_good += 1
    if row.mode == "approval" and row.consecutive_good >= APPROVAL_MIN_CONSECUTIVE_GOOD:
        return await _move(
            session,
            row,
            "auto",
            f"{row.consecutive_good} consecutive verified remediations in {row.namespace}",
        )
    await session.flush()
    return None


async def demote(
    session: AsyncSession, runbook: str, namespace: str | None, *, reason: str
) -> Transition | None:
    """Drop to approval now. Used by "was this correct? -> No" and by the masking guard."""
    row = await _row(session, runbook, namespace, create=True)
    row.consecutive_good = 0
    if row.mode == "auto":
        return await _move(session, row, "approval", reason)
    row.changed_reason = reason
    await session.flush()
    return None


async def set_mode(
    session: AsyncSession, runbook: str, namespace: str | None, mode: Mode, *, by: str, reason: str
) -> Transition | None:
    """A human override (`healopsctl promote`). Recorded like any other transition."""
    row = await _row(session, runbook, namespace, create=True)
    row.code_fingerprint = fingerprint(runbook)
    if row.mode == mode:
        return None
    return await _move(session, row, mode, f"manual: {reason}", actor=by)


async def _move(
    session: AsyncSession,
    row: RunbookPromotion,
    to_mode: str,
    reason: str,
    *,
    actor: str = "healops",
) -> Transition:
    transition = Transition(row.runbook, row.namespace, row.mode, to_mode, reason)
    row.mode = to_mode
    row.changed_reason = reason
    row.last_changed_at = await db_now(session)
    await session.flush()
    await ledger.append(
        session,
        "promote",
        {
            "runbook": row.runbook,
            "namespace": row.namespace,
            "from": transition.from_mode,
            "to": to_mode,
            "reason": reason,
        },
        actor=actor,
    )
    _export(row)
    log.info(
        "promotion changed",
        runbook=row.runbook,
        namespace=row.namespace,
        to=to_mode,
        reason=reason,
    )
    return transition


def _export(row: RunbookPromotion) -> None:
    metrics.promotion_mode.labels(runbook=row.runbook, namespace=row.namespace).set(
        metrics.PROMOTION_MODE_VALUES[row.mode]
    )

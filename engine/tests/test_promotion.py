"""The promotion ladder: shadow -> approval -> auto, earned on evidence, lost on one strike."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import promotion
from db.models import AuditLedger, RunbookPromotion

pytestmark = pytest.mark.db

RB = "crashloop_rollback"


async def rung(session: AsyncSession, namespace: str = "shop") -> RunbookPromotion:
    row = await session.get(RunbookPromotion, (RB, namespace))
    assert row is not None
    return row


async def set_rung(session: AsyncSession, mode: str, namespace: str = "shop") -> None:
    await promotion.set_mode(session, RB, namespace, mode, by="test", reason="setup")  # type: ignore[arg-type]


def test_the_specified_thresholds() -> None:
    assert promotion.SHADOW_MIN_PLANS == 10
    assert promotion.SHADOW_MIN_AGREEMENT == 0.90
    assert promotion.APPROVAL_MIN_CONSECUTIVE_GOOD == 20


async def test_a_new_runbook_starts_in_shadow(db_session: AsyncSession) -> None:
    snap = await promotion.get(db_session, RB, "shop")
    assert snap.mode == "shadow"


async def test_shadow_promotes_on_ten_judged_plans_at_ninety_percent(
    db_session: AsyncSession,
) -> None:
    for _ in range(9):
        assert (
            await promotion.on_shadow_feedback(db_session, RB, "shop", agreed=True, by="u") is None
        )
    transition = await promotion.on_shadow_feedback(db_session, RB, "shop", agreed=True, by="u")

    assert transition is not None and transition.to_mode == "approval"
    assert "10 of 10" in transition.reason


async def test_disagreement_holds_a_runbook_in_shadow(db_session: AsyncSession) -> None:
    for agreed in [True] * 8 + [False, False]:
        await promotion.on_shadow_feedback(db_session, RB, "shop", agreed=agreed, by="u")
    assert (await rung(db_session)).mode == "shadow", "8/10 is below 90%"


async def test_unjudged_shadow_plans_are_not_evidence_either_way(db_session: AsyncSession) -> None:
    for _ in range(30):
        await promotion.on_shadow_plan(db_session, RB, "shop")
    row = await rung(db_session)
    assert row.shadow_plans == 30 and row.shadow_judged == 0 and row.mode == "shadow"


async def test_approval_promotes_after_twenty_consecutive_good(db_session: AsyncSession) -> None:
    await set_rung(db_session, "approval")
    for _ in range(19):
        assert await promotion.on_outcome(db_session, RB, "shop", good=True, why="") is None
    transition = await promotion.on_outcome(db_session, RB, "shop", good=True, why="")

    assert transition is not None and transition.to_mode == "auto"
    assert "20 consecutive" in transition.reason


async def test_one_bad_outcome_resets_the_streak(db_session: AsyncSession) -> None:
    await set_rung(db_session, "approval")
    for _ in range(19):
        await promotion.on_outcome(db_session, RB, "shop", good=True, why="")
    await promotion.on_outcome(db_session, RB, "shop", good=False, why="verification not_recovered")
    assert (await rung(db_session)).consecutive_good == 0
    assert await promotion.on_outcome(db_session, RB, "shop", good=True, why="") is None


async def test_demotion_is_one_strike(db_session: AsyncSession) -> None:
    """Promotion takes twenty; demotion takes one. That asymmetry is the safety property."""
    await set_rung(db_session, "auto")

    transition = await promotion.on_outcome(
        db_session, RB, "shop", good=False, why="verification not_recovered"
    )

    assert transition is not None and transition.to_mode == "approval"
    assert "one strike" in transition.reason
    assert (await rung(db_session)).total_reverted == 1


async def test_authority_is_per_namespace(db_session: AsyncSession) -> None:
    await set_rung(db_session, "auto", "chaos")
    await set_rung(db_session, "approval", "payments")

    await promotion.on_outcome(db_session, RB, "chaos", good=False, why="x")

    assert (await promotion.get(db_session, RB, "payments")).mode == "approval"
    assert (await promotion.get(db_session, RB, "chaos")).mode == "approval"
    assert (await promotion.get(db_session, RB, "shop")).mode == "shadow"


async def test_a_human_no_demotes(db_session: AsyncSession) -> None:
    await set_rung(db_session, "auto")
    transition = await promotion.demote(db_session, RB, "shop", reason="U1 answered 'No'")
    assert transition is not None and transition.to_mode == "approval"


async def test_demoting_a_runbook_without_authority_changes_no_rung(
    db_session: AsyncSession,
) -> None:
    assert await promotion.demote(db_session, RB, "shop", reason="x") is None
    assert (await rung(db_session)).mode == "shadow"


async def test_a_code_or_policy_change_resets_auto_to_approval(
    db_session: AsyncSession,
) -> None:
    """Shipping new code must never silently keep authority earned on the old code."""
    await set_rung(db_session, "auto")
    row = await rung(db_session)
    row.code_fingerprint = "0" * 64
    await db_session.flush()

    snap = await promotion.get(db_session, RB, "shop")

    assert snap.mode == "approval"
    assert "changed" in ((await rung(db_session)).changed_reason or "")


async def test_an_unchanged_fingerprint_keeps_authority(db_session: AsyncSession) -> None:
    await set_rung(db_session, "auto")
    assert (await promotion.get(db_session, RB, "shop")).mode == "auto"


def test_the_fingerprint_covers_code_and_policy() -> None:
    first = promotion.fingerprint(RB)
    assert len(first) == 64
    assert promotion.fingerprint("oom_memory_bump") != first


async def test_every_transition_is_a_ledger_entry(db_session: AsyncSession) -> None:
    await set_rung(db_session, "auto")
    await promotion.on_outcome(db_session, RB, "shop", good=False, why="x")

    payloads = (
        (
            await db_session.execute(
                select(AuditLedger.payload)
                .where(AuditLedger.event_type == "promote")
                .order_by(AuditLedger.seq.desc())
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    assert [p["to"] for p in payloads] == ["approval", "auto"]


async def test_node_runbooks_use_the_cluster_scope(db_session: AsyncSession) -> None:
    snap = await promotion.get(db_session, "node_drain", None)
    assert snap.mode == "shadow"
    assert await db_session.get(RunbookPromotion, ("node_drain", "-")) is not None

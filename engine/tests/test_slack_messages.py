"""Block Kit builders. Pure functions, so message shape is testable with no Slack at all.

What is asserted is content a human relies on -- the literal actions, the registered undo, the
deadline from the database, the evidence hash -- plus the structural limits Slack enforces.
"""

from __future__ import annotations

import json
from typing import Any

from compensator import CompensationOutcome
from integrations import slack_messages as m
from plan import refuse
from policy import PolicyDecision
from tests.factories import make_plan
from verifier import Verdict


def text_of(blocks: list[dict[str, Any]]) -> str:
    return json.dumps(blocks, ensure_ascii=False)


def comp(**overrides: Any) -> CompensationOutcome:
    base: dict[str, Any] = {
        "status": "reverted",
        "reverted": True,
        "detail": "rolled forward to revision 4",
        "escalate": True,
        "trigger": "verification",
        "ledger_hash": "ab" * 32,
    }
    return CompensationOutcome(**(base | overrides))


def buttons(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for b in blocks if b["type"] == "actions" for e in b["elements"]]


def test_approval_card_shows_what_will_happen_and_how_to_undo_it() -> None:
    blocks = m.approval_request(
        make_plan(), 42, reasons=("blast radius above 25",), expires_at="2026-10-04T12:10:00+00:00"
    )
    text = text_of(blocks)

    assert "patch apps/v1/Deployment shop/api" in text
    assert "Registered undo" in text
    assert "blast radius above 25" in text
    assert "2026-10-04T12:10:00+00:00" in text
    assert "never auto-executes" in text
    assert {b["action_id"] for b in buttons(blocks)} == {"healops_approve", "healops_reject"}
    assert all(b["value"] == "42" for b in buttons(blocks))


def test_approval_card_includes_cost_when_there_is_one() -> None:
    assert "₹900/month" in text_of(m.approval_request(make_plan(cost_delta_inr_month=900.0), 1))


def test_resolved_card_has_no_buttons_left_to_click() -> None:
    blocks = m.approval_resolved(make_plan(), approved=True, decided_by="U1")
    assert buttons(blocks) == []
    assert "<@U1>" in text_of(blocks)


def test_expired_card_says_nothing_was_executed() -> None:
    assert "Nothing was executed" in text_of(m.approval_expired(make_plan()))


def test_success_card_says_how_it_knows_and_asks_for_feedback() -> None:
    verdict = Verdict(outcome="remediated", reasons=("alert cleared",), alert_cleared=True)
    blocks = m.verification_success(make_plan(), verdict, execution_id=9)

    assert "alert cleared" in text_of(blocks)
    assert {b["action_id"] for b in buttons(blocks)} == {
        "healops_feedback_yes",
        "healops_feedback_no",
    }


def test_reverted_card_distinguishes_failure_from_uncertainty() -> None:
    failed = m.verification_reverted(
        make_plan(), Verdict(outcome="not_recovered", reasons=("still firing",)), comp()
    )
    unsure = m.verification_reverted(
        make_plan(), Verdict(outcome="inconclusive", reasons=("no signals",)), comp()
    )

    assert "Why it failed" in text_of(failed)
    assert "could not be confirmed" in text_of(unsure)
    assert "ledger `abababababababab`" in text_of(failed), "evidence is one lookup away"


def test_watchdog_and_revert_failed_cards_page_oncall() -> None:
    assert "subteam^oncall" in text_of(m.watchdog_reverted(make_plan(), comp(trigger="watchdog")))
    failed = m.revert_failed(make_plan(), comp(status="revert_failed", reverted=False))
    assert "COMPENSATION FAILED" in text_of(failed)


def test_evidence_is_omitted_when_there_is_no_hash() -> None:
    assert "ledger `" not in text_of(m.watchdog_reverted(make_plan(), comp(ledger_hash=None)))


def test_inaction_gets_first_class_cards() -> None:
    refusal = refuse("scripted_runbook", 1, "StatefulSets are excluded")
    assert "StatefulSets are excluded" in text_of(m.refusal(refusal))

    decision = PolicyDecision(
        effect="deny", reasons=("deny-protected",), messages=("stateful workloads are excluded",)
    )
    denied = text_of(m.policy_denied(make_plan(), decision))
    assert "deny-protected" in denied and "policy/remediation.yaml" in denied

    assert "SHADOW" in text_of(m.shadow_notice(make_plan()))
    assert "could not be applied" in text_of(m.execution_failed(make_plan(), "409 conflict"))


def test_expiry_and_authority_cards() -> None:
    assert "pull/1" in text_of(m.expiry_notice(make_plan(), pr_url="https://x/pull/1"))
    assert "promoted" in text_of(m.promotion_notice("r", "shop", to_mode="auto", evidence="20/20"))
    assert "demoted" in text_of(m.promotion_notice("r", "shop", to_mode="approval", evidence="1"))
    masking = text_of(m.masking_guard_warning("r", "shop/api", 4, "https://x/issues/7"))
    assert "4 times" in masking and "issues/7" in masking


def test_header_text_respects_slacks_limit() -> None:
    header = m._header("x" * 400)
    assert len(header["text"]["text"]) == 150


def test_fallback_text_prefers_the_header() -> None:
    assert m.fallback_text(m.approval_expired(make_plan())).startswith(":raising_hand:")
    assert m.fallback_text(m.verification_success(make_plan(), Verdict(outcome="remediated")))
    assert m.fallback_text([]) == "HealOps notification"

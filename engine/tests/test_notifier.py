"""Which card each outcome gets -- and that a notification can never break the caller."""

from __future__ import annotations

from typing import Any

import pytest

import notifier
from compensator import CompensationOutcome
from integrations.slack_client import PostedMessage
from orchestrator import HandleResult
from policy import PolicyDecision
from tests.factories import make_plan
from verifier import Verdict


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    sent: dict[str, list[Any]] = {"post": [], "update": []}

    async def post(blocks: list[dict[str, Any]], **_kwargs: Any) -> PostedMessage:
        sent["post"].append(blocks)
        return PostedMessage("C1", "1700.1")

    async def update(message: PostedMessage, blocks: list[dict[str, Any]]) -> bool:
        sent["update"].append((message, blocks))
        return True

    async def first_time(_execution_id: int) -> bool:
        return True

    monkeypatch.setattr(notifier.slack_client, "post", post)
    monkeypatch.setattr(notifier.slack_client, "update", update)
    monkeypatch.setattr(notifier.approval_store, "mark_notified", first_time)
    return sent


def header(blocks: list[dict[str, Any]]) -> str:
    return next(b["text"]["text"] for b in blocks if b["type"] in ("header", "section"))


def result(outcome: str, **overrides: Any) -> HandleResult:
    base: dict[str, Any] = {"outcome": outcome, "plan": make_plan(), "execution_id": 5}
    return HandleResult(**(base | overrides))


def comp(**overrides: Any) -> CompensationOutcome:
    base: dict[str, Any] = {
        "status": "reverted",
        "reverted": True,
        "detail": "d",
        "escalate": True,
        "trigger": "verification",
    }
    return CompensationOutcome(**(base | overrides))


async def test_each_orchestrator_outcome_gets_its_card(slack: dict[str, list[Any]]) -> None:
    deny = PolicyDecision(effect="deny", reasons=("r",), messages=("m",))
    for outcome, extra in (
        ("refused", {}),
        ("denied", {"decision": deny}),
        ("shadow", {}),
        ("execute_failed", {"detail": "409"}),
        ("blocked", {"detail": "locked"}),
    ):
        await notifier.orchestrated(result(outcome, **extra))

    headers = [header(b) for b in slack["post"]]
    assert ["refused" in headers[0], "Denied" in headers[1], "SHADOW" in headers[2]] == [True] * 3
    assert "could not be applied" in headers[3] and "could not be applied" in headers[4]


async def test_acting_is_not_announced(slack: dict[str, list[Any]]) -> None:
    """The verdict is the news, and it follows within the window."""
    await notifier.orchestrated(result("executed"))
    assert slack["post"] == []


async def test_the_approval_card_is_posted_once_and_its_handle_returned(
    slack: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = await notifier.orchestrated(result("awaiting_approval"), expires_at="2026-10-04T12:00")
    assert posted == PostedMessage("C1", "1700.1")

    async def already(_execution_id: int) -> bool:
        return False

    monkeypatch.setattr(notifier.approval_store, "mark_notified", already)
    assert await notifier.orchestrated(result("awaiting_approval")) is None
    assert len(slack["post"]) == 1


async def test_an_unreachable_cache_still_posts_the_approval_card(
    slack: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate card is harmless; no card means nobody is ever asked."""

    async def down(_execution_id: int) -> bool:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(notifier.approval_store, "mark_notified", down)
    assert await notifier.orchestrated(result("awaiting_approval")) is not None


async def test_verification_outcomes_map_to_their_cards(slack: dict[str, list[Any]]) -> None:
    bad = Verdict(outcome="not_recovered", reasons=("still firing",))
    await notifier.verified(
        make_plan(),
        Verdict(outcome="remediated"),
        comp(status="remediated", reverted=False, escalate=False),
        execution_id=1,
    )
    await notifier.verified(make_plan(), bad, comp(), execution_id=1)
    await notifier.verified(
        make_plan(), bad, comp(status="revert_failed", reverted=False), execution_id=1
    )

    headers = [header(b) for b in slack["post"]]
    assert "remediated" in headers[0]
    assert "reverted itself" in headers[1]
    assert "COMPENSATION FAILED" in headers[2]


async def test_watchdog_outcomes_map_to_their_cards(slack: dict[str, list[Any]]) -> None:
    await notifier.watchdog_acted(make_plan(), comp(trigger="watchdog"))
    await notifier.watchdog_acted(make_plan(), comp(trigger="expiry", status="expired"))
    await notifier.watchdog_acted(make_plan(), comp(status="revert_failed", reverted=False))

    headers = [header(b) for b in slack["post"]]
    assert "Watchdog" in headers[0]
    assert "Mitigation expired" in headers[1]
    assert "COMPENSATION FAILED" in headers[2]


async def test_an_expired_approval_retires_its_card_in_place(slack: dict[str, list[Any]]) -> None:
    await notifier.approval_expired(make_plan(), channel="C1", message_ts="1700.1")
    await notifier.approval_expired(make_plan(), channel=None, message_ts=None)

    assert slack["update"][0][0] == PostedMessage("C1", "1700.1")
    assert len(slack["post"]) == 1, "never posted means there is no card to update"


async def test_a_broken_notification_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("malformed card")

    monkeypatch.setattr(notifier.slack_client, "post", explode)
    monkeypatch.setattr(notifier.slack_client, "update", explode)

    await notifier.orchestrated(result("refused"))
    await notifier.verified(make_plan(), Verdict(outcome="remediated"), comp(), execution_id=1)
    await notifier.watchdog_acted(make_plan(), comp())
    await notifier.approval_expired(make_plan(), channel="C1", message_ts="1")

"""The Bolt handlers: a Slack click is an input, never an authority.

The handlers translate a verified interaction into `approval_manager.resolve()` and retire the card.
Bolt's signature check is Bolt's to test; what is tested here is what a click is allowed to do.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from cache import streams
from config import get_settings
from core import approval_manager
from db.models import IncidentState
from integrations import slack_app
from tests.factories import bound_to, ledger_payloads, make_execution


class Respond:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def click(execution_id: Any, user: str | None = "U123") -> dict[str, Any]:
    body: dict[str, Any] = {
        "actions": [{"value": str(execution_id)}],
        "channel": {"id": "C1"},
        "message": {"ts": "1700.5"},
    }
    if user is not None:
        body["user"] = {"id": user}
    return body


@pytest.fixture
def slack_configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    settings = get_settings()
    monkeypatch.setattr(settings, "slack_bot_token", SecretStr("xoxb-test"))
    monkeypatch.setattr(settings, "slack_signing_secret", SecretStr("signing-secret"))
    yield


# --- payload parsing ------------------------------------------------------------------------------


def test_payload_fields_are_extracted_defensively() -> None:
    body = click(42)
    assert slack_app._execution_id(body) == 42
    assert slack_app._user_id(body) == "U123"
    assert slack_app._channel_id(body) == "C1"
    assert slack_app._message_ts(body) == "1700.5"

    assert slack_app._execution_id({}) is None
    assert slack_app._execution_id({"actions": [{"value": "not-a-number"}]}) is None
    assert slack_app._user_id({}) is None


# --- construction and mounting --------------------------------------------------------------------


def test_build_app_refuses_without_a_signing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """No secret means Bolt cannot verify a click, and an unverified click must never approve."""
    monkeypatch.setattr(get_settings(), "slack_signing_secret", None)
    monkeypatch.setattr(get_settings(), "slack_bot_token", SecretStr("xoxb-test"))
    with pytest.raises(RuntimeError, match="signing secret"):
        slack_app.build_app()


def test_mount_puts_slack_on_the_same_server(slack_configured: None) -> None:
    """One web server, one port."""
    app = FastAPI()
    slack_app.mount_slack(app)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert {"/slack/events", "/slack/interactivity"} <= paths


# --- the decision handler -------------------------------------------------------------------------


@pytest.fixture
def handler_env(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, dict[str, Any]]]:
    enqueued: list[tuple[str, dict[str, Any]]] = []

    async def enqueue(kind: str, payload: dict[str, Any]) -> str:
        enqueued.append((kind, payload))
        return "1-0"

    async def no_cache(*_args: Any) -> None:
        return None

    monkeypatch.setattr(slack_app, "transaction", bound_to(db_session))
    monkeypatch.setattr(streams, "enqueue", enqueue)
    monkeypatch.setattr(approval_manager.approval_store, "mirror", no_cache)
    monkeypatch.setattr(approval_manager.approval_store, "invalidate", no_cache)
    return enqueued


async def awaiting(session: AsyncSession) -> Any:
    incident, execution, _ = await make_execution(
        session,
        incident_state=IncidentState.AWAITING_APPROVAL,
        status="awaiting_approval",
        mode="approval",
        finished_ago_seconds=None,
        decision="require_approval",
    )
    await approval_manager.request(session, execution)
    return incident, execution


@pytest.mark.db
async def test_approve_click_queues_once_and_retires_the_card(
    db_session: AsyncSession, handler_env: list[tuple[str, dict[str, Any]]]
) -> None:
    incident, execution = await awaiting(db_session)
    respond = Respond()

    await slack_app._record_decision(click(execution.id), respond, approved=True)
    await slack_app._record_decision(click(execution.id), respond, approved=True)

    assert handler_env == [
        ("execute_approved", {"execution_id": execution.id, "incident_id": incident.id})
    ], "a double click is one decision"
    assert respond.calls[0]["replace_original"] is True
    assert "Approved" in str(respond.calls[0]["blocks"])
    entry = (await ledger_payloads(db_session, incident.id, "approve"))[0]
    assert entry["decided_by"] == "U123"


@pytest.mark.db
async def test_reject_click_escalates_and_queues_nothing(
    db_session: AsyncSession, handler_env: list[tuple[str, dict[str, Any]]]
) -> None:
    incident, execution = await awaiting(db_session)
    respond = Respond()

    await slack_app._record_decision(click(execution.id), respond, approved=False)

    assert handler_env == []
    assert incident.state == IncidentState.ESCALATED.value
    assert "Rejected" in str(respond.calls[0]["blocks"])


@pytest.mark.db
async def test_click_for_an_unknown_execution_is_answered_not_raised(
    db_session: AsyncSession, handler_env: list[tuple[str, dict[str, Any]]]
) -> None:
    respond = Respond()
    await slack_app._record_decision(click(987_654_321), respond, approved=True)
    assert "No execution" in respond.calls[0]["text"]


async def test_malformed_click_is_ignored() -> None:
    respond = Respond()
    await slack_app._record_decision(click(1, user=None), respond, approved=True)
    assert respond.calls == []


@pytest.mark.db
async def test_feedback_is_recorded_against_the_execution(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(slack_app, "transaction", bound_to(db_session))
    incident, execution, _ = await make_execution(db_session)
    respond = Respond()

    await slack_app._record_feedback(click(execution.id, "U9"), respond, correct=False)

    [entry] = await ledger_payloads(db_session, incident.id, "verify")
    assert entry == {
        "stage": "human_feedback",
        "execution_id": execution.id,
        "was_correct": False,
        "answered_by": "U9",
    }
    assert "demote" in respond.calls[0]["text"]


# --- Phase 6: feedback that changes authority -----------------------------------------------------


@pytest.mark.db
async def test_a_no_demotes_the_runbook(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import notifier
    import promotion
    from db.models import RunbookPromotion

    monkeypatch.setattr(slack_app, "transaction", bound_to(db_session))
    posted: list[Any] = []

    async def promoted(transition: Any) -> None:
        posted.append(transition)

    monkeypatch.setattr(notifier, "promoted", promoted)
    await promotion.set_mode(db_session, "scripted_runbook", "shop", "auto", by="t", reason="t")
    _, execution, _ = await make_execution(db_session)

    await slack_app._record_feedback(click(execution.id, "U9"), Respond(), correct=False)

    row = await db_session.get(RunbookPromotion, ("scripted_runbook", "shop"))
    assert row is not None and row.mode == "approval"
    assert posted and "U9" in posted[0].reason


@pytest.mark.db
async def test_shadow_judgements_are_recorded_on_the_ladder(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from db.models import RunbookPromotion

    monkeypatch.setattr(slack_app, "transaction", bound_to(db_session))
    _, execution, _ = await make_execution(
        db_session, incident_state=IncidentState.REMEDIATED, status="shadow", mode="shadow"
    )
    respond = Respond()

    await slack_app._record_shadow_judgement(click(execution.id), respond, agreed=True)

    row = await db_session.get(RunbookPromotion, ("scripted_runbook", "shop"))
    assert row is not None and (row.shadow_judged, row.shadow_agreements) == (1, 1)
    assert "recorded" in respond.calls[0]["text"]


@pytest.mark.db
async def test_only_shadow_plans_can_be_judged(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(slack_app, "transaction", bound_to(db_session))
    _, execution, _ = await make_execution(db_session)
    respond = Respond()
    await slack_app._record_shadow_judgement(click(execution.id), respond, agreed=True)
    assert "not a shadow plan" in respond.calls[0]["text"]

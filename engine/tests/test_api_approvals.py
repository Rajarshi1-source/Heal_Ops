"""`/api/approvals`: authenticated, fail-closed when unconfigured, idempotent.

The route handlers are called directly for the database cases (the test session is bound to this
event loop); the HTTP-level auth checks go through the TestClient because they never touch the
database.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from api import approvals
from config import get_settings
from core import approval_manager
from db.models import IncidentState
from tests.factories import bound_to, make_execution

TOKEN = "dashboard-token"


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(get_settings(), "approvals_api_token", SecretStr(TOKEN))
    yield


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(approvals.router)
    return TestClient(app)


BODY = {"decision": "approve", "decided_by": "alice"}


def test_no_configured_token_refuses_every_decision(client: TestClient) -> None:
    """Fail closed: with no way to authenticate a caller, Slack is the only approval path."""
    response = client.post("/api/approvals/1", json=BODY, headers={"Authorization": "Bearer x"})
    assert response.status_code == 503


def test_a_wrong_or_missing_token_is_rejected(client: TestClient, token: None) -> None:
    assert client.post("/api/approvals/1", json=BODY).status_code == 401
    wrong = client.post("/api/approvals/1", json=BODY, headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    raw = client.post("/api/approvals/1", json=BODY, headers={"Authorization": TOKEN})
    assert raw.status_code == 401, "only the bearer scheme is accepted"


def test_a_read_only_build_refuses_decisions(
    client: TestClient, token: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "read_only", True)
    response = client.post(
        "/api/approvals/1", json=BODY, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 403


@pytest.fixture
def db_env(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    enqueued: list[Any] = []

    async def enqueue(kind: str, payload: dict[str, Any]) -> str:
        enqueued.append((kind, payload))
        return "1-0"

    async def no_cache(*_args: Any) -> None:
        return None

    monkeypatch.setattr(approvals, "transaction", bound_to(db_session))
    monkeypatch.setattr(approvals.streams, "enqueue", enqueue)
    monkeypatch.setattr(approval_manager.approval_store, "mirror", no_cache)
    monkeypatch.setattr(approval_manager.approval_store, "invalidate", no_cache)
    return enqueued


async def awaiting(session: AsyncSession) -> Any:
    _, execution, _ = await make_execution(
        session,
        incident_state=IncidentState.AWAITING_APPROVAL,
        status="awaiting_approval",
        mode="approval",
        finished_ago_seconds=None,
        decision="require_approval",
    )
    await approval_manager.request(session, execution)
    return execution


@pytest.mark.db
async def test_approve_enqueues_exactly_once(
    db_session: AsyncSession, db_env: list[Any], token: None
) -> None:
    execution = await awaiting(db_session)
    body = approvals.ApprovalDecision(decision="approve", decided_by="alice")

    first = await approvals.decide(execution.id, body, f"Bearer {TOKEN}")
    second = await approvals.decide(execution.id, body, f"Bearer {TOKEN}")

    assert first.allowed and first.enqueued and first.status == "queued"
    assert second.allowed and not second.enqueued
    assert len(db_env) == 1


@pytest.mark.db
async def test_reject_is_recorded_and_not_enqueued(
    db_session: AsyncSession, db_env: list[Any], token: None
) -> None:
    execution = await awaiting(db_session)
    result = await approvals.decide(
        execution.id,
        approvals.ApprovalDecision(decision="reject", decided_by="bob"),
        f"Bearer {TOKEN}",
    )
    assert result.allowed is False and result.enqueued is False
    assert result.status == approval_manager.REJECTED
    assert db_env == []


@pytest.mark.db
async def test_unknown_execution_is_404(
    db_session: AsyncSession, db_env: list[Any], token: None
) -> None:
    with pytest.raises(HTTPException) as exc:
        await approvals.decide(
            987_654_321,
            approvals.ApprovalDecision(decision="approve", decided_by="a"),
            f"Bearer {TOKEN}",
        )
    assert exc.value.status_code == 404


@pytest.mark.db
async def test_read_reports_the_authoritative_state(
    db_session: AsyncSession, db_env: list[Any]
) -> None:
    execution = await awaiting(db_session)
    state = await approvals.read(execution.id)
    assert state["allowed"] is False
    assert "awaiting" in str(state["reason"])

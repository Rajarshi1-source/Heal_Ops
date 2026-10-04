"""Approvals: PostgreSQL owns the decision, and `can_execute()` fails closed.

The property under test is that every state other than "approved, inside its window" refuses --
including the states that only arise from bugs, races or outages. A safety control that can be
talked into "yes" by an unreadable row is not one.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core import approval_manager
from db.connection import db_now
from db.models import Approval, IncidentState, RunbookExecution
from tests.factories import ledger_payloads, make_execution

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def cache_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """The Redis mirror is a cache; its semantics are covered in `test_approval_store.py`."""
    calls: list[tuple[str, Any]] = []

    async def mirror(approval: Any) -> None:
        calls.append(("mirror", approval))

    async def invalidate(execution_id: Any) -> None:
        calls.append(("invalidate", execution_id))

    monkeypatch.setattr(approval_manager.approval_store, "mirror", mirror)
    monkeypatch.setattr(approval_manager.approval_store, "invalidate", invalidate)
    return calls


async def awaiting(session: AsyncSession) -> tuple[Any, RunbookExecution, Approval]:
    incident, execution, _ = await make_execution(
        session,
        incident_state=IncidentState.AWAITING_APPROVAL,
        status="awaiting_approval",
        mode="approval",
        finished_ago_seconds=None,
        decision="require_approval",
    )
    approval = await approval_manager.request(session, execution, reasons=("blast radius 30",))
    return incident, execution, approval


# --- request --------------------------------------------------------------------------------------


async def test_request_sets_the_deadline_in_the_database(db_session: AsyncSession) -> None:
    _, _, approval = await awaiting(db_session)
    now = await db_now(db_session)

    assert approval.status == "pending"
    assert approval.expires_at == now + timedelta(seconds=600)
    assert approval.reason == "blast radius 30"


async def test_request_is_idempotent_per_execution(db_session: AsyncSession) -> None:
    """A double-delivered job must not produce two approval requests for one decision."""
    _, execution, first = await awaiting(db_session)
    second = await approval_manager.request(db_session, execution)
    assert second.id == first.id


# --- can_execute: fail closed ---------------------------------------------------------------------


async def test_no_record_refuses(db_session: AsyncSession) -> None:
    result = await approval_manager.can_execute(db_session, 987_654_321)
    assert result.allowed is False
    assert "fail closed" in result.reason


async def test_pending_refuses(db_session: AsyncSession) -> None:
    _, execution, _ = await awaiting(db_session)
    result = await approval_manager.can_execute(db_session, execution.id)
    assert not result
    assert "awaiting" in result.reason


async def test_pending_past_its_deadline_expires_and_refuses(db_session: AsyncSession) -> None:
    """Silence is not consent."""
    _, execution, approval = await awaiting(db_session)
    approval.expires_at = await db_now(db_session) - timedelta(seconds=1)
    await db_session.flush()

    result = await approval_manager.can_execute(db_session, execution.id)

    assert not result
    assert "expired" in result.reason
    await db_session.refresh(approval)
    assert approval.status == "expired", "the row transitions; it is never deleted"


async def test_approved_inside_the_window_allows(db_session: AsyncSession) -> None:
    _, execution, _ = await awaiting(db_session)
    await approval_manager.decide(db_session, execution.id, approved=True, decided_by="U123")

    result = await approval_manager.can_execute(db_session, execution.id)

    assert result.allowed is True
    assert result.decided_by == "U123"


async def test_approved_after_the_window_refuses(db_session: AsyncSession) -> None:
    _, execution, approval = await awaiting(db_session)
    approval.status = "approved"
    approval.decided_by = "U123"
    approval.decided_at = approval.expires_at + timedelta(seconds=5)
    await db_session.flush()

    assert not await approval_manager.can_execute(db_session, execution.id)


async def test_approved_without_a_timestamp_is_not_trusted(db_session: AsyncSession) -> None:
    _, execution, approval = await awaiting(db_session)
    approval.status = "approved"
    await db_session.flush()

    result = await approval_manager.can_execute(db_session, execution.id)
    assert not result
    assert "inconsistent" in result.reason


async def test_rejected_refuses(db_session: AsyncSession) -> None:
    _, execution, _ = await awaiting(db_session)
    await approval_manager.decide(db_session, execution.id, approved=False, decided_by="U1")
    assert not await approval_manager.can_execute(db_session, execution.id)


async def test_unreadable_state_refuses(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: Any) -> None:
        raise ConnectionError("postgres went away")

    monkeypatch.setattr(approval_manager, "_fetch", broken)
    result = await approval_manager.can_execute(db_session, 1)
    assert not result
    assert "unreadable" in result.reason


# --- decide: one decision per question ------------------------------------------------------------


async def test_decision_is_audited_with_the_human_as_actor(db_session: AsyncSession) -> None:
    incident, execution, _ = await awaiting(db_session)
    await approval_manager.decide(db_session, execution.id, approved=True, decided_by="U123")

    [entry] = await ledger_payloads(db_session, incident.id, "approve")
    assert entry["stage"] == "granted"
    assert entry["decided_by"] == "U123"


async def test_a_second_click_does_not_overwrite_the_first(db_session: AsyncSession) -> None:
    _, execution, _ = await awaiting(db_session)
    await approval_manager.decide(db_session, execution.id, approved=True, decided_by="alice")

    second = await approval_manager.decide(
        db_session, execution.id, approved=False, decided_by="bob"
    )

    assert second.allowed is True
    assert second.decided_by == "alice"


async def test_a_decision_after_the_deadline_is_recorded_as_expiry(
    db_session: AsyncSession,
) -> None:
    incident, execution, approval = await awaiting(db_session)
    approval.expires_at = await db_now(db_session) - timedelta(seconds=1)
    await db_session.flush()

    result = await approval_manager.decide(
        db_session, execution.id, approved=True, decided_by="late"
    )

    assert not result
    [entry] = await ledger_payloads(db_session, incident.id, "approve")
    assert entry["stage"] == "expired"
    assert entry["attempted_by"] == "late"


async def test_decide_without_a_record_refuses(db_session: AsyncSession) -> None:
    assert not await approval_manager.decide(db_session, 999_999, approved=True, decided_by="x")


async def test_a_cache_outage_does_not_roll_back_a_decision(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redis is disposable. A cache write failing inside the decision must not undo the decision."""

    async def down(*_args: Any) -> None:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(approval_manager.approval_store, "mirror", down)
    _, execution, approval = await awaiting(db_session)

    result = await approval_manager.decide(db_session, execution.id, approved=True, decided_by="U1")

    assert result.allowed is True
    assert approval.status == "approved"


# --- resolve: what a decision does to the execution -----------------------------------------------


async def test_approval_queues_the_execution_once(db_session: AsyncSession) -> None:
    _, execution, _ = await awaiting(db_session)

    first = await approval_manager.resolve(db_session, execution, approved=True, decided_by="U1")
    assert first.enqueue is True
    assert execution.status == "queued"

    # A worker picks it up and starts running it; then the button is clicked again.
    execution.status = "executing"
    again = await approval_manager.resolve(db_session, execution, approved=True, decided_by="U1")

    assert again.enqueue is False, "a double click must not queue the execution twice"
    assert execution.status == "executing", "and must not rewind an execution already running"


async def test_rejection_escalates_the_incident(db_session: AsyncSession) -> None:
    incident, execution, _ = await awaiting(db_session)

    result = await approval_manager.resolve(db_session, execution, approved=False, decided_by="U1")

    assert result.enqueue is False
    assert execution.status == approval_manager.REJECTED
    assert incident.state == IncidentState.ESCALATED.value
    assert incident.outcome == "rejected"


async def test_a_late_approval_escalates_as_expired(db_session: AsyncSession) -> None:
    incident, execution, approval = await awaiting(db_session)
    approval.expires_at = await db_now(db_session) - timedelta(seconds=1)
    await db_session.flush()

    result = await approval_manager.resolve(db_session, execution, approved=True, decided_by="U1")

    assert result.enqueue is False
    assert execution.status == approval_manager.APPROVAL_EXPIRED
    assert incident.state == IncidentState.ESCALATED.value


# --- the reaper -----------------------------------------------------------------------------------


async def test_reaper_expires_and_escalates(
    db_session: AsyncSession, cache_calls: list[tuple[str, Any]]
) -> None:
    incident, execution, approval = await awaiting(db_session)
    approval.expires_at = await db_now(db_session) - timedelta(seconds=1)
    await db_session.flush()

    reaped = await approval_manager.reap_expired(db_session)

    assert [r.execution_id for r in reaped] == [execution.id]
    await db_session.refresh(approval)
    assert approval.status == "expired"
    assert execution.status == approval_manager.APPROVAL_EXPIRED
    assert incident.state == IncidentState.ESCALATED.value
    assert ("invalidate", execution.id) in cache_calls
    [entry] = await ledger_payloads(db_session, incident.id, "approve")
    assert entry["stage"] == "expired"


async def test_reaper_leaves_in_window_approvals_alone(db_session: AsyncSession) -> None:
    _, execution, _ = await awaiting(db_session)
    reaped = await approval_manager.reap_expired(db_session)
    assert execution.id not in [r.execution_id for r in reaped]
    assert execution.status == "awaiting_approval"

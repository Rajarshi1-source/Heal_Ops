"""The dashboard's read API: the trail, the evidence, and the scorecard's honest denominators."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api import approvals as approvals_api
from api import incidents as api
from api import runbooks as runbooks_api
from core import approval_manager
from db.models import IncidentState, RunbookPromotion, Verification
from plan import refuse
from tests.factories import bound_to, make_execution, make_incident

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def bound(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (api, approvals_api):
        monkeypatch.setattr(module, "session_scope", bound_to(db_session), raising=False)
    import db.connection

    monkeypatch.setattr(db.connection, "session_scope", bound_to(db_session))


def stages(summary: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(s["stage"], s["state"], s["label"]) for s in summary["trail"]]


async def verdict(session: AsyncSession, execution: Any, outcome: str, compensated: bool) -> None:
    session.add(
        Verification(
            execution_id=execution.id,
            window_seconds=300,
            signals_before={},
            signals_after={},
            outcome=outcome,
            compensated=compensated,
            reasons=["r"],
        )
    )
    await session.flush()


# --- the trail ------------------------------------------------------------------------------------


async def test_a_remediated_incident_shows_every_stage_passing(db_session: AsyncSession) -> None:
    incident, execution, _ = await make_execution(
        db_session, incident_state=IncidentState.REMEDIATED, status="remediated"
    )
    incident.outcome = "remediated"
    await verdict(db_session, execution, "remediated", False)

    summary = await api.get_incident(incident.id)

    assert summary["outcome"] == "remediated"
    assert [s[0] for s in stages(summary)] == [
        "correlate",
        "plan",
        "policy",
        "gate",
        "execute",
        "verify",
        "outcome",
    ]
    assert all(state == "passed" for _, state, _ in stages(summary))


async def test_a_reverted_incident_reads_as_a_correction_not_a_failure(
    db_session: AsyncSession,
) -> None:
    incident, execution, _ = await make_execution(
        db_session, incident_state=IncidentState.REVERTED, status="reverted"
    )
    incident.outcome = "reverted"
    execution.compensated = True
    await verdict(db_session, execution, "not_recovered", True)

    trail = stages(await api.get_incident(incident.id))

    assert ("verify", "failed", "not_recovered") in trail
    assert ("compensate", "corrected", "reverted") in trail
    assert trail[-1] == ("outcome", "corrected", "reverted")


async def test_inaction_is_a_first_class_stage(db_session: AsyncSession) -> None:
    import orchestrator
    from policy import PolicyDecision

    refused = await make_incident(db_session, state=IncidentState.ESCALATED, outcome="refused")
    await orchestrator._persist_plan(
        db_session,
        refused,
        refuse("r", refused.id, "StatefulSets are excluded"),
        PolicyDecision(effect="deny"),
    )
    trail = stages(await api.get_incident(refused.id))
    assert ("plan", "stopped", "refused") in trail

    denied, _, _ = await make_execution(
        db_session, incident_state=IncidentState.ESCALATED, decision="deny"
    )
    denied.outcome = "denied"
    assert ("policy", "stopped", "denied") in stages(await api.get_incident(denied.id))

    shadow, _, _ = await make_execution(
        db_session, incident_state=IncidentState.REMEDIATED, status="shadow", mode="shadow"
    )
    shadow.outcome = "shadow"
    summary = await api.get_incident(shadow.id)
    assert ("gate", "stopped", "shadow") in stages(summary)
    assert summary["outcome"] == "shadow"


async def test_a_pending_approval_stops_the_trail_at_the_gate(db_session: AsyncSession) -> None:
    incident, execution, _ = await make_execution(
        db_session,
        incident_state=IncidentState.AWAITING_APPROVAL,
        status="awaiting_approval",
        mode="approval",
        finished_ago_seconds=None,
        decision="require_approval",
    )
    await approval_manager.request(db_session, execution)
    summary = await api.get_incident(incident.id)
    assert stages(summary)[-1] == ("gate", "pending", "awaiting approval")
    assert summary["outcome"] == "pending"


def test_raw_outcomes_normalise_to_the_badge_vocabulary() -> None:
    from db.models import Incident

    def of(state: str, outcome: str | None) -> str:
        return api.outcome_of(Incident(state=state, outcome=outcome))

    assert of("escalated", "rejected") == "escalated"
    assert of("escalated", "blocked") == "escalated"
    assert of("reverted", "expired") == "reverted"
    assert of("verifying", None) == "pending"


# --- detail ---------------------------------------------------------------------------------------


async def test_detail_carries_the_whole_evidence_trail(db_session: AsyncSession) -> None:
    incident, execution, _ = await make_execution(db_session)
    execution.state_before, execution.state_after = {"revision": 4}, {"revision": 3}
    import ledger

    await ledger.append(db_session, "execute", {"ok": True}, incident_id=incident.id)

    detail = await api.get_incident(incident.id)

    assert detail["plans"][0]["runbook"] == "scripted_runbook"
    assert detail["executions"][0]["state_after"] == {"revision": 3}
    assert detail["ledger"][0]["hash"] and detail["ledger"][0]["event_type"] == "execute"


async def test_an_unknown_incident_is_a_404(db_session: AsyncSession) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await api.get_incident(987_654_321)


# --- list -----------------------------------------------------------------------------------------


async def test_the_list_is_newest_first_and_filterable(db_session: AsyncSession) -> None:
    await make_incident(db_session, namespace="payments", target_key="payments/Deployment/x")
    shadow, _, _ = await make_execution(
        db_session, incident_state=IncidentState.REMEDIATED, status="shadow", mode="shadow"
    )
    shadow.outcome = "shadow"
    await db_session.flush()

    page = await api.list_incidents(limit=200, offset=0, namespace=None, outcome=None)
    assert page["total"] >= 2

    payments = await api.list_incidents(limit=200, offset=0, namespace="payments", outcome=None)
    assert {i["namespace"] for i in payments["items"]} == {"payments"}

    only_shadow = await api.list_incidents(limit=200, offset=0, namespace=None, outcome="shadow")
    assert only_shadow["items"] and {i["outcome"] for i in only_shadow["items"]} == {"shadow"}


# --- the scorecard --------------------------------------------------------------------------------


async def test_the_scorecard_carries_its_denominators(db_session: AsyncSession) -> None:
    good, good_execution, _ = await make_execution(
        db_session, incident_state=IncidentState.REMEDIATED, status="remediated"
    )
    good.outcome = "remediated"
    await verdict(db_session, good_execution, "remediated", False)

    bad, bad_execution, _ = await make_execution(
        db_session, incident_state=IncidentState.REVERTED, status="reverted"
    )
    bad.outcome = "reverted"
    await verdict(db_session, bad_execution, "not_recovered", True)

    card = await api.stats()

    false = card["false_remediation"]
    assert false["numerator"] >= 1 and false["denominator"] >= 2
    assert 0 < false["value"] <= 1
    assert card["auto_resolution"]["denominator"] == card["incidents"]
    assert card["time_to_detect_failure_seconds"] is not None


# --- approvals and runbooks -----------------------------------------------------------------------


async def test_pending_approvals_carry_what_the_card_shows(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, execution, _ = await make_execution(
        db_session,
        incident_state=IncidentState.AWAITING_APPROVAL,
        status="awaiting_approval",
        mode="approval",
        finished_ago_seconds=None,
        decision="require_approval",
    )
    approval = await approval_manager.request(db_session, execution)

    listing = await approvals_api.list_approvals()

    [item] = [i for i in listing["items"] if i["execution_id"] == execution.id]
    assert item["expires_at"] == approval.expires_at, "the database owns the deadline"
    assert item["plan"]["compensation"] and item["plan"]["blast_radius_score"] == 3


async def test_the_catalog_shows_promotion_per_namespace(db_session: AsyncSession) -> None:
    db_session.add(RunbookPromotion(runbook="crashloop_rollback", namespace="shop", mode="auto"))
    await db_session.flush()

    catalog = await runbooks_api.list_runbooks()

    entry = next(r for r in catalog["runbooks"] if r["name"] == "crashloop_rollback")
    assert entry["promotion"][0]["namespace"] == "shop"
    assert entry["promotion"][0]["mode"] == "auto"

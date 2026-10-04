"""Compensation: the saga's undo, and what each path records.

Three claims this file exists to prove:

* only a proven recovery keeps the change -- `not_recovered` and `inconclusive` both revert;
* a revert that itself fails is recorded as the worst case and escalates, never swallowed;
* a TTL expiry is not a failure: it closes the execution as `expired`, leaves a settled incident
  alone, and stays out of the false-remediation rate.
"""

from __future__ import annotations

import gc

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import compensator
import verifier
from db.models import IncidentState, Verification
from tests.factories import (
    ScriptedRunbook,
    ledger_events,
    ledger_payloads,
    make_execution,
)
from verifier import Verdict

pytestmark = pytest.mark.db


def verdict(outcome: str, *reasons: str) -> Verdict:
    return Verdict(outcome=outcome, reasons=reasons or ("x",))  # type: ignore[arg-type]


# --- path 1: the verifier decided -----------------------------------------------------------------


async def test_remediated_closes_without_compensating(db_session: AsyncSession) -> None:
    incident, execution, plan = await make_execution(db_session)
    runbook = ScriptedRunbook()

    outcome = await compensator.on_verification(
        db_session, incident, execution, plan, runbook, verdict("remediated")
    )

    assert outcome.status == compensator.CLOSED_REMEDIATED
    assert outcome.reverted is False and outcome.escalate is False
    assert runbook.compensate_calls == 0
    assert execution.status == "remediated"
    assert incident.state == IncidentState.REMEDIATED.value
    assert incident.outcome == "remediated"


async def test_not_recovered_reverts_and_escalates(db_session: AsyncSession) -> None:
    incident, execution, plan = await make_execution(db_session)
    runbook = ScriptedRunbook()

    outcome = await compensator.on_verification(
        db_session, incident, execution, plan, runbook, verdict("not_recovered", "still firing")
    )

    assert runbook.compensate_calls == 1
    assert outcome.status == compensator.CLOSED_REVERTED
    assert outcome.reverted is True
    assert outcome.escalate is True, "the original problem is back; a human needs the evidence"
    assert execution.compensated is True
    assert execution.status == "reverted"


async def test_a_verification_revert_is_scored_as_reverted(db_session: AsyncSession) -> None:
    """`reverted` is the scorecard's bucket for the system catching its own mistake.

    Recording it as a generic `escalated` would hide the number the project is proudest of.
    """
    incident, execution, plan = await make_execution(db_session)

    await compensator.on_verification(
        db_session, incident, execution, plan, ScriptedRunbook(), verdict("not_recovered")
    )

    assert incident.state == IncidentState.REVERTED.value
    assert incident.outcome == "reverted"


async def test_inconclusive_also_reverts(db_session: AsyncSession) -> None:
    """A change that could not be shown to work is not left in production on trust."""
    incident, execution, plan = await make_execution(db_session)
    runbook = ScriptedRunbook()

    outcome = await compensator.on_verification(
        db_session, incident, execution, plan, runbook, verdict("inconclusive", "no signals")
    )

    assert runbook.compensate_calls == 1
    assert outcome.reverted is True
    assert "could not confirm recovery" in (execution.detail or "")


async def test_compensation_is_audited_with_its_trigger_and_hash(db_session: AsyncSession) -> None:
    incident, execution, plan = await make_execution(db_session)

    outcome = await compensator.on_verification(
        db_session, incident, execution, plan, ScriptedRunbook(), verdict("not_recovered")
    )

    assert await ledger_events(db_session, incident.id) == ["compensate"]
    [payload] = await ledger_payloads(db_session, incident.id, "compensate")
    assert payload["trigger"] == "verification"
    assert payload["ok"] is True
    assert payload["plan_id"] == execution.plan_id
    assert outcome.ledger_hash is not None and len(outcome.ledger_hash) == 64


async def test_the_verification_row_is_flagged_compensated(db_session: AsyncSession) -> None:
    """The false-remediation rate is read from one table, so the flag sits next to the verdict."""
    incident, execution, plan = await make_execution(db_session)
    bad = verdict("not_recovered", "still firing")
    await verifier.record(db_session, execution, plan, bad)

    # The session holds clean rows only weakly; make sure the verification row is not in memory,
    # which is the situation that left the flag unset when it was found by walking the session.
    gc.collect()

    await compensator.on_verification(db_session, incident, execution, plan, ScriptedRunbook(), bad)

    flagged = await db_session.scalar(
        select(Verification.compensated).where(Verification.execution_id == execution.id)
    )
    assert flagged is True


# --- the worst case ------------------------------------------------------------------------------


async def test_a_failed_undo_is_revert_failed(db_session: AsyncSession) -> None:
    incident, execution, plan = await make_execution(db_session)

    outcome = await compensator.on_verification(
        db_session,
        incident,
        execution,
        plan,
        ScriptedRunbook(compensate_ok=False),
        verdict("not_recovered"),
    )

    assert outcome.failed_to_revert is True
    assert outcome.escalate is True
    assert execution.status == "revert_failed"
    assert "COMPENSATION FAILED" in (execution.detail or "")
    assert incident.state == IncidentState.ESCALATED.value
    assert incident.outcome == "revert_failed"


async def test_a_raising_undo_is_recorded_not_swallowed(db_session: AsyncSession) -> None:
    incident, execution, plan = await make_execution(db_session)

    outcome = await compensator.on_verification(
        db_session,
        incident,
        execution,
        plan,
        ScriptedRunbook(compensate_raises=RuntimeError("api server gone")),
        verdict("not_recovered"),
    )

    assert outcome.failed_to_revert is True
    [payload] = await ledger_payloads(db_session, incident.id, "compensate")
    assert payload["ok"] is False
    assert "api server gone" in payload["detail"]


# --- path 2: the watchdog -------------------------------------------------------------------------


async def test_force_revert_is_a_watchdog_compensation(db_session: AsyncSession) -> None:
    incident, execution, plan = await make_execution(db_session)

    outcome = await compensator.force_revert(
        db_session, incident, execution, plan, ScriptedRunbook(), reason="never verified"
    )

    assert outcome.trigger == "watchdog"
    assert outcome.reverted is True
    assert incident.state == IncidentState.REVERTED.value
    [payload] = await ledger_payloads(db_session, incident.id, "compensate")
    assert payload["trigger"] == "watchdog"
    assert payload["reason"] == "never verified"


# --- expiry is not a failure ---------------------------------------------------------------------


async def test_expiry_after_remediation_leaves_the_incident_closed(
    db_session: AsyncSession,
) -> None:
    """Terminal states are terminal. Before this was fixed, expiring a fix on a remediated incident
    raised `IllegalTransition` and the ephemeral change was never removed."""
    incident, execution, plan = await make_execution(
        db_session, incident_state=IncidentState.REMEDIATED, status="remediated"
    )
    runbook = ScriptedRunbook()

    outcome = await compensator.expire(db_session, incident, execution, plan, runbook)

    assert runbook.compensate_calls == 1
    assert outcome.status == compensator.CLOSED_EXPIRED
    assert outcome.escalate is False
    assert execution.status == "expired", "kept out of the false-remediation rate"
    assert incident.state == IncidentState.REMEDIATED.value
    assert incident.outcome is None or incident.outcome != "reverted"


async def test_expiry_before_verification_closes_as_reverted(db_session: AsyncSession) -> None:
    incident, execution, plan = await make_execution(db_session)

    await compensator.expire(db_session, incident, execution, plan, ScriptedRunbook())

    assert incident.state == IncidentState.REVERTED.value
    assert incident.outcome == "expired"


async def test_a_failed_expiry_still_escalates(db_session: AsyncSession) -> None:
    incident, execution, plan = await make_execution(db_session)

    outcome = await compensator.expire(
        db_session, incident, execution, plan, ScriptedRunbook(compensate_ok=False)
    )

    assert outcome.failed_to_revert is True
    assert incident.state == IncidentState.ESCALATED.value


def test_outcome_flags_are_derived_from_status() -> None:
    failed = compensator.CompensationOutcome(
        status=compensator.CLOSED_REVERT_FAILED, reverted=False, detail="x", escalate=True
    )
    assert failed.failed_to_revert is True

"""The incident state machine.

The transitions that must NOT exist are the interesting half of this file. A machine that lets
`executing` jump straight to `remediated` would let the system claim success without verifying, and
that is precisely defect D1 reappearing as a state-machine bug rather than a runbook bug.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import pytest

from core.incident_state import (
    ALLOWED,
    TERMINAL,
    IllegalTransition,
    assert_transition,
    can_transition,
    is_terminal,
    outcome_for,
)
from db.models import IncidentState as S


def test_the_happy_path_is_reachable() -> None:
    path = [S.OPEN, S.PLANNING, S.AWAITING_APPROVAL, S.EXECUTING, S.VERIFYING, S.REMEDIATED]
    for source, target in pairwise(path):
        assert can_transition(source, target), f"{source} -> {target} must be allowed"


def test_the_auto_path_skips_approval() -> None:
    # Policy decides auto-vs-approval from plan properties, so planning must be able to go
    # straight to executing without inventing a fake approval record.
    assert can_transition(S.PLANNING, S.EXECUTING)


def test_execution_cannot_claim_success_without_verifying() -> None:
    """This is defect D1 expressed as a state machine.

    "The API call returned 200" must not be a route to `remediated`. The only edge into a success
    claim comes from `verifying`.
    """
    assert not can_transition(S.EXECUTING, S.REMEDIATED)
    assert not can_transition(S.EXECUTING, S.REVERTED)
    assert can_transition(S.VERIFYING, S.REMEDIATED)


def test_only_verifying_can_produce_remediated() -> None:
    sources = [s for s in S if can_transition(s, S.REMEDIATED)]
    # `planning` is included deliberately: a shadow-mode plan or a self-resolved incident closes
    # without ever acting, and that is an honest outcome rather than a claimed fix.
    assert set(sources) == {S.VERIFYING, S.PLANNING}


def test_approval_timeout_escalates_and_never_executes_itself() -> None:
    assert can_transition(S.AWAITING_APPROVAL, S.ESCALATED)
    # Executing from awaiting_approval is allowed only because a human granted it; there is no
    # edge that bypasses the decision, and expiry routes to escalated instead.
    assert can_transition(S.AWAITING_APPROVAL, S.EXECUTING)
    assert not can_transition(S.AWAITING_APPROVAL, S.REMEDIATED)


def test_every_non_terminal_state_can_escalate() -> None:
    # Escalation is always available: the system must be able to hand any incident to a human.
    for state in S:
        if state in TERMINAL:
            continue
        assert can_transition(state, S.ESCALATED), f"{state} cannot escalate"


def test_terminal_states_are_terminal() -> None:
    for state in TERMINAL:
        assert ALLOWED[state] == frozenset()
        assert is_terminal(state)
        # A recurrence is a NEW incident. Reopening would collapse repetitions and blind the
        # anti-masking guard to the thing it exists to count.
        for target in S:
            assert not can_transition(state, target)


def test_backwards_transitions_are_rejected() -> None:
    assert not can_transition(S.VERIFYING, S.EXECUTING)
    assert not can_transition(S.EXECUTING, S.PLANNING)
    assert not can_transition(S.PLANNING, S.OPEN)


def test_assert_transition_returns_the_target() -> None:
    assert assert_transition(1, S.OPEN, S.PLANNING) is S.PLANNING


def test_assert_transition_raises_with_a_useful_message() -> None:
    with pytest.raises(IllegalTransition) as exc:
        assert_transition(42, S.EXECUTING, S.REMEDIATED)

    message = str(exc.value)
    assert "incident 42" in message
    assert "executing -> remediated" in message
    # The message lists what WAS allowed, so the ledger entry recording this raise is diagnostic
    # rather than merely an error string.
    assert "verifying" in message
    assert exc.value.incident_id == 42


def test_unknown_states_are_rejected_rather_than_assumed() -> None:
    assert not can_transition("not_a_state", S.PLANNING)
    assert not can_transition(S.OPEN, "not_a_state")
    assert not is_terminal("not_a_state")
    assert outcome_for("not_a_state") is None


def test_outcome_mapping_treats_escalation_as_a_first_class_outcome() -> None:
    assert outcome_for(S.REMEDIATED) == "remediated"
    assert outcome_for(S.REVERTED) == "reverted"
    # Escalation is the system admitting "not mine". It is a good number, not a failure, and the
    # scorecard depends on it being reported as its own bucket.
    assert outcome_for(S.ESCALATED) == "escalated"
    assert outcome_for(S.OPEN) is None


def test_every_state_appears_in_the_transition_table() -> None:
    # A state missing from ALLOWED would raise KeyError at runtime inside can_transition.
    assert set(ALLOWED) == set(S)


async def test_reaching_a_terminal_state_records_when_the_incident_closed(
    db_session: Any,
) -> None:
    """Regression: `closed_at` existed from the first migration and nothing ever wrote it."""
    from db.models import Incident, IncidentState
    from tests.factories import make_incident

    incident: Incident = await make_incident(db_session)
    assert incident.closed_at is None
    incident.state = IncidentState.REMEDIATED.value
    await db_session.flush()
    await db_session.refresh(incident)
    assert incident.closed_at is not None and incident.closed_at >= incident.opened_at

    # Once closed, it stays closed at that moment: a later write does not move it.
    first = incident.closed_at
    incident.state = IncidentState.ESCALATED.value
    await db_session.flush()
    await db_session.refresh(incident)
    assert incident.closed_at == first

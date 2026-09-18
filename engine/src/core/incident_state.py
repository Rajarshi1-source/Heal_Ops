"""The incident state machine.

Monotonic and explicit:

    open -> planning -> awaiting_approval -> executing -> verifying
                                                       -> {remediated | reverted | escalated}

Two properties worth stating because both are easy to lose:

1. **Illegal transitions raise**, and the raise is itself a ledger entry. A state machine that
   silently accepts a nonsense transition will eventually record an incident as `remediated` that
   never executed, and the scorecard becomes fiction.
2. **Terminal states are terminal.** Nothing leaves `remediated`, `reverted` or `escalated`. A
   recurrence is a *new* incident, which is exactly what lets the anti-masking guard count
   repetitions instead of collapsing them.
"""

from __future__ import annotations

from typing import Final

from db.models import IncidentState

S = IncidentState

# Only these edges exist. Anything else raises.
ALLOWED: Final[dict[IncidentState, frozenset[IncidentState]]] = {
    S.OPEN: frozenset({S.PLANNING, S.ESCALATED}),
    # Planning can end without acting: a refusal or a policy denial escalates or closes, and a
    # shadow-mode plan records and stops. Those branches are outcomes, not errors.
    S.PLANNING: frozenset({S.AWAITING_APPROVAL, S.EXECUTING, S.ESCALATED, S.REMEDIATED}),
    # An approval that expires escalates. It never auto-executes.
    S.AWAITING_APPROVAL: frozenset({S.EXECUTING, S.ESCALATED}),
    S.EXECUTING: frozenset({S.VERIFYING, S.ESCALATED}),
    # Verification is the only path to a success claim, and it can also decide the opposite.
    S.VERIFYING: frozenset({S.REMEDIATED, S.REVERTED, S.ESCALATED}),
    S.REMEDIATED: frozenset(),
    S.REVERTED: frozenset(),
    S.ESCALATED: frozenset(),
}

TERMINAL: Final[frozenset[IncidentState]] = frozenset({S.REMEDIATED, S.REVERTED, S.ESCALATED})


class IllegalTransition(RuntimeError):
    """An attempt to move the incident somewhere the machine does not allow.

    Callers must append an `illegal_transition` ledger entry before re-raising: the attempt is
    itself evidence about a bug in the orchestrator, and losing it makes the bug invisible.
    """

    def __init__(self, incident_id: int | None, source: str, target: str) -> None:
        self.incident_id = incident_id
        self.source = source
        self.target = target
        super().__init__(
            f"incident {incident_id}: illegal transition {source} -> {target}; "
            f"allowed from {source}: "
            f"{sorted(s.value for s in ALLOWED.get(IncidentState(source), frozenset()))}"
        )


def can_transition(source: str | IncidentState, target: str | IncidentState) -> bool:
    try:
        src, dst = IncidentState(source), IncidentState(target)
    except ValueError:
        return False
    return dst in ALLOWED[src]


def assert_transition(
    incident_id: int | None, source: str | IncidentState, target: str | IncidentState
) -> IncidentState:
    """Validate an edge and return the target. Raises `IllegalTransition` otherwise."""
    if not can_transition(source, target):
        raise IllegalTransition(incident_id, str(source), str(target))
    return IncidentState(target)


def is_terminal(state: str | IncidentState) -> bool:
    try:
        return IncidentState(state) in TERMINAL
    except ValueError:
        return False


def outcome_for(state: str | IncidentState) -> str | None:
    """The scorecard bucket for a terminal state.

    `escalated` is a **good** outcome, not a failure: it is the system admitting "not mine". The
    efficacy scorecard depends on this distinction, so it lives here rather than in a chart query.
    """
    mapping = {
        S.REMEDIATED: "remediated",
        S.REVERTED: "reverted",
        S.ESCALATED: "escalated",
    }
    try:
        return mapping.get(IncidentState(state))
    except ValueError:
        return None

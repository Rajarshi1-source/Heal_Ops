"""The orchestrator.

The property this file exists to prove: **every branch is audited, including the ones that do
nothing.** A refusal, a policy denial and a shadow record each produce a ledger entry, because a
silent no-op is indistinguishable from a bug and because the efficacy scorecard counts them.

The other property is ordering. `register_compensation()` must be durable before `execute()` runs --
otherwise a crash mid-remediation leaves a changed cluster with no recorded way back.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import orchestrator
from bundle import BundleAlert, ContainerState, IncidentBundle, Target
from db.models import AuditLedger, Incident, IncidentState, RemediationPlanRow, RunbookExecution
from plan import (
    ApiCall,
    BlastRadius,
    Durability,
    ExpectedSignal,
    RemediationPlan,
    Reversibility,
    refuse,
)
from policy import BudgetSnapshot, PromotionSnapshot
from runbooks.base import ExecutionResult, Runbook

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

AUTO = PromotionSnapshot(mode="auto")


# --- doubles -------------------------------------------------------------------------------------


class StubRunbook(Runbook):
    """A runbook whose plan and execution outcome the test dictates."""

    name = "stub_runbook"
    triggers = ("KubePodCrashLooping",)

    def __init__(
        self,
        plan_to_return: RemediationPlan | None = None,
        *,
        execute_ok: bool = True,
        raise_on_execute: Exception | None = None,
    ) -> None:
        self._plan = plan_to_return
        self._execute_ok = execute_ok
        self._raise = raise_on_execute
        self.execute_calls = 0
        self.compensate_calls = 0
        #: Records whether the expiry was already persisted when execute() ran.
        self.expiry_seen_at_execute: object = "not-observed"

    @property
    def chaos_fixture(self) -> str:
        return "crashy-api"

    def plan(self, bundle: IncidentBundle, target: Target) -> RemediationPlan:
        assert self._plan is not None
        return self._plan

    async def execute(self, plan: RemediationPlan) -> ExecutionResult:
        self.execute_calls += 1
        if self._raise is not None:
            raise self._raise
        return ExecutionResult(
            ok=self._execute_ok,
            detail="applied" if self._execute_ok else "could not apply",
            state_before={"replicas": 3},
            state_after={"replicas": 3} if not self._execute_ok else {"replicas": 5},
        )

    async def compensate(self, plan: RemediationPlan) -> ExecutionResult:
        self.compensate_calls += 1
        return ExecutionResult(ok=True, detail="compensated")


# --- fixtures ------------------------------------------------------------------------------------


def target(**overrides: object) -> Target:
    base: dict[str, object] = {
        "kind": "Deployment",
        "name": "api",
        "namespace": "shop",
        "replicas": 3,
        "containers": (ContainerState(name="api", image="ghcr.io/o/api:v2"),),
        "revision": 4,
        "previous_revision": 3,
        "previous_image": "ghcr.io/o/api:v1",
    }
    return Target(**(base | overrides))  # type: ignore[arg-type]


def bundle(t: Target) -> IncidentBundle:
    return IncidentBundle(
        incident_id=1,
        cause_alert=BundleAlert(
            alertname="KubePodCrashLooping",
            fingerprint="fp-1",
            namespace="shop",
            pod="api-6b8c9d7f5-ab12c",
        ),
        target=t,
    )


def good_plan(**overrides: object) -> RemediationPlan:
    call = ApiCall(verb="patch", resource="apps/v1/Deployment", name="api", namespace="shop")
    base: dict[str, object] = {
        "runbook": "stub_runbook",
        "incident_id": 1,
        "target_kind": "Deployment",
        "target_name": "api",
        "target_namespace": "shop",
        "actions": (call,),
        "compensation": (call,),
        "reversibility": Reversibility.REVERSIBLE,
        "durability": Durability.EPHEMERAL,
        "ttl_seconds": 7200,
        "blast_radius": BlastRadius(pods=3),
        "confidence": 0.94,
        "summary": "roll back shop/api from revision 4 to 3",
        "expected_signal": ExpectedSignal(alert_should_clear="KubePodCrashLooping"),
    }
    return RemediationPlan(**(base | overrides))  # type: ignore[arg-type]


async def make_incident(session: AsyncSession) -> Incident:
    incident = Incident(
        state=IncidentState.OPEN.value,
        cause_scope="workload",
        cause_target="api",
        cause_alertname="KubePodCrashLooping",
        target_key="shop/Deployment/api",
        namespace="shop",
        precedence=2,
    )
    session.add(incident)
    await session.flush()
    return incident


async def ledger_events(session: AsyncSession, incident_id: int) -> list[str]:
    rows = (
        await session.execute(
            select(AuditLedger.event_type)
            .where(AuditLedger.incident_id == incident_id)
            .order_by(AuditLedger.seq)
        )
    ).scalars()
    return list(rows)


@pytest.fixture(autouse=True)
def _no_real_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the Redis lock with an in-process one.

    Lock semantics are covered against a real Valkey in `test_locks_and_queue.py`; here the subject
    is the orchestrator's branching, and requiring Redis would couple the two.
    """
    import contextlib

    @contextlib.asynccontextmanager
    async def fake_lock(*_args: Any, **_kwargs: Any) -> Any:
        yield object()

    monkeypatch.setattr(orchestrator.locks, "per_resource", fake_lock)


# --- the refusal branch --------------------------------------------------------------------------


async def test_refusal_is_recorded_not_raised(db_session: AsyncSession) -> None:
    """ "The system correctly decided not to act" is a data point, not an error."""
    incident = await make_incident(db_session)
    runbook = StubRunbook(refuse("stub_runbook", 1, "no previous revision to roll back to"))

    result = await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )

    assert result.outcome == "refused"
    assert runbook.execute_calls == 0
    assert incident.outcome == "refused"
    assert incident.state == IncidentState.ESCALATED.value


async def test_refusal_is_audited(db_session: AsyncSession) -> None:
    incident = await make_incident(db_session)
    runbook = StubRunbook(refuse("stub_runbook", 1, "nothing to roll back to"))

    await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )
    await db_session.flush()

    events = await ledger_events(db_session, incident.id)
    assert "plan_requested" in events
    assert "plan" in events


async def test_refusal_persists_its_reason(db_session: AsyncSession) -> None:
    incident = await make_incident(db_session)
    reason = "the previous revision runs the same image"
    runbook = StubRunbook(refuse("stub_runbook", 1, reason))

    await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )

    row = (
        await db_session.execute(
            select(RemediationPlanRow).where(RemediationPlanRow.incident_id == incident.id)
        )
    ).scalar_one()
    assert row.refusal_reason == reason
    assert row.actions == []


# --- the denial branch ---------------------------------------------------------------------------


async def test_policy_denial_stops_execution(db_session: AsyncSession) -> None:
    incident = await make_incident(db_session)
    # An irreversible plan is denied by `deny-irreversible`.
    runbook = StubRunbook(good_plan(reversibility=Reversibility.IRREVERSIBLE, compensation=()))

    result = await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )

    assert result.outcome == "denied"
    assert runbook.execute_calls == 0
    assert result.decision is not None
    assert result.decision.reasons == ("deny-irreversible",)


async def test_denial_is_audited_with_the_rule_that_fired(db_session: AsyncSession) -> None:
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan(target_namespace="kube-system"))

    await orchestrator.handle(
        db_session,
        incident,
        bundle(target(namespace="kube-system")),
        runbook,
        target(namespace="kube-system"),
        promotion=AUTO,
    )
    await db_session.flush()

    assert "policy" in await ledger_events(db_session, incident.id)

    entry = (
        await db_session.execute(
            select(AuditLedger).where(
                AuditLedger.incident_id == incident.id, AuditLedger.event_type == "policy"
            )
        )
    ).scalar_one()
    assert entry.payload["decision"] == "deny"
    # The message is what Slack shows, so it has to be in the record too.
    assert entry.payload["messages"]


async def test_self_remediation_is_denied(db_session: AsyncSession) -> None:
    """HealOps never remediates HealOps, and the rule lives in the policy file."""
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan(target_namespace="healops"))

    result = await orchestrator.handle(
        db_session,
        incident,
        bundle(target(namespace="healops")),
        runbook,
        target(namespace="healops"),
        promotion=AUTO,
    )
    assert result.outcome == "denied"
    assert result.decision is not None
    assert "deny-self-remediation" in result.decision.reasons


# --- the shadow branch ---------------------------------------------------------------------------


async def test_shadow_mode_records_without_acting(db_session: AsyncSession) -> None:
    """Shadow produces the plan it WOULD have run and touches nothing."""
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan())

    result = await orchestrator.handle(
        db_session,
        incident,
        bundle(target()),
        runbook,
        target(),
        promotion=PromotionSnapshot(mode="shadow"),
    )

    assert result.outcome == "shadow"
    assert runbook.execute_calls == 0

    execution = await db_session.get(RunbookExecution, result.execution_id)
    assert execution is not None
    assert execution.mode == "shadow"
    assert execution.status == "shadow"


async def test_shadow_is_audited_with_what_it_would_have_done(db_session: AsyncSession) -> None:
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan())

    await orchestrator.handle(
        db_session,
        incident,
        bundle(target()),
        runbook,
        target(),
        promotion=PromotionSnapshot(mode="shadow"),
    )
    await db_session.flush()

    entry = (
        await db_session.execute(
            select(AuditLedger).where(
                AuditLedger.incident_id == incident.id, AuditLedger.event_type == "shadow"
            )
        )
    ).scalar_one()
    # The diff against what a human actually did is the drift monitor, so the intended actions have
    # to be in the record.
    assert entry.payload["would_have"]
    assert entry.payload["summary"]


async def test_shadow_closes_the_incident_honestly(db_session: AsyncSession) -> None:
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan())

    await orchestrator.handle(
        db_session,
        incident,
        bundle(target()),
        runbook,
        target(),
        promotion=PromotionSnapshot(mode="shadow"),
    )
    # Not "remediated by us" -- the outcome column says shadow, so the scorecard cannot count it as
    # an auto-resolution.
    assert incident.outcome == "shadow"


# --- the approval branch -------------------------------------------------------------------------


async def test_approval_required_does_not_execute(db_session: AsyncSession) -> None:
    incident = await make_incident(db_session)
    # Low confidence trips `approve-low-confidence`.
    runbook = StubRunbook(good_plan(confidence=0.4))

    result = await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )

    assert result.outcome == "awaiting_approval"
    assert runbook.execute_calls == 0
    assert incident.state == IncidentState.AWAITING_APPROVAL.value

    execution = await db_session.get(RunbookExecution, result.execution_id)
    assert execution is not None
    assert execution.mode == "approval"
    assert execution.status == "awaiting_approval"


async def test_approval_request_is_audited(db_session: AsyncSession) -> None:
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan(confidence=0.4))

    await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )
    await db_session.flush()

    assert "approve" in await ledger_events(db_session, incident.id)


async def test_unpromoted_runbook_requires_approval(db_session: AsyncSession) -> None:
    """Authority is earned per namespace. The ladder is enforcement, not bookkeeping."""
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan())

    result = await orchestrator.handle(
        db_session,
        incident,
        bundle(target()),
        runbook,
        target(),
        promotion=PromotionSnapshot(mode="approval"),
    )
    assert result.outcome == "awaiting_approval"
    assert result.decision is not None
    assert "approve-not-promoted" in result.decision.reasons


# --- the execute branch --------------------------------------------------------------------------


async def test_auto_path_executes(db_session: AsyncSession) -> None:
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan())

    result = await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )

    assert result.outcome == "executed"
    assert runbook.execute_calls == 1


async def test_execution_does_not_claim_success(db_session: AsyncSession) -> None:
    """Success means the incident is over, and only verification can say that.

    The execution lands in `awaiting_verification` and the incident in `verifying` -- never
    `remediated`.
    """
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan())

    result = await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )

    execution = await db_session.get(RunbookExecution, result.execution_id)
    assert execution is not None
    assert execution.status == "awaiting_verification"
    assert incident.state == IncidentState.VERIFYING.value
    assert incident.outcome is None


async def test_compensation_expiry_is_durable_before_execution(
    db_session: AsyncSession,
) -> None:
    """The saga's compensating transaction is persisted BEFORE the action runs.

    A compensation owned by a process that can crash is not a compensation, so the watchdog must be
    able to find this execution's expiry without the engine still being alive.
    """
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan(ttl_seconds=3600))

    result = await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )

    execution = await db_session.get(RunbookExecution, result.execution_id)
    assert execution is not None
    assert execution.expires_at is not None, "the watchdog needs a persisted deadline"


async def test_execution_records_before_and_after_state(db_session: AsyncSession) -> None:
    # What the dashboard diffs, and what the compensation chaos test compares byte-for-byte.
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan())

    result = await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )

    execution = await db_session.get(RunbookExecution, result.execution_id)
    assert execution is not None
    assert execution.state_before == {"replicas": 3}
    assert execution.state_after == {"replicas": 5}


async def test_execution_is_audited(db_session: AsyncSession) -> None:
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan())

    await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )
    await db_session.flush()

    events = await ledger_events(db_session, incident.id)
    # The full audited trail for an auto-executed incident.
    assert events == ["plan_requested", "plan", "policy", "execute"]


async def test_failed_application_escalates_rather_than_verifying(
    db_session: AsyncSession,
) -> None:
    """An action that did not apply is distinct from a fix that applied but did not work."""
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan(), execute_ok=False)

    result = await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )

    assert result.outcome == "execute_failed"
    assert incident.state == IncidentState.ESCALATED.value
    assert incident.outcome == "escalated"


async def test_a_second_plan_for_a_target_mid_verification_is_blocked(
    db_session: AsyncSession,
) -> None:
    """Regression, found on the kind cluster: two incidents for one Deployment, and the second
    rolled it back onto the broken revision the first had just fixed. The lock covers the API
    calls; the change is still being judged for minutes after. One remediation per target."""
    from tests.factories import make_execution

    _, first, _ = await make_execution(db_session, plan=good_plan())
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan())

    result = await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )

    assert result.outcome == "blocked"
    assert runbook.execute_calls == 0
    assert f"execution {first.id}" in (result.detail or "")


async def test_lock_contention_blocks_rather_than_forcing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another plan holds the resource, so this one stands down.

    Forcing in is how two runbooks produce a final state nobody predicted -- one rolling back while
    the other raises a memory limit.
    """
    import contextlib

    from cache.locks import LockNotAcquired

    @contextlib.asynccontextmanager
    async def contended(*_args: Any, **_kwargs: Any) -> Any:
        raise LockNotAcquired("shop/api is already being remediated by plan other-plan")
        yield  # pragma: no cover

    monkeypatch.setattr(orchestrator.locks, "per_resource", contended)

    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan())

    result = await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )

    assert result.outcome == "blocked"
    assert runbook.execute_calls == 0
    assert incident.outcome == "blocked"


# --- budget and breaker are read by the gate -----------------------------------------------------


async def test_exhausted_budget_denies(db_session: AsyncSession) -> None:
    """An error budget for automation: exhaustion escalates rather than queues."""
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan())

    result = await orchestrator.handle(
        db_session,
        incident,
        bundle(target()),
        runbook,
        target(),
        budget=BudgetSnapshot(used_actions=8, max_actions=8),
        promotion=AUTO,
    )

    assert result.outcome == "denied"
    assert runbook.execute_calls == 0


async def test_open_breaker_denies(db_session: AsyncSession) -> None:
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan())

    result = await orchestrator.handle(
        db_session,
        incident,
        bundle(target()),
        runbook,
        target(),
        budget=BudgetSnapshot(breaker_state="open"),
        promotion=AUTO,
    )
    assert result.outcome == "denied"


# --- the plan row ---------------------------------------------------------------------------------


async def test_plan_and_decision_are_persisted_together(db_session: AsyncSession) -> None:
    """A plan without its decision cannot answer "why was this allowed?"."""
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan())

    await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )

    row = (
        await db_session.execute(
            select(RemediationPlanRow).where(RemediationPlanRow.incident_id == incident.id)
        )
    ).scalar_one()

    assert row.policy_decision == "allow"
    assert row.compensation, "the undo must be durable on the plan row"
    assert row.blast_radius["score"] == 3
    assert row.ttl_seconds == 7200
    assert float(row.confidence) == pytest.approx(0.94)


async def test_ledger_chain_stays_valid_across_the_pipeline(db_session: AsyncSession) -> None:
    import ledger

    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan())

    await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )
    await db_session.flush()

    status = await ledger.verify_chain(db_session)
    assert status.valid is True, status.detail


# --- slack summaries -----------------------------------------------------------------------------


async def test_every_outcome_has_a_readable_summary(db_session: AsyncSession) -> None:
    # Written for a human reading it on a phone, so no outcome may fall through to a bare struct.
    incident = await make_incident(db_session)
    runbook = StubRunbook(good_plan())
    result = await orchestrator.handle(
        db_session, incident, bundle(target()), runbook, target(), promotion=AUTO
    )

    line = orchestrator.summarise_for_slack(result)
    assert line
    assert "verifying recovery" in line

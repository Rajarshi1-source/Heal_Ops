"""The worker: queued alert -> plan -> gate -> act -> verify -> (compensate) -> notify.

These run the real orchestrator, policy file, verifier, compensator and ledger against PostgreSQL.
Only the edges are faked: the cluster (target builder, runbook), Prometheus and Alertmanager (the
`observe` function), Redis (the stream) and Slack (the notifier). That makes this the closest the
unit suite gets to the Phase 4 demo -- "a deliberately bad rollback is auto-reverted and escalated"
-- without a kind cluster.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import orchestrator
import worker
from cache.streams import QueuedJob
from core import approval_manager
from db.connection import db_now
from db.models import (
    Approval,
    Incident,
    IncidentState,
    RunbookExecution,
    RunbookPromotion,
    Verification,
)
from integrations.slack_client import PostedMessage
from plan import RemediationPlan
from runbooks.base import SignalWindow
from tests.factories import (
    ScriptedRunbook,
    bound_to,
    healthy_window,
    ledger_events,
    ledger_payloads,
    make_execution,
    make_incident,
    make_plan,
    make_target,
    no_lock,
    registry_of,
)

pytestmark = pytest.mark.db


class Recorder:
    def __init__(self, returns: Any = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.returns = returns

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.returns


class Observer:
    """Stands in for `verifier.observe`: returns scripted windows and records the ranges asked."""

    def __init__(self, *windows: SignalWindow) -> None:
        self.windows = list(windows) or [healthy_window()]
        self.ranges: list[str] = []

    async def __call__(self, plan: RemediationPlan, *, window: str = "5m") -> SignalWindow:
        self.ranges.append(window)
        return self.windows.pop(0) if len(self.windows) > 1 else self.windows[0]


@pytest.fixture
def wired(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Point the worker at the test session and replace every external edge with a recorder."""
    runbook = ScriptedRunbook(make_plan())
    enqueue = Recorder()
    orchestrated = Recorder()
    verified = Recorder()
    expired = Recorder()

    monkeypatch.setattr(worker, "transaction", bound_to(db_session))
    monkeypatch.setattr(worker, "session_scope", bound_to(db_session))
    monkeypatch.setattr(worker, "load_all", lambda: registry_of(runbook))
    monkeypatch.setattr(orchestrator.locks, "per_resource", no_lock)
    monkeypatch.setattr(worker.streams, "enqueue", enqueue)
    monkeypatch.setattr(worker.notifier, "orchestrated", orchestrated)
    monkeypatch.setattr(worker.notifier, "verified", verified)
    monkeypatch.setattr(worker.notifier, "approval_expired", expired)
    monkeypatch.setattr(approval_manager.approval_store, "invalidate", Recorder())

    return {
        "runbook": runbook,
        "enqueue": enqueue,
        "orchestrated": orchestrated,
        "verified": verified,
        "expired": expired,
    }


async def promote(session: AsyncSession, mode: str) -> None:
    session.add(RunbookPromotion(runbook="scripted_runbook", namespace="shop", mode=mode))
    await session.flush()


async def target_builder(_incident: Incident) -> Any:
    return make_target()


# --- planning ------------------------------------------------------------------------------------


async def test_auto_path_acts_and_wakes_the_verifier(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    await promote(db_session, "auto")
    incident = await make_incident(db_session)
    observer = Observer(healthy_window(alert_firing=True, new_alerts=("KubePodCrashLooping",)))

    result = await worker.plan_incident(
        incident.id, target_builder=target_builder, observe=observer
    )

    assert result is not None and result.outcome == "executed"
    assert wired["runbook"].execute_calls == 1
    assert incident.state == IncidentState.VERIFYING.value
    assert wired["enqueue"].calls == [(("verify", {"execution_id": result.execution_id}), {})]

    execution = await db_session.get(RunbookExecution, result.execution_id)
    assert execution is not None
    assert execution.signals_before["alert_firing"] is True, "the pre-action window is durable"
    assert execution.started_at is not None and execution.finished_at is not None


async def test_a_handled_incident_is_not_planned_twice(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    incident = await make_incident(db_session, state=IncidentState.VERIFYING)
    assert await worker.plan_incident(incident.id, target_builder=target_builder) is None
    assert wired["runbook"].execute_calls == 0


async def test_unpromoted_runbook_is_shadowed(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    """No promotion row means no authority yet. The plan is recorded, nothing is touched."""
    incident = await make_incident(db_session)

    result = await worker.plan_incident(incident.id, target_builder=target_builder)

    assert result is not None and result.outcome == "shadow"
    assert wired["runbook"].execute_calls == 0
    assert wired["enqueue"].calls == []


async def test_approval_path_creates_the_row_and_remembers_the_card(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    await promote(db_session, "approval")
    wired["orchestrated"].returns = PostedMessage(channel="C1", ts="1700.01")
    incident = await make_incident(db_session)

    result = await worker.plan_incident(incident.id, target_builder=target_builder)

    assert result is not None and result.outcome == "awaiting_approval"
    approval = (
        await db_session.execute(
            select(Approval).where(Approval.execution_id == result.execution_id)
        )
    ).scalar_one()
    assert approval.status == "pending"
    assert (approval.slack_channel, approval.slack_message_ts) == ("C1", "1700.01")
    # The card is rendered with the database's deadline, never one computed client-side.
    [(_args, kwargs)] = wired["orchestrated"].calls
    assert kwargs["expires_at"] == approval.expires_at.isoformat()


async def test_an_alert_no_runbook_answers_is_escalated_on_the_record(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    incident = await make_incident(db_session, cause_alertname="SomethingNobodyHandles")

    assert await worker.plan_incident(incident.id, target_builder=target_builder) is None

    assert incident.state == IncidentState.ESCALATED.value
    [entry] = await ledger_payloads(db_session, incident.id, "state_transition")
    assert entry["stage"] == "unplannable"
    assert "no runbook answers" in entry["reason"]


async def test_a_missing_target_is_escalated_not_retried(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    async def gone(_incident: Incident) -> Any:
        raise worker.UnplannableIncident("shop/Deployment/api not found")

    incident = await make_incident(db_session)
    assert await worker.plan_incident(incident.id, target_builder=gone) is None
    assert incident.state == IncidentState.ESCALATED.value


# --- approved executions --------------------------------------------------------------------------


async def awaiting_approval(session: AsyncSession) -> tuple[Incident, RunbookExecution]:
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


async def test_an_approved_execution_runs_after_rechecking_the_database(
    db_session: AsyncSession, wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approval_manager.approval_store, "mirror", Recorder())
    incident, execution = await awaiting_approval(db_session)
    await approval_manager.resolve(db_session, execution, approved=True, decided_by="U1")

    result = await worker.execute_approved_job(execution.id, observe=Observer())

    assert result is not None and result.outcome == "executed"
    assert wired["runbook"].execute_calls == 1
    assert incident.state == IncidentState.VERIFYING.value


async def test_a_queued_execution_without_a_valid_approval_is_refused(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    """Fail closed at the moment of acting: the message says someone clicked; the database decides.

    Here the execution was queued but the approval row is still pending -- the shape a forged or
    replayed message would have.
    """
    incident, execution = await awaiting_approval(db_session)
    execution.status = "queued"
    await db_session.flush()

    assert await worker.execute_approved_job(execution.id, observe=Observer()) is None

    assert wired["runbook"].execute_calls == 0
    assert incident.state == IncidentState.ESCALATED.value
    payloads = await ledger_payloads(db_session, incident.id, "approve")
    assert payloads[-1]["stage"] == "refused_at_execution"


async def test_an_execution_that_is_not_queued_is_left_alone(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    _, execution = await awaiting_approval(db_session)
    assert await worker.execute_approved_job(execution.id) is None
    assert wired["runbook"].execute_calls == 0


# --- verification ---------------------------------------------------------------------------------


async def test_a_recovered_target_is_remediated_after_the_full_window(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    incident, execution, _ = await make_execution(db_session, finished_ago_seconds=400)
    observer = Observer(healthy_window())

    assert await worker.verification_pass(observe=observer) == 1

    assert incident.state == IncidentState.REMEDIATED.value
    assert execution.status == "remediated"
    assert wired["runbook"].compensate_calls == 0
    # The post-action range excludes the settle period: 400 s elapsed, 30 s settle.
    assert observer.ranges == ["370s"]
    assert len(wired["verified"].calls) == 1


async def test_a_bad_rollback_is_reverted_before_the_window_closes(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    """The Phase 4 demo, in miniature: rolled back onto an equally broken revision.

    Ninety seconds in, the new pods are already crash-looping. Waiting out the remaining three and a
    half minutes would only prolong the outage, so the failure is called early and undone.
    """
    incident, execution, _ = await make_execution(db_session, finished_ago_seconds=90)

    await worker.verification_pass(observe=Observer(healthy_window(alert_firing=True, restarts=3)))

    assert wired["runbook"].compensate_calls == 1
    assert execution.status == "reverted"
    assert incident.state == IncidentState.REVERTED.value
    assert await ledger_events(db_session, incident.id) == ["verify", "compensate"]
    verification = (
        await db_session.execute(
            select(Verification).where(Verification.execution_id == execution.id)
        )
    ).scalar_one()
    assert verification.outcome == "not_recovered"
    assert verification.compensated is True


async def test_early_on_a_healthy_looking_target_keeps_watching(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    """Success is never called early -- `for:` durations lag relapse as much as recovery."""
    incident, execution, _ = await make_execution(db_session, finished_ago_seconds=90)

    assert await worker.verification_pass(observe=Observer(healthy_window())) == 0

    assert execution.status == "awaiting_verification"
    assert incident.state == IncidentState.VERIFYING.value


async def test_nothing_is_judged_inside_the_settle_period(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    await make_execution(db_session, finished_ago_seconds=20)
    observer = Observer()

    assert await worker.verification_pass(observe=observer) == 0
    assert observer.ranges == [], "no probe is spent on a window that cannot yet say anything"


async def test_unobservable_signals_revert_rather_than_pass(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    """A missing signal is never a healthy signal."""
    incident, execution, _ = await make_execution(db_session, finished_ago_seconds=400)

    await worker.verification_pass(observe=Observer(SignalWindow()))

    assert wired["runbook"].compensate_calls == 1
    assert incident.state == IncidentState.REVERTED.value


async def test_conclude_yields_to_whoever_settled_the_execution_first(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    from verifier import Verdict

    _, execution, plan = await make_execution(db_session)
    execution.status = "reverted"  # the watchdog got there first
    await db_session.flush()

    assert not await worker.conclude(execution.id, plan, Verdict(outcome="not_recovered"))
    assert wired["runbook"].compensate_calls == 0


# --- the reaper -----------------------------------------------------------------------------------


async def test_reap_pass_escalates_and_retires_the_card(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    incident, execution = await awaiting_approval(db_session)
    approval = (
        await db_session.execute(select(Approval).where(Approval.execution_id == execution.id))
    ).scalar_one()
    approval.expires_at = await db_now(db_session) - timedelta(seconds=1)
    approval.slack_channel, approval.slack_message_ts = "C1", "1700.02"
    await db_session.flush()

    assert await worker.reap_pass() == 1

    assert incident.state == IncidentState.ESCALATED.value
    [(_args, kwargs)] = wired["expired"].calls
    assert (kwargs["channel"], kwargs["message_ts"]) == ("C1", "1700.02")


# --- the loop machinery ---------------------------------------------------------------------------


class Elector:
    def __init__(self, leader: bool) -> None:
        self.is_leader = leader


def job(kind: str, payload: dict[str, Any], delivery_count: int = 1) -> QueuedJob:
    return QueuedJob(message_id="1-0", kind=kind, payload=payload, delivery_count=delivery_count)


@pytest.fixture
def stream(monkeypatch: pytest.MonkeyPatch) -> dict[str, Recorder]:
    recorders = {"ack": Recorder(), "to_dlq": Recorder(), "should_retry": Recorder(True)}
    for name, recorder in recorders.items():
        monkeypatch.setattr(worker.streams, name, recorder)
    return recorders


async def test_dispatch_acks_after_success(
    monkeypatch: pytest.MonkeyPatch, stream: dict[str, Recorder]
) -> None:
    planned = Recorder()
    monkeypatch.setattr(worker, "plan_incident", planned)

    await worker.Worker(Elector(True), consumer="c").dispatch(job("plan", {"incident_id": 7}))

    assert planned.calls == [((7,), {})]
    assert len(stream["ack"].calls) == 1


async def test_dispatch_leaves_a_failed_job_pending_while_retries_remain(
    monkeypatch: pytest.MonkeyPatch, stream: dict[str, Recorder]
) -> None:
    async def boom(*_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError("api server unavailable")

    monkeypatch.setattr(worker, "plan_incident", boom)

    await worker.Worker(Elector(True), consumer="c").dispatch(job("plan", {"incident_id": 7}))

    assert stream["ack"].calls == [] and stream["to_dlq"].calls == []


async def test_dispatch_dead_letters_and_escalates_when_retries_run_out(
    monkeypatch: pytest.MonkeyPatch, stream: dict[str, Recorder]
) -> None:
    async def boom(*_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError("api server unavailable")

    escalated = Recorder()
    monkeypatch.setattr(worker, "plan_incident", boom)
    monkeypatch.setattr(worker, "escalate_unplannable", escalated)
    stream["should_retry"].returns = False

    await worker.Worker(Elector(True), consumer="c").dispatch(job("plan", {"incident_id": 7}, 3))

    assert len(stream["to_dlq"].calls) == 1
    assert escalated.calls[0][0][0] == 7, "a dead-lettered incident is not left open forever"


async def test_dispatch_routes_approved_executions(
    monkeypatch: pytest.MonkeyPatch, stream: dict[str, Recorder]
) -> None:
    executed = Recorder()
    monkeypatch.setattr(worker, "execute_approved_job", executed)

    await worker.Worker(Elector(True), consumer="c").dispatch(
        job("execute_approved", {"execution_id": "42"})
    )

    assert executed.calls == [((42,), {})]


async def test_dispatch_dead_letters_an_unknown_job(stream: dict[str, Recorder]) -> None:
    await worker.Worker(Elector(True), consumer="c").dispatch(job("mystery", {}))
    assert len(stream["to_dlq"].calls) == 1
    assert stream["ack"].calls == []


async def test_dispatch_acks_verify_wakeups_without_acting(stream: dict[str, Recorder]) -> None:
    await worker.Worker(Elector(True), consumer="c").dispatch(job("verify", {"execution_id": 1}))
    assert len(stream["ack"].calls) == 1


async def test_verify_tick_runs_a_pass_when_woken(monkeypatch: pytest.MonkeyPatch) -> None:
    passes = Recorder(0)
    acked = Recorder()
    monkeypatch.setattr(worker, "verification_pass", passes)
    monkeypatch.setattr(worker.streams, "claim_stale", Recorder([]))
    monkeypatch.setattr(worker.streams, "read", Recorder([job("verify", {"execution_id": 1})]))
    monkeypatch.setattr(worker.streams, "ack", acked)

    loop = worker.Worker(Elector(True), consumer="c")
    loop._last_verification = asyncio.get_running_loop().time() + 10**9  # cadence not yet due
    await loop.verify_tick()

    assert len(passes.calls) == 1, "a fresh execution wakes the verifier without waiting"
    assert len(acked.calls) == 1


async def test_exec_tick_dispatches_reclaimed_and_new_jobs(
    monkeypatch: pytest.MonkeyPatch, stream: dict[str, Recorder]
) -> None:
    dispatched: list[str] = []
    loop = worker.Worker(Elector(True), consumer="c")

    async def record(j: QueuedJob) -> None:
        dispatched.append(j.kind)

    monkeypatch.setattr(loop, "dispatch", record)
    monkeypatch.setattr(worker.streams, "claim_stale", Recorder([job("plan", {"incident_id": 1})]))
    monkeypatch.setattr(worker.streams, "read", Recorder([job("verify", {})]))

    await loop.exec_tick()
    assert dispatched == ["plan", "verify"]


async def test_loops_idle_while_not_leading(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both replicas run the loops; only the Lease holder's loops do anything."""
    ticks = Recorder()
    loop = worker.Worker(Elector(False), consumer="c")
    monkeypatch.setattr(loop, "exec_tick", ticks)
    monkeypatch.setattr(loop, "verify_tick", ticks)
    monkeypatch.setattr(loop, "reap_tick", ticks)

    loop.start()
    await asyncio.sleep(0.05)
    await loop.stop()

    assert ticks.calls == []


async def test_a_failing_tick_does_not_kill_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    loop = worker.Worker(Elector(True), consumer="c")
    original_sleep = asyncio.sleep

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")

    async def fast_sleep(_seconds: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(worker.asyncio, "sleep", fast_sleep)
    task = asyncio.create_task(loop._run("test", flaky))
    while calls < 3:
        await original_sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert calls >= 3

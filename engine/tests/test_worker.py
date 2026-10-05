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
    requeued = Recorder()
    monkeypatch.setattr(worker.streams, "enqueue_batch", requeued)
    seen: set[str] = set()

    async def seen_recently(fingerprint: str, *_: Any, incident_id: Any = None, **__: Any) -> bool:
        key = f"{fingerprint}:{incident_id}"
        if key in seen:
            return True
        seen.add(key)
        return False

    monkeypatch.setattr(worker.dedup_store, "seen_recently", seen_recently)
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
        "requeued": requeued,
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


async def test_a_stranded_open_incident_gets_its_plan_job_back(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    """Regression, found on the kind cluster: an incident whose plan job never arrived sat `open`
    forever. The reaper re-enqueues it -- once per dedup window, not on every pass."""
    stranded = await make_incident(db_session)
    stranded.opened_at = await db_now(db_session) - timedelta(seconds=600)
    fresh = await make_incident(db_session)  # just opened: its job is presumably still queued
    await db_session.flush()

    assert await worker.requeue_orphans() == 1
    [((jobs,), _)] = wired["requeued"].calls
    assert jobs == [("plan", {"incident_id": stranded.id, "requeued": True})]
    assert fresh.id not in [j[1]["incident_id"] for j in jobs]

    assert await worker.requeue_orphans() == 0  # deduplicated inside the window


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


# --- bounded automation, end to end ---------------------------------------------------------------


async def test_an_exhausted_budget_is_denied_at_the_gate(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    import budget

    await promote(db_session, "auto")
    for _ in range(8):  # ns:shop allows 8 per rolling hour
        await budget.consume(db_session, "shop")
    incident = await make_incident(db_session)

    result = await worker.plan_incident(incident.id, target_builder=target_builder)

    assert result is not None and result.outcome == "denied"
    assert result.decision is not None and "deny-budget-exhausted" in result.decision.reasons
    assert wired["runbook"].execute_calls == 0


async def test_an_open_breaker_blocks_an_approved_execution_at_the_moment_of_acting(
    db_session: AsyncSession, wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The human approved ten minutes ago; the breaker tripped since. It must not act."""
    import breaker

    monkeypatch.setattr(approval_manager.approval_store, "mirror", Recorder())
    incident, execution = await awaiting_approval(db_session)
    await approval_manager.resolve(db_session, execution, approved=True, decided_by="U1")
    await breaker.trip(db_session, "4 compensations in 15 minutes")

    result = await worker.execute_approved_job(execution.id, observe=Observer())

    assert result is not None and result.outcome == "blocked"
    assert "breaker open" in result.detail
    assert wired["runbook"].execute_calls == 0
    assert incident.state == IncidentState.ESCALATED.value


async def test_a_half_open_trial_is_settled_by_its_verdict(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    import breaker
    from db.models import RemediationBudget

    _, execution, _ = await make_execution(db_session, finished_ago_seconds=400)
    await breaker.trip(db_session, "testing")
    row = await db_session.get(RemediationBudget, "global")
    assert row is not None
    row.breaker_until = await db_now(db_session) - timedelta(seconds=1)
    await db_session.flush()
    assert execution.plan_id is not None
    assert (await breaker.admit(db_session, execution.plan_id))[0]

    await worker.verification_pass(observe=Observer(healthy_window()))

    assert (await breaker.view(db_session)).state == "closed"


async def test_the_fourth_fix_files_the_masking_issue(
    db_session: AsyncSession, wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from integrations import github_client

    filed: list[tuple[str, list[str]]] = []

    async def create_issue(title: str, body: str, labels: list[str]) -> str:
        filed.append((title, labels))
        return "https://github.com/o/r/issues/9"

    masked = Recorder()
    monkeypatch.setattr(github_client, "create_issue", create_issue)
    monkeypatch.setattr(worker.notifier, "masking", masked)
    await promote(db_session, "auto")

    results = []
    for _ in range(4):
        incident = await make_incident(db_session)
        result = await worker.plan_incident(
            incident.id, target_builder=target_builder, observe=Observer()
        )
        results.append(result)
        # Each fix is judged before the same target breaks again; with one still in flight the
        # next would be blocked, by design, and never reach the recurrence count at all.
        if result and result.execution_id is not None:
            done = await db_session.get(RunbookExecution, result.execution_id)
            assert done is not None
            done.status = "remediated"
            await db_session.flush()

    assert [r.masking is not None for r in results if r] == [False, False, False, True]
    assert filed and "needs-root-cause" in filed[0][1]
    assert masked.calls[0][0][1] == "https://github.com/o/r/issues/9"
    promotion = await db_session.get(RunbookPromotion, ("scripted_runbook", "shop"))
    assert promotion is not None and promotion.mode == "approval", "it stops fixing unattended"


async def test_oom_targets_are_sized_from_the_observed_peak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bundle import ContainerState
    from integrations import prometheus_client
    from k8s import state_capture

    async def deployment(namespace: str, name: str) -> Any:
        return make_target(containers=(ContainerState(name="worker", image="i"),))

    async def peak(namespace: str, workload: str, container: str) -> float:
        return 640.0

    monkeypatch.setattr(state_capture, "build_target_for_deployment", deployment)
    monkeypatch.setattr(prometheus_client, "memory_peak_mib", peak)
    oom = Incident(
        cause_scope="workload",
        cause_target="api",
        namespace="shop",
        cause_alertname="KubePodOOMKilled",
        target_key="shop/Deployment/api",
    )

    target = await worker.build_target(oom)
    assert target.containers[0].memory_peak_mib == 640.0

    other = Incident(
        cause_scope="workload",
        cause_target="api",
        namespace="shop",
        cause_alertname="KubePodCrashLooping",
        target_key="shop/Deployment/api",
    )
    assert (await worker.build_target(other)).containers[0].memory_peak_mib is None


# --- Phase 6: the ladder and the durable PR, driven by verdicts -----------------------------------


async def test_a_verified_auto_fix_climbs_the_ladder_and_opens_its_pr(
    db_session: AsyncSession, wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import durable
    import promotion

    opened = Recorder(durable.PrResult("https://github.com/o/m/pull/1", "opened"))
    monkeypatch.setattr(worker.durable, "open_for", opened)
    monkeypatch.setattr(worker.notifier, "durable_fix", Recorder())
    monkeypatch.setattr(worker.notifier, "promoted", Recorder())
    await promotion.set_mode(db_session, "scripted_runbook", "shop", "auto", by="t", reason="t")

    _, execution, _ = await make_execution(
        db_session, plan=make_plan(gitops_managed=True), finished_ago_seconds=400
    )
    await worker.verification_pass(observe=Observer(healthy_window()))

    row = await db_session.get(RunbookPromotion, ("scripted_runbook", "shop"))
    assert row is not None and row.consecutive_good == 1
    assert opened.calls == [((execution.id,), {})], "stage 2, only after stage 1 verified"


async def test_a_reverted_fix_demotes_and_opens_no_pr(
    db_session: AsyncSession, wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import promotion

    opened = Recorder()
    promoted = Recorder()
    monkeypatch.setattr(worker.durable, "open_for", opened)
    monkeypatch.setattr(worker.notifier, "promoted", promoted)
    await promotion.set_mode(db_session, "scripted_runbook", "shop", "auto", by="t", reason="t")

    await make_execution(db_session, plan=make_plan(gitops_managed=True), finished_ago_seconds=90)
    await worker.verification_pass(observe=Observer(healthy_window(restarts=3)))

    row = await db_session.get(RunbookPromotion, ("scripted_runbook", "shop"))
    assert row is not None and row.mode == "approval", "one strike"
    assert opened.calls == [], "a PR for a change that did not work is worse than none"
    assert promoted.calls and promoted.calls[0][0][0].to_mode == "approval"


async def test_an_unmanaged_fix_opens_no_pr(
    db_session: AsyncSession, wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = Recorder()
    monkeypatch.setattr(worker.durable, "open_for", opened)
    await make_execution(db_session, finished_ago_seconds=400)
    await worker.verification_pass(observe=Observer(healthy_window()))
    assert opened.calls == []


async def test_a_failed_gitops_pause_blocks_the_action(
    db_session: AsyncSession, wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change the controller would revert in three minutes is not a mitigation."""
    import gitops

    async def fail(*_args: Any, **_kwargs: Any) -> None:
        raise gitops.GitOpsPauseFailed("Application shop-app not found")

    monkeypatch.setattr(orchestrator.gitops, "pause", fail)
    await promote(db_session, "auto")
    incident = await make_incident(db_session)

    async def managed(_incident: Incident) -> Any:
        return make_target(labels={"argocd.argoproj.io/instance": "shop-app"})

    result = await worker.plan_incident(incident.id, target_builder=managed, observe=Observer())

    assert result is not None and result.outcome == "blocked"
    assert "shop-app" in result.detail
    assert wired["runbook"].execute_calls == 0


async def test_shadow_plans_are_counted_on_the_ladder(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    incident = await make_incident(db_session)
    await worker.plan_incident(incident.id, target_builder=target_builder, observe=Observer())
    row = await db_session.get(RunbookPromotion, ("scripted_runbook", "shop"))
    assert row is not None and row.mode == "shadow" and row.shadow_plans == 1


async def test_an_approved_fix_on_a_managed_workload_still_pauses_the_controller(
    db_session: AsyncSession, wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: rehydrating the plan dropped `gitops_managed`, so an approved fix on an Argo CD
    workload ran without pausing the controller -- which would revert it within one sync."""
    paused: list[int] = []

    async def pause(session: Any, execution: Any, plan: Any) -> None:
        assert plan.gitops_managed is True
        paused.append(execution.id)

    monkeypatch.setattr(orchestrator.gitops, "pause", pause)
    monkeypatch.setattr(approval_manager.approval_store, "mirror", Recorder())
    _, execution, _ = await make_execution(
        db_session,
        plan=make_plan(gitops_managed=True),
        incident_state=IncidentState.AWAITING_APPROVAL,
        status="awaiting_approval",
        mode="approval",
        finished_ago_seconds=None,
        decision="require_approval",
    )
    await approval_manager.request(db_session, execution)
    await approval_manager.resolve(db_session, execution, approved=True, decided_by="U1")

    result = await worker.execute_approved_job(execution.id, observe=Observer())

    assert result is not None and result.outcome == "executed"
    assert paused == [execution.id]


async def test_the_hourly_chain_check_exports_its_verdict(
    db_session: AsyncSession, wired: dict[str, Any]
) -> None:
    """Regression: an alert on healops_ledger_valid == 0 existed; nothing ever set the gauge."""
    import ledger
    import metrics

    await ledger.append(db_session, "freeze", {"reason": "t"})
    status = await worker.verify_ledger()

    assert status.valid
    assert metrics.ledger_valid._value.get() == 1.0
    assert metrics.ledger_entries._value.get() == status.entries
    assert metrics.ledger_checked._value.get() > 0


def test_an_unchecked_chain_is_not_reported_broken() -> None:
    """Regression: the gauge started at 0, so every follower replica paged "chain broken".

    A fresh interpreter, because the gauge's value at import is the property under test and this
    process has long since imported it.
    """
    import subprocess
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    probe = (
        "import math, metrics; "
        "raise SystemExit(0 if math.isnan(metrics.ledger_valid._value.get()) else 1)"
    )
    run = subprocess.run([sys.executable, "-c", probe], cwd=src, check=False)  # noqa: S603
    assert run.returncode == 0

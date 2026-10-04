"""The worker: the loops that turn queued alerts into verified outcomes.

Three loops, and they are separate tasks on purpose:

    exec      reads the `exec` consumer group: plans incidents, runs approved executions
    verify    reads the `verify` consumer group, then judges every execution in its window
    reaper    every 30 s, expires approvals nobody answered -- and escalates them

The split between `exec` and `verify` is a **bulkhead**. Verification waits out a five-minute window
per execution; if it shared a loop with planning, a backlog of verifications would stop the engine
responding to new incidents, which is the failure mode where the system goes quiet precisely because
it is busy.

**The stream wakes the verifier; the database schedules it.** An execution's verification is due at
`finished_at + window`, a timestamp in PostgreSQL. The `verify` message only shortens the wait. A
schedule held in Redis, or in this process's memory, would vanish with either -- and the execution
would sit unverified until the watchdog force-reverted it.

Every loop acts only while this replica holds the Lease. Both replicas serve webhooks and enqueue;
only one plans, executes, verifies or reaps. Leadership and webhook availability are deliberately
decoupled.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import structlog
from kubernetes.client.rest import ApiException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import compensator
import ledger
import notifier
import orchestrator
import plan_store
import verifier
from bundle import BundleAlert, IncidentBundle, Target
from cache import streams
from cache.streams import QueuedJob
from config import get_settings
from core import approval_manager
from core.incident_state import assert_transition
from db.connection import db_now, session_scope, transaction
from db.models import (
    AlertEvent,
    Approval,
    Incident,
    IncidentState,
    RemediationPlanRow,
    RunbookExecution,
    RunbookPromotion,
)
from orchestrator import HandleResult
from plan import RemediationPlan
from policy import BudgetSnapshot, PromotionSnapshot
from runbooks import load_all
from runbooks.base import SignalWindow

log = structlog.get_logger(__name__)

TargetBuilder = Callable[[Incident], Awaitable[Target]]
Observe = Callable[..., Awaitable[SignalWindow]]


class Elector(Protocol):
    @property
    def is_leader(self) -> bool: ...


class UnplannableIncident(Exception):
    """Nothing to plan against. Escalated rather than retried: retrying will not create a target."""


# --- planning -------------------------------------------------------------------------------------


async def build_target(incident: Incident) -> Target:
    """Every cluster read the planner needs, once, before planning."""
    from k8s import state_capture

    try:
        if incident.cause_scope == "node":
            return await state_capture.build_target_for_node(incident.cause_target)
        if not incident.namespace:
            raise UnplannableIncident(f"{incident.target_key} has no namespace to look it up in")
        return await state_capture.build_target_for_deployment(
            incident.namespace, incident.cause_target
        )
    except ApiException as exc:
        if exc.status == 404:
            # The workload name is guessed from the pod name; a bare pod, or a guess that was
            # wrong, has no Deployment behind it. Nothing to act on, so a human takes it.
            raise UnplannableIncident(
                f"{incident.target_key} not found in the cluster ({exc.reason})"
            ) from exc
        raise


async def plan_incident(
    incident_id: int,
    *,
    target_builder: TargetBuilder = build_target,
    observe: Observe = verifier.observe,
) -> HandleResult | None:
    """Plan, gate and -- if policy allows -- act on one incident.

    Idempotent: an incident that has left `open` was already handled, by an earlier delivery of
    this job or a correlated cause alert, and the job is acknowledged without doing anything.
    """
    async with session_scope() as session:
        incident = await session.get(Incident, incident_id)
        if incident is None or incident.state != IncidentState.OPEN.value:
            return None

    runbook = load_all().for_alert(incident.cause_alertname)
    if runbook is None:
        # An unrecognised alert escalates rather than being force-fitted to a runbook.
        await escalate_unplannable(incident_id, f"no runbook answers {incident.cause_alertname}")
        return None

    # Cluster reads happen before the transaction, so no row lock is held across the API server.
    try:
        target = await target_builder(incident)
    except UnplannableIncident as exc:
        await escalate_unplannable(incident_id, str(exc))
        return None

    expires_at: str | None = None
    async with transaction() as session:
        incident = await session.get(Incident, incident_id, with_for_update=True)
        if incident is None or incident.state != IncidentState.OPEN.value:
            return None

        result = await orchestrator.handle(
            session,
            incident,
            await _bundle(session, incident, target),
            runbook,
            target,
            # The token-bucket budget and breaker arrive in Phase 5; until then the gate sees a
            # closed breaker and an empty bucket, and every other rule still applies.
            budget=BudgetSnapshot(),
            promotion=await _promotion(session, runbook.name, target.namespace),
            observe_before=observe,
        )
        if result.outcome == "awaiting_approval" and result.execution_id is not None:
            approval = await _approval(session, result.execution_id)
            expires_at = approval.expires_at.isoformat() if approval else None

    await _after_commit(result, expires_at=expires_at)
    return result


async def execute_approved_job(
    execution_id: int, *, observe: Observe = verifier.observe
) -> HandleResult | None:
    """Run an execution a human approved -- after checking, again, that they did.

    `can_execute()` is consulted here, at the moment of acting, and not trusted from the message.
    The message only says "someone clicked"; the database says whether that click is still valid.
    """
    registry = load_all()
    async with transaction() as session:
        execution = await session.get(RunbookExecution, execution_id, with_for_update=True)
        if execution is None or execution.status != "queued" or execution.mode != "approval":
            return None  # already run, or never approved: nothing for this delivery to do

        authorisation = await approval_manager.can_execute(session, execution_id)
        plan_row = (
            await session.get(RemediationPlanRow, execution.plan_id) if execution.plan_id else None
        )
        runbook = registry.get(plan_row.runbook) if plan_row is not None else None
        incident = await session.get(Incident, execution.incident_id)
        if incident is None:
            return None

        if not authorisation or plan_row is None or runbook is None:
            reason = (
                authorisation.reason
                if not authorisation
                else "the approved plan or its runbook is no longer available"
            )
            await ledger.append(
                session,
                "approve",
                {"stage": "refused_at_execution", "execution_id": execution_id, "detail": reason},
                incident_id=incident.id,
                execution_id=execution_id,
            )
            execution.status = approval_manager.APPROVAL_EXPIRED
            if incident.state == IncidentState.AWAITING_APPROVAL.value:
                incident.state = assert_transition(
                    incident.id, incident.state, IncidentState.ESCALATED
                ).value
                incident.outcome = "escalated"
            log.warning("approved execution refused at execution time", reason=reason)
            return None

        result = await orchestrator.execute_approved(
            session,
            incident,
            plan_store.rehydrate(plan_row),
            runbook,
            execution,
            plan_row.id,
            plan_store.decision_of(plan_row),
            observe_before=observe,
        )

    await _after_commit(result)
    return result


async def escalate_unplannable(incident_id: int, reason: str) -> None:
    """Hand an incident to a human, on the record. A silent drop would look like a healthy queue."""
    async with transaction() as session:
        incident = await session.get(Incident, incident_id, with_for_update=True)
        if incident is None or incident.state != IncidentState.OPEN.value:
            return
        await ledger.append(
            session,
            "state_transition",
            {"stage": "unplannable", "from": "open", "to": "escalated", "reason": reason},
            incident_id=incident_id,
        )
        incident.state = assert_transition(
            incident.id, incident.state, IncidentState.ESCALATED
        ).value
        incident.outcome = "escalated"
    log.info("incident escalated without a plan", incident_id=incident_id, reason=reason)


async def _after_commit(result: HandleResult, *, expires_at: str | None = None) -> None:
    posted = await notifier.orchestrated(result, expires_at=expires_at)
    if posted is not None and result.execution_id is not None:
        # Remember the card, so a decision or an expiry can retire its buttons in place.
        async with transaction() as session:
            approval = await _approval(session, result.execution_id)
            if approval is not None:
                approval.slack_message_ts = posted.ts
                approval.slack_channel = posted.channel
    if result.outcome == "executed" and result.execution_id is not None:
        await streams.enqueue("verify", {"execution_id": result.execution_id})


async def _bundle(session: AsyncSession, incident: Incident, target: Target) -> IncidentBundle:
    """The planner's whole view of the incident: its alerts and the target, nothing else."""
    events = list(
        (
            await session.execute(
                select(AlertEvent)
                .where(AlertEvent.incident_id == incident.id, AlertEvent.status == "firing")
                .order_by(AlertEvent.received_at)
            )
        ).scalars()
    )
    causes = [e for e in events if e.role == "cause" and e.alertname == incident.cause_alertname]
    cause = causes[-1] if causes else None
    symptoms = tuple(_bundle_alert(e) for e in events if e.role == "symptom")

    return IncidentBundle(
        incident_id=str(incident.id),
        cause_alert=(
            _bundle_alert(cause)
            if cause is not None
            else BundleAlert(
                alertname=incident.cause_alertname,
                fingerprint="",
                namespace=incident.namespace,
            )
        ),
        target=target,
        symptom_alerts=symptoms,
        cause_scope=incident.cause_scope,  # type: ignore[arg-type]
        correlation_reason=incident.correlation_reason,
        opened_at=incident.opened_at.isoformat() if incident.opened_at else None,
    )


def _bundle_alert(event: AlertEvent) -> BundleAlert:
    labels = {str(k): str(v) for k, v in (event.labels or {}).items()}
    return BundleAlert(
        alertname=event.alertname,
        fingerprint=event.fingerprint,
        status="firing",
        severity=event.severity or "warning",
        namespace=event.namespace,
        pod=event.pod_name,
        container=labels.get("container"),
        node=event.node_name,
        starts_at=event.starts_at.isoformat() if event.starts_at else None,
        labels=labels,
        annotations={str(k): str(v) for k, v in (event.annotations or {}).items()},
    )


async def _promotion(
    session: AsyncSession, runbook: str, namespace: str | None
) -> PromotionSnapshot:
    """The (runbook, namespace) rung. No row means no authority yet: shadow."""
    row = await session.get(RunbookPromotion, (runbook, namespace or "-"))
    if row is None:
        return PromotionSnapshot()
    return PromotionSnapshot(mode=row.mode, consecutive_good=row.consecutive_good)


async def _approval(session: AsyncSession, execution_id: int) -> Approval | None:
    return (
        await session.execute(select(Approval).where(Approval.execution_id == execution_id))
    ).scalar_one_or_none()


# --- verification ---------------------------------------------------------------------------------


async def verification_pass(*, observe: Observe = verifier.observe) -> int:
    """Judge every execution whose window allows a verdict. Returns how many were concluded.

    Signals are read outside any transaction; the verdict is written inside one that re-locks the
    execution with `SKIP LOCKED` and re-checks its status, because the watchdog may have reached it
    first. Whoever gets the row lock owns the outcome.
    """
    settings = get_settings()
    registry = load_all()

    async with session_scope() as session:
        now = await db_now(session)
        rows = (
            await session.execute(
                select(RunbookExecution, RemediationPlanRow)
                .join(RemediationPlanRow, RemediationPlanRow.id == RunbookExecution.plan_id)
                .where(
                    RunbookExecution.status == "awaiting_verification",
                    RunbookExecution.compensated.is_(False),
                    RunbookExecution.finished_at.isnot(None),
                )
                .order_by(RunbookExecution.finished_at)
                .limit(50)
            )
        ).all()
        due = [
            (execution.id, execution.finished_at, execution.signals_before, plan_row)
            for execution, plan_row in rows
        ]

    concluded = 0
    for execution_id, finished_at, signals_before, plan_row in due:
        elapsed = (now - finished_at).total_seconds()
        if elapsed < settings.verification_early_after_seconds:
            continue

        runbook = registry.get(plan_row.runbook)
        if runbook is None:
            # Cannot judge with logic that no longer exists; the watchdog escalates it at the
            # deadline rather than this loop guessing.
            continue

        plan = plan_store.rehydrate(plan_row)
        after = await observe(plan, window=verifier.post_action_window(elapsed))
        extra = await runbook.extra_verification(plan, after)
        before = SignalWindow.from_dict(signals_before)

        if elapsed >= verifier.window_seconds(plan):
            verdict: verifier.Verdict | None = verifier.judge(
                plan, before, after, extra_failure=extra
            )
        else:
            verdict = verifier.judge_early(plan, before, after, extra_failure=extra)
        if verdict is None:
            continue

        if await conclude(execution_id, plan, verdict):
            concluded += 1
    return concluded


async def conclude(execution_id: int, plan: RemediationPlan, verdict: verifier.Verdict) -> bool:
    """Record the verdict and let the compensator act on it, in one transaction."""
    runbook = load_all().get(plan.runbook)
    if runbook is None:
        return False

    async with transaction() as session:
        execution = (
            await session.execute(
                select(RunbookExecution)
                .where(
                    RunbookExecution.id == execution_id,
                    RunbookExecution.status == "awaiting_verification",
                )
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if execution is None:
            return False  # the watchdog, or another pass, got here first
        incident = await session.get(Incident, execution.incident_id)
        if incident is None:
            return False

        await verifier.record(session, execution, plan, verdict)
        outcome = await compensator.on_verification(
            session, incident, execution, plan, runbook, verdict
        )

    await notifier.verified(plan, verdict, outcome, execution_id=execution_id)
    return True


# --- approvals ------------------------------------------------------------------------------------


async def reap_pass() -> int:
    """Expire unanswered approvals, escalate their incidents, and retire their Slack cards."""
    cards: list[tuple[RemediationPlan, str | None, str | None]] = []
    async with transaction() as session:
        for approval in await approval_manager.reap_expired(session):
            execution = await session.get(RunbookExecution, approval.execution_id)
            if execution is None or execution.plan_id is None:
                continue
            plan_row = await session.get(RemediationPlanRow, execution.plan_id)
            if plan_row is not None:
                cards.append(
                    (
                        plan_store.rehydrate(plan_row),
                        approval.slack_channel,
                        approval.slack_message_ts,
                    )
                )

    for plan, channel, ts in cards:
        await notifier.approval_expired(plan, channel=channel, message_ts=ts)
    return len(cards)


# --- the loops ------------------------------------------------------------------------------------


class Worker:
    """Owns the three loops. Started from the FastAPI lifespan; acts only while leading."""

    def __init__(self, elector: Elector, *, consumer: str) -> None:
        self.elector = elector
        self.consumer = consumer
        self._tasks: list[asyncio.Task[None]] = []
        self._last_verification = 0.0

    def start(self) -> None:
        reap_every = get_settings().approval_reaper_interval_seconds
        self._tasks = [
            asyncio.create_task(self._run("exec", self.exec_tick), name="healops-exec"),
            asyncio.create_task(self._run("verify", self.verify_tick), name="healops-verify"),
            asyncio.create_task(
                self._run("reaper", self.reap_tick, pause=reap_every),
                name="healops-reaper",
            ),
        ]
        log.info("worker loops started", consumer=self.consumer)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _run(
        self, name: str, tick: Callable[[], Awaitable[Any]], *, pause: float = 0.0
    ) -> None:
        while True:
            if not self.elector.is_leader:
                await asyncio.sleep(1.0)
                continue
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # A loop that dies on one bad tick stops remediating silently. Log, back off, go on.
                log.error("worker loop tick failed", loop=name, error=str(exc))
                await asyncio.sleep(5.0)
                continue
            # Always yield, even with no pause: a tick that happened not to await anything would
            # otherwise spin this loop without ever letting the event loop serve a request.
            await asyncio.sleep(pause)

    async def exec_tick(self) -> None:
        group = get_settings().consumer_group_exec
        jobs = await streams.claim_stale(group, self.consumer) + await streams.read(
            group, self.consumer, count=10, block_ms=2000
        )
        for job in jobs:
            await self.dispatch(job)

    async def dispatch(self, job: QueuedJob) -> None:
        """Run one exec job. Ack only after its transaction committed; DLQ once retries run out."""
        group = get_settings().consumer_group_exec
        try:
            match job.kind:
                case "plan" if job.incident_id is not None:
                    await plan_incident(job.incident_id)
                case "execute_approved" if "execution_id" in job.payload:
                    await execute_approved_job(int(job.payload["execution_id"]))
                case "verify":
                    pass  # wake-ups for the verify group; every group sees every message
                case _:
                    await streams.to_dlq(group, job, f"unrecognised job {job.kind!r}")
                    return
        except Exception as exc:  # noqa: BLE001
            if await streams.should_retry(job):
                # Left pending, not acked: `claim_stale` redelivers it once it has sat idle.
                log.warning("job failed; will retry", kind=job.kind, error=str(exc))
                return
            await streams.to_dlq(group, job, str(exc))
            log.error("job dead-lettered", kind=job.kind, error=str(exc))
            if job.kind == "plan" and job.incident_id is not None:
                await escalate_unplannable(job.incident_id, f"planning kept failing: {exc}")
            return
        await streams.ack(group, job.message_id)

    async def verify_tick(self) -> None:
        settings = get_settings()
        group = settings.consumer_group_verify
        poll = settings.verification_poll_seconds
        # Blocking on the group is this loop's sleep: a fresh execution wakes it early.
        jobs = await streams.claim_stale(
            group, self.consumer, min_idle_ms=settings.verify_reclaim_idle_seconds * 1000
        ) + await streams.read(group, self.consumer, count=100, block_ms=poll * 1000)
        for job in jobs:
            await streams.ack(group, job.message_id)

        # Every group sees every message, so an alert storm would otherwise drive a pass per
        # batch. Passes stay on the poll cadence however busy the stream is.
        woken = any(job.kind == "verify" for job in jobs)
        if woken or time.monotonic() - self._last_verification >= poll:
            self._last_verification = time.monotonic()
            await verification_pass()

    async def reap_tick(self) -> None:
        await reap_pass()

"""Shared builders for the Phase 4 suites: plans, incidents, executions, and a scriptable runbook.

Kept out of `conftest.py` because these are plain functions, not fixtures -- a test reads more
clearly when it calls `make_execution(...)` with the fields it cares about than when the setup is
hidden in fixture parameters.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import orchestrator
from bundle import IncidentBundle, Target
from db.models import (
    AuditLedger,
    Incident,
    IncidentState,
    RunbookExecution,
)
from plan import (
    ApiCall,
    BlastRadius,
    Durability,
    ExpectedSignal,
    RemediationPlan,
    Reversibility,
)
from policy import PolicyDecision
from runbooks.base import ExecutionResult, Runbook, RunbookRegistry, SignalWindow


class ScriptedRunbook(Runbook):
    """A runbook whose plan and whose execute/compensate outcomes the test dictates."""

    name = "scripted_runbook"
    triggers = ("KubePodCrashLooping",)

    def __init__(
        self,
        plan_to_return: RemediationPlan | None = None,
        *,
        execute_ok: bool = True,
        compensate_ok: bool = True,
        compensate_raises: Exception | None = None,
        extra_failure: str | None = None,
    ) -> None:
        self._plan = plan_to_return
        self.execute_ok = execute_ok
        self.compensate_ok = compensate_ok
        self.compensate_raises = compensate_raises
        self.extra_failure = extra_failure
        self.execute_calls = 0
        self.compensate_calls = 0

    @property
    def chaos_fixture(self) -> str:
        return "crashy-api"

    def plan(self, bundle: IncidentBundle, target: Target) -> RemediationPlan:
        if self._plan is None:
            raise AssertionError("ScriptedRunbook.plan called without a scripted plan")
        return self._plan

    async def execute(self, plan: RemediationPlan) -> ExecutionResult:
        self.execute_calls += 1
        return ExecutionResult(
            ok=self.execute_ok,
            detail="applied" if self.execute_ok else "could not apply",
            state_before={"revision": 4},
            state_after={"revision": 3} if self.execute_ok else {"revision": 4},
        )

    async def compensate(self, plan: RemediationPlan) -> ExecutionResult:
        self.compensate_calls += 1
        if self.compensate_raises is not None:
            raise self.compensate_raises
        return ExecutionResult(
            ok=self.compensate_ok,
            detail="rolled forward to revision 4" if self.compensate_ok else "revision 4 is gone",
            state_before={"revision": 3},
            state_after={"revision": 4} if self.compensate_ok else {"revision": 3},
        )

    async def extra_verification(self, plan: RemediationPlan, after: SignalWindow) -> str | None:
        return self.extra_failure


def registry_of(*runbooks: Runbook) -> RunbookRegistry:
    registry = RunbookRegistry()
    for runbook in runbooks:
        registry.register(runbook)
    return registry


def make_plan(**overrides: Any) -> RemediationPlan:
    call = ApiCall(
        verb="patch",
        resource="apps/v1/Deployment",
        name="api",
        namespace="shop",
        body={"to_revision": 3},
    )
    undo = ApiCall(
        verb="patch",
        resource="apps/v1/Deployment",
        name="api",
        namespace="shop",
        body={"to_revision": 4},
    )
    base: dict[str, Any] = {
        "runbook": "scripted_runbook",
        "incident_id": 1,
        "target_kind": "Deployment",
        "target_name": "api",
        "target_namespace": "shop",
        "actions": (call,),
        "compensation": (undo,),
        "reversibility": Reversibility.REVERSIBLE,
        "durability": Durability.EPHEMERAL,
        "ttl_seconds": 7200,
        "blast_radius": BlastRadius(pods=3, namespaces=("shop",)),
        "confidence": 0.94,
        "summary": "roll back shop/api from revision 4 to 3",
        "expected_signal": ExpectedSignal(
            alert_should_clear="KubePodCrashLooping", window_seconds=300
        ),
    }
    return RemediationPlan(**(base | overrides))


def make_target(**overrides: Any) -> Target:
    base: dict[str, Any] = {
        "kind": "Deployment",
        "name": "api",
        "namespace": "shop",
        "replicas": 3,
        "revision": 4,
        "previous_revision": 3,
        "previous_image": "ghcr.io/o/api:v1",
    }
    return Target(**(base | overrides))


async def make_incident(
    session: AsyncSession, *, state: IncidentState = IncidentState.OPEN, **overrides: Any
) -> Incident:
    base: dict[str, Any] = {
        "state": state.value,
        "cause_scope": "workload",
        "cause_target": "api",
        "cause_alertname": "KubePodCrashLooping",
        "target_key": "shop/Deployment/api",
        "namespace": "shop",
        "precedence": 2,
    }
    incident = Incident(**(base | overrides))
    session.add(incident)
    await session.flush()
    return incident


async def make_execution(
    session: AsyncSession,
    *,
    plan: RemediationPlan | None = None,
    incident_state: IncidentState = IncidentState.VERIFYING,
    status: str = "awaiting_verification",
    mode: str = "auto",
    finished_ago_seconds: float | None = 400,
    expires_in_seconds: float | None = None,
    decision: str = "allow",
    signals_before: dict[str, Any] | None = None,
) -> tuple[Incident, RunbookExecution, RemediationPlan]:
    """An incident, its persisted plan and one execution, in whatever state the test needs.

    Times are relative to the database clock, which inside the test transaction is frozen at the
    transaction's start -- the same clock every module under test reads.
    """
    plan = plan or make_plan()
    incident = await make_incident(session, state=incident_state)
    row = await orchestrator._persist_plan(
        session,
        incident,
        plan,
        PolicyDecision(effect=decision),  # type: ignore[arg-type]
    )
    now = await _now(session)
    execution = RunbookExecution(
        incident_id=incident.id,
        plan_id=row.id,
        runbook=plan.runbook,
        status=status,
        mode=mode,
        durability=str(plan.durability),
        gitops_managed=plan.gitops_managed,
        state_before={},
        state_after={},
        signals_before=signals_before or {},
        finished_at=(
            now - timedelta(seconds=finished_ago_seconds)
            if finished_ago_seconds is not None
            else None
        ),
        expires_at=(
            now + timedelta(seconds=expires_in_seconds) if expires_in_seconds is not None else None
        ),
    )
    session.add(execution)
    await session.flush()
    return incident, execution, plan.with_plan_id(row.id)


async def _now(session: AsyncSession) -> datetime:
    from db.connection import db_now

    return await db_now(session)


async def ledger_events(session: AsyncSession, incident_id: int) -> list[str]:
    rows = await session.execute(
        select(AuditLedger.event_type)
        .where(AuditLedger.incident_id == incident_id)
        .order_by(AuditLedger.seq)
    )
    return list(rows.scalars())


async def ledger_payloads(
    session: AsyncSession, incident_id: int, event_type: str
) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(AuditLedger.payload)
        .where(AuditLedger.incident_id == incident_id, AuditLedger.event_type == event_type)
        .order_by(AuditLedger.seq)
    )
    return list(rows.scalars())


def bound_to(session: AsyncSession) -> Any:
    """A stand-in for `transaction()` / `session_scope()` that hands out the test's session.

    The test session lives inside a transaction that is rolled back afterwards, so the code under
    test writes through it exactly as it would through its own, and nothing outlives the test.
    """

    @contextlib.asynccontextmanager
    async def _scope() -> AsyncIterator[AsyncSession]:
        yield session

    return _scope


@contextlib.asynccontextmanager
async def no_lock(*_args: Any, **_kwargs: Any) -> AsyncIterator[object]:
    yield object()


def healthy_window(**overrides: Any) -> SignalWindow:
    base: dict[str, Any] = {
        "alert_firing": False,
        "restarts": 0,
        "oom_events": 0.0,
        "throttle_ratio": 0.01,
        "psi_full": 0.0,
        "memory_usage_ratio": 0.4,
        "new_alerts": (),
    }
    return SignalWindow(**(base | overrides))

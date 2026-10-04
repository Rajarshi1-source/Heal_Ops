"""The watchdog: the dead-man's switch that runs outside the engine.

It exists for one failure -- the engine dying between `execute()` and `verify()` -- so the tests
build exactly that state in PostgreSQL and run a sweep with no engine process involved at all. If
the sweep can find and undo the change from the database alone, the saga holds.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import watchdog
from db.connection import db_now
from db.models import IncidentState
from plan import Durability
from tests.factories import (
    ScriptedRunbook,
    bound_to,
    ledger_payloads,
    make_execution,
    make_plan,
    registry_of,
)

pytestmark = pytest.mark.db


@pytest.fixture
def sweep_env(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    runbook = ScriptedRunbook()
    notified: list[Any] = []

    async def notify(plan: Any, outcome: Any, **_kwargs: Any) -> None:
        notified.append(outcome)

    monkeypatch.setattr(watchdog, "transaction", bound_to(db_session))
    monkeypatch.setattr(watchdog, "load_all", lambda: registry_of(runbook))
    monkeypatch.setattr(watchdog.notifier, "watchdog_acted", notify)
    return {"runbook": runbook, "notified": notified}


async def test_a_stranded_execution_is_force_reverted(
    db_session: AsyncSession, sweep_env: dict[str, Any]
) -> None:
    """Past the 900 s deadline with no verdict: assume the worst and undo."""
    incident, execution, _ = await make_execution(db_session, finished_ago_seconds=1000)

    report = await watchdog.sweep()

    assert report.force_reverted == 1 and report.total_actions == 1
    assert sweep_env["runbook"].compensate_calls == 1
    assert execution.status == "reverted"
    assert incident.state == IncidentState.REVERTED.value
    [entry] = await ledger_payloads(db_session, incident.id, "compensate")
    assert entry["trigger"] == "watchdog"
    assert [o.trigger for o in sweep_env["notified"]] == ["watchdog"]


async def test_an_execution_still_inside_its_deadline_is_left_alone(
    db_session: AsyncSession, sweep_env: dict[str, Any]
) -> None:
    """The verifier owns it until the deadline. Reverting early would race a verdict in progress."""
    _, execution, _ = await make_execution(db_session, finished_ago_seconds=400)

    report = await watchdog.sweep()

    assert report.total_actions == 0
    assert execution.status == "awaiting_verification"


async def test_an_expired_ephemeral_fix_is_removed(
    db_session: AsyncSession, sweep_env: dict[str, Any]
) -> None:
    incident, execution, _ = await make_execution(
        db_session,
        incident_state=IncidentState.REMEDIATED,
        status="remediated",
        expires_in_seconds=-60,
    )

    report = await watchdog.sweep()

    assert report.expired == 1
    assert execution.status == "expired"
    assert incident.state == IncidentState.REMEDIATED.value
    assert [o.trigger for o in sweep_env["notified"]] == ["expiry"]


async def test_a_durable_fix_never_expires(
    db_session: AsyncSession, sweep_env: dict[str, Any]
) -> None:
    await make_execution(
        db_session,
        plan=make_plan(durability=Durability.DURABLE, ttl_seconds=None),
        incident_state=IncidentState.REMEDIATED,
        status="remediated",
        expires_in_seconds=-60,
    )
    assert (await watchdog.sweep()).expired == 0


async def test_a_second_sweep_finds_nothing_left_to_do(
    db_session: AsyncSession, sweep_env: dict[str, Any]
) -> None:
    """Idempotent: the CronJob may overlap with itself."""
    await make_execution(db_session, finished_ago_seconds=1000)
    await watchdog.sweep()

    assert (await watchdog.sweep()).total_actions == 0
    assert sweep_env["runbook"].compensate_calls == 1


async def test_a_runbook_that_no_longer_exists_is_skipped_not_guessed(
    db_session: AsyncSession, sweep_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watchdog, "load_all", lambda: registry_of())
    _, execution, _ = await make_execution(db_session, finished_ago_seconds=1000)

    report = await watchdog.sweep()

    assert report.skipped == 1 and report.total_actions == 0
    assert execution.status == "awaiting_verification"


async def test_one_failure_does_not_abort_the_sweep(
    db_session: AsyncSession, sweep_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await make_execution(db_session, finished_ago_seconds=1100)
    await make_execution(db_session, finished_ago_seconds=1000)

    original = watchdog.compensator.force_revert
    calls = 0

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("ledger partition missing")
        return await original(*args, **kwargs)

    monkeypatch.setattr(watchdog.compensator, "force_revert", flaky)

    report = await watchdog.sweep()

    assert report.failures == 1
    assert report.force_reverted == 1, "the second execution is still compensated"


async def test_find_unverified_selects_on_finish_time(db_session: AsyncSession) -> None:
    _, stale, _ = await make_execution(db_session, finished_ago_seconds=1000)
    _, fresh, _ = await make_execution(db_session, finished_ago_seconds=100)
    _, running, _ = await make_execution(db_session, finished_ago_seconds=None)

    found = await watchdog.find_unverified(db_session, await db_now(db_session), 900)

    ids = {e.id for e in found}
    assert stale.id in ids
    assert fresh.id not in ids and running.id not in ids


async def test_main_reports_failures_through_its_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing() -> watchdog.SweepReport:
        return watchdog.SweepReport(failures=1)

    async def nothing() -> None:
        return None

    monkeypatch.setattr(watchdog, "sweep", failing)
    monkeypatch.setattr("db.connection.dispose", nothing)
    assert await watchdog.main() == 1


async def test_actions_are_counted_from_the_ledger(
    db_session: AsyncSession, sweep_env: dict[str, Any]
) -> None:
    """The CronJob is never scraped, so the engine reads the count from the shared record."""
    before = await watchdog.actions_recorded(db_session)
    await make_execution(db_session, finished_ago_seconds=1000)
    await watchdog.sweep()

    assert await watchdog.actions_recorded(db_session) == before + 1


def test_report_serialises() -> None:
    report = watchdog.SweepReport(force_reverted=1, expired=2)
    assert report.total_actions == 3
    assert report.to_dict()["expired"] == 2


async def test_a_reverted_fix_has_its_gitops_pause_released(
    db_session: AsyncSession, sweep_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume belongs to the watchdog, never to the request path."""
    resumed: list[int] = []

    async def resume(session: Any, execution: Any) -> bool:
        resumed.append(execution.id)
        execution.gitops_paused = False
        return True

    monkeypatch.setattr(watchdog.gitops, "resume", resume)
    _, reverted, _ = await make_execution(db_session, status="reverted")
    reverted.gitops_paused, reverted.compensated = True, True
    _, live, _ = await make_execution(
        db_session, status="remediated", expires_in_seconds=3600, finished_ago_seconds=400
    )
    live.gitops_paused = True
    await db_session.flush()

    report = await watchdog.sweep()

    assert resumed == [reverted.id], "a live mitigation keeps its pause until the TTL"
    assert report.resumed == 1
    assert report.total_actions == 0, "routine, so not counted as a watchdog action"


async def test_an_expired_fix_is_compensated_then_resumed_in_one_sweep(
    db_session: AsyncSession, sweep_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    resumed: list[int] = []

    async def resume(session: Any, execution: Any) -> bool:
        resumed.append(execution.id)
        return True

    monkeypatch.setattr(watchdog.gitops, "resume", resume)
    _, execution, _ = await make_execution(
        db_session,
        incident_state=IncidentState.REMEDIATED,
        status="remediated",
        expires_in_seconds=-60,
    )
    execution.gitops_paused = True
    await db_session.flush()

    report = await watchdog.sweep()
    assert report.expired == 1 and resumed == [execution.id]

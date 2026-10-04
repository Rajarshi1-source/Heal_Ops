"""The anti-masking guard: on the fourth fix of the same thing in a week, stop and file a bug."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import recurrence
from config import get_settings
from db.connection import db_now
from db.models import RemediationRecurrence, RunbookPromotion
from integrations import github_client
from tests.factories import ledger_events, make_incident, make_plan

pytestmark = pytest.mark.db


async def occur(session: AsyncSession, n: int, **plan_overrides: Any) -> list[Any]:
    events = []
    for _ in range(n):
        incident = await make_incident(session)
        events.append(await recurrence.record(session, make_plan(**plan_overrides), incident))
    return events


async def test_three_fixes_in_a_week_are_just_incidents(db_session: AsyncSession) -> None:
    assert await occur(db_session, 3) == [None, None, None]


async def test_the_fourth_fires_the_guard_once(db_session: AsyncSession) -> None:
    events = await occur(db_session, 5)

    fired = [e for e in events if e is not None]
    assert len(fired) == 1, "a fifth occurrence does not file a second issue"
    event = fired[0]
    assert event.occurrences == 4
    assert event.target_key == "shop/Deployment/api"
    assert "4x in 7 days" in event.title


async def test_firing_demotes_the_runbook_in_that_namespace(db_session: AsyncSession) -> None:
    db_session.add(RunbookPromotion(runbook="scripted_runbook", namespace="shop", mode="auto"))
    await db_session.flush()

    [*_, event] = await occur(db_session, 4)

    row = await db_session.get(RunbookPromotion, ("scripted_runbook", "shop"))
    assert row is not None and row.mode == "approval"
    assert "masking guard" in (row.changed_reason or "")
    assert event is not None


async def test_firing_is_audited(db_session: AsyncSession) -> None:
    await occur(db_session, 4)
    events = await ledger_events(db_session, None)  # type: ignore[arg-type]
    assert "promote" in events
    from sqlalchemy import func

    from db.models import AuditLedger

    count = await db_session.scalar(
        select(func.count())
        .select_from(AuditLedger)
        .where(AuditLedger.event_type == "masking_guard")
    )
    assert count == 1


async def test_the_key_is_the_workload_not_the_pod(db_session: AsyncSession) -> None:
    """An OOM fix targets a pod, and every restart is a new pod. Keying by the plan's target would
    see four different pods and never fire on the defect the guard exists for."""
    pods = [f"api-7d-{i}" for i in range(4)]
    events = []
    for pod in pods:
        incident = await make_incident(db_session)
        plan = make_plan(runbook="oom_memory_bump", target_kind="Pod", target_name=pod)
        events.append(await recurrence.record(db_session, plan, incident))
    assert events[-1] is not None


async def test_occurrences_older_than_the_window_do_not_count(db_session: AsyncSession) -> None:
    today = (await db_now(db_session)).date()
    db_session.add(
        RemediationRecurrence(
            runbook="scripted_runbook",
            target_key="shop/Deployment/api",
            window_start=today - timedelta(days=8),
            occurrences=10,
        )
    )
    await db_session.flush()
    assert await occur(db_session, 3) == [None, None, None]


async def test_the_report_carries_the_evidence(db_session: AsyncSession) -> None:
    from tests.factories import make_execution

    for _ in range(2):
        await make_execution(db_session)
    since = (await db_now(db_session)).date() - timedelta(days=6)

    body = await recurrence.render_report(
        db_session, "scripted_runbook", "shop/Deployment/api", since
    )

    assert "demoted to approval mode" in body
    assert body.count("roll back shop/api") == 2
    assert "| Opened | Incident | Plan | Execution | Verdict | Ledger |" in body
    assert "--verify-chain" in body


async def test_the_issue_url_replaces_the_filing_marker(db_session: AsyncSession) -> None:
    [*_, event] = await occur(db_session, 4)
    assert event is not None

    await recurrence.attach_issue(db_session, event, "https://github.com/o/r/issues/7")

    urls = (
        (
            await db_session.execute(
                select(RemediationRecurrence.issue_url).where(
                    RemediationRecurrence.issue_url.isnot(None)
                )
            )
        )
        .scalars()
        .all()
    )
    assert urls == ["https://github.com/o/r/issues/7"]


async def test_no_github_still_closes_the_marker_honestly(db_session: AsyncSession) -> None:
    [*_, event] = await occur(db_session, 4)
    assert event is not None
    await recurrence.attach_issue(db_session, event, None)
    url = await db_session.scalar(
        select(RemediationRecurrence.issue_url).where(RemediationRecurrence.issue_url.isnot(None))
    )
    assert url is not None and "not filed" in url


# --- the GitHub client ----------------------------------------------------------------------------


async def test_unconfigured_github_files_nothing() -> None:
    assert await github_client.create_issue("t", "b", ["healops"]) is None


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "github_token", SecretStr("github_pat_test"))
    monkeypatch.setattr(get_settings(), "github_repo", "o/r")


@respx.mock
async def test_an_issue_is_filed_with_its_labels(github: None) -> None:
    route = respx.post("https://api.github.com/repos/o/r/issues").mock(
        return_value=httpx.Response(201, json={"html_url": "https://github.com/o/r/issues/7"})
    )

    url = await github_client.create_issue("title", "body", recurrence.LABELS)

    assert url == "https://github.com/o/r/issues/7"
    sent = route.calls[0].request
    assert sent.headers["Authorization"] == "Bearer github_pat_test"
    assert b"needs-root-cause" in sent.content


@respx.mock
async def test_a_github_outage_returns_none(github: None) -> None:
    respx.post("https://api.github.com/repos/o/r/issues").mock(return_value=httpx.Response(503))
    assert await github_client.create_issue("t", "b", []) is None

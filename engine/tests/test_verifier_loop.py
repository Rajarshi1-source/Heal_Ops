"""The verifier's moving parts around `judge()`: early failure, observation, and the record.

`test_verifier.py` covers the final verdict. This file covers what makes "auto-reverted in ~90 s"
possible without ever calling success early, and what lands in PostgreSQL when a verdict is reached.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import plan_store
import verifier
from db.models import RemediationPlanRow, Verification
from plan import Durability
from runbooks.base import SignalWindow
from tests.factories import healthy_window, ledger_payloads, make_execution, make_plan

BEFORE = healthy_window(alert_firing=True, new_alerts=("KubePodCrashLooping",))


# --- judge_early: only ever a failure -------------------------------------------------------------


def test_a_healthy_early_window_is_not_a_verdict() -> None:
    assert verifier.judge_early(make_plan(), BEFORE, healthy_window()) is None


def test_the_triggering_alert_still_firing_early_is_not_a_failure() -> None:
    """`for:` lag means it is expected to still be firing ninety seconds in."""
    early = healthy_window(alert_firing=True, new_alerts=("KubePodCrashLooping",))
    assert verifier.judge_early(make_plan(), BEFORE, early) is None


def test_a_single_restart_early_is_not_yet_definitive() -> None:
    assert verifier.judge_early(make_plan(), BEFORE, healthy_window(restarts=1)) is None


@pytest.mark.parametrize(
    ("after", "extra", "fragment"),
    [
        (healthy_window(restarts=2), None, "restarted 2 time(s)"),
        (healthy_window(oom_events=1.0), None, "OOM"),
        (healthy_window(new_alerts=("KubePodOOMKilled",)), None, "KubePodOOMKilled"),
        (healthy_window(), "restartCount moved", "restartCount moved"),
    ],
)
def test_definite_evidence_fails_early(
    after: SignalWindow, extra: str | None, fragment: str
) -> None:
    verdict = verifier.judge_early(make_plan(), BEFORE, after, extra_failure=extra)

    assert verdict is not None
    assert verdict.outcome == "not_recovered"
    assert verdict.reasons[0] == "failed before the window closed"
    assert any(fragment in reason for reason in verdict.reasons)


def test_unknown_alert_state_survives_into_an_early_verdict() -> None:
    verdict = verifier.judge_early(
        make_plan(), BEFORE, healthy_window(alert_firing=None, restarts=5)
    )
    assert verdict is not None and verdict.alert_cleared is None


# --- windows --------------------------------------------------------------------------------------


def test_the_post_action_window_excludes_the_settle_period() -> None:
    assert verifier.post_action_window(400) == "370s"


def test_the_post_action_window_has_a_floor() -> None:
    assert verifier.post_action_window(35) == f"{verifier.MIN_WINDOW_SECONDS}s"


def test_window_falls_back_to_settings_without_an_expected_signal() -> None:
    assert verifier.window_seconds(make_plan(expected_signal=None)) == 300


# --- observe --------------------------------------------------------------------------------------


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    seen: dict[str, list[Any]] = {"prom": [], "firing": [], "target": []}

    async def golden(namespace: Any, **kwargs: Any) -> Any:
        from integrations.prometheus_client import GoldenSignals

        seen["prom"].append((namespace, kwargs))
        return GoldenSignals(restarts=2.0, oom_events=0.0, throttle_ratio=0.01, psi_full=0.0)

    async def is_firing(alertname: str, **kwargs: Any) -> bool:
        seen["firing"].append((alertname, kwargs))
        return False

    async def on_target(namespace: Any, **kwargs: Any) -> list[str]:
        seen["target"].append((namespace, kwargs))
        return ["KubePodCrashLooping"]

    monkeypatch.setattr(verifier.prometheus_client, "golden_signals", golden)
    monkeypatch.setattr(verifier.alertmanager_client, "is_firing", is_firing)
    monkeypatch.setattr(verifier.alertmanager_client, "alerts_on_target", on_target)
    return seen


async def test_observe_scopes_every_probe_to_the_workload(probes: dict[str, list[Any]]) -> None:
    window = await verifier.observe(make_plan(), window="60s")

    assert window.restarts == 2 and window.alert_firing is False
    assert window.new_alerts == ("KubePodCrashLooping",)
    assert probes["prom"] == [("shop", {"workload": "api", "window": "60s", "pod": None})]
    assert probes["firing"][0][1]["workload"] == "api"
    assert probes["target"][0][1] == {"pod": None, "node": None, "workload": "api"}


async def test_observe_scopes_a_node_target_by_node(probes: dict[str, list[Any]]) -> None:
    await verifier.observe(make_plan(target_kind="Node", target_name="worker-2"))
    # Workload golden signals would be read cluster-wide for a node, so they are not read at all.
    assert probes["prom"] == []
    assert probes["target"][0][1] == {"pod": None, "node": "worker-2", "workload": None}


async def test_observe_makes_no_alert_claim_without_an_expected_signal(
    probes: dict[str, list[Any]],
) -> None:
    window = await verifier.observe(make_plan(expected_signal=None))
    assert window.alert_firing is None
    assert probes["firing"] == []


# --- the signal window round-trips through PostgreSQL ---------------------------------------------


def test_signal_window_round_trips() -> None:
    window = healthy_window(restarts=3, new_alerts=("A", "B"))
    assert SignalWindow.from_dict(window.to_dict()) == window


def test_an_empty_persisted_window_reads_as_unobserved() -> None:
    assert SignalWindow.from_dict({}) == SignalWindow()
    assert SignalWindow.from_dict(None) == SignalWindow()


# --- record ---------------------------------------------------------------------------------------


@pytest.mark.db
async def test_record_writes_the_row_and_the_ledger_entry_together(
    db_session: AsyncSession,
) -> None:
    incident, execution, plan = await make_execution(db_session)
    verdict = verifier.judge(plan, BEFORE, healthy_window(restarts=4))

    digest = await verifier.record(db_session, execution, plan, verdict)

    row = (
        await db_session.execute(
            select(Verification).where(Verification.execution_id == execution.id)
        )
    ).scalar_one()
    assert row.outcome == "not_recovered"
    assert row.window_seconds == 300
    assert row.signals_after["restarts"] == 4.0
    [entry] = await ledger_payloads(db_session, incident.id, "verify")
    assert entry["outcome"] == "not_recovered"
    assert len(digest) == 64


# --- plan_store -----------------------------------------------------------------------------------


@pytest.mark.db
async def test_a_plan_survives_the_round_trip_through_its_row(db_session: AsyncSession) -> None:
    """The watchdog can only undo what it can reconstruct, compensation included."""
    _, execution, plan = await make_execution(db_session, decision="require_approval")
    row = await db_session.get(RemediationPlanRow, execution.plan_id)
    assert row is not None

    rebuilt = plan_store.rehydrate(row)

    # The factory's plan names incident 1; the persisted row belongs to the incident just created.
    assert rebuilt.digest() == replace(plan, incident_id=row.incident_id).digest()
    assert rebuilt.compensation == plan.compensation
    assert plan_store.decision_of(row).effect == "require_approval"


@pytest.mark.db
async def test_a_durable_plan_without_signal_round_trips(db_session: AsyncSession) -> None:
    original = make_plan(durability=Durability.DURABLE, ttl_seconds=None, expected_signal=None)
    _, execution, _ = await make_execution(db_session, plan=original)
    row = await db_session.get(RemediationPlanRow, execution.plan_id)
    assert row is not None

    rebuilt = plan_store.rehydrate(row)
    assert rebuilt.expected_signal is None
    assert rebuilt.durability is Durability.DURABLE


# --- the plan's own committed recovery criterion --------------------------------------------------


def drain_plan() -> Any:
    from plan import ExpectedSignal

    return make_plan(
        target_kind="Node",
        target_name="worker-3",
        target_namespace=None,
        expected_signal=ExpectedSignal(
            alert_should_clear="",
            queries={"pods_left_on_node": "q1", "unavailable_replicas": "q2"},
            thresholds={"pods_left_on_node": 0.0, "unavailable_replicas": 0.0},
        ),
    )


def node_window(**custom: float | None) -> SignalWindow:
    return SignalWindow(new_alerts=("KubeNodeNotReady",), custom=dict(custom))


NODE_BEFORE = SignalWindow(new_alerts=("KubeNodeNotReady",))


def test_a_plan_that_promised_no_alert_is_judged_on_its_own_checks() -> None:
    """A drain does not make the node Ready; the still-firing NotReady alert is not a failure."""
    verdict = verifier.judge(
        drain_plan(), NODE_BEFORE, node_window(pods_left_on_node=0.0, unavailable_replicas=0.0)
    )
    assert verdict.outcome == "remediated"
    assert verdict.alert_cleared is None, "not applicable must not read as 'did not clear'"


def test_a_committed_threshold_breached_is_a_failure() -> None:
    verdict = verifier.judge(
        drain_plan(), NODE_BEFORE, node_window(pods_left_on_node=2.0, unavailable_replicas=0.0)
    )
    assert verdict.outcome == "not_recovered"
    assert any("pods_left_on_node is 2" in r for r in verdict.reasons)


def test_a_committed_check_that_cannot_be_read_is_inconclusive() -> None:
    """The plan promised this criterion. Unreadable is unproven, never passed."""
    verdict = verifier.judge(
        drain_plan(), NODE_BEFORE, node_window(pods_left_on_node=0.0, unavailable_replicas=None)
    )
    assert verdict.outcome == "inconclusive"
    assert "unavailable_replicas" in verdict.reasons[0]


def test_committed_values_are_recorded_with_the_verdict() -> None:
    verdict = verifier.judge(
        drain_plan(), NODE_BEFORE, node_window(pods_left_on_node=0.0, unavailable_replicas=0.0)
    )
    assert verdict.signals_after["custom:pods_left_on_node"] == 0.0


async def test_observe_evaluates_the_committed_queries(
    probes: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def thresholds(queries: dict[str, str]) -> dict[str, float | None]:
        return dict.fromkeys(queries, 0.0)

    monkeypatch.setattr(verifier.prometheus_client, "query_thresholds", thresholds)
    window = await verifier.observe(drain_plan())
    assert window.custom == {"pods_left_on_node": 0.0, "unavailable_replicas": 0.0}
    assert window.alert_firing is None


async def test_a_pod_plan_is_judged_on_that_pod_alone(probes: dict[str, list[Any]]) -> None:
    await verifier.observe(make_plan(target_kind="Pod", target_name="api-7d-abc"))
    assert probes["prom"][0][1]["pod"] == "api-7d-abc"
    assert probes["prom"][0][1]["workload"] is None
    assert probes["target"][0][1]["pod"] == "api-7d-abc"


def test_custom_signals_round_trip() -> None:
    window = node_window(pods_left_on_node=0.0, unavailable_replicas=None)
    assert SignalWindow.from_dict(window.to_dict()) == window

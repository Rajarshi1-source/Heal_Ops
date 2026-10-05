"""Runbook 3, cpu_scale_up, on a real cluster: scaling out fixes CPU that comes from load, the
compensation restores the replica count exactly, and an HPA on an external metric is refused."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.chaos import harness
from tests.chaos.harness import CHAOS_NS, kubectl

RUNBOOK = "cpu_scale_up"
DEPLOYMENT = "cpu-burner"
TARGET_KEY = f"{CHAOS_NS}/Deployment/{DEPLOYMENT}"
BASELINE_REPLICAS = "2"

EXTERNAL_HPA = f"""
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {DEPLOYMENT}-queue
  namespace: {CHAOS_NS}
spec:
  scaleTargetRef: {{ apiVersion: apps/v1, kind: Deployment, name: {DEPLOYMENT} }}
  minReplicas: 2
  maxReplicas: 6
  metrics:
    - type: External
      external:
        metric: {{ name: queue_depth }}
        target: {{ type: AverageValue, averageValue: "30" }}
"""


def _load(replicas: int) -> None:
    kubectl("-n", CHAOS_NS, "scale", "deployment/cpu-load", f"--replicas={replicas}")


@pytest.fixture
def baseline(cluster: None) -> Iterator[None]:
    """No load, two burners -- and back to that however the test ends."""
    _load(0)
    kubectl("-n", CHAOS_NS, "scale", f"deployment/{DEPLOYMENT}", f"--replicas={BASELINE_REPLICAS}")
    harness.rollout_settled(f"deployment/{DEPLOYMENT}")
    yield
    _load(0)
    kubectl("-n", CHAOS_NS, "scale", f"deployment/{DEPLOYMENT}", f"--replicas={BASELINE_REPLICAS}")
    harness.rollout_settled(f"deployment/{DEPLOYMENT}")


async def test_compensation_restores_the_replica_count_exactly(baseline: None) -> None:
    """Scale out, compensate, and compare the whole deployment snapshot with `==`.

    A scale touches no template, so here even the revision counter must match.
    """
    from k8s import state_capture
    from planner import replan
    from runbooks import load_all

    before = await state_capture.capture_deployment(CHAOS_NS, DEPLOYMENT)
    target = await harness.live_target("HighPodCPU", namespace=CHAOS_NS, name=DEPLOYMENT)
    plan = replan(harness.bundle_for("HighPodCPU", target, container=DEPLOYMENT))
    assert plan is not None and not plan.is_refusal, plan and plan.refusal_reason

    runbook = load_all().get(RUNBOOK)
    assert runbook is not None
    executed = await runbook.execute(plan)
    assert executed.ok, executed.detail
    assert executed.state_after["replicas"] == 4

    compensated = await runbook.compensate(plan)
    assert compensated.ok, compensated.detail
    assert await state_capture.capture_deployment(CHAOS_NS, DEPLOYMENT) == before


async def test_refuses_to_fight_an_hpa_on_an_external_metric(baseline: None) -> None:
    """An HPA scaling on queue depth owns the replica count, and lowering a CPU target would
    fight the real signal. Captured live from the cluster, the plan is a refusal."""
    from planner import plan_for

    kubectl("apply", "-f", "-", stdin=EXTERNAL_HPA)
    try:
        target = await harness.live_target("HighPodCPU", namespace=CHAOS_NS, name=DEPLOYMENT)
        assert target.autoscaler is not None and target.autoscaler.metric == "external"
        decision = plan_for(harness.bundle_for("HighPodCPU", target, container=DEPLOYMENT))
    finally:
        kubectl("-n", CHAOS_NS, "delete", "hpa", f"{DEPLOYMENT}-queue", "--ignore-not-found")

    assert decision.effect == "refuse", decision.plan.refusal_reason
    assert "external" in (decision.plan.refusal_reason or "").lower()


def test_fixes_cpu_saturation_end_to_end(engine: harness.Engine, baseline: None) -> None:
    """Load saturates two burners; the engine scales to four; per-pod CPU halves; remediated.

    The load comes through the Service from cpu-load, so more replicas genuinely share it -- the
    only kind of CPU problem a scale-out can honestly be credited with fixing.
    """
    mark = engine.high_water_mark()
    with harness.promoted(RUNBOOK, CHAOS_NS):
        _load(1)
        incident = engine.wait_for_incident(
            after=mark, target_key=TARGET_KEY, alertname="HighPodCPU", timeout=20 * 60
        )
        detail = engine.wait_for_state(
            incident["id"], harness.TERMINAL, timeout=harness.FIX_TIMEOUT_SECONDS
        )

    replay = engine.evidence(incident["id"])["replay"]
    assert detail["state"] == "remediated", replay
    assert detail["runbook"] == RUNBOOK
    assert detail["verifications"][-1]["outcome"] == "remediated", replay
    deployment = harness.kubectl_json("-n", CHAOS_NS, "get", "deployment", DEPLOYMENT)
    assert deployment["spec"]["replicas"] == 4

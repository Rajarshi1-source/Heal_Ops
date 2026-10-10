"""Runbook 5, node_drain, on a real cluster: draining a node whose kubelet is genuinely gone,
uncordoning it back exactly, refusing when there is nowhere to drain to, and stopping -- cordoned,
escalated, NOT uncordoned -- when a PodDisruptionBudget says no.

Faults land only on the general pool (k8s/kind/cluster.yaml). The platform pool holds HealOps'
own database and the only Prometheus, and a drain that took either with it would prove nothing.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.chaos import harness
from tests.chaos.harness import CHAOS_NS, kubectl

RUNBOOK = "node_drain"
FLAKY_NODE = Path(__file__).resolve().parents[3] / "chaos-app" / "injectors" / "flaky_node.py"

CHAOS_PDB = f"""
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: chaos-suite-hold
  namespace: {CHAOS_NS}
spec:
  maxUnavailable: 0
  selector:
    matchLabels: {{ app.kubernetes.io/part-of: chaos-app }}
"""


def _general_pool() -> list[str]:
    nodes = harness.kubectl_json("get", "nodes", "-l", "healops.io/pool=general")["items"]
    return sorted(n["metadata"]["name"] for n in nodes)


def _chaos_pods_on(node: str) -> list[str]:
    pods = harness.kubectl_json(
        "-n", CHAOS_NS, "get", "pods", "--field-selector", f"spec.nodeName={node}"
    )["items"]
    return sorted(p["metadata"]["name"] for p in pods if not p["metadata"].get("deletionTimestamp"))


def _schedulable(node: str) -> bool:
    """Uncordoned in full: the field clear AND the controller's mirrored cordon taint lifted."""
    spec = harness.kubectl_json("get", "node", node)["spec"]
    taints = spec.get("taints") or []
    return not spec.get("unschedulable") and not any(
        t["key"] == "node.kubernetes.io/unschedulable" for t in taints
    )


def _flaky(action: str, node: str) -> None:
    subprocess.run(  # noqa: S603
        [sys.executable, str(FLAKY_NODE), action, "--node", node, "--wait"], check=True
    )


@pytest.fixture
def victim(cluster: None) -> Iterator[str]:
    """A general-pool node with chaos pods on it -- and Ready and uncordoned again afterwards."""
    candidates = [n for n in _general_pool() if _chaos_pods_on(n)]
    if not candidates:
        pytest.skip("no general-pool node is running chaos pods to drain")
    node = candidates[-1]
    # A previous test's uncordon may still be inside the controller's lag; a snapshot taken then
    # would record a cordon taint as the node's "before".
    harness.wait_until(lambda: _schedulable(node), timeout=120, interval=2, what=f"{node} settled")
    yield node
    _flaky("restore", node)  # idempotent: starts the kubelet if stopped, and uncordons
    for name in _general_pool():
        kubectl("uncordon", name)
    kubectl("-n", CHAOS_NS, "delete", "pdb", "chaos-suite-hold", "--ignore-not-found")


async def _plan(node: str):  # noqa: ANN202 - RemediationPlan, imported lazily
    from planner import replan

    target = await harness.live_target("KubeNodeNotReady", name=node)
    plan = replan(harness.bundle_for("KubeNodeNotReady", target))
    assert plan is not None and not plan.is_refusal, plan and plan.refusal_reason
    return plan


async def test_compensation_uncordons_the_node_exactly(victim: str) -> None:
    """Cordon and evict, compensate, and compare the node with `==`.

    Partially reversible, and the test says so: the node comes back exactly, the evicted pods do
    not move back -- they were rescheduled, and the plan never claimed otherwise.
    """
    from k8s import state_capture
    from runbooks import load_all

    before = await state_capture.capture_node(victim)
    plan = await _plan(victim)

    runbook = load_all().get(RUNBOOK)
    assert runbook is not None
    executed = await runbook.execute(plan)
    assert executed.ok, executed.detail
    assert (await state_capture.capture_node(victim))["unschedulable"] is True

    compensated = await runbook.compensate(plan)
    assert compensated.ok, compensated.detail
    assert await state_capture.capture_node(victim) == before


async def test_refuses_when_there_is_nowhere_to_drain_to(victim: str) -> None:
    """Every other worker cordoned: draining would strand the pods, so the plan refuses."""
    from planner import plan_for

    peers = [n for n in harness.worker_nodes() if n != victim]
    for peer in peers:
        kubectl("cordon", peer)
    try:
        target = await harness.live_target("KubeNodeNotReady", name=victim)
        decision = plan_for(harness.bundle_for("KubeNodeNotReady", target))
    finally:
        for peer in peers:
            kubectl("uncordon", peer)

    assert decision.effect == "refuse", decision.plan.refusal_reason
    assert "no healthy, schedulable node" in (decision.plan.refusal_reason or "")


async def test_a_pdb_stops_the_drain_and_the_node_stays_cordoned(victim: str) -> None:
    """The decision on record: a drain a PDB stops leaves the node cordoned and escalates. It is
    not uncordoned automatically -- a half-drained node is a human's call, not the runbook's."""
    from k8s import state_capture
    from runbooks import load_all

    kubectl("apply", "-f", "-", stdin=CHAOS_PDB)
    pods_before = _chaos_pods_on(victim)
    plan = await _plan(victim)

    runbook = load_all().get(RUNBOOK)
    assert runbook is not None
    result = await runbook.execute(plan)

    assert not result.ok
    assert "stopped by a PodDisruptionBudget" in result.detail
    assert "stays cordoned" in result.detail
    assert (await state_capture.capture_node(victim))["unschedulable"] is True
    assert _chaos_pods_on(victim) == pods_before  # nothing the budget protects was evicted


def test_drains_a_dead_node_end_to_end(engine: harness.Engine, victim: str) -> None:
    """The kubelet is stopped for real; the node goes NotReady; the engine plans a drain, a human
    approves it, and the verifier judges the moved workloads running elsewhere."""
    mark = engine.high_water_mark()
    with harness.promoted(RUNBOOK, "-", mode="approval"):
        _flaky("break", victim)
        incident = engine.wait_for_incident(
            after=mark,
            target_key=f"node/{victim}",
            alertname="KubeNodeNotReady",
            timeout=harness.ALERT_TIMEOUT_SECONDS,
        )
        waiting = engine.wait_for_state(
            incident["id"], ("awaiting_approval", *harness.TERMINAL), timeout=5 * 60
        )
        assert waiting["state"] == "awaiting_approval", engine.evidence(incident["id"])["replay"]
        engine.approve(waiting["executions"][-1]["id"])
        detail = engine.wait_for_state(
            incident["id"], harness.TERMINAL, timeout=harness.FIX_TIMEOUT_SECONDS
        )

    replay = engine.evidence(incident["id"])["replay"]
    assert detail["state"] == "remediated", replay
    assert detail["verifications"][-1]["outcome"] == "remediated", replay
    # Remediated does not mean healthy: the node is still down, and still cordoned.
    assert not harness.node_ready(victim)
    node = harness.kubectl_json("get", "node", victim)
    assert node["spec"].get("unschedulable") is True

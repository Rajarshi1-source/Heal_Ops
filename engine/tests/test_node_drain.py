"""Runbook 5: cordon and drain, with a capacity precheck and PDB-respecting evictions."""

from __future__ import annotations

from typing import Any

import pytest

from bundle import BundleAlert, IncidentBundle, NodeState, Target
from k8s import node_ops, state_capture
from plan import Reversibility
from runbooks.node_drain import NodeDrain

RUNBOOK = NodeDrain()

# Worker-3 is NotReady with 1000m / 1000Mi of movable requests on it.
SICK = NodeState(name="worker-3", ready=False, requested_cpu_millis=1000, requested_memory_mib=1000)


def peer(name: str, free_cpu: int, free_memory: int, **overrides: Any) -> NodeState:
    base: dict[str, Any] = {
        "name": name,
        "allocatable_cpu_millis": 4000,
        "allocatable_memory_mib": 8000,
        "requested_cpu_millis": 4000 - free_cpu,
        "requested_memory_mib": 8000 - free_memory,
    }
    return NodeState(**(base | overrides))


def target(peers: tuple[NodeState, ...], **overrides: Any) -> Target:
    base: dict[str, Any] = {
        "kind": "Node",
        "name": "worker-3",
        "node": "worker-3",
        "pods": ("chaos/crashy-api-1", "shop/api-2"),
        "node_state": SICK,
        "peer_nodes": peers,
    }
    return Target(**(base | overrides))


def plan_for(t: Target) -> Any:
    bundle = IncidentBundle(
        incident_id="5",
        cause_alert=BundleAlert(alertname="KubeNodeNotReady", fingerprint="f", node="worker-3"),
        target=t,
        cause_scope="node",
    )
    return RUNBOOK.plan(bundle, t)


ROOMY = (peer("worker-1", 800, 800), peer("worker-2", 800, 800))


# --- plan ----------------------------------------------------------------------------------------


def test_plans_a_cordon_then_one_eviction_per_movable_pod() -> None:
    plan = plan_for(target(ROOMY))

    assert not plan.is_refusal
    assert [a.verb for a in plan.actions] == ["cordon", "evict", "evict"]
    assert [(a.namespace, a.name) for a in plan.actions[1:]] == [
        ("chaos", "crashy-api-1"),
        ("shop", "api-2"),
    ]
    assert [c.verb for c in plan.compensation] == ["uncordon"]
    assert plan.reversibility is Reversibility.PARTIAL, "evictions cannot be undone"
    assert plan.blast_radius.nodes == 1
    assert plan.blast_radius.namespaces == ("chaos", "shop")


def test_capacity_exactly_at_120_percent_is_enough() -> None:
    exact = (peer("worker-1", 600, 600), peer("worker-2", 600, 600))  # 1200 / 1200 free
    assert not plan_for(target(exact)).is_refusal


def test_capacity_under_120_percent_is_a_refusal() -> None:
    """Draining onto nodes that cannot fit the load turns one sick node into an outage."""
    tight = (peer("worker-1", 600, 600), peer("worker-2", 599, 600))  # 1199m < 1200m
    plan = plan_for(target(tight))
    assert plan.is_refusal
    assert "remaining capacity is too small" in (plan.refusal_reason or "")
    assert "1200m" in (plan.refusal_reason or "")


def test_memory_is_checked_as_well_as_cpu() -> None:
    low_memory = (peer("worker-1", 2000, 500), peer("worker-2", 2000, 500))
    assert plan_for(target(low_memory)).is_refusal


def test_unhealthy_or_cordoned_peers_do_not_count() -> None:
    peers = (
        peer("worker-1", 2000, 2000, ready=False),
        peer("worker-2", 2000, 2000, schedulable=False),
    )
    plan = plan_for(target(peers))
    assert plan.is_refusal and "no healthy, schedulable node" in (plan.refusal_reason or "")


def test_non_node_targets_are_refused() -> None:
    assert plan_for(Target(kind="Deployment", name="api", namespace="shop")).is_refusal


def test_recovery_promises_no_alert_and_commits_its_own_checks() -> None:
    """Draining does not make the node Ready, so the plan makes no claim about that alert."""
    signal = plan_for(target(ROOMY)).expected_signal

    assert signal.alert_should_clear == ""
    assert set(signal.thresholds) == {"pods_left_on_node", "unavailable_replicas"}
    assert 'node="worker-3"' in signal.queries["pods_left_on_node"]
    assert 'namespace=~"chaos|shop"' in signal.queries["unavailable_replicas"]
    # A dead exporter must not read as a drained node.
    assert "vector(0)" not in " ".join(signal.queries.values())
    assert "kube-state-metrics" in signal.queries["pods_left_on_node"]
    # Evicted pods on a dead kubelet stay Terminating; they are leaving, not left.
    assert (
        "unless on (namespace, pod) kube_pod_deletion_timestamp"
        in (signal.queries["pods_left_on_node"])
    )


def test_recovery_is_judged_on_the_moved_workloads_not_their_namespaces() -> None:
    """Regression: a namespace-wide check reverted a drain that worked.

    Found by the chaos suite. The chaos namespace holds workloads that crash-loop on purpose, so
    `unavailable replicas in the moved namespaces` was never zero, whatever the drain did.
    """
    moved = ("chaos/Deployment/crashy-api", "chaos/Deployment/cpu-burner", "shop/StatefulSet/db")
    signal = plan_for(target(ROOMY, drained_workloads=moved)).expected_signal
    query = signal.queries["unavailable_replicas"]

    assert 'deployment=~"cpu-burner|crashy-api"' in query
    assert 'namespace="chaos"' in query
    assert 'statefulset=~"db"' in query
    assert "namespace=~" not in query  # no namespace-wide reading left
    # Every term is guarded, so a dead exporter empties the sum instead of zeroing it.
    assert query.count("kube-state-metrics") == 2
    assert "vector(0)" not in query


def test_an_empty_node_is_only_cordoned() -> None:
    plan = plan_for(target((), pods=(), node_state=NodeState(name="worker-3", ready=False)))
    assert [a.verb for a in plan.actions] == ["cordon"]
    assert "unavailable_replicas" not in plan.expected_signal.thresholds


# --- execute -------------------------------------------------------------------------------------


class Cluster:
    def __init__(self, *, blocked: set[str] | None = None, gone: set[str] | None = None) -> None:
        self.unschedulable = False
        self.blocked = blocked or set()
        self.gone = gone or set()
        self.evicted: list[str] = []
        # Whether the node lifecycle controller ever lifts its cordon taint after an uncordon.
        self.controller_lifts_taint = True

    async def set_unschedulable(self, name: str, value: bool) -> None:
        self.unschedulable = value

    async def evict(self, namespace: str, name: str) -> str:
        key = f"{namespace}/{name}"
        if key in self.blocked:
            return "blocked"
        if key in self.gone:
            return "gone"
        self.evicted.append(key)
        return "evicted"

    async def await_schedulable(self, name: str) -> bool:
        return not self.unschedulable and self.controller_lifts_taint

    async def capture_node(self, name: str) -> dict[str, Any]:
        return {"name": name, "unschedulable": self.unschedulable}


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> Cluster:
    fake = Cluster()
    monkeypatch.setattr(node_ops, "set_unschedulable", fake.set_unschedulable)
    monkeypatch.setattr(node_ops, "evict", fake.evict)
    monkeypatch.setattr(node_ops, "await_schedulable", fake.await_schedulable)
    monkeypatch.setattr(state_capture, "capture_node", fake.capture_node)
    return fake


async def test_drain_cordons_then_evicts(cluster: Cluster) -> None:
    result = await RUNBOOK.execute(plan_for(target(ROOMY)))

    assert result.ok
    assert cluster.unschedulable is True
    assert cluster.evicted == ["chaos/crashy-api-1", "shop/api-2"]
    assert result.state_before == {"name": "worker-3", "unschedulable": False}
    assert result.state_after == {"name": "worker-3", "unschedulable": True}


async def test_a_pdb_stops_the_drain_and_names_what_was_held_back(cluster: Cluster) -> None:
    cluster.blocked = {"chaos/crashy-api-1"}

    result = await RUNBOOK.execute(plan_for(target(ROOMY)))

    assert not result.ok
    assert cluster.evicted == [], "stops rather than pushing on to the next pod"
    assert "PodDisruptionBudget" in result.detail
    assert "chaos/crashy-api-1, shop/api-2" in result.detail
    assert "kubectl uncordon worker-3" in result.detail


async def test_pods_already_gone_are_not_a_failure(cluster: Cluster) -> None:
    cluster.gone = {"shop/api-2"}
    result = await RUNBOOK.execute(plan_for(target(ROOMY)))
    assert result.ok and "1 were already gone" in result.detail


async def test_compensation_uncordons_and_says_what_it_cannot_undo(cluster: Cluster) -> None:
    plan = plan_for(target(ROOMY))
    await RUNBOOK.execute(plan)

    result = await RUNBOOK.compensate(plan)

    assert result.ok
    assert cluster.unschedulable is False
    assert "cannot be undone" in result.detail


async def test_compensation_is_idempotent(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []

    async def record(name: str, value: bool) -> None:
        calls.append(value)

    monkeypatch.setattr(node_ops, "set_unschedulable", record)
    assert (await RUNBOOK.compensate(plan_for(target(ROOMY)))).ok
    assert calls == [], "already schedulable: nothing to patch"


async def test_an_uncordon_the_controller_never_completes_is_not_a_compensation(
    cluster: Cluster,
) -> None:
    """The PATCH landed, the taint did not lift: the node still repels pods, so say so."""
    plan = plan_for(target(ROOMY))
    await RUNBOOK.execute(plan)
    cluster.controller_lifts_taint = False

    result = await RUNBOOK.compensate(plan)

    assert not result.ok
    assert node_ops.UNSCHEDULABLE_TAINT in result.detail


def _node(unschedulable: bool, *taint_keys: str) -> Any:
    from types import SimpleNamespace

    taints = [SimpleNamespace(key=k, value=None, effect="NoSchedule") for k in taint_keys]
    return SimpleNamespace(spec=SimpleNamespace(unschedulable=unschedulable, taints=taints))


async def test_schedulable_waits_for_the_controller_to_lift_its_taint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Field cleared at once; the controller's mirrored taint lingers for two reads.
    reads = iter(
        [
            _node(False, node_ops.UNSCHEDULABLE_TAINT),
            _node(False, node_ops.UNSCHEDULABLE_TAINT),
            _node(False, "dedicated"),
        ]
    )

    async def read(*_: Any) -> Any:
        return next(reads)

    monkeypatch.setattr(node_ops, "call", read)
    assert await node_ops.await_schedulable("worker-3", timeout_s=5, interval_s=0)
    assert next(reads, None) is None, "returned only once the taint was gone"


async def test_schedulable_gives_up_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def read(*_: Any) -> Any:
        return _node(False, node_ops.UNSCHEDULABLE_TAINT)

    monkeypatch.setattr(node_ops, "call", read)
    assert not await node_ops.await_schedulable("worker-3", timeout_s=0, interval_s=0)

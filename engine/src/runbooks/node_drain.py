"""Runbook 5 -- cordon and drain a NotReady node.

The most disruptive runbook, and the guardrails reflect it:

* **A capacity precheck in `plan()`.** If the healthy, schedulable peers cannot absorb 120% of what
  is being moved, the plan is a refusal. Draining onto nodes that cannot fit the load converts one
  sick node into a cluster-wide outage, which is the failure-mode row this exists for.
* **PodDisruptionBudgets are respected**, because every pod leaves through the Eviction API. A
  budget that would be breached stops the drain and the pods it protects stay put, reported.
* **It always asks first** -- but that is the policy file's `approve-node-scoped` rule speaking, not
  this runbook. A runbook never decides whether it may run.

Reversibility is honest: cordon is undone exactly by uncordon, but evicted pods have already been
rescheduled and their connections dropped. That makes the plan *partially* reversible, and the
compensation restores only what can be restored.

**What recovery means here.** Draining does not make a node Ready, so `KubeNodeNotReady` is not
expected to clear and the plan makes no claim about it. Recovery is the workloads running elsewhere:
nothing movable left on the node, and no unavailable replicas in the namespaces that were moved.
Those are committed as the plan's own queries before anything is touched.
"""

from __future__ import annotations

import structlog

from bundle import IncidentBundle, NodeState, Target
from config import get_settings
from plan import (
    ApiCall,
    BlastRadius,
    Durability,
    ExpectedSignal,
    RemediationPlan,
    Reversibility,
    refuse,
)
from runbooks.base import ExecutionResult, Runbook, registry

log = structlog.get_logger(__name__)

#: The cordon expires on its own. A node that recovered stays cordoned only as long as a human
#: needs to decide about it; the durable fix is a node-pool change, not a forgotten cordon.
CORDON_TTL_SECONDS = 7200


class NodeDrain(Runbook):
    name = "node_drain"
    triggers = ("KubeNodeNotReady",)

    @property
    def chaos_fixture(self) -> str:
        return "flaky-node"

    # --- plan ------------------------------------------------------------------------------------

    def plan(self, bundle: IncidentBundle, target: Target) -> RemediationPlan:
        headroom = get_settings().drain_capacity_headroom

        def no(reason: str) -> RemediationPlan:
            return refuse(
                self.name, bundle.incident_id, reason, target_kind="Node", target_name=target.name
            )

        if target.kind != "Node" or target.node_state is None:
            return no(f"{target.kind} {target.name} is not a node with captured capacity")

        state = target.node_state
        peers = [p for p in target.peer_nodes if p.ready and p.schedulable]
        if target.pods and not peers:
            return no("there is no healthy, schedulable node to move these pods onto")

        free_cpu, free_memory = _free(peers)
        need_cpu = int(state.requested_cpu_millis * headroom)
        need_memory = int(state.requested_memory_mib * headroom)
        if free_cpu < need_cpu or free_memory < need_memory:
            return no(
                f"remaining capacity is too small: moving {state.requested_cpu_millis}m CPU / "
                f"{state.requested_memory_mib}Mi needs {need_cpu}m / {need_memory}Mi free "
                f"({headroom:.0%}) and the {len(peers)} healthy node(s) have {free_cpu}m / "
                f"{free_memory}Mi -- draining would overload them"
            )

        moved = [tuple(entry.split("/", 1)) for entry in target.pods]
        namespaces = tuple(sorted({namespace for namespace, _ in moved}))

        cordon = ApiCall(
            verb="cordon",
            resource="v1/Node",
            name=target.name,
            body={"spec": {"unschedulable": True}},
        )
        evictions = tuple(
            ApiCall(
                verb="evict",
                resource="v1/Pod",
                name=name,
                namespace=namespace,
                subresource="eviction",
            )
            for namespace, name in moved
        )
        uncordon = ApiCall(
            verb="uncordon",
            resource="v1/Node",
            name=target.name,
            body={"spec": {"unschedulable": False}},
        )

        return RemediationPlan(
            runbook=self.name,
            incident_id=bundle.incident_id,
            target_kind="Node",
            target_name=target.name,
            target_namespace=None,
            actions=(cordon, *evictions),
            compensation=(uncordon,),
            reversibility=Reversibility.PARTIAL,
            durability=Durability.EPHEMERAL,
            ttl_seconds=CORDON_TTL_SECONDS,
            blast_radius=BlastRadius(pods=len(moved), nodes=1, namespaces=namespaces),
            expected_signal=_recovery(target.name, namespaces),
            # High: a NotReady node is unambiguous, and capacity was checked rather than assumed.
            confidence=0.90,
            summary=(
                f"cordon {target.name} and evict {len(moved)} pod(s) onto {len(peers)} healthy "
                f"node(s) (need {need_cpu}m / {need_memory}Mi, have {free_cpu}m / {free_memory}Mi)"
            ),
        )

    # --- execute ---------------------------------------------------------------------------------

    async def execute(self, plan: RemediationPlan) -> ExecutionResult:
        """Cordon first, then evict one pod at a time, stopping at the first PDB refusal."""
        from k8s import node_ops, state_capture

        node = plan.target_name
        before = await state_capture.capture_node(node)
        await node_ops.set_unschedulable(node, True)

        evicted: list[str] = []
        gone: list[str] = []
        for call in plan.actions[1:]:
            key = f"{call.namespace}/{call.name}"
            result = await node_ops.evict(call.namespace or "default", call.name)
            if result == "blocked":
                after = await state_capture.capture_node(node)
                held = [f"{c.namespace}/{c.name}" for c in plan.actions[1:]][
                    len(evicted) + len(gone) :
                ]
                # Stop rather than push through: the next pod may belong to the same service.
                return ExecutionResult(
                    False,
                    f"drain stopped by a PodDisruptionBudget at {key}; evicted {len(evicted)}, "
                    f"held back {len(held)} ({', '.join(held)}). {node} stays cordoned -- "
                    f"`kubectl uncordon {node}` once it is healthy",
                    before,
                    after,
                )
            (evicted if result == "evicted" else gone).append(key)

        after = await state_capture.capture_node(node)
        return ExecutionResult(
            True,
            f"cordoned {node} and evicted {len(evicted)} pod(s)"
            + (f"; {len(gone)} were already gone" if gone else ""),
            before,
            after,
        )

    # --- compensate ------------------------------------------------------------------------------

    async def compensate(self, plan: RemediationPlan) -> ExecutionResult:
        """Uncordon. Idempotent. The evictions cannot be undone, and the detail says so."""
        from k8s import node_ops, state_capture

        node = plan.target_name
        before = await state_capture.capture_node(node)
        if before["unschedulable"]:
            await node_ops.set_unschedulable(node, False)
        after = await state_capture.capture_node(node)
        return ExecutionResult(
            True,
            f"uncordoned {node}; the {len(plan.actions) - 1} evicted pod(s) stay where they were "
            "rescheduled -- eviction is the part of a drain that cannot be undone",
            before,
            after,
        )


def _free(peers: list[NodeState]) -> tuple[int, int]:
    return (
        sum(max(p.allocatable_cpu_millis - p.requested_cpu_millis, 0) for p in peers),
        sum(max(p.allocatable_memory_mib - p.requested_memory_mib, 0) for p in peers),
    )


def _recovery(node: str, namespaces: tuple[str, ...]) -> ExpectedSignal:
    """Recovery committed before acting: the work left the node, and it is running elsewhere.

    The `or (up * 0)` arms turn "no series" into 0 only while kube-state-metrics is being scraped.
    A bare `or vector(0)` would read a dead exporter as a perfectly drained node -- a missing
    signal must never become a healthy one.
    """
    ksm_up = 'max(up{job="kube-state-metrics"}) * 0'
    queries = {
        "pods_left_on_node": (
            f'count(kube_pod_info{{node="{node}",created_by_kind!="DaemonSet"}}) or ({ksm_up})'
        ),
    }
    thresholds = {"pods_left_on_node": 0.0}
    if namespaces:
        selector = "|".join(namespaces)
        queries["unavailable_replicas"] = (
            f'sum(kube_deployment_status_replicas_unavailable{{namespace=~"{selector}"}}) '
            f"or ({ksm_up})"
        )
        thresholds["unavailable_replicas"] = 0.0
    return ExpectedSignal(
        # Draining does not make the node Ready, so no alert is promised to clear.
        alert_should_clear="",
        queries=queries,
        thresholds=thresholds,
        window_seconds=300,
    )


registry.register(NodeDrain())

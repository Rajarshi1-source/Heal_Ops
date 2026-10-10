"""Node operations for cordon and drain.

Evictions go through the **Eviction API**, never a plain pod delete. That is what makes the drain
PodDisruptionBudget-aware: the API server refuses an eviction that would breach a PDB with 429, and
the runbook stops and reports which pods were held back rather than forcing them out. A drain that
deletes pods to finish faster has converted "this node is sick" into "this service is down".
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from k8s.client import call, core_v1

EvictionResult = Literal["evicted", "gone", "blocked"]

UNSCHEDULABLE_TAINT = "node.kubernetes.io/unschedulable"
SCHEDULABLE_TIMEOUT_SECONDS = 60.0


async def set_unschedulable(name: str, value: bool) -> Any:
    """Cordon (`True`) or uncordon (`False`). Idempotent: re-patching the same value is a no-op."""
    return await call(
        "cordon" if value else "uncordon",
        core_v1().patch_node,
        name,
        {"spec": {"unschedulable": value}},
    )


async def is_unschedulable(name: str) -> bool:
    node = await call("get_node", core_v1().read_node, name)
    return bool(node.spec.unschedulable)


async def await_schedulable(
    name: str, *, timeout_s: float = SCHEDULABLE_TIMEOUT_SECONDS, interval_s: float = 1.0
) -> bool:
    """Wait until the node genuinely takes pods again; `False` if it still does not at the deadline.

    Uncordoning is two writes by two parties. The PATCH clears `spec.unschedulable`; the node
    lifecycle controller then lifts the `node.kubernetes.io/unschedulable:NoSchedule` taint it
    mirrors from that field, asynchronously. Until it does, the scheduler still keeps pods off the
    node, so a 200 on the PATCH is not an uncordon -- on a busy control plane the gap is seconds.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        node = await call("get_node", core_v1().read_node, name)
        taints = node.spec.taints or []
        if not node.spec.unschedulable and not any(t.key == UNSCHEDULABLE_TAINT for t in taints):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval_s)


async def evict(namespace: str, name: str) -> EvictionResult:
    """Ask the API server to evict one pod, honouring any PodDisruptionBudget."""
    body = k8s_client.V1Eviction(metadata=k8s_client.V1ObjectMeta(name=name, namespace=namespace))
    try:
        await call("evict", core_v1().create_namespaced_pod_eviction, name, namespace, body)
    except ApiException as exc:
        if exc.status == 404:
            return "gone"  # already rescheduled, or deleted by its controller: nothing to do
        if exc.status == 429:
            return "blocked"  # a PDB says this pod's service cannot lose another replica now
        raise
    return "evicted"

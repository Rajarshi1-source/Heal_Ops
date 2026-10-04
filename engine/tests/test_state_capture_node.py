"""The node target builder: the numbers the drain's 120% precheck is computed from.

Two mistakes this pins down: peers reported with nothing requested (which overstates their free
capacity by everything already running there), and DaemonSet or mirror pods counted as moving
(they stay on the node, so counting them inflates what the peers must absorb).
"""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

import pytest

from k8s import state_capture


def node(name: str, *, labels: dict[str, str] | None = None) -> Any:
    return NS(
        metadata=NS(name=name, labels=labels or {}, annotations={}),
        spec=NS(unschedulable=False),
        status=NS(
            conditions=[NS(type="Ready", status="True")],
            allocatable={"cpu": "4", "memory": "8Gi"},
        ),
    )


def pod(name: str, node_name: str, *, cpu: str, memory: str, **extra: Any) -> Any:
    return NS(
        metadata=NS(
            name=name,
            namespace=extra.get("namespace", "shop"),
            annotations=extra.get("annotations", {}),
            owner_references=extra.get("owners", []),
        ),
        spec=NS(
            node_name=node_name,
            containers=[NS(resources=NS(requests={"cpu": cpu, "memory": memory}))],
        ),
        status=NS(phase=extra.get("phase", "Running")),
    )


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = [
        node("worker-3"),
        node("worker-1"),
        node("cp", labels={"node-role.kubernetes.io/control-plane": ""}),
    ]
    pods = [
        pod("api-1", "worker-3", cpu="500m", memory="512Mi"),
        pod("ds-1", "worker-3", cpu="100m", memory="64Mi", owners=[NS(kind="DaemonSet")]),
        pod(
            "static",
            "worker-3",
            cpu="100m",
            memory="64Mi",
            annotations={"kubernetes.io/config.mirror": "x"},
        ),
        pod("done", "worker-3", cpu="900m", memory="900Mi", phase="Succeeded"),
        pod("busy", "worker-1", cpu="1", memory="2Gi"),
    ]

    async def call(verb: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        if verb == "get_node":
            return nodes[0]
        if verb == "list_nodes":
            return NS(items=nodes)
        return NS(items=pods)

    monkeypatch.setattr(state_capture, "call", call)
    monkeypatch.setattr(
        state_capture,
        "core_v1",
        lambda: NS(read_node=None, list_node=None, list_pod_for_all_namespaces=None),
    )


async def test_only_pods_that_move_are_listed_and_counted(cluster: None) -> None:
    target = await state_capture.build_target_for_node("worker-3")

    assert target.pods == ("shop/api-1",)
    assert target.node_state is not None
    assert (target.node_state.requested_cpu_millis, target.node_state.requested_memory_mib) == (
        500,
        512,
    )


async def test_peers_carry_what_is_already_requested_on_them(cluster: None) -> None:
    target = await state_capture.build_target_for_node("worker-3")

    [worker_1] = target.peer_nodes  # the control plane is never a drain destination
    assert worker_1.name == "worker-1"
    assert (worker_1.requested_cpu_millis, worker_1.requested_memory_mib) == (1000, 2048)
    assert worker_1.allocatable_cpu_millis == 4000

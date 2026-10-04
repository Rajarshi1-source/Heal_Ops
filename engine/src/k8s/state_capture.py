"""Before/after state capture, and building the incident bundle's `Target`.

Two consumers with different needs:

* **`capture_*` snapshots** feed `ExecutionResult.state_before` / `state_after`. The dashboard diffs
  them, and the compensation chaos test compares them with `==` -- byte-identical, not "close
  enough". That exactness is what makes auto-execution defensible, so a snapshot must contain the
  fields a fix changes and nothing that drifts on its own (no timestamps, no resourceVersion, no
  observed status counters).

* **`build_target`** produces the `Target` the planner reads. It runs *before* planning so that
  `plan()` can stay a pure function of the bundle -- every cluster read happens here, once.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import structlog
from kubernetes.client.rest import ApiException

from bundle import AutoscalerState, ContainerState, GitOpsManager, NodeState, Target
from k8s import deployment_ops
from k8s.client import call, core_v1, server_version

log = structlog.get_logger(__name__)


# --- quantity parsing ----------------------------------------------------------------------------


def cpu_to_millis(raw: str | None) -> int | None:
    if not raw:
        return None
    text = str(raw)
    if text.endswith("n"):
        return int(float(text[:-1]) / 1_000_000)
    if text.endswith("u"):
        return int(float(text[:-1]) / 1_000)
    if text.endswith("m"):
        return int(float(text[:-1]))
    return int(float(text) * 1000)


_MEMORY_UNITS: dict[str, float] = {
    "Ki": 1 / 1024,
    "Mi": 1,
    "Gi": 1024,
    "Ti": 1024 * 1024,
    "K": 1000 / (1024 * 1024),
    "M": 1_000_000 / (1024 * 1024),
    "G": 1_000_000_000 / (1024 * 1024),
}


def memory_to_mib(raw: str | None) -> int | None:
    if not raw:
        return None
    text = str(raw)
    for suffix, factor in _MEMORY_UNITS.items():
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * factor)
    return int(float(text) / (1024 * 1024))


def resize_policy(container: Any, resource: str) -> str | None:
    for entry in getattr(container, "resize_policy", None) or []:
        if entry.resource_name == resource:
            return str(entry.restart_policy)
    return None


# --- snapshots -----------------------------------------------------------------------------------


def _container_snapshot(container: Any) -> dict[str, Any]:
    resources = container.resources
    requests = (resources.requests or {}) if resources else {}
    limits = (resources.limits or {}) if resources else {}
    return {
        "name": container.name,
        "image": container.image,
        "cpu_request": requests.get("cpu"),
        "cpu_limit": limits.get("cpu"),
        "memory_request": requests.get("memory"),
        "memory_limit": limits.get("memory"),
        "memory_resize_policy": resize_policy(container, "memory"),
        "cpu_resize_policy": resize_policy(container, "cpu"),
    }


async def capture_deployment(namespace: str, name: str) -> dict[str, Any]:
    """Snapshot the fields a remediation can change.

    Deliberately excludes `resourceVersion`, `generation`, timestamps and status counters. Including
    them would make the compensation test's byte-for-byte comparison fail for reasons that have
    nothing to do with whether state was restored.
    """
    deployment = await deployment_ops.read_deployment(namespace, name)
    spec = deployment.spec
    return {
        "kind": "Deployment",
        "namespace": namespace,
        "name": name,
        "replicas": spec.replicas,
        "revision": (deployment.metadata.annotations or {}).get(deployment_ops.REVISION_ANNOTATION),
        "containers": [_container_snapshot(c) for c in spec.template.spec.containers],
        "annotations": {
            k: v
            for k, v in (deployment.metadata.annotations or {}).items()
            # This one changes on every rollout and is not part of the declared state.
            if k != "kubectl.kubernetes.io/last-applied-configuration"
        },
    }


async def capture_pod_resources(namespace: str, name: str) -> dict[str, Any]:
    """Snapshot a pod's *actual* resources from status, not its spec.

    In-place resize is asserted on `status.containerStatuses[].resources`, never on the HTTP 200
    from the patch. This is the whole verification philosophy in miniature: the API accepting a
    request is not the same as the kubelet having applied it.
    """
    pod = await call("get_pod", core_v1().read_namespaced_pod, name, namespace)
    statuses = pod.status.container_statuses or []
    return {
        "kind": "Pod",
        "namespace": namespace,
        "name": name,
        "containers": [
            {
                "name": cs.name,
                "restart_count": cs.restart_count,
                "actual_memory_limit": (
                    (cs.resources.limits or {}).get("memory")
                    if getattr(cs, "resources", None)
                    else None
                ),
                "actual_cpu_limit": (
                    (cs.resources.limits or {}).get("cpu")
                    if getattr(cs, "resources", None)
                    else None
                ),
            }
            for cs in statuses
        ],
        "conditions": [
            {"type": c.type, "status": c.status, "reason": c.reason}
            for c in (pod.status.conditions or [])
            # PodResizePending carries Deferred/Infeasible, which decides whether a resize is
            # worth waiting for or must be abandoned immediately.
            if c.type in ("PodResizePending", "PodResizeInProgress", "Ready")
        ],
    }


async def capture_node(name: str) -> dict[str, Any]:
    node = await call("get_node", core_v1().read_node, name)
    conditions = {c.type: c.status for c in node.status.conditions or []}
    return {
        "kind": "Node",
        "name": name,
        "unschedulable": bool(node.spec.unschedulable),
        "ready": conditions.get("Ready") == "True",
        "taints": sorted(
            ({"key": t.key, "value": t.value, "effect": t.effect} for t in node.spec.taints or []),
            key=lambda t: (t["key"] or "", t["effect"] or ""),
        ),
    }


# --- building the planner's Target ---------------------------------------------------------------


def classify_gitops(labels: dict[str, str], annotations: dict[str, str]) -> GitOpsManager:
    """Which controller, if any, also has write access to this object. Defined in `gitops`."""
    from gitops import classify

    return classify(labels, annotations)


async def build_target_for_deployment(
    namespace: str, name: str, *, node: str | None = None
) -> Target:
    """Every cluster read the planner needs, done once, before planning.

    This is what keeps `plan()` a pure function of the bundle -- and therefore what makes the
    decision eval hermetic and the determinism digest stable.
    """
    deployment = await deployment_ops.read_deployment(namespace, name)
    spec = deployment.spec
    metadata = deployment.metadata
    labels = dict(metadata.labels or {})
    annotations = dict(metadata.annotations or {})

    pods = await call(
        "list_pods",
        core_v1().list_namespaced_pod,
        namespace,
        label_selector=",".join(f"{k}={v}" for k, v in (spec.selector.match_labels or {}).items()),
    )
    pod_names = tuple(sorted(p.metadata.name for p in pods.items))

    # Restarts per container name, taken as the worst case across pods. The CrashLoop runbook needs
    # to know a container is restarting; which replica is worst does not change the decision.
    restarts_by_container: dict[str, int] = {}
    for pod in pods.items:
        for cs in pod.status.container_statuses or []:
            restarts_by_container[cs.name] = max(
                restarts_by_container.get(cs.name, 0), cs.restart_count or 0
            )

    containers = tuple(
        ContainerState(
            name=c.name,
            image=c.image,
            cpu_request_millis=cpu_to_millis((c.resources.requests or {}).get("cpu"))
            if c.resources
            else None,
            cpu_limit_millis=cpu_to_millis((c.resources.limits or {}).get("cpu"))
            if c.resources
            else None,
            memory_request_mib=memory_to_mib((c.resources.requests or {}).get("memory"))
            if c.resources
            else None,
            memory_limit_mib=memory_to_mib((c.resources.limits or {}).get("memory"))
            if c.resources
            else None,
            restart_count=restarts_by_container.get(c.name, 0),
            memory_resize_policy=resize_policy(c, "memory"),
            cpu_resize_policy=resize_policy(c, "cpu"),
        )
        for c in spec.template.spec.containers
    )

    autoscaler: AutoscalerState | None = None
    try:
        hpa = await deployment_ops.find_autoscaler(namespace, name)
    except ApiException:
        # Cannot see HPAs. Left as None, and the CPU runbook refuses rather than risk fighting an
        # autoscaler it cannot observe.
        hpa = None
    if hpa is not None:
        autoscaler = AutoscalerState(
            name=hpa.metadata.name,
            metric=deployment_ops.autoscaler_metric(hpa),  # type: ignore[arg-type]
            target_utilization=deployment_ops.autoscaler_target_utilization(hpa),
            min_replicas=hpa.spec.min_replicas or 1,
            max_replicas=hpa.spec.max_replicas,
            current_replicas=(hpa.status.current_replicas if hpa.status else 0) or 0,
        )

    revisions = await deployment_ops.list_revisions(namespace, name)
    current = revisions[0].revision if revisions else None
    prior = revisions[1] if len(revisions) > 1 else None

    version = await server_version()

    return Target(
        kind="Deployment",
        name=name,
        namespace=namespace,
        labels=labels,
        annotations=annotations,
        replicas=spec.replicas or 0,
        ready_replicas=(deployment.status.ready_replicas if deployment.status else 0) or 0,
        containers=containers,
        pods=pod_names,
        node=node,
        autoscaler=autoscaler,
        gitops_manager=classify_gitops(labels, annotations),
        revision=current,
        previous_revision=prior.revision if prior else None,
        previous_image=prior.images[0] if prior and prior.images else None,
        server_supports_in_place_resize=version.supports_in_place_resize,
    )


async def build_target_for_node(name: str) -> Target:
    """Node target, including peer capacity for the drain precheck."""
    node = await call("get_node", core_v1().read_node, name)
    all_nodes = await call("list_nodes", core_v1().list_node)

    def to_state(obj: Any) -> NodeState:
        conditions = {c.type: c.status for c in obj.status.conditions or []}
        allocatable = obj.status.allocatable or {}
        return NodeState(
            name=obj.metadata.name,
            ready=conditions.get("Ready") == "True",
            schedulable=not bool(obj.spec.unschedulable),
            allocatable_cpu_millis=cpu_to_millis(allocatable.get("cpu")) or 0,
            allocatable_memory_mib=memory_to_mib(allocatable.get("memory")) or 0,
        )

    # Every pod in the cluster, once: the precheck needs what is already requested on the PEERS, not
    # only on this node, or their free capacity is overstated by everything already running there.
    pods = await call("list_pods", core_v1().list_pod_for_all_namespaces)
    requested: dict[str, tuple[int, int]] = {}
    drainable: list[str] = []
    for pod in pods.items:
        node_name = pod.spec.node_name
        if not node_name or not _occupies_node(pod):
            continue
        cpu, memory = _pod_requests(pod)
        prior_cpu, prior_memory = requested.get(node_name, (0, 0))
        requested[node_name] = (prior_cpu + cpu, prior_memory + memory)
        if node_name == name and _moves_on_drain(pod):
            drainable.append(f"{pod.metadata.namespace}/{pod.metadata.name}")

    def with_requests(state: NodeState) -> NodeState:
        cpu, memory = requested.get(state.name, (0, 0))
        return replace(state, requested_cpu_millis=cpu, requested_memory_mib=memory)

    # This node's requests count only what a drain would move: DaemonSet and mirror pods stay.
    moved_cpu = moved_memory = 0
    for pod in pods.items:
        if pod.spec.node_name == name and _occupies_node(pod) and _moves_on_drain(pod):
            cpu, memory = _pod_requests(pod)
            moved_cpu += cpu
            moved_memory += memory

    state = to_state(node)
    return Target(
        kind="Node",
        name=name,
        namespace=None,
        labels=dict(node.metadata.labels or {}),
        annotations=dict(node.metadata.annotations or {}),
        node=name,
        pods=tuple(sorted(drainable)),
        node_state=replace(
            state,
            requested_cpu_millis=moved_cpu,
            requested_memory_mib=moved_memory,
            pod_count=len(drainable),
        ),
        peer_nodes=tuple(
            with_requests(to_state(n))
            for n in all_nodes.items
            if n.metadata.name != name
            and "node-role.kubernetes.io/control-plane" not in (n.metadata.labels or {})
        ),
    )


def _occupies_node(pod: Any) -> bool:
    """Finished pods hold no resources and have nothing to reschedule."""
    return (pod.status.phase if pod.status else None) not in ("Succeeded", "Failed")


def _moves_on_drain(pod: Any) -> bool:
    """DaemonSet pods are recreated on the same node and mirror pods belong to the kubelet."""
    if (pod.metadata.annotations or {}).get("kubernetes.io/config.mirror"):
        return False
    return not any(ref.kind == "DaemonSet" for ref in pod.metadata.owner_references or [])


def _pod_requests(pod: Any) -> tuple[int, int]:
    cpu = memory = 0
    for container in pod.spec.containers:
        requests = (container.resources.requests or {}) if container.resources else {}
        cpu += cpu_to_millis(requests.get("cpu")) or 0
        memory += memory_to_mib(requests.get("memory")) or 0
    return cpu, memory

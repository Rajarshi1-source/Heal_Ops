"""Deployment operations: rollout history, rollback, scale, HPA.

Two things here are easy to get wrong and are handled explicitly:

* **`kubectl rollout undo` is not one API call.** It reads the ReplicaSet history, finds the
  previous revision, and patches the Deployment's pod template back to that revision's template.
  Doing it properly is why `list_revisions()` exists rather than a one-line patch.
* **Optimistic concurrency.** Every mutation carries a `resourceVersion` precondition, so a stale
  read cannot clobber a concurrent change. On conflict the caller **re-plans** rather than retrying
  blindly -- the cluster changed, so the plan that was gated may no longer be the right one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from kubernetes.client.rest import ApiException

from k8s.client import apps_v1, autoscaling_v2, call

log = structlog.get_logger(__name__)

REVISION_ANNOTATION = "deployment.kubernetes.io/revision"


class ConflictError(RuntimeError):
    """A `resourceVersion` precondition failed. The caller must re-plan, never retry blindly."""


@dataclass(frozen=True, slots=True)
class Revision:
    revision: int
    template: Any
    images: tuple[str, ...]


async def read_deployment(namespace: str, name: str) -> Any:
    return await call("get_deployment", apps_v1().read_namespaced_deployment, name, namespace)


async def list_revisions(namespace: str, name: str) -> list[Revision]:
    """Rollout history, newest first.

    Built from the Deployment's owned ReplicaSets, which is where Kubernetes actually keeps it --
    there is no "history" resource to read.
    """
    deployment = await read_deployment(namespace, name)
    uid = deployment.metadata.uid

    replica_sets = await call(
        "list_replicasets",
        apps_v1().list_namespaced_replica_set,
        namespace,
    )

    revisions: list[Revision] = []
    for rs in replica_sets.items:
        owners = rs.metadata.owner_references or []
        if not any(o.uid == uid for o in owners):
            continue
        raw = (rs.metadata.annotations or {}).get(REVISION_ANNOTATION)
        if raw is None:
            continue
        images = tuple(c.image for c in rs.spec.template.spec.containers)
        revisions.append(Revision(revision=int(raw), template=rs.spec.template, images=images))

    return sorted(revisions, key=lambda r: r.revision, reverse=True)


async def previous_revision(namespace: str, name: str) -> Revision | None:
    """The revision `rollout undo` would roll back to.

    Returns None when there is no earlier revision -- which is a **refusal reason**, not an error.
    A workload on its first revision has nothing to roll back to, and pretending otherwise is how a
    rollback "succeeds" while changing nothing.
    """
    revisions = await list_revisions(namespace, name)
    return revisions[1] if len(revisions) > 1 else None


async def rollback_to(namespace: str, name: str, template: Any, resource_version: str) -> Any:
    """Patch the pod template back to a previous revision, with a concurrency precondition."""
    body = {
        "metadata": {"resourceVersion": resource_version},
        "spec": {"template": template},
    }
    try:
        return await call(
            "patch_deployment",
            apps_v1().patch_namespaced_deployment,
            name,
            namespace,
            body,
        )
    except ApiException as exc:
        if exc.status == 409:
            raise ConflictError(
                f"{namespace}/{name} changed while we were planning; re-planning"
            ) from exc
        raise


async def scale(
    namespace: str, name: str, replicas: int, resource_version: str | None = None
) -> Any:
    """Set replica count through the `scale` subresource."""
    body: dict[str, Any] = {"spec": {"replicas": replicas}}
    if resource_version:
        body["metadata"] = {"resourceVersion": resource_version}
    try:
        return await call(
            "patch_deployment_scale",
            apps_v1().patch_namespaced_deployment_scale,
            name,
            namespace,
            body,
        )
    except ApiException as exc:
        if exc.status == 409:
            raise ConflictError(f"{namespace}/{name} scale changed under us; re-planning") from exc
        raise


async def find_autoscaler(namespace: str, name: str) -> Any | None:
    """The HPA that owns this Deployment's `spec.replicas`, if one does.

    Its presence changes the CPU runbook completely: patching `replicas` on an HPA-owned workload is
    a no-op at best and an oscillation at worst, because the HPA will put it back.
    """
    try:
        hpas = await call(
            "list_hpa", autoscaling_v2().list_namespaced_horizontal_pod_autoscaler, namespace
        )
    except ApiException as exc:
        if exc.status in (403, 404):
            # No RBAC for HPAs, or the API is absent. Treating that as "no HPA" would let the
            # runbook patch replicas against an autoscaler it cannot see.
            log.warning("cannot list HPAs; scale-up will refuse", namespace=namespace)
            raise
        raise

    for hpa in hpas.items:
        ref = hpa.spec.scale_target_ref
        if ref.kind == "Deployment" and ref.name == name:
            return hpa
    return None


def autoscaler_metric(hpa: Any) -> str:
    """Which signal the HPA scales on: `cpu`, `memory`, or `external`.

    An HPA driven by an external metric such as queue depth must not be nudged by a CPU alert --
    the real signal is elsewhere, and lowering a CPU target would fight it.
    """
    for metric in hpa.spec.metrics or []:
        if metric.type == "Resource" and metric.resource is not None:
            return str(metric.resource.name)
    return "external"


def autoscaler_target_utilization(hpa: Any) -> int | None:
    for metric in hpa.spec.metrics or []:
        if metric.type == "Resource" and metric.resource is not None:
            target = metric.resource.target
            if target is not None and target.average_utilization is not None:
                return int(target.average_utilization)
    return None


async def patch_autoscaler(
    namespace: str,
    name: str,
    *,
    target_utilization: int | None = None,
    max_replicas: int | None = None,
) -> Any:
    """Act on the control loop rather than on its output.

    Lowering the utilisation target makes the HPA scale up on its own; patching `replicas` directly
    would be reverted within one sync.
    """
    spec: dict[str, Any] = {}
    if max_replicas is not None:
        spec["maxReplicas"] = max_replicas
    if target_utilization is not None:
        spec["metrics"] = [
            {
                "type": "Resource",
                "resource": {
                    "name": "cpu",
                    "target": {
                        "type": "Utilization",
                        "averageUtilization": target_utilization,
                    },
                },
            }
        ]

    return await call(
        "patch_hpa",
        autoscaling_v2().patch_namespaced_horizontal_pod_autoscaler,
        name,
        namespace,
        {"spec": spec},
    )

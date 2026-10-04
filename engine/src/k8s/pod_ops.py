"""Pod operations for the in-place memory resize.

Two rules this module exists to enforce:

* **Mutation goes through the `resize` subresource**, never the pod spec. The typed client lags new
  subresources, so the PATCH is sent through the generic `call_api` path; the client minor is pinned
  to the cluster minor so the rest of the surface matches.
* **What the kubelet applied is read from `status.containerStatuses[].resources`**, never inferred
  from the PATCH returning 200. The API server accepting a resize is not the node having made room
  for it -- `PodResizePending: Infeasible` is exactly that gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from k8s.client import call, call_api, core_v1


@dataclass(frozen=True, slots=True)
class ResizeCondition:
    """`PodResizePending` / `PodResizeInProgress`, if the pod carries either."""

    type: str
    reason: str | None
    message: str | None

    @property
    def infeasible(self) -> bool:
        """The node can never satisfy this. Waiting is pointless, so the attempt ends now."""
        return self.type == "PodResizePending" and self.reason == "Infeasible"


async def read_pod(namespace: str, name: str) -> Any:
    return await call("get_pod", core_v1().read_namespaced_pod, name, namespace)


async def resize(namespace: str, name: str, body: dict[str, Any]) -> Any:
    """PATCH `/api/v1/namespaces/{ns}/pods/{name}/resize`."""
    return await call_api(
        "resize_pod",
        f"/api/v1/namespaces/{namespace}/pods/{name}/resize",
        "PATCH",
        body=body,
        content_type="application/strategic-merge-patch+json",
    )


def _status(pod: Any, container: str) -> Any | None:
    for status in (pod.status.container_statuses or []) if pod.status else []:
        if status.name == container:
            return status
    return None


def actual_memory_limit(pod: Any, container: str) -> str | None:
    """The memory limit the kubelet reports as applied -- the only number that counts."""
    status = _status(pod, container)
    resources = getattr(status, "resources", None) if status is not None else None
    if resources is None or not resources.limits:
        return None
    value = resources.limits.get("memory")
    return str(value) if value is not None else None


def restart_count(pod: Any, container: str) -> int | None:
    status = _status(pod, container)
    return int(status.restart_count or 0) if status is not None else None


def resize_condition(pod: Any) -> ResizeCondition | None:
    for condition in (pod.status.conditions or []) if pod.status else []:
        if condition.type in ("PodResizePending", "PodResizeInProgress"):
            return ResizeCondition(condition.type, condition.reason, condition.message)
    return None

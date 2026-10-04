"""Runbook 4 -- OOM memory bump, in place.

In-place pod resize went GA in Kubernetes 1.35, and that changed this runbook from "patch the
template and restart" into a non-disruptive, reversible, auto-eligible fix:

    PATCH /api/v1/namespaces/{ns}/pods/{pod}/resize      -> cgroup limit raised, no restart

Five details make it credible under questioning, and each is enforced here:

1. **Mutation goes through the `resize` subresource**, through the generic `call_api` path because
   the typed client lags new subresources.
2. **Success is asserted on `status.containerStatuses[].resources`**, never on the HTTP 200.
3. **`PodResizePending: Infeasible` ends the attempt immediately** -- the node cannot fit it, and
   waiting would be an unbounded wait masquerading as progress.
4. **`resizePolicy: RestartContainer` for memory is a refusal**, or "no restart" would be a lie.
5. **The QoS class is preserved.** A resize may not change it, so a Guaranteed pod has request and
   limit raised together and a Burstable pod has only its limit raised.

This is stage 1 of the two-stage fix: the mitigation is ephemeral and expires; the durable change to
the template is a pull request opened only after verification proves the bump held (Phase 6).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from kubernetes.client.rest import ApiException

from bundle import ContainerState, IncidentBundle, Target
from config import Settings, get_settings
from plan import (
    ApiCall,
    BlastRadius,
    Durability,
    ExpectedSignal,
    RemediationPlan,
    Reversibility,
    refuse,
)
from runbooks.base import ExecutionResult, Runbook, SignalWindow, registry

log = structlog.get_logger(__name__)

#: The new limit must clear the observed peak by this much, or the next spike OOMs again.
PEAK_HEADROOM = 1.25

#: Rupees per GiB-month of memory, from the same pinned price list as the CPU runbook. A constant so
#: the decision eval stays deterministic.
INR_PER_GIB_MONTH = 300.0

#: The mitigation returns on its own. A permanently larger limit is a sizing decision, and that
#: belongs in the template, in review.
RESIZE_TTL_SECONDS = 7200

#: How often the kubelet's applied resources are re-read while waiting for a resize.
RESIZE_POLL_SECONDS = 2.0


class OomMemoryBump(Runbook):
    name = "oom_memory_bump"
    triggers = ("KubePodOOMKilled",)

    @property
    def chaos_fixture(self) -> str:
        return "hungry-worker"

    # --- plan ------------------------------------------------------------------------------------

    def plan(self, bundle: IncidentBundle, target: Target) -> RemediationPlan:
        settings = get_settings()
        pod = bundle.cause_alert.pod

        def no(reason: str) -> RemediationPlan:
            return refuse(
                self.name,
                bundle.incident_id,
                reason,
                target_kind="Pod" if pod else target.kind,
                target_name=pod or target.name,
                target_namespace=target.namespace,
            )

        if target.kind != "Deployment":
            return no(f"{target.kind} is not handled by this runbook")
        if not pod or not target.namespace:
            return no("the alert names no pod, so there is nothing to resize")

        container = _container(target, bundle.cause_alert.container)
        if container is None:
            return no(
                f"cannot tell which container in {pod} was OOM killed "
                f"(alert says {bundle.cause_alert.container!r})"
            )

        current = container.memory_limit_mib
        if current is None:
            # No limit means the container was killed by node pressure, not by its own cgroup.
            # Adding a limit is a sizing decision, not a resize.
            return no(
                f"{container.name} has no memory limit to raise; this needs a sizing decision"
            )

        new = _new_limit(current, container.memory_peak_mib, settings)
        if new <= current:
            return no(
                f"{container.name} is already at the {settings.memory_ceiling_mib}Mi ceiling -- "
                "this needs a code fix, not more memory"
            )

        guaranteed = container.memory_request_mib == current
        if not target.server_supports_in_place_resize:
            return self._template_plan(bundle, target, container, current, new, guaranteed)

        if container.memory_resize_policy == "RestartContainer":
            return no(
                f"{container.name} declares resizePolicy RestartContainer for memory, so an "
                "in-place bump would restart it -- escalating instead of claiming no restart"
            )

        def resize(mib: int, request_mib: int | None) -> ApiCall:
            return ApiCall(
                verb="resize",
                resource="v1/Pod",
                name=pod,
                namespace=target.namespace,
                subresource="resize",
                body=_resources_body(container.name, mib, request_mib if guaranteed else None),
            )

        return RemediationPlan(
            runbook=self.name,
            incident_id=bundle.incident_id,
            target_kind="Pod",
            target_name=pod,
            target_namespace=target.namespace,
            actions=(resize(new, new),),
            compensation=(resize(current, container.memory_request_mib),),
            reversibility=Reversibility.REVERSIBLE,
            durability=Durability.EPHEMERAL,
            ttl_seconds=RESIZE_TTL_SECONDS,
            blast_radius=BlastRadius(pods=1, namespaces=(target.namespace,)),
            expected_signal=ExpectedSignal(
                alert_should_clear="KubePodOOMKilled", window_seconds=600
            ),
            confidence=_confidence(container),
            summary=(
                f"raise {target.namespace}/{pod} {container.name} memory {current}Mi -> {new}Mi "
                "in place (no restart)"
            ),
            cost_delta_inr_month=_cost(current, new),
        )

    def _template_plan(
        self,
        bundle: IncidentBundle,
        target: Target,
        container: ContainerState,
        current: int,
        new: int,
        guaranteed: bool,
    ) -> RemediationPlan:
        """Below 1.35 the only way up is the template, and that is a rolling restart.

        Marked partially reversible because the restarts it causes cannot be undone -- which is what
        sends it to a human through policy rather than through anything hardcoded here.
        """

        def patch(mib: int, request_mib: int | None) -> ApiCall:
            return ApiCall(
                verb="patch",
                resource="apps/v1/Deployment",
                name=target.name,
                namespace=target.namespace,
                body={
                    "spec": {
                        "template": _resources_body(
                            container.name, mib, request_mib if guaranteed else None
                        )
                    }
                },
            )

        return RemediationPlan(
            runbook=self.name,
            incident_id=bundle.incident_id,
            target_kind="Deployment",
            target_name=target.name,
            target_namespace=target.namespace,
            actions=(patch(new, new),),
            compensation=(patch(current, container.memory_request_mib),),
            reversibility=Reversibility.PARTIAL,
            durability=Durability.EPHEMERAL,
            ttl_seconds=RESIZE_TTL_SECONDS,
            blast_radius=BlastRadius(pods=target.replicas, namespaces=(target.namespace or "",)),
            expected_signal=ExpectedSignal(
                alert_should_clear="KubePodOOMKilled", window_seconds=600
            ),
            confidence=_confidence(container),
            summary=(
                f"raise {target.namespace}/{target.name} {container.name} memory {current}Mi -> "
                f"{new}Mi via the template (cluster predates in-place resize; pods restart)"
            ),
            cost_delta_inr_month=_cost(current, new) * max(target.replicas, 1),
        )

    # --- execute ---------------------------------------------------------------------------------

    async def execute(self, plan: RemediationPlan) -> ExecutionResult:
        action = plan.actions[0]
        if action.subresource != "resize":
            return await self._patch_template(action)

        from k8s import pod_ops

        namespace, pod = action.namespace or "default", action.name
        container, wanted = _wanted(action)
        before = _snapshot(await pod_ops.read_pod(namespace, pod), container)
        if before["memory_limit_mib"] == wanted:
            return ExecutionResult(True, f"already at {wanted}Mi; nothing to do", before, before)

        await pod_ops.resize(namespace, pod, action.body)
        applied, after, why = await _await_applied(namespace, pod, container, wanted)

        if not applied:
            # Withdraw the desired change so a Deferred resize cannot land later, unannounced,
            # after this attempt has already been reported as failed.
            await _best_effort_withdraw(plan, namespace, pod)
            return ExecutionResult(False, f"resize not applied: {why}", before, after)

        restarts = (before["restart_count"], after["restart_count"])
        detail = (
            f"raised {container} memory {before['memory_limit_mib']}Mi -> {wanted}Mi in place; "
            + (
                f"restartCount unchanged at {restarts[0]}"
                if restarts[0] == restarts[1]
                else f"restartCount moved {restarts[0]} -> {restarts[1]}"
            )
        )
        return ExecutionResult(True, detail, before, after)

    async def _patch_template(self, action: ApiCall) -> ExecutionResult:
        from k8s import deployment_ops, state_capture

        namespace = action.namespace or "default"
        before = await state_capture.capture_deployment(namespace, action.name)
        await deployment_ops.patch(namespace, action.name, action.body)
        after = await state_capture.capture_deployment(namespace, action.name)
        return ExecutionResult(True, "patched the template memory limit", before, after)

    # --- compensate ------------------------------------------------------------------------------

    async def compensate(self, plan: RemediationPlan) -> ExecutionResult:
        """Resize back to the exact prior limit. Idempotent, and safe if the pod is gone."""
        undo = plan.compensation[0]
        if undo.subresource != "resize":
            return await self._patch_template(undo)

        from k8s import pod_ops

        namespace, pod = undo.namespace or "default", undo.name
        container, original = _wanted(undo)
        try:
            current = await pod_ops.read_pod(namespace, pod)
        except ApiException as exc:
            if exc.status == 404:
                # Its replacement was built from the template, which this resize never touched, so
                # the declared limit is already back. Nothing to restore.
                return ExecutionResult(
                    True, f"{pod} no longer exists; its replacement runs the template limit"
                )
            raise

        before = _snapshot(current, container)
        if before["memory_limit_mib"] == original:
            return ExecutionResult(
                True, f"already at {original}Mi; nothing to compensate", before, before
            )

        await pod_ops.resize(namespace, pod, undo.body)
        applied, after, why = await _await_applied(namespace, pod, container, original)
        if not applied:
            return ExecutionResult(False, f"could not resize back: {why}", before, after)
        return ExecutionResult(True, f"resized {container} back to {original}Mi", before, after)

    # --- verification ----------------------------------------------------------------------------

    async def extra_verification(self, plan: RemediationPlan, after: SignalWindow) -> str | None:
        """The generic signals cannot see whether the resize is still the thing keeping it alive.

        If the pod was replaced, its successor runs the template's old limit and the next OOM is a
        matter of time -- that is a failure of this fix even while every graph looks calm.
        """
        action = plan.actions[0]
        if action.subresource != "resize":
            return None

        from k8s import pod_ops
        from k8s.client import KubernetesUnavailable

        container, wanted = _wanted(action)
        try:
            pod = await pod_ops.read_pod(action.namespace or "default", action.name)
        except ApiException as exc:
            if exc.status == 404:
                return f"{action.name} was replaced, so the in-place resize no longer applies"
            return None
        except KubernetesUnavailable:
            return None  # cannot tell; the generic signals still decide

        actual = _mib(pod_ops.actual_memory_limit(pod, container))
        if actual != wanted:
            return f"resize no longer in effect: {container} is at {actual}Mi, not {wanted}Mi"
        return None


# --- helpers --------------------------------------------------------------------------------------


def _container(target: Target, name: str | None) -> ContainerState | None:
    if name:
        return next((c for c in target.containers if c.name == name), None)
    return target.containers[0] if len(target.containers) == 1 else None


def _new_limit(current: int, peak: float | None, settings: Settings) -> int:
    bumped = int(current * settings.memory_bump_factor)
    from_peak = int(peak * PEAK_HEADROOM) if peak else 0
    return min(max(bumped, from_peak), settings.memory_ceiling_mib)


def _confidence(container: ContainerState) -> float:
    """Lower without an observed peak: the bump is then a guess, and policy asks a human."""
    return 0.90 if container.memory_peak_mib else 0.75


def _cost(current: int, new: int) -> float:
    return round((new - current) / 1024 * INR_PER_GIB_MONTH, 2)


def _resources_body(container: str, limit_mib: int, request_mib: int | None) -> dict[str, Any]:
    resources: dict[str, Any] = {"limits": {"memory": f"{limit_mib}Mi"}}
    if request_mib is not None:
        resources["requests"] = {"memory": f"{request_mib}Mi"}
    return {"spec": {"containers": [{"name": container, "resources": resources}]}}


def _wanted(call: ApiCall) -> tuple[str, int]:
    spec = call.body["spec"]
    containers = (
        spec["containers"] if "containers" in spec else spec["template"]["spec"]["containers"]
    )
    entry = containers[0]
    return str(entry["name"]), int(str(entry["resources"]["limits"]["memory"]).removesuffix("Mi"))


def _mib(raw: str | None) -> int | None:
    from k8s.state_capture import memory_to_mib

    return memory_to_mib(raw)


def _snapshot(pod: Any, container: str) -> dict[str, Any]:
    from k8s import pod_ops

    return {
        "pod": pod.metadata.name,
        "container": container,
        "memory_limit_mib": _mib(pod_ops.actual_memory_limit(pod, container)),
        "restart_count": pod_ops.restart_count(pod, container),
    }


async def _await_applied(
    namespace: str, pod: str, container: str, wanted: int
) -> tuple[bool, dict[str, Any], str]:
    """Wait for the kubelet to report the new limit, bounded, and give up at once if Infeasible."""
    from k8s import pod_ops

    deadline = asyncio.get_running_loop().time() + get_settings().resize_wait_seconds
    why = f"not applied within {get_settings().resize_wait_seconds}s"
    while True:
        current = await pod_ops.read_pod(namespace, pod)
        snapshot = _snapshot(current, container)
        if snapshot["memory_limit_mib"] == wanted:
            return True, snapshot, "applied"

        condition = pod_ops.resize_condition(current)
        if condition is not None and condition.infeasible:
            return (
                False,
                snapshot,
                f"PodResizePending Infeasible ({condition.message or 'node cannot fit it'}); "
                "the template change needs approval instead",
            )
        if condition is not None:
            why = f"{condition.type} {condition.reason or ''} after the wait".strip()

        if asyncio.get_running_loop().time() >= deadline:
            return False, snapshot, why
        await asyncio.sleep(RESIZE_POLL_SECONDS)


async def _best_effort_withdraw(plan: RemediationPlan, namespace: str, pod: str) -> None:
    from k8s import pod_ops

    try:
        await pod_ops.resize(namespace, pod, plan.compensation[0].body)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not withdraw the pending resize", pod=pod, error=str(exc))


registry.register(OomMemoryBump())

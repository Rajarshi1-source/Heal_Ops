"""Runbook 3 -- CPU scale-up.

This runbook is the corrected version of defect D4. The naive form -- "scale replicas from N to
N+2" -- has three failure modes, and all three are addressed here:

1. **It fights an HPA.** If a HorizontalPodAutoscaler owns `spec.replicas`, patching replicas is a
   no-op at best and an oscillation at worst. So the runbook acts on the **control loop, not its
   output**: it lowers the HPA's utilisation target and raises `maxReplicas` when that is the
   binding constraint.
2. **It has no ceiling.** Repeated firings walk replicas up indefinitely, and a CPU alert becomes a
   cost incident. `MAX_REPLICAS_CEILING` and `MAX_SCALE_FACTOR` bound every action.
3. **It ignores cost.** Projected spend is computed and carried on the plan, where the policy gate
   turns it into an approval requirement above a threshold.

An HPA driven by an external metric -- queue depth, say -- is a **refusal**: the real signal is
elsewhere, and lowering a CPU target would fight it.
"""

from __future__ import annotations

import structlog

from bundle import IncidentBundle, Target
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

#: How far an HPA's utilisation target is lowered in one step, in percentage points.
HPA_TARGET_STEP = 15
#: Never drive a utilisation target below this; lower would keep the workload permanently oversized.
HPA_TARGET_FLOOR = 50

#: Rupees per month for one replica of a small workload, from a pinned price list. Deliberately a
#: constant rather than a live pricing API: the number needs to be reproducible in the decision
#: eval, and an approximate figure that gates on the right side is worth more than an exact one
#: that makes planning non-deterministic.
INR_PER_REPLICA_MONTH = 420.0

#: Scale-ups expire on their own. Load spikes end, and automation that leaves a workload
#: permanently oversized has quietly made a capacity decision nobody reviewed.
SCALE_TTL_SECONDS = 7200


class CpuScaleUp(Runbook):
    name = "cpu_scale_up"
    triggers = ("HighPodCPU",)

    @property
    def chaos_fixture(self) -> str:
        return "cpu-burner"

    # --- plan ------------------------------------------------------------------------------------

    def plan(self, bundle: IncidentBundle, target: Target) -> RemediationPlan:
        settings = get_settings()

        if target.kind != "Deployment":
            return refuse(
                self.name,
                bundle.incident_id,
                f"{target.kind} is not scalable by this runbook",
                target_kind=target.kind,
                target_name=target.name,
                target_namespace=target.namespace,
            )

        hpa = target.autoscaler
        if hpa is not None:
            return self._plan_via_hpa(bundle, target, hpa)
        return self._plan_via_replicas(bundle, target, settings.max_replicas_ceiling)

    def _plan_via_hpa(self, bundle: IncidentBundle, target: Target, hpa: object) -> RemediationPlan:
        from bundle import AutoscalerState

        assert isinstance(hpa, AutoscalerState)

        if hpa.metric == "external":
            # The workload scales on something we are not looking at. Nudging a CPU target here
            # would fight the signal that actually drives it.
            return refuse(
                self.name,
                bundle.incident_id,
                f"HPA {hpa.name} scales on an external metric, so CPU-based intervention would "
                "fight the real signal -- this needs whoever owns that metric",
                target_kind=target.kind,
                target_name=target.name,
                target_namespace=target.namespace,
            )

        at_max = hpa.current_replicas >= hpa.max_replicas
        current_target = hpa.target_utilization

        if at_max and hpa.max_replicas >= get_settings().max_replicas_ceiling:
            return refuse(
                self.name,
                bundle.incident_id,
                f"HPA {hpa.name} is already at maxReplicas={hpa.max_replicas}, which is at the "
                f"ceiling of {get_settings().max_replicas_ceiling}. More replicas is not the "
                "answer -- this is a capacity or efficiency decision, not an incident",
                target_kind=target.kind,
                target_name=target.name,
                target_namespace=target.namespace,
            )

        new_max = hpa.max_replicas
        new_target: int | None = current_target
        changes: list[str] = []

        if at_max:
            # maxReplicas is the binding constraint, so lowering the target would achieve nothing.
            new_max = min(hpa.max_replicas + 2, get_settings().max_replicas_ceiling)
            changes.append(f"maxReplicas {hpa.max_replicas} -> {new_max}")
        elif current_target is not None:
            new_target = max(HPA_TARGET_FLOOR, current_target - HPA_TARGET_STEP)
            if new_target == current_target:
                return refuse(
                    self.name,
                    bundle.incident_id,
                    f"HPA {hpa.name} target is already at the floor of {HPA_TARGET_FLOOR}% and it "
                    "is not replica-bound; lowering further would keep the workload permanently "
                    "oversized",
                    target_kind=target.kind,
                    target_name=target.name,
                    target_namespace=target.namespace,
                )
            changes.append(f"averageUtilization {current_target}% -> {new_target}%")
        else:
            return refuse(
                self.name,
                bundle.incident_id,
                f"HPA {hpa.name} exposes no CPU utilisation target to adjust",
                target_kind=target.kind,
                target_name=target.name,
                target_namespace=target.namespace,
            )

        # Only the fields that actually change are carried, so the plan reads as the exact patch a
        # reviewer would apply by hand rather than a full object with one edit buried in it.
        body: dict[str, int] = {}
        undo_body: dict[str, int] = {}
        if new_max != hpa.max_replicas:
            body["max_replicas"] = new_max
            undo_body["max_replicas"] = hpa.max_replicas
        if new_target is not None and current_target is not None and new_target != current_target:
            body["target_utilization"] = new_target
            undo_body["target_utilization"] = current_target

        action = ApiCall(
            verb="hpa_patch",
            resource="autoscaling/v2/HorizontalPodAutoscaler",
            name=hpa.name,
            namespace=target.namespace,
            body=body,
        )
        compensation = ApiCall(
            verb="hpa_patch",
            resource="autoscaling/v2/HorizontalPodAutoscaler",
            name=hpa.name,
            namespace=target.namespace,
            body=undo_body,
        )

        headroom = max(new_max - hpa.current_replicas, 0)
        return RemediationPlan(
            runbook=self.name,
            incident_id=bundle.incident_id,
            target_kind=target.kind,
            target_name=target.name,
            target_namespace=target.namespace,
            actions=(action,),
            compensation=(compensation,),
            reversibility=Reversibility.REVERSIBLE,
            durability=Durability.EPHEMERAL,
            ttl_seconds=SCALE_TTL_SECONDS,
            blast_radius=BlastRadius(
                pods=headroom,
                namespaces=(target.namespace,) if target.namespace else (),
                user_facing=_is_user_facing(target),
            ),
            expected_signal=_cpu_signal(target),
            confidence=_confidence(target, via_hpa=True),
            summary=(
                f"adjust HPA {hpa.name} for {target.namespace}/{target.name}: "
                + ", ".join(changes)
                + " (the HPA owns spec.replicas, so patching replicas would be reverted)"
            ),
            cost_delta_inr_month=headroom * INR_PER_REPLICA_MONTH,
        )

    def _plan_via_replicas(
        self, bundle: IncidentBundle, target: Target, ceiling: int
    ) -> RemediationPlan:
        settings = get_settings()
        current = target.replicas

        desired = min(
            int(current * settings.max_scale_factor),
            current + 2,
            ceiling,
        )

        if desired <= current:
            return refuse(
                self.name,
                bundle.incident_id,
                f"already at the replica ceiling ({ceiling}) -- this is a capacity decision, "
                "not an incident, and more replicas will not fix a workload that needs more "
                "efficiency",
                target_kind=target.kind,
                target_name=target.name,
                target_namespace=target.namespace,
            )

        added = desired - current
        action = ApiCall(
            verb="scale",
            resource="apps/v1/Deployment",
            name=target.name,
            namespace=target.namespace,
            body={"replicas": desired, "from_replicas": current},
        )
        compensation = ApiCall(
            verb="scale",
            resource="apps/v1/Deployment",
            name=target.name,
            namespace=target.namespace,
            body={"replicas": current, "from_replicas": desired},
        )

        return RemediationPlan(
            runbook=self.name,
            incident_id=bundle.incident_id,
            target_kind=target.kind,
            target_name=target.name,
            target_namespace=target.namespace,
            actions=(action,),
            compensation=(compensation,),
            reversibility=Reversibility.REVERSIBLE,
            durability=Durability.EPHEMERAL,
            ttl_seconds=SCALE_TTL_SECONDS,
            blast_radius=BlastRadius(
                pods=added,
                namespaces=(target.namespace,) if target.namespace else (),
                user_facing=_is_user_facing(target),
            ),
            expected_signal=_cpu_signal(target),
            confidence=_confidence(target, via_hpa=False),
            summary=(
                f"scale {target.namespace}/{target.name} from {current} to {desired} replicas "
                f"(ceiling {ceiling}, expires in {SCALE_TTL_SECONDS // 3600}h)"
            ),
            cost_delta_inr_month=added * INR_PER_REPLICA_MONTH,
        )

    # --- execute ---------------------------------------------------------------------------------

    async def execute(self, plan: RemediationPlan) -> ExecutionResult:
        from k8s import deployment_ops, state_capture

        namespace = plan.target_namespace or "default"
        before = await state_capture.capture_deployment(namespace, plan.target_name)
        action = plan.actions[0]

        try:
            if action.verb == "hpa_patch":
                await deployment_ops.patch_autoscaler(
                    namespace,
                    action.name,
                    target_utilization=action.body.get("target_utilization"),
                    max_replicas=action.body.get("max_replicas"),
                )
                detail = f"patched HPA {action.name}: {action.body}"
            else:
                deployment = await deployment_ops.read_deployment(namespace, plan.target_name)
                await deployment_ops.scale(
                    namespace,
                    plan.target_name,
                    int(action.body["replicas"]),
                    deployment.metadata.resource_version,
                )
                detail = f"scaled to {action.body['replicas']} replicas"
        except deployment_ops.ConflictError as exc:
            return ExecutionResult(
                ok=False, detail=str(exc), state_before=before, state_after=before
            )

        after = await state_capture.capture_deployment(namespace, plan.target_name)
        return ExecutionResult(ok=True, detail=detail, state_before=before, state_after=after)

    # --- compensate ------------------------------------------------------------------------------

    async def compensate(self, plan: RemediationPlan) -> ExecutionResult:
        """Restore the prior replica count or HPA target.

        Idempotent: if the value is already what we want, this reports ok without patching, because
        the verifier and the watchdog may both call it.
        """
        from k8s import deployment_ops, state_capture

        namespace = plan.target_namespace or "default"
        before = await state_capture.capture_deployment(namespace, plan.target_name)
        undo = plan.compensation[0]

        if undo.verb == "hpa_patch":
            await deployment_ops.patch_autoscaler(
                namespace,
                undo.name,
                target_utilization=undo.body.get("target_utilization"),
                max_replicas=undo.body.get("max_replicas"),
            )
            after = await state_capture.capture_deployment(namespace, plan.target_name)
            return ExecutionResult(
                ok=True,
                detail=f"compensated HPA {undo.name} back to {undo.body}",
                state_before=before,
                state_after=after,
            )

        wanted = int(undo.body["replicas"])
        if before.get("replicas") == wanted:
            return ExecutionResult(
                ok=True,
                detail=f"already at {wanted} replicas; nothing to compensate",
                state_before=before,
                state_after=before,
            )

        deployment = await deployment_ops.read_deployment(namespace, plan.target_name)
        await deployment_ops.scale(
            namespace, plan.target_name, wanted, deployment.metadata.resource_version
        )
        after = await state_capture.capture_deployment(namespace, plan.target_name)
        return ExecutionResult(
            ok=True,
            detail=f"compensated: scaled back to {wanted} replicas",
            state_before=before,
            state_after=after,
        )


def _cpu_signal(target: Target) -> ExpectedSignal:
    settings = get_settings()
    ns = target.namespace or "default"
    return ExpectedSignal(
        alert_should_clear="HighPodCPU",
        queries={
            # The dimensionless ratio, not raw throttled-period counters. Raw counters move with
            # replica count -- and this runbook CHANGES the replica count, so a raw comparison
            # before and after would be meaningless by construction.
            "throttle_ratio": (
                f'max(healops:container_cpu_throttle_ratio:rate5m{{namespace="{ns}"}})'
            ),
            "cpu_usage_ratio": (
                f'max(healops:container_cpu_usage_ratio:rate5m{{namespace="{ns}"}})'
            ),
        },
        thresholds={
            "throttle_ratio": settings.throttle_ratio_ceiling,
            "cpu_usage_ratio": 0.8,
        },
        window_seconds=300,
    )


def _is_user_facing(target: Target) -> bool:
    labels = target.labels
    if labels.get("healops.io/user-facing") == "true":
        return True
    tier = labels.get("tier") or labels.get("app.kubernetes.io/component") or ""
    return tier in ("frontend", "web", "api", "gateway", "edge")


def _confidence(target: Target, *, via_hpa: bool) -> float:
    """Deterministic rules; graded in CI for calibration."""
    confidence = 0.88

    # Acting on the control loop is more reliable than acting on its output.
    if via_hpa:
        confidence += 0.04

    # Plenty of headroom below the ceiling means the action can actually take effect.
    if target.replicas < get_settings().max_replicas_ceiling // 2:
        confidence += 0.03

    # CPU-bound workloads with a declared limit are the case this runbook is designed for.
    if target.containers and target.containers[0].cpu_limit_millis:
        confidence += 0.02

    # An unmanaged single replica is often a batch job or a singleton, where scaling out may not
    # help at all.
    if target.replicas <= 1:
        confidence -= 0.10

    if target.gitops_manager != "unmanaged":
        confidence -= 0.05

    return round(max(min(confidence, 0.99), 0.0), 3)


registry.register(CpuScaleUp())

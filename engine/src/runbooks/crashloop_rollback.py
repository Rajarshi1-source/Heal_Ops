"""Runbook 1 -- CrashLoop rollback.

The canonical self-healing demo is `kubectl rollout undo`, and it is where most projects stop. Two
things make this version defensible:

* **It can refuse.** A workload on its first revision has nothing to roll back *to*, and a rollback
  onto a revision running the same image is not a fix. Both produce a refusal rather than an action
  that "succeeds" while changing nothing -- which was defect D1 in miniature.
* **Rolling back is a mitigation, not the fix.** The plan is ephemeral with a TTL, and the durable
  fix is a pull request pinning the known-good image. On an Argo CD-managed workload the imperative
  patch would otherwise be reverted on the next sync.

Success is not "the API returned 200". It is the alert clearing *and* restarts going stable, which
is the verifier's job.
"""

from __future__ import annotations

from typing import Any

import structlog

from bundle import IncidentBundle, Target
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

#: How long the imperative rollback is allowed to stand before it expires on its own.
ROLLBACK_TTL_SECONDS = 7200

#: Restarts must be flat afterwards. A rollback that halves the crash rate is still a crash loop.
RESTART_TOLERANCE = 0


class CrashLoopRollback(Runbook):
    name = "crashloop_rollback"
    triggers = ("KubePodCrashLooping",)

    @property
    def chaos_fixture(self) -> str:
        return "crashy-api"

    # --- plan ------------------------------------------------------------------------------------

    def plan(self, bundle: IncidentBundle, target: Target) -> RemediationPlan:
        if target.kind != "Deployment":
            return refuse(
                self.name,
                bundle.incident_id,
                f"{target.kind} is not rollable by this runbook; only Deployments carry the "
                "ReplicaSet revision history a rollback needs",
                target_kind=target.kind,
                target_name=target.name,
                target_namespace=target.namespace,
            )

        if target.previous_revision is None:
            # First revision. There is nothing behind it, and "rolled back to nothing" is not a fix.
            return refuse(
                self.name,
                bundle.incident_id,
                "no previous revision to roll back to -- this workload has only ever had one, so "
                "the crash is in its first deployment and needs a human",
                target_kind=target.kind,
                target_name=target.name,
                target_namespace=target.namespace,
            )

        current_image = target.containers[0].image if target.containers else None
        if _rollback_changes_nothing(target, current_image):
            # The previous revision is the same pod template, so rolling back cannot change the
            # outcome. This is the "rollback onto an equally broken revision" case, caught at plan
            # time instead of being discovered by verification after the fact.
            return refuse(
                self.name,
                bundle.incident_id,
                "the previous revision is the same pod template -- a rollback would land on an "
                "equally broken revision, so the cause is outside this workload's spec (a "
                "dependency, a node, or data) and needs a human",
                target_kind=target.kind,
                target_name=target.name,
                target_namespace=target.namespace,
            )

        action = ApiCall(
            verb="rollout_undo",
            resource="apps/v1/Deployment",
            name=target.name,
            namespace=target.namespace,
            body={
                "to_revision": target.previous_revision,
                "from_revision": target.revision,
                "to_image": target.previous_image,
                **_template_ids(
                    to=target.previous_template_digest, came_from=target.template_digest
                ),
            },
        )
        # The undo of a rollback is a roll *forward* to the revision we came from, and it is
        # decided here -- before acting -- rather than reconstructed afterwards from a cluster whose
        # state is the thing being undone.
        compensation = ApiCall(
            verb="rollout_undo",
            resource="apps/v1/Deployment",
            name=target.name,
            namespace=target.namespace,
            body={
                "to_revision": target.revision,
                "from_revision": target.previous_revision,
                "to_image": current_image,
                **_template_ids(
                    to=target.template_digest, came_from=target.previous_template_digest
                ),
            },
        )

        blast_radius = BlastRadius(
            pods=max(target.replicas, 1),
            nodes=0,
            namespaces=(target.namespace,) if target.namespace else (),
            user_facing=_is_user_facing(target),
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
            # Ephemeral: on a GitOps-managed workload the next sync would re-apply the bad image, so
            # the imperative patch is explicitly a TTL'd mitigation and the PR is the real fix.
            durability=Durability.EPHEMERAL,
            ttl_seconds=ROLLBACK_TTL_SECONDS,
            blast_radius=blast_radius,
            expected_signal=ExpectedSignal(
                alert_should_clear="KubePodCrashLooping",
                # Scoped to this workload's pods and to the window after the rollback: a
                # namespace-wide count would fail on any other crashing pod, and a fixed [5m] would
                # still contain the crash loop being fixed.
                queries={
                    "restarts": (
                        "max by (namespace, pod, container) ("
                        "increase(kube_pod_container_status_restarts_total"
                        f'{{namespace="{target.namespace}",pod=~"{target.name}-.*"}}[$WINDOW])'
                        ")"
                    ),
                },
                thresholds={"restarts": float(RESTART_TOLERANCE)},
                window_seconds=300,
            ),
            confidence=_confidence(target),
            summary=(
                f"roll back {target.namespace}/{target.name} "
                f"from revision {target.revision} to {target.previous_revision}"
            ),
            cost_delta_inr_month=0.0,
        )

    # --- execute ---------------------------------------------------------------------------------

    async def execute(self, plan: RemediationPlan) -> ExecutionResult:
        from k8s import deployment_ops, state_capture

        namespace = plan.target_namespace or "default"
        before = await state_capture.capture_deployment(namespace, plan.target_name)

        target_revision = int(plan.actions[0].body["to_revision"])
        revisions = await deployment_ops.list_revisions(namespace, plan.target_name)
        wanted = _find(revisions, plan.actions[0].body)
        if wanted is None:
            # The template was garbage-collected between planning and execution. Re-planning is the
            # right response; inventing a different target revision is not.
            return ExecutionResult(
                ok=False,
                detail=(
                    f"revision {target_revision} no longer exists "
                    f"(available: {[r.revision for r in revisions]}); re-plan required"
                ),
                state_before=before,
                state_after=before,
            )

        deployment = await deployment_ops.read_deployment(namespace, plan.target_name)
        try:
            await deployment_ops.rollback_to(
                namespace,
                plan.target_name,
                wanted.template,
                deployment.metadata.resource_version,
            )
        except deployment_ops.ConflictError as exc:
            # Optimistic concurrency: someone else changed the object. Re-plan, never retry blindly.
            return ExecutionResult(
                ok=False, detail=str(exc), state_before=before, state_after=before
            )

        after = await state_capture.capture_deployment(namespace, plan.target_name)
        return ExecutionResult(
            ok=True,
            detail=f"rolled back to revision {target_revision}",
            state_before=before,
            state_after=after,
        )

    # --- compensate ------------------------------------------------------------------------------

    async def compensate(self, plan: RemediationPlan) -> ExecutionResult:
        """Roll forward to the revision we came from.

        Idempotent: if the deployment is already on the target revision this returns ok without
        patching, because both the verifier and the watchdog may call it.
        """
        from k8s import deployment_ops, state_capture

        namespace = plan.target_namespace or "default"
        before = await state_capture.capture_deployment(namespace, plan.target_name)

        undo = plan.compensation[0].body
        wanted_revision = int(undo["to_revision"])
        revisions = await deployment_ops.list_revisions(namespace, plan.target_name)

        if await _already_on(namespace, plan.target_name, undo, revisions):
            return ExecutionResult(
                ok=True,
                detail=f"already on the template of revision {wanted_revision}; nothing to "
                "compensate",
                state_before=before,
                state_after=before,
            )

        wanted = _find(revisions, undo)
        if wanted is None:
            return ExecutionResult(
                ok=False,
                detail=f"cannot compensate: revision {wanted_revision} is gone",
                state_before=before,
                state_after=before,
            )

        deployment = await deployment_ops.read_deployment(namespace, plan.target_name)
        await deployment_ops.rollback_to(
            namespace, plan.target_name, wanted.template, deployment.metadata.resource_version
        )

        after = await state_capture.capture_deployment(namespace, plan.target_name)
        return ExecutionResult(
            ok=True,
            detail=f"compensated: rolled forward to revision {wanted_revision}",
            state_before=before,
            state_after=after,
        )

    # --- verification ----------------------------------------------------------------------------

    async def extra_verification(self, plan: RemediationPlan, after: SignalWindow) -> str | None:
        """A rollback that merely slows the crash loop has not fixed it.

        The generic check already covers the alert and the golden signals; this adds the specific
        claim this runbook makes -- that restarts stopped.
        """
        if after.restarts is None:
            # Absence of evidence is not evidence of recovery. Returning a reason routes this to
            # `inconclusive` rather than letting it pass silently.
            return "restart count unavailable, cannot confirm the crash loop stopped"
        if after.restarts > RESTART_TOLERANCE:
            return f"container still restarting ({after.restarts} restarts after the rollback)"
        return None


def _is_user_facing(target: Target) -> bool:
    """A conservative guess used only for the blast-radius premium.

    Wrong in the safe direction: treating an internal service as user-facing asks a human, while the
    reverse would let a customer-facing change through unattended.
    """
    labels = target.labels
    if labels.get("healops.io/user-facing") == "true":
        return True
    tier = labels.get("tier") or labels.get("app.kubernetes.io/component") or ""
    return tier in ("frontend", "web", "api", "gateway", "edge")


def _confidence(target: Target) -> float:
    """Deterministic rules, not a model, so this can be unit-tested, replayed and hashed.

    The number is graded in CI on a labelled corpus (Brier score and expected calibration error), so
    it has to track reality rather than express optimism.
    """
    confidence = 0.90

    # A different image on the previous revision is the strongest evidence a rollback will help.
    if (
        target.previous_image
        and target.containers
        and target.previous_image != target.containers[0].image
    ):
        confidence += 0.05

    # A high restart count is unambiguous evidence the workload is genuinely crash-looping.
    if target.containers and target.containers[0].restart_count >= 5:
        confidence += 0.03

    # Nothing ready at all is consistent with a bad revision rather than a flaky dependency.
    if target.ready_replicas == 0 and target.replicas > 0:
        confidence += 0.02

    # A GitOps-managed target adds a reconciliation race we cannot fully control from here.
    if target.gitops_manager != "unmanaged":
        confidence -= 0.05

    return round(min(confidence, 0.99), 3)


def _template_ids(*, to: str | None, came_from: str | None) -> dict[str, str]:
    """Templates named by digest, when captured: the only stable name a revision has.

    Kubernetes renumbers a revision every time its ReplicaSet is reused -- roll back from 8 to 7
    and 7 becomes 9; roll forward and the 8 you came from is now 10. An undo that looked its
    target up by number was registered before acting and could not be found after it. Found on the
    kind cluster: "cannot compensate: revision 8 is gone", with revision 8's template sitting right
    there as revision 10. The numbers stay on the plan for people; the digests are what it uses.
    """
    ids: dict[str, str] = {}
    if to:
        ids["to_template"] = to
    if came_from:
        ids["from_template"] = came_from
    return ids


def _find(revisions: list[Any], body: dict[str, Any]) -> Any | None:
    """The revision a call means: by template digest when the plan has one, else by number."""
    digest = body.get("to_template")
    if digest:
        from k8s.state_capture import template_digest

        return next((r for r in revisions if template_digest(r.template) == digest), None)
    number = int(body["to_revision"])
    return next((r for r in revisions if r.revision == number), None)


async def _already_on(
    namespace: str, name: str, body: dict[str, Any], revisions: list[Any]
) -> bool:
    """Is the Deployment already running the template this call would restore? (Idempotency.)"""
    digest = body.get("to_template")
    if digest:
        from k8s import deployment_ops
        from k8s.state_capture import template_digest

        deployment = await deployment_ops.read_deployment(namespace, name)
        return bool(template_digest(deployment.spec.template) == digest)
    current = revisions[0].revision if revisions else None
    return bool(current == int(body["to_revision"]))


def _rollback_changes_nothing(target: Target, current_image: str | None) -> bool:
    """Whether the previous revision is the revision we are on, in everything but its number.

    Compared on the whole template, not the image. A bad config rollout -- an env var, an argument,
    a probe -- on an unchanged image is exactly what a rollback undoes, and refusing it as "the
    same image" would refuse the commonest bad deploy there is. Only without captured digests does
    this fall back to the image, the conservative reading.
    """
    if target.template_digest and target.previous_template_digest:
        return target.template_digest == target.previous_template_digest
    return bool(target.previous_image) and current_image == target.previous_image


registry.register(CrashLoopRollback())

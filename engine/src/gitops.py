"""GitOps-aware remediation: never pretend an imperative patch is permanent.

In a cluster run by Argo CD or Flux, `kubectl rollout undo` is temporary at best and a fight at
worst: drift detection re-applies the bad image within the sync interval. These are not different
use cases -- they are two controllers with write access to the same object. So every target is
classified, and the fix reaches the cluster in two stages:

    stage 1  ephemeral mitigation, automatic, TTL'd -- the controller stands down for exactly that
             window, for exactly the fields being changed
    stage 2  durable fix as a pull request, opened only AFTER verification proves stage 1 worked
             (see `durable.py`), so git becomes the source of truth again

Three rules this module keeps:

* **Unmanaged targets still get a TTL.** An imperative change nobody tracks is how clusters drift
  from their manifests, GitOps or not.
* **The narrowest pause that works.** Argo CD gets a field-scoped `ignoreDifferences` entry for
  the exact JSON pointers this plan changes, plus `RespectIgnoreDifferences` so a sync does not
  overwrite them -- not `selfHeal=false` for the whole application. Flux has no field-level
  equivalent, so its Kustomization is suspended for the window.
* **Resume belongs to the watchdog, never to the request path.** A pause that depends on the process
  that set it surviving is not a pause. What was paused is read back from the ledger entry written
  when it was set, so the resume needs no memory of the process that paused.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import structlog
from kubernetes.client.rest import ApiException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import ledger
from bundle import GitOpsManager, Target
from config import get_settings
from db.models import AuditLedger, RunbookExecution
from plan import Durability, RemediationPlan

log = structlog.get_logger(__name__)

ARGO = ("argoproj.io", "v1alpha1", "applications")
FLUX = ("kustomize.toolkit.fluxcd.io", "v1", "kustomizations")
RESPECT = "RespectIgnoreDifferences=true"


class GitOpsPauseFailed(RuntimeError):
    """The controller could not be told to stand down, so acting would only start a fight."""


# --- classification and preparation (pure) --------------------------------------------------------


def classify(labels: dict[str, str], annotations: dict[str, str]) -> GitOpsManager:
    """Which controller, if any, also has write access to this object."""
    if "argocd.argoproj.io/instance" in labels or "argocd.argoproj.io/tracking-id" in annotations:
        return "argocd"
    if "kustomize.toolkit.fluxcd.io/name" in labels:
        return "flux"
    return "unmanaged"


def prepare(plan: RemediationPlan, target: Target) -> RemediationPlan:
    """Decide how the fix reaches the cluster. Pure: part of planning, so it is in the digest."""
    if plan.is_refusal:
        return plan
    managed = classify(dict(target.labels), dict(target.annotations)) != "unmanaged"
    if plan.durability is Durability.EPHEMERAL:
        ttl = plan.ttl_seconds or get_settings().default_ephemeral_ttl_seconds
        return plan.with_gitops(managed=managed, durability=Durability.EPHEMERAL, ttl=ttl)
    # Durable: the cluster is not the source of truth. Git is.
    return plan.with_gitops(managed=managed, durability=Durability.DURABLE, ttl=plan.ttl_seconds)


@dataclass(frozen=True, slots=True)
class IgnoreRule:
    """One Argo CD `ignoreDifferences` entry: these fields of this object, for this window."""

    group: str
    kind: str
    name: str
    namespace: str
    jsonPointers: tuple[str, ...]  # noqa: N815 - Argo's own field name, serialised verbatim

    def to_argo(self) -> dict[str, Any]:
        entry = asdict(self)
        entry["jsonPointers"] = list(self.jsonPointers)
        return entry


def ignore_rules(plan: RemediationPlan) -> list[IgnoreRule]:
    """The exact fields this plan changes on objects a GitOps controller tracks.

    A resize changes a *pod*, and pods are not in git -- the controller owns the Deployment, not its
    pods -- so an in-place resize needs no pause at all. Only the objects git declares are listed.
    """
    rules: list[IgnoreRule] = []
    for action in plan.actions:
        namespace = action.namespace or ""
        if action.resource == "apps/v1/Deployment" and action.verb in ("rollout_undo", "patch"):
            rules.append(
                IgnoreRule("apps", "Deployment", action.name, namespace, ("/spec/template",))
            )
        elif action.resource == "apps/v1/Deployment" and action.verb == "scale":
            rules.append(
                IgnoreRule("apps", "Deployment", action.name, namespace, ("/spec/replicas",))
            )
        elif action.verb == "hpa_patch":
            rules.append(
                IgnoreRule(
                    "autoscaling",
                    "HorizontalPodAutoscaler",
                    action.name,
                    namespace,
                    ("/spec/maxReplicas", "/spec/metrics"),
                )
            )
    return rules


# --- pause and resume -----------------------------------------------------------------------------


async def pause(
    session: AsyncSession, execution: RunbookExecution, plan: RemediationPlan
) -> dict[str, Any] | None:
    """Tell the controller to stand down for these fields, inside the mutating window.

    Returns the ledger record of what was paused, or None when nothing the controller tracks is
    being changed. Raises `GitOpsPauseFailed` when it could not pause -- the orchestrator then does
    not act, because a change the controller will revert in three minutes is not a mitigation.
    """
    rules = ignore_rules(plan)
    if not rules or not plan.target_namespace:
        return None

    from k8s import deployment_ops

    try:
        deployment = await deployment_ops.read_deployment(plan.target_namespace, plan.target_name)
        labels = dict(deployment.metadata.labels or {})
        annotations = dict(deployment.metadata.annotations or {})
        manager = classify(labels, annotations)
        if manager == "argocd":
            record = await _pause_argo(labels, annotations, rules)
        elif manager == "flux":
            record = await _pause_flux(labels)
        else:
            return None  # no longer managed since planning: nothing to pause
    except ApiException as exc:
        raise GitOpsPauseFailed(f"could not pause {plan.target_key}: {exc.reason}") from exc

    record["ttl_seconds"] = plan.ttl_seconds
    record["reason"] = f"healops:{plan.runbook}"
    execution.gitops_paused = True
    await ledger.append(
        session,
        "gitops_pause",
        record,
        incident_id=execution.incident_id,
        execution_id=execution.id,
    )
    log.info("gitops reconciliation paused", **{k: v for k, v in record.items() if k != "rules"})
    return record


async def resume(session: AsyncSession, execution: RunbookExecution) -> bool:
    """Undo exactly what `pause` did, read back from its ledger entry. Idempotent."""
    record = await session.scalar(
        select(AuditLedger.payload)
        .where(AuditLedger.execution_id == execution.id, AuditLedger.event_type == "gitops_pause")
        .order_by(AuditLedger.seq.desc())
        .limit(1)
    )
    if record is None:
        execution.gitops_paused = False
        return False

    try:
        if record["manager"] == "argocd":
            await _resume_argo(
                record, others_need_option=await _other_live_pauses(session, execution, record)
            )
        else:
            await _resume_flux(record)
    except ApiException as exc:
        if exc.status != 404:  # the Application or Kustomization is gone: nothing left to resume
            raise

    execution.gitops_paused = False
    await ledger.append(
        session,
        "gitops_resume",
        {"manager": record["manager"], "resumed": record.get("application") or record.get("name")},
        incident_id=execution.incident_id,
        execution_id=execution.id,
    )
    log.info("gitops reconciliation resumed", execution_id=execution.id)
    return True


async def _pause_argo(
    labels: dict[str, str], annotations: dict[str, str], rules: list[IgnoreRule]
) -> dict[str, Any]:
    from k8s.client import call, custom_objects

    app = labels.get("argocd.argoproj.io/instance") or _app_from_tracking_id(
        annotations.get("argocd.argoproj.io/tracking-id", "")
    )
    namespace = get_settings().argocd_namespace
    api = custom_objects()
    obj = await call(
        "get_application", api.get_namespaced_custom_object, *ARGO[:2], namespace, ARGO[2], app
    )
    spec = obj.get("spec", {})

    existing = list(spec.get("ignoreDifferences") or [])
    added = [rule.to_argo() for rule in rules if rule.to_argo() not in existing]
    options = list((spec.get("syncPolicy") or {}).get("syncOptions") or [])
    added_option = RESPECT not in options

    body = {
        "metadata": {"resourceVersion": obj["metadata"]["resourceVersion"]},
        "spec": {
            "ignoreDifferences": existing + added,
            "syncPolicy": {"syncOptions": options + ([RESPECT] if added_option else [])},
        },
    }
    await call(
        "pause_application",
        api.patch_namespaced_custom_object,
        *ARGO[:2],
        namespace,
        ARGO[2],
        app,
        body,
    )
    return {
        "manager": "argocd",
        "application": app,
        "namespace": namespace,
        "rules": added,
        "added_sync_option": added_option,
    }


async def _other_live_pauses(
    session: AsyncSession, execution: RunbookExecution, record: dict[str, Any]
) -> bool:
    """Is another execution's pause still live on the same Argo CD application?"""
    rows = await session.execute(
        select(AuditLedger.payload)
        .join(RunbookExecution, RunbookExecution.id == AuditLedger.execution_id)
        .where(
            AuditLedger.event_type == "gitops_pause",
            RunbookExecution.gitops_paused.is_(True),
            RunbookExecution.id != execution.id,
        )
    )
    return any(
        p.get("manager") == "argocd" and p.get("application") == record.get("application")
        for p in rows.scalars()
    )


async def _resume_argo(record: dict[str, Any], *, others_need_option: bool = False) -> None:
    from k8s.client import call, custom_objects

    api = custom_objects()
    app, namespace = record["application"], record["namespace"]
    obj = await call(
        "get_application", api.get_namespaced_custom_object, *ARGO[:2], namespace, ARGO[2], app
    )
    spec = obj.get("spec", {})
    remaining = [e for e in spec.get("ignoreDifferences") or [] if e not in record["rules"]]
    options = list((spec.get("syncPolicy") or {}).get("syncOptions") or [])
    # Withdraw the sync option only if this pause added it and no other HealOps pause on this
    # application still relies on it. The application's own ignore rules never needed it -- keeping
    # it because of them would permanently change how the app syncs.
    if record.get("added_sync_option") and not others_need_option:
        options = [o for o in options if o != RESPECT]
    body = {
        "metadata": {"resourceVersion": obj["metadata"]["resourceVersion"]},
        "spec": {"ignoreDifferences": remaining, "syncPolicy": {"syncOptions": options}},
    }
    await call(
        "resume_application",
        api.patch_namespaced_custom_object,
        *ARGO[:2],
        namespace,
        ARGO[2],
        app,
        body,
    )


async def _pause_flux(labels: dict[str, str]) -> dict[str, Any]:
    from k8s.client import call, custom_objects

    name = labels["kustomize.toolkit.fluxcd.io/name"]
    namespace = labels.get("kustomize.toolkit.fluxcd.io/namespace", "flux-system")
    api = custom_objects()
    obj = await call(
        "get_kustomization", api.get_namespaced_custom_object, *FLUX[:2], namespace, FLUX[2], name
    )
    was_suspended = bool(obj.get("spec", {}).get("suspend"))
    if not was_suspended:
        await call(
            "suspend_kustomization",
            api.patch_namespaced_custom_object,
            *FLUX[:2],
            namespace,
            FLUX[2],
            name,
            {"spec": {"suspend": True}},
        )
    return {"manager": "flux", "name": name, "namespace": namespace, "was_suspended": was_suspended}


async def _resume_flux(record: dict[str, Any]) -> None:
    if record.get("was_suspended"):
        return  # someone else suspended it before us; that is theirs to lift
    from k8s.client import call, custom_objects

    api = custom_objects()
    await call(
        "resume_kustomization",
        api.patch_namespaced_custom_object,
        *FLUX[:2],
        record["namespace"],
        FLUX[2],
        record["name"],
        {"spec": {"suspend": False}},
    )


def _app_from_tracking_id(tracking_id: str) -> str:
    # `app-name:group/kind:namespace/name`; the application is the first field.
    return tracking_id.split(":", 1)[0]

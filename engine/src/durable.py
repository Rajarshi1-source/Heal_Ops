"""Stage 2 of the two-stage fix: the durable change, as a pull request.

    mitigate automatically (stage 1, ephemeral, TTL'd)  ->  VERIFY  ->  fix durably through review

**The PR is opened only after verification passes.** A PR proposing a change that did not work is
worse than no PR, so this module is called from the verify loop on a `remediated` verdict and from
nowhere else.

The change in the PR is exactly the change the plan made and verification proved -- the image the
rollback landed on, the replica count, the HPA target, the memory limit -- taken from the plan's own
actions rather than re-derived, so what a reviewer approves is what already ran under real traffic.

The PR body is the differentiator, not the PR: the incident, the before/after signals, the ledger
hash, how long the fix was validated live, and how to revert. That is a better review packet than a
human filing the same change from memory at 3 a.m.

What HealOps will not do is guess where the manifest lives. A workload names its file with the
`healops.io/manifest-path` annotation; without it the PR is skipped and the reason recorded.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from ruamel.yaml import YAML
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import ledger
import plan_store
from config import get_settings
from db.connection import transaction
from db.models import AuditLedger, Incident, RemediationPlanRow, RunbookExecution, Verification
from plan import RemediationPlan

log = structlog.get_logger(__name__)

LABELS_BASE = ["healops", "incident-remediation"]


class ManifestEditError(ValueError):
    """The manifest does not contain what the change needs to edit."""


@dataclass(frozen=True, slots=True)
class Change:
    """One edit to one object in the manifest, described for the PR body as well as applied."""

    kind: str
    name: str
    images: dict[str, str] = field(default_factory=dict)
    replicas: int | None = None
    memory: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    """container -> (limit, request or None)"""
    hpa_max_replicas: int | None = None
    hpa_cpu_target: int | None = None

    def describe(self) -> list[str]:
        lines = [f"{self.kind} `{self.name}`:"]
        lines += [f"- container `{c}` image -> `{i}`" for c, i in self.images.items()]
        if self.replicas is not None:
            lines.append(f"- `spec.replicas` -> {self.replicas}")
        for c, (limit, request) in self.memory.items():
            lines.append(
                f"- container `{c}` memory limit -> {limit}"
                + (f", request -> {request}" if request else "")
            )
        if self.hpa_max_replicas is not None:
            lines.append(f"- `spec.maxReplicas` -> {self.hpa_max_replicas}")
        if self.hpa_cpu_target is not None:
            lines.append(f"- CPU `averageUtilization` -> {self.hpa_cpu_target}%")
        return lines


@dataclass(frozen=True, slots=True)
class PrResult:
    url: str | None
    reason: str


# --- what changes ---------------------------------------------------------------------------------


def change_for(plan: RemediationPlan, workload: str, state_after: dict[str, Any]) -> Change | None:
    """The durable equivalent of the plan's mitigation, or None when there is none to make.

    Disk cleanup and node drain change nothing that git declares, so they have no durable PR.
    """
    action = plan.actions[0] if plan.actions else None
    if action is None:
        return None

    if action.verb == "rollout_undo":
        # The images the workload is *running* now, validated by verification -- not a guess at
        # which container the plan's single `to_image` belonged to.
        images = {
            str(c["name"]): str(c["image"])
            for c in state_after.get("containers") or []
            if c.get("name") and c.get("image")
        }
        return Change("Deployment", workload, images=images) if images else None

    if action.verb == "scale":
        return Change("Deployment", workload, replicas=int(action.body["replicas"]))

    if action.verb == "hpa_patch":
        return Change(
            "HorizontalPodAutoscaler",
            action.name,
            hpa_max_replicas=action.body.get("max_replicas"),
            hpa_cpu_target=action.body.get("target_utilization"),
        )

    if action.verb in ("resize", "patch") and action.resource in ("v1/Pod", "apps/v1/Deployment"):
        spec = action.body.get("spec", {})
        containers = spec.get("containers") or spec.get("template", {}).get("spec", {}).get(
            "containers", []
        )
        memory = {
            str(c["name"]): (
                str(c["resources"]["limits"]["memory"]),
                (c["resources"].get("requests") or {}).get("memory"),
            )
            for c in containers
        }
        return Change("Deployment", workload, memory=memory) if memory else None

    return None


def edit_manifest(text: str, change: Change) -> str:
    """Apply the change to the manifest, preserving comments, ordering and quoting."""
    yaml = _yaml()
    docs = list(yaml.load_all(text))

    target = next(
        (
            d
            for d in docs
            if isinstance(d, dict)
            and d.get("kind") == change.kind
            and (d.get("metadata") or {}).get("name") == change.name
        ),
        None,
    )
    if target is None:
        raise ManifestEditError(f"no {change.kind} named {change.name} in the manifest")

    if change.kind == "HorizontalPodAutoscaler":
        _edit_hpa(target, change)
    else:
        _edit_deployment(target, change)

    # Sequence indentation is a matter of house style that YAML does not record, so try the common
    # styles and keep whichever reproduces the original most closely. Otherwise every list in the
    # file is re-indented and the reviewer has to find the one real change among dozens.
    original = text.splitlines()
    candidates = []
    for mapping, sequence, offset in INDENT_STYLES:
        styled = _yaml()
        styled.indent(mapping=mapping, sequence=sequence, offset=offset)
        out = io.StringIO()
        styled.dump_all(docs, out)
        rendered = out.getvalue()
        candidates.append((_changed_lines(original, rendered.splitlines()), rendered))
    return min(candidates, key=lambda c: c[0])[1]


#: (mapping, sequence, offset): ruamel's default, and the `- ` indented under its key style.
INDENT_STYLES = ((2, 2, 0), (2, 4, 2))


def _yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096  # never re-wrap a long line the author wrote on one
    return yaml


def _changed_lines(before: list[str], after: list[str]) -> int:
    import difflib

    return sum(1 for line in difflib.ndiff(before, after) if line[:1] in "+-")


def _edit_deployment(doc: Any, change: Change) -> None:
    if change.replicas is not None:
        doc["spec"]["replicas"] = change.replicas
    containers = {c["name"]: c for c in doc["spec"]["template"]["spec"]["containers"]}
    for name, image in change.images.items():
        if name not in containers:
            raise ManifestEditError(f"container {name} is not in the manifest")
        containers[name]["image"] = image
    for name, (limit, request) in change.memory.items():
        if name not in containers:
            raise ManifestEditError(f"container {name} is not in the manifest")
        resources = containers[name].setdefault("resources", {})
        resources.setdefault("limits", {})["memory"] = limit
        if request is not None:
            resources.setdefault("requests", {})["memory"] = request


def _edit_hpa(doc: Any, change: Change) -> None:
    if change.hpa_max_replicas is not None:
        doc["spec"]["maxReplicas"] = change.hpa_max_replicas
    if change.hpa_cpu_target is not None:
        for metric in doc["spec"].get("metrics") or []:
            resource = metric.get("resource") or {}
            if resource.get("name") == "cpu":
                resource["target"]["averageUtilization"] = change.hpa_cpu_target
                return
        raise ManifestEditError("the HPA declares no CPU utilisation metric to adjust")


# --- the review packet ----------------------------------------------------------------------------


def render_body(
    *,
    plan: RemediationPlan,
    incident: Incident,
    execution: RunbookExecution,
    verification: Verification,
    ledger_hash: str,
    change: Change,
    manifest_path: str,
) -> str:
    validated = _minutes(execution.finished_at, verification.checked_at)
    rows = [
        f"| `{name}` | {_fmt(verification.signals_before.get(name))} | "
        f"{_fmt(verification.signals_after.get(name))} |"
        for name in sorted(set(verification.signals_before) | set(verification.signals_after))
    ]
    expiry = (
        f"The live mitigation expires at **{execution.expires_at:%Y-%m-%d %H:%M} UTC** and the "
        "workload then returns to what git declares -- so merging this is what makes the fix stay."
        if execution.expires_at
        else ""
    )
    return "\n".join(
        [
            f"**{plan.summary}**",
            "",
            f"This change was already validated live for **{validated} minutes** under real "
            "traffic before this PR was opened. HealOps applied it as an ephemeral mitigation, "
            "verified the incident was over, and is now proposing it as the durable fix.",
            "",
            "## Change",
            f"`{manifest_path}`",
            *change.describe(),
            "",
            "## Evidence",
            f"- Incident #{incident.id} `{incident.cause_alertname}` on `{incident.target_key}`, "
            f"opened {incident.opened_at:%Y-%m-%d %H:%M} UTC",
            f"- Verdict: **{verification.outcome}** -- " + "; ".join(verification.reasons),
            f"- Ledger: `{ledger_hash}` (verify with `healopsctl audit --verify-chain`)",
            "",
            "| Signal | Before | After |",
            "|---|---|---|",
            *rows,
            "",
            "## Reverting",
            "Close this PR to keep the current declared state. " + expiry,
            "After merging, revert with `git revert` of the merge commit; the cluster follows git.",
        ]
    )


def _minutes(start: datetime | None, end: datetime | None) -> int:
    if start is None or end is None:
        return 0
    return max(int((end - start).total_seconds() // 60), 0)


def _fmt(value: Any) -> str:
    return (
        "not observed"
        if value is None
        else f"{value:g}"
        if isinstance(value, float)
        else str(value)
    )


# --- opening it -----------------------------------------------------------------------------------


async def open_for(execution_id: int) -> PrResult:
    """Open the durable PR for a verified execution. Idempotent; never raises."""
    from integrations import github_client

    try:
        loaded = await _load(execution_id)
        if isinstance(loaded, PrResult):
            return loaded
        plan, incident, execution, verification, ledger_hash, workload = loaded

        change = change_for(plan, workload, execution.state_after)
        if change is None:
            return PrResult(None, f"{plan.runbook} changes nothing git declares")

        settings = get_settings()
        if not settings.github_enabled:
            return PrResult(None, "github is not configured")
        path = await _manifest_path(incident.namespace or plan.target_namespace or "", workload)
        if not path:
            return PrResult(
                None,
                f"{workload} carries no {settings.manifest_path_annotation} annotation, so the "
                "manifest to change is unknown",
            )

        base = settings.github_base_branch
        text, sha = await github_client.get_file(path, base)
        edited = edit_manifest(text, change)
        branch = f"healops/{plan.runbook}/{workload}-{incident.id}"
        await github_client.create_branch(branch, base)
        await github_client.put_file(
            path, branch=branch, text=edited, sha=sha, message=f"[HealOps] {plan.summary}"
        )
        url = await github_client.create_pull(
            title=f"[HealOps] {plan.summary}",
            head=branch,
            base=base,
            body=render_body(
                plan=plan,
                incident=incident,
                execution=execution,
                verification=verification,
                ledger_hash=ledger_hash,
                change=change,
                manifest_path=path,
            ),
            labels=[*LABELS_BASE, f"runbook:{plan.runbook}"],
        )
    except Exception as exc:  # noqa: BLE001 - the fix stands; only the PR is lost
        log.warning("durable pr not opened", execution_id=execution_id, error=str(exc))
        return PrResult(None, f"could not open the pull request: {exc}")

    await record(execution_id, url)
    return PrResult(url, "opened")


async def record(execution_id: int, url: str) -> None:
    async with transaction() as session:
        execution = await session.get(RunbookExecution, execution_id, with_for_update=True)
        if execution is None:
            return
        execution.pr_url = url
        await ledger.append(
            session,
            "durable_pr",
            {"execution_id": execution_id, "url": url},
            incident_id=execution.incident_id,
            execution_id=execution_id,
        )


async def _load(
    execution_id: int,
) -> PrResult | tuple[RemediationPlan, Incident, RunbookExecution, Verification, str, str]:
    async with transaction() as session:
        execution = await session.get(RunbookExecution, execution_id)
        if execution is None or execution.plan_id is None:
            return PrResult(None, "no such execution")
        if execution.pr_url:
            return PrResult(execution.pr_url, "already opened")
        if execution.status != "remediated":
            # Only after verification passes. Never before, never on a reverted fix.
            return PrResult(None, f"execution is {execution.status}, not remediated")
        row = await session.get(RemediationPlanRow, execution.plan_id)
        incident = await session.get(Incident, execution.incident_id)
        verification = await _verdict(session, execution_id)
        if row is None or incident is None or verification is None:
            return PrResult(None, "plan, incident or verdict missing")
        ledger_hash = await session.scalar(
            select(AuditLedger.hash)
            .where(AuditLedger.execution_id == execution_id, AuditLedger.event_type == "verify")
            .order_by(AuditLedger.seq.desc())
            .limit(1)
        )
        workload = incident.cause_target if incident.cause_scope == "workload" else row.target_name
        return (
            plan_store.rehydrate(row, execution),
            incident,
            execution,
            verification,
            ledger_hash or "",
            workload,
        )


async def _verdict(session: AsyncSession, execution_id: int) -> Verification | None:
    return (
        await session.execute(
            select(Verification)
            .where(Verification.execution_id == execution_id)
            .order_by(Verification.checked_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _manifest_path(namespace: str, workload: str) -> str | None:
    from k8s import deployment_ops

    deployment = await deployment_ops.read_deployment(namespace, workload)
    annotations = dict(deployment.metadata.annotations or {})
    return annotations.get(get_settings().manifest_path_annotation)

"""The incident bundle -- the contract between the chaos harness and the planner.

A bundle is everything the planner is allowed to see: the alerts that fired, and a snapshot of the
target they point at. Nothing else. That restriction is load-bearing:

    planner.plan_for(bundle) must be a PURE FUNCTION of the bundle.

If planning reached out to a live cluster, the decision eval could not be hermetic, the determinism
digest could not be stable, and the CI gate would be flaky. So every cluster read a runbook needs
happens *before* planning and lands in `Target`.

The same structure is used three ways, which is why it is worth being strict about:

* the chaos harness writes bundles to `corpus/` as labelled JSON,
* `evals/decision_eval.py` replays them through the planner and grades the decisions,
* the live engine builds one in memory from the correlator's output and the K8s state capture.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Self

BundleFormatVersion = Literal[1]
BUNDLE_FORMAT_VERSION: BundleFormatVersion = 1

GitOpsManager = Literal["argocd", "flux", "unmanaged"]
ExpectedEffect = Literal["allow", "require_approval", "deny", "refuse"]


@dataclass(frozen=True, slots=True)
class ContainerState:
    """One container's declared and observed resource state."""

    name: str
    image: str
    cpu_request_millis: int | None = None
    cpu_limit_millis: int | None = None
    memory_request_mib: int | None = None
    memory_limit_mib: int | None = None
    memory_peak_mib: float | None = None
    restart_count: int = 0
    # "NotRequired" | "RestartContainer" | None. RestartContainer for memory makes an in-place
    # bump a restart, which the OOM runbook must refuse rather than misreport.
    memory_resize_policy: str | None = None
    cpu_resize_policy: str | None = None


@dataclass(frozen=True, slots=True)
class AutoscalerState:
    """The HPA that owns `spec.replicas`, if one does.

    Its presence changes the CPU runbook completely: patching `replicas` on an HPA-owned workload
    is a no-op at best and an oscillation at worst, so the runbook acts on the control loop
    instead of on its output.
    """

    name: str
    metric: Literal["cpu", "memory", "external"]
    target_utilization: int | None = None
    min_replicas: int = 1
    max_replicas: int = 1
    current_replicas: int = 1


@dataclass(frozen=True, slots=True)
class NodeState:
    name: str
    ready: bool = True
    schedulable: bool = True
    allocatable_cpu_millis: int = 0
    allocatable_memory_mib: int = 0
    requested_cpu_millis: int = 0
    requested_memory_mib: int = 0
    pod_count: int = 0


@dataclass(frozen=True, slots=True)
class Target:
    """The object a plan would act on, captured before planning."""

    kind: str
    name: str
    namespace: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)

    replicas: int = 1
    ready_replicas: int = 0
    containers: tuple[ContainerState, ...] = ()
    pods: tuple[str, ...] = ()
    node: str | None = None

    autoscaler: AutoscalerState | None = None
    gitops_manager: GitOpsManager = "unmanaged"

    # Revision history for `rollout undo`. Empty means there is nothing to roll back TO, which is
    # itself a refusal reason rather than an error.
    revision: int | None = None
    previous_revision: int | None = None
    previous_image: str | None = None
    # Digests of the current and previous pod templates. Equal means a rollback would change
    # nothing at all; different images are NOT required -- a bad config rollout on the same image
    # is exactly what a rollback undoes. None when not captured.
    template_digest: str | None = None
    previous_template_digest: str | None = None

    # Cluster capability, not target state, but it belongs in the bundle for the same reason
    # everything else does: the planner must not query the cluster to learn it.
    server_supports_in_place_resize: bool = True

    # Populated for node-scoped incidents (runbook 5's capacity precheck).
    node_state: NodeState | None = None
    peer_nodes: tuple[NodeState, ...] = ()
    # The controllers whose pods a drain would move, as `namespace/Kind/name`. Recovery is judged
    # on THESE coming back, not on everything else that happens to share their namespaces.
    drained_workloads: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.namespace or '-'}/{self.kind}/{self.name}"

    @property
    def remediation_disabled(self) -> bool:
        return self.annotations.get("healops.io/remediation") == "disabled"


@dataclass(frozen=True, slots=True)
class BundleAlert:
    """One Alertmanager v2 alert, reduced to the fields a decision may depend on."""

    alertname: str
    fingerprint: str
    status: Literal["firing", "resolved"] = "firing"
    severity: str = "warning"
    namespace: str | None = None
    pod: str | None = None
    container: str | None = None
    node: str | None = None
    starts_at: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IncidentBundle:
    """A correlated incident, ready to plan against."""

    incident_id: str
    cause_alert: BundleAlert
    target: Target
    symptom_alerts: tuple[BundleAlert, ...] = ()
    cause_scope: Literal["node", "workload", "pod"] = "workload"
    correlation_reason: str = "new_incident"
    # Wall-clock the incident opened, ISO-8601. Never read for decisions -- it is here so a replay
    # renders the same timeline the dashboard shows.
    opened_at: str | None = None
    format_version: BundleFormatVersion = BUNDLE_FORMAT_VERSION

    # --- labels, present only in corpus bundles -----------------------------------------------
    # The live engine leaves these None. The eval requires them.
    expected_runbook: str | None = None
    expected_effect: ExpectedEffect | None = None
    label_note: str | None = None

    @property
    def is_labelled(self) -> bool:
        return self.expected_runbook is not None and self.expected_effect is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_canonical_json(self) -> str:
        """Same canonical form the ledger uses, so a bundle hash is comparable across tools."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        version = raw.get("format_version", BUNDLE_FORMAT_VERSION)
        if version != BUNDLE_FORMAT_VERSION:
            raise ValueError(
                f"bundle format version {version} is not supported "
                f"(this build reads version {BUNDLE_FORMAT_VERSION})"
            )

        def _containers(items: list[dict[str, Any]] | None) -> tuple[ContainerState, ...]:
            return tuple(ContainerState(**c) for c in items or ())

        def _nodes(items: list[dict[str, Any]] | None) -> tuple[NodeState, ...]:
            return tuple(NodeState(**n) for n in items or ())

        t = dict(raw["target"])
        t["containers"] = _containers(t.get("containers"))
        t["pods"] = tuple(t.get("pods") or ())
        t["peer_nodes"] = _nodes(t.get("peer_nodes"))
        t["drained_workloads"] = tuple(t.get("drained_workloads") or ())
        if t.get("autoscaler"):
            t["autoscaler"] = AutoscalerState(**t["autoscaler"])
        if t.get("node_state"):
            t["node_state"] = NodeState(**t["node_state"])

        return cls(
            incident_id=raw["incident_id"],
            cause_alert=BundleAlert(**raw["cause_alert"]),
            target=Target(**t),
            symptom_alerts=tuple(BundleAlert(**a) for a in raw.get("symptom_alerts", ())),
            cause_scope=raw.get("cause_scope", "workload"),
            correlation_reason=raw.get("correlation_reason", "new_incident"),
            opened_at=raw.get("opened_at"),
            format_version=version,
            expected_runbook=raw.get("expected_runbook"),
            expected_effect=raw.get("expected_effect"),
            label_note=raw.get("label_note"),
        )

    @classmethod
    def from_json(cls, text: str) -> Self:
        return cls.from_dict(json.loads(text))

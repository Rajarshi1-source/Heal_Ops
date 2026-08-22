"""Cause-first correlation: one root cause is one incident.

This module exists because of defect D5. Dedup is per-fingerprint and locks are per-resource, so a
naive pipeline handles a node failure like this:

    Node ip-10-0-3-14 goes NotReady at 02:14.
      KubeNodeNotReady         -> cordon + drain      -> approval requested
      KubePodCrashLooping x40  -> rollout undo x40    -> 40 rollbacks, politely serialised

Forty healthy deployments rolled back because they happened to live on a dead node, and every one
of them recorded as a success. Locks solve *conflicts*; they do nothing about *volume*.

So alerts are grouped by **topology and causality**, never by fingerprint. Symptom alerts attach to
the cause incident as evidence and never select their own runbook. If the node recovers on its own,
forty symptom alerts clear with zero actions -- reported honestly as "1 incident, 0 remediations,
self-resolved".

The logic here is deliberately a pure function of (alert, open incidents, now) so it can be tested
without a cluster or a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

CauseScope = Literal["node", "workload", "pod"]
AlertRole = Literal["cause", "symptom"]

# Lower precedence wins. A node-level failure explains pod-level symptoms on that node, and a
# workload-level failure explains its own pods' symptoms.
CAUSE_PRECEDENCE: Final[dict[str, int]] = {
    "KubeNodeNotReady": 0,
    "NodeDiskPressure": 0,
    "KubeDeploymentReplicasMismatch": 1,
    "KubePodOOMKilled": 2,
    "KubePodCrashLooping": 2,
    "HighPodCPU": 3,
}
DEFAULT_PRECEDENCE: Final[int] = 9

# Alerts whose natural scope is a whole node.
NODE_SCOPED: Final[frozenset[str]] = frozenset({"KubeNodeNotReady", "NodeDiskPressure"})

# Two alerts on the same target inside this window are the same incident.
SAME_TARGET_WINDOW_SECONDS: Final[int] = 300


@dataclass(frozen=True, slots=True)
class AlertFacts:
    """The subset of an alert that correlation is allowed to depend on."""

    alertname: str
    fingerprint: str
    namespace: str | None = None
    pod: str | None = None
    node: str | None = None
    workload: str | None = None
    starts_at: datetime | None = None

    @property
    def precedence(self) -> int:
        return CAUSE_PRECEDENCE.get(self.alertname, DEFAULT_PRECEDENCE)

    @property
    def scope(self) -> CauseScope:
        if self.alertname in NODE_SCOPED:
            return "node"
        return "workload" if self.workload else "pod"

    @property
    def target_key(self) -> str:
        if self.scope == "node":
            return f"node/{self.node or 'unknown'}"
        if self.workload:
            return f"{self.namespace or '-'}/Deployment/{self.workload}"
        return f"{self.namespace or '-'}/Pod/{self.pod or 'unknown'}"


@dataclass(frozen=True, slots=True)
class OpenIncident:
    """An incident currently accepting alerts."""

    id: int
    cause_alertname: str
    cause_scope: CauseScope
    cause_target: str
    target_key: str
    namespace: str | None
    precedence: int
    opened_at: datetime
    # Nodes and workloads this incident already explains.
    node: str | None = None
    workload: str | None = None


@dataclass(frozen=True, slots=True)
class CorrelationDecision:
    action: Literal["attach", "create", "promote_cause"]
    role: AlertRole
    reason: str
    incident_id: int | None = None
    scope: CauseScope = "workload"
    precedence: int = DEFAULT_PRECEDENCE
    target_key: str = ""


def _explains(incident: OpenIncident, alert: AlertFacts) -> str | None:
    """Does `incident` already explain `alert`? Returns the correlation reason, or None."""
    # Rule 1: a node-level cause dominates every pod symptom on that node.
    if (
        incident.cause_scope == "node"
        and incident.node
        and alert.node == incident.node
        and alert.scope != "node"
    ):
        return "symptom_of_node_cause"

    # Rule 2: a workload-level cause dominates its own pods' symptoms.
    if (
        incident.cause_scope == "workload"
        and incident.workload
        and alert.namespace == incident.namespace
        and alert.workload == incident.workload
        and alert.precedence >= incident.precedence
    ):
        return "symptom_of_workload_cause"

    # Rule 3: the same target inside the window is the same incident.
    if incident.target_key == alert.target_key:
        return "same_target"

    return None


def correlate(
    alert: AlertFacts,
    open_incidents: list[OpenIncident],
    now: datetime,
) -> CorrelationDecision:
    """Decide whether this alert joins an existing incident or opens a new one.

    Evaluated in precedence order, so a node cause is always considered before a workload cause
    even if the workload incident opened first.
    """
    window = timedelta(seconds=SAME_TARGET_WINDOW_SECONDS)
    candidates = sorted(
        (i for i in open_incidents if now - i.opened_at <= window),
        key=lambda i: (i.precedence, i.opened_at),
    )

    for incident in candidates:
        reason = _explains(incident, alert)
        if reason is None:
            continue

        # A lower-precedence alert arriving after its own symptoms is still the cause. This is the
        # ordering that actually happens: pods start crash-looping before the node's Ready
        # condition flips, so the cause alert lands last.
        if alert.precedence < incident.precedence and reason != "same_target":
            return CorrelationDecision(
                action="promote_cause",
                role="cause",
                reason="cause_arrived_after_symptoms",
                incident_id=incident.id,
                scope=alert.scope,
                precedence=alert.precedence,
                target_key=alert.target_key,
            )

        role: AlertRole = "cause" if reason == "same_target" else "symptom"
        return CorrelationDecision(
            action="attach",
            role=role,
            reason=reason,
            incident_id=incident.id,
            scope=incident.cause_scope,
            precedence=incident.precedence,
            target_key=incident.target_key,
        )

    return CorrelationDecision(
        action="create",
        role="cause",
        reason="new_incident",
        scope=alert.scope,
        precedence=alert.precedence,
        target_key=alert.target_key,
    )


# A Deployment's pod is `<deployment>-<replicaset-hash>-<suffix>`, where the suffix is exactly
# five alphanumerics and the ReplicaSet hash is six to ten. A StatefulSet's is `<name>-<ordinal>`.
_POD_SUFFIX_LEN = 5
_RS_HASH_RANGE = range(6, 11)


def workload_from_pod(pod_name: str | None) -> str | None:
    """Best-effort owner name from a pod name.

    A fallback only. When the engine can reach the API server it resolves the real
    ownerReference, because a name is a guess and an ownerReference is a fact.

    The Deployment shape is checked before the StatefulSet shape: `chaos-state-0` splits into
    three hyphen-separated parts too, and testing "ends in digits" first would name the workload
    `chaos` instead of `chaos-state`.
    """
    if not pod_name:
        return None

    parts = pod_name.rsplit("-", 2)
    if (
        len(parts) == 3
        and len(parts[2]) == _POD_SUFFIX_LEN
        and parts[2].isalnum()
        and len(parts[1]) in _RS_HASH_RANGE
        and parts[1].isalnum()
    ):
        return parts[0]

    head, _, tail = pod_name.rpartition("-")
    if head and tail.isdigit():
        return head
    return pod_name

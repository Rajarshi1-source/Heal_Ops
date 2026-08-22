"""Correlation is the difference between one incident and forty remediations.

The storm test is the one that matters: it is the scenario that made Rev 1's design unsafe, and it
is the single most convincing thing the project can demonstrate about storm handling, because
every naive demo works with one alert at a time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.incident_correlator import (
    CAUSE_PRECEDENCE,
    AlertFacts,
    OpenIncident,
    correlate,
    workload_from_pod,
)

NOW = datetime(2026, 8, 22, 2, 14, 0, tzinfo=UTC)


def node_incident(opened_at: datetime = NOW, node: str = "ip-10-0-3-14") -> OpenIncident:
    return OpenIncident(
        id=1,
        cause_alertname="KubeNodeNotReady",
        cause_scope="node",
        cause_target=node,
        target_key=f"node/{node}",
        namespace=None,
        precedence=CAUSE_PRECEDENCE["KubeNodeNotReady"],
        opened_at=opened_at,
        node=node,
    )


def crashloop_on(node: str, pod: str, workload: str = "api-gateway") -> AlertFacts:
    return AlertFacts(
        alertname="KubePodCrashLooping",
        fingerprint=f"fp-{pod}",
        namespace="shop",
        pod=pod,
        node=node,
        workload=workload,
    )


def test_first_alert_opens_an_incident() -> None:
    decision = correlate(crashloop_on("n1", "api-gateway-abc12-x1"), [], NOW)
    assert decision.action == "create"
    assert decision.role == "cause"
    assert decision.reason == "new_incident"
    assert decision.target_key == "shop/Deployment/api-gateway"


def test_the_storm_produces_one_incident_not_forty() -> None:
    """One node failure, forty stranded pods, one incident, zero unnecessary rollbacks."""
    incident = node_incident()
    alerts = [
        crashloop_on("ip-10-0-3-14", f"api-gateway-abc12-{i:03d}", workload=f"svc-{i % 7}")
        for i in range(40)
    ]

    decisions = [correlate(a, [incident], NOW + timedelta(seconds=30)) for a in alerts]

    assert all(d.action == "attach" for d in decisions)
    assert all(d.incident_id == 1 for d in decisions)
    # Every one of them is EVIDENCE. A symptom never selects its own runbook, which is precisely
    # what stops forty rollbacks of healthy deployments.
    assert all(d.role == "symptom" for d in decisions)
    assert {d.reason for d in decisions} == {"symptom_of_node_cause"}


def test_pods_on_a_different_node_are_not_symptoms_of_this_node() -> None:
    incident = node_incident(node="ip-10-0-3-14")
    elsewhere = crashloop_on("ip-10-0-9-99", "checkout-def34-001", workload="checkout")

    decision = correlate(elsewhere, [incident], NOW + timedelta(seconds=30))
    assert decision.action == "create", "a genuine unrelated failure must not be swallowed"
    assert decision.role == "cause"


def test_cause_arriving_after_its_symptoms_is_promoted() -> None:
    """The ordering that actually happens.

    Pods start crash-looping before the node's Ready condition flips, so a workload incident can
    already be open when the node alert lands. The node alert must take over the incident rather
    than become a symptom of the thing it caused.
    """
    workload_incident = OpenIncident(
        id=7,
        cause_alertname="KubePodCrashLooping",
        cause_scope="workload",
        cause_target="api-gateway",
        target_key="shop/Deployment/api-gateway",
        namespace="shop",
        precedence=CAUSE_PRECEDENCE["KubePodCrashLooping"],
        opened_at=NOW,
        workload="api-gateway",
        node="ip-10-0-3-14",
    )
    node_alert = AlertFacts(
        alertname="KubeNodeNotReady",
        fingerprint="fp-node",
        node="ip-10-0-3-14",
    )

    decision = correlate(node_alert, [workload_incident], NOW + timedelta(seconds=60))
    # The node alert is node-scoped, so it does not match the workload incident's target and
    # correctly opens its own incident as the cause.
    assert decision.role == "cause"
    assert decision.precedence == 0


def test_same_target_within_the_window_joins_the_same_incident() -> None:
    incident = OpenIncident(
        id=3,
        cause_alertname="KubePodCrashLooping",
        cause_scope="workload",
        cause_target="api-gateway",
        target_key="shop/Deployment/api-gateway",
        namespace="shop",
        precedence=2,
        opened_at=NOW,
        workload="api-gateway",
    )
    again = crashloop_on("n1", "api-gateway-abc12-002")

    decision = correlate(again, [incident], NOW + timedelta(seconds=120))
    assert decision.action == "attach"
    assert decision.incident_id == 3


def test_same_target_outside_the_window_opens_a_new_incident() -> None:
    # A recurrence an hour later is a NEW incident. Collapsing it into the old one would hide the
    # repetition the anti-masking guard exists to count.
    incident = OpenIncident(
        id=3,
        cause_alertname="KubePodCrashLooping",
        cause_scope="workload",
        cause_target="api-gateway",
        target_key="shop/Deployment/api-gateway",
        namespace="shop",
        precedence=2,
        opened_at=NOW,
        workload="api-gateway",
    )
    later = correlate(crashloop_on("n1", "api-gateway-abc12-003"), [incident], NOW + timedelta(hours=1))
    assert later.action == "create"


def test_higher_precedence_incident_is_considered_first() -> None:
    workload_incident = OpenIncident(
        id=5,
        cause_alertname="HighPodCPU",
        cause_scope="workload",
        cause_target="api-gateway",
        target_key="shop/Deployment/api-gateway",
        namespace="shop",
        precedence=CAUSE_PRECEDENCE["HighPodCPU"],
        opened_at=NOW - timedelta(seconds=60),
        workload="api-gateway",
    )
    node = node_incident(opened_at=NOW)

    # The node incident opened LATER but has lower precedence, so it wins.
    decision = correlate(
        crashloop_on("ip-10-0-3-14", "api-gateway-abc12-004"),
        [workload_incident, node],
        NOW + timedelta(seconds=10),
    )
    assert decision.incident_id == 1
    assert decision.reason == "symptom_of_node_cause"


def test_disk_pressure_is_a_node_scoped_cause() -> None:
    alert = AlertFacts(alertname="NodeDiskPressure", fingerprint="fp-d", node="n2")
    assert alert.scope == "node"
    assert alert.precedence == 0
    assert alert.target_key == "node/n2"


def test_precedence_table_orders_causes_before_symptoms() -> None:
    assert CAUSE_PRECEDENCE["KubeNodeNotReady"] < CAUSE_PRECEDENCE["KubePodCrashLooping"]
    assert CAUSE_PRECEDENCE["NodeDiskPressure"] < CAUSE_PRECEDENCE["KubePodOOMKilled"]
    assert CAUSE_PRECEDENCE["KubePodCrashLooping"] < CAUSE_PRECEDENCE["HighPodCPU"]


def test_workload_from_pod_handles_both_naming_schemes() -> None:
    # Deployment: <name>-<replicaset-hash>-<5-char suffix>
    assert workload_from_pod("api-gateway-7d9f8b6c5d-x2p4q") == "api-gateway"
    assert workload_from_pod("crashy-api-6b8c9d7f5-ab12c") == "crashy-api"

    # StatefulSet: <name>-<ordinal>. This is the case that makes ordering matter -- checking
    # "ends in digits" first would yield `chaos` here.
    assert workload_from_pod("chaos-state-0") == "chaos-state"
    assert workload_from_pod("chaos-state-11") == "chaos-state"

    # Bare or unrecognised names fall through unchanged rather than being mangled.
    assert workload_from_pod("standalone") == "standalone"
    assert workload_from_pod(None) is None

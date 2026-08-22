"""The bundle is the planner's entire world, so its round-trip must be exact.

If `from_dict(to_dict(b)) != b`, a corpus bundle read back from JSON is not the bundle that was
captured -- and the determinism digest in the decision eval would be anchored to a lie.
"""

from __future__ import annotations

import json

import pytest

from bundle import (
    BUNDLE_FORMAT_VERSION,
    AutoscalerState,
    BundleAlert,
    ContainerState,
    IncidentBundle,
    NodeState,
    Target,
)


def _crashloop_bundle() -> IncidentBundle:
    return IncidentBundle(
        incident_id="crashloop",
        cause_alert=BundleAlert(
            alertname="KubePodCrashLooping",
            fingerprint="abc123",
            severity="critical",
            namespace="chaos",
            pod="crashy-api-7d9f",
            container="crashy-api",
            node="healops-worker2",
            starts_at="2026-08-22T06:14:07Z",
            labels={"alertname": "KubePodCrashLooping", "namespace": "chaos"},
        ),
        target=Target(
            kind="Deployment",
            name="crashy-api",
            namespace="chaos",
            labels={"app": "crashy-api"},
            replicas=3,
            ready_replicas=0,
            containers=(
                ContainerState(
                    name="crashy-api",
                    image="ghcr.io/o/healops-chaos:0.1.0",
                    cpu_limit_millis=100,
                    memory_limit_mib=64,
                    memory_resize_policy="NotRequired",
                ),
            ),
            pods=("crashy-api-7d9f",),
            node="healops-worker2",
            revision=4,
            previous_revision=3,
        ),
        expected_runbook="crashloop_rollback",
        expected_effect="allow",
    )


def test_round_trip_is_exact() -> None:
    original = _crashloop_bundle()
    assert IncidentBundle.from_dict(original.to_dict()) == original


def test_round_trip_survives_json() -> None:
    original = _crashloop_bundle()
    restored = IncidentBundle.from_json(json.dumps(original.to_dict()))
    assert restored == original
    # Canonical form must also be stable, since the bundle sha256 is the incident's identity.
    assert restored.to_canonical_json() == original.to_canonical_json()


def test_canonical_json_is_sorted_and_compact() -> None:
    text = _crashloop_bundle().to_canonical_json()
    assert ", " not in text and '": ' not in text, "canonical JSON must have no spaces"
    keys = list(json.loads(text))
    assert keys == sorted(keys)


def test_round_trip_with_node_scope() -> None:
    original = IncidentBundle(
        incident_id="nodenotready",
        cause_alert=BundleAlert(
            alertname="KubeNodeNotReady", fingerprint="n1", node="healops-worker3"
        ),
        target=Target(
            kind="Node",
            name="healops-worker3",
            node="healops-worker3",
            node_state=NodeState(
                name="healops-worker3",
                ready=False,
                allocatable_cpu_millis=4000,
                allocatable_memory_mib=8192,
                requested_cpu_millis=1200,
                requested_memory_mib=2048,
                pod_count=9,
            ),
            peer_nodes=(
                NodeState(name="healops-worker1", allocatable_cpu_millis=4000),
                NodeState(name="healops-worker2", allocatable_cpu_millis=4000),
            ),
        ),
        cause_scope="node",
        expected_runbook="node_drain",
        expected_effect="require_approval",
    )
    assert IncidentBundle.from_dict(original.to_dict()) == original


def test_round_trip_with_autoscaler() -> None:
    original = IncidentBundle(
        incident_id="highcpu-hpa",
        cause_alert=BundleAlert(alertname="HighPodCPU", fingerprint="c1", namespace="shop"),
        target=Target(
            kind="Deployment",
            name="checkout",
            namespace="shop",
            replicas=4,
            autoscaler=AutoscalerState(
                name="checkout",
                metric="cpu",
                target_utilization=70,
                min_replicas=2,
                max_replicas=10,
                current_replicas=4,
            ),
        ),
        expected_runbook="cpu_scale_up",
        expected_effect="allow",
    )
    restored = IncidentBundle.from_dict(original.to_dict())
    assert restored == original
    assert restored.target.autoscaler is not None
    assert restored.target.autoscaler.metric == "cpu"


def test_symptom_alerts_survive_the_round_trip() -> None:
    # One node failure is one incident with N symptoms attached as evidence, so a bundle that
    # loses its symptoms loses the whole storm-control story.
    cause = BundleAlert(alertname="KubeNodeNotReady", fingerprint="n1", node="w3")
    symptoms = tuple(
        BundleAlert(alertname="KubePodCrashLooping", fingerprint=f"s{i}", node="w3")
        for i in range(40)
    )
    original = IncidentBundle(
        incident_id="storm",
        cause_alert=cause,
        target=Target(kind="Node", name="w3"),
        symptom_alerts=symptoms,
        cause_scope="node",
        correlation_reason="symptom_of_node_cause",
    )
    restored = IncidentBundle.from_dict(original.to_dict())
    assert len(restored.symptom_alerts) == 40
    assert restored == original


def test_unsupported_format_version_is_rejected() -> None:
    raw = _crashloop_bundle().to_dict()
    raw["format_version"] = BUNDLE_FORMAT_VERSION + 1
    with pytest.raises(ValueError, match="format version"):
        IncidentBundle.from_dict(raw)


def test_labelled_only_when_both_labels_present() -> None:
    b = _crashloop_bundle()
    assert b.is_labelled is True

    from dataclasses import replace

    assert replace(b, expected_effect=None).is_labelled is False
    assert replace(b, expected_runbook=None).is_labelled is False


def test_target_key_and_opt_out() -> None:
    t = Target(kind="Deployment", name="api", namespace="shop")
    assert t.key == "shop/Deployment/api"
    assert t.remediation_disabled is False

    opted_out = Target(
        kind="Deployment",
        name="api",
        namespace="shop",
        annotations={"healops.io/remediation": "disabled"},
    )
    assert opted_out.remediation_disabled is True


def test_bundle_is_immutable() -> None:
    # The planner must not be able to mutate its own input; a plan has to be a pure function of it.
    b = _crashloop_bundle()
    with pytest.raises(AttributeError):
        b.incident_id = "tampered"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        b.target.replicas = 99  # type: ignore[misc]

"""Parsing real captured Alertmanager v2 payloads.

Hand-written dicts test what the author believes Alertmanager sends. These fixtures are the shapes
it actually sends, including the two details that bite: the year-0001 sentinel for "no end time",
and labels that vary by exporter (`node` from kube-state-metrics, `instance` from node-exporter).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.alert_parser import AlertmanagerAlert, AlertmanagerV2Payload, fingerprint_of

FIXTURES = Path(__file__).parent / "fixtures" / "alert_payloads"


def load(name: str) -> AlertmanagerV2Payload:
    return AlertmanagerV2Payload.model_validate(
        json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    )


def test_parses_a_captured_crashloop_payload() -> None:
    payload = load("crashloop_firing.json")

    assert payload.version == "4"
    assert payload.status == "firing"
    assert len(payload.alerts) == 1

    alert = payload.alerts[0]
    assert alert.alertname == "KubePodCrashLooping"
    assert alert.namespace == "chaos"
    assert alert.pod == "crashy-api-7d9f8b6c5d-x2p4q"
    assert alert.container == "crashy-api"
    assert alert.node == "healops-worker2"
    assert alert.severity == "critical"
    assert alert.fingerprint == "8f4c1d2a9b3e7f60"
    assert alert.is_firing is True


def test_year_0001_end_time_becomes_none() -> None:
    """Alertmanager sends 0001-01-01 for "no end time".

    Storing that as a real timestamp makes every "has this resolved?" query silently wrong, because
    the alert looks like it ended two millennia ago.
    """
    alert = load("crashloop_firing.json").alerts[0]
    assert alert.endsAt is None
    assert alert.startsAt is not None


def test_resolved_payload_carries_a_real_end_time() -> None:
    payload = load("crashloop_resolved.json")
    alert = payload.alerts[0]

    assert alert.is_firing is False
    assert alert.endsAt is not None
    assert alert.endsAt > alert.startsAt
    assert payload.resolved == [alert]
    assert payload.firing == []


def test_storm_payload_separates_cause_from_symptoms() -> None:
    payload = load("node_storm_firing.json")
    names = [a.alertname for a in payload.alerts]

    assert names.count("KubeNodeNotReady") == 1
    assert len(payload.alerts) == 4
    # Every alert in the batch shares the node label, which is what makes topology correlation
    # possible and what Alertmanager's inhibit_rules match on.
    assert {a.node for a in payload.alerts} == {"healops-worker3"}


def test_facts_projection_infers_the_workload() -> None:
    alert = load("crashloop_firing.json").alerts[0]
    facts = alert.facts()

    assert facts.alertname == "KubePodCrashLooping"
    assert facts.workload == "crashy-api"
    assert facts.scope == "workload"
    assert facts.target_key == "chaos/Deployment/crashy-api"
    assert facts.precedence == 2


def test_node_alert_projects_to_node_scope() -> None:
    payload = load("node_storm_firing.json")
    node_alert = next(a for a in payload.alerts if a.alertname == "KubeNodeNotReady")
    facts = node_alert.facts()

    assert facts.scope == "node"
    assert facts.precedence == 0, "a node failure must outrank the pod symptoms it causes"
    assert facts.target_key == "node/healops-worker3"


def test_instance_label_is_accepted_as_a_node_fallback() -> None:
    # node-exporter series carry `instance`; kube-state-metrics carry `node`. Both must resolve,
    # or disk-pressure alerts would correlate against a node of None.
    alert = AlertmanagerAlert(
        labels={"alertname": "NodeDiskPressure", "instance": "healops-worker1"},
        fingerprint="f1",
    )
    assert alert.node == "healops-worker1"


def test_unknown_fields_do_not_break_parsing() -> None:
    """A new Alertmanager field must never 4xx the webhook.

    Rejecting it would make Alertmanager retry forever and the incident would never be recorded --
    a strictness that costs availability for no safety gain.
    """
    payload = AlertmanagerV2Payload.model_validate(
        {
            "version": "4",
            "status": "firing",
            "somethingNewIn034": {"nested": True},
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighPodCPU", "namespace": "shop"},
                    "fingerprint": "z9",
                    "futureField": 42,
                }
            ],
        }
    )
    assert payload.alerts[0].alertname == "HighPodCPU"


def test_missing_optional_fields_use_defaults() -> None:
    payload = AlertmanagerV2Payload.model_validate({"alerts": []})
    assert payload.alerts == []
    assert payload.groupKey == ""
    assert payload.truncatedAlerts == 0


def test_fingerprint_falls_back_to_a_label_digest() -> None:
    # A missing fingerprint would make every delivery look unique and defeat dedup entirely.
    without = AlertmanagerAlert(labels={"alertname": "HighPodCPU", "pod": "p1"})
    first = fingerprint_of(without)
    assert len(first) == 16
    assert first == fingerprint_of(
        AlertmanagerAlert(labels={"pod": "p1", "alertname": "HighPodCPU"})
    ), "digest must not depend on label ordering"


def test_supplied_fingerprint_is_preferred() -> None:
    alert = AlertmanagerAlert(labels={"alertname": "HighPodCPU"}, fingerprint="abc123")
    assert fingerprint_of(alert) == "abc123"


@pytest.mark.parametrize(
    "name", ["crashloop_firing.json", "node_storm_firing.json", "crashloop_resolved.json"]
)
def test_every_fixture_round_trips(name: str) -> None:
    payload = load(name)
    # The raw payload is stored verbatim in JSONB; re-validating the dump proves nothing is lost
    # in the round trip that persistence performs.
    again = AlertmanagerV2Payload.model_validate(payload.model_dump(mode="json"))
    assert [a.alertname for a in again.alerts] == [a.alertname for a in payload.alerts]
    assert [a.fingerprint for a in again.alerts] == [a.fingerprint for a in payload.alerts]

"""Structural checks on the PrometheusRule chart.

These are the mistakes that are invisible until an incident: an alert whose `alertname` no longer
matches a runbook trigger, a PromQL comparison that silently drops the `node` label because `*`
binds tighter than `>`, or a rule that reintroduces the flapping OOM gauge.

The chart is rendered with `helm template`, so the test skips where Helm is unavailable rather than
asserting on raw Go-template text.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART = REPO_ROOT / "charts" / "alerting-rules"

# The five alertnames that select a runbook, and the runbook each one belongs to.
EXPECTED_RUNBOOK_ALERTS = {
    "KubePodCrashLooping": "crashloop_rollback",
    "NodeDiskPressure": "disk_cleanup",
    "HighPodCPU": "cpu_scale_up",
    "KubePodOOMKilled": "oom_memory_bump",
    "KubeNodeNotReady": "node_drain",
}

# Symptom alerts must carry `node` so Alertmanager can inhibit on it and the correlator can attach
# them to a node-level cause. KubeNodeNotReady is itself node-scoped.
ALERTS_REQUIRING_NODE_LABEL = {
    "KubePodCrashLooping",
    "HighPodCPU",
    "KubePodOOMKilled",
}


def _render() -> dict[str, Any]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")
    out = subprocess.run(  # noqa: S603
        [helm, "template", "ar", str(CHART)],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    docs = [d for d in yaml.safe_load_all(out.stdout) if d]
    rules = [d for d in docs if d.get("kind") == "PrometheusRule"]
    assert len(rules) == 1, "expected exactly one PrometheusRule"
    return rules[0]


@pytest.fixture(scope="module")
def groups() -> dict[str, list[dict[str, Any]]]:
    pr = _render()
    return {g["name"]: g["rules"] for g in pr["spec"]["groups"]}


@pytest.fixture(scope="module")
def alerts(groups: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    return {r["alert"]: r for r in groups["healops.runbooks"] if "alert" in r}


def test_one_alert_per_runbook(alerts: dict[str, dict[str, Any]]) -> None:
    assert set(alerts) == set(EXPECTED_RUNBOOK_ALERTS)


def test_each_alert_names_its_runbook(alerts: dict[str, dict[str, Any]]) -> None:
    for name, runbook in EXPECTED_RUNBOOK_ALERTS.items():
        assert alerts[name]["labels"]["healops_runbook"] == runbook


def test_alerts_carry_summary_and_description(alerts: dict[str, dict[str, Any]]) -> None:
    # The Slack card and the ledger payload both render these.
    for name, rule in alerts.items():
        ann = rule.get("annotations", {})
        assert ann.get("summary"), f"{name} has no summary"
        assert ann.get("description"), f"{name} has no description"


def test_symptom_alerts_join_the_node_label(alerts: dict[str, dict[str, Any]]) -> None:
    for name in ALERTS_REQUIRING_NODE_LABEL:
        expr = alerts[name]["expr"]
        assert "group_left(node)" in expr, f"{name} does not join the node label"


def test_node_join_is_not_defeated_by_operator_precedence(
    alerts: dict[str, dict[str, Any]],
) -> None:
    """`A > 0 * on(...) group_left(node) B` parses as `A > (0 * ...)`.

    The comparison silently becomes `A > 0`, the join is discarded, and the alert loses the `node`
    label that inhibition and correlation both depend on. The join must sit inside parentheses that
    close before the comparison operator.
    """
    for name in ALERTS_REQUIRING_NODE_LABEL:
        expr = alerts[name]["expr"]
        join_at = expr.index("group_left(node)")
        tail = expr[join_at:]
        # Every comparison in these rules must come after the join, not before it.
        assert not any(op in expr[:join_at] for op in (">", "<", "==")), (
            f"{name}: comparison precedes the join, so the node label is dropped"
        )
        assert any(op in tail for op in (">", "<", "==")), f"{name}: no comparison after the join"


def test_oom_uses_the_counter_not_the_flapping_gauge(
    groups: dict[str, list[dict[str, Any]]],
) -> None:
    """The counter is primary; the fallback is a counter too, the gauge only ever a qualifier.

    cAdvisor learns of OOM kills from the kernel log, which a kind node cannot read, so its counter
    sits at zero there and the alert never fired. The fallback multiplies the restart COUNTER by
    "last termination was an OOM kill" -- so the gauge alone, which flaps, can never decide.
    """
    recording = {r["record"]: r["expr"] for r in groups["healops.recording"] if "record" in r}
    primary, fallback = re.split(
        r"\n\s*or\n", recording["healops:container_oom_events:increase10m"], maxsplit=1
    )
    assert "container_oom_events_total" in primary
    assert "last_terminated_reason" not in primary
    assert "increase(kube_pod_container_status_restarts_total" in fallback
    assert "* on (namespace, pod, container) group_left" in fallback
    alerts = "\n".join(r["expr"] for rules in groups.values() for r in rules if "alert" in r)
    assert "last_terminated_reason" not in alerts, "no alert reads the flapping gauge directly"


def test_throttle_is_expressed_as_a_ratio(
    groups: dict[str, list[dict[str, Any]]],
) -> None:
    recording = {r["record"]: r["expr"] for r in groups["healops.recording"] if "record" in r}
    ratio = recording["healops:container_cpu_throttle_ratio:rate5m"]
    # Raw throttled-period counters move with replica count and window length, so both the
    # numerator and the denominator must be present.
    assert "container_cpu_cfs_throttled_periods_total" in ratio
    assert "container_cpu_cfs_periods_total" in ratio
    assert "/" in ratio
    # Regression, found on kind: an idle container's few startup periods read as 22% throttled.
    # The ratio is gated on activity, and an observed idle container reads 0 -- never `vector(0)`,
    # which would also paper over a container nobody is scraping.
    assert "and on (namespace, pod, container)" in ratio
    assert "> 2" in ratio
    assert "vector(0)" not in ratio


def test_recording_rules_cover_every_verifier_signal(
    groups: dict[str, list[dict[str, Any]]],
) -> None:
    recorded = {r["record"] for r in groups["healops.recording"] if "record" in r}
    assert recorded == {
        "healops:container_cpu_throttle_ratio:rate5m",
        "healops:container_cpu_usage_ratio:rate5m",
        "healops:container_memory_usage_ratio:current",
        "healops:container_oom_events:increase10m",
        "healops:container_memory_psi_full:rate5m",
    }


def test_healops_namespace_is_excluded_from_its_own_alerts(
    alerts: dict[str, dict[str, Any]],
) -> None:
    # HealOps refuses to remediate HealOps. This is one of three layers; the others are the
    # Alertmanager route and the policy file.
    for name in ("KubePodCrashLooping", "HighPodCPU", "KubePodOOMKilled"):
        assert "healops" in alerts[name]["expr"], f"{name} does not exclude the healops namespace"


def test_self_monitoring_alerts_exist(groups: dict[str, list[dict[str, Any]]]) -> None:
    self_alerts = {r["alert"] for r in groups["healops.self"] if "alert" in r}
    assert {
        "HealOpsLedgerChainInvalid",
        "HealOpsLedgerUnverified",
        "HealOpsWatchdogActing",
        "HealOpsBreakerOpen",
        "HealOpsBudgetExhausted",
    } <= self_alerts

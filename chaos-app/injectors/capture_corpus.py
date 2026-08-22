"""Generate the labelled incident corpus from a live kind cluster.

The corpus is **generated, not hand-written**: the harness induces each fault, waits for the alert
to fire, captures the resulting Alertmanager payload plus the cluster state of the target, and
writes a labelled bundle. Hand-written fixtures encode what the author *believes* the cluster does;
captured ones encode what it actually did.

Bundles are committed as JSON so `evals/decision_eval.py` needs no cluster.

Usage:
    python chaos-app/injectors/capture_corpus.py --out corpus/
    python chaos-app/injectors/capture_corpus.py --out corpus/ --only crashloop
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "engine" / "src"))

from bundle import (  # noqa: E402
    AutoscalerState,
    BundleAlert,
    ContainerState,
    IncidentBundle,
    NodeState,
    Target,
)

DEFAULT_ALERTMANAGER = "http://localhost:9093"
CHAOS_NS = "chaos"

# Every scenario the corpus must contain. The four must-refuse cases are not optional: refusal
# recall is graded at 100%, and a remediation engine that never refuses is a liability.
SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "crashloop",
        "alertname": "KubePodCrashLooping",
        "workload": "crashy-api",
        "expected_runbook": "crashloop_rollback",
        "expected_effect": "allow",
        "setup": [
            "kubectl",
            "-n",
            CHAOS_NS,
            "set",
            "env",
            "deployment/crashy-api",
            "CRASH_AFTER=20s",
        ],
        "teardown": [
            "kubectl",
            "-n",
            CHAOS_NS,
            "set",
            "env",
            "deployment/crashy-api",
            "CRASH_AFTER-",
        ],
        "note": "A bad revision on top of a good one; rollout undo is a genuine fix.",
    },
    {
        "id": "oomkill",
        "alertname": "KubePodOOMKilled",
        "workload": "hungry-worker",
        "expected_runbook": "oom_memory_bump",
        "expected_effect": "allow",
        "note": "In-place resize; reversible, blast radius of one pod.",
    },
    {
        "id": "highcpu",
        "alertname": "HighPodCPU",
        "workload": "cpu-burner",
        "expected_runbook": "cpu_scale_up",
        "expected_effect": "allow",
        "setup": [
            "kubectl",
            "-n",
            CHAOS_NS,
            "exec",
            "deployment/cpu-burner",
            "--",
            "python",
            "-c",
            "import urllib.request;urllib.request.urlopen(urllib.request.Request("
            "'http://localhost:8080/burn?seconds=900&workers=4',method='POST'))",
        ],
        "note": "No HPA on this workload, so replicas are the correct control to touch.",
    },
    {
        "id": "diskpressure",
        "alertname": "NodeDiskPressure",
        "workload": "log-spammer",
        "expected_runbook": "disk_cleanup",
        "expected_effect": "allow",
        "note": "Archive-then-delete above a 7-day floor.",
    },
    {
        "id": "nodenotready",
        "alertname": "KubeNodeNotReady",
        "workload": None,
        "expected_runbook": "node_drain",
        "expected_effect": "require_approval",
        "note": "Drain is partially irreversible, so it always requires a human.",
    },
    # --- the must-refuse cases ------------------------------------------------------------------
    {
        "id": "refuse-statefulset",
        "alertname": "KubePodCrashLooping",
        "workload": "chaos-state",
        "kind": "StatefulSet",
        "expected_runbook": "crashloop_rollback",
        "expected_effect": "deny",
        "note": "Stateful workloads are denied outright.",
    },
    {
        "id": "refuse-single-replica",
        "alertname": "KubePodCrashLooping",
        "workload": "lonely-api",
        "expected_runbook": "crashloop_rollback",
        "expected_effect": "deny",
        "note": "One replica means no safe blast radius for a disruptive fix.",
    },
    {
        "id": "refuse-opt-out",
        "alertname": "KubePodCrashLooping",
        "workload": "hands-off-api",
        "expected_runbook": "crashloop_rollback",
        "expected_effect": "deny",
        "note": "Workload-level kill switch via the healops.io/remediation annotation.",
    },
    {
        "id": "refuse-resize-policy",
        "alertname": "KubePodOOMKilled",
        "workload": "restart-on-resize",
        "expected_runbook": "oom_memory_bump",
        "expected_effect": "refuse",
        "note": "resizePolicy RestartContainer for memory: 'no restart' would be a lie.",
    },
]


def _kubectl_json(args: list[str]) -> dict[str, Any]:
    exe = shutil.which("kubectl")
    if exe is None:
        raise SystemExit("kubectl is not on PATH")
    proc = subprocess.run(  # noqa: S603
        [exe, *args, "-o", "json"], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(args)} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def _run(cmd: list[str]) -> None:
    exe = shutil.which(cmd[0])
    if exe is None:
        raise SystemExit(f"{cmd[0]} is not on PATH")
    subprocess.run([exe, *cmd[1:]], check=False, capture_output=True)  # noqa: S603


def _quantity_to_millis(raw: str | None) -> int | None:
    if not raw:
        return None
    return int(float(raw[:-1])) if raw.endswith("m") else int(float(raw) * 1000)


def _quantity_to_mib(raw: str | None) -> int | None:
    if not raw:
        return None
    units = {
        "Ki": 1 / 1024,
        "Mi": 1,
        "Gi": 1024,
        "Ti": 1024 * 1024,
        "K": 1 / 1024,
        "M": 1,
        "G": 1024,
    }
    for suffix, factor in units.items():
        if raw.endswith(suffix):
            return int(float(raw[: -len(suffix)]) * factor)
    return int(float(raw) / (1024 * 1024))


def _resize_policy(container: dict[str, Any], resource: str) -> str | None:
    for entry in container.get("resizePolicy", []) or []:
        if entry.get("resourceName") == resource:
            return str(entry.get("restartPolicy"))
    return None


def _container_state(c: dict[str, Any]) -> ContainerState:
    res = c.get("resources", {})
    requests = res.get("requests", {})
    limits = res.get("limits", {})
    return ContainerState(
        name=c["name"],
        image=c["image"],
        cpu_request_millis=_quantity_to_millis(requests.get("cpu")),
        cpu_limit_millis=_quantity_to_millis(limits.get("cpu")),
        memory_request_mib=_quantity_to_mib(requests.get("memory")),
        memory_limit_mib=_quantity_to_mib(limits.get("memory")),
        memory_resize_policy=_resize_policy(c, "memory"),
        cpu_resize_policy=_resize_policy(c, "cpu"),
    )


def fetch_firing_alerts(
    alertname: str, base_url: str, timeout_s: int = 420
) -> list[dict[str, Any]]:
    """Poll the Alertmanager **v2** API. v1 was removed in 0.27."""
    url = f"{base_url}/api/v2/alerts?active=true&silenced=false&inhibited=false"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
                alerts = json.loads(resp.read())
        except OSError as exc:
            print(f"  alertmanager unreachable ({exc}); retrying", flush=True)
            time.sleep(10)
            continue
        matched = [a for a in alerts if a.get("labels", {}).get("alertname") == alertname]
        if matched:
            return matched
        print(f"  waiting for {alertname}...", flush=True)
        time.sleep(15)
    return []


def to_bundle_alert(raw: dict[str, Any]) -> BundleAlert:
    labels = raw.get("labels", {})
    return BundleAlert(
        alertname=labels.get("alertname", ""),
        fingerprint=raw.get("fingerprint", ""),
        status="firing",
        severity=labels.get("severity", "warning"),
        namespace=labels.get("namespace"),
        pod=labels.get("pod"),
        container=labels.get("container"),
        node=labels.get("node"),
        starts_at=raw.get("startsAt"),
        labels=labels,
        annotations=raw.get("annotations", {}),
    )


def capture_workload(namespace: str, kind: str, name: str) -> Target:
    obj = _kubectl_json(["get", kind.lower(), name, "-n", namespace])
    spec, meta = obj["spec"], obj["metadata"]
    pod_spec = spec["template"]["spec"]

    containers = tuple(_container_state(c) for c in pod_spec["containers"])

    pods = _kubectl_json(["get", "pods", "-n", namespace, "-l", f"app={name}"]).get("items", [])
    pod_names = tuple(sorted(p["metadata"]["name"] for p in pods))

    autoscaler = None
    for hpa in _kubectl_json(["get", "hpa", "-n", namespace]).get("items", []):
        ref = hpa["spec"].get("scaleTargetRef", {})
        if ref.get("name") == name:
            metrics = hpa["spec"].get("metrics", []) or []
            resource = next((m for m in metrics if m.get("type") == "Resource"), None)
            autoscaler = AutoscalerState(
                name=hpa["metadata"]["name"],
                metric=(resource or {}).get("resource", {}).get("name", "external"),
                target_utilization=(resource or {})
                .get("resource", {})
                .get("target", {})
                .get("averageUtilization"),
                min_replicas=hpa["spec"].get("minReplicas", 1),
                max_replicas=hpa["spec"].get("maxReplicas", 1),
                current_replicas=hpa.get("status", {}).get("currentReplicas", 1),
            )
            break

    labels = meta.get("labels", {}) or {}
    if labels.get("argocd.argoproj.io/instance"):
        manager = "argocd"
    elif labels.get("kustomize.toolkit.fluxcd.io/name"):
        manager = "flux"
    else:
        manager = "unmanaged"

    revision = meta.get("annotations", {}).get("deployment.kubernetes.io/revision")
    return Target(
        kind=kind,
        name=name,
        namespace=namespace,
        labels=labels,
        annotations=meta.get("annotations", {}) or {},
        replicas=spec.get("replicas", 1),
        ready_replicas=obj.get("status", {}).get("readyReplicas", 0) or 0,
        containers=containers,
        pods=pod_names,
        node=(pods[0].get("spec", {}).get("nodeName") if pods else None),
        autoscaler=autoscaler,
        gitops_manager=manager,  # type: ignore[arg-type]
        revision=int(revision) if revision else None,
        previous_revision=(int(revision) - 1 if revision and int(revision) > 1 else None),
        server_supports_in_place_resize=True,
    )


def capture_node(node_name: str) -> Target:
    node = _kubectl_json(["get", "node", node_name])
    conditions = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}
    alloc = node.get("status", {}).get("allocatable", {})

    def _node_state(obj: dict[str, Any]) -> NodeState:
        st = obj.get("status", {})
        conds = {c["type"]: c["status"] for c in st.get("conditions", [])}
        a = st.get("allocatable", {})
        return NodeState(
            name=obj["metadata"]["name"],
            ready=conds.get("Ready") == "True",
            schedulable=not obj["spec"].get("unschedulable", False),
            allocatable_cpu_millis=_quantity_to_millis(a.get("cpu")) or 0,
            allocatable_memory_mib=_quantity_to_mib(a.get("memory")) or 0,
        )

    peers = tuple(
        _node_state(n)
        for n in _kubectl_json(["get", "nodes"]).get("items", [])
        if n["metadata"]["name"] != node_name
        and "node-role.kubernetes.io/control-plane" not in (n["metadata"].get("labels") or {})
    )

    return Target(
        kind="Node",
        name=node_name,
        namespace=None,
        labels=node["metadata"].get("labels", {}) or {},
        annotations=node["metadata"].get("annotations", {}) or {},
        node=node_name,
        node_state=NodeState(
            name=node_name,
            ready=conditions.get("Ready") == "True",
            schedulable=not node["spec"].get("unschedulable", False),
            allocatable_cpu_millis=_quantity_to_millis(alloc.get("cpu")) or 0,
            allocatable_memory_mib=_quantity_to_mib(alloc.get("memory")) or 0,
        ),
        peer_nodes=peers,
    )


def capture_scenario(scenario: dict[str, Any], out_dir: Path, alertmanager: str) -> bool:
    print(f"[{scenario['id']}] inducing {scenario['alertname']}", flush=True)
    if setup := scenario.get("setup"):
        _run(setup)

    try:
        raw_alerts = fetch_firing_alerts(scenario["alertname"], alertmanager)
        if not raw_alerts:
            print(f"[{scenario['id']}] alert never fired -- skipping", file=sys.stderr)
            return False

        workload = scenario.get("workload")
        if workload is None:
            node = raw_alerts[0]["labels"].get("node", "")
            target = capture_node(node)
            cause_scope = "node"
        else:
            relevant = [a for a in raw_alerts if workload in (a["labels"].get("pod") or "")]
            raw_alerts = relevant or raw_alerts
            target = capture_workload(CHAOS_NS, scenario.get("kind", "Deployment"), workload)
            cause_scope = "workload"

        cause = to_bundle_alert(raw_alerts[0])
        bundle = IncidentBundle(
            incident_id=scenario["id"],
            cause_alert=cause,
            target=target,
            symptom_alerts=tuple(to_bundle_alert(a) for a in raw_alerts[1:]),
            cause_scope=cause_scope,  # type: ignore[arg-type]
            correlation_reason="captured_by_chaos_harness",
            opened_at=cause.starts_at,
            expected_runbook=scenario["expected_runbook"],
            expected_effect=scenario["expected_effect"],
            label_note=scenario.get("note"),
        )

        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{scenario['id']}.json"
        path.write_text(json.dumps(bundle.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        print(f"[{scenario['id']}] wrote {path}", flush=True)
        return True
    finally:
        if teardown := scenario.get("teardown"):
            _run(teardown)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, default=REPO_ROOT / "corpus")
    p.add_argument("--only", action="append", help="scenario id; repeatable")
    p.add_argument("--alertmanager", default=DEFAULT_ALERTMANAGER)
    args = p.parse_args()

    selected = [s for s in SCENARIOS if not args.only or s["id"] in args.only]
    captured = sum(capture_scenario(s, args.out, args.alertmanager) for s in selected)

    print(f"\ncaptured {captured}/{len(selected)} scenarios into {args.out}")
    must_refuse = sum(1 for s in selected if s["expected_effect"] in ("deny", "refuse"))
    print(f"must-refuse cases in this run: {must_refuse}")
    return 0 if captured == len(selected) else 1


if __name__ == "__main__":
    raise SystemExit(main())

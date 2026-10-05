"""Generate the labelled incident corpus -- and the committed evidence -- from a live kind cluster.

The corpus is **generated, not hand-written**, in two ways, and every case says which:

* ``harvest`` reads the incidents the in-cluster engine actually handled (after ``make chaos``):
  each one's evidence bundle goes to ``evidence/`` with its golden replay in ``evidence/replay/``,
  and the planner input the engine recorded on its ``plan_requested`` ledger entry becomes a corpus
  case. Alert and target are both real -- the alert came from Prometheus through Alertmanager.
* ``scenarios`` sets up a cluster state the end-to-end faults do not reach (a GitOps-managed
  workload, an HPA, a replica ceiling, a cordoned pool, a frozen breaker...), captures the target
  with the engine's own capture code -- the exact reads the worker makes -- and synthesises the
  alert in the shape Alertmanager delivers. The target is real; the alert is labelled as made.

**Labels are never read off the outcome.** Each comes from the tables below, written from what the
policy file and the runbook contracts require. Labelling a case with what the planner happened to
decide would make the eval grade the planner against itself.

Bundles are committed as JSON so ``evals/decision_eval.py`` needs no cluster.

    python chaos-app/injectors/capture_corpus.py harvest       # after `make chaos`
    python chaos-app/injectors/capture_corpus.py scenarios
    python chaos-app/injectors/capture_corpus.py scenarios --only cpu-at-replica-ceiling
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO_ROOT / "engine" / "src"), str(REPO_ROOT / "engine"), str(REPO_ROOT)]

from tests.chaos import harness  # noqa: E402 - the chaos suite's cluster access, shared on purpose
from tests.chaos.harness import CHAOS_NS, kubectl  # noqa: E402

CORPUS = REPO_ROOT / "corpus"
EVIDENCE = REPO_ROOT / "evidence"
REPLAY = EVIDENCE / "replay"

CRASHLOOP = "crashloop_rollback"
OOM = "oom_memory_bump"
CPU = "cpu_scale_up"
DISK = "disk_cleanup"
DRAIN = "node_drain"


@dataclass(frozen=True)
class Label:
    case: str
    runbook: str
    effect: str
    note: str


# --- harvest: labels for the incidents the end-to-end chaos tests cause ---------------------------

#: (cause alert, target key) -> label. A target key of `node/*` matches any node.
HARVEST_LABELS: dict[tuple[str, str], Label] = {
    ("KubePodCrashLooping", f"{CHAOS_NS}/Deployment/crashy-api"): Label(
        "e2e-crashloop-bad-config",
        CRASHLOOP,
        "allow",
        "A bad config rollout on an unchanged image; the previous template differs, so a rollback "
        "is a genuine fix. Three replicas, internal: inside every automatic threshold.",
    ),
    ("KubePodCrashLooping", f"{CHAOS_NS}/Deployment/chaos-state"): Label(
        "e2e-crashloop-statefulset",
        CRASHLOOP,
        "deny",
        "Stateful workloads are never rolled back by automation (deny-protected-workloads).",
    ),
    ("KubePodCrashLooping", f"{CHAOS_NS}/Deployment/lonely-api"): Label(
        "e2e-crashloop-nothing-to-roll-back-to",
        CRASHLOOP,
        "refuse",
        "Crash-looping since its first revision (or since a restart that changed nothing): there "
        "is no different template to roll back to.",
    ),
    ("KubePodCrashLooping", f"{CHAOS_NS}/Deployment/hands-off-api"): Label(
        "e2e-crashloop-opt-out",
        CRASHLOOP,
        "deny",
        "The workload opted out with healops.io/remediation: disabled (deny-kill-switch).",
    ),
    ("KubePodCrashLooping", f"{CHAOS_NS}/Deployment/restart-on-resize"): Label(
        "e2e-crashloop-oom-looping-first-revision",
        CRASHLOOP,
        "refuse",
        "Crash-looping because it OOMs, on its only revision: a rollback has nowhere to go.",
    ),
    ("KubePodOOMKilled", f"{CHAOS_NS}/Deployment/hungry-worker"): Label(
        "e2e-oom-fixable",
        OOM,
        "allow",
        "A real OOM kill below the memory ceiling, with an observed peak: an in-place resize, "
        "reversible, one pod, no restart.",
    ),
    ("KubePodOOMKilled", f"{CHAOS_NS}/Deployment/restart-on-resize"): Label(
        "e2e-oom-restart-policy",
        OOM,
        "refuse",
        "resizePolicy RestartContainer for memory: an 'in-place' bump would restart it.",
    ),
    ("HighPodCPU", f"{CHAOS_NS}/Deployment/cpu-burner"): Label(
        "e2e-cpu-load",
        CPU,
        "allow",
        "CPU from load through the Service, no HPA: two more small replicas, under the cost line.",
    ),
    ("NodeDiskPressure", "node/*"): Label(
        "e2e-disk-old-logs",
        DISK,
        "require_approval",
        "Archive-then-delete on a node: node-scoped and only partially reversible, so a human "
        "approves. (The alert was delivered by the suite; kind cannot fill a real disk.)",
    ),
    ("KubeNodeNotReady", "node/*"): Label(
        "e2e-node-dead-kubelet",
        DRAIN,
        "require_approval",
        "A genuinely dead kubelet with capacity elsewhere: a drain is always a human's call.",
    ),
}


def _label_for(alertname: str, target_key: str) -> Label | None:
    if target_key.startswith("node/"):
        return HARVEST_LABELS.get((alertname, "node/*"))
    return HARVEST_LABELS.get((alertname, target_key))


def _planner_input(bundle: dict[str, Any]) -> dict[str, Any] | None:
    for entry in bundle.get("ledger") or []:
        if entry["event_type"] == "plan_requested":
            raw = (entry.get("payload") or {}).get("input")
            if raw is not None:
                return dict(raw)
    return None


def harvest(engine: harness.Engine) -> int:
    import evidence
    import replay

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    REPLAY.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()
    for summary in sorted(engine.incidents(), key=lambda i: i["id"]):
        if summary["state"] not in harness.TERMINAL:
            continue  # an open incident's bundle is still changing; it is not evidence yet
        payload = engine.evidence(summary["id"])
        bundle = payload["bundle"]
        digest = evidence.sha256(bundle)
        if digest != payload["sha256"]:
            raise SystemExit(f"incident {summary['id']}: bundle changed in transit")
        evidence.write(bundle, EVIDENCE)
        (REPLAY / f"{digest}.txt").write_text(
            replay.render(bundle, digest), encoding="utf-8", newline="\n"
        )

        label = _label_for(summary["cause_alertname"], summary["target_key"])
        raw = _planner_input(bundle)
        if label is None or raw is None:
            print(f"  #{summary['id']} {summary['target_key']}: evidence only (no label)")
            continue
        if label.case in written:
            # One case per situation: forty restarts of the same fixture are one data point.
            continue
        case = {
            **raw,
            "incident_id": label.case,
            "expected_runbook": label.runbook,
            "expected_effect": label.effect,
            "label_note": f"{label.note} [captured end to end: incident #{summary['id']}, "
            f"evidence {digest[:12]}]",
        }
        _write_case(label.case, case)
        written.add(label.case)
        print(f"  #{summary['id']} -> corpus/{label.case}.json, evidence {digest[:12]}")
    return len(written)


# --- scenarios: cluster states the end-to-end faults do not reach ---------------------------------


@dataclass
class Scenario:
    id: str
    alertname: str
    label: Label
    target: Callable[[], dict[str, Any]]
    setup: Callable[[], None] = lambda: None
    teardown: Callable[[], None] = lambda: None
    context: dict[str, Any] = field(default_factory=dict)


def _steps(*steps: Callable[[], object]) -> Callable[[], None]:
    def run() -> None:
        for step in steps:
            step()

    return run


def _k(*args: str) -> Callable[[], object]:
    return lambda: kubectl(*args)


def _env(deployment: str, assignment: str) -> Callable[[], object]:
    return _steps(
        _k("-n", CHAOS_NS, "set", "env", f"deployment/{deployment}", assignment),
        lambda: harness.rollout_settled(f"deployment/{deployment}"),
    )


def _apply(manifest: str) -> Callable[[], object]:
    return lambda: kubectl("apply", "-f", "-", stdin=manifest)


def _workload(
    name: str, *, container: str | None = None, namespace: str = CHAOS_NS
) -> Callable[[], dict[str, Any]]:
    def spec() -> dict[str, Any]:
        pods = harness.pods_of(name, namespace)
        return {
            "namespace": namespace,
            "name": name,
            "pod": pods[0]["metadata"]["name"] if pods else None,
            "container": container or name,
            "node": pods[0]["spec"].get("nodeName") if pods else None,
        }

    return spec


def _node(pick: Callable[[], str]) -> Callable[[], dict[str, Any]]:
    def spec() -> dict[str, Any]:
        node = pick()
        return {"namespace": None, "name": node, "node": node}

    return spec


def _pool(name: str) -> list[str]:
    nodes = harness.kubectl_json("get", "nodes", "-l", f"healops.io/pool={name}")["items"]
    return sorted(n["metadata"]["name"] for n in nodes)


def _spammer_node() -> str:
    return str(harness.pods_of("log-spammer")[0]["spec"]["nodeName"])


def _quiet_general_node() -> str:
    return next(n for n in _pool("general") if n != _spammer_node())


def _platform_node() -> str:
    return _pool("platform")[0]


def _clear_stale_seed_files(node: str) -> None:
    """Make "nothing old enough" true rather than assumed.

    log-spammer seeds back-dated files under its hostPath, and they outlive the pod: a node it ran
    on earlier still holds 60 MiB of genuinely old logs, and the planner was right to plan their
    cleanup. Only the chaos fixture's own directory is cleared; the node's system logs are not.
    """
    harness.docker_exec(node, "rm", "-rf", "/var/log/chaos")


def _cordon_peers() -> None:
    victim = _quiet_general_node()
    for node in harness.worker_nodes():
        if node != victim:
            kubectl("cordon", node)


def _uncordon_all() -> None:
    for node in harness.worker_nodes():
        kubectl("uncordon", node)


SCRATCH = """
apiVersion: v1
kind: Namespace
metadata: { name: scratch }
---
apiVersion: apps/v1
kind: Deployment
metadata: { name: scratch-api, namespace: scratch }
spec:
  replicas: 2
  selector: { matchLabels: { app: scratch-api } }
  template:
    metadata: { labels: { app: scratch-api } }
    spec:
      nodeSelector: { healops.io/pool: general }
      containers:
        - name: scratch-api
          image: ghcr.io/rajarshi1-source/healops-chaos:0.1.0
          imagePullPolicy: IfNotPresent
          args: ["crashy-api"]
          env: [{ name: REVISION, value: "%s" }]
          resources: { requests: { cpu: 20m, memory: 32Mi }, limits: { memory: 64Mi } }
"""

BIG = f"""
apiVersion: apps/v1
kind: Deployment
metadata: {{ name: big-api, namespace: {CHAOS_NS} }}
spec:
  replicas: 2
  selector: {{ matchLabels: {{ app: big-api }} }}
  template:
    metadata: {{ labels: {{ app: big-api }} }}
    spec:
      nodeSelector: {{ healops.io/pool: general }}
      containers:
        - name: big-api
          image: ghcr.io/rajarshi1-source/healops-chaos:0.1.0
          imagePullPolicy: IfNotPresent
          args: ["hungry-worker"]
          env: [{{ name: TARGET_MIB, value: "32" }}]
          # Reserves a full core and 1Gi per replica: what a costly scale-out looks like. Mostly
          # idle; its 4Gi limit is the memory ceiling the OOM runbook will not go past.
          resources:
            requests: {{ cpu: "1", memory: 1Gi }}
            limits: {{ memory: 4Gi }}
          resizePolicy: [{{ resourceName: memory, restartPolicy: NotRequired }}]
"""

CPU_HPA = f"""
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: {{ name: cpu-burner-cpu, namespace: {CHAOS_NS} }}
spec:
  scaleTargetRef: {{ apiVersion: apps/v1, kind: Deployment, name: cpu-burner }}
  minReplicas: 2
  maxReplicas: 4
  metrics:
    - type: Resource
      resource: {{ name: cpu, target: {{ type: Utilization, averageUtilization: 80 }} }}
"""

EXTERNAL_HPA = f"""
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: {{ name: cpu-burner-queue, namespace: {CHAOS_NS} }}
spec:
  scaleTargetRef: {{ apiVersion: apps/v1, kind: Deployment, name: cpu-burner }}
  minReplicas: 2
  maxReplicas: 6
  metrics:
    - type: External
      external:
        metric: {{ name: queue_depth }}
        target: {{ type: AverageValue, averageValue: "30" }}
"""

_scratch_up = _steps(
    _apply(SCRATCH % "1"),
    lambda: harness.rollout_settled("deployment/scratch-api", namespace="scratch"),
    _apply(SCRATCH % "2"),
    lambda: harness.rollout_settled("deployment/scratch-api", namespace="scratch"),
)
_big_up = _steps(_apply(BIG), lambda: harness.rollout_settled("deployment/big-api"))
_big_down = _k("-n", CHAOS_NS, "delete", "deployment", "big-api", "--ignore-not-found")

SCENARIOS: list[Scenario] = [
    # --- crashloop_rollback -------------------------------------------------------------------
    Scenario(
        "crashloop-same-template",
        "KubePodCrashLooping",
        Label(
            "crashloop-same-template",
            CRASHLOOP,
            "refuse",
            "The previous revision differs only by kubectl's restartedAt stamp: rolling back a "
            "restart restores nothing, so the cause is outside the spec.",
        ),
        _workload("crashy-api"),
        setup=_steps(
            _k("-n", CHAOS_NS, "rollout", "restart", "deployment/crashy-api"),
            lambda: harness.rollout_settled("deployment/crashy-api"),
        ),
    ),
    Scenario(
        "crashloop-gitops-managed",
        "KubePodCrashLooping",
        Label(
            "crashloop-gitops-managed",
            CRASHLOOP,
            "allow",
            "A bad rollout on an Argo CD-managed workload: still a genuine rollback, but an "
            "ephemeral, TTL'd mitigation with reconciliation paused; the PR is the real fix.",
        ),
        _workload("crashy-api"),
        setup=_steps(
            _k(
                "-n",
                CHAOS_NS,
                "label",
                "deployment/crashy-api",
                "argocd.argoproj.io/instance=chaos",
                "--overwrite",
            ),
            _env("crashy-api", "CRASH_AFTER=900s"),
        ),
        teardown=_steps(
            _k("-n", CHAOS_NS, "label", "deployment/crashy-api", "argocd.argoproj.io/instance-"),
            _env("crashy-api", "CRASH_AFTER-"),
        ),
    ),
    Scenario(
        "crashloop-user-facing-wide",
        "KubePodCrashLooping",
        Label(
            "crashloop-user-facing-wide",
            CRASHLOOP,
            "require_approval",
            "A user-facing workload at six replicas: blast radius 6 + 20 = 26 > 25, so a human "
            "approves even a reversible rollback.",
        ),
        _workload("crashy-api"),
        setup=_steps(
            _k("-n", CHAOS_NS, "label", "deployment/crashy-api", "tier=frontend", "--overwrite"),
            _k("-n", CHAOS_NS, "scale", "deployment/crashy-api", "--replicas=6"),
            _env("crashy-api", "CRASH_AFTER=900s"),
        ),
        teardown=_steps(
            _k("-n", CHAOS_NS, "label", "deployment/crashy-api", "tier-"),
            _k("-n", CHAOS_NS, "scale", "deployment/crashy-api", "--replicas=3"),
            _env("crashy-api", "CRASH_AFTER-"),
        ),
    ),
    Scenario(
        "crashloop-budget-nearly-spent",
        "KubePodCrashLooping",
        Label(
            "crashloop-budget-nearly-spent",
            CRASHLOOP,
            "require_approval",
            "A fixable bad rollout -- with 17 of the hour's 20 actions already used. Past 80% of "
            "the budget a human sees every action before it lands.",
        ),
        _workload("crashy-api"),
        setup=_env("crashy-api", "CRASH_AFTER=900s"),
        teardown=_env("crashy-api", "CRASH_AFTER-"),
        context={"used_actions": 17},
    ),
    Scenario(
        "crashloop-outside-allowed-namespaces",
        "KubePodCrashLooping",
        Label(
            "crashloop-outside-allowed-namespaces",
            CRASHLOOP,
            "deny",
            "A rollable bad revision in a namespace automation was never granted "
            "(deny-outside-allowed-namespaces).",
        ),
        _workload("scratch-api", namespace="scratch"),
        setup=_scratch_up,
        teardown=_k("delete", "namespace", "scratch", "--wait=false"),
    ),
    Scenario(
        "crashloop-healops-itself",
        "KubePodCrashLooping",
        Label(
            "crashloop-healops-itself",
            CRASHLOOP,
            "deny",
            "HealOps never remediates HealOps (deny-self-remediation), whatever its pods do.",
        ),
        _workload("healops-engine", container="engine", namespace="healops"),
    ),
    # --- oom_memory_bump ----------------------------------------------------------------------
    Scenario(
        "oom-single-replica",
        "KubePodOOMKilled",
        Label(
            "oom-single-replica",
            OOM,
            "allow",
            "One replica, but an in-place resize is reversible and restarts nothing, so the "
            "single-replica guard -- which is about disruptive plans -- does not apply.",
        ),
        _workload("hungry-worker"),
    ),
    Scenario(
        "oom-statefulset",
        "KubePodOOMKilled",
        Label(
            "oom-statefulset",
            OOM,
            "deny",
            "An OOMKill on a StatefulSet's pod: stateful workloads are never touched by automation "
            "(deny-protected-workloads).",
        ),
        _workload("chaos-state", container="crashy"),
    ),
    Scenario(
        "oom-at-ceiling",
        "KubePodOOMKilled",
        Label(
            "oom-at-ceiling",
            OOM,
            "refuse",
            "Already at the 4Gi memory ceiling: more memory is not the fix, a code change is.",
        ),
        _workload("big-api"),
        setup=_big_up,
        teardown=_big_down,
    ),
    Scenario(
        "oom-opt-out",
        "KubePodOOMKilled",
        Label(
            "oom-opt-out",
            OOM,
            "deny",
            "The workload opted out with healops.io/remediation: disabled (deny-kill-switch).",
        ),
        _workload("hands-off-api", container="crashy"),
    ),
    # --- cpu_scale_up -------------------------------------------------------------------------
    Scenario(
        "cpu-hpa-on-cpu",
        "HighPodCPU",
        Label(
            "cpu-hpa-on-cpu",
            CPU,
            "allow",
            "An HPA owns the replica count, so the runbook lowers the HPA's CPU target instead of "
            "patching replicas the HPA would put back. Small pods: under the cost line.",
        ),
        _workload("cpu-burner"),
        setup=_apply(CPU_HPA),
        teardown=_k("-n", CHAOS_NS, "delete", "hpa", "cpu-burner-cpu", "--ignore-not-found"),
    ),
    Scenario(
        "cpu-hpa-external",
        "HighPodCPU",
        Label(
            "cpu-hpa-external",
            CPU,
            "refuse",
            "An HPA scaling on queue depth: nudging a CPU target would fight the real signal.",
        ),
        _workload("cpu-burner"),
        setup=_apply(EXTERNAL_HPA),
        teardown=_k("-n", CHAOS_NS, "delete", "hpa", "cpu-burner-queue", "--ignore-not-found"),
    ),
    Scenario(
        "cpu-at-replica-ceiling",
        "HighPodCPU",
        Label(
            "cpu-at-replica-ceiling",
            CPU,
            "refuse",
            "Already at the 10-replica ceiling: more replicas is not the answer to a CPU alert.",
        ),
        _workload("cpu-burner"),
        setup=_steps(
            _k("-n", CHAOS_NS, "scale", "deployment/cpu-burner", "--replicas=10"),
            lambda: harness.rollout_settled("deployment/cpu-burner"),
        ),
        teardown=_k("-n", CHAOS_NS, "scale", "deployment/cpu-burner", "--replicas=2"),
    ),
    Scenario(
        "cpu-costly",
        "HighPodCPU",
        Label(
            "cpu-costly",
            CPU,
            "require_approval",
            "Two more replicas that each reserve a core and 1Gi: thousands of rupees a month, "
            "past the automatic cost line.",
        ),
        _workload("big-api"),
        setup=_big_up,
        teardown=_big_down,
    ),
    Scenario(
        "cpu-opt-out",
        "HighPodCPU",
        Label(
            "cpu-opt-out",
            CPU,
            "deny",
            "The workload opted out with healops.io/remediation: disabled (deny-kill-switch).",
        ),
        _workload("hands-off-api", container="crashy"),
    ),
    # --- disk_cleanup -------------------------------------------------------------------------
    Scenario(
        "disk-old-logs",
        "NodeDiskPressure",
        Label(
            "disk-old-logs",
            DISK,
            "require_approval",
            "Old logs above the 7-day floor on the node: archive-then-delete is node-scoped and "
            "partially reversible, so a human approves.",
        ),
        _node(_spammer_node),
    ),
    Scenario(
        "disk-nothing-old-enough",
        "NodeDiskPressure",
        Label(
            "disk-nothing-old-enough",
            DISK,
            "refuse",
            "Nothing on this node is both past the 7-day floor and worth archiving: deleting "
            "fresh logs is not a fix, so it refuses and a human looks.",
        ),
        _node(_quiet_general_node),
        setup=lambda: _clear_stale_seed_files(_quiet_general_node()),
    ),
    Scenario(
        "disk-during-freeze",
        "NodeDiskPressure",
        Label(
            "disk-during-freeze",
            DISK,
            "deny",
            "`healopsctl freeze` is in force (breaker open): no action of any kind.",
        ),
        _node(_spammer_node),
        context={"breaker_state": "open"},
    ),
    # --- node_drain ---------------------------------------------------------------------------
    Scenario(
        "drain-with-capacity",
        "KubeNodeNotReady",
        Label(
            "drain-with-capacity",
            DRAIN,
            "require_approval",
            "Peers can absorb 120% of what moves: a drain is plannable, and always a human's call.",
        ),
        _node(_quiet_general_node),
    ),
    Scenario(
        "drain-no-capacity",
        "KubeNodeNotReady",
        Label(
            "drain-no-capacity",
            DRAIN,
            "refuse",
            "Every other worker is cordoned: there is nowhere to move the pods to.",
        ),
        _node(_quiet_general_node),
        setup=_cordon_peers,
        teardown=_uncordon_all,
    ),
    Scenario(
        "drain-during-planned-maintenance",
        "KubeNodeNotReady",
        Label(
            "drain-during-planned-maintenance",
            DRAIN,
            "deny",
            "A node alert during a planned drain, with automation frozen for the maintenance "
            "(breaker open): the humans already draining it do not need a second drain.",
        ),
        _node(_quiet_general_node),
        context={"breaker_state": "open"},
    ),
    Scenario(
        "drain-budget-exhausted",
        "KubeNodeNotReady",
        Label(
            "drain-budget-exhausted",
            DRAIN,
            "deny",
            "The hour's action budget is spent: a cluster that needs a 21st action is having a "
            "systemic problem, and a human should be looking at it.",
        ),
        _node(_quiet_general_node),
        context={"used_actions": 20},
    ),
    Scenario(
        "drain-platform-node",
        "KubeNodeNotReady",
        Label(
            "drain-platform-node",
            DRAIN,
            "deny",
            "The node hosts HealOps itself, so a drain would move HealOps' own pods "
            "(deny-self-remediation).",
        ),
        _node(_platform_node),
    ),
]


async def _capture(scenario: Scenario) -> dict[str, Any]:
    import worker
    from bundle import BundleAlert, IncidentBundle

    spec = scenario.target()
    is_node = spec["namespace"] is None
    target = await harness.live_target(
        scenario.alertname, namespace=spec["namespace"], name=spec["name"]
    )
    annotations = (
        await worker.scan_archive_candidates(spec["name"])
        if scenario.alertname == "NodeDiskPressure"
        else {}
    )
    labels = {"alertname": scenario.alertname, "severity": "warning"}
    labels.update(
        {k: str(spec[k]) for k in ("namespace", "pod", "container", "node") if spec.get(k)}
    )
    alert = BundleAlert(
        alertname=scenario.alertname,
        fingerprint=f"scenario-{scenario.id}",
        namespace=spec.get("namespace"),
        pod=spec.get("pod"),
        container=spec.get("container"),
        node=spec.get("node"),
        labels=labels,
        annotations=annotations,
    )
    bundle = IncidentBundle(
        incident_id=scenario.id,
        cause_alert=alert,
        target=target,
        cause_scope="node" if is_node else "workload",
        correlation_reason="captured_by_chaos_harness",
        expected_runbook=scenario.label.runbook,
        expected_effect=scenario.label.effect,  # type: ignore[arg-type]
        label_note=f"{scenario.label.note} [target captured live; alert synthesised]",
    )
    case = bundle.to_dict()
    if scenario.context:
        case["context"] = scenario.context
    return case


def run_scenarios(only: list[str] | None) -> int:
    done = 0
    for scenario in SCENARIOS:
        if only and scenario.id not in only:
            continue
        print(f"[{scenario.id}] setting up", flush=True)
        try:
            scenario.setup()
            _write_case(scenario.id, asyncio.run(_capture(scenario)))
            done += 1
            print(f"[{scenario.id}] -> corpus/{scenario.id}.json", flush=True)
        finally:
            scenario.teardown()
    return done


def _write_case(name: str, case: dict[str, Any]) -> None:
    CORPUS.mkdir(parents=True, exist_ok=True)
    (CORPUS / f"{name}.json").write_text(
        json.dumps(case, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="mode", required=True)
    sub.add_parser("harvest", help="evidence + corpus cases from the engine's real incidents")
    scen = sub.add_parser("scenarios", help="corpus cases from live cluster states")
    scen.add_argument("--only", action="append", help="scenario id; repeatable")
    args = p.parse_args()

    harness.require_cluster()
    harness.configure_in_process_engine()
    if args.mode == "harvest":
        with harness.port_forwarded_engine() as engine:
            count = harvest(engine)
        print(f"\nharvested {count} labelled cases; evidence in {EVIDENCE.relative_to(REPO_ROOT)}")
        return 0
    count = run_scenarios(args.only)
    print(f"\ncaptured {count} scenario cases into {CORPUS.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

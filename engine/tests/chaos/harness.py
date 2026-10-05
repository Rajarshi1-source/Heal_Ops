"""The chaos suite's view of a real kind cluster.

Two ways in, used for different claims:

* **End to end** -- a fault is injected into the chaos app, Prometheus fires the real alert,
  Alertmanager delivers it to the engine running *in the cluster*, and the test reads the verdict
  back through the engine's API. This is the only way to prove a **fix**: nothing in the path is
  stubbed, and "remediated" is the verifier's word, not the test's.
* **In process** -- the engine's own capture, planner and runbooks run from the test against the
  same cluster, with the same RBAC-shaped calls. This is how **compensation** is proven byte for
  byte, and how a refusal is shown on a live target for which no permanently failing fixture
  exists. The planner is a pure function of what the capture returns, so this is the same
  decision the in-cluster engine would make.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

CHAOS_NS = "chaos"
HEALOPS_NS = os.environ.get("HEALOPS_CHAOS_NAMESPACE", "healops")
# The local values file's secrets. CI installs the chart with the same file.
WEBHOOK_TOKEN = os.environ.get("HEALOPS_CHAOS_WEBHOOK_TOKEN", "change-me")
APPROVER_TOKEN = os.environ.get("HEALOPS_CHAOS_APPROVER_TOKEN", "local-dashboard-token")

#: Long, because every timer in the path is real: a `for:` duration, Alertmanager's group_wait, the
#: verifier's window. A fix test that times out has found something; one that passes early has not.
FIX_TIMEOUT_SECONDS = 25 * 60

#: From fault to incident. A crash loop only alerts once its back-off outlasts the rule's `for:`
#: (minutes, as the back-off doubles), and a notification into an Alertmanager group that already
#: exists waits for `group_interval` (5m). Measured on kind: up to ~17 minutes.
ALERT_TIMEOUT_SECONDS = 25 * 60

TERMINAL = ("remediated", "reverted", "escalated")


class ClusterUnavailable(RuntimeError):
    pass


def _exe(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise ClusterUnavailable(f"{name} is not on PATH")
    return found


def kubectl(*args: str, check: bool = True, stdin: str | None = None, timeout: int = 120) -> str:
    # Git Bash rewrites arguments that look like POSIX paths; kubectl must see them verbatim.
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    proc = subprocess.run(  # noqa: S603
        [_exe("kubectl"), *args],
        capture_output=True,
        text=True,
        check=False,
        input=stdin,
        timeout=timeout,
        env=env,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def kubectl_json(*args: str) -> Any:
    return json.loads(kubectl(*args, "-o", "json"))


def docker_exec(container: str, *args: str) -> str:
    """kind nodes are containers, so the node's own filesystem is one `docker exec` away."""
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    proc = subprocess.run(  # noqa: S603
        [_exe("docker"), "exec", container, *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return proc.stdout


def wait_until(
    predicate: Callable[[], Any], *, timeout: float, interval: float = 5.0, what: str = "condition"
) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(interval)


def require_cluster() -> None:
    """Skip, not fail, when there is no cluster: the chaos marker is opt-in, not a merge gate."""
    try:
        kubectl("get", "--raw", "/readyz", timeout=15)
        kubectl("-n", HEALOPS_NS, "get", "deployment", "healops-engine", timeout=15)
    except (ClusterUnavailable, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise ClusterUnavailable(f"no HealOps kind cluster reachable: {exc}") from exc


def configure_in_process_engine() -> None:
    """Point the engine's own code at the cluster from outside it.

    kind maps Prometheus and Alertmanager to the host (k8s/kind/cluster.yaml). Node work runs as
    a Job on the node, exactly as in the cluster, using the release's Secret for S3.
    """
    defaults = {
        "HEALOPS_PROMETHEUS_URL": "http://localhost:9090",
        "HEALOPS_ALERTMANAGER_URL": "http://localhost:9093",
        "HEALOPS_NODE_AGENT_MODE": "job",
        "HEALOPS_SELF_NAMESPACE": HEALOPS_NS,
        "HEALOPS_DISK_ARCHIVE_ROOTS": '["/var/log"]',
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    from config import get_settings

    get_settings.cache_clear()


# --- the in-cluster engine ------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Engine:
    """The in-cluster engine's REST API, through a port-forward."""

    base: str

    def _request(self, method: str, path: str, body: Any = None, token: str | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.base}{path}", data=data, method=method)  # noqa: S310
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.loads(response.read() or b"null")

    def incidents(self, namespace: str | None = None) -> list[dict[str, Any]]:
        query = f"?limit=200&namespace={namespace}" if namespace else "?limit=200"
        return list(self._request("GET", f"/api/incidents{query}")["items"])

    def incident(self, incident_id: int) -> dict[str, Any]:
        return dict(self._request("GET", f"/api/incidents/{incident_id}"))

    def evidence(self, incident_id: int) -> dict[str, Any]:
        return dict(self._request("GET", f"/api/incidents/{incident_id}/evidence"))

    def high_water_mark(self) -> int:
        """The newest incident id now, so a test only ever judges incidents it caused."""
        return max((i["id"] for i in self.incidents()), default=0)

    def wait_for_incident(
        self, *, after: int, target_key: str, alertname: str, timeout: float
    ) -> dict[str, Any]:
        def find() -> dict[str, Any] | None:
            return next(
                (
                    i
                    for i in self.incidents()
                    if i["id"] > after
                    and i["target_key"] == target_key
                    and i["cause_alertname"] == alertname
                ),
                None,
            )

        found: dict[str, Any] = wait_until(
            find, timeout=timeout, interval=10, what=f"a {alertname} incident on {target_key}"
        )
        return found

    def wait_for_state(
        self, incident_id: int, states: tuple[str, ...], *, timeout: float
    ) -> dict[str, Any]:
        def reached() -> dict[str, Any] | None:
            detail = self.incident(incident_id)
            return detail if detail["state"] in states else None

        found: dict[str, Any] = wait_until(
            reached, timeout=timeout, interval=10, what=f"incident {incident_id} in {states}"
        )
        return found

    def approve(self, execution_id: int) -> dict[str, Any]:
        return dict(
            self._request(
                "POST",
                f"/api/approvals/{execution_id}",
                {"decision": "approve", "decided_by": "chaos-suite"},
                token=APPROVER_TOKEN,
            )
        )

    def webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(
            self._request("POST", "/api/webhooks/alertmanager", payload, token=WEBHOOK_TOKEN)
        )


@contextlib.contextmanager
def port_forwarded_engine() -> Iterator[Engine]:
    port = _free_port()
    proc = subprocess.Popen(  # noqa: S603
        [
            _exe("kubectl"),
            "-n",
            HEALOPS_NS,
            "port-forward",
            "svc/healops-engine",
            f"{port}:8000",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    engine = Engine(f"http://127.0.0.1:{port}")

    def live() -> bool:
        try:
            with urllib.request.urlopen(f"{engine.base}/livez", timeout=3):  # noqa: S310
                return True
        except urllib.error.URLError, OSError:
            return False

    try:
        wait_until(live, timeout=60, interval=1, what="the engine port-forward")
        yield engine
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def healopsctl(*args: str) -> str:
    """The CLI inside the engine pod: same database, same role, same ledger as the engine."""
    return kubectl(
        "-n",
        HEALOPS_NS,
        "exec",
        "deploy/healops-engine",
        "--",
        "python",
        "-m",
        "cli.healopsctl",
        *args,
    )


def breaker_state() -> str:
    """The global breaker as `healopsctl breaker-status` reports it: closed, open or half_open."""
    first = healopsctl("breaker-status").splitlines()[0].split()
    return first[1] if len(first) > 1 else "unknown"


@contextlib.contextmanager
def promoted(runbook: str, namespace: str, mode: str = "auto") -> Iterator[None]:
    """Grant a runbook authority for one test -- on the record -- and always take it back.

    Every rung starts at shadow. A fix test needs the runbook to act, so it is promoted by hand,
    which the ledger records as exactly that, and returned to shadow however the test ends.
    """
    healopsctl(
        "promote",
        runbook,
        namespace,
        "--to",
        mode,
        "--reason",
        "chaos suite: fix test",
        "--owner",
        "chaos-suite",
    )
    try:
        yield
    finally:
        healopsctl(
            "promote",
            runbook,
            namespace,
            "--to",
            "shadow",
            "--reason",
            "chaos suite: fix test finished",
            "--owner",
            "chaos-suite",
        )


# --- the in-process path --------------------------------------------------------------------------


async def live_target(
    alertname: str, *, namespace: str | None = None, name: str, node: str | None = None
) -> Any:
    """The engine's own capture of a live target -- the exact reads the worker makes."""
    import worker

    scope = "node" if alertname in ("KubeNodeNotReady", "NodeDiskPressure") else "workload"
    incident = SimpleNamespace(
        cause_scope=scope,
        cause_target=name,
        namespace=namespace,
        target_key=f"node/{name}" if scope == "node" else f"{namespace}/{name}",
        cause_alertname=alertname,
        node=node,
    )
    return await worker.build_target(incident)  # type: ignore[arg-type]


def bundle_for(
    alertname: str,
    target: Any,
    *,
    pod: str | None = None,
    container: str | None = None,
    annotations: dict[str, str] | None = None,
    incident_id: str = "chaos",
) -> Any:
    from bundle import BundleAlert, IncidentBundle

    scope = "node" if target.kind == "Node" else "workload"
    return IncidentBundle(
        incident_id=incident_id,
        cause_alert=BundleAlert(
            alertname=alertname,
            fingerprint=f"chaos-{alertname}",
            namespace=target.namespace,
            pod=pod,
            container=container,
            node=target.name if scope == "node" else target.node,
            annotations=annotations or {},
        ),
        target=target,
        cause_scope=scope,  # type: ignore[arg-type]
        correlation_reason="chaos_test",
    )


def declared(snapshot: dict[str, Any]) -> dict[str, Any]:
    """A deployment snapshot without the controller's revision counter.

    Kubernetes numbers every rollout afresh -- a rollback and a roll-forward produce revisions N+1
    and N+2, never N again -- so the counter cannot match by construction. Everything that was
    declared (template digest, images, resources, replicas, other annotations) must.
    """
    from k8s import deployment_ops

    kept = {k: v for k, v in snapshot.items() if k != "revision"}
    kept["annotations"] = {
        k: v
        for k, v in (snapshot.get("annotations") or {}).items()
        if k != deployment_ops.REVISION_ANNOTATION
    }
    return kept


def pods_of(deployment: str, namespace: str = CHAOS_NS) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = kubectl_json(
        "-n", namespace, "get", "pods", "-l", f"app={deployment}"
    )["items"]
    return [p for p in items if not p["metadata"].get("deletionTimestamp")]


def rollout_settled(kind_name: str, namespace: str = CHAOS_NS, timeout: int = 240) -> None:
    kubectl(
        "-n",
        namespace,
        "rollout",
        "status",
        kind_name,
        f"--timeout={timeout}s",
        timeout=timeout + 30,
    )


def worker_nodes() -> list[str]:
    nodes = kubectl_json("get", "nodes", "-l", "!node-role.kubernetes.io/control-plane")["items"]
    return sorted(n["metadata"]["name"] for n in nodes)


def node_ready(node: str) -> bool:
    obj = kubectl_json("get", "node", node)
    return any(c["type"] == "Ready" and c["status"] == "True" for c in obj["status"]["conditions"])

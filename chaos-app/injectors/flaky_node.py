"""flaky-node -- drives a node's Ready condition false.

Chaos fixture for runbook `node_drain`, and the source of the storm scenario: one node failure
produces `KubeNodeNotReady` plus a `KubePodCrashLooping` for every pod stranded on it.

Two mechanisms, because they prove different things:

* ``kubelet`` stops the kubelet inside the kind node container. The node genuinely goes NotReady
  after the node-monitor grace period, pods on it become unhealthy, and the alert fires from real
  cluster state. This is the honest fault and the default.
* ``taint`` only applies ``node.kubernetes.io/unreachable:NoExecute``. It evicts pods without
  making the node unready, which is useful for exercising the drain path in isolation but does
  **not** fire `KubeNodeNotReady`.

Both are reversible, and `restore` is idempotent so a failed demo cannot leave a node down.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time

UNREACHABLE_TAINT = "node.kubernetes.io/unreachable"


def _run(cmd: list[str], check: bool = True) -> str:
    exe = shutil.which(cmd[0])
    if exe is None:
        raise SystemExit(f"{cmd[0]} is not on PATH")
    proc = subprocess.run(  # noqa: S603
        [exe, *cmd[1:]], capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise SystemExit(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def list_worker_nodes() -> list[str]:
    out = _run(
        [
            "kubectl",
            "get",
            "nodes",
            "-l",
            "!node-role.kubernetes.io/control-plane",
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ]
    )
    return out.split()


def node_ready(node: str) -> bool | None:
    out = _run(
        [
            "kubectl",
            "get",
            "node",
            node,
            "-o",
            'jsonpath={.status.conditions[?(@.type=="Ready")].status}',
        ],
        check=False,
    )
    if not out:
        return None
    return out.strip() == "True"


def _kubelet(node: str, action: str) -> None:
    """kind runs each node as a container with systemd inside it."""
    _run(["docker", "exec", node, "systemctl", action, "kubelet"])


def break_node(node: str, mechanism: str) -> None:
    if mechanism == "kubelet":
        print(f"flaky-node: stopping kubelet on {node}", flush=True)
        _kubelet(node, "stop")
    else:
        print(f"flaky-node: tainting {node} as unreachable", flush=True)
        _run(["kubectl", "taint", "nodes", node, f"{UNREACHABLE_TAINT}=:NoExecute", "--overwrite"])


def restore_node(node: str, mechanism: str) -> None:
    """Idempotent: a demo that dies halfway must not leave a node down."""
    if mechanism == "kubelet":
        print(f"flaky-node: starting kubelet on {node}", flush=True)
        _kubelet(node, "start")
    else:
        _run(["kubectl", "taint", "nodes", node, f"{UNREACHABLE_TAINT}-"], check=False)
    # The runbook cordons before draining, so uncordon regardless of mechanism.
    _run(["kubectl", "uncordon", node], check=False)


def wait_for_ready(node: str, want: bool, timeout_s: int = 240) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        state = node_ready(node)
        if state is want:
            return True
        time.sleep(5)
    return False


def main() -> int:
    p = argparse.ArgumentParser(description="Make a node NotReady, and put it back.")
    p.add_argument("action", choices=["break", "restore", "status"])
    p.add_argument("--node", help="node name; defaults to the last worker")
    p.add_argument("--mechanism", choices=["kubelet", "taint"], default="kubelet")
    p.add_argument("--wait", action="store_true", help="block until the condition flips")
    args = p.parse_args()

    workers = list_worker_nodes()
    if not workers:
        print("no worker nodes found", file=sys.stderr)
        return 1
    # Default to the LAST worker: the drain runbook's capacity precheck needs the remaining nodes
    # to have headroom, and draining the first worker tends to be where the demo workloads sit.
    node = args.node or workers[-1]

    if args.action == "status":
        print(json.dumps({"node": node, "ready": node_ready(node), "workers": workers}, indent=2))
        return 0

    if args.action == "break":
        break_node(node, args.mechanism)
        if args.wait and not wait_for_ready(node, want=False):
            print(f"timed out waiting for {node} to go NotReady", file=sys.stderr)
            return 1
        return 0

    restore_node(node, args.mechanism)
    if args.wait and not wait_for_ready(node, want=True):
        print(f"timed out waiting for {node} to become Ready", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

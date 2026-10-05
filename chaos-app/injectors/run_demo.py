"""The five-fault demo, in sequence.

Mirrors the rehearsed demo script: five faults go in, three resolve automatically, one needs
approval, and one is refused by policy. The pauses are deliberate -- each fault needs its alert's
`for:` duration to elapse before the next one starts, or the storm-control logic groups faults that
were meant to be separate incidents.

    python chaos-app/injectors/run_demo.py            # the full run
    python chaos-app/injectors/run_demo.py --step 3   # one fault
    python chaos-app/injectors/run_demo.py --reset    # undo everything
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHAOS_NS = "chaos"


def _kubectl(*args: str, check: bool = False) -> str:
    exe = shutil.which("kubectl")
    if exe is None:
        raise SystemExit("kubectl is not on PATH")
    proc = subprocess.run(  # noqa: S603
        [exe, *args], capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _banner(step: int, title: str, expect: str) -> None:
    print(f"\n{'=' * 78}\n  STEP {step}: {title}\n  expect: {expect}\n{'=' * 78}", flush=True)


def step_1_crashloop() -> None:
    _banner(1, "CrashLoopBackOff on crashy-api", "auto-remediated by rollout undo, then VERIFIED")
    # Setting the env var creates revision N+1 on top of a healthy revision N. That is what makes
    # `rollout undo` a genuine fix rather than a coincidence -- and what lets verification honestly
    # report `remediated` instead of "the API call returned 200".
    _kubectl("-n", CHAOS_NS, "set", "env", "deployment/crashy-api", "CRASH_AFTER=20s", check=True)
    print("crashy-api now rolls out a revision that dies after 20s", flush=True)


def step_2_storm() -> None:
    _banner(
        2,
        "Node failure -> one incident, not forty",
        "1 incident with pod alerts attached as evidence; 0 unnecessary rollbacks",
    )
    subprocess.run(  # noqa: S603
        [sys.executable, str(Path(__file__).parent / "flaky_node.py"), "break", "--wait"],
        check=False,
    )


def step_3_oomkill() -> None:
    _banner(3, "OOMKill on hungry-worker", "in-place memory resize; restartCount stays UNCHANGED")
    # Above the 256Mi limit and below the 384Mi the runbook's bump produces: a fixable OOM.
    _kubectl("-n", CHAOS_NS, "set", "env", "deployment/hungry-worker", "TARGET_MIB=320", check=True)
    print("watch alongside: kubectl -n chaos get pod -l app=hungry-worker -w", flush=True)


def step_4_highcpu() -> None:
    _banner(4, "CPU saturation on cpu-burner", "scale-up within the replica ceiling, TTL'd")
    # Load through the Service, not a self-inflicted burn: only CPU that comes from requests is
    # something more replicas can fix, so only that makes the scale-up a genuine remediation.
    _kubectl("-n", CHAOS_NS, "scale", "deployment/cpu-load", "--replicas=1", check=True)


def step_5_refusal() -> None:
    _banner(
        5,
        "CrashLoop on a StatefulSet and an opt-out Deployment",
        "DENIED by policy -- knowing what not to automate is the feature",
    )
    _kubectl("-n", CHAOS_NS, "rollout", "restart", "statefulset/chaos-state")
    _kubectl("-n", CHAOS_NS, "rollout", "restart", "deployment/hands-off-api")


STEPS = {
    1: (step_1_crashloop, 240),
    2: (step_2_storm, 300),
    3: (step_3_oomkill, 240),
    4: (step_4_highcpu, 420),
    5: (step_5_refusal, 0),
}


def reset() -> None:
    print("resetting the chaos app to a healthy state", flush=True)
    _kubectl("-n", CHAOS_NS, "set", "env", "deployment/crashy-api", "CRASH_AFTER-")
    _kubectl("-n", CHAOS_NS, "scale", "deployment/cpu-load", "--replicas=0")
    _kubectl("-n", CHAOS_NS, "set", "env", "deployment/hungry-worker", "TARGET_MIB=128")
    subprocess.run(  # noqa: S603
        [sys.executable, str(Path(__file__).parent / "flaky_node.py"), "restore", "--wait"],
        check=False,
    )
    for kind, name in (
        ("deployment", "crashy-api"),
        ("deployment", "hungry-worker"),
        ("deployment", "cpu-burner"),
        ("deployment", "log-spammer"),
        ("statefulset", "chaos-state"),
    ):
        _kubectl("-n", CHAOS_NS, "rollout", "status", f"{kind}/{name}", "--timeout=120s")
    print("reset complete", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--step", type=int, choices=sorted(STEPS), help="run a single fault")
    p.add_argument("--reset", action="store_true", help="restore everything to healthy")
    p.add_argument("--no-wait", action="store_true", help="skip the inter-step pauses")
    args = p.parse_args()

    if args.reset:
        reset()
        return 0

    steps = [args.step] if args.step else sorted(STEPS)
    for i, number in enumerate(steps):
        fn, pause = STEPS[number]
        fn()
        if pause and not args.no_wait and i < len(steps) - 1:
            print(f"\n  ... waiting {pause}s for the alert to fire and HealOps to act ...")
            time.sleep(pause)

    print(
        "\nDemo faults injected. Watch the dashboard timeline, then run with --reset.\n"
        "Scorecard to expect: 3 auto-remediated, 1 approval, 1 policy refusal, "
        "0 false remediations.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

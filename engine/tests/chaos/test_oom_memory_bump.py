"""Runbook 4, oom_memory_bump, on a real cluster: an in-place resize that fixes an OOM without a
restart, undoes exactly, and refuses a container whose resize policy would restart it."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from tests.chaos import harness
from tests.chaos.harness import CHAOS_NS, kubectl

RUNBOOK = "oom_memory_bump"
DEPLOYMENT = "hungry-worker"
TARGET_KEY = f"{CHAOS_NS}/Deployment/{DEPLOYMENT}"
HEALTHY_TARGET_MIB = "128"
#: Above the 256Mi limit, below the 384Mi a 1.5x bump gives: a fault the runbook can really fix.
FAULT_TARGET_MIB = "320"


def _set_target(mib: str) -> None:
    kubectl("-n", CHAOS_NS, "set", "env", f"deployment/{DEPLOYMENT}", f"TARGET_MIB={mib}")


def _container_status(pod: str) -> dict[str, object]:
    obj = harness.kubectl_json("-n", CHAOS_NS, "get", "pod", pod)
    status: dict[str, object] = obj["status"]["containerStatuses"][0]
    return status


@pytest.fixture
def healthy(cluster: None) -> Iterator[None]:
    _set_target(HEALTHY_TARGET_MIB)
    harness.rollout_settled(f"deployment/{DEPLOYMENT}")
    yield
    _set_target(HEALTHY_TARGET_MIB)
    harness.rollout_settled(f"deployment/{DEPLOYMENT}")


async def test_compensation_resizes_back_exactly(healthy: None) -> None:
    """Resize in place, resize back, and compare the pod's ACTUAL resources with `==`.

    Actual means `status.containerStatuses[].resources` -- what the kubelet applied -- never the
    spec the API accepted.
    """
    from k8s import state_capture
    from planner import replan
    from runbooks import load_all

    [pod] = [p["metadata"]["name"] for p in harness.pods_of(DEPLOYMENT)]
    before = await state_capture.capture_pod_resources(CHAOS_NS, pod)

    target = await harness.live_target("KubePodOOMKilled", namespace=CHAOS_NS, name=DEPLOYMENT)
    plan = replan(harness.bundle_for("KubePodOOMKilled", target, pod=pod, container=DEPLOYMENT))
    assert plan is not None and not plan.is_refusal, plan and plan.refusal_reason
    assert plan.actions[0].subresource == "resize"

    runbook = load_all().get(RUNBOOK)
    assert runbook is not None
    executed = await runbook.execute(plan)
    assert executed.ok, executed.detail
    resized = await state_capture.capture_pod_resources(CHAOS_NS, pod)
    assert resized["containers"][0]["actual_memory_limit"] == "384Mi"
    # In place means in place: same container, no restart.
    assert resized["containers"][0]["restart_count"] == before["containers"][0]["restart_count"]

    compensated = await runbook.compensate(plan)
    assert compensated.ok, compensated.detail
    after = await state_capture.capture_pod_resources(CHAOS_NS, pod)

    assert after == before


def test_refuses_a_resize_that_would_restart_the_container(engine: harness.Engine) -> None:
    """`resizePolicy: RestartContainer` for memory makes "no restart" a lie, so it refuses --
    end to end, from a real OOM, with the runbook holding full authority."""
    mark = engine.high_water_mark()
    with harness.promoted(RUNBOOK, CHAOS_NS):
        kubectl("-n", CHAOS_NS, "rollout", "restart", "deployment/restart-on-resize")
        incident = engine.wait_for_incident(
            after=mark,
            target_key=f"{CHAOS_NS}/Deployment/restart-on-resize",
            alertname="KubePodOOMKilled",
            timeout=harness.ALERT_TIMEOUT_SECONDS,
        )
        detail = engine.wait_for_state(incident["id"], harness.TERMINAL, timeout=5 * 60)

    assert detail["outcome"] == "refused", detail["outcome"]
    assert "RestartContainer" in (detail["plans"][-1]["refusal_reason"] or "")
    assert not detail["executions"] or all(e["mode"] != "auto" for e in detail["executions"])


def test_fixes_an_oom_in_place_end_to_end(engine: harness.Engine, healthy: None) -> None:
    """A real OOM kill; the engine raises the limit in place; the verifier says remediated."""
    mark = engine.high_water_mark()
    with harness.promoted(RUNBOOK, CHAOS_NS):
        _set_target(FAULT_TARGET_MIB)
        incident = engine.wait_for_incident(
            after=mark,
            target_key=TARGET_KEY,
            alertname="KubePodOOMKilled",
            timeout=harness.ALERT_TIMEOUT_SECONDS,
        )
        detail = engine.wait_for_state(
            incident["id"], harness.TERMINAL, timeout=harness.FIX_TIMEOUT_SECONDS
        )

    replay = engine.evidence(incident["id"])["replay"]
    assert detail["state"] == "remediated", replay
    assert detail["runbook"] == RUNBOOK
    assert detail["verifications"][-1]["outcome"] == "remediated", replay

    # The pod that was resized is still the one running, at the new limit, and it has stopped
    # restarting: watched for a minute after the verdict, the count does not move.
    resized_pod = detail["plans"][-1]["actions"][0]["name"]
    status = _container_status(resized_pod)
    assert status["resources"]["limits"]["memory"] == "384Mi"  # type: ignore[index]
    restarts = status["restartCount"]
    time.sleep(60)
    assert _container_status(resized_pod)["restartCount"] == restarts

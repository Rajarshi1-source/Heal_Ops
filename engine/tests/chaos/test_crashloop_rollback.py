"""Runbook 1, crashloop_rollback, on a real cluster: it fixes, it undoes exactly, it refuses."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.chaos import harness
from tests.chaos.harness import CHAOS_NS, kubectl

RUNBOOK = "crashloop_rollback"
DEPLOYMENT = "crashy-api"
TARGET_KEY = f"{CHAOS_NS}/Deployment/{DEPLOYMENT}"


def _env() -> dict[str, str | None]:
    deployment = harness.kubectl_json("-n", CHAOS_NS, "get", "deployment", DEPLOYMENT)
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container.get("env") or []}


def _set_env(assignment: str) -> None:
    kubectl("-n", CHAOS_NS, "set", "env", f"deployment/{DEPLOYMENT}", assignment)


@pytest.fixture
def healthy(cluster: None) -> Iterator[None]:
    """A good revision to start from, and back to it however the test ends."""
    _set_env("CRASH_AFTER-")
    harness.rollout_settled(f"deployment/{DEPLOYMENT}")
    yield
    _set_env("CRASH_AFTER-")
    harness.rollout_settled(f"deployment/{DEPLOYMENT}")


async def test_compensation_restores_the_declared_state_exactly(healthy: None) -> None:
    """Execute the rollback, compensate it, and compare the declared state with `==`."""
    from k8s import state_capture
    from planner import replan
    from runbooks import load_all

    # A second revision on top of the good one. It would crash after 15 minutes; this test is over
    # long before, so it raises no alert of its own.
    _set_env("CRASH_AFTER=900s")
    harness.rollout_settled(f"deployment/{DEPLOYMENT}")
    before = await state_capture.capture_deployment(CHAOS_NS, DEPLOYMENT)

    target = await harness.live_target("KubePodCrashLooping", namespace=CHAOS_NS, name=DEPLOYMENT)
    plan = replan(harness.bundle_for("KubePodCrashLooping", target, container=DEPLOYMENT))
    assert plan is not None and not plan.is_refusal, plan and plan.refusal_reason

    runbook = load_all().get(RUNBOOK)
    assert runbook is not None
    executed = await runbook.execute(plan)
    assert executed.ok, executed.detail
    # The regression a strategic merge hid: the rollback must REMOVE what the bad revision added.
    assert "CRASH_AFTER" not in _env()
    assert executed.state_after["template_digest"] != before["template_digest"]

    compensated = await runbook.compensate(plan)
    assert compensated.ok, compensated.detail
    after = await state_capture.capture_deployment(CHAOS_NS, DEPLOYMENT)

    assert harness.declared(after) == harness.declared(before)
    assert _env()["CRASH_AFTER"] == "900s"

    # Idempotent: the watchdog may compensate after the verifier already did.
    again = await runbook.compensate(plan)
    assert again.ok
    assert harness.declared(
        await state_capture.capture_deployment(CHAOS_NS, DEPLOYMENT)
    ) == harness.declared(before)


def test_refuses_what_it_must_even_with_full_authority(engine: harness.Engine) -> None:
    """The refusal fixtures crash-loop for real; with the runbook on `auto`, none is touched.

    Each fixture is restarted so a fresh alert reaches the engine end to end, and each must end
    refused or denied with nothing executed: a StatefulSet, a single replica with nothing to roll
    back to, and a workload that opted out with `healops.io/remediation: disabled`.
    """
    fixtures = {
        "chaos-state": "statefulset/chaos-state",
        "lonely-api": "deployment/lonely-api",
        "hands-off-api": "deployment/hands-off-api",
    }
    mark = engine.high_water_mark()
    with harness.promoted(RUNBOOK, CHAOS_NS):
        for kind_name in fixtures.values():
            kubectl("-n", CHAOS_NS, "rollout", "restart", kind_name)

        for name in fixtures:
            incident = engine.wait_for_incident(
                after=mark,
                target_key=f"{CHAOS_NS}/Deployment/{name}",
                alertname="KubePodCrashLooping",
                timeout=harness.ALERT_TIMEOUT_SECONDS,
            )
            detail = engine.wait_for_state(incident["id"], harness.TERMINAL, timeout=5 * 60)

            assert detail["outcome"] in ("refused", "denied"), (name, detail["outcome"])
            assert not [e for e in detail["executions"] if e["mode"] == "auto"], name
            plan = detail["plans"][-1]
            reason = plan["refusal_reason"] or ", ".join(plan["policy_reasons"])
            assert reason, f"{name} was refused without saying why"


def test_fixes_a_bad_rollout_end_to_end(engine: harness.Engine, healthy: None) -> None:
    """A bad config rollout crash-loops; the engine rolls it back and the verifier says so."""
    mark = engine.high_water_mark()
    with harness.promoted(RUNBOOK, CHAOS_NS):
        # Revision N+1 dies 20s after starting. The image is unchanged -- a bad config rollout,
        # the commonest bad deploy there is, and exactly what a rollback should undo.
        _set_env("CRASH_AFTER=20s")

        incident = engine.wait_for_incident(
            after=mark,
            target_key=TARGET_KEY,
            alertname="KubePodCrashLooping",
            timeout=harness.ALERT_TIMEOUT_SECONDS,
        )
        detail = engine.wait_for_state(
            incident["id"], harness.TERMINAL, timeout=harness.FIX_TIMEOUT_SECONDS
        )

    replay = engine.evidence(incident["id"])["replay"]
    assert detail["state"] == "remediated", replay
    assert detail["runbook"] == RUNBOOK
    assert detail["verifications"][-1]["outcome"] == "remediated", replay
    assert "CRASH_AFTER" not in _env()
    assert "MATCH" in replay  # the recorded input reproduces the recorded plan

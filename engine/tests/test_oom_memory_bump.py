"""Runbook 4: in-place memory resize.

Planning is a pure function of the bundle, so every refusal is tested directly. Execution runs
against a fake kubelet that reports `status.containerStatuses[].resources` the way a real one does
-- including not applying a resize, and saying `Infeasible` -- because success must be read from
there and never from the PATCH returning.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes.client.rest import ApiException

from bundle import BundleAlert, ContainerState, IncidentBundle, Target
from k8s import pod_ops
from plan import Durability, Reversibility
from runbooks import oom_memory_bump
from runbooks.oom_memory_bump import OomMemoryBump

RUNBOOK = OomMemoryBump()


def container(**overrides: Any) -> ContainerState:
    base: dict[str, Any] = {
        "name": "worker",
        "image": "ghcr.io/o/hungry-worker:v1",
        "memory_request_mib": 256,
        "memory_limit_mib": 512,
        "memory_peak_mib": 500.0,
        "memory_resize_policy": "NotRequired",
    }
    return ContainerState(**(base | overrides))


def target(**overrides: Any) -> Target:
    base: dict[str, Any] = {
        "kind": "Deployment",
        "name": "hungry-worker",
        "namespace": "chaos",
        "replicas": 2,
        "containers": (container(),),
    }
    return Target(**(base | overrides))


def bundle(t: Target, *, pod: str | None = "hungry-worker-7d-abc", c: str | None = "worker") -> Any:
    return IncidentBundle(
        incident_id="9",
        cause_alert=BundleAlert(
            alertname="KubePodOOMKilled", fingerprint="f", namespace="chaos", pod=pod, container=c
        ),
        target=t,
    )


def plan_for(t: Target | None = None, **kwargs: Any) -> Any:
    t = t or target()
    return RUNBOOK.plan(bundle(t, **kwargs), t)


# --- plan ----------------------------------------------------------------------------------------


def test_plans_an_in_place_resize_of_the_named_pod() -> None:
    plan = plan_for()

    assert not plan.is_refusal
    [action] = plan.actions
    assert (action.verb, action.subresource, action.name) == (
        "resize",
        "resize",
        "hungry-worker-7d-abc",
    )
    assert plan.target_kind == "Pod"
    assert plan.reversibility is Reversibility.REVERSIBLE
    assert plan.durability is Durability.EPHEMERAL and plan.ttl_seconds
    assert "no restart" in plan.summary


def test_the_bump_clears_the_peak_and_respects_the_factor() -> None:
    # 512 * 1.5 = 768; peak 700 * 1.25 = 875 -> the larger wins.
    plan = plan_for(target(containers=(container(memory_peak_mib=700.0),)))
    limits = plan.actions[0].body["spec"]["containers"][0]["resources"]["limits"]
    assert limits["memory"] == "875Mi"


def test_a_burstable_pod_keeps_its_request() -> None:
    """A resize may not change QoS class. Raising the request of a Burstable pod to the limit would
    make it Guaranteed, and the API server would reject the resize."""
    resources = plan_for().actions[0].body["spec"]["containers"][0]["resources"]
    assert "requests" not in resources


def test_a_guaranteed_pod_has_request_and_limit_raised_together() -> None:
    plan = plan_for(target(containers=(container(memory_request_mib=512),)))
    resources = plan.actions[0].body["spec"]["containers"][0]["resources"]
    assert resources["requests"]["memory"] == resources["limits"]["memory"] == "768Mi"
    undo = plan.compensation[0].body["spec"]["containers"][0]["resources"]
    assert undo == {"limits": {"memory": "512Mi"}, "requests": {"memory": "512Mi"}}


def test_the_compensation_is_the_exact_prior_limit() -> None:
    undo = plan_for().compensation[0]
    assert undo.subresource == "resize"
    assert undo.body["spec"]["containers"][0]["resources"]["limits"]["memory"] == "512Mi"


def test_without_an_observed_peak_confidence_drops_below_auto() -> None:
    """The bump is then a guess, and the policy gate's confidence rule asks a human."""
    plan = plan_for(target(containers=(container(memory_peak_mib=None),)))
    assert plan.confidence < 0.80


def test_cost_is_carried_on_the_plan() -> None:
    assert plan_for().cost_delta_inr_month > 0


@pytest.mark.parametrize(
    ("t", "kwargs", "fragment"),
    [
        (
            target(containers=(container(memory_resize_policy="RestartContainer"),)),
            {},
            "RestartContainer",
        ),
        (target(containers=(container(memory_limit_mib=4096),)), {}, "ceiling"),
        (target(containers=(container(memory_limit_mib=None),)), {}, "no memory limit"),
        (target(), {"pod": None}, "names no pod"),
        (target(), {"c": "sidecar"}, "which container"),
        (target(kind="StatefulSet"), {}, "not handled"),
    ],
)
def test_refusals(t: Target, kwargs: dict[str, Any], fragment: str) -> None:
    plan = plan_for(t, **kwargs)
    assert plan.is_refusal
    assert fragment in (plan.refusal_reason or "")


def test_an_unnamed_container_is_inferred_only_when_unambiguous() -> None:
    assert not plan_for(c=None).is_refusal
    two = target(containers=(container(), container(name="sidecar")))
    assert plan_for(two, c=None).is_refusal


def test_below_1_35_falls_back_to_a_template_patch_that_policy_will_gate() -> None:
    plan = plan_for(target(server_supports_in_place_resize=False))

    assert plan.actions[0].verb == "patch"
    assert plan.actions[0].resource == "apps/v1/Deployment"
    # The restarts cannot be undone: partially reversible, which is what sends it to a human.
    assert plan.reversibility is Reversibility.PARTIAL
    assert "pods restart" in plan.summary


def test_the_fallback_ignores_resize_policy() -> None:
    """RestartContainer only matters for an in-place resize; a template change restarts anyway."""
    t = target(
        server_supports_in_place_resize=False,
        containers=(container(memory_resize_policy="RestartContainer"),),
    )
    assert not plan_for(t).is_refusal


# --- execute against a fake kubelet --------------------------------------------------------------


class Kubelet:
    """Reports applied resources from status, and may refuse to apply them."""

    def __init__(
        self, limit: str = "512Mi", *, applies: bool = True, condition: Any = None
    ) -> None:
        self.limit = limit
        self.applies = applies
        self.condition = condition
        self.restarts = 0
        self.resizes: list[str] = []
        self.gone = False

    def pod(self) -> Any:
        status = SimpleNamespace(
            name="worker",
            restart_count=self.restarts,
            resources=SimpleNamespace(limits={"memory": self.limit}),
        )
        return SimpleNamespace(
            metadata=SimpleNamespace(name="hungry-worker-7d-abc"),
            status=SimpleNamespace(
                container_statuses=[status],
                conditions=[self.condition] if self.condition else [],
            ),
        )

    async def read_pod(self, namespace: str, name: str) -> Any:
        if self.gone:
            raise ApiException(status=404, reason="Not Found")
        return self.pod()

    async def resize(self, namespace: str, name: str, body: dict[str, Any]) -> None:
        wanted = body["spec"]["containers"][0]["resources"]["limits"]["memory"]
        self.resizes.append(wanted)
        if self.applies:
            self.limit = wanted


@pytest.fixture
def kubelet(monkeypatch: pytest.MonkeyPatch) -> Kubelet:
    fake = Kubelet()
    monkeypatch.setattr(pod_ops, "read_pod", fake.read_pod)
    monkeypatch.setattr(pod_ops, "resize", fake.resize)
    monkeypatch.setattr(oom_memory_bump, "RESIZE_POLL_SECONDS", 0.0)
    return fake


async def test_success_is_read_from_container_status(kubelet: Kubelet) -> None:
    result = await RUNBOOK.execute(plan_for())

    assert result.ok
    assert kubelet.resizes == ["768Mi"]
    assert result.state_before["memory_limit_mib"] == 512
    assert result.state_after["memory_limit_mib"] == 768
    assert "restartCount unchanged at 0" in result.detail


async def test_infeasible_ends_the_attempt_at_once_and_withdraws_it(kubelet: Kubelet) -> None:
    kubelet.applies = False
    kubelet.condition = SimpleNamespace(
        type="PodResizePending", reason="Infeasible", message="Node didn't have enough capacity"
    )

    result = await RUNBOOK.execute(plan_for())

    assert not result.ok
    assert "Infeasible" in result.detail and "approval" in result.detail
    assert kubelet.resizes == ["768Mi", "512Mi"], "the pending desired change is withdrawn"


async def test_a_resize_that_never_lands_times_out_instead_of_hanging(
    kubelet: Kubelet, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config import get_settings

    monkeypatch.setattr(get_settings(), "resize_wait_seconds", 0)
    kubelet.applies = False
    kubelet.condition = SimpleNamespace(type="PodResizePending", reason="Deferred", message="")

    result = await RUNBOOK.execute(plan_for())

    assert not result.ok
    assert "Deferred" in result.detail


async def test_execute_is_a_no_op_when_already_resized(kubelet: Kubelet) -> None:
    kubelet.limit = "768Mi"
    result = await RUNBOOK.execute(plan_for())
    assert result.ok and kubelet.resizes == []


async def test_a_moved_restart_count_is_reported_not_hidden(
    kubelet: Kubelet, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def restarting(namespace: str, name: str, body: dict[str, Any]) -> None:
        await kubelet.resize(namespace, name, body)
        kubelet.restarts = 1

    monkeypatch.setattr(pod_ops, "resize", restarting)
    result = await RUNBOOK.execute(plan_for())
    assert "restartCount moved 0 -> 1" in result.detail


async def test_compensation_resizes_back_exactly(kubelet: Kubelet) -> None:
    plan = plan_for()
    await RUNBOOK.execute(plan)

    result = await RUNBOOK.compensate(plan)

    assert result.ok
    assert kubelet.limit == "512Mi"


async def test_compensation_is_idempotent(kubelet: Kubelet) -> None:
    result = await RUNBOOK.compensate(plan_for())
    assert result.ok and kubelet.resizes == []


async def test_compensation_succeeds_when_the_pod_is_gone(kubelet: Kubelet) -> None:
    """Its replacement was built from the template, which this resize never changed."""
    kubelet.gone = True
    result = await RUNBOOK.compensate(plan_for())
    assert result.ok and "no longer exists" in result.detail


async def test_extra_verification_fails_if_the_pod_was_replaced(kubelet: Kubelet) -> None:
    from runbooks.base import SignalWindow

    plan = plan_for()
    await RUNBOOK.execute(plan)
    assert await RUNBOOK.extra_verification(plan, SignalWindow()) is None

    kubelet.limit = "512Mi"
    assert "no longer in effect" in (await RUNBOOK.extra_verification(plan, SignalWindow()) or "")

    kubelet.gone = True
    assert "was replaced" in (await RUNBOOK.extra_verification(plan, SignalWindow()) or "")


async def test_the_template_fallback_patches_the_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from k8s import deployment_ops, state_capture

    patched: list[dict[str, Any]] = []

    async def patch(namespace: str, name: str, body: dict[str, Any]) -> None:
        patched.append(body)

    async def capture(namespace: str, name: str) -> dict[str, Any]:
        return {"name": name}

    monkeypatch.setattr(deployment_ops, "patch", patch)
    monkeypatch.setattr(state_capture, "capture_deployment", capture)
    plan = plan_for(target(server_supports_in_place_resize=False))

    assert (await RUNBOOK.execute(plan)).ok
    assert (await RUNBOOK.compensate(plan)).ok
    assert len(patched) == 2
    assert await RUNBOOK.extra_verification(plan, None) is None  # type: ignore[arg-type]


def test_pod_ops_readers() -> None:
    pod = Kubelet(
        condition=SimpleNamespace(type="PodResizePending", reason="Infeasible", message="m")
    ).pod()
    assert pod_ops.actual_memory_limit(pod, "worker") == "512Mi"
    assert pod_ops.actual_memory_limit(pod, "missing") is None
    assert pod_ops.restart_count(pod, "worker") == 0
    condition = pod_ops.resize_condition(pod)
    assert condition is not None and condition.infeasible

"""GitOps-aware remediation: classify, prepare, and the narrowest pause, undone exactly."""

from __future__ import annotations

import copy
from types import SimpleNamespace as NS
from typing import Any

import pytest
from kubernetes.client.rest import ApiException
from sqlalchemy.ext.asyncio import AsyncSession

import gitops
from k8s import client as k8s
from k8s import deployment_ops
from plan import ApiCall, Durability
from tests.factories import ledger_payloads, make_execution, make_plan, make_target

ARGO = {"argocd.argoproj.io/instance": "shop-app"}
FLUX = {"kustomize.toolkit.fluxcd.io/name": "apps", "kustomize.toolkit.fluxcd.io/namespace": "flux"}


# --- classify and prepare -------------------------------------------------------------------------


def test_classification() -> None:
    assert gitops.classify(ARGO, {}) == "argocd"
    assert (
        gitops.classify({}, {"argocd.argoproj.io/tracking-id": "app:apps/Deployment:shop/api"})
        == "argocd"
    )
    assert gitops.classify(FLUX, {}) == "flux"
    assert gitops.classify({"app": "api"}, {}) == "unmanaged"


def test_an_unmanaged_target_still_gets_a_ttl() -> None:
    """An imperative change nobody tracks is how clusters drift, GitOps or not."""
    plan = gitops.prepare(make_plan(ttl_seconds=None, durability=Durability.DURABLE), make_target())
    assert plan.gitops_managed is False

    ephemeral = gitops.prepare(make_plan(), make_target())
    assert ephemeral.ttl_seconds and ephemeral.durability is Durability.EPHEMERAL


def test_a_managed_target_is_marked_for_the_two_stage_fix() -> None:
    plan = gitops.prepare(make_plan(), make_target(labels=ARGO))
    assert plan.gitops_managed is True
    assert plan.durability is Durability.EPHEMERAL and plan.ttl_seconds


def test_refusals_pass_through_untouched() -> None:
    from plan import refuse

    refusal = refuse("r", 1, "no")
    assert gitops.prepare(refusal, make_target(labels=ARGO)) is refusal


def test_ignore_rules_name_exactly_the_fields_changed() -> None:
    rollback = gitops.ignore_rules(make_plan())
    assert [(r.kind, r.jsonPointers) for r in rollback] == [("Deployment", ("/spec/template",))]

    scale = ApiCall(verb="scale", resource="apps/v1/Deployment", name="api", namespace="shop")
    assert gitops.ignore_rules(make_plan(actions=(scale,)))[0].jsonPointers == ("/spec/replicas",)

    hpa = ApiCall(verb="hpa_patch", resource="autoscaling/v2/HorizontalPodAutoscaler", name="api")
    assert gitops.ignore_rules(make_plan(actions=(hpa,)))[0].kind == "HorizontalPodAutoscaler"


def test_an_in_place_resize_needs_no_pause() -> None:
    """Pods are not in git; the controller owns the Deployment, not its pods."""
    resize = ApiCall(
        verb="resize", resource="v1/Pod", name="api-1", namespace="shop", subresource="resize"
    )
    assert gitops.ignore_rules(make_plan(actions=(resize,))) == []


# --- pause and resume against a fake API server ---------------------------------------------------


class ApiServer:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {
            "applications/shop-app": {
                "metadata": {"resourceVersion": "7"},
                "spec": {
                    "ignoreDifferences": [
                        {"group": "", "kind": "Secret", "jsonPointers": ["/data"]}
                    ],
                    "syncPolicy": {"syncOptions": ["CreateNamespace=true"]},
                },
            },
            "kustomizations/apps": {
                "metadata": {"resourceVersion": "3"},
                "spec": {"suspend": False},
            },
        }
        self.labels: dict[str, str] = dict(ARGO)
        self.patches: list[tuple[str, dict[str, Any]]] = []

    def get(
        self, group: str, version: str, namespace: str, plural: str, name: str, **_: Any
    ) -> Any:
        key = f"{plural}/{name}"
        if key not in self.objects:
            raise ApiException(status=404)
        return copy.deepcopy(self.objects[key])

    def patch(
        self, group: str, version: str, namespace: str, plural: str, name: str, body: Any, **_: Any
    ) -> None:
        key = f"{plural}/{name}"
        self.patches.append((key, body))
        spec = self.objects[key]["spec"]
        for field, value in body.get("spec", {}).items():
            if isinstance(value, dict):
                spec.setdefault(field, {}).update(value)
            else:
                spec[field] = value


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> ApiServer:
    server = ApiServer()

    async def call(verb: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    async def read_deployment(namespace: str, name: str) -> Any:
        return NS(metadata=NS(labels=server.labels, annotations={}))

    monkeypatch.setattr(k8s, "call", call)
    monkeypatch.setattr(
        k8s,
        "custom_objects",
        lambda: NS(
            get_namespaced_custom_object=server.get, patch_namespaced_custom_object=server.patch
        ),
    )
    monkeypatch.setattr(deployment_ops, "read_deployment", read_deployment)
    return server


@pytest.mark.db
async def test_argo_gets_a_field_scoped_ignore_not_a_disabled_self_heal(
    db_session: AsyncSession, api: ApiServer
) -> None:
    incident, execution, plan = await make_execution(
        db_session, plan=make_plan(gitops_managed=True)
    )

    record = await gitops.pause(db_session, execution, plan)

    assert record is not None and record["manager"] == "argocd"
    spec = api.objects["applications/shop-app"]["spec"]
    added = spec["ignoreDifferences"][-1]
    assert added == {
        "group": "apps",
        "kind": "Deployment",
        "name": "api",
        "namespace": "shop",
        "jsonPointers": ["/spec/template"],
    }
    assert "RespectIgnoreDifferences=true" in spec["syncPolicy"]["syncOptions"]
    assert "automated" not in spec["syncPolicy"], "self-heal is never switched off"
    assert api.patches[0][1]["metadata"]["resourceVersion"] == "7"
    assert execution.gitops_paused is True
    [entry] = await ledger_payloads(db_session, incident.id, "gitops_pause")
    assert entry["application"] == "shop-app" and entry["ttl_seconds"] == plan.ttl_seconds


@pytest.mark.db
async def test_resume_undoes_exactly_what_pause_did(
    db_session: AsyncSession, api: ApiServer
) -> None:
    """Read back from the ledger: the resume needs no memory of the process that paused."""
    incident, execution, plan = await make_execution(
        db_session, plan=make_plan(gitops_managed=True)
    )
    original = copy.deepcopy(api.objects["applications/shop-app"]["spec"])
    await gitops.pause(db_session, execution, plan)

    assert await gitops.resume(db_session, execution) is True

    assert api.objects["applications/shop-app"]["spec"] == original
    assert execution.gitops_paused is False
    assert await ledger_payloads(db_session, incident.id, "gitops_resume")


@pytest.mark.db
async def test_another_pauses_sync_option_is_left_in_place(
    db_session: AsyncSession, api: ApiServer
) -> None:
    _, first, plan = await make_execution(db_session, plan=make_plan(gitops_managed=True))
    _, second, other = await make_execution(
        db_session,
        plan=make_plan(
            gitops_managed=True,
            actions=(
                ApiCall(verb="scale", resource="apps/v1/Deployment", name="api", namespace="shop"),
            ),
        ),
    )
    await gitops.pause(db_session, first, plan)
    await gitops.pause(db_session, second, other)

    await gitops.resume(db_session, first)

    spec = api.objects["applications/shop-app"]["spec"]
    assert "RespectIgnoreDifferences=true" in spec["syncPolicy"]["syncOptions"]
    assert any(e.get("jsonPointers") == ["/spec/replicas"] for e in spec["ignoreDifferences"])


@pytest.mark.db
async def test_flux_is_suspended_and_only_unsuspended_if_we_suspended_it(
    db_session: AsyncSession, api: ApiServer
) -> None:
    api.labels = dict(FLUX)
    _, execution, plan = await make_execution(db_session, plan=make_plan(gitops_managed=True))

    record = await gitops.pause(db_session, execution, plan)
    assert record is not None and record["was_suspended"] is False
    assert api.objects["kustomizations/apps"]["spec"]["suspend"] is True

    await gitops.resume(db_session, execution)
    assert api.objects["kustomizations/apps"]["spec"]["suspend"] is False

    # Someone else had it suspended already: that is theirs to lift.
    api.objects["kustomizations/apps"]["spec"]["suspend"] = True
    _, again, plan2 = await make_execution(db_session, plan=make_plan(gitops_managed=True))
    await gitops.pause(db_session, again, plan2)
    await gitops.resume(db_session, again)
    assert api.objects["kustomizations/apps"]["spec"]["suspend"] is True


@pytest.mark.db
async def test_a_pause_that_cannot_be_set_raises(db_session: AsyncSession, api: ApiServer) -> None:
    del api.objects["applications/shop-app"]
    _, execution, plan = await make_execution(db_session, plan=make_plan(gitops_managed=True))
    with pytest.raises(gitops.GitOpsPauseFailed):
        await gitops.pause(db_session, execution, plan)
    assert execution.gitops_paused is False


@pytest.mark.db
async def test_resume_tolerates_a_deleted_application(
    db_session: AsyncSession, api: ApiServer
) -> None:
    _, execution, plan = await make_execution(db_session, plan=make_plan(gitops_managed=True))
    await gitops.pause(db_session, execution, plan)
    del api.objects["applications/shop-app"]
    assert await gitops.resume(db_session, execution) is True


@pytest.mark.db
async def test_nothing_to_pause_when_the_target_is_no_longer_managed(
    db_session: AsyncSession, api: ApiServer
) -> None:
    api.labels = {}
    _, execution, plan = await make_execution(db_session, plan=make_plan(gitops_managed=True))
    assert await gitops.pause(db_session, execution, plan) is None
    assert await gitops.resume(db_session, execution) is False


def test_app_name_from_a_tracking_id() -> None:
    assert gitops._app_from_tracking_id("shop-app:apps/Deployment:shop/api") == "shop-app"

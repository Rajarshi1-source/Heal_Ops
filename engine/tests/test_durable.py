"""Durable fixes as pull requests: opened only after verification, carrying the evidence."""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

import durable
from config import get_settings
from db.models import IncidentState, Verification
from plan import ApiCall
from tests.factories import bound_to, ledger_payloads, make_execution, make_plan

MANIFEST = """\
# The checkout API. Owned by the shop team.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: shop
spec:
  replicas: 3   # sized for weekday peak
  template:
    spec:
      containers:
        - name: api
          image: "ghcr.io/o/api:v2"
          resources:
            limits:
              memory: 512Mi
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: api
spec:
  maxReplicas: 6
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 80
"""


# --- what changes ---------------------------------------------------------------------------------


def test_a_rollback_pins_the_image_that_verification_proved() -> None:
    change = durable.change_for(
        make_plan(actions=(ROLLBACK,)),
        "api",
        {"containers": [{"name": "api", "image": "ghcr.io/o/api:v1"}]},
    )
    assert change is not None and change.images == {"api": "ghcr.io/o/api:v1"}


def test_scale_hpa_and_memory_changes() -> None:
    scale = ApiCall(verb="scale", resource="apps/v1/Deployment", name="api", body={"replicas": 5})
    assert durable.change_for(make_plan(actions=(scale,)), "api", {}).replicas == 5  # type: ignore[union-attr]

    hpa = ApiCall(
        verb="hpa_patch",
        resource="autoscaling/v2/HorizontalPodAutoscaler",
        name="api",
        body={"max_replicas": 8, "target_utilization": 65},
    )
    change = durable.change_for(make_plan(actions=(hpa,)), "api", {})
    assert change is not None and (change.hpa_max_replicas, change.hpa_cpu_target) == (8, 65)

    resize = ApiCall(
        verb="resize",
        resource="v1/Pod",
        name="api-7d-x",
        subresource="resize",
        body={
            "spec": {"containers": [{"name": "api", "resources": {"limits": {"memory": "768Mi"}}}]}
        },
    )
    change = durable.change_for(make_plan(actions=(resize,)), "api", {})
    assert change is not None and change.memory == {"api": ("768Mi", None)}


def test_runbooks_that_change_nothing_git_declares_have_no_pr() -> None:
    cordon = ApiCall(verb="cordon", resource="v1/Node", name="worker-3")
    assert durable.change_for(make_plan(actions=(cordon,)), "worker-3", {}) is None


# --- editing the manifest -------------------------------------------------------------------------


def test_the_edit_preserves_comments_quoting_and_the_other_documents() -> None:
    """A diff that rewrites the whole file is unreviewable; this one changes one line."""
    change = durable.Change("Deployment", "api", images={"api": "ghcr.io/o/api:v1"})

    edited = durable.edit_manifest(MANIFEST, change)

    assert "# The checkout API. Owned by the shop team." in edited
    assert "replicas: 3   # sized for weekday peak" in edited
    assert 'image: "ghcr.io/o/api:v1"' in edited
    changed = [a for a, b in zip(MANIFEST.splitlines(), edited.splitlines(), strict=True) if a != b]
    assert changed == ['          image: "ghcr.io/o/api:v2"']


def test_hpa_and_memory_edits() -> None:
    edited = durable.edit_manifest(
        MANIFEST,
        durable.Change("HorizontalPodAutoscaler", "api", hpa_max_replicas=8, hpa_cpu_target=65),
    )
    assert "maxReplicas: 8" in edited and "averageUtilization: 65" in edited

    edited = durable.edit_manifest(
        MANIFEST,
        durable.Change("Deployment", "api", memory={"api": ("768Mi", "768Mi")}, replicas=4),
    )
    assert "memory: 768Mi" in edited and "replicas: 4" in edited
    assert edited.count("768Mi") == 2, "the request is added alongside the limit"


@pytest.mark.parametrize(
    "change",
    [
        durable.Change("Deployment", "checkout", replicas=2),
        durable.Change("Deployment", "api", images={"sidecar": "x"}),
        durable.Change("HorizontalPodAutoscaler", "nope", hpa_max_replicas=2),
    ],
)
def test_a_manifest_without_the_object_is_an_error_not_a_guess(change: durable.Change) -> None:
    with pytest.raises(durable.ManifestEditError):
        durable.edit_manifest(MANIFEST, change)


def test_change_descriptions_are_readable() -> None:
    lines = durable.Change(
        "Deployment", "api", images={"api": "v1"}, replicas=3, memory={"api": ("1Gi", "1Gi")}
    ).describe()
    assert lines[0] == "Deployment `api`:"
    assert any("image -> `v1`" in line for line in lines)
    assert any("request -> 1Gi" in line for line in lines)


# --- opening it -----------------------------------------------------------------------------------


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from integrations import github_client

    calls: dict[str, Any] = {}
    monkeypatch.setattr(get_settings(), "github_token", SecretStr("github_pat_test"))
    monkeypatch.setattr(get_settings(), "github_repo", "o/manifests")

    async def get_file(path: str, ref: str) -> tuple[str, str]:
        calls["read"] = (path, ref)
        return MANIFEST, "blobsha"

    async def create_branch(branch: str, base: str) -> None:
        calls["branch"] = (branch, base)

    async def put_file(path: str, **kwargs: Any) -> None:
        calls["put"] = (path, kwargs)

    async def create_pull(**kwargs: Any) -> str:
        calls["pull"] = kwargs
        return "https://github.com/o/manifests/pull/1"

    async def read_deployment(namespace: str, name: str) -> Any:
        return NS(metadata=NS(annotations={"healops.io/manifest-path": "apps/shop/api.yaml"}))

    from k8s import deployment_ops

    for name, fn in [
        ("get_file", get_file),
        ("create_branch", create_branch),
        ("put_file", put_file),
        ("create_pull", create_pull),
    ]:
        monkeypatch.setattr(github_client, name, fn)
    monkeypatch.setattr(deployment_ops, "read_deployment", read_deployment)
    return calls


ROLLBACK = ApiCall(
    verb="rollout_undo",
    resource="apps/v1/Deployment",
    name="api",
    namespace="shop",
    body={"to_revision": 3, "from_revision": 4, "to_image": "ghcr.io/o/api:v1"},
)


async def verified(session: AsyncSession, **kwargs: Any) -> Any:
    incident, execution, plan = await make_execution(
        session,
        plan=make_plan(actions=(ROLLBACK,)),
        incident_state=IncidentState.REMEDIATED,
        status="remediated",
        **kwargs,
    )
    execution.state_after = {"containers": [{"name": "api", "image": "ghcr.io/o/api:v1"}]}
    session.add(
        Verification(
            execution_id=execution.id,
            window_seconds=300,
            signals_before={"restarts": 7.0},
            signals_after={"restarts": 0.0},
            outcome="remediated",
            reasons=["alert cleared"],
        )
    )
    await session.flush()
    return incident, execution, plan


@pytest.mark.db
async def test_a_verified_fix_opens_a_pr_with_the_evidence(
    db_session: AsyncSession, github: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(durable, "transaction", bound_to(db_session))
    incident, execution, _ = await verified(db_session)

    result = await durable.open_for(execution.id)

    assert result.url == "https://github.com/o/manifests/pull/1"
    assert github["read"] == ("apps/shop/api.yaml", "main")
    assert github["branch"][0] == f"healops/scripted_runbook/api-{incident.id}"
    pull = github["pull"]
    assert "already validated live" in pull["body"]
    assert "| `restarts` | 7 | 0 |" in pull["body"]
    assert "git revert" in pull["body"]
    assert "runbook:scripted_runbook" in pull["labels"]
    assert 'image: "ghcr.io/o/api:v1"' in github["put"][1]["text"]
    assert execution.pr_url == result.url
    assert await ledger_payloads(db_session, incident.id, "durable_pr")


@pytest.mark.db
async def test_no_pr_for_a_fix_that_was_not_verified(
    db_session: AsyncSession, github: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR proposing a change that did not work is worse than no PR."""
    monkeypatch.setattr(durable, "transaction", bound_to(db_session))
    _, execution, _ = await make_execution(db_session, status="reverted")

    result = await durable.open_for(execution.id)

    assert result.url is None and "not remediated" in result.reason
    assert "pull" not in github


@pytest.mark.db
async def test_an_already_opened_pr_is_not_opened_twice(
    db_session: AsyncSession, github: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(durable, "transaction", bound_to(db_session))
    _, execution, _ = await verified(db_session)
    execution.pr_url = "https://github.com/o/manifests/pull/9"
    await db_session.flush()

    result = await durable.open_for(execution.id)
    assert result.url == execution.pr_url and "pull" not in github


@pytest.mark.db
async def test_without_the_manifest_annotation_it_says_so_instead_of_guessing(
    db_session: AsyncSession, github: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from k8s import deployment_ops

    async def bare(namespace: str, name: str) -> Any:
        return NS(metadata=NS(annotations={}))

    monkeypatch.setattr(deployment_ops, "read_deployment", bare)
    monkeypatch.setattr(durable, "transaction", bound_to(db_session))
    _, execution, _ = await verified(db_session)

    result = await durable.open_for(execution.id)
    assert result.url is None and "healops.io/manifest-path" in result.reason


@pytest.mark.db
async def test_a_github_failure_loses_the_pr_not_the_fix(
    db_session: AsyncSession, github: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from integrations import github_client

    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise github_client.GitHubError("POST /git/refs -> 422")

    monkeypatch.setattr(github_client, "create_branch", broken)
    monkeypatch.setattr(durable, "transaction", bound_to(db_session))
    _, execution, _ = await verified(db_session)

    result = await durable.open_for(execution.id)
    assert result.url is None and "422" in result.reason
    assert execution.status == "remediated"


@pytest.mark.db
async def test_unconfigured_github_skips_cleanly(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(durable, "transaction", bound_to(db_session))
    _, execution, _ = await verified(db_session)
    assert (await durable.open_for(execution.id)).reason == "github is not configured"

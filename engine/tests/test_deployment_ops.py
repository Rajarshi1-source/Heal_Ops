"""The rollback patch, which the runbook tests fake away and only a real API server would judge.

Regression for a defect the chaos suite found: the rollback was a strategic merge, which only adds
and updates list entries -- so rolling back from a revision that ADDED an env var kept the env var,
and the pods went on crashing behind a rollback that reported success.
"""

from __future__ import annotations

from typing import Any

import pytest
from kubernetes.client.rest import ApiException

from k8s import deployment_ops


def _template() -> dict[str, Any]:
    return {
        "metadata": {"labels": {"app": "crashy-api", "pod-template-hash": "7c65fbbbb5"}},
        "spec": {"containers": [{"name": "crashy-api", "image": "chaos:0.1.0", "env": []}]},
    }


def test_the_template_is_replaced_not_merged() -> None:
    patch = deployment_ops.rollback_patch(_template(), "4242")

    assert patch[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "4242"}
    assert patch[1]["op"] == "replace"
    assert patch[1]["path"] == "/spec/template"
    # An empty env list replaces whatever the bad revision added; a merge would have kept it.
    assert patch[1]["value"]["spec"]["containers"][0]["env"] == []


def test_the_replicaset_hash_label_is_not_restored() -> None:
    template = _template()
    patch = deployment_ops.rollback_patch(template, "1")

    assert patch[1]["value"]["metadata"]["labels"] == {"app": "crashy-api"}
    # The revision's own template is left untouched.
    assert template["metadata"]["labels"]["pod-template-hash"] == "7c65fbbbb5"


def test_the_patch_is_sent_as_a_json_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client picks the content type from the body: a list is a JSON patch, a dict a merge."""
    sent: list[Any] = []

    async def fake_call(verb: str, fn: Any, *args: Any, **kwargs: Any) -> None:
        sent.append(args[2])

    monkeypatch.setattr(deployment_ops, "call", fake_call)
    monkeypatch.setattr(
        deployment_ops, "apps_v1", lambda: type("A", (), {"patch_namespaced_deployment": None})()
    )

    import asyncio

    asyncio.run(deployment_ops.rollback_to("chaos", "crashy-api", _template(), "7"))

    assert isinstance(sent[0], list)


@pytest.mark.parametrize(
    ("status", "body"),
    [(409, "Conflict"), (422, 'the server rejected our request: testing value "7" failed')],
)
def test_a_moved_object_is_a_conflict_whichever_way_the_server_says_it(
    monkeypatch: pytest.MonkeyPatch, status: int, body: str
) -> None:
    async def fake_call(verb: str, fn: Any, *args: Any, **kwargs: Any) -> None:
        exc = ApiException(status=status)
        exc.body = body
        raise exc

    monkeypatch.setattr(deployment_ops, "call", fake_call)
    monkeypatch.setattr(
        deployment_ops, "apps_v1", lambda: type("A", (), {"patch_namespaced_deployment": None})()
    )

    import asyncio

    with pytest.raises(deployment_ops.ConflictError):
        asyncio.run(deployment_ops.rollback_to("chaos", "crashy-api", _template(), "7"))


def test_a_scale_is_preconditioned_on_the_replica_count_not_the_version() -> None:
    """Regression, found on the kind cluster: a resourceVersion precondition failed every
    compensation straight after a scale-out, because pod readiness rewrites status -- and the
    version -- every few seconds. The field the runbook owns is what must not have moved."""
    assert deployment_ops.scale_patch(2, expect_replicas=4) == [
        {"op": "test", "path": "/spec/replicas", "value": 4},
        {"op": "replace", "path": "/spec/replicas", "value": 2},
    ]
    assert deployment_ops.scale_patch(3, expect_replicas=None) == [
        {"op": "replace", "path": "/spec/replicas", "value": 3}
    ]


def test_a_restart_does_not_change_the_template_digest() -> None:
    """Regression, found on the kind cluster: a revision differing only by kubectl's restartedAt
    stamp digested differently from the original -- `annotations: {}` versus no key -- so rolling
    back a restart was planned (and verified, and reverted) as if it could fix something."""
    from k8s.state_capture import template_digest

    original = {
        "metadata": {"labels": {"app": "lonely-api"}},
        "spec": {"containers": [{"name": "c"}]},
    }
    restarted = {
        "metadata": {
            "labels": {"app": "lonely-api", "pod-template-hash": "5dc797bbb6"},
            "annotations": {"kubectl.kubernetes.io/restartedAt": "2026-10-04T18:00:00Z"},
        },
        "spec": {"containers": [{"name": "c"}]},
    }
    assert template_digest(restarted) == template_digest(original)

    changed = {**original, "spec": {"containers": [{"name": "c", "env": [{"name": "X"}]}]}}
    assert template_digest(changed) != template_digest(original)

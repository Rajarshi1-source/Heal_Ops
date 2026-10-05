"""The node-agent Job: what it may touch, and how its answer is read back.

Its manifest is the whole security story of the disk runbook -- one node, the log roots and nothing
else, every capability dropped but three -- so it is asserted here rather than trusted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import node_agent
from k8s import agent_jobs


def test_a_raw_log_is_decoded_so_the_result_marker_starts_its_line() -> None:
    """Regression, found on the kind cluster: the typed client returned `str(bytes)`, so the line
    began with `b'` and every in-cluster scan, archive and restore read as 'no result'."""
    raw = SimpleNamespace(
        data=b'diagnostics\nHEALOPS_AGENT_RESULT {"ok": true, "candidates": []}\n'
    )
    assert node_agent.decode_result(agent_jobs.log_text(raw)) == {"ok": True, "candidates": []}
    # The broken form, for the record: this is what the client used to hand back.
    assert node_agent.decode_result(str(raw.data)) is None


def test_the_job_touches_one_node_and_only_the_log_roots() -> None:
    async def build() -> object:
        return agent_jobs.job_manifest(
            "healops-agent-scan-w2-1", "worker-2", {"op": "scan", "roots": ["/var/log"]}
        )

    job = asyncio.run(build())
    pod = job.spec.template.spec  # type: ignore[attr-defined]
    [container] = pod.containers
    caps = container.security_context.capabilities

    assert pod.node_name == "worker-2"  # pinned, never scheduled
    assert [v.host_path.path for v in pod.volumes] == ["/var/log"]
    assert caps.drop == ["ALL"]
    assert sorted(caps.add) == ["CHOWN", "DAC_OVERRIDE", "FOWNER"]
    assert container.security_context.allow_privilege_escalation is False
    assert container.security_context.read_only_root_filesystem is True
    assert pod.automount_service_account_token is False
    sources = [s.config_map_ref or s.secret_ref for s in container.env_from]
    assert [s.name for s in sources] == ["healops-engine", "healops-engine"]
    assert container.env_from[0].config_map_ref is not None  # where the S3 endpoint lives
    assert job.spec.backoff_limit == 0  # type: ignore[attr-defined]  # a partial delete is never retried


def test_the_generic_call_uses_the_installed_clients_signature(monkeypatch) -> None:  # noqa: ANN001
    """Regression, found on the kind cluster: `pods/resize` went through `call_api` with a keyword
    the pinned client no longer has, so every in-place resize raised before reaching the server.
    Checked against the real method's signature, so the next client bump fails here, not live."""
    import inspect

    from kubernetes import client as k8s_client

    from k8s import client

    seen: dict[str, object] = {}

    class Recorder:
        def call_api(self, *args: object, **kwargs: object) -> dict[str, str]:
            inspect.signature(k8s_client.ApiClient.call_api).bind(None, *args, **kwargs)
            seen.update(kwargs)
            return {"kind": "Pod"}

    monkeypatch.setattr(client, "api_client", lambda: Recorder())
    result = asyncio.run(
        client.call_api("resize", "/api/v1/namespaces/c/pods/p/resize", "PATCH", body={})
    )

    assert result == {"kind": "Pod"}
    assert seen["response_types_map"] == {200: "object", 201: "object"}

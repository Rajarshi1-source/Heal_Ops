"""Run a node-agent task on a specific node, as a short-lived Job, and read back its result.

The Job is deliberately narrow:

* `nodeName` pins it to the node being cleaned -- it is never scheduled, so the disk-pressure taint
  cannot keep it off the one node it exists for (it tolerates the taint as well, for clarity);
* only the configured log roots are mounted from the host, read-write, and nothing else;
* it runs the engine image, so the archive code is the same code the unit suite tests;
* `backoffLimit: 0`: a failed archive is reported, never silently retried -- a retry of a
  partially-applied deletion is exactly what the retry rules forbid;
* `activeDeadlineSeconds` bounds it, and the Job is deleted once its result is read.

In `local` mode (development, the unit suite) the same operation runs in-process with no prefix.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import structlog
from kubernetes import client as k8s_client

import node_agent
from config import get_settings
from k8s.client import KubernetesUnavailable, call, core_v1

log = structlog.get_logger(__name__)

HOST_PREFIX = "/host"


def _batch() -> k8s_client.BatchV1Api:
    from k8s.client import api_client

    return k8s_client.BatchV1Api(api_client())


def _job_name(node: str, op: str) -> str:
    stamp = f"{asyncio.get_running_loop().time():.0f}"[-6:]
    safe = re.sub(r"[^a-z0-9-]", "-", node.lower())[:30].strip("-")
    return f"healops-agent-{op}-{safe}-{stamp}"


def job_manifest(name: str, node: str, task: dict[str, Any]) -> k8s_client.V1Job:
    settings = get_settings()
    roots = list(task["roots"])
    volumes = [
        k8s_client.V1Volume(
            name=f"root-{i}",
            host_path=k8s_client.V1HostPathVolumeSource(path=root, type="Directory"),
        )
        for i, root in enumerate(roots)
    ]
    mounts = [
        k8s_client.V1VolumeMount(name=f"root-{i}", mount_path=f"{HOST_PREFIX}{root}")
        for i, root in enumerate(roots)
    ]
    container = k8s_client.V1Container(
        name="agent",
        image=settings.node_agent_image,
        image_pull_policy="IfNotPresent",
        command=["python", "-m", "node_agent"],
        working_dir="/app/engine/src",
        env=[
            k8s_client.V1EnvVar(name=node_agent.TASK_ENV, value=node_agent.encode(task)),
            k8s_client.V1EnvVar(name="HEALOPS_AGENT_HOST_PREFIX", value=HOST_PREFIX),
        ],
        # The engine's own configuration AND its credentials. The S3 endpoint, bucket and region
        # are configuration, not secrets: an agent given only the Secret archived to the wrong
        # place -- or, in kind, reported object storage unreachable and rightly deleted nothing.
        env_from=[
            k8s_client.V1EnvFromSource(
                config_map_ref=k8s_client.V1ConfigMapEnvSource(name=settings.node_agent_config_map)
            ),
            k8s_client.V1EnvFromSource(
                secret_ref=k8s_client.V1SecretEnvSource(name=settings.node_agent_secret)
            ),
        ],
        volume_mounts=mounts,
        # Root, because node logs are root-owned -- but with every capability dropped except the
        # three the work needs: remove a file it does not own (DAC_OVERRIDE, FOWNER), and hand a
        # restored file back to its owner (CHOWN). No privilege escalation, read-only image.
        security_context=k8s_client.V1SecurityContext(
            run_as_user=0,
            run_as_non_root=False,
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=k8s_client.V1Capabilities(
                drop=["ALL"], add=["DAC_OVERRIDE", "FOWNER", "CHOWN"]
            ),
        ),
        resources=k8s_client.V1ResourceRequirements(
            requests={"cpu": "20m", "memory": "64Mi"}, limits={"memory": "256Mi"}
        ),
    )
    return k8s_client.V1Job(
        metadata=k8s_client.V1ObjectMeta(
            name=name,
            labels={
                "app.kubernetes.io/name": "healops",
                "app.kubernetes.io/component": "node-agent",
                "healops.io/remediation": "disabled",
            },
        ),
        spec=k8s_client.V1JobSpec(
            backoff_limit=0,
            active_deadline_seconds=settings.node_agent_timeout_seconds,
            ttl_seconds_after_finished=600,
            template=k8s_client.V1PodTemplateSpec(
                metadata=k8s_client.V1ObjectMeta(
                    labels={"app.kubernetes.io/component": "node-agent", "job-name": name}
                ),
                spec=k8s_client.V1PodSpec(
                    node_name=node,
                    restart_policy="Never",
                    service_account_name=settings.node_agent_service_account,
                    automount_service_account_token=False,
                    tolerations=[
                        k8s_client.V1Toleration(operator="Exists", effect="NoSchedule"),
                        k8s_client.V1Toleration(operator="Exists", effect="NoExecute"),
                    ],
                    containers=[container],
                    volumes=volumes,
                ),
            ),
        ),
    )


async def run(node: str, task: dict[str, Any]) -> dict[str, Any]:
    """Perform `task` on `node`. An agent failure is a result, never an exception."""
    settings = get_settings()
    if settings.node_agent_mode == "local":
        return await node_agent.perform(task)

    namespace = settings.self_namespace
    name = _job_name(node, str(task["op"]))
    try:
        await call(
            "create_agent_job",
            _batch().create_namespaced_job,
            namespace,
            job_manifest(name, node, task),
        )
        return await _await_result(namespace, name, settings.node_agent_timeout_seconds)
    except KubernetesUnavailable as exc:
        return {"ok": False, "detail": f"node agent unavailable: {exc}"}
    finally:
        try:
            await call(
                "delete_agent_job",
                _batch().delete_namespaced_job,
                name,
                namespace,
                propagation_policy="Background",
            )
        except Exception as exc:  # noqa: BLE001 - the TTL controller removes it regardless
            log.warning("could not delete agent job", job=name, error=str(exc))


def log_text(response: Any) -> str:
    """A pod log as text.

    Read raw and decoded here, not through the client's deserialiser: the typed client returns the
    `str()` of the bytes -- a literal `b'HEALOPS_AGENT_RESULT {...}'` -- so the result marker never
    starts a line and every agent result read as missing. Found on the kind cluster, where every
    disk scan "finished without a result" while the Job's own log held the answer.
    """
    data = getattr(response, "data", response)
    return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)


async def _await_result(namespace: str, name: str, limit_seconds: int) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + limit_seconds + 15
    while asyncio.get_running_loop().time() < deadline:
        job = await call("get_agent_job", _batch().read_namespaced_job_status, name, namespace)
        status = job.status
        if status and (status.succeeded or status.failed):
            pods = await call(
                "list_agent_pods",
                core_v1().list_namespaced_pod,
                namespace,
                label_selector=f"job-name={name}",
            )
            for pod in pods.items:
                response = await call(
                    "agent_logs",
                    core_v1().read_namespaced_pod_log,
                    pod.metadata.name,
                    namespace,
                    _preload_content=False,
                )
                result = node_agent.decode_result(log_text(response))
                if result is not None:
                    return result
            return {"ok": False, "detail": f"agent job {name} finished without a result"}
        await asyncio.sleep(2)
    return {"ok": False, "detail": f"agent job {name} did not finish within {limit_seconds}s"}

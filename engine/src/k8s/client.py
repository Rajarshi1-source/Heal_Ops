"""Kubernetes API client.

Two things this wrapper exists to centralise:

* **Timeouts on every call.** An unbounded wait masquerading as "in progress" is the bug that makes
  a degraded API server look like a hung remediation.
* **The generic `call_api` escape hatch.** The typed Python client lags new subresources, so
  `pods/resize` (GA in 1.35) is not exposed as a method. Reaching it through `call_api` is not a
  workaround to be embarrassed about -- it is the documented consequence of pinning the client
  minor to the cluster minor.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

import metrics
from config import get_settings


class KubernetesUnavailable(RuntimeError):
    """The API server could not be reached. Callers fail closed."""


@dataclass(frozen=True, slots=True)
class ServerVersion:
    major: int
    minor: int

    @property
    def supports_in_place_resize(self) -> bool:
        """In-place pod resize went GA in 1.35. That is the floor's entire reason for existing."""
        return (self.major, self.minor) >= (1, 35)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"


@lru_cache(maxsize=1)
def _load_config() -> None:
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        try:
            k8s_config.load_kube_config()
        except Exception as exc:  # noqa: BLE001
            raise KubernetesUnavailable(f"no usable kubeconfig: {exc}") from exc


@lru_cache(maxsize=1)
def api_client() -> k8s_client.ApiClient:
    _load_config()
    configuration = k8s_client.Configuration.get_default_copy()
    configuration.retries = 0  # retries are the caller's decision, never silent
    return k8s_client.ApiClient(configuration)


def apps_v1() -> k8s_client.AppsV1Api:
    return k8s_client.AppsV1Api(api_client())


def core_v1() -> k8s_client.CoreV1Api:
    return k8s_client.CoreV1Api(api_client())


def autoscaling_v2() -> k8s_client.AutoscalingV2Api:
    return k8s_client.AutoscalingV2Api(api_client())


def coordination_v1() -> k8s_client.CoordinationV1Api:
    return k8s_client.CoordinationV1Api(api_client())


def policy_v1() -> k8s_client.PolicyV1Api:
    return k8s_client.PolicyV1Api(api_client())


async def call(verb: str, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking client call in a worker thread with a hard timeout.

    The official client is synchronous. Calling it directly from the event loop would let one slow
    API request stall the webhook receiver, which must stay responsive or Alertmanager starts
    retrying and the queue amplifies.
    """
    settings = get_settings()
    kwargs.setdefault("_request_timeout", settings.k8s_timeout_seconds)
    metrics.k8s_api_calls.labels(verb=verb).inc()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(fn, *args, **kwargs),
            timeout=settings.k8s_timeout_seconds + 2,
        )
    except TimeoutError as exc:
        metrics.k8s_api_errors.labels(verb=verb).inc()
        raise KubernetesUnavailable(
            f"{verb} timed out after {settings.k8s_timeout_seconds}s"
        ) from exc
    except ApiException:
        metrics.k8s_api_errors.labels(verb=verb).inc()
        raise


async def call_api(
    verb: str,
    path: str,
    method: str,
    body: Any = None,
    content_type: str = "application/strategic-merge-patch+json",
) -> Any:
    """Generic request, for subresources the typed client does not expose.

    `pods/resize` is the reason this exists.
    """
    settings = get_settings()
    metrics.k8s_api_calls.labels(verb=verb).inc()

    def _invoke() -> Any:
        return api_client().call_api(
            path,
            method,
            body=body,
            header_params={"Content-Type": content_type, "Accept": "application/json"},
            response_type="object",
            auth_settings=["BearerToken"],
            _request_timeout=settings.k8s_timeout_seconds,
            _preload_content=True,
        )

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(_invoke), timeout=settings.k8s_timeout_seconds + 2
        )
    except TimeoutError as exc:
        metrics.k8s_api_errors.labels(verb=verb).inc()
        raise KubernetesUnavailable(f"{verb} timed out") from exc
    except ApiException:
        metrics.k8s_api_errors.labels(verb=verb).inc()
        raise
    return response[0] if isinstance(response, tuple) else response


async def server_version() -> ServerVersion:
    """Read the API server version, so in-place resize support is a fact and not an assumption."""
    version = k8s_client.VersionApi(api_client())
    info = await call("version", version.get_code)
    # Minor can carry a suffix such as "36+" on managed clusters.
    minor_digits = "".join(c for c in str(info.minor) if c.isdigit())
    return ServerVersion(major=int(info.major), minor=int(minor_digits or 0))


async def ping() -> bool:
    try:
        await server_version()
        return True
    except Exception:  # noqa: BLE001 - readiness must report, never raise
        return False

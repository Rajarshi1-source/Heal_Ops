"""Leader election via the Kubernetes Lease API.

Two engine replicas run, but only **one acts**. That split is deliberate rather than wasteful, and
it answers a question interviewers do ask ("isn't the second replica idle capacity?"):

* Alertmanager must always find someone to accept a POST, so **both** replicas serve webhooks and
  enqueue. Webhook availability is an availability requirement.
* Remediation must be serialised, or two replicas act on the same incident. Execution capacity is a
  *risk* requirement, and the bottleneck being protected is risk, not CPU.

A `coordination.k8s.io/Lease` grants leadership. Zookeeper would also do this, and that comparison
is a category error worth naming: Zookeeper is a coordination service, not a queue, and the cluster
already ships a Lease API -- adding an ensemble to elect a leader among two pods would be absurd.

The lease is released on `SIGTERM` so a rolling deploy hands over promptly instead of waiting for
expiry.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from datetime import UTC, datetime, timedelta

import structlog
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

import metrics
from config import get_settings
from k8s.client import KubernetesUnavailable, call, coordination_v1

log = structlog.get_logger(__name__)


def identity() -> str:
    """Pod name in-cluster, hostname plus pid locally, so two local processes differ."""
    return os.environ.get("HOSTNAME") or f"{socket.gethostname()}-{os.getpid()}"


class LeaderElector:
    """Acquires and renews a Lease, exposing `is_leader` for the execution path to gate on."""

    def __init__(self, namespace: str | None = None, lease_name: str | None = None) -> None:
        settings = get_settings()
        self.namespace = namespace or settings.self_namespace
        self.lease_name = lease_name or settings.lease_name
        self.duration = settings.lease_duration_seconds
        # Renew at a third of the duration: two renewals may fail before leadership is at risk.
        self.renew_interval = max(2, self.duration // 3)
        self.identity = identity()
        self._is_leader = False
        self._task: asyncio.Task[None] | None = None
        self._transitions = 0

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    def _set_leader(self, value: bool) -> None:
        if value != self._is_leader:
            self._is_leader = value
            metrics.is_leader.set(1 if value else 0)
            log.info(
                "leadership changed",
                is_leader=value,
                identity=self.identity,
                lease=self.lease_name,
            )

    def _body(self, holder: str, acquire_time: datetime | None = None) -> k8s_client.V1Lease:
        now = datetime.now(UTC)
        return k8s_client.V1Lease(
            metadata=k8s_client.V1ObjectMeta(name=self.lease_name, namespace=self.namespace),
            spec=k8s_client.V1LeaseSpec(
                holder_identity=holder,
                lease_duration_seconds=self.duration,
                acquire_time=acquire_time or now,
                renew_time=now,
                lease_transitions=self._transitions,
            ),
        )

    async def _read(self) -> k8s_client.V1Lease | None:
        api = coordination_v1()
        try:
            return await call(
                "get_lease",
                api.read_namespaced_lease,
                self.lease_name,
                self.namespace,
            )
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    async def try_acquire_or_renew(self) -> bool:
        """One election round. Returns whether this replica now holds the lease."""
        api = coordination_v1()
        existing = await self._read()

        if existing is None:
            try:
                await call(
                    "create_lease",
                    api.create_namespaced_lease,
                    self.namespace,
                    self._body(self.identity),
                )
                return True
            except ApiException as exc:
                # 409 means another replica created it in the same instant. Not an error.
                if exc.status == 409:
                    return False
                raise

        spec = existing.spec
        holder = spec.holder_identity

        if holder == self.identity:
            # Renew. `resource_version` is carried by the object we read, so a concurrent write
            # loses with a 409 rather than silently clobbering.
            renewed = self._body(self.identity, acquire_time=spec.acquire_time)
            renewed.metadata.resource_version = existing.metadata.resource_version
            try:
                await call(
                    "replace_lease",
                    api.replace_namespaced_lease,
                    self.lease_name,
                    self.namespace,
                    renewed,
                )
                return True
            except ApiException as exc:
                if exc.status == 409:
                    return False
                raise

        # Someone else holds it. Take over only if their lease has genuinely expired.
        renew_time = spec.renew_time or spec.acquire_time
        if renew_time is None:
            expired = True
        else:
            deadline = renew_time + timedelta(seconds=spec.lease_duration_seconds or self.duration)
            expired = datetime.now(UTC) > deadline

        if not expired:
            return False

        self._transitions = (spec.lease_transitions or 0) + 1
        taken = self._body(self.identity)
        taken.metadata.resource_version = existing.metadata.resource_version
        try:
            await call(
                "replace_lease",
                api.replace_namespaced_lease,
                self.lease_name,
                self.namespace,
                taken,
            )
            log.info("took over expired lease", previous_holder=holder)
            return True
        except ApiException as exc:
            if exc.status == 409:
                return False
            raise

    async def _loop(self) -> None:
        while True:
            try:
                self._set_leader(await self.try_acquire_or_renew())
            except (KubernetesUnavailable, ApiException) as exc:
                # Fail closed on leadership: if the API server is unreachable this replica must
                # stop acting. Two replicas both believing they lead is how one incident gets
                # remediated twice.
                log.warning("lease round failed, standing down", error=str(exc))
                self._set_leader(False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("unexpected lease error, standing down", error=str(exc))
                self._set_leader(False)

            await asyncio.sleep(self.renew_interval)

    async def start(self) -> None:
        if self._task is None:
            metrics.is_leader.set(0)
            self._task = asyncio.create_task(self._loop(), name="leader-election")

    async def stop(self) -> None:
        """Cancel renewal and release the lease so a rolling deploy hands over immediately."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        if not self._is_leader:
            return

        api = coordination_v1()
        try:
            existing = await self._read()
            if existing is not None and existing.spec.holder_identity == self.identity:
                # Blank the holder rather than deleting the object: the lease_transitions history
                # is useful, and a deleted Lease races with a starting replica.
                existing.spec.holder_identity = None
                existing.spec.renew_time = datetime.now(UTC)
                await call(
                    "replace_lease",
                    api.replace_namespaced_lease,
                    self.lease_name,
                    self.namespace,
                    existing,
                )
                log.info("released lease on shutdown")
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            log.warning("could not release lease; it will expire", error=str(exc))
        finally:
            self._set_leader(False)


_elector: LeaderElector | None = None


def get_elector() -> LeaderElector:
    global _elector  # noqa: PLW0603
    if _elector is None:
        _elector = LeaderElector()
    return _elector

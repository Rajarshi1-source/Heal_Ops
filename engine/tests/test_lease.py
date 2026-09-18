"""Leader election.

The invariant: **at most one replica believes it leads**. Everything here is a scenario that could
break it, because two replicas both acting on one incident is how a single alert produces a double
remediation.

The Kubernetes API is faked at the `coordination_v1()` boundary. A real API server is exercised by
the chaos suite; what matters here is the decision logic -- when to take over, when to stand down,
and what happens when the API server itself is unreachable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from k8s import lease as lease_module
from k8s.client import KubernetesUnavailable
from k8s.lease import LeaderElector

pytestmark = pytest.mark.asyncio


class FakeLeaseApi:
    """A minimal in-memory Lease store with optimistic concurrency."""

    def __init__(self, existing: k8s_client.V1Lease | None = None) -> None:
        self.lease = existing
        self.creates = 0
        self.replaces = 0
        self.raise_on_create: int | None = None
        self.raise_on_replace: int | None = None
        self.unavailable = False

    def _check(self) -> None:
        if self.unavailable:
            raise KubernetesUnavailable("api server unreachable")

    def read_namespaced_lease(self, name: str, namespace: str, **_: Any) -> k8s_client.V1Lease:
        self._check()
        if self.lease is None:
            raise ApiException(status=404, reason="Not Found")
        return self.lease

    def create_namespaced_lease(
        self, namespace: str, body: k8s_client.V1Lease, **_: Any
    ) -> k8s_client.V1Lease:
        self._check()
        if self.raise_on_create is not None:
            raise ApiException(status=self.raise_on_create, reason="conflict")
        self.creates += 1
        body.metadata.resource_version = "1"
        self.lease = body
        return body

    def replace_namespaced_lease(
        self, name: str, namespace: str, body: k8s_client.V1Lease, **_: Any
    ) -> k8s_client.V1Lease:
        self._check()
        if self.raise_on_replace is not None:
            raise ApiException(status=self.raise_on_replace, reason="conflict")
        self.replaces += 1
        current = int(self.lease.metadata.resource_version or "1") if self.lease else 1
        body.metadata.resource_version = str(current + 1)
        self.lease = body
        return body


def make_lease(
    holder: str | None,
    *,
    renewed_seconds_ago: float = 0,
    duration: int = 15,
    transitions: int = 0,
) -> k8s_client.V1Lease:
    now = datetime.now(UTC)
    return k8s_client.V1Lease(
        metadata=k8s_client.V1ObjectMeta(
            name="healops-engine-leader", namespace="healops", resource_version="7"
        ),
        spec=k8s_client.V1LeaseSpec(
            holder_identity=holder,
            lease_duration_seconds=duration,
            acquire_time=now - timedelta(seconds=renewed_seconds_ago),
            renew_time=now - timedelta(seconds=renewed_seconds_ago),
            lease_transitions=transitions,
        ),
    )


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeLeaseApi:
    fake = FakeLeaseApi()
    monkeypatch.setattr(lease_module, "coordination_v1", lambda: fake)

    async def fake_call(_verb: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    monkeypatch.setattr(lease_module, "call", fake_call)
    return fake


def elector(identity: str = "engine-0") -> LeaderElector:
    e = LeaderElector(namespace="healops", lease_name="healops-engine-leader")
    e.identity = identity
    return e


# --- acquiring ----------------------------------------------------------------------------------


async def test_creates_the_lease_when_absent(api: FakeLeaseApi) -> None:
    e = elector()
    assert await e.try_acquire_or_renew() is True
    assert api.creates == 1
    assert api.lease is not None
    assert api.lease.spec.holder_identity == "engine-0"


async def test_conflict_on_create_means_someone_else_won(api: FakeLeaseApi) -> None:
    """Two replicas starting simultaneously both see 404 and both try to create.

    The loser gets 409 and must stand down, not error.
    """
    api.raise_on_create = 409
    e = elector()
    assert await e.try_acquire_or_renew() is False


async def test_renews_its_own_lease(api: FakeLeaseApi) -> None:
    api.lease = make_lease("engine-0")
    e = elector("engine-0")
    assert await e.try_acquire_or_renew() is True
    assert api.replaces == 1


async def test_does_not_steal_a_live_lease(api: FakeLeaseApi) -> None:
    """The core mutual-exclusion assertion.

    Another replica holds a lease renewed two seconds ago with a fifteen-second duration. Taking it
    would mean two leaders.
    """
    api.lease = make_lease("engine-1", renewed_seconds_ago=2, duration=15)
    e = elector("engine-0")

    assert await e.try_acquire_or_renew() is False
    assert api.replaces == 0
    assert api.lease.spec.holder_identity == "engine-1"


async def test_takes_over_an_expired_lease(api: FakeLeaseApi) -> None:
    # The previous leader died; its lease lapsed. Someone must pick up execution.
    api.lease = make_lease("engine-1", renewed_seconds_ago=60, duration=15)
    e = elector("engine-0")

    assert await e.try_acquire_or_renew() is True
    assert api.lease.spec.holder_identity == "engine-0"


async def test_takeover_increments_the_transition_count(api: FakeLeaseApi) -> None:
    api.lease = make_lease("engine-1", renewed_seconds_ago=60, transitions=3)
    e = elector("engine-0")

    await e.try_acquire_or_renew()
    assert api.lease.spec.lease_transitions == 4


async def test_conflict_on_takeover_means_another_replica_got_there_first(
    api: FakeLeaseApi,
) -> None:
    api.lease = make_lease("engine-1", renewed_seconds_ago=60)
    api.raise_on_replace = 409
    e = elector("engine-0")
    assert await e.try_acquire_or_renew() is False


async def test_conflict_on_renewal_relinquishes(api: FakeLeaseApi) -> None:
    # A 409 while renewing means our view was stale. Assuming we still lead would be the unsafe
    # choice, so we report False and re-elect on the next round.
    api.lease = make_lease("engine-0")
    api.raise_on_replace = 409
    e = elector("engine-0")
    assert await e.try_acquire_or_renew() is False


async def test_lease_with_no_renew_time_is_treated_as_expired(api: FakeLeaseApi) -> None:
    stale = make_lease("engine-1")
    stale.spec.renew_time = None
    stale.spec.acquire_time = None
    api.lease = stale

    e = elector("engine-0")
    assert await e.try_acquire_or_renew() is True


# --- failure handling ---------------------------------------------------------------------------


async def test_api_unavailable_propagates_so_the_loop_stands_down(api: FakeLeaseApi) -> None:
    """Fail closed on leadership.

    If the API server cannot be reached this replica must stop acting. The alternative -- assuming
    continued leadership -- is how a network partition produces two leaders.
    """
    api.unavailable = True
    e = elector()
    with pytest.raises(KubernetesUnavailable):
        await e.try_acquire_or_renew()


async def test_unexpected_api_error_is_not_swallowed(api: FakeLeaseApi) -> None:
    api.raise_on_create = 500
    e = elector()
    with pytest.raises(ApiException):
        await e.try_acquire_or_renew()


# --- state tracking -----------------------------------------------------------------------------


async def test_is_leader_starts_false() -> None:
    assert elector().is_leader is False


async def test_set_leader_tracks_transitions(api: FakeLeaseApi) -> None:
    e = elector()
    e._set_leader(True)
    assert e.is_leader is True
    e._set_leader(False)
    assert e.is_leader is False


async def test_renew_interval_leaves_room_for_failures() -> None:
    """Renewing at a third of the duration means two consecutive failures are survivable.

    Renewing at the deadline would make a single slow API call cost leadership, and every handover
    is a window where nothing is executing.
    """
    e = elector()
    assert e.renew_interval < e.duration
    assert e.duration // e.renew_interval >= 2


async def test_release_on_shutdown_clears_the_holder(api: FakeLeaseApi) -> None:
    """A rolling deploy should hand over immediately rather than wait for expiry."""
    api.lease = make_lease("engine-0")
    e = elector("engine-0")
    await e.try_acquire_or_renew()
    e._set_leader(True)

    await e.stop()

    assert api.lease.spec.holder_identity is None
    assert e.is_leader is False


async def test_release_does_nothing_when_not_the_holder(api: FakeLeaseApi) -> None:
    api.lease = make_lease("engine-1", renewed_seconds_ago=2)
    e = elector("engine-0")
    await e.stop()
    # Another replica's lease must not be disturbed by our shutdown.
    assert api.lease.spec.holder_identity == "engine-1"


async def test_shutdown_never_raises(api: FakeLeaseApi) -> None:
    # A failure while releasing must not prevent the process exiting; the lease will simply expire.
    api.lease = make_lease("engine-0")
    e = elector("engine-0")
    e._set_leader(True)
    api.unavailable = True
    await e.stop()
    assert e.is_leader is False


async def test_identity_is_unique_per_process() -> None:
    assert lease_module.identity()
    # Two local processes must not collide, or both would think they hold the same lease.
    assert "-" in lease_module.identity() or lease_module.identity().startswith("engine")

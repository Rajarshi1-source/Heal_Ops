"""Fixtures for the chaos suite. Every test here is marked `chaos` and needs a live kind cluster
with kube-prometheus-stack, the alerting rules, the chaos app and HealOps installed (`make cluster
observability images deploy`, or the CI chaos job)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.chaos import harness


@pytest.fixture(scope="session")
def cluster() -> None:
    try:
        harness.require_cluster()
    except harness.ClusterUnavailable as exc:
        pytest.skip(str(exc))
    harness.configure_in_process_engine()


@pytest.fixture(scope="session")
def engine(cluster: None) -> Iterator[harness.Engine]:
    # A tripped breaker refuses every action, so every fix test would fail for one reason. Say so
    # once, up front -- and leave it tripped: the suite never overrides a safety control.
    state = harness.breaker_state()
    if state != "closed":
        pytest.fail(
            f"the remediation breaker is {state}: inspect `healopsctl breaker-status`, and "
            "`healopsctl unfreeze` only once the cause is understood"
        )
    with harness.port_forwarded_engine() as api:
        yield api


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Everything under this directory is a chaos test, whether or not its module remembered the
    # marker -- so the hermetic unit gate can never pick one up by accident.
    for item in items:
        if "tests/chaos/" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.chaos)

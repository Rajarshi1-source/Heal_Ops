"""A replica costs what it reserves.

Regression, found on the kind cluster: a flat ₹420 per replica priced cpu-burner's 50m pods like a
four-core service, so the policy's ₹500 approval line sent every two-replica scale-out to a human.
The line itself is right; the price under it was wrong.
"""

from __future__ import annotations

from bundle import ContainerState
from pricing import UNSIZED_CPU_MILLIS, replica_cost

APPROVAL_LINE_INR = 500.0  # policy/remediation.yaml, approve-costly


def test_two_small_replicas_stay_under_the_approval_line() -> None:
    small = (
        ContainerState(name="burner", image="x", cpu_request_millis=50, memory_request_mib=32),
    )
    assert 2 * replica_cost(small) < APPROVAL_LINE_INR


def test_two_large_replicas_cross_it() -> None:
    large = (
        ContainerState(name="api", image="x", cpu_request_millis=1000, memory_request_mib=2048),
    )
    assert 2 * replica_cost(large) > APPROVAL_LINE_INR


def test_requests_win_over_limits() -> None:
    both = (
        ContainerState(
            name="a",
            image="x",
            cpu_request_millis=100,
            cpu_limit_millis=2000,
            memory_request_mib=64,
            memory_limit_mib=4096,
        ),
    )
    requests_only = (
        ContainerState(name="a", image="x", cpu_request_millis=100, memory_request_mib=64),
    )
    assert replica_cost(both) == replica_cost(requests_only)


def test_an_unsized_container_is_priced_as_large_so_a_human_decides() -> None:
    unsized = (ContainerState(name="mystery", image="x"),)
    sized = (
        ContainerState(
            name="mystery",
            image="x",
            cpu_request_millis=UNSIZED_CPU_MILLIS,
            memory_request_mib=1024,
        ),
    )
    assert replica_cost(unsized) == replica_cost(sized)
    assert 2 * replica_cost(unsized) > APPROVAL_LINE_INR

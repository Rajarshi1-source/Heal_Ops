"""The pinned price list every cost on a plan is computed from.

Constants, not a live pricing API: a plan's cost decides whether policy asks a human, so it has to
be reproducible in the decision eval and identical on replay. An approximate figure that gates on
the right side is worth more than an exact one that makes planning non-deterministic.

A replica is priced by what it reserves -- its containers' requests -- because that is what a
scheduler, and therefore an invoice, charges for. A flat per-replica figure priced a 50m sidecar
like a four-core service, and sent every scale-out to a human whatever its size.
"""

from __future__ import annotations

from collections.abc import Iterable

from bundle import ContainerState

#: Rupees per vCPU-month and per GiB-month of reserved capacity.
INR_PER_VCPU_MONTH = 1800.0
INR_PER_GIB_MONTH = 300.0

#: What a container that declares nothing is assumed to reserve. Deliberately generous: an unsized
#: workload is priced as if it were large, so the uncertainty lands on "ask a human".
UNSIZED_CPU_MILLIS = 1000
UNSIZED_MEMORY_MIB = 1024


def replica_cost(containers: Iterable[ContainerState]) -> float:
    """Monthly cost of ONE more replica: requests, else limits, else the unsized assumption."""
    total = 0.0
    for c in containers:
        cpu = c.cpu_request_millis or c.cpu_limit_millis or UNSIZED_CPU_MILLIS
        memory = c.memory_request_mib or c.memory_limit_mib or UNSIZED_MEMORY_MIB
        total += cpu / 1000 * INR_PER_VCPU_MONTH + memory / 1024 * INR_PER_GIB_MONTH
    return round(total, 2)

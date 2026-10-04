"""The five runbooks and the contract they implement.

A runbook that cannot describe its own undo is not registered -- `compensate()` is abstract.

Importing this package registers every runbook as a side effect, which is why `load_all()` exists as
an explicit alternative: tests that want an empty registry can avoid it, and anything that needs the
registry populated can say so rather than depending on import order.
"""

from __future__ import annotations

from runbooks.base import ExecutionResult, Runbook, RunbookRegistry, SignalWindow, registry

__all__ = [
    "ExecutionResult",
    "Runbook",
    "RunbookRegistry",
    "SignalWindow",
    "load_all",
    "registry",
]


def load_all() -> RunbookRegistry:
    """Import every runbook module so the registry is populated.

    Idempotent: registration happens at module import, and Python caches modules, so calling this
    twice does not attempt a duplicate registration.
    """
    from runbooks import (  # noqa: F401
        cpu_scale_up,
        crashloop_rollback,
        disk_cleanup,
        node_drain,
        oom_memory_bump,
    )

    return registry

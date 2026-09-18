"""`GET /api/runbooks` -- the registry, rendered.

This endpoint is the model card as data: each runbook's triggers, the fault injector that proves it,
its reversibility class, and its promotion mode per namespace. The dashboard's runbook catalog reads
it directly.

Exposing the chaos fixture is deliberate. A reader can follow the name to the injector that proves
the runbook works, making "how do you know it works?" answerable without reading the test suite.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from runbooks import load_all

router = APIRouter(tags=["runbooks"])


@router.get("/api/runbooks", summary="List registered runbooks and their contracts")
async def list_runbooks() -> dict[str, Any]:
    registry = load_all()
    return {
        "runbooks": registry.describe(),
        "triggers": list(registry.triggers),
        "count": len(registry.all()),
    }

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
    from sqlalchemy import select

    from db.connection import session_scope
    from db.models import RunbookPromotion

    registry = load_all()
    ladder: dict[str, list[dict[str, Any]]] = {}
    try:
        async with session_scope() as session:
            for row in (await session.execute(select(RunbookPromotion))).scalars():
                ladder.setdefault(row.runbook, []).append(
                    {
                        "namespace": row.namespace,
                        "mode": row.mode,
                        "consecutive_good": row.consecutive_good,
                        "total_executions": row.total_executions,
                        "total_reverted": row.total_reverted,
                        "shadow_judged": row.shadow_judged,
                        "shadow_agreements": row.shadow_agreements,
                        "last_changed_at": row.last_changed_at,
                        "changed_reason": row.changed_reason,
                    }
                )
    except Exception:  # noqa: BLE001 - render the catalog without the ladder, not nothing
        ladder = {}

    runbooks = [
        {**entry, "promotion": sorted(ladder.get(entry["name"], []), key=lambda r: r["namespace"])}
        for entry in registry.describe()
    ]
    return {"runbooks": runbooks, "triggers": list(registry.triggers), "count": len(runbooks)}

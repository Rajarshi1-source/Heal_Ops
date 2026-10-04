"""Rebuilding a plan, and the decision that gated it, from their persisted rows.

Every consumer that acts on a plan after the request that created it has ended -- the verify loop,
the watchdog CronJob, the approved-execution worker, the Slack handler restating what was approved
-- works from the database rather than from memory. That is the whole design: a compensation, an
approval or a verification owned by the process that can crash is not one. So there is exactly one
way back from a row to a `RemediationPlan`, and it lives here.
"""

from __future__ import annotations

from typing import Any, cast

from db.models import RemediationPlanRow
from plan import (
    ApiCall,
    BlastRadius,
    Durability,
    ExpectedSignal,
    RemediationPlan,
    Reversibility,
)
from policy import Effect, PolicyDecision


def rehydrate(row: RemediationPlanRow) -> RemediationPlan:
    """Rebuild the plan object, and crucially its **compensation**, from its persisted row.

    This is why `compensation` is a column rather than something derived at revert time: by the time
    anything reads it back, the cluster state it would be derived from is the thing being undone.
    """
    signal = row.expected_signal or {}
    radius = row.blast_radius or {}

    return RemediationPlan(
        runbook=row.runbook,
        incident_id=row.incident_id,
        target_kind=row.target_kind,
        target_name=row.target_name,
        target_namespace=row.target_namespace,
        actions=tuple(_to_call(c) for c in row.actions or []),
        compensation=tuple(_to_call(c) for c in row.compensation or []),
        reversibility=Reversibility(row.reversibility),
        durability=Durability(row.durability),
        ttl_seconds=row.ttl_seconds,
        blast_radius=BlastRadius(
            pods=int(radius.get("pods", 0)),
            nodes=int(radius.get("nodes", 0)),
            namespaces=tuple(radius.get("namespaces", ())),
            user_facing=bool(radius.get("user_facing", False)),
        ),
        expected_signal=None if not signal else _to_signal(signal),
        confidence=float(row.confidence),
        summary=row.summary or "",
        cost_delta_inr_month=float(row.cost_delta_inr_month or 0),
        refusal_reason=row.refusal_reason,
        planner_version=row.planner_version,
        plan_id=row.id,
    )


def decision_of(row: RemediationPlanRow) -> PolicyDecision:
    """The policy decision recorded alongside the plan.

    An approved execution runs under the decision made when it was planned, not under a fresh
    evaluation: the human approved *that* plan for *those* reasons, and re-gating it ten minutes
    later against a different budget would make the approval mean something other than what was
    shown on the card.
    """
    return PolicyDecision(
        effect=cast(Effect, row.policy_decision),
        reasons=tuple(row.policy_reasons or ()),
    )


def _to_call(raw: dict[str, Any]) -> ApiCall:
    namespace = raw.get("namespace")
    subresource = raw.get("subresource")
    return ApiCall(
        verb=str(raw.get("verb", "")),
        resource=str(raw.get("resource", "")),
        name=str(raw.get("name", "")),
        namespace=str(namespace) if namespace is not None else None,
        body=raw.get("body") or {},
        subresource=str(subresource) if subresource is not None else None,
    )


def _to_signal(raw: dict[str, Any]) -> ExpectedSignal:
    return ExpectedSignal(
        alert_should_clear=str(raw.get("alert_should_clear", "")),
        queries=raw.get("queries") or {},
        thresholds=raw.get("thresholds") or {},
        window_seconds=int(raw.get("window_seconds") or 300),
    )

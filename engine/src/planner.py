"""`plan_for(bundle)`: the decision, as a pure function of the incident bundle.

This is the function the decision eval grades, `healopsctl plan` prints, and the chaos tests drive.
It is pure on purpose -- no cluster, no database, no clock, no network -- because three properties
depend on it:

* the **decision eval is hermetic**: committed bundles in, graded decisions out, no cluster in CI;
* the **determinism digest is stable**: the same bundle must produce a byte-identical plan, and the
  frozen digests per `PLANNER_VERSION` make every decision-affecting change visible in review;
* **replay is offline**: an auditor can reconstruct what HealOps would decide from the bundle alone.

Runbook selection, planning, GitOps preparation and the policy gate all run here, in that order --
the same order the orchestrator uses, so the eval grades what production actually does. What is
deliberately *not* here is everything stateful: the budget, the breaker and the promotion ladder
are inputs, defaulting to "closed breaker, empty bucket, promoted", so the eval grades the decision
logic rather than whatever state a ladder happened to be in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import gitops
import policy as policy_module
from bundle import IncidentBundle, Target
from plan import RemediationPlan, refuse
from policy import BudgetSnapshot, Policy, PolicyDecision, PromotionSnapshot, TargetView
from runbooks import load_all

Effect = Literal["allow", "require_approval", "deny", "refuse"]


@dataclass(frozen=True, slots=True)
class Decision:
    """Everything the planner decided about one incident."""

    runbook: str | None
    plan: RemediationPlan
    policy: PolicyDecision | None

    @property
    def effect(self) -> Effect:
        """The single label the corpus grades: refuse beats any policy outcome."""
        if self.plan.is_refusal or self.policy is None:
            return "refuse"
        return self.policy.effect


def target_view(target: Target) -> TargetView:
    return TargetView(
        kind=target.kind,
        name=target.name,
        namespace=target.namespace,
        replicas=target.replicas,
        labels=dict(target.labels),
        annotations=dict(target.annotations),
    )


def replan(bundle: IncidentBundle) -> RemediationPlan | None:
    """The plan alone, built exactly as the orchestrator builds it. None when no runbook answers."""
    runbook = load_all().for_alert(bundle.cause_alert.alertname)
    if runbook is None:
        return None
    return gitops.prepare(runbook.plan(bundle, bundle.target), bundle.target)


def plan_for(
    bundle: IncidentBundle,
    *,
    promotion: PromotionSnapshot | None = None,
    budget: BudgetSnapshot | None = None,
    active_policy: Policy | None = None,
) -> Decision:
    """Select the runbook, plan, prepare for GitOps, and gate -- with no side effects at all."""
    runbook = load_all().for_alert(bundle.cause_alert.alertname)
    if runbook is None:
        # An unrecognised alert escalates rather than being force-fitted to a runbook.
        return Decision(
            runbook=None,
            plan=refuse(
                "none",
                bundle.incident_id,
                f"no runbook answers {bundle.cause_alert.alertname}",
                target_kind=bundle.target.kind,
                target_name=bundle.target.name,
                target_namespace=bundle.target.namespace,
            ),
            policy=None,
        )

    plan = gitops.prepare(runbook.plan(bundle, bundle.target), bundle.target)
    if plan.is_refusal:
        return Decision(runbook=runbook.name, plan=plan, policy=None)

    decision = policy_module.evaluate(
        plan,
        target_view(bundle.target),
        budget or BudgetSnapshot(),
        promotion or PromotionSnapshot(mode="auto"),
        active_policy,
    )
    return Decision(runbook=runbook.name, plan=plan, policy=decision)

"""The RemediationPlan.

This object is the project's central idea: **a plan is data, not code.** That is what makes it
reviewable before it runs, shadow-executable without acting, replayable offline, and hashable for
the determinism gate. `terraform plan`, for incident response.

Three properties are structural rather than stylistic:

* **Frozen.** A plan that could be mutated between the policy gate and execution would make the
  gate advisory. Every derivation goes through `dataclasses.replace`.
* **Compensation is part of the plan**, decided before the action runs and never derived afterwards.
  A runbook that cannot describe its own undo does not get registered.
* **A refusal is a first-class plan**, not an exception and not a `None`. "The system correctly
  decided not to act" is a data point the scorecard needs, so it carries the same shape, the same
  audit path, and the same ledger entry as an action.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any, Self


class Reversibility(StrEnum):
    """How completely the prior state can be restored."""

    REVERSIBLE = "reversible"
    """Exact prior state restorable: scale back, rollout undo, resize back."""

    PARTIAL = "partially_reversible"
    """Side effects persist: logs already in S3, pods already rescheduled."""

    IRREVERSIBLE = "irreversible"
    """Always denied by policy. Present so a runbook can *say* it, not so it can do it."""


class Durability(StrEnum):
    """How the fix reaches the cluster, and whether it is allowed to stay."""

    EPHEMERAL = "ephemeral"
    """Imperative, TTL'd, expires on its own. `ttl_seconds` is REQUIRED."""

    DURABLE = "durable"
    """Must land in git as a pull request. The cluster is not the source of truth; git is."""


@dataclass(frozen=True, slots=True)
class ApiCall:
    """One exact API call, with its payload resolved at plan time.

    Resolving the body during planning rather than during execution is what makes the plan
    reviewable: a human reading the Slack card sees the literal patch, not a description of one.
    """

    verb: str
    """patch | scale | evict | cordon | uncordon | resize | s3_put | s3_delete | hpa_patch"""

    resource: str
    """API group and kind, e.g. apps/v1/Deployment."""

    name: str
    namespace: str | None = None
    body: dict[str, Any] = field(default_factory=dict)
    subresource: str | None = None
    """e.g. "resize" -- the typed client does not expose it, so execution uses call_api."""

    def describe(self) -> str:
        target = f"{self.namespace}/{self.name}" if self.namespace else self.name
        sub = f"/{self.subresource}" if self.subresource else ""
        return f"{self.verb} {self.resource}{sub} {target}"


@dataclass(frozen=True, slots=True)
class ExpectedSignal:
    """What recovery is supposed to look like, stated before acting.

    Committing to the recovery criterion in advance is what stops verification becoming a search
    for any metric that happens to look better afterwards.
    """

    alert_should_clear: str
    queries: dict[str, str] = field(default_factory=dict)
    """Committed recovery checks. `$WINDOW` is replaced at verification time with the post-action
    window, so an event count never includes the failure being fixed."""
    thresholds: dict[str, float] = field(default_factory=dict)
    window_seconds: int = 300


@dataclass(frozen=True, slots=True)
class BlastRadius:
    pods: int = 0
    nodes: int = 0
    namespaces: tuple[str, ...] = ()
    user_facing: bool = False

    def score(self) -> int:
        """A cheap ordinal for policy rules, deliberately simple and explainable.

        A policy rule a reviewer can evaluate in their head is worth more than a clever weighting
        nobody trusts. Nodes dominate pods because draining one affects everything on it, and
        user-facing carries a flat premium because the blast radius is customers, not replicas.
        """
        return self.pods + 10 * self.nodes + (20 if self.user_facing else 0)


@dataclass(frozen=True, slots=True)
class RemediationPlan:
    """What we intend to do, before we do it."""

    runbook: str
    incident_id: int | str
    target_kind: str
    target_name: str
    target_namespace: str | None = None

    actions: tuple[ApiCall, ...] = ()
    compensation: tuple[ApiCall, ...] = ()

    reversibility: Reversibility = Reversibility.REVERSIBLE
    durability: Durability = Durability.EPHEMERAL
    blast_radius: BlastRadius = field(default_factory=BlastRadius)
    expected_signal: ExpectedSignal | None = None

    confidence: float = 0.0
    summary: str = ""
    """One line, rendered verbatim in Slack. Written for a human at 3 a.m."""

    cost_delta_inr_month: float = 0.0
    ttl_seconds: int | None = None
    refusal_reason: str | None = None

    planner_version: str = "0"
    gitops_managed: bool = False
    plan_id: str | None = None

    def __post_init__(self) -> None:
        # An ephemeral fix with no expiry is a permanent hand-edit nobody is tracking, and that is
        # precisely how clusters drift away from their manifests. The database enforces this too;
        # failing here means a runbook bug surfaces in its unit test rather than at INSERT time.
        if (
            not self.is_refusal
            and self.durability is Durability.EPHEMERAL
            and self.ttl_seconds is None
        ):
            raise ValueError(
                f"{self.runbook}: an ephemeral plan must set ttl_seconds -- "
                "an imperative change with no expiry never goes away on its own"
            )

        if not self.is_refusal and not self.actions:
            raise ValueError(
                f"{self.runbook}: a plan with no actions and no refusal_reason does nothing; "
                "use refuse() to decline explicitly"
            )

        # The undo must be described up front. Deriving it after the fact means reconstructing the
        # prior state from a cluster that has already changed.
        if (
            not self.is_refusal
            and self.reversibility is not Reversibility.IRREVERSIBLE
            and not self.compensation
        ):
            raise ValueError(
                f"{self.runbook}: a reversible plan must carry its compensation; "
                "a runbook that cannot describe its own undo is not registered"
            )

        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"{self.runbook}: confidence must be in [0, 1]")

    # --- properties ------------------------------------------------------------------------------

    @property
    def is_refusal(self) -> bool:
        """Kept as a property so a caller cannot forget to check for it."""
        return self.refusal_reason is not None

    @property
    def target_key(self) -> str:
        return f"{self.target_namespace or '-'}/{self.target_kind}/{self.target_name}"

    @property
    def requires_git_followup(self) -> bool:
        return self.durability is Durability.DURABLE

    # --- serialisation ---------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The form written to the ledger, the plans table, and the Slack card."""
        data = asdict(self)
        data["reversibility"] = str(self.reversibility)
        data["durability"] = str(self.durability)
        data["blast_radius_score"] = self.blast_radius.score()
        return data

    def canonical_json(self) -> str:
        """Same canonical contract as the ledger, so digests are comparable across tools."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), default=str)

    def digest(self) -> str:
        """The determinism anchor.

        The decision eval asserts that the same incident bundle produces a byte-identical plan.
        `plan_id` is excluded because it is assigned per run, not derived from the decision.
        """
        data = self.to_dict()
        data.pop("plan_id", None)
        payload = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    def describe_actions(self) -> list[str]:
        return [a.describe() for a in self.actions]

    def with_gitops(self, *, managed: bool, durability: Durability, ttl: int | None) -> Self:
        """Derive the GitOps-adjusted plan. See `gitops.prepare`."""
        return replace(self, gitops_managed=managed, durability=durability, ttl_seconds=ttl)

    def with_plan_id(self, plan_id: str) -> Self:
        return replace(self, plan_id=plan_id)


def refuse(
    runbook: str,
    incident_id: int | str,
    reason: str,
    *,
    target_kind: str = "Unknown",
    target_name: str = "unknown",
    target_namespace: str | None = None,
    confidence: float = 1.0,
) -> RemediationPlan:
    """Decline to act, explicitly and auditably.

    A refusal is **not** an error. It is an outcome the efficacy scorecard counts, it gets a ledger
    entry and a Slack message, and its recall is graded at 100% in CI -- because a remediation
    engine that never refuses is a liability.

    `confidence` defaults to 1.0 on purpose: the runbook is highly confident about its refusal, and
    a low number here would make the calibration gate treat a correct decision as a hesitant one.
    """
    return RemediationPlan(
        runbook=runbook,
        incident_id=incident_id,
        target_kind=target_kind,
        target_name=target_name,
        target_namespace=target_namespace,
        refusal_reason=reason,
        confidence=confidence,
        summary=f"REFUSED: {reason}",
        # A refusal changes nothing, so it is trivially reversible and needs no TTL.
        reversibility=Reversibility.REVERSIBLE,
        durability=Durability.EPHEMERAL,
        ttl_seconds=0,
    )

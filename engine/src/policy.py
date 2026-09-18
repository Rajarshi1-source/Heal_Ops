"""The policy gate.

Evaluates `policy/remediation.yaml` against a plan with `cel-python`, in-process. In-process is a
deliberate choice: a demo cluster should not need an extra service to make a decision, and keeping
the gate hermetic is what lets `evals/policy_eval.py` grade the rules in CI without a cluster.

Ordering is fixed, not configurable:

    first DENY wins  ->  then any REQUIRE_APPROVAL  ->  else ALLOW

Two failure modes are handled explicitly rather than left to chance:

* **A rule that raises is treated as a DENY.** An expression referencing a field that turns out
  to be absent must not silently evaluate as "no match" and let an action through. Fail closed.
* **A policy file that will not load stops the gate entirely.** There is no implicit "allow
  everything" fallback, because the one time that path runs is the one time it must not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import celpy
import structlog
import yaml
from celpy import celtypes

from plan import RemediationPlan

log = structlog.get_logger(__name__)

Effect = Literal["allow", "require_approval", "deny"]

# Only these effects exist. A typo in the policy file must fail loudly at load time rather than
# become an unrecognised rule that never fires.
VALID_EFFECTS = ("deny", "require_approval")


class PolicyError(RuntimeError):
    """The policy file is unusable. The gate refuses to operate rather than defaulting to allow."""


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    effect: str
    expr: str
    message: str


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    effect: Effect
    reasons: tuple[str, ...] = ()
    """Rule ids that fired, in evaluation order."""
    messages: tuple[str, ...] = ()
    """Human-readable messages, used verbatim in Slack and the ledger."""

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"

    @property
    def needs_approval(self) -> bool:
        return self.effect == "require_approval"

    @property
    def denied(self) -> bool:
        return self.effect == "deny"

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.effect,
            "reasons": list(self.reasons),
            "messages": list(self.messages),
        }


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """What the gate is allowed to know about the remediation budget."""

    scope: str = "global"
    used_actions: int = 0
    max_actions: int = 20
    breaker_state: str = "closed"


@dataclass(frozen=True, slots=True)
class PromotionSnapshot:
    """The (runbook, namespace) rung on the promotion ladder."""

    mode: str = "shadow"
    consecutive_good: int = 0


@dataclass(frozen=True, slots=True)
class TargetView:
    """The target fields policy rules may reference.

    Deliberately narrow. A rule that could reach arbitrary cluster state would be impossible to
    reason about and impossible to test hermetically.
    """

    kind: str = "Deployment"
    name: str = ""
    namespace: str | None = None
    replicas: int = 1
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Policy:
    version: int
    rules: tuple[Rule, ...]
    source: str

    @property
    def deny_rules(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.effect == "deny")

    @property
    def approval_rules(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.effect == "require_approval")


def load_policy(path: str | Path) -> Policy:
    """Parse and validate the policy file. Raises rather than degrading."""
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PolicyError(f"policy file not found: {p}") from exc
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy file is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict) or "rules" not in raw:
        raise PolicyError(f"policy file {p} has no `rules` key")

    rules: list[Rule] = []
    seen: set[str] = set()
    for entry in raw["rules"]:
        for required in ("id", "effect", "expr"):
            if required not in entry:
                raise PolicyError(f"policy rule missing `{required}`: {entry}")

        rule_id = str(entry["id"])
        if rule_id in seen:
            # Duplicate ids would make the eval's per-rule coverage requirement meaningless and
            # make a ledger entry ambiguous about which rule fired.
            raise PolicyError(f"duplicate policy rule id: {rule_id}")
        seen.add(rule_id)

        effect = str(entry["effect"])
        if effect not in VALID_EFFECTS:
            raise PolicyError(
                f"rule {rule_id} has effect {effect!r}; expected one of {VALID_EFFECTS}. "
                "There is no explicit `allow` rule -- allow is the absence of a match."
            )

        rules.append(
            Rule(
                id=rule_id,
                effect=effect,
                expr=str(entry["expr"]),
                message=str(entry.get("message", f"policy rule {rule_id} matched")).strip(),
            )
        )

    if not rules:
        raise PolicyError(f"policy file {p} defines no rules")

    return Policy(version=int(raw.get("version", 1)), rules=tuple(rules), source=str(p))


@lru_cache(maxsize=8)
def _compiled(expr: str) -> celpy.Runner:
    env = celpy.Environment()
    return env.program(env.compile(expr))


def _to_cel(value: Any) -> Any:
    """Convert Python values into CEL types.

    `celtypes` is strict about numeric kinds, and a Python bool is a subclass of int -- so bool must
    be checked before int or every boolean becomes 1.
    """
    if isinstance(value, bool):
        return celtypes.BoolType(value)
    if isinstance(value, int):
        return celtypes.IntType(value)
    if isinstance(value, float):
        return celtypes.DoubleType(value)
    if isinstance(value, str):
        return celtypes.StringType(value)
    if value is None:
        return None
    if isinstance(value, dict):
        return celtypes.MapType({_to_cel(k): _to_cel(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return celtypes.ListType([_to_cel(v) for v in value])
    return celtypes.StringType(str(value))


def _plan_view(plan: RemediationPlan) -> dict[str, Any]:
    """The plan fields rules may reference.

    `blast_radius_score` is flattened to a scalar because CEL cannot call Python methods, and a rule
    reading `plan.blast_radius.score()` would not work. Exposing the computed number keeps the rule
    readable.
    """
    return {
        "runbook": plan.runbook,
        "target_kind": plan.target_kind,
        "target_name": plan.target_name,
        "target_namespace": plan.target_namespace or "",
        "reversibility": str(plan.reversibility),
        "durability": str(plan.durability),
        "confidence": float(plan.confidence),
        "cost_delta_inr_month": float(plan.cost_delta_inr_month),
        "ttl_seconds": plan.ttl_seconds if plan.ttl_seconds is not None else 0,
        "gitops_managed": plan.gitops_managed,
        "is_refusal": plan.is_refusal,
        "action_count": len(plan.actions),
        "blast_radius_score": plan.blast_radius.score(),
        "blast_radius": {
            "pods": plan.blast_radius.pods,
            "nodes": plan.blast_radius.nodes,
            "namespaces": list(plan.blast_radius.namespaces),
            "user_facing": plan.blast_radius.user_facing,
        },
    }


#: The workload-level opt-out annotation, resolved here rather than indexed in CEL.
REMEDIATION_ANNOTATION = "healops.io/remediation"


def _target_view(target: TargetView) -> dict[str, Any]:
    """The target fields rules may reference.

    Note `remediation_disabled`: CEL raises on an absent map key, and this gate treats a raising
    rule as a deny. Indexing `annotations[...]` directly in a rule would therefore DENY every
    workload that simply has no annotations -- correct fail-closed behaviour applied to the wrong
    question. Resolving the opt-out to a boolean here removes the trap instead of asking every
    policy author to remember an `in` guard.
    """
    annotations = dict(target.annotations)
    return {
        "kind": target.kind,
        "name": target.name,
        "namespace": target.namespace or "",
        "replicas": int(target.replicas),
        "labels": dict(target.labels),
        "annotations": annotations,
        "remediation_disabled": annotations.get(REMEDIATION_ANNOTATION) == "disabled",
    }


def evaluate(
    plan: RemediationPlan,
    target: TargetView,
    budget: BudgetSnapshot | None = None,
    promotion: PromotionSnapshot | None = None,
    policy: Policy | None = None,
) -> PolicyDecision:
    """Run the gate. `first DENY wins, then any REQUIRE_APPROVAL, else ALLOW`."""
    active = policy or get_policy()
    budget = budget or BudgetSnapshot()
    promotion = promotion or PromotionSnapshot()

    context = {
        "plan": _to_cel(_plan_view(plan)),
        "target": _to_cel(_target_view(target)),
        "budget": _to_cel(
            {
                "scope": budget.scope,
                "used_actions": budget.used_actions,
                "max_actions": budget.max_actions,
                "breaker_state": budget.breaker_state,
            }
        ),
        "promotion": _to_cel(
            {"mode": promotion.mode, "consecutive_good": promotion.consecutive_good}
        ),
    }

    def matches(rule: Rule) -> bool:
        try:
            return bool(_compiled(rule.expr).evaluate(context))
        except Exception as exc:  # noqa: BLE001
            # Fail closed. A rule that cannot be evaluated is not evidence that the action is safe.
            log.error(
                "policy rule failed to evaluate; treating as a match to fail closed",
                rule=rule.id,
                expr=rule.expr,
                error=str(exc),
            )
            return True

    for rule in active.deny_rules:
        if matches(rule):
            return PolicyDecision(effect="deny", reasons=(rule.id,), messages=(rule.message,))

    fired = [r for r in active.approval_rules if matches(r)]
    if fired:
        return PolicyDecision(
            effect="require_approval",
            reasons=tuple(r.id for r in fired),
            messages=tuple(r.message for r in fired),
        )

    return PolicyDecision(effect="allow")


_policy: Policy | None = None


def get_policy() -> Policy:
    global _policy  # noqa: PLW0603
    if _policy is None:
        from config import get_settings

        _policy = load_policy(get_settings().policy_file)
        log.info(
            "policy loaded",
            source=_policy.source,
            version=_policy.version,
            deny_rules=len(_policy.deny_rules),
            approval_rules=len(_policy.approval_rules),
        )
    return _policy


def reset_policy_cache() -> None:
    """For tests and for `healopsctl` reloading a modified file."""
    global _policy  # noqa: PLW0603
    _policy = None
    _compiled.cache_clear()

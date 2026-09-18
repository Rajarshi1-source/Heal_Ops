"""The policy gate.

Two classes of test live here. The **ordering** tests pin the contract (`first DENY wins, then any
REQUIRE_APPROVAL, else ALLOW`). The **fail-closed** tests cover what happens when the policy itself
is broken, which is the case that decides whether the gate is a safety control or a suggestion.

Per-rule must-allow/must-deny coverage is enforced separately by `evals/policy_eval.py`, which
gates merges -- the policy file is code, so it gets tested like code.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

import policy as policy_module
from plan import ApiCall, BlastRadius, Durability, RemediationPlan, Reversibility, refuse
from policy import (
    BudgetSnapshot,
    Policy,
    PolicyError,
    PromotionSnapshot,
    TargetView,
    evaluate,
    load_policy,
)

POLICY_FILE = Path(__file__).resolve().parents[2] / "policy" / "remediation.yaml"


@pytest.fixture(scope="module")
def live_policy() -> Policy:
    return load_policy(POLICY_FILE)


def call() -> ApiCall:
    return ApiCall(verb="patch", resource="apps/v1/Deployment", name="api", namespace="shop")


def plan(**overrides: object) -> RemediationPlan:
    base: dict[str, object] = {
        "runbook": "crashloop_rollback",
        "incident_id": 1,
        "target_kind": "Deployment",
        "target_name": "api",
        "target_namespace": "shop",
        "actions": (call(),),
        "compensation": (call(),),
        "reversibility": Reversibility.REVERSIBLE,
        "durability": Durability.EPHEMERAL,
        "ttl_seconds": 7200,
        "blast_radius": BlastRadius(pods=3),
        "confidence": 0.94,
        "summary": "rollout undo",
    }
    return RemediationPlan(**(base | overrides))  # type: ignore[arg-type]


def target(**overrides: object) -> TargetView:
    base: dict[str, object] = {
        "kind": "Deployment",
        "name": "api",
        "namespace": "shop",
        "replicas": 3,
    }
    return TargetView(**(base | overrides))  # type: ignore[arg-type]


AUTO = PromotionSnapshot(mode="auto")


def decide(
    p: RemediationPlan | None = None,
    t: TargetView | None = None,
    budget: BudgetSnapshot | None = None,
    promotion: PromotionSnapshot | None = AUTO,
    pol: Policy | None = None,
) -> policy_module.PolicyDecision:
    return evaluate(p or plan(), t or target(), budget, promotion, pol)


# --- loading ------------------------------------------------------------------------------------


def test_the_shipped_policy_file_loads(live_policy: Policy) -> None:
    assert live_policy.version == 1
    assert live_policy.deny_rules
    assert live_policy.approval_rules


def test_every_rule_has_a_message(live_policy: Policy) -> None:
    # The message is used verbatim in Slack and in the ledger, so a rule without one produces an
    # audit entry that says nothing.
    for rule in live_policy.rules:
        assert rule.message, f"rule {rule.id} has no message"
        assert len(rule.message) > 20, f"rule {rule.id} has a uselessly terse message"


def test_missing_file_raises_rather_than_allowing(tmp_path: Path) -> None:
    """There is no implicit allow-everything fallback.

    The one time that path would run is the one time it must not.
    """
    with pytest.raises(PolicyError, match="not found"):
        load_policy(tmp_path / "nope.yaml")


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("rules: [unclosed", encoding="utf-8")
    with pytest.raises(PolicyError, match="not valid YAML"):
        load_policy(bad)


def test_duplicate_rule_ids_are_rejected(tmp_path: Path) -> None:
    # Duplicate ids would make a ledger entry ambiguous about which rule fired, and would defeat
    # the eval's per-rule coverage requirement.
    f = tmp_path / "dup.yaml"
    f.write_text(
        "version: 1\nrules:\n"
        "  - {id: same, effect: deny, expr: 'true', message: 'a message long enough'}\n"
        "  - {id: same, effect: deny, expr: 'false', message: 'another message here'}\n",
        encoding="utf-8",
    )
    with pytest.raises(PolicyError, match="duplicate policy rule id"):
        load_policy(f)


def test_unknown_effect_is_rejected(tmp_path: Path) -> None:
    f = tmp_path / "eff.yaml"
    f.write_text(
        "version: 1\nrules:\n"
        "  - {id: r, effect: allow, expr: 'true', message: 'allow is the absence of a match'}\n",
        encoding="utf-8",
    )
    with pytest.raises(PolicyError, match="expected one of"):
        load_policy(f)


def test_empty_rule_set_is_rejected(tmp_path: Path) -> None:
    f = tmp_path / "empty.yaml"
    f.write_text("version: 1\nrules: []\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="no rules"):
        load_policy(f)


# --- ordering -----------------------------------------------------------------------------------


def test_a_clean_plan_is_allowed(live_policy: Policy) -> None:
    assert decide(pol=live_policy).effect == "allow"


def test_deny_beats_require_approval(live_policy: Policy) -> None:
    """A StatefulSet with a huge blast radius matches both categories. Deny must win."""
    decision = decide(
        p=plan(
            target_kind="StatefulSet",
            target_namespace="chaos",
            blast_radius=BlastRadius(pods=40, nodes=3),
        ),
        t=target(kind="StatefulSet", namespace="chaos"),
        pol=live_policy,
    )
    assert decision.effect == "deny"
    assert decision.reasons == ("deny-protected-workloads",)


def test_multiple_approval_rules_are_all_reported(live_policy: Policy) -> None:
    # The Slack card should say every reason a human is being asked, not just the first.
    decision = decide(
        p=plan(confidence=0.4, cost_delta_inr_month=900.0),
        pol=live_policy,
    )
    assert decision.effect == "require_approval"
    assert "approve-low-confidence" in decision.reasons
    assert "approve-costly" in decision.reasons
    assert len(decision.messages) == len(decision.reasons)


# --- the deny rules -----------------------------------------------------------------------------


def test_healops_never_remediates_healops(live_policy: Policy) -> None:
    """The rule lives in the policy file, not in code. That is the point of policy-as-code."""
    decision = decide(
        p=plan(target_namespace="healops"),
        t=target(namespace="healops"),
        pol=live_policy,
    )
    assert decision.effect == "deny"
    assert decision.reasons == ("deny-self-remediation",)


def test_irreversible_is_always_denied(live_policy: Policy) -> None:
    decision = decide(
        p=plan(reversibility=Reversibility.IRREVERSIBLE, compensation=()),
        pol=live_policy,
    )
    assert decision.reasons == ("deny-irreversible",)


def test_namespace_allow_list_is_enforced(live_policy: Policy) -> None:
    decision = decide(
        p=plan(target_namespace="kube-system"),
        t=target(namespace="kube-system"),
        pol=live_policy,
    )
    assert decision.reasons == ("deny-outside-allowed-namespaces",)


def test_opt_out_annotation_denies(live_policy: Policy) -> None:
    decision = decide(t=target(annotations={"healops.io/remediation": "disabled"}), pol=live_policy)
    assert decision.reasons == ("deny-kill-switch",)


def test_workload_without_annotations_is_not_denied(live_policy: Policy) -> None:
    """The regression this file exists for.

    CEL raises on an absent map key and a raising rule is treated as a deny, so indexing
    `annotations[...]` in the rule denied every workload that simply had none. The opt-out is
    resolved to a boolean instead.
    """
    assert decide(t=target(annotations={}), pol=live_policy).effect == "allow"


def test_single_replica_blocks_a_disruptive_fix(live_policy: Policy) -> None:
    decision = decide(
        p=plan(reversibility=Reversibility.PARTIAL),
        t=target(replicas=1),
        pol=live_policy,
    )
    assert decision.reasons == ("deny-single-replica-disruptive",)


def test_single_replica_still_allows_a_reversible_fix(live_policy: Policy) -> None:
    # Resizing or scaling up a single-replica workload does not take it down, so it is permitted.
    assert decide(t=target(replicas=1), pol=live_policy).effect == "allow"


def test_exhausted_budget_denies(live_policy: Policy) -> None:
    """An error budget for automation: exhaustion escalates rather than queues."""
    decision = decide(budget=BudgetSnapshot(used_actions=20, max_actions=20), pol=live_policy)
    assert decision.reasons == ("deny-budget-exhausted",)


def test_open_breaker_denies(live_policy: Policy) -> None:
    decision = decide(budget=BudgetSnapshot(breaker_state="open"), pol=live_policy)
    assert decision.reasons == ("deny-breaker-open",)


# --- the approval rules --------------------------------------------------------------------------


def test_unpromoted_runbook_asks_first(live_policy: Policy) -> None:
    """The ladder becomes enforcement here rather than bookkeeping."""
    decision = decide(promotion=PromotionSnapshot(mode="shadow"), pol=live_policy)
    assert decision.effect == "require_approval"
    assert "approve-not-promoted" in decision.reasons


def test_node_actions_always_require_approval(live_policy: Policy) -> None:
    # Cordon is reversible; the rescheduling it causes is not.
    decision = decide(
        p=plan(
            target_kind="Node",
            target_name="worker-1",
            target_namespace="default",
            reversibility=Reversibility.PARTIAL,
        ),
        t=target(kind="Node", name="worker-1", namespace="default", replicas=3),
        pol=live_policy,
    )
    assert decision.effect == "require_approval"
    assert "approve-node-scoped" in decision.reasons


def test_large_blast_radius_requires_approval(live_policy: Policy) -> None:
    decision = decide(p=plan(blast_radius=BlastRadius(pods=6, nodes=2)), pol=live_policy)
    assert "approve-large-blast-radius" in decision.reasons


def test_blast_radius_just_under_the_threshold_is_allowed(live_policy: Policy) -> None:
    # 25 is the boundary and the rule is `> 25`, so exactly 25 must pass.
    assert BlastRadius(pods=25).score() == 25
    assert decide(p=plan(blast_radius=BlastRadius(pods=25)), pol=live_policy).effect == "allow"


def test_durable_change_requires_approval(live_policy: Policy) -> None:
    """The ephemeral mitigation can be automatic; changing what git says should not be."""
    decision = decide(p=plan(durability=Durability.DURABLE, ttl_seconds=None), pol=live_policy)
    assert "approve-durable-change" in decision.reasons


def test_half_open_breaker_requires_approval(live_policy: Policy) -> None:
    decision = decide(budget=BudgetSnapshot(breaker_state="half_open"), pol=live_policy)
    assert "approve-breaker-half-open" in decision.reasons


def test_nearly_exhausted_budget_requires_approval(live_policy: Policy) -> None:
    decision = decide(budget=BudgetSnapshot(used_actions=8, max_actions=10), pol=live_policy)
    assert "approve-budget-nearly-exhausted" in decision.reasons


def test_confidence_boundary_is_inclusive_at_080(live_policy: Policy) -> None:
    # The rule is `< 0.80`, so exactly 0.80 must be automatic.
    assert decide(p=plan(confidence=0.80), pol=live_policy).effect == "allow"
    assert decide(p=plan(confidence=0.79), pol=live_policy).effect == "require_approval"


# --- fail closed --------------------------------------------------------------------------------


def test_a_rule_that_raises_is_treated_as_a_deny(tmp_path: Path) -> None:
    """A rule that cannot be evaluated is not evidence that the action is safe."""
    f = tmp_path / "broken.yaml"
    f.write_text(
        "version: 1\nrules:\n"
        "  - id: broken\n"
        "    effect: deny\n"
        "    expr: 'plan.no_such_field == \"x\"'\n"
        "    message: 'this rule references a field that does not exist'\n",
        encoding="utf-8",
    )
    decision = decide(pol=load_policy(f))
    assert decision.effect == "deny"
    assert decision.reasons == ("broken",)


def test_refusals_still_pass_through_the_gate(live_policy: Policy) -> None:
    """A refusal is audited like an action, so it is evaluated rather than short-circuited.

    The orchestrator returns early on a refusal, but the gate must not crash if handed one.
    """
    r = refuse("crashloop_rollback", 1, "no previous revision to roll back to")
    decision = evaluate(
        dataclasses.replace(r, target_namespace="shop"), target(), None, AUTO, live_policy
    )
    assert decision.effect in ("allow", "require_approval", "deny")


# --- decision shape -----------------------------------------------------------------------------


def test_decision_exposes_readable_predicates(live_policy: Policy) -> None:
    allowed = decide(pol=live_policy)
    assert allowed.allowed and not allowed.needs_approval and not allowed.denied

    denied = decide(
        p=plan(target_namespace="kube-system"), t=target(namespace="kube-system"), pol=live_policy
    )
    assert denied.denied and not denied.allowed


def test_decision_serialises_for_the_ledger(live_policy: Policy) -> None:
    payload = decide(promotion=PromotionSnapshot(mode="shadow"), pol=live_policy).to_dict()
    assert payload["decision"] == "require_approval"
    assert "approve-not-promoted" in payload["reasons"]
    assert payload["messages"]

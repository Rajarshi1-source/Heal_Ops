"""Policy gate: every rule in the policy file has a must-allow and a must-deny case, and both hold.

The policy file is the guardrail, and adding a guardrail is a pull request rather than a code
change -- which only stays safe if the pull request also says what the new rule blocks and what it
deliberately lets through. This gate makes that a merge requirement:

* every rule in `policy/remediation.yaml` has at least one `must_deny` and one `must_allow` case in
  `evals/policy_cases.yaml`;
* in each `must_deny` case the rule fires, and the whole policy does not allow the plan;
* in each `must_allow` case the rule does not fire;
* the baseline fires no rule, so every case is measured against a known-allowed plan;
* no case names a rule that does not exist (a renamed rule cannot leave its tests behind).

Rules are evaluated individually, not only through first-deny-wins: a rule hidden behind an earlier
deny would otherwise never be tested at all.

    python -m evals.policy_eval
"""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from evals._paths import ENGINE_SRC, POLICY_FILE, REPO_ROOT

sys.path.insert(0, str(ENGINE_SRC))

import policy as policy_module  # noqa: E402
from plan import ApiCall, BlastRadius, Durability, RemediationPlan, Reversibility  # noqa: E402
from policy import BudgetSnapshot, PromotionSnapshot, TargetView  # noqa: E402

CASES = REPO_ROOT / "evals" / "policy_cases.yaml"


@dataclass(frozen=True, slots=True)
class Result:
    rule: str
    kind: str
    index: int
    ok: bool
    detail: str


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict) and key != "blast_radius":
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def build(
    case: dict[str, Any],
) -> tuple[RemediationPlan, TargetView, BudgetSnapshot, PromotionSnapshot]:
    p, t, b, m = case["plan"], case["target"], case["budget"], case["promotion"]
    call = ApiCall(
        verb="patch",
        resource=f"apps/v1/{p['target_kind']}",
        name=p["target_name"],
        namespace=p.get("target_namespace"),
    )
    radius = p["blast_radius"]
    plan = RemediationPlan(
        runbook=p["runbook"],
        incident_id="policy-eval",
        target_kind=p["target_kind"],
        target_name=p["target_name"],
        target_namespace=p.get("target_namespace"),
        actions=(call,),
        compensation=(call,),
        reversibility=Reversibility(p["reversibility"]),
        durability=Durability(p["durability"]),
        ttl_seconds=p.get("ttl_seconds"),
        blast_radius=BlastRadius(
            pods=radius["pods"],
            nodes=radius["nodes"],
            namespaces=tuple(radius["namespaces"]),
            user_facing=radius["user_facing"],
        ),
        confidence=float(p["confidence"]),
        cost_delta_inr_month=float(p["cost_delta_inr_month"]),
    )
    target = TargetView(
        kind=t["kind"],
        name=t["name"],
        namespace=t.get("namespace"),
        replicas=int(t["replicas"]),
        annotations=dict(t.get("annotations") or {}),
    )
    budget = BudgetSnapshot(
        used_actions=int(b["used_actions"]),
        max_actions=int(b["max_actions"]),
        breaker_state=str(b["breaker_state"]),
    )
    return plan, target, budget, PromotionSnapshot(mode=str(m["mode"]))


def run(
    policy_path: Path = POLICY_FILE, cases_path: Path = CASES
) -> tuple[list[Result], list[str]]:
    policy = policy_module.load_policy(policy_path)
    spec = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    baseline: dict[str, Any] = spec["baseline"]
    rules_spec: dict[str, Any] = spec.get("rules") or {}
    problems: list[str] = []
    results: list[Result] = []

    fired = policy_module.matching_rules(*build(baseline), policy)
    if fired:
        problems.append(f"the baseline must fire no rule, but fired: {fired}")

    known = {r.id: r for r in policy.rules}
    for rule_id in sorted(set(rules_spec) - set(known)):
        problems.append(f"cases name a rule that is not in the policy file: {rule_id}")

    for rule in policy.rules:
        cases = rules_spec.get(rule.id) or {}
        for kind in ("must_deny", "must_allow"):
            if not cases.get(kind):
                problems.append(f"rule {rule.id} has no {kind} case")
        for kind in ("must_deny", "must_allow"):
            for index, overrides in enumerate(cases.get(kind) or []):
                inputs = build(_merge(baseline, overrides))
                fires = rule.id in policy_module.matching_rules(*inputs, policy)
                effect = policy_module.evaluate(*inputs, policy).effect
                if kind == "must_deny":
                    ok = fires and effect != "allow"
                    detail = f"rule fired={fires}, policy={effect}"
                else:
                    ok = not fires
                    detail = f"rule fired={fires}"
                results.append(Result(rule.id, kind, index, ok, detail))

    return results, problems


def main() -> int:
    results, problems = run()
    for r in results:
        print(f"{'ok  ' if r.ok else 'FAIL'}  {r.rule:<34} {r.kind:<10} #{r.index}  {r.detail}")
    failed = [r for r in results if not r.ok]
    rules = {r.rule for r in results}
    print(f"\n{len(rules)} rules, {len(results)} cases, {len(failed)} failed")
    for problem in problems:
        print(f"  - {problem}")
    passed = not failed and not problems
    print("PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

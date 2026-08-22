---
name: healops-mlops-promotion
description: "Build the HealOps MLOps wrapper: how automation earns authority and how its confidence is graded. Use for ANY work on the promotion ladder (shadow to approval to auto, per runbook per namespace, instant demotion), shadow-mode recording and plan diffing against human actions, the was-this-correct feedback loop that actually changes behaviour, calibrated confidence, the chaos-generated labelled incident corpus, the decision eval gating CI on accuracy, Brier score, calibration error, determinism digest and refusal recall, drift monitoring via agreement rate, and the RUNBOOKS.md model card. Trigger on promotion ladder, shadow mode, demotion, was this correct, calibrated confidence, Brier score, ECE, decision eval, incident corpus, determinism digest, refusal recall, or model card. MANDATE: the decision layer is deliberately deterministic rules, never a model, so it can be unit-tested, replayed, and hashed. Any LLM writes prose only and never alters a plan, a policy decision, or a verdict."
---

# HealOps MLOps Wrapper — Promotion and Calibration

Two things live here. **The promotion ladder** answers the hardest question an interviewer will ask: *how do you know your automation is safe enough to run unattended?* **The calibration gate** makes the confidence number on every plan mean something.

The central claim to defend: the decision layer is **deliberately deterministic** — a rules engine, not a model — but it gets the **full MLOps treatment**, and that contrast is the point.

## The ladder

Every `(runbook, namespace)` pair sits on three rungs and climbs or falls on evidence.

```
SHADOW              runbook produces plans for real incidents but CANNOT act.
                    Plans are recorded and diffed against what the human did.
   |  promote when: >= 10 shadow plans AND >= 90% match the human action
   v                AND zero plans that policy would have denied
APPROVAL            runbook acts only after a human clicks Approve in Slack.
   |  promote when: >= 20 consecutive good outcomes in this namespace
   v                (verified remediated, no revert, no "was this correct? -> No")
AUTO                runbook acts within policy, no human in the loop.
      demote when:  ANY of -> verification failed and compensation ran
                            -> a human answered "No" to "was this correct?"
                            -> the masking guard tripped
                            -> the runbook's code or policy changed (back to APPROVAL)
```

```python
# engine/src/promotion.py
SHADOW_MIN_PLANS = 10
SHADOW_MIN_AGREEMENT = 0.90
APPROVAL_MIN_CONSECUTIVE_GOOD = 20

def on_outcome(runbook: str, namespace: str, outcome: str, human_feedback: str | None) -> None:
    st = db.promotion(runbook, namespace)

    bad = outcome in ("not_recovered", "reverted", "revert_failed") or human_feedback == "no"
    if bad:
        db.set_promotion(runbook, namespace, mode="approval", consecutive_good=0,
                         reason=f"demoted: outcome={outcome} feedback={human_feedback}")
        slack.post_demotion(runbook, namespace, outcome, human_feedback)
        ledger.append("promote", payload={"runbook": runbook, "namespace": namespace,
                                          "to": "approval", "reason": outcome})
        return

    good = st.consecutive_good + 1
    if st.mode == "approval" and good >= APPROVAL_MIN_CONSECUTIVE_GOOD:
        db.set_promotion(runbook, namespace, mode="auto", consecutive_good=good,
                         reason=f"promoted after {good} consecutive verified remediations")
        slack.post_promotion(runbook, namespace, good)
        ledger.append("promote", payload={"runbook": runbook, "namespace": namespace,
                                          "to": "auto", "evidence": good})
    else:
        db.bump_consecutive_good(runbook, namespace, good)
```

Design properties to preserve when editing:

- **Demotion is instant and one-strike.** A single bad outcome drops the rung. Promotion takes twenty; demotion takes one. That asymmetry is the safety property.
- **Authority is per-namespace.** A runbook can be fully automatic in `shop` and still approval-only in `payments`. State that explicitly — it's what makes the blast radius argument concrete.
- **Any code or policy change resets to approval.** Shipping a new engine version must never silently grant a runbook more power. Promotion of the *software* is `git push → CI → Helm`; promotion of the *automation authority* is this ladder. Two different pipelines, deliberately.
- **The policy gate reads promotion state.** The `approve-not-promoted` rule (`promotion.mode != "auto"` → require approval) is how the ladder becomes enforcement rather than bookkeeping.
- **Every transition is a ledger entry**, so an auditor can see when a bot was granted authority and on what evidence.

### "Was this correct?" must do something

Rev 1 sent the button and logged the answer. That is a survey, not a feedback loop. The answer must feed `on_outcome`, and a `No` must demote — otherwise the button is theatre and an interviewer will notice.

### Shadow mode is also the demo

> *"Here's runbook 6, which I wrote last week. It's in shadow mode. Watch — I trigger the fault, and HealOps posts to Slack: 'SHADOW: I would have patched the readiness probe timeout 1s → 3s on checkout-api. Blast radius 1 deployment, reversible.' It didn't touch anything. Here's the shadow log next to what I actually did: 8 of 9 agreements. It needs one more before I'll let it ask for approval."*

Shadow plans are recorded in `runbook_executions` with `mode='shadow'` and no execution. The **agreement rate** between shadow plans and human actions is exported to Prometheus — that is the drift monitor.

### The strong answer to the hardest question

> *"I don't decide that up front — I earn it per namespace. A new runbook starts in shadow mode: it writes the plan it would have executed, and I compare it to what the on-call actually did. When it agrees with humans 90% of the time over at least ten incidents, it graduates to approval mode. After twenty consecutive verified-good outcomes in that namespace, it graduates to auto. One bad outcome demotes it immediately, and any change to its code or policy sends it back to approval. It's progressive delivery, applied to automation itself."*

The weak answer, for contrast — do not let this creep back into the README: *"I only automated the safe runbooks."*

## Calibrated confidence

Every plan carries a `confidence`. That number is worthless unless it is **measured**, so grade it the way a forecast is graded.

| Metric | Gate | Meaning |
|---|---|---|
| Top-1 accuracy | ≥ 0.90 | The chosen runbook matches the labelled correct action |
| **Brier score** | ≤ 0.08 | Confidence numbers track reality |
| **Expected Calibration Error (ECE)** | ≤ 0.10 | When it says 0.9, it's right about 90% of the time |
| Determinism digest | exact match | Same incident bundle → byte-identical plan |
| Refusal recall | 100% of must-refuse cases | Denials and refusals are graded like verdicts |

```python
# evals/decision_eval.py — a CI gate, not a unit test
"""
Replays a labelled corpus of incident bundles through the planner and grades DECISIONS:
which runbook, auto vs approval vs refuse, and the confidence calibration.

Hermetic: bundles are committed JSON, so CI needs no cluster.
"""
TARGET_ACCURACY, MAX_BRIER, MAX_ECE = 0.90, 0.08, 0.10

for case in corpus:
    plan = planner.plan_for(case["bundle"])          # pure function of the bundle
    ok = (plan.runbook == case["expected_runbook"]
          and policy.evaluate(plan, ...).effect == case["expected_effect"])
    pairs.append((plan.confidence, ok))
    digests.append(sha256_canonical(plan.to_dict()))

assert accuracy(pairs) >= TARGET_ACCURACY
assert brier(pairs) <= MAX_BRIER and ece(pairs) <= MAX_ECE
assert digests == frozen_digests[PLANNER_VERSION]     # determinism anchor
```

Two properties that make this real rather than decorative:

**`planner.plan_for(bundle)` must be a pure function of the bundle.** If planning reaches out to a live cluster, the eval can't be hermetic, the digest can't be stable, and the gate becomes flaky. Keep cluster reads behind the target object that the bundle carries.

**The determinism digest is a frozen artefact keyed by `PLANNER_VERSION`.** When a deliberate logic change moves the digests, the reviewer updates the frozen set *in the same PR* — which makes every decision-affecting change visible in review. That is the whole point.

## The corpus is generated, not hand-written

The chaos harness drives a `kind` cluster, induces each fault, captures the resulting alert payloads plus cluster state as a bundle, and labels it with the correct action.

- Target **at least 25 cases** across the five runbooks.
- Include **at least four must-refuse cases**: a StatefulSet OOMKill (policy denies), a single-replica CrashLoop (no safe blast radius), a node alert during a planned drain (freeze active), and an alert on an annotated opt-out workload.
- Commit the bundles as JSON. Hermetic corpus, no cluster in CI for this gate.

**A remediation engine that never refuses is a liability**, which is why refusal recall is graded at 100% rather than as an average.

## The MLOps mapping — say this table out loud

| MLOps practice | HealOps equivalent |
|---|---|
| Versioned model artefact | `PLANNER_VERSION` + policy file version, recorded on every plan |
| Labelled dataset | Chaos-generated incident corpus, committed to the repo |
| Offline evaluation gating CI | `decision_eval.py` — accuracy, calibration, determinism, refusal recall |
| Shadow deployment | Shadow mode per (runbook, namespace) |
| Progressive rollout | The promotion ladder |
| Drift monitoring | Agreement rate between plans and human actions, exported to Prometheus |
| Rollback | Demotion, plus `healopsctl freeze` |
| Model card | `RUNBOOKS.md` — each runbook's trigger, action, undo, blast radius, refusals, and current promotion state per namespace |

> *"I did use an LLM, but only to write postmortems from the evidence bundle — never to decide anything. The decision engine is deterministic rules, because I need to unit-test it, replay it, and hash it. What I borrowed from ML engineering is the discipline: a labelled corpus, an offline eval that gates CI on accuracy **and calibration**, shadow deployment, and progressive rollout. Those practices are more valuable here than a model would be."*

## The optional LLM layer and its hard boundary

```python
# engine/src/narrative.py
class Narrator:                                   # provider-agnostic; model id from config
    """Writes the postmortem. NEVER produces or alters a plan, a policy decision, or a verdict."""

class TemplateNarrator(Narrator):                 # DEFAULT: deterministic, offline, free
    ...

class LLMNarrator(Narrator):
    """Given the ledger entries for an incident, drafts a timeline + contributing factors +
    action items. Output is DISCARDED if it references any action not present in the ledger."""
```

Four rules:

1. **`TemplateNarrator` is the default.** The system works with no API key and no network.
2. **Provider-agnostic adapter, model id from config.** Never hardcode a vendor or a model name in the call site.
3. **Grounding check is not optional:** if the draft references an action absent from the ledger, discard it. A hallucinated postmortem is worse than a templated one.
4. **The boundary is stated before anyone asks.** In 2026 "AI SRE agent" products are everywhere, and *"my automation is deterministic and my writing is AI-assisted"* is the more defensible position.

## CI wiring

Three separate gate jobs, so a failure names its own cause:

```yaml
decision-gate:            # accuracy + calibration + determinism + refusal recall
  - run: python -m evals.decision_eval
  - run: python -m evals.ledger_verify      # hash chain over the committed corpus
policy-gate:              # every policy rule has a must-allow and a must-deny case
  - run: python -m evals.policy_eval
```

The chaos job that generates and refreshes the corpus lives in `healops-kubernetes-platform`; these gates only consume committed bundles.

## Build-order note

If time is short, ship **the ladder before the calibration gate** — the ladder answers the hardest interview question and takes about three days; calibration is another three and pays off mainly in AI/ML-flavoured panels. The intermediate position is the corpus plus the accuracy gate, leaving Brier and ECE for later. Do not ship the corpus without the accuracy gate: an ungated corpus is a folder of JSON nobody trusts.

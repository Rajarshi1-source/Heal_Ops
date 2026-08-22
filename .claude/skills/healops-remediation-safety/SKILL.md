---
name: healops-remediation-safety
description: "Build and maintain the HealOps safety pipeline, the differentiator versus trigger-then-script remediation tools. Use for ANY decision-path work: the RemediationPlan object, blast-radius scoring, reversibility and durability classes, the CEL policy gate in git, the five-method runbook contract (plan, execute, compensate, extra_verification, chaos_fixture), the orchestrator, closed-loop verification with PromQL golden signals, registered compensation and the watchdog, the efficacy scorecard, cause-first correlation, the remediation budget, the circuit breaker, the anti-masking guard, and the three chaos tests per runbook. Trigger on RemediationPlan, blast radius, reversibility, policy gate, CEL, runbook contract, compensate, saga, closed-loop verification, throttle ratio, PSI, anti-masking, or refusal. MANDATE: correlate, PLAN, gate, act, VERIFY, compensate. Success means the incident is over, never that an API call returned 200. A runbook that cannot describe its own undo is not registered."
---

# HealOps Remediation Safety Pipeline

Every existing auto-remediation tool is `trigger → action`: match an alert, run a script. HealOps inserts stages that change the whole safety story:

```
alert -> correlate -> PLAN -> policy gate -> [shadow | approval | execute] -> VERIFY -> compensate?
```

That sequence is the project. When in doubt about where code belongs, ask which stage it serves.

## Non-negotiable invariants

1. **Success means the incident is over.** Not that the Kubernetes API returned 200. A `rollout undo` onto an equally broken revision is a **failure** and must be recorded as one.
2. **A runbook that cannot describe its own undo is not registered.** `compensate()` is abstract for a reason.
3. **Compensation is persisted before the action runs**, never derived afterwards.
4. **Refusals and denials are outcomes worth auditing.** "The system correctly decided not to act" is a data point the scorecard needs.
5. **The auto-vs-approval split is computed from plan properties by policy**, not hardcoded per runbook.
6. **A missing signal is never a healthy signal.** Unavailable metrics produce `inconclusive` and escalate.

## The plan object

`engine/src/plan.py`. A plan is **data, not code** — which is what makes it reviewable, shadow-executable, and replayable.

```python
class Reversibility(StrEnum):
    REVERSIBLE   = "reversible"              # exact prior state restorable (scale, rollout undo)
    PARTIAL      = "partially_reversible"    # side effects persist (logs in S3, pods rescheduled)
    IRREVERSIBLE = "irreversible"            # always denied by policy

class Durability(StrEnum):
    EPHEMERAL = "ephemeral"                  # imperative, TTL'd, expires
    DURABLE   = "durable"                    # must land in git as a PR

@dataclass(frozen=True)
class ApiCall:
    verb: str                                # patch | scale | evict | cordon | resize | s3_put
    resource: str                            # apps/v1/Deployment
    namespace: Optional[str]
    name: str
    body: dict[str, Any] = field(default_factory=dict)
    subresource: Optional[str] = None        # e.g. "resize"

@dataclass(frozen=True)
class ExpectedSignal:
    alert_should_clear: str                  # alertname
    queries: dict[str, str]                  # name -> PromQL
    thresholds: dict[str, float]             # name -> max acceptable value
    window_seconds: int = 300

@dataclass(frozen=True)
class BlastRadius:
    pods: int
    nodes: int
    namespaces: list[str]
    user_facing: bool

    def score(self) -> int:
        """Cheap ordinal for policy rules; deliberately simple and explainable."""
        return self.pods + 10 * self.nodes + (20 if self.user_facing else 0)
```

`RemediationPlan` is frozen and carries: `runbook`, `incident_id`, target triple, `actions`, `compensation`, `reversibility`, `durability`, `blast_radius`, `expected_signal`, `confidence`, `summary` (one line for Slack), `cost_delta_inr_month`, `ttl_seconds` (**required when `durability == EPHEMERAL`**), and `refusal_reason`.

**A refusal is a first-class plan.** `refuse(runbook, incident_id, reason)` returns a plan with `refusal_reason` set and no actions. It is audited exactly like an action. Keep `is_refusal` as a property so callers can't forget to check.

The blast-radius score stays arithmetic on purpose — a policy rule that a reviewer can evaluate in their head is worth more than a clever weighting nobody trusts.

## The policy gate — CEL, in git

`policy/remediation.yaml`, evaluated with `cel-python` in-process. Rules are ordered: **first DENY wins, then any REQUIRE_APPROVAL, else ALLOW.**

```yaml
version: 1
rules:
  - id: deny-irreversible
    effect: deny
    expr: 'plan.reversibility == "irreversible"'
  - id: deny-outside-allowed-namespaces
    effect: deny
    expr: '!(plan.target_namespace in ["shop","payments","default","chaos"])'
  - id: deny-protected-workloads
    effect: deny
    expr: 'plan.target_name.startsWith("db-") || plan.target_kind == "StatefulSet"'
  - id: deny-kill-switch
    effect: deny
    expr: 'target.annotations["healops.io/remediation"] == "disabled"'
  - id: deny-single-replica-disruptive
    effect: deny
    expr: 'target.replicas < 2 && plan.reversibility != "reversible"'
  - id: deny-budget-exhausted
    effect: deny
    expr: 'budget.breaker_state == "open" || budget.used_actions >= budget.max_actions'
  - id: approve-large-blast-radius
    effect: require_approval
    expr: 'plan.blast_radius.score() > 25'
  - id: approve-costly
    effect: require_approval
    expr: 'plan.cost_delta_inr_month > 500.0'
  - id: approve-low-confidence
    effect: require_approval
    expr: 'plan.confidence < 0.80'
  - id: approve-not-promoted
    effect: require_approval
    expr: 'promotion.mode != "auto"'
```

Every rule carries a `message` used verbatim in Slack and the ledger. Two properties to state out loud: adding a guardrail is **a pull request, not a code change**, and the policy file is tested — `evals/policy_eval.py` requires **every rule to have a must-allow and a must-deny case** and it gates merges.

Optionally mirror the gate in-cluster with a `ValidatingAdmissionPolicy` (CEL, in-tree, GA since 1.30) as a second enforcement point. Keep `cel-python` as the primary so a demo cluster needs no extra service.

## The runbook contract

`engine/src/runbooks/base.py`. Five members. **The framework, not the runbook, decides whether to execute.**

```python
class Runbook(abc.ABC):
    name: str
    triggers: tuple[str, ...]                 # alertnames this runbook answers

    @abc.abstractmethod
    def plan(self, incident, target) -> RemediationPlan: ...
    # Describe intent. Reads cluster state, mutates nothing.

    @abc.abstractmethod
    def execute(self, plan: RemediationPlan) -> ExecutionResult: ...
    # Apply the plan's actions. Called ONLY after the policy gate and any approval.

    @abc.abstractmethod
    def compensate(self, plan: RemediationPlan) -> ExecutionResult: ...
    # Undo. Must be idempotent — the watchdog may call it after execute() already did.

    def extra_verification(self, plan, after: SignalWindow) -> Optional[str]:
        return None
    # Optional runbook-specific probe on top of the generic signal check.

    @property
    @abc.abstractmethod
    def chaos_fixture(self) -> str: ...
    # Name of the fault injector that proves this runbook. Enforced in CI.
```

`ExecutionResult` carries `ok`, `detail`, `state_before`, `state_after` — the before/after snapshots are what the dashboard diffs and what the compensation test compares byte-for-byte.

### The orchestrator

Everything flows through `engine/src/orchestrator.py`, which is what makes safety **structural** rather than a matter of each runbook remembering:

```python
def handle(incident, runbook: Runbook, target) -> None:
    ledger.append("plan_requested", incident_id=incident.id, payload={"runbook": runbook.name})

    plan = runbook.plan(incident, target)
    ledger.append("plan", incident_id=incident.id, payload=plan.to_dict())
    if plan.is_refusal:
        slack.post_refusal(plan); return

    decision = policy.evaluate(plan, target, budget.snapshot(plan.target_namespace),
                               promotion.get(runbook.name, plan.target_namespace))
    ledger.append("policy", incident_id=incident.id,
                  payload={"decision": decision.effect, "reasons": decision.reasons})
    if decision.effect == "deny":
        slack.post_denied(plan, decision); return

    mode = promotion.get(runbook.name, plan.target_namespace).mode
    if mode == "shadow":                                  # record, do not act
        store.record_shadow(plan, decision); slack.post_shadow(plan); return

    if decision.effect == "require_approval":
        approval = approvals.request(plan, decision)      # Postgres source of truth
        if not approval.granted:
            slack.post_escalation(plan, approval); return

    # --- the only mutating window in the whole system ---
    with locks.per_resource(plan.target_kind, plan.target_namespace, plan.target_name):
        before = signals.window(plan, seconds=plan.expected_signal.window_seconds)
        store.register_compensation(plan)                 # BEFORE acting (saga)
        result = runbook.execute(plan)
        ledger.append("execute", incident_id=incident.id, payload=result.__dict__)

    verifier.schedule(plan, result, after=plan.expected_signal.window_seconds)
    budget.consume(plan.target_namespace)
    recurrence.record(runbook.name, plan)                 # anti-masking
```

Note that **every branch is audited, including the two that do nothing.** Preserve that when editing.

## Closed-loop verification

`REMEDIATED` requires all three: the triggering alert **cleared**, the workload's **golden signals recovered**, and **no new alert** appeared on the same target that the action could have caused. Otherwise compensate and escalate. Signals unavailable → `inconclusive` and escalate. Never a silent success.

```python
THROTTLE_RATIO_CEILING = 0.05     # 5% of CFS periods throttled
PSI_FULL_CEILING       = 0.05     # 5% of wall time fully stalled
RESTART_TOLERANCE      = 0
```

The verifier checks, in order: Alertmanager reachability (None → `inconclusive`), alert still firing, new OOM kills, restarts beyond tolerance, throttle ratio, PSI full-stall, and `after.new_alerts`. If any tracked signal is `None` and nothing else is wrong, the verdict is `inconclusive` — **absence of evidence is never evidence of recovery.**

### PromQL that must not be simplified

```promql
# Throttle RATIO — dimensionless. Raw throttled-period counters move with replica
# count and window length, which makes a raw comparison meaningless.
sum by (namespace,pod,container) (rate(container_cpu_cfs_throttled_periods_total{...}[5m]))
/
sum by (namespace,pod,container) (rate(container_cpu_cfs_periods_total{...}[5m]))

# OOM by COUNTER, not the flapping last_terminated_reason gauge
sum by (namespace,pod,container) (increase(container_oom_events_total{...}[10m]))

# PSI: share of wall time fully stalled (beta 1.34, GA 1.36)
sum by (namespace,pod,container) (rate(container_pressure_memory_stalled_seconds_total{...}[5m]))
```

Alert state comes from **Alertmanager API v2** (`GET /api/v2/alerts?filter=...`); v1 was removed in 0.27.

### Why this beats "the alert stopped firing"

An interviewer who has run production will ask exactly that. Three reasons, worth keeping as comments in `verifier.py`: alerts clear for the wrong reasons (pod deleted, scaled to zero, metric went stale); `for:` durations lag recovery *and* relapse by minutes; and the action can cause a *different* problem — scaling up fixes CPU and starts an OOM cascade on a memory-tight node, which only a whole-target signal check catches.

## Compensation — two paths, because one is not enough

```python
# compensator.py — path 1: verification decided it didn't work
def on_verification(plan, verdict) -> None:
    if verdict.outcome == "remediated":
        store.close(plan, "remediated")
        if plan.durability == "durable":
            git.open_pr(plan)                  # see healops-gitops-remediation
        slack.post_success(plan, verdict); return
    result = runbooks.get(plan.runbook).compensate(plan)
    ledger.append("compensate", payload={"plan": plan.to_dict(), "result": result.__dict__})
    store.close(plan, "reverted" if result.ok else "revert_failed")
    slack.post_escalation(plan, verdict, result)          # @oncall, evidence + bundle hash
```

```python
# watchdog.py — path 2: CronJob every minute. A compensation owned by the
# process that can crash is not a compensation.
def sweep() -> int:
    for row in store.executions_awaiting_verification(older_than_seconds=900):
        compensator.force_revert(row, reason="verification never completed")
    for row in store.expired_ephemeral_fixes():           # expires_at < now()
        compensator.expire(row)
    # -> healops_watchdog_actions_total (alert if > 0)
```

This is the **saga pattern with a persisted compensating transaction**, and naming it that way is worth the two hours it takes to build.

## The efficacy scorecard

Replace any hand-waved "3 of 5" with measured metrics that have honest denominators.

| Metric | Definition |
|---|---|
| Auto-resolution rate | `remediated_without_human / total_incidents` — human-approved fixes excluded |
| Assisted-resolution rate | `remediated_after_approval / total_incidents` |
| Escalation rate | `escalated / total_incidents` — a **good** number, the system admitting "not mine" |
| **False-remediation rate** | `reverted_after_verification / executed` — **the trust metric nobody else reports** |
| MTTR (auto) | median(`remediated_at − alert_started_at`) on the auto path |
| Time-to-detect-failure | median(`verification_at − executed_at`) |
| Refusal rate | `refused_by_policy / plans_created` — proves guardrails are load-bearing |

> Demo line: *"3 of 5 auto-resolved, 2 escalated after approval, zero false remediations — and the false-remediation rate is a metric on my Grafana dashboard, not a claim. The system also reverted itself once during testing, in 90 seconds, which is the number I'm proudest of."*

## Correlation, budget, breaker, anti-masking

### Cause-first correlation
Group by **topology and causality**, never by fingerprint. Rules in order: node-level cause dominates pod symptoms on that node; deployment-level cause dominates its pods' symptoms; same target inside a 300 s window joins the same incident; everything else is new. `CAUSE_PRECEDENCE` puts `KubeNodeNotReady` and `NodeDiskPressure` at 0, replica mismatch at 1, OOM/CrashLoop at 2, `HighPodCPU` at 3.

Symptom alerts attach as **evidence** and never trigger their own runbook; the *cause* alert selects the runbook. If a node recovers on its own, forty symptom alerts clear with **zero actions** — reported as "1 incident, 0 remediations, self-resolved", the honest outcome.

### Remediation budget — an error budget for automation
Token bucket over **actions on production**, per rolling hour: `global: 20`, `ns:payments: 3`, `ns:shop: 8`, `ns:chaos: 50`. Exhaustion escalates rather than queues. *"If my system needs to act more than 20 times an hour, the cluster has a systemic problem and a human should be looking at it."*

### Global circuit breaker
Trips on any of: more than 3 compensations in 15 minutes; more than 2 consecutive verification failures for the same runbook; Kubernetes API error rate above 20%; or a manual `healopsctl freeze --reason "release in progress"`. Recovery is `open → half_open` after 30 minutes, where **exactly one plan** is allowed through and its outcome decides `closed` or `open` again. The freeze itself is a ledger entry with a reason and an owner.

### The anti-masking guard
The criticism every self-healing system deserves and almost none answer: **automatically fixing the same failure every day converts a bug into a chore and hides it.**

At `RECURRENCE_THRESHOLD = 4` for the same (runbook, target) within 7 days: **demote** that (runbook, namespace) to approval mode, **open a GitHub issue** with the rendered masking report (timeline, plans, verifications, bundle hashes), labels `healops` / `recurring-incident` / `needs-root-cause`, attach the issue URL to the recurrence row, and post to Slack.

> *"Automation that keeps papering over a defect files a bug report against it. I haven't seen an open-source remediation tool that does that."*

### Scaling decision to document
> **Bounded automation (token bucket + circuit breaker) over unbounded event-driven remediation.** Remediation throughput scales with alert volume, and alert volume spikes exactly when the cluster is least healthy — so an unbounded system does its most aggressive work at the worst moment. Rejected: scaling the workers, because the bottleneck is *risk*, not compute. **Trade-off accepted:** in a genuine mass event HealOps fixes *less* and pages *sooner*. Per-resource locking solves *conflicts*; this solves *volume*. Different problems.

## Chaos tests — three per runbook, or it doesn't merge

| Test | Asserts |
|---|---|
| **Fix** | Inject the fault in `kind` → this runbook's plan is chosen → after execution the fault clears **and verification returns `remediated`** |
| **Compensation** | After a successful execution, force compensation → cluster state is **byte-identical** to the pre-incident snapshot (image, replicas, resources, annotations) |
| **Refusal** | Present the case it must refuse (StatefulSet, single replica, opt-out annotation, ceiling reached) → **plan is a refusal or policy denies** |

Mark them `@pytest.mark.chaos` so the unit suite stays hermetic. The compensation test compares snapshots with `==`, not "close enough" — that exactness is what makes auto-execution defensible. CI wiring for the kind cluster lives in `healops-kubernetes-platform`.

> *"Every runbook ships three tests against a real kind cluster with real faults: it fixes the fault, its compensation restores the cluster byte-for-byte, and it refuses the cases it's supposed to refuse. A runbook without those three tests doesn't merge."*

## Runbook-specific guardrails worth preserving

- **CPU scale-up** must detect an HPA owning the workload and patch the **HPA target** instead of `replicas`, or refuse. Hard `MAX_REPLICAS_CEILING`, projected-cost check, ephemeral TTL so it returns on its own.
- **Disk cleanup** is **archive-then-delete, never delete-only**, with a 7-day floor, a dry-run listing inside the plan, and a **refusal if the S3 write fails**.
- **Node drain** refuses if remaining allocatable is under 120% of drained requests, respects PDBs, and always requires approval.
- **OOM memory bump** goes through in-place resize — see `healops-gitops-remediation`.

## Where the pattern names come from

Saga/compensating transaction, policy-as-code, event sourcing via the hash-chained ledger, and token bucket + circuit breaker **as policy rather than transport** — that last one is the idea most auto-remediation projects miss, because they rate-limit HTTP calls and never rate-limit *changes to production*.

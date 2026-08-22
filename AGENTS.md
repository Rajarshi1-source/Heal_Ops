# AGENTS.md — HealOps

Self-healing Kubernetes infrastructure with automated remediation runbooks. Prometheus alerts trigger
runbooks that **plan** a fix, get gated by **policy**, execute, then **verify recovery and revert
themselves when it failed** — with every decision written to a hash-chained audit ledger.

## Where the rules live

| Source | Consumed by |
|---|---|
| `.cursor/rules/*.mdc` | Cursor — `healops-core.mdc` always applies; the rest are glob-scoped |
| `.claude/skills/<name>/SKILL.md` | Claude Code — seven skills, model-invoked by description |
| `HealOps_Implementation_Plan_Rev2.md` | The authoritative 3,632-line specification |

The seven skills and the areas they own:

- **healops-python-engine** — FastAPI webhook receiver, correlation, persistence, Redis Streams,
  locks, leader election, Slack wiring. Has a `references/data-model-and-endpoints.md` with the full
  schema, REST surface and resilience table.
- **healops-remediation-safety** — the decision path: `RemediationPlan`, blast radius, the CEL policy
  gate, the runbook contract, the orchestrator, verification, compensation, budget, breaker,
  anti-masking guard, chaos tests.
- **healops-kubernetes-platform** — Prometheus rules, Alertmanager, RBAC, Helm 4, kind, chaos app,
  CI gates, Grafana, deployment strategy.
- **healops-gitops-remediation** — Argo CD/Flux classification, ephemeral vs durable fixes, durable
  fixes as pull requests, in-place pod resize.
- **healops-audit-ledger** — the hash chain, canonical JSON, evidence bundles, cosign, `healopsctl`.
- **healops-mlops-promotion** — the promotion ladder, calibrated confidence, the decision eval.
- **healops-nextjs-dashboard** — the Next.js 16.3 audit dashboard.

Read the relevant skill before writing code in its area. The `.mdc` rules are condensed; the skills
are the full text.

## The pipeline

```
alert -> correlate -> PLAN -> policy gate -> [shadow | approval | execute] -> VERIFY -> compensate?
```

When unsure where code belongs, ask which stage it serves. **Every branch is audited, including the
branches that do nothing** — refusals, denials and shadow records are first-class outcomes.

## Non-negotiable invariants

1. **Success means the incident is over**, never that the Kubernetes API returned 200.
2. A runbook that cannot describe its own undo is not registered — `compensate()` is abstract.
3. Compensation is **persisted before the action runs**, never derived afterwards.
4. Refusals and denials are audited outcomes, never errors.
5. The auto-vs-approval split is **computed by policy from plan properties**, never hardcoded.
6. A missing signal is never a healthy signal — unavailable metrics produce `inconclusive`.
7. The ledger append happens **in the same transaction** as the state change. No ledger write, no action.
8. Authorisation **fails closed** — `can_execute()` returns `False` on any unreadable approval state.
9. **HealOps never remediates HealOps** — enforced in the policy file, not in code.

## Version pins — never `latest`

Kubernetes **1.36** demo / **1.35** floor (in-place resize GA, cgroup v2) · Python **3.14** ·
FastAPI **0.128.x** · PostgreSQL **18.6** (`uuidv7()`, temporal constraints) · Valkey **9.1** /
Redis 8.x · Alertmanager **0.33.1** (**API v2 only**) · Prometheus **3.x** chart-pinned · Grafana
**13.0.x** · Helm **4.2.x** · Node **24 LTS** · Next.js **16.3.x** · slack-bolt **1.28.x**.

> §9.1 and §10 of the Rev 2 plan still contain Rev 1 code blocks with stale pins (`version: '3.8'`,
> Python 3.12, Node 20, `:latest` tags, `redis-server` against a Valkey image). **The skills supersede
> those blocks.**

## Repository layout

```
engine/        Python 3.14 remediation engine (FastAPI)
dashboard/     Next.js 16.3 audit dashboard
chaos-app/     Deliberately fragile services, one fault per alert type
charts/        Helm 4 charts: healops, chaos-app, alerting-rules
k8s/           kind config, kube-prometheus-stack values, RBAC
policy/        remediation.yaml — the CEL policy gate, in git
evals/         decision_eval, policy_eval, ledger_verify — CI gates
corpus/        Chaos-generated labelled incident bundles
cli/           healopsctl — replay, audit, freeze, plan
monitoring/    Grafana dashboards as provisioned JSON
```

## Commands

```bash
make dev            # docker compose up, local stack
make test           # pytest -m "not chaos" --cov=engine --cov-fail-under=80
make chaos          # pytest -m chaos, needs a kind cluster
make gates          # decision_eval + policy_eval + ledger_verify
make cluster        # kind create cluster --config k8s/kind/cluster.yaml
make demo           # inject the five faults in sequence
```

Python dependencies are managed with `uv`. The unit suite is hermetic and needs no cluster; anything
cluster-dependent is marked `@pytest.mark.chaos`.

## Style

Comments explain non-obvious intent, trade-offs, or constraints the code cannot convey — never what
the next line does. Several interview-framing comments are deliberate and specified by the skills;
preserve them.

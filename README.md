# HealOps — Self-Healing Infrastructure with Automated Remediation Runbooks

> Fix the incident before the engineer wakes up — and prove it worked.

Prometheus detects a problem. Alertmanager posts a webhook. HealOps **correlates** the alerts into one incident, **plans** a remediation (blast radius, undo, and the signal that would prove recovery), checks that plan against **policy**, then either records a shadow plan, asks a human, or executes. Afterwards it **verifies** that the incident actually cleared, and **reverts** the change when it did not. Every branch — including a refusal, a denial, and a shadow record — is a hash-chained audit entry.

The pipeline is fixed:

```
alert -> correlate -> PLAN -> policy gate -> [shadow | approval | execute] -> VERIFY -> compensate?
```

Success means the incident is over. An API call that returned 200 is not success. A `rollout undo` onto an equally broken revision is a failure, and it is recorded as one.

The authoritative specification is [`HealOps_Implementation_Plan_Rev2.md`](HealOps_Implementation_Plan_Rev2.md). The build order and phase status live in [`.cursor/plans/healops_full_build_138d9c6c.plan.md`](.cursor/plans/healops_full_build_138d9c6c.plan.md). This README describes the repository as it is, against that plan.

## Six things that make it different

Auto-remediation is not an empty market. The difference is the safety envelope around the action, not the `kubectl` call itself.

1. **A plan exists before any change.** Every runbook emits a frozen `RemediationPlan`: the exact API calls, blast radius, reversibility, the compensating actions, and the recovery signal. `healopsctl plan` prints that plan from a committed incident bundle and changes nothing. Planning is a pure function of the bundle, so the same input always yields the same plan.

2. **Policy can refuse, and the refusal is recorded.** `policy/remediation.yaml` is CEL, evaluated in-process. Order is fixed: first DENY wins, then any REQUIRE_APPROVAL, else ALLOW. Irreversible actions, StatefulSets, opted-out workloads, and HealOps' own namespace are denied in the policy file. A refusal is a first-class outcome with its own ledger entry.

3. **One cause becomes one incident.** Correlation groups alerts by topology. A node-level cause outranks a pod symptom. Symptom alerts attach as evidence and never select their own runbook, so one node fault does not become forty rollbacks. Alertmanager `inhibit_rules` are the first line of that defence; correlation is the second, because inhibition cannot express owner-reference topology.

4. **Recovery is the definition of success.** `REMEDIATED` requires all three: the triggering alert cleared in Alertmanager (API v2), the golden signals recovered (throttle ratio, PSI, restarts, OOM events), and no new alert on the same target that the action could have caused. A missing signal is `inconclusive` and escalates. Absence of evidence is never evidence of recovery.

5. **The undo is saved before the action.** `register_compensation()` runs before `execute()`, and the compensating calls are persisted on the plan row. Both the verifier and a separate watchdog can run `compensate()`, which must be idempotent. A compensation that lives only inside the process that might crash is not a compensation.

6. **The ledger is required for the action to happen.** Each state change and its ledger append share one transaction. The chain is `sha256(prev_hash || canonical_json(entry))`. The application role has `INSERT` and `SELECT` on `audit_ledger`; `UPDATE` and `DELETE` are revoked. If PostgreSQL cannot take the append, the engine does not act.

Two further differentiators are specified and are not in this tree yet: GitOps-aware ephemeral mitigations with a durable pull request only after verification, and an OOM fix that raises memory through the `pods/resize` subresource without restarting the container. Both are described under [What is actually here](#what-is-actually-here).

## Architecture

```mermaid
flowchart LR
    AM["Alertmanager v2 webhook"] --> Corr[correlate]
    Corr --> Plan["plan() RemediationPlan"]
    Plan --> Gate["CEL policy gate"]
    Gate -->|deny| Denied[denied]
    Gate -->|shadow| Shadow[shadow record]
    Gate -->|require_approval| Appr["Slack approval Postgres is truth"]
    Gate -->|allow| Exec["execute()"]
    Appr --> Exec
    Exec --> Verify["verify: alert cleared + golden signals"]
    Verify -->|remediated| Done[remediated]
    Verify -->|not_recovered or inconclusive| Comp["compensate()"]
    Comp --> Esc[escalate]
```

Every branch in that diagram, including `denied` and `shadow`, appends to the hash-chained ledger.

```
  chaos app + workloads
           │ scrape
           ▼
  Prometheus 3.x  +  PrometheusRule alerts
           │ firing
           ▼
  Alertmanager 0.33.1   group, inhibit, UTF-8 strict, API v2
           │ webhook
           ▼
  HealOps engine (FastAPI, Python 3.14)
     correlate → plan → policy → shadow | approval | execute → verify → compensate
           │
           ├── PostgreSQL 18     plans, executions, approvals, hash-chained ledger
           ├── Valkey 9.1        streams, locks, dedup, approval cache
           ├── Kubernetes API    the only place a runbook mutates a workload
           ├── Slack             approval and "was this correct?"  (optional)
           └── S3 / MinIO        log archive for the disk runbook
```

The engine ingests and persists. It does not decide whether an action is allowed. That decision is the policy gate, which is deterministic rules in git, so it can be unit-tested, replayed, and hashed. An LLM, if one is added later, writes prose only and never alters a plan, a policy decision, or a verdict.

Two engine replicas can accept webhooks. A Kubernetes `Lease` elects the one that executes. Webhook availability and execution authority are different requirements.

## Quick start

You can exercise the decision path on a laptop with no cluster. A kind cluster is only required for the chaos app, Prometheus, and Alertmanager.

**Toolchain the repo is pinned for:** Python 3.14, [uv](https://docs.astral.sh/uv/), Docker, and — for the cluster path — kind, kubectl, and Helm 4.2.x. On Windows, `make` targets expect Git Bash or WSL (`SHELL := /bin/bash`). The commands below are the ones those targets run, written for PowerShell.

### 1. Tests and an offline plan

```powershell
uv sync --all-groups
uv run pytest -m "not chaos" --cov=engine/src --cov-fail-under=80

uv run python -m cli.healopsctl plan corpus/crashloop-allow.json
uv run python -m cli.healopsctl plan corpus/refuse-statefulset.json
uv run python -m cli.healopsctl runbooks
```

`plan` reads a committed bundle, selects the runbook, and evaluates `policy/remediation.yaml`. It exits `2` on a runbook refusal and `3` on a policy denial. Nothing is executed.

### 2. Engine against local Postgres, Valkey, and MinIO

`docker compose up` also builds a dashboard image from `dashboard/`, which is not in this repository yet, and Grafana expects provisioning files under `monitoring/grafana/` that are not here either. Start the three data services directly:

```powershell
copy .env.example .env
docker compose up -d postgres redis minio

# Migrations run as the database owner, not as healops_app.
# alembic.ini lives in engine/ and resolves `script_location` from that directory.
$env:HEALOPS_MIGRATION_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/healops"
uv run --directory engine alembic upgrade head

uv run uvicorn main:app --app-dir engine/src --host 127.0.0.1 --port 8000
```

Then open `http://127.0.0.1:8000/docs`, `http://127.0.0.1:8000/healthz`, and `http://127.0.0.1:8000/api/runbooks`.

The receiver is `POST /api/webhooks/alertmanager` with `Authorization: Bearer <HEALOPS_WEBHOOK_SHARED_SECRET>`. It persists the Alertmanager v2 payload, enqueues cause alerts, and returns **202**. Leave the Slack variables empty: the engine still serves, and approvals stay held rather than executing.

The webhook enqueues work onto a Valkey stream. The library that plans, gates, executes, verifies, and compensates is in `engine/src/` and covered by unit tests. A long-running consumer that drains that stream into `orchestrator.handle()` is not started by `main.py` yet, so a posted webhook is stored and queued, and it is not turned into a cluster change by the running process.

### 3. Kind cluster, alerts, and the chaos app

```powershell
kind create cluster --name healops --config k8s/kind/cluster.yaml --image kindest/node:v1.36.0

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install kps prometheus-community/kube-prometheus-stack `
  --version 88.4.0 `
  --namespace monitoring --create-namespace `
  -f k8s/prometheus/kube-prometheus-stack-values.yaml `
  --wait --timeout 10m

helm upgrade --install alerting-rules charts/alerting-rules --namespace monitoring
helm upgrade --install chaos charts/chaos-app --namespace chaos --create-namespace --wait
```

`k8s/kind/cluster.yaml` maps Prometheus to host port `9090` and Alertmanager to `9093`. Point the engine at those with `HEALOPS_PROMETHEUS_URL` and `HEALOPS_ALERTMANAGER_URL` (the defaults in `.env.example`).

`make deploy` also installs `charts/healops`. That chart is not in the repository yet, so the engine is not installed into the cluster by a Helm release. Run it from the host, as in step 2, with a kubeconfig for the kind cluster.

`make demo` injects the five faults via `chaos-app/injectors/run_demo.py`. Runbooks 4 and 5 (OOM resize, cordon and drain) are not registered, so that script can break the workloads without HealOps closing those two incidents.

### Makefile targets

| Target | What it does today |
|---|---|
| `make install` | `uv sync --all-groups` |
| `make test` | Hermetic unit suite, coverage gate 80% on `engine/src` |
| `make healopsctl ARGS="plan corpus/crashloop-allow.json"` | Offline plan |
| `make cluster` | kind cluster on `kindest/node:v1.36.0` |
| `make observability` | kube-prometheus-stack chart `88.4.0` |
| `make dev` | `docker compose up --build` — blocked on the missing `dashboard/` build context and Grafana provisioning files |
| `make deploy` | Blocked on the missing `charts/healops` chart |
| `make gates` | Blocked on `evals/decision_eval.py`, `evals/policy_eval.py`, and `evals/ledger_verify.py` |
| `make chaos` | No tests are marked `chaos` yet |

## Repository layout

```
engine/          Python 3.14 remediation engine (FastAPI, Alembic, unit tests)
chaos-app/       Fragile workloads and fault injectors, one fault per alert
charts/          Helm charts: alerting-rules, chaos-app
k8s/             kind cluster config, kube-prometheus-stack values
policy/          remediation.yaml — the CEL policy gate
cli/             healopsctl (plan, runbooks)
corpus/          Labelled incident bundles used by healopsctl plan
evals/           Package stub for the CI decision gates
```

Also at the root: `docker-compose.yml`, `Makefile`, `pyproject.toml`, `AGENTS.md`, and `HealOps_Implementation_Plan_Rev2.md`.

The plan's tree also names `dashboard/`, `charts/healops`, `k8s/rbac/`, `monitoring/` (Grafana JSON), and `.github/workflows/`. Those paths are not in the repository.

## What is actually here

Phases follow [the full build plan](.cursor/plans/healops_full_build_138d9c6c.plan.md). "In this repository" is what you can read and test today.

| Phase | In this repository | Still in the plan |
|---|---|---|
| **0 — Rules and scaffold** | Seven skills under `.claude/skills/`, Cursor rules, `AGENTS.md`, `pyproject.toml` (`requires-python >= 3.14`), pinned `docker-compose.yml`, Makefile, kind config, `.env.example` | — |
| **1 — Cluster, alerts, chaos app** | kind config on Kubernetes 1.36, kube-prometheus-stack values (Alertmanager `v0.33.1`, 3 replicas, UTF-8 strict, `inhibit_rules`, webhook to the engine), five runbook alerts plus recording rules and engine self-monitoring alerts, chaos app (`crashy-api`, `hungry-worker`, `cpu-burner`, `log-spammer`, `flaky-node`) and `capture_corpus.py` | A captured corpus of 25 or more bundles. `corpus/` currently holds three committed bundles |
| **2 — Engine core** | Alertmanager v2 webhook (constant-time secret, durable write, then 202), cause-first correlator, PostgreSQL 18 schema via Alembic including partitioned `audit_ledger` with `uuidv7()` and `REVOKE UPDATE, DELETE`, hash chain and `verify_chain()`, Valkey streams with separate `exec` and `verify` groups, fenced locks, Lease election | A worker process that reads the stream and calls the orchestrator |
| **3 — Plan and policy** | Frozen `RemediationPlan`, blast-radius score, `refuse()`, CEL gate, runbook contract (`plan`, `execute`, `compensate`, `extra_verification`, `chaos_fixture`), orchestrator with each branch audited, runbooks 1–3, `healopsctl plan` | — |
| **4 — Verify, compensate, approve** | `verifier.py`, `compensator.py`, `watchdog.py`, Slack Block Kit builders, Bolt `AsyncApp` mounted on FastAPI when tokens are set, approvals in PostgreSQL with `expires_at`, a reaper, and fail-closed `can_execute()` | The stream consumer that schedules verification after execute; the watchdog CronJob manifest; an end-to-end "bad rollback reverts in ~90s" run |
| **5 — Runbooks 4–5, budget, breaker, masking** | Schema tables `remediation_budget` and `remediation_recurrence`. Policy already *reads* a budget snapshot and a promotion snapshot when a caller supplies them | `pods/resize` runbook, cordon+drain runbook, token-bucket budget, circuit breaker, anti-masking GitHub issue |
| **6 — GitOps, promotion, dashboard** | `classify_gitops()` labels a target from annotations. CrashLoop plans are ephemeral with a TTL when the target is managed. Promotion rows exist in the schema. Slack "was this correct?" writes a ledger entry | `gitops.py` pause/resume, durable pull requests after verification, `promotion.py` ladder and one-strike demotion, Next.js 16.3 dashboard |
| **7 — CI, replay, Helm, public demo** | `healopsctl plan` and `healopsctl runbooks`. `evals/` is an empty package | Chaos tests (three per runbook), decision/policy/ledger gates, `replay` / `audit` / `freeze`, GitHub Actions, cosign, the HealOps Helm chart, read-only public dashboard |

### The five runbooks

| # | Trigger | Mitigation | Registered here | Chaos fixture in `chaos-app/` |
|---|---|---|---|---|
| 1 | `KubePodCrashLooping` | `rollout undo` to the previous revision; refuses a first revision and a rollback onto the same image | `crashloop_rollback` | `crashy-api` |
| 2 | `NodeDiskPressure` | Archive logs older than 7 days to S3, then delete; refuses if the archive write fails | `disk_cleanup` | `log-spammer` |
| 3 | `HighPodCPU` | Scale the Deployment, or patch the HPA target when an HPA owns it; replica ceiling and a cost guard | `cpu_scale_up` | `cpu-burner` |
| 4 | `KubePodOOMKilled` | In-place memory resize via `pods/resize`, success read from container status, refuse `RestartContainer` and `Infeasible` | Not registered | `hungry-worker` (the fault injector is here) |
| 5 | `KubeNodeNotReady` | Cordon and drain, capacity precheck, PDB-aware, always requires approval | Not registered | `flaky-node` (the injector is here) |

Runbooks 1–3 implement `plan`, `execute`, and `compensate`. Default mode in the plan is auto for 1–3 only after the promotion ladder has earned it, and approval in `payments`. The ladder itself is not wired, so `healopsctl plan` takes an explicit `--promotion` (default `auto`) and the policy file decides allow, approval, or deny from plan properties.

## Operational honesty

**The demo scorecard in the plan is a script, not a measurement.** The specification's line — five faults injected, three auto-remediated, two escalated, zero false remediations — describes the rehearsal in §28.4 of the plan. This repository has not produced that scorecard. There is no dashboard, no chaos suite, and no consumer turning a firing alert into an execution.

**A webhook accepted is not an incident closed.** `POST /api/webhooks/alertmanager` returns 202 after a durable insert and an `XADD`. Unit tests call `orchestrator.handle()`, `verifier.verify()`, and `compensator` directly. The running server does not.

**Approvals fail closed.** `can_execute()` returns false when the approval row is missing, unreadable, or past `expires_at`. Timeout escalates. It does not execute. With Slack unset, approval-required plans stay held. PostgreSQL is the source of truth; Valkey only caches the decision. Wiping Valkey drops queue position and caches. It does not drop plans, approvals, or the chain.

**HealOps does not remediate HealOps.** The `healops` namespace is denied in `policy/remediation.yaml`, and the Alertmanager route for that namespace goes to the null receiver. The rule lives in the policy file.

**What this system leaves alone, by design:** multi-cluster federation, LLM-chosen actions, arbitrary user-defined runbooks, database or network remediation, and anything irreversible. The decision layer stays deterministic so a plan can be replayed and hashed.

**Where other tools are ahead.** Robusta has years of production hardening, alert enrichment, and integrations this project does not attempt. StackStorm is a general-purpose automation platform with a large action library. Argo Workflows provides durable, retryable workflows. AI-agent products investigate open-ended problems this engine will not touch. HealOps is a demonstration of a safety envelope — plan, gate, act, verify, revert, audit, and earn authority over time — not a replacement for those systems. The head-to-head grid in §28.2 of the plan is dated August 2026 and was written as positioning; this README does not re-verify those projects' current READMEs, and several HealOps column entries in that grid (promotion ladder, calibrated confidence in CI, GitOps pull requests, in-place resize, remediation budget, anti-masking issues, chaos tests, offline `replay`) are specified rather than present here.

**Pins that are actually in the tree.** PostgreSQL `18.6-alpine`, Valkey `9.1-alpine` (the service is named `redis`; the process is `valkey-server`), MinIO `RELEASE.2026-04-17T23-12-11Z`, Grafana `13.0.3` (image only; no dashboards are provisioned), Alertmanager `v0.33.1`, kube-prometheus-stack chart `88.4.0`, kind node `v1.36.0`, Python `>=3.14`. Python dependencies are exact pins in `pyproject.toml` (FastAPI `0.141.1`, slack-bolt `1.30.0`, kubernetes `36.0.3`). The plan's §B floors (FastAPI 0.128.x, slack-bolt 1.28.x) are older points on those lines; the lockfile is what installs. Sections §9.1 and §10 of the Rev 2 document still contain stale snippets (`:latest`, Python 3.12, `redis-server` against a Valkey image). The skills and this repository supersede those snippets.

**No public remediation endpoint.** There is no live demo URL. When a public dashboard exists, the plan requires it to be read-only and seeded. An endpoint that can trigger remediation stays inside the cluster.

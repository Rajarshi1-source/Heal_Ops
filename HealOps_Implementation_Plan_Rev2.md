# HealOps — Self-Healing Infrastructure with Automated Remediation Runbooks

## Complete Implementation Plan for Junior DevOps / SRE Engineer Interview (2026)

**Revision 2 — August 2026.** Supersedes Rev 1.

---

> ### Revision 2 — What Changed and Why
>
> Rev 1 was audited line-by-line against the brief. **The core is sound** — the five runbooks, the auto-vs-approval split, PostgreSQL-over-TimescaleDB, Redis-over-Kafka, per-resource locking, and the "3 out of 5" demo are all correct calls and Rev 2 keeps every one of them. What Rev 2 adds is the difference between *a project that automates remediation* and *a project a senior SRE would trust in production*:
>
> **1. Five real defects fixed (§C).** The biggest: **Rev 1 never verifies that a remediation actually worked.** It calls the Kubernetes API, gets a 200, writes "success" to the audit log and posts to Slack — but a `rollout undo` that rolls back to an equally broken revision is recorded as a success. Also fixed: imperative fixes silently reverted by Argo CD/Flux drift detection; approval state living only in Redis (restart Redis and pending approvals vanish, with the executor unable to distinguish "expired" from "lost"); the CPU scale-up runbook fighting an HPA it doesn't know about; and alert-storm amplification, where one node failure becomes forty rollbacks because dedup is per-fingerprint and locks only serialise.
>
> **2. Stack re-pinned against reality as of 18 August 2026 (§B).** PostgreSQL **18.6** (and PG 18's `uuidv7()` plus temporal constraints turn out to be genuinely useful for an audit ledger), Alertmanager **0.33.1**, Kubernetes **1.36** demo / **1.35** floor, Helm **4.2**, Python **3.14**, FastAPI **0.128.x**, slack-bolt **1.28.x**, Next.js **16.3**, Valkey **9.1** (or Redis 8.x), Grafana **13**.
>
> **3. Eight differentiators that make HealOps better than the tools that already exist (§14–§21).** This matters because auto-remediation is *not* an empty market — Robusta ships self-healing rules plus AI investigation and change tracking, StackStorm has done event-driven ops automation for a decade, Argo Workflows can be an Alertmanager webhook target, and there are AI-SRE-agent products doing LLM triage. A fresher who builds "Alertmanager → webhook → kubectl" has rebuilt the easy 20% of Robusta. So Rev 2 builds the parts they *don't* do:
> - **§14 Plan-then-apply.** Every runbook first emits a **RemediationPlan** — the exact API calls, the blast radius (how many pods, nodes, namespaces), a reversibility class, the compensating action, and an expected recovery signal — which is then checked against policy before anything executes. `terraform plan` for incident response.
> - **§15 Closed-loop verification with automatic compensation.** After acting, HealOps verifies the alert cleared *and* the golden signals recovered. If not, it **executes the compensation it registered before acting** and escalates. Success means "the incident is over," not "the API call returned 200."
> - **§16 Cause-first correlation, storm control, and the anti-masking guard.** One node failure produces one incident, not forty remediations. A per-namespace **remediation budget** and a global circuit breaker mean automation stops and pages a human when it's firing too often. And when the same runbook fixes the same workload for the fourth time in a week, HealOps **opens a GitHub issue with the evidence** — because self-healing that hides a bug is a liability, and no competing tool tells you it's doing it.
> - **§17 GitOps-aware remediation.** The detail almost nobody gets right: `kubectl scale` on an Argo CD-managed workload gets reverted by drift detection, and `rollout undo` gets undone by the next sync. HealOps classifies every fix as **ephemeral** (TTL'd, auto-expiring, reconciliation-aware) or **durable** (a pull request), and never pretends an imperative patch is a permanent fix.
> - **§18 Non-disruptive remediation using in-place pod resize (GA in Kubernetes 1.35).** The OOMKill runbook raises memory **without restarting the container**, which changes the approval calculus: the mitigation becomes safe-and-reversible, and only the durable template change needs a human.
> - **§19 The promotion ladder — "was this correct?" that actually changes behaviour.** Feedback isn't just logged. Each (runbook, namespace) pair climbs **shadow → approval-required → auto**, on evidence, with a documented threshold and instant demotion on a bad outcome. Progressive delivery, applied to automation itself, plus **calibrated confidence** graded in CI (Brier score / ECE) against a chaos-generated incident corpus.
> - **§20 Tamper-evident audit ledger.** Hash-chained records, cosign-signed evidence bundles, offline replay via `healopsctl replay <sha>`. This is the answer to *"how would a regulated company let a bot change production?"* — every automated action has a change record, an approver identity, a rollback record, and a signature.
> - **§21 Chaos-tested runbooks in CI.** Every runbook ships three tests: it fixes the fault, its compensation restores the original state, and its policy gate refuses the case it should refuse. A runbook without a chaos test does not merge.
>
> **4. The brief's missing sections written properly (§22–§28):** resilience patterns, availability & consistency, a 20-row mitigation table, deployment strategies, the README blueprint, build order with a head-to-head competitive grid, and a fact-check appendix so every version claim here is defensible.
>
> **What Rev 1 got right and Rev 2 keeps:** plain PostgreSQL over TimescaleDB (the cross-project contrast with KubeThrifty is still the best database answer in the portfolio), Redis Streams over Kafka, the five runbooks, the reversibility-based approval split, per-resource distributed locks, the self-referential demo, and the "3 of 5" scorecard framing.

---

## §A. Completeness Audit — Rev 1 vs the Brief

| # | Brief asked for | In Rev 1? | Where | Rev 2 action |
|---|---|---|---|---|
| 1 | Tech stack, justified | ✅ | §2 | Re-pinned (§B); rows added for policy engine, ledger, GitOps client |
| 2 | Exact versions | ⚠️ stale | §2 | **Rewritten** (§B) + fact-check appendix (§28) |
| 3 | MVP blueprint | ✅ | §3 | Runbook contract extended (plan/verify/compensate); Runbook 4 becomes non-disruptive (§18) |
| 4 | Detailed system design | ✅ | §4 | Plan → policy → execute → verify → compensate stages added |
| 5 | Detailed system architecture | ✅ | §4 | Ledger, budget guard, GitOps reconciler added |
| 6 | High-level design | ✅ | §4 | Unchanged in shape |
| 7 | Low-level design | ✅ | §5 | Runbook base class rewritten (§14.4) |
| 8 | Detailed database design | ✅ | §6.2 | New tables: plans, verifications, ledger, promotion state, budgets (§C.6) |
| 9 | Database choice + matrix | ✅ | §6.1 | Verdict unchanged (plain PostgreSQL) — upgraded to 18.6, ledger partitioning, `uuidv7()` |
| 10 | Cache & messaging choice | ✅ | §7 | Verdict unchanged (Redis Streams) — Valkey noted; **approval state moved to Postgres as source of truth** (defect C3) |
| 11 | Design patterns | ✅ 6 | §8 | +4 (saga/compensation, policy-as-code, ledger/event-sourcing, token bucket) |
| 12 | Docker & K8s | ✅ | §9 | Helm 4 notes, pinned compose, minimal RBAC restated |
| 13 | DevOps/**MLOps** wrapper | ❌ | — | **§19.4** — chaos-generated corpus + decision gate + calibration gate in CI |
| 14 | Resilience patterns | ⚠️ partial (circuit breaker only) | §8.5 | **§22** — full table |
| 15 | Mitigation strategies | ❌ | — | **§24** — 20-row failure-mode table |
| 16 | Availability & consistency | ❌ | — | **§23** |
| 17 | Deployment strategies | ⚠️ partial | §9, §13 | **§25** — environments, canary for HealOps itself, remediation promotion as progressive delivery, migrations, rollback |
| 18 | Interview prep | ✅ 8 Q&A | §12 | **+14 Q&A** (§27) |
| 19 | README + architecture diagram + screenshots | ⚠️ in checklist only | §13 | **§26** — full blueprint |
| 20 | CI/CD (Actions → registry → deploy) | ✅ | §10 | + policy gate, corpus gate, cosign signing (§21) |
| 21 | docker-compose.yml | ✅ | §9.1 | Image tags pinned |
| 22 | Monitoring dashboard | ✅ | §11 | + efficacy scorecard panels, budget/circuit-breaker panels (§15.5) |
| 23 | Documented "scaling decision" | ✅ 1 | §11 | + a second (remediation budget vs unbounded automation) (§16.5) |
| 24 | Public deployment + live link | ✅ | §13 | Unchanged |
| 25 | **Differentiators vs existing tools** | ❌ | — | **§14–§21 + the grid in §28.2** |
| 26 | Best DB choice among PG/NoSQL/Timescale/Cosmos/Cassandra/Mongo | ✅ | §6.1 | Kept and sharpened |
| 27 | Cache choice among Redis/Kafka/Zookeeper/RabbitMQ | ⚠️ Zookeeper not addressed | §7 | Addressed (§C.7) — Zookeeper is coordination, not a queue, and Kafka dropped it in KRaft |

**Verdict: the plan was complete on structure and thin on rigour.** Every gap above is closed in Rev 2.

---

## §B. Exact Version Matrix — verified 18 August 2026

Pin everything. Sources and dates in **§28**.

| Component | Rev 1 | **Rev 2 — pin this** | Why |
|---|---|---|---|
| **Kubernetes** | not pinned | **1.36** for the demo (`kindest/node:v1.36.x`); **1.35 floor** | 1.34 entered maintenance 27 Aug 2026. **In-place pod resize is GA in 1.35** — that is what makes the OOMKill runbook non-disruptive (§18). 1.35+ also requires **cgroup v2** |
| **Python** | 3.12 | **3.14** (`python:3.14-slim`, non-root) | Current stable line; wheels available across the DevOps stack. slack-bolt added 3.14 support in the 1.28 line. Pin `requires-python = ">=3.14"` |
| **FastAPI** | unpinned | **0.128.x** (with `uvicorn[standard]`) | Current line as of mid-2026 (slack-bolt's own dev requirement moved to `>=0.128.8,<1`). Pin exactly and let Renovate bump |
| **Alertmanager** | unpinned | **0.33.1** (Jul 2026) | Two things matter: **API v1 is gone** (v2 only — your webhook receiver and any `amtool` scripting must be v2), and the **UTF-8 matcher parser** is the default path now, so enable UTF-8 strict mode on a new config |
| **Prometheus** | unpinned | **3.x, chart-pinned** (3.11.2 was current in Apr 2026 — check the current patch) | Never `latest`. Install via `kube-prometheus-stack` with an explicit chart version (~88.x line) |
| **Grafana** | 12.x | **13.0.x**, dashboards provisioned as JSON in-repo | Reproducibility |
| **PostgreSQL** | 16 | **18.6** (`postgres:18.6-alpine`) | 18.6 released 11 Aug 2026. Three PG18 features earn their place here: **`uuidv7()`** (timestamp-ordered UUIDs — perfect primary keys for an append-only ledger), **temporal constraints** (range-based `PRIMARY KEY`/`UNIQUE`/`FOREIGN KEY` — models "this approval is valid for this window" natively), and the new async I/O subsystem. PG 19 is in beta — **do not** ship a beta database |
| **Cache / queue** | Redis 7 | **Valkey 9.1** (default) or **Redis 8.x** | Valkey 9.1 (May 2026) is BSD-3 and drop-in; pin the minor either way and be ready to explain the Redis licence change. Redis **Streams** remain the queue |
| **Slack SDK** | unpinned | **slack-bolt 1.28.x** (+ `slack_sdk`) | The 1.28 line added Python 3.14 support and the FastAPI adapter is first-class. Use the **`AsyncApp` + FastAPI adapter**, not a second web server |
| **Kubernetes client (Python)** | unpinned | **pin the client minor that matches your cluster minor**; verify `pods/resize` subresource support | The typed client lags new subresources; expect to call `resize` through the generic `call_api` path (§18.2) |
| **Node.js / Next.js** | Next 14 | **Node 24 LTS**, **Next.js 16.3.x** | Next 14 is EOL; Next 15 hits EOL Oct 2026. Turbopack is the default bundler in 16 |
| **Helm** | 3 | **4.2.x** | **Helm 3 bug fixes ended 8 July 2026** (security-only to Feb 2027). Three migration gotchas: server-side apply is the default for new installs, `--wait` needs the **`watch`** RBAC verb, and `--post-renderer` must now be a plugin |
| **AWS SDK** | boto3 | **boto3 pinned exactly**, MinIO for local | S3 archival; pin because a silent boto3 bump in CI is avoidable pain |
| **Policy engine** | absent | **CEL via `cel-python`** in-process (plus optional ValidatingAdmissionPolicy in-cluster) | Powers the §14 plan gate without running an extra service in a demo cluster |
| **GitOps client** | absent | **Argo CD API/CLI (or Flux)**, sync-window aware | §17 — the reconciliation interaction is the thing nobody handles |
| **Chaos harness** | custom Python | **custom Python + optional LitmusChaos** | Custom fault injectors stay (they're the demo); Litmus is the "what I'd use at scale" answer |
| **Signing** | absent | **cosign (keyless OIDC)** in CI | Signs images *and* evidence bundles (§20) |

### API and feature status this plan depends on

| Capability | Status (Aug 2026) | Why HealOps cares |
|---|---|---|
| **In-place pod resize** (`InPlacePodVerticalScaling`) | **GA in 1.35**; mutate via the **`resize` subresource**; CPU changes need no restart, memory follows `resizePolicy`; outcomes surface as `PodResizePending` (`Deferred`/`Infeasible`) | §18 — the OOMKill runbook raises memory with **zero restarts**, turning a disruptive fix into a reversible one |
| **cgroup v2** | required by K8s 1.35+ | Reliable OOM and pressure signals; `container_oom_events_total` and cgroup `memory.events` are the trustworthy OOM sources |
| **PSI metrics** (`KubeletPSI`) | beta 1.34 → **GA 1.36** | §15 — "did the workload actually recover?" is answered with pressure, not just with "the alert stopped firing" |
| **ValidatingAdmissionPolicy** (CEL, in-tree) | GA since 1.30 | Optional second enforcement point for the §14 policy gate |
| **Alertmanager API v2 only** | v1 removed in 0.27 | Your webhook contract and any silence automation must target v2 |
| **PostgreSQL 18 `uuidv7()` + temporal constraints** | GA in PG 18 | §20 ledger keys and §19 approval-validity windows |

> Interview soundbite: *"Between revisions two of my pins went out of support — Helm 3 lost bug-fix support in July 2026 and Kubernetes 1.34 was entering maintenance — so I moved to Helm 4.2 and Kubernetes 1.36 with 1.35 as my floor. The 1.35 floor isn't arbitrary: in-place pod resize went GA there, which is what lets my OOMKill runbook raise a memory limit without restarting the container. And I upgraded to PostgreSQL 18 for a specific reason — `uuidv7()` gives me timestamp-ordered primary keys for an append-only audit ledger, which is exactly what you want for hash-chained records."*

---

## Table of Contents

**Foundation (Rev 1, re-verified)**
1. Project Overview & Interview Hook
2. Tech Stack — Every Choice Justified (exact pins in §B)
3. MVP Blueprint — 7-Week Build Plan (revised schedule in §28.3)
4. High-Level Design (HLD)
5. Low-Level Design (LLD)
6. Database Design & Choice (comparison matrix)
7. Caching & Messaging — Redis vs Kafka vs RabbitMQ vs Zookeeper
8. Design Patterns Used
9. Docker & Kubernetes Deployment
10. CI/CD Pipeline
11. Monitoring & the First "Scaling Decision"
12. Interview Prep — Top Questions & Answers
13. Deployment Checklist

**New in Rev 2 — corrections**
C. **Rev 2 Corrections — the five defects, with fixes** (also amends §5, §6, §7, §8)

**New in Rev 2 — the differentiators**
14. **Plan-Then-Apply: RemediationPlan, blast radius, and the policy gate**
15. **Closed-Loop Verification, Compensation, and the Efficacy Scorecard**
16. **Cause-First Correlation, Storm Control, and the Anti-Masking Guard**
17. **GitOps-Aware Remediation: ephemeral vs durable fixes**
18. **Non-Disruptive Remediation with In-Place Pod Resize (GA 1.35)**
19. **The Promotion Ladder + Calibrated Confidence (the MLOps wrapper)**
20. **Tamper-Evident Audit Ledger, Signed Evidence, Offline Replay**
21. **Chaos-Tested Runbooks: the runbook contract in CI**

**New in Rev 2 — cross-cutting engineering the brief asked for**
22. Resilience Patterns
23. Availability & Consistency Patterns
24. Mitigation Strategies (failure-mode table)
25. Deployment Strategies
26. README Blueprint with Architecture Diagram
27. Interview Prep — Rev 2 additions
28. Build Order, Competitive Grid, Revised Schedule, Demo Script, Fact-Check Appendix

---

## 1. Project Overview & Interview Hook

**Project Name:** HealOps

**Tagline:** "Fix the incident before the engineer wakes up."

**One-liner:** A self-healing Kubernetes infrastructure platform where Prometheus alerts trigger Alertmanager webhooks that invoke Python remediation runbooks — automatically rolling back CrashLoopBackOff deployments, archiving logs when disks fill up, and scaling replicas on high CPU — with every automated action logged to a Next.js audit dashboard and Slack notifications that include human-in-the-loop approval buttons for irreversible operations.

**Interview Hook (memorize this):**

> "I built a self-healing infrastructure platform that fixed 3 out of 5 incidents automatically in my demo — and paged me only for the 2 that needed human judgment. Prometheus detects CrashLoopBackOff and fires an alert → Alertmanager webhook hits my Python runbook engine → the runbook automatically runs `kubectl rollout undo` to restore the last working version → Slack notifies the team with full context: what happened, what was done, and a 'was this correct?' button. For irreversible actions like deleting pods or scaling down, the system sends a Slack approval request and waits for human confirmation before executing. I used PostgreSQL over TimescaleDB because my event volume is low-frequency alerts, not high-frequency metrics. I chose Redis over Kafka for the runbook execution queue because my alert rate is under 100 per hour. The entire system runs on K8s with Helm charts and a full CI/CD pipeline."

**Why DevOps interviewers love this:**
- **This is what senior SREs do** — automated incident response is the pinnacle of operational maturity
- **Prometheus + Alertmanager mastery** — the core K8s observability stack
- **Human-in-the-loop design** — shows you understand that not everything should be automated blindly
- **Kubectl operations via code** — programmatic K8s API interaction, not just YAML
- **Slack integration with approval workflows** — real-world incident management
- **Audit trail** — every action logged with context, reasoning, and outcome
- **Freshers never demo this** — most show monitoring dashboards. You show automated remediation

**Target Companies:**
- **Razorpay, Flipkart, Swiggy** — SRE teams managing large K8s clusters with incident automation
- **Atlassian India** — Incident management (they build Opsgenie/Statuspage)
- **PagerDuty, Grafana Labs** — Literally their product space
- **Platform9, InfraCloud** — K8s-native companies
- **Any SRE/DevOps role** — incident response automation is universally valued
- **Freshworks, Zoho, Nutanix** — Platform engineering teams

---

## 2. Tech Stack — Every Choice Justified

### Core Stack

> **Rev 2: the exact pins live in §B** (verified 18 Aug 2026). The table below explains *why* each layer exists; §B says which version to install and what breaks if you don't. Three rows changed materially: Python 3.12 → **3.14**, PostgreSQL 16 → **18.6** (for `uuidv7()` on the audit ledger), and Helm 3 → **4.2.x** (Helm 3 lost bug-fix support in July 2026). Rev 2 also adds three components this table lacks: a **CEL policy engine** for the plan gate (§14.3), a **hash-chained ledger** (§20), and a **GitOps client** for drift-aware remediation (§17).

| Layer | Technology | Why This (Interview Answer) |
|---|---|---|
| **Runbook Engine** | Python 3.14 (FastAPI 0.128.x) | Python dominates DevOps tooling. FastAPI gives async webhook handling + auto-docs. Kubernetes Python client (`kubernetes` library) for programmatic kubectl. Boto3 for AWS S3 operations. |
| **Alert Source** | Prometheus + Alertmanager | Industry standard for K8s. Alertmanager handles deduplication, grouping, silencing, and routing — we configure it to route alerts to our webhook. |
| **Dashboard** | Next.js 16.3 (App Router) + TypeScript | Audit log dashboard, runbook status, incident timeline. SSR for the landing page. Server components for fast loads. |
| **UI Framework** | Tailwind CSS + shadcn/ui | Dark-mode-first (operations dashboards are always dark). Tremor for KPI cards and incident timeline. |
| **Database** | PostgreSQL 18 | See Section 6. Alert events, runbook executions, audit logs, approval states — all relational. Low-frequency events (alerts, not metrics). JSONB for flexible alert payloads. |
| **Cache & Queue** | Valkey 9.1 / Redis 8.x | See Section 7. Runbook execution queue (Redis Streams), alert deduplication, approval state cache, dashboard cache. |
| **Kubernetes API** | Python `kubernetes` client | Programmatic access: rollback deployments, scale replicas, delete pods, read logs. Uses in-cluster ServiceAccount with RBAC. |
| **Slack Integration** | Slack Bolt SDK (Python) | Interactive messages with approval buttons. Block Kit for rich incident cards. Handles OAuth + event subscriptions. |
| **Log Archival** | AWS S3 (or MinIO for local) | When disk fills up → runbook archives old logs to S3. MinIO provides S3-compatible local dev experience. |
| **Container Orchestration** | Kubernetes 1.36 (kind for local, EKS/GKE prod; 1.35 is the supported floor) | HealOps runs ON K8s and remediates ON K8s. Self-referential: it can heal its own pods. |
| **Package Management** | Helm 4.2.x | Charts for HealOps platform, sample chaos app, Prometheus stack. |
| **Chaos Engineering** | Custom fault injectors (Python) | Scripts that intentionally break the sample app: OOMKill, CrashLoop, CPU spike, disk fill. Triggers alerts for demo. |
| **CI/CD** | GitHub Actions | Build → test → push → Helm deploy. Also runs chaos tests in CI. |
| **Monitoring** | Prometheus + Grafana | Self-monitoring: HealOps tracks its own remediation latency, success rate, approval times. |

### Why NOT These Alternatives

| Rejected Option | Why |
|---|---|
| Rundeck / StackStorm | These are existing runbook automation tools — using them means you're configuring, not building. Building from scratch demonstrates deep understanding of the webhook → decision → execute → audit pipeline. |
| TimescaleDB | Our data is low-frequency events (alerts, remediations) — not high-frequency time-series metrics. PostgreSQL handles "100 alerts/hour" trivially. TimescaleDB's hypertables and continuous aggregates add complexity for no benefit at this event rate. |
| MongoDB | Alert chains are relational: alert → triggers runbook → creates execution → awaits approval → logs outcome. Foreign keys and JOINs model this naturally. MongoDB would require denormalization or $lookup chains. |
| Kafka | Our alert rate is < 100/hour. Kafka's 3-broker minimum consumes 4GB+ RAM for a system that processes 1-2 alerts per minute. Redis Streams handles this with zero additional infrastructure. |
| Go for runbook engine | Python has the richest DevOps library ecosystem: `kubernetes` client, `boto3` (AWS), `slack-bolt`, `prometheus-api-client`. Go would require more boilerplate for the same functionality. |
| ArgoCD Rollbacks | ArgoCD handles GitOps deployment, not incident-triggered rollbacks. Our system reacts to Prometheus alerts, not git changes. Different trigger, different use case. |
| AWS Lambda for runbooks | Lambda can't access the K8s API natively (no in-cluster auth). Lambda adds cold start latency (2-5s) to incident response. A pod running inside the cluster has sub-second access to the K8s API. |

---

## 3. MVP Blueprint — 7-Week Build Plan

### MVP Scope — The 5 Runbooks

> **Rev 2 changes three things about this table.** (1) Every runbook now implements `plan() / execute() / compensate()` and ships a chaos fixture — a runbook that cannot describe its own undo is not registered (§14.4, §21). (2) **Runbook 4 becomes non-disruptive and auto-eligible**: in-place pod resize went GA in Kubernetes 1.35, so memory is raised through the `pods/resize` subresource with no container restart (§18). (3) **Runbook 3 no longer patches `replicas` blindly** — if an HPA owns the workload it patches the HPA target instead, and it has a hard ceiling plus a cost guard (§C.4). The auto-vs-approval column below is now the *default output* of the policy rules in §14.3 rather than a hardcoded property.

Each runbook is a Python function that receives an alert payload and executes a remediation action:

| # | Runbook | Trigger Alert | Action | Reversible? | Approval Required? |
|---|---|---|---|---|---|
| 1 | **CrashLoop Rollback** | `KubePodCrashLooping` | `kubectl rollout undo deployment/{name}` | Yes (can roll forward) | No — auto-execute |
| 2 | **Disk Cleanup** | `NodeDiskPressure` | Archive logs > 7 days to S3, delete archived | Partially (logs in S3) | No — auto-execute |
| 3 | **CPU Scale-Up** | `HighPodCPU` (> 80% for 5min) | Scale deployment replicas from N to N+2 | Yes (can scale back) | No — auto-execute |
| 4 | **OOMKill Restart** | `KubePodOOMKilled` | Increase memory limit by 50%, restart pod | Yes (can revert limit) | **Yes — Slack approval** |
| 5 | **Node Not Ready** | `KubeNodeNotReady` | Cordon node, drain pods to healthy nodes | Partially (cordon is reversible, drain is disruptive) | **Yes — Slack approval** |

**Key Design Principle:** Runbooks 1-3 are **safe and reversible** → execute automatically. Runbooks 4-5 are **potentially disruptive** → require Slack approval from a human before execution.

### What to Build (MVP)

**Must Have:**
- Alertmanager webhook endpoint receiving alert payloads
- Alert classification: match alert name to appropriate runbook
- 5 runbook implementations (as above)
- K8s API integration: rollback, scale, cordon, drain, patch resource limits
- S3/MinIO integration for log archival
- Slack notifications for every action (with Block Kit rich messages)
- Slack interactive approval for irreversible actions
- Audit log: every alert → decision → action → outcome stored in PostgreSQL
- Next.js dashboard: incident timeline, runbook execution history, success/failure rates
- Sample "chaos app" that we break intentionally to trigger alerts
- Helm chart for the entire platform
- Grafana dashboard for self-monitoring

**Nice to Have (Post-MVP):**
- Custom runbook YAML definitions (user-defined runbooks without code)
- Escalation chains: if auto-fix fails → page on-call engineer
- PagerDuty integration (as alternative to Slack)
- Runbook dry-run mode ("show me what WOULD happen without doing it")
- Incident post-mortem auto-generation
- Cost of downtime calculator (minutes of outage × $/min)
- OPA (Open Policy Agent) guardrails for runbook actions
- Multi-cluster support

### The Chaos App (What We're Breaking)

A deliberately fragile 3-service app designed to trigger every alert type:

| Service | Fault | How Triggered | Alert Fired |
|---|---|---|---|
| `crashy-api` | CrashLoopBackOff | Environment variable `CRASH_AFTER=30s` | KubePodCrashLooping |
| `hungry-worker` | OOM Kill | Allocates memory until killed | KubePodOOMKilled |
| `cpu-burner` | CPU spike | Runs tight loop on demand | HighPodCPU |
| `log-spammer` | Disk pressure | Writes 1GB/min to /var/log | NodeDiskPressure |
| `flaky-node` (simulated) | Node not ready | K8s node taint injection | KubeNodeNotReady |

### Week-by-Week Schedule

**Week 1 — Foundation: K8s, Prometheus, Alertmanager**
- Day 1–2: K8s cluster (kind), Prometheus stack via `kube-prometheus-stack` Helm chart
- Day 3–4: Configure Alertmanager: custom alert rules for all 5 scenarios
- Day 5: Build chaos app Docker images with configurable fault triggers
- Day 6: Deploy chaos app with Helm, verify alerts fire in Prometheus UI
- Day 7: Configure Alertmanager webhook receiver pointing to HealOps endpoint
- Deliverable: 5 alerts firing and routing to our webhook endpoint

**Week 2 — Python Runbook Engine (Core)**
- Day 1–2: FastAPI app with webhook endpoint `/api/webhooks/alertmanager`
- Day 3: Alert parser: extract alert name, labels, annotations, status (firing/resolved)
- Day 4: Runbook registry: map alert names to runbook functions
- Day 5: Runbook 1 — CrashLoop Rollback (kubernetes client: `rollout undo`)
- Day 6: Runbook 2 — Disk Cleanup (boto3: archive to S3/MinIO)
- Day 7: Runbook 3 — CPU Scale-Up (kubernetes client: `scale deployment`)
- Deliverable: 3 automatic runbooks execute on alerts

**Week 3 — Approval Workflow + Remaining Runbooks**
- Day 1–2: Slack Bolt SDK setup, OAuth flow, interactive message handling
- Day 3: Slack approval workflow: send approval request → wait for button click → execute
- Day 4: Runbook 4 — OOMKill Memory Increase (with Slack approval)
- Day 5: Runbook 5 — Node Cordon + Drain (with Slack approval)
- Day 6: Approval timeout: if no response in 10 minutes → escalate (send again with urgency)
- Day 7: "Was this correct?" feedback button on auto-executed runbooks
- Deliverable: Full approval workflow, all 5 runbooks functional

**Week 4 — Database + Audit Trail**
- Day 1–2: PostgreSQL schema, SQLAlchemy models, Alembic migrations
- Day 3: Audit logging: every alert → decision → action → outcome persisted
- Day 4: Incident timeline: link related alerts (same pod, same window) into one incident
- Day 5: Execution context: store full alert payload, K8s state before/after, runbook output
- Day 6: Redis caching: alert deduplication (same alert within 5 min → skip)
- Day 7: Redis Streams: runbook execution queue with acknowledgment
- Deliverable: Complete audit trail in PostgreSQL, Redis dedup + queue

**Week 5 — Next.js Dashboard**
- Day 1–2: Next.js scaffold, Tailwind + shadcn/ui, dark mode dashboard layout
- Day 3: Incident timeline (vertical timeline component with status icons)
- Day 4: Runbook execution detail page (before/after state, logs, timing)
- Day 5: KPI cards: total incidents, auto-resolved, human-escalated, MTTR (mean time to remediate)
- Day 6: Runbook success rate chart, runbook execution time distribution
- Day 7: Live incident feed (polling or SSE for real-time updates)
- Deliverable: Complete audit dashboard

**Week 6 — Helm Charts + Self-Healing Demo**
- Day 1–2: Helm chart for HealOps (runbook engine + dashboard + PostgreSQL + Redis)
- Day 3: Helm chart for chaos app with configurable fault parameters
- Day 4: Self-healing demo script: trigger all 5 faults in sequence, watch auto-fixes
- Day 5: Grafana dashboards: self-monitoring (remediation latency, success rate)
- Day 6–7: README with architecture diagram, screenshots, demo recording
- Deliverable: One-command deploy, self-healing demo

**Week 7 — CI/CD, Polish, Deploy**
- Day 1–2: GitHub Actions: lint → test → build → push → Helm deploy
- Day 3: Integration test in CI: deploy to kind → trigger chaos → verify remediation
- Day 4: Document scaling decisions
- Day 5: Deploy to cloud K8s cluster (GKE/EKS)
- Day 6: Record demo video: "Watch 5 incidents happen and 3 auto-resolve"
- Day 7: Polish, final testing, publish
- Deliverable: Live deployed, CI/CD, documentation complete

---

## 4. High-Level Design (HLD)

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      KUBERNETES CLUSTER                                  │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────┐           │
│  │            CHAOS APP (Intentionally Fragile)              │           │
│  │                                                          │           │
│  │  ┌────────────┐  ┌──────────────┐  ┌─────────────────┐   │           │
│  │  │crashy-api  │  │hungry-worker │  │cpu-burner       │   │           │
│  │  │(CrashLoop) │  │(OOM Kill)    │  │(CPU spike)      │   │           │
│  │  └────────────┘  └──────────────┘  └─────────────────┘   │           │
│  │  ┌────────────┐  ┌──────────────┐                         │           │
│  │  │log-spammer │  │flaky-node    │                         │           │
│  │  │(disk fill) │  │(node issues) │                         │           │
│  │  └────────────┘  └──────────────┘                         │           │
│  └──────────────────────────┬───────────────────────────────┘           │
│                             │ metrics                                    │
│                             ▼                                            │
│  ┌───────────────────────────────────────────────────┐                  │
│  │                OBSERVABILITY STACK                  │                  │
│  │                                                   │                  │
│  │  ┌─────────────┐   ┌──────────────────────────┐   │                  │
│  │  │ Prometheus   │   │ Alertmanager             │   │                  │
│  │  │              │──→│                          │   │                  │
│  │  │ Scrapes pods │   │ Routes: alert → webhook  │   │                  │
│  │  │ Evaluates    │   │ Groups: by namespace     │   │                  │
│  │  │ alert rules  │   │ Dedup: 5 min window      │   │                  │
│  │  └──────────────┘   └────────────┬─────────────┘   │                  │
│  │                                  │                  │                  │
│  │  ┌──────────────┐               │ webhook POST    │                  │
│  │  │   Grafana    │               │                  │                  │
│  │  │  (raw + ops) │               │                  │                  │
│  │  └──────────────┘               │                  │                  │
│  └──────────────────────────────────┼──────────────────┘                  │
│                                     │                                    │
│                                     ▼                                    │
│  ┌──────────────────────────────────────────────────────────┐           │
│  │               HEALOPS PLATFORM                            │           │
│  │                                                          │           │
│  │  ┌──────────────────────────────────────────────────┐     │           │
│  │  │  PYTHON RUNBOOK ENGINE (FastAPI)                  │     │           │
│  │  │                                                  │     │           │
│  │  │  ┌─────────────┐  ┌──────────────┐               │     │           │
│  │  │  │ Webhook     │  │ Alert        │               │     │           │
│  │  │  │ Receiver    │→ │ Classifier   │               │     │           │
│  │  │  └─────────────┘  └──────┬───────┘               │     │           │
│  │  │                          │                        │     │           │
│  │  │               ┌─────────▼──────────┐              │     │           │
│  │  │               │  Runbook Router    │              │     │           │
│  │  │               │  (match alert →    │              │     │           │
│  │  │               │   runbook)         │              │     │           │
│  │  │               └────────┬───────────┘              │     │           │
│  │  │                        │                          │     │           │
│  │  │         ┌──────────────┼──────────────┐           │     │           │
│  │  │         ▼              ▼              ▼           │     │           │
│  │  │  ┌────────────┐ ┌───────────┐ ┌────────────┐     │     │           │
│  │  │  │ Auto-Exec  │ │ Approval  │ │ Escalate   │     │     │           │
│  │  │  │ (safe)     │ │ Required  │ │ (unknown   │     │     │           │
│  │  │  │            │ │ (risky)   │ │  alert)    │     │     │           │
│  │  │  └──────┬─────┘ └─────┬─────┘ └──────┬─────┘     │     │           │
│  │  │         │              │              │           │     │           │
│  │  │         ▼              ▼              ▼           │     │           │
│  │  │  ┌──────────┐  ┌───────────┐  ┌──────────┐       │     │           │
│  │  │  │ Execute  │  │ Slack     │  │ Slack    │       │     │           │
│  │  │  │ Runbook  │  │ Approval  │  │ Page     │       │     │           │
│  │  │  │ + Notify │  │ Request   │  │ On-Call  │       │     │           │
│  │  │  └──────┬───┘  └─────┬─────┘  └──────────┘       │     │           │
│  │  │         │             │                           │     │           │
│  │  │         ▼             ▼ (on approve)              │     │           │
│  │  │  ┌─────────────────────────┐                      │     │           │
│  │  │  │  K8s API Actions        │                      │     │           │
│  │  │  │  - rollout undo         │                      │     │           │
│  │  │  │  - scale deployment     │                      │     │           │
│  │  │  │  - cordon/drain node    │                      │     │           │
│  │  │  │  - patch resource limits│                      │     │           │
│  │  │  │  - archive logs to S3   │                      │     │           │
│  │  │  └─────────────┬───────────┘                      │     │           │
│  │  │                │                                  │     │           │
│  │  │                ▼                                  │     │           │
│  │  │  ┌─────────────────────────┐                      │     │           │
│  │  │  │  Audit Logger           │                      │     │           │
│  │  │  │  (every action logged)  │                      │     │           │
│  │  │  └─────────────────────────┘                      │     │           │
│  │  └──────────────────────────────────────────────────┘     │           │
│  │                                                          │           │
│  │  ┌────────────────┐  ┌────────────┐  ┌────────────────┐   │           │
│  │  │ Next.js        │  │PostgreSQL  │  │ Redis 7       │   │           │
│  │  │ Dashboard      │  │16          │  │               │   │           │
│  │  │ (audit +       │  │(audit logs │  │ (alert dedup  │   │           │
│  │  │  timeline +    │  │ + incidents│  │  + exec queue │   │           │
│  │  │  KPIs)         │  │ + approvals│  │  + cache)     │   │           │
│  │  └────────────────┘  └────────────┘  └────────────────┘   │           │
│  └──────────────────────────────────────────────────────────┘           │
└─────────────────────────────────────────────────────────────────────────┘
                       │                          │
                       ▼                          ▼
              ┌────────────────┐         ┌─────────────────┐
              │  Slack         │         │  AWS S3 / MinIO │
              │                │         │  (log archive)  │
              │  - Notifications│         │                 │
              │  - Approvals    │         └─────────────────┘
              │  - Feedback     │
              └────────────────┘
```

### Core Request Flows

**Flow 1: Auto-Remediation (CrashLoopBackOff → Rollback)**
```
Step 1:  crashy-api pod enters CrashLoopBackOff (backoff increasing: 10s, 20s, 40s...)
    ↓
Step 2:  Prometheus evaluates alert rule every 15s:
         ALERT KubePodCrashLooping
         expr: rate(kube_pod_container_status_restarts_total[10m]) * 60 * 5 > 0
         for: 5m
         → Alert transitions to FIRING
    ↓
Step 3:  Alertmanager receives alert → matches route → sends webhook POST to:
         http://healops-engine:8000/api/webhooks/alertmanager
    ↓
Step 4:  HealOps Webhook Receiver:
         a) Validate payload structure
         b) Check Redis for deduplication (same alert within 5 min → skip)
         c) Store raw alert in Redis: "alert:dedup:{fingerprint}" (TTL: 300s)
    ↓
Step 5:  Alert Classifier:
         alertname = "KubePodCrashLooping" → maps to Runbook: CRASHLOOP_ROLLBACK
         approval_required = false (safe, reversible operation)
    ↓
Step 6:  Enqueue to Redis Stream: XADD runbook-executions * 
         runbookId CRASHLOOP_ROLLBACK alertPayload {json} approval NONE
    ↓
Step 7:  Runbook Executor picks up job:
         a) Record execution start in PostgreSQL (status: EXECUTING)
         b) Capture pre-state: kubectl get deployment {name} -o json
         c) Execute: kubernetes.client.AppsV1Api().patch_namespaced_deployment(
              name=deployment_name, namespace=namespace,
              body={"spec": {"rollbackTo": {"revision": 0}}}  # rollback to previous
            )
            OR: subprocess("kubectl rollout undo deployment/{name}")
         d) Wait 30 seconds
         e) Verify: check if pod is Running (not CrashLooping)
         f) Capture post-state: kubectl get deployment {name} -o json
    ↓
Step 8:  Record outcome in PostgreSQL:
         status: SUCCESS | FAILED | PARTIAL
         pre_state: {json}, post_state: {json}
         execution_time_ms: 32450
    ↓
Step 9:  Slack Notification (auto-remediation complete):
         ┌──────────────────────────────────────────────┐
         │ 🔧 Auto-Remediation Executed                  │
         │                                              │
         │ Alert: KubePodCrashLooping                   │
         │ Pod: crashy-api-7b9f4d-abc12                 │
         │ Namespace: chaos-app                         │
         │                                              │
         │ Action: Rolled back deployment to revision 3  │
         │ Status: ✅ SUCCESS — pod now Running          │
         │ Time: 32s                                     │
         │                                              │
         │ [👍 Correct]  [👎 Wrong — undo this]          │
         └──────────────────────────────────────────────┘
    ↓
Step 10: Dashboard updates: new entry in incident timeline
```

**Flow 2: Human-in-the-Loop Approval (OOMKill → Memory Increase)**
```
Step 1:  hungry-worker pod is OOM killed (exit code 137)
    ↓
Step 2:  Prometheus alert: KubePodOOMKilled fires
    ↓
Step 3:  HealOps classifies: OOMKILL_MEMORY_INCREASE → approval_required = true
    ↓
Step 4:  Slack Approval Request:
         ┌──────────────────────────────────────────────┐
         │ ⚠️ Approval Required — Irreversible Action    │
         │                                              │
         │ Alert: KubePodOOMKilled                      │
         │ Pod: hungry-worker-5c8d2b-xyz89              │
         │ Current Memory Limit: 512 MB                 │
         │ Proposed New Limit: 768 MB (+50%)            │
         │                                              │
         │ This will RESTART the pod with new limits.   │
         │                                              │
         │ [✅ Approve]  [❌ Reject]  [🔍 Show Details]  │
         └──────────────────────────────────────────────┘
    ↓
Step 5:  Execution state in Redis: "approval:{executionId}" = PENDING (TTL: 600s)
    ↓
Step 6a: IF approved (within 10 min):
         → Execute runbook: patch deployment with new memory limit
         → Verify: pod restarts with new limit, no OOMKill
         → Slack: "✅ Approved by @alice. Memory limit increased to 768 MB."
         → Audit log: approved_by: alice, approved_at: timestamp
    ↓
Step 6b: IF rejected:
         → Slack: "❌ Rejected by @bob. No action taken."
         → Audit log: rejected_by: bob, reason: "Known issue, will fix in code"
    ↓
Step 6c: IF timeout (10 min no response):
         → Slack: "⏰ Approval timed out. Escalating to on-call."
         → Resend with @channel mention and urgency flag
         → Audit log: status: TIMEOUT, escalated: true
```

**Flow 3: Disk Cleanup (Log Archival to S3)**
```
Step 1:  log-spammer writes 1GB/min → node disk usage crosses 85%
    ↓
Step 2:  Prometheus: NodeDiskPressure fires (node condition DiskPressure: true)
    ↓
Step 3:  HealOps Runbook: DISK_CLEANUP (auto-execute, no approval)
    ↓
Step 4:  Runbook execution:
         a) SSH to node (via kubectl exec on a DaemonSet helper pod)
         b) Find logs older than 7 days: find /var/log -type f -mtime +7
         c) Compress and upload to S3:
            aws s3 cp /var/log/pods/old-logs.tar.gz s3://healops-archives/{date}/
         d) Delete local archived files
         e) Verify: node disk usage dropped below 80%
    ↓
Step 5:  Slack: "🧹 Disk cleanup complete. Archived 4.2 GB to S3. 
               Disk usage: 85% → 62%"
    ↓
Step 6:  Audit log: action: DISK_CLEANUP, archived_size: 4.2GB, 
         s3_path: s3://healops-archives/2026-04-15/node-1/
```

### Alert Flow Decision Tree

```
ALERT RECEIVED
    │
    ├── Is this a DUPLICATE? (same fingerprint within 5 min)
    │   └── YES → Skip, log as "deduplicated"
    │
    ├── Is this a RESOLVED alert? (status = "resolved")
    │   └── YES → Update incident status, log "auto-resolved by platform"
    │
    ├── Does a RUNBOOK exist for this alert?
    │   ├── YES → Route to runbook
    │   │   ├── Is approval REQUIRED?
    │   │   │   ├── NO  → EXECUTE IMMEDIATELY → Notify Slack (post-action)
    │   │   │   └── YES → REQUEST APPROVAL via Slack
    │   │   │       ├── APPROVED  → Execute → Notify
    │   │   │       ├── REJECTED  → Log rejection → Close
    │   │   │       └── TIMEOUT   → Escalate → Re-notify with urgency
    │   │   │
    │   │   └── Execution complete → Log outcome → Update dashboard
    │   │
    │   └── NO  → ESCALATE: Notify Slack as "unhandled alert — needs human"
    │            Log as "no_runbook_available"
    │
    └── All paths → AUDIT LOG in PostgreSQL
```

---

## 5. Low-Level Design (LLD)

### 5.1 Project Structure

```
healops/
├── engine/                                    # Python runbook engine
│   ├── src/
│   │   ├── __init__.py
│   │   ├── main.py                            # FastAPI entrypoint
│   │   ├── config.py                          # Configuration from env vars
│   │   │
│   │   ├── api/
│   │   │   ├── webhooks.py                    # POST /api/webhooks/alertmanager
│   │   │   ├── approvals.py                   # POST /api/approvals/{id}
│   │   │   ├── health.py                      # GET /api/health
│   │   │   └── runbooks.py                    # GET /api/runbooks (list available)
│   │   │
│   │   ├── core/
│   │   │   ├── alert_classifier.py            # Match alert → runbook
│   │   │   ├── runbook_registry.py            # Registry of all runbooks
│   │   │   ├── runbook_executor.py            # Execution orchestrator
│   │   │   ├── approval_manager.py            # Slack approval workflow
│   │   │   ├── deduplicator.py                # Redis-based alert dedup
│   │   │   └── incident_correlator.py         # Group related alerts
│   │   │
│   │   ├── runbooks/                          # Individual runbook implementations
│   │   │   ├── base.py                        # Abstract Runbook class
│   │   │   ├── crashloop_rollback.py          # Runbook 1
│   │   │   ├── disk_cleanup.py                # Runbook 2
│   │   │   ├── cpu_scaleup.py                 # Runbook 3
│   │   │   ├── oomkill_memory.py              # Runbook 4
│   │   │   └── node_drain.py                  # Runbook 5
│   │   │
│   │   ├── k8s/
│   │   │   ├── client.py                      # Kubernetes API client wrapper
│   │   │   ├── deployment_ops.py              # Rollback, scale, patch
│   │   │   ├── pod_ops.py                     # Delete, logs, exec
│   │   │   ├── node_ops.py                    # Cordon, drain, uncordon
│   │   │   └── state_capture.py               # Capture before/after state
│   │   │
│   │   ├── integrations/
│   │   │   ├── slack_client.py                # Slack Bolt: notifications + approvals
│   │   │   ├── slack_messages.py              # Block Kit message builders
│   │   │   ├── s3_client.py                   # S3/MinIO operations
│   │   │   └── prometheus_client.py           # Query Prometheus for context
│   │   │
│   │   ├── db/
│   │   │   ├── connection.py                  # SQLAlchemy async engine
│   │   │   ├── models.py                      # ORM models
│   │   │   └── queries.py                     # Complex queries
│   │   │
│   │   └── cache/
│   │       ├── redis_client.py                # Redis connection
│   │       ├── dedup_store.py                 # Alert deduplication
│   │       └── approval_store.py              # Approval state management
│   │
│   ├── tests/
│   │   ├── test_alert_classifier.py
│   │   ├── test_runbook_executor.py
│   │   ├── test_crashloop_rollback.py
│   │   ├── test_approval_manager.py
│   │   ├── test_deduplicator.py
│   │   └── fixtures/
│   │       ├── alert_payloads/                # Sample Alertmanager payloads
│   │       └── k8s_states/                    # Mock K8s API responses
│   ├── Dockerfile
│   └── requirements.txt
│
├── dashboard/                                 # Next.js audit dashboard
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx
│   │   │   ├── page.tsx                       # Main incident timeline
│   │   │   ├── incidents/
│   │   │   │   └── [id]/page.tsx              # Incident detail
│   │   │   ├── runbooks/page.tsx              # Runbook catalog + stats
│   │   │   ├── approvals/page.tsx             # Pending approvals
│   │   │   └── api/
│   │   │       ├── incidents/route.ts         # GET incidents
│   │   │       ├── executions/route.ts        # GET runbook executions
│   │   │       ├── stats/route.ts             # GET KPI stats
│   │   │       └── health/route.ts
│   │   ├── components/
│   │   │   ├── timeline/
│   │   │   │   ├── IncidentTimeline.tsx       # Vertical incident timeline
│   │   │   │   ├── TimelineEvent.tsx          # Single event card
│   │   │   │   └── ExecutionDetail.tsx        # Before/after state diff
│   │   │   ├── dashboard/
│   │   │   │   ├── KPICards.tsx               # MTTR, success rate, auto-resolved%
│   │   │   │   ├── RunbookSuccessChart.tsx    # Per-runbook success/failure
│   │   │   │   ├── IncidentFrequency.tsx      # Incidents over time
│   │   │   │   └── ApprovalPending.tsx        # Pending approval cards
│   │   │   └── common/
│   │   │       ├── StatusBadge.tsx            # SUCCESS, FAILED, PENDING
│   │   │       ├── JsonDiff.tsx               # Before/after state diff viewer
│   │   │       └── LoadingSkeleton.tsx
│   │   ├── hooks/
│   │   │   ├── useIncidents.ts
│   │   │   ├── useExecutions.ts
│   │   │   └── useStats.ts
│   │   └── types/index.ts
│   ├── Dockerfile
│   └── package.json
│
├── chaos-app/                                 # Intentionally fragile services
│   ├── crashy-api/
│   │   ├── main.go                            # Crashes after configurable delay
│   │   └── Dockerfile
│   ├── hungry-worker/
│   │   ├── worker.py                          # Allocates memory until OOM
│   │   └── Dockerfile
│   ├── cpu-burner/
│   │   ├── burner.py                          # Tight CPU loop
│   │   └── Dockerfile
│   └── log-spammer/
│       ├── spammer.py                         # Writes massive log files
│       └── Dockerfile
│
├── charts/
│   ├── healops/                               # Main platform chart
│   │   ├── Chart.yaml
│   │   ├── values.yaml
│   │   └── templates/
│   │       ├── engine-deployment.yaml
│   │       ├── dashboard-deployment.yaml
│   │       ├── postgres-statefulset.yaml
│   │       ├── redis-deployment.yaml
│   │       ├── rbac.yaml                      # ServiceAccount + ClusterRole
│   │       ├── configmap.yaml
│   │       ├── secrets.yaml
│   │       ├── service.yaml
│   │       └── ingress.yaml
│   ├── chaos-app/                             # Chaos app chart
│   │   └── templates/
│   └── alerting-rules/                        # Prometheus alert rules
│       └── templates/
│           └── prometheusrule.yaml
│
├── k8s/
│   ├── prometheus/
│   │   └── kube-prometheus-stack-values.yaml  # Custom alert routing to HealOps
│   └── rbac/
│       ├── healops-clusterrole.yaml           # Permissions for K8s API operations
│       └── healops-serviceaccount.yaml
│
├── monitoring/
│   ├── grafana/
│   │   ├── healops-dashboard.json             # Self-monitoring dashboard
│   │   └── chaos-app-dashboard.json           # Chaos app metrics
│   └── alerting-rules.yml
│
├── .github/workflows/
│   ├── ci.yml
│   ├── deploy.yml
│   └── chaos-test.yml                         # Integration: trigger chaos, verify fix
│
├── docker-compose.yml
├── Makefile
└── README.md
```

### 5.2 Core: Abstract Runbook Base Class

> ⚠️ **Rev 2 replaces this contract — see §14.4 for the complete file.** The class below has `execute()` but no `plan()`, no `compensate()`, and no verification hook, which is how defect D1 (“success” = HTTP 200) got in. The Rev 2 contract is five methods: `plan`, `execute`, `compensate`, `extra_verification`, and a required `chaos_fixture`. Read the structure below for orientation, then implement §14.4.

```python
# engine/src/runbooks/base.py

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, Any
import time


class RunbookAction(Enum):
    AUTO_EXECUTE = "auto_execute"       # Safe, reversible — execute immediately
    APPROVAL_REQUIRED = "approval"      # Risky — wait for human approval
    ESCALATE = "escalate"               # Unknown — page on-call


class ExecutionStatus(Enum):
    QUEUED = "queued"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    SUCCESS = "success"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    ROLLED_BACK = "rolled_back"


@dataclass
class RunbookResult:
    status: ExecutionStatus
    message: str
    pre_state: Dict[str, Any]       # K8s state BEFORE remediation
    post_state: Dict[str, Any]      # K8s state AFTER remediation
    execution_time_ms: int
    actions_taken: list[str]        # Human-readable list of actions
    metadata: Dict[str, Any] = None


class Runbook(ABC):
    """
    Abstract base class for all runbooks.
    Every runbook implements: can_handle, action_type, execute, verify, rollback.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier: 'crashloop_rollback', 'disk_cleanup', etc."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description for Slack messages and audit logs."""
        ...

    @property
    @abstractmethod
    def action_type(self) -> RunbookAction:
        """AUTO_EXECUTE or APPROVAL_REQUIRED."""
        ...

    @abstractmethod
    def can_handle(self, alert: dict) -> bool:
        """Does this runbook handle the given alert? Match by alertname + labels."""
        ...

    @abstractmethod
    async def execute(self, alert: dict, context: dict) -> RunbookResult:
        """
        Execute the remediation action.
        Must capture pre/post state and return a RunbookResult.
        """
        ...

    @abstractmethod
    async def verify(self, alert: dict, context: dict) -> bool:
        """Verify the remediation was successful (e.g., pod is Running)."""
        ...

    async def rollback(self, alert: dict, context: dict) -> RunbookResult:
        """Optional: rollback the remediation if it made things worse."""
        return RunbookResult(
            status=ExecutionStatus.FAILED,
            message=f"Rollback not implemented for {self.name}",
            pre_state={}, post_state={},
            execution_time_ms=0, actions_taken=[]
        )
```

### 5.3 Runbook Implementation: CrashLoop Rollback

> ⚠️ **Rev 2 correction (defect D2): `rollout undo` is a *mitigation*, not a fix, on any Argo CD or Flux managed workload** — drift detection reverts it, and the next sync re-deploys the bad image. In Rev 2 this runbook classifies the target first: unmanaged → imperative with a TTL; GitOps-managed → ephemeral mitigation (with reconciliation paused for the TTL window) plus a **pull request** pinning the known-good image once verification passes. See §17.

```python
# engine/src/runbooks/crashloop_rollback.py

from .base import Runbook, RunbookAction, RunbookResult, ExecutionStatus
from ..k8s.client import K8sClient
from ..k8s.state_capture import capture_deployment_state
import asyncio
import time


class CrashLoopRollbackRunbook(Runbook):

    @property
    def name(self) -> str:
        return "crashloop_rollback"

    @property
    def description(self) -> str:
        return "Rolls back a CrashLooping deployment to its previous stable revision"

    @property
    def action_type(self) -> RunbookAction:
        return RunbookAction.AUTO_EXECUTE  # Safe: can roll forward again

    def can_handle(self, alert: dict) -> bool:
        return alert.get('labels', {}).get('alertname') == 'KubePodCrashLooping'

    async def execute(self, alert: dict, context: dict) -> RunbookResult:
        start = time.monotonic()
        k8s = K8sClient()
        actions = []

        namespace = alert['labels']['namespace']
        pod = alert['labels']['pod']

        # Extract deployment name from pod name (remove replicaset hash)
        deployment = self._extract_deployment_name(pod)

        try:
            # 1. Capture pre-state
            pre_state = await capture_deployment_state(k8s, deployment, namespace)
            current_revision = pre_state.get('revision', 'unknown')
            actions.append(f"Captured current state: revision {current_revision}")

            # 2. Check rollback history (don't rollback if already at revision 1)
            history = await k8s.get_rollout_history(deployment, namespace)
            if len(history.get('revisions', [])) < 2:
                return RunbookResult(
                    status=ExecutionStatus.FAILED,
                    message="No previous revision to rollback to",
                    pre_state=pre_state, post_state=pre_state,
                    execution_time_ms=int((time.monotonic() - start) * 1000),
                    actions_taken=["Checked rollout history — only 1 revision exists"]
                )

            # 3. Execute rollback
            await k8s.rollout_undo(deployment, namespace)
            actions.append(f"Executed: kubectl rollout undo deployment/{deployment}")

            # 4. Wait for rollout to complete (max 60 seconds)
            await k8s.wait_for_rollout(deployment, namespace, timeout=60)
            actions.append("Waited for rollout to stabilise")

            # 5. Wait additional 30s to confirm no CrashLoop
            await asyncio.sleep(30)
            actions.append("Waited 30s for crash confirmation window")

            # 6. Verify: pod should be Running now
            is_healthy = await self.verify(alert, context)

            # 7. Capture post-state
            post_state = await capture_deployment_state(k8s, deployment, namespace)

            elapsed = int((time.monotonic() - start) * 1000)

            if is_healthy:
                actions.append(f"Verification: pod is Running on revision {post_state.get('revision')}")
                return RunbookResult(
                    status=ExecutionStatus.SUCCESS,
                    message=f"Rolled back {deployment} from revision {current_revision} "
                            f"to {post_state.get('revision')}. Pod is now healthy.",
                    pre_state=pre_state,
                    post_state=post_state,
                    execution_time_ms=elapsed,
                    actions_taken=actions
                )
            else:
                actions.append("Verification FAILED: pod still CrashLooping after rollback")
                return RunbookResult(
                    status=ExecutionStatus.FAILED,
                    message=f"Rollback completed but pod is still unhealthy. "
                            f"Escalating to on-call.",
                    pre_state=pre_state,
                    post_state=post_state,
                    execution_time_ms=elapsed,
                    actions_taken=actions
                )

        except Exception as e:
            elapsed = int((time.monotonic() - start) * 1000)
            actions.append(f"ERROR: {str(e)}")
            return RunbookResult(
                status=ExecutionStatus.FAILED,
                message=f"Runbook failed: {str(e)}",
                pre_state=pre_state if 'pre_state' in dir() else {},
                post_state={},
                execution_time_ms=elapsed,
                actions_taken=actions
            )

    async def verify(self, alert: dict, context: dict) -> bool:
        """Check if the pod is Running and not restarting."""
        k8s = K8sClient()
        namespace = alert['labels']['namespace']
        deployment = self._extract_deployment_name(alert['labels']['pod'])

        pods = await k8s.get_deployment_pods(deployment, namespace)
        for pod in pods:
            status = pod.get('status', {}).get('phase')
            restarts = sum(
                cs.get('restartCount', 0)
                for cs in pod.get('status', {}).get('containerStatuses', [])
            )
            if status != 'Running' or restarts > 0:
                return False
        return True

    async def rollback(self, alert: dict, context: dict) -> RunbookResult:
        """Undo the rollback = roll forward to the version we rolled back FROM."""
        k8s = K8sClient()
        namespace = alert['labels']['namespace']
        deployment = self._extract_deployment_name(alert['labels']['pod'])
        # Roll forward (undo the undo)
        await k8s.rollout_undo(deployment, namespace)
        return RunbookResult(
            status=ExecutionStatus.ROLLED_BACK,
            message="Rolled forward to the original version (undid the rollback)",
            pre_state={}, post_state={},
            execution_time_ms=0,
            actions_taken=["kubectl rollout undo (forward)"]
        )

    def _extract_deployment_name(self, pod_name: str) -> str:
        """Extract deployment from pod: 'api-gateway-7b9f4d-abc12' → 'api-gateway'"""
        parts = pod_name.rsplit('-', 2)
        return parts[0] if len(parts) >= 3 else pod_name
```

### 5.4 RBAC — Kubernetes Permissions

```yaml
# k8s/rbac/healops-clusterrole.yaml
# HealOps needs specific K8s API permissions for remediation actions

apiVersion: v1
kind: ServiceAccount
metadata:
  name: healops-engine
  namespace: healops
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: healops-remediator
rules:
  # Read pods, deployments, nodes (for state capture)
  - apiGroups: ["", "apps"]
    resources: ["pods", "pods/log", "deployments", "replicasets", "nodes"]
    verbs: ["get", "list", "watch"]

  # Rollback deployments (rollout undo)
  - apiGroups: ["apps"]
    resources: ["deployments/rollback", "deployments"]
    verbs: ["patch", "update"]

  # Scale deployments (CPU scale-up)
  - apiGroups: ["apps"]
    resources: ["deployments/scale"]
    verbs: ["get", "patch", "update"]

  # Cordon/drain nodes
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["patch", "update"]  # For cordon (set unschedulable: true)

  # Evict pods (for drain)
  - apiGroups: [""]
    resources: ["pods/eviction"]
    verbs: ["create"]

  # Patch resource limits (for OOMKill memory increase)
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["patch"]

  # Exec into pods (for log cleanup on nodes)
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: healops-remediator-binding
subjects:
  - kind: ServiceAccount
    name: healops-engine
    namespace: healops
roleRef:
  kind: ClusterRole
  name: healops-remediator
  apiGroup: rbac.authorization.k8s.io
```

### 5.5 Alertmanager Configuration

```yaml
# k8s/prometheus/alertmanager-config.yaml

global:
  resolve_timeout: 5m

route:
  group_by: ['alertname', 'namespace']
  group_wait: 30s          # Wait 30s for related alerts to group
  group_interval: 5m       # Between groups of same alerts
  repeat_interval: 4h      # Re-send same alert after 4 hours
  receiver: 'healops-webhook'
  routes:
    # Route ALL firing alerts to HealOps
    - match_re:
        alertname: '.*'
      receiver: 'healops-webhook'

receivers:
  - name: 'healops-webhook'
    webhook_configs:
      - url: 'http://healops-engine.healops.svc.cluster.local:8000/api/webhooks/alertmanager'
        send_resolved: true     # Also notify when alert resolves
        max_alerts: 10          # Max alerts per webhook call
```

### 5.6 Prometheus Alert Rules

```yaml
# charts/alerting-rules/templates/prometheusrule.yaml

apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: healops-alerts
spec:
  groups:
    - name: healops.pod.rules
      rules:
        # Runbook 1: CrashLoopBackOff
        - alert: KubePodCrashLooping
          expr: |
            rate(kube_pod_container_status_restarts_total[10m]) * 60 * 5 > 0
          for: 5m
          labels:
            severity: critical
            runbook: crashloop_rollback
          annotations:
            summary: "Pod {{ $labels.pod }} is crash looping"
            description: "Pod {{ $labels.pod }} in {{ $labels.namespace }} has restarted {{ $value }} times in the last 10 minutes"

        # Runbook 3: High CPU
        - alert: HighPodCPU
          expr: |
            (sum by (pod, namespace) (rate(container_cpu_usage_seconds_total[5m]))
             / sum by (pod, namespace) (kube_pod_container_resource_requests{resource="cpu"}))
            > 0.80
          for: 5m
          labels:
            severity: warning
            runbook: cpu_scaleup
          annotations:
            summary: "Pod {{ $labels.pod }} CPU usage above 80%"

        # Runbook 4: OOM Killed
        - alert: KubePodOOMKilled
          expr: |
            kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} > 0
          for: 0m
          labels:
            severity: critical
            runbook: oomkill_memory
          annotations:
            summary: "Pod {{ $labels.pod }} was OOM killed"

    - name: healops.node.rules
      rules:
        # Runbook 2: Disk Pressure
        - alert: NodeDiskPressure
          expr: |
            kube_node_status_condition{condition="DiskPressure", status="true"} == 1
          for: 2m
          labels:
            severity: critical
            runbook: disk_cleanup
          annotations:
            summary: "Node {{ $labels.node }} has disk pressure"

        # Runbook 5: Node Not Ready
        - alert: KubeNodeNotReady
          expr: |
            kube_node_status_condition{condition="Ready", status="true"} == 0
          for: 5m
          labels:
            severity: critical
            runbook: node_drain
          annotations:
            summary: "Node {{ $labels.node }} is not ready"
```

### 5.7 Key API Endpoints

```
WEBHOOKS
  POST   /api/webhooks/alertmanager       # Alertmanager webhook receiver

APPROVALS
  POST   /api/approvals/{executionId}     # Slack sends approval/rejection here
         body: { action: "approve" | "reject", user: "alice" }

EXECUTIONS
  GET    /api/executions                  # List all runbook executions
         ?status=success&limit=50
  GET    /api/executions/{id}             # Execution detail (pre/post state)

INCIDENTS
  GET    /api/incidents                   # Correlated incident timeline
  GET    /api/incidents/{id}              # Full incident detail

RUNBOOKS
  GET    /api/runbooks                    # List available runbooks + stats
  GET    /api/runbooks/{name}/stats       # Per-runbook success rate, MTTR

STATS
  GET    /api/stats/summary               # KPIs: total, auto-resolved, MTTR

HEALTH
  GET    /api/health                      # Liveness + readiness
```

---

## 6. Database Design & Choice

### 6.1 Comparison Matrix

| Criteria | PostgreSQL 18 | TimescaleDB | MongoDB | Cassandra | CosmosDB |
|---|---|---|---|---|---|
| **Data Model** | Relational (alerts → executions → approvals) | Time-series + relational | Document | Wide-column | Multi-model |
| **Event Rate** | < 100 alerts/hour. Trivial for Postgres | Designed for 100K+ events/sec — overkill | Works but no JOINs | Designed for millions/sec — absurd | Overkill |
| **Relational Queries** | Native JOINs: "alert → execution → approval by user X" | Same (is Postgres) | $lookup (slow) | Not available | Depends on API |
| **JSONB** | Excellent (store alert payloads, K8s state) | Same | Native document model | No | Native |
| **Full-text Search** | tsvector for searching alert descriptions | Same | $text | No | No |
| **Audit Trail Integrity** | ACID transactions, FK constraints | Same | Single-doc ACID only | Eventual consistency | Configurable |
| **Operational Cost** | Low (single instance for this volume) | Same + extension overhead | Medium | High (3+ nodes) | High (cloud-only) |

### VERDICT: PostgreSQL 18 (Plain — NOT TimescaleDB)

**Why plain PostgreSQL wins over TimescaleDB for HealOps:**

This is a critical interview distinction. In my previous project (KubeThrifty), I chose TimescaleDB because the data was high-frequency metric snapshots — 40,000+ rows per pod per week requiring P95 aggregation over time windows. Here, our data is fundamentally different:

1. **Low-frequency events:** We receive < 100 alerts per hour. Each alert generates 1 execution record, possibly 1 approval record. That's < 300 rows/hour. PostgreSQL handles this without breaking a sweat — no hypertable partitioning needed.

2. **Relational integrity is critical for audit trails:** An alert → triggers an execution → requires an approval → has an outcome. This is a chain of foreign keys with cascading relationships. Audit trails MUST be ACID-compliant — an execution record must NEVER exist without its parent alert record. PostgreSQL's transaction guarantees are essential.

3. **Our queries are relational, not time-series:** "Show me all executions that were approved by Alice in the last 7 days" is a simple WHERE clause with JOINs. "Show me the MTTR for the crashloop_rollback runbook" is an AVG() with GROUP BY. These are standard SQL — no time-series functions needed.

4. **JSONB handles flexible payloads:** Alertmanager payloads vary per alert type. K8s state snapshots have different shapes per resource type. JSONB columns store these without rigid schemas, while remaining queryable.

**Interview answer:** "I chose plain PostgreSQL over TimescaleDB because my data is low-frequency events — under 100 alerts per hour — not high-frequency time-series metrics. TimescaleDB's hypertables, continuous aggregates, and compression are designed for millions of data points that need rolling-window aggregation. My queries are relational: 'show me all executions approved by Alice' or 'what's the MTTR for CrashLoop runbooks.' Standard SQL JOINs handle these perfectly. Adding TimescaleDB would add extension complexity for zero benefit at this event rate. In KubeThrifty, where I had 40,000+ metric rows per pod per week, TimescaleDB was essential. Here, it would be overengineering."

### 6.2 Schema Design

> **Rev 2 adds five tables and one migration on top of this schema (§C.6):** `remediation_plans`, `verifications`, the hash-chained `audit_ledger` (monthly partitions, `uuidv7()` keys, `UPDATE`/`DELETE` revoked), `runbook_promotion`, `remediation_budget`, and `remediation_recurrence`. The verdict below (plain PostgreSQL, not TimescaleDB) is unchanged and still correct — only the version moves, to **18.6**.

```sql
-- Alert events (received from Alertmanager)
CREATE TABLE alert_events (
    id                BIGSERIAL PRIMARY KEY,
    fingerprint       VARCHAR(64) NOT NULL,              -- Alertmanager fingerprint (for dedup)
    alertname         VARCHAR(200) NOT NULL,
    status            VARCHAR(20) NOT NULL,               -- firing, resolved
    severity          VARCHAR(20),                        -- critical, warning, info
    namespace         VARCHAR(200),
    pod_name          VARCHAR(200),
    node_name         VARCHAR(200),
    labels            JSONB NOT NULL DEFAULT '{}',
    annotations       JSONB NOT NULL DEFAULT '{}',
    full_payload      JSONB NOT NULL,                     -- Complete Alertmanager payload
    starts_at         TIMESTAMP NOT NULL,
    ends_at           TIMESTAMP,
    received_at       TIMESTAMP DEFAULT NOW(),
    is_deduplicated   BOOLEAN DEFAULT FALSE,
    incident_id       BIGINT REFERENCES incidents(id)
);
CREATE INDEX idx_alert_name ON alert_events(alertname);
CREATE INDEX idx_alert_time ON alert_events(received_at DESC);
CREATE INDEX idx_alert_finger ON alert_events(fingerprint);
CREATE INDEX idx_alert_namespace ON alert_events(namespace);

-- Incidents (correlated group of related alerts)
CREATE TABLE incidents (
    id                BIGSERIAL PRIMARY KEY,
    title             VARCHAR(500) NOT NULL,
    namespace         VARCHAR(200),
    affected_resource VARCHAR(300),                       -- "deployment/crashy-api" or "node/worker-1"
    status            VARCHAR(20) DEFAULT 'active',       -- active, resolved, escalated
    severity          VARCHAR(20),
    alert_count       INT DEFAULT 1,
    first_alert_at    TIMESTAMP NOT NULL,
    resolved_at       TIMESTAMP,
    resolution_method VARCHAR(30),                        -- auto_remediated, human_resolved, auto_resolved
    created_at        TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_incident_status ON incidents(status);

-- Runbook executions (core audit trail)
CREATE TABLE runbook_executions (
    id                BIGSERIAL PRIMARY KEY,
    alert_id          BIGINT REFERENCES alert_events(id) ON DELETE CASCADE,
    incident_id       BIGINT REFERENCES incidents(id),
    runbook_name      VARCHAR(100) NOT NULL,
    runbook_description TEXT,

    -- Execution lifecycle
    status            VARCHAR(30) NOT NULL DEFAULT 'queued',  -- queued, awaiting_approval,
                                                              -- executing, success, failed,
                                                              -- rejected, timeout, rolled_back
    action_type       VARCHAR(20) NOT NULL,                    -- auto_execute, approval

    -- K8s state capture
    pre_state         JSONB DEFAULT '{}',                      -- K8s state BEFORE action
    post_state        JSONB DEFAULT '{}',                      -- K8s state AFTER action
    actions_taken     JSONB DEFAULT '[]',                      -- Array of action descriptions

    -- Timing
    queued_at         TIMESTAMP DEFAULT NOW(),
    started_at        TIMESTAMP,
    completed_at      TIMESTAMP,
    execution_time_ms INT,

    -- Outcome
    success           BOOLEAN,
    error_message     TEXT,
    rollback_performed BOOLEAN DEFAULT FALSE,

    -- Context
    target_resource   VARCHAR(300),                             -- "deployment/api-gateway"
    target_namespace  VARCHAR(200),
    metadata          JSONB DEFAULT '{}'
);
CREATE INDEX idx_exec_runbook ON runbook_executions(runbook_name);
CREATE INDEX idx_exec_status ON runbook_executions(status);
CREATE INDEX idx_exec_time ON runbook_executions(queued_at DESC);

-- Approval records (for human-in-the-loop actions)
CREATE TABLE approvals (
    id                BIGSERIAL PRIMARY KEY,
    execution_id      BIGINT UNIQUE REFERENCES runbook_executions(id) ON DELETE CASCADE,
    slack_message_ts  VARCHAR(50),                              -- Slack message timestamp
    slack_channel     VARCHAR(50),
    status            VARCHAR(20) DEFAULT 'pending',            -- pending, approved, rejected, timeout
    requested_at      TIMESTAMP DEFAULT NOW(),
    responded_at      TIMESTAMP,
    responded_by      VARCHAR(100),                             -- Slack user who approved/rejected
    response_time_ms  INT,
    reason            TEXT,                                     -- Optional rejection reason
    timeout_seconds   INT DEFAULT 600                           -- 10 min default
);
CREATE INDEX idx_approval_status ON approvals(status);
CREATE INDEX idx_approval_exec ON approvals(execution_id);

-- Feedback records ("Was this correct?" button responses)
CREATE TABLE feedback (
    id                BIGSERIAL PRIMARY KEY,
    execution_id      BIGINT REFERENCES runbook_executions(id) ON DELETE CASCADE,
    is_correct        BOOLEAN NOT NULL,
    feedback_by       VARCHAR(100),                             -- Slack user
    comment           TEXT,
    created_at        TIMESTAMP DEFAULT NOW()
);

-- Runbook statistics (materialized view, refreshed periodically)
CREATE MATERIALIZED VIEW runbook_stats AS
SELECT
    runbook_name,
    COUNT(*) AS total_executions,
    COUNT(*) FILTER (WHERE status = 'success') AS successes,
    COUNT(*) FILTER (WHERE status = 'failed') AS failures,
    COUNT(*) FILTER (WHERE status = 'rejected') AS rejections,
    ROUND(AVG(execution_time_ms)) AS avg_execution_ms,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY execution_time_ms) AS p95_execution_ms,
    ROUND(
        COUNT(*) FILTER (WHERE status = 'success')::numeric /
        NULLIF(COUNT(*), 0) * 100, 1
    ) AS success_rate_pct,
    MAX(queued_at) AS last_execution
FROM runbook_executions
GROUP BY runbook_name;

-- Refresh every 5 minutes
-- (In production, triggered by pg_cron or application code)
```

### 6.3 Key Queries

```sql
-- 1. Incident timeline (dashboard main view)
SELECT i.id, i.title, i.status, i.severity, i.affected_resource,
       i.first_alert_at, i.resolved_at, i.resolution_method,
       i.alert_count,
       json_agg(json_build_object(
           'execution_id', re.id,
           'runbook', re.runbook_name,
           'status', re.status,
           'execution_time_ms', re.execution_time_ms,
           'actions', re.actions_taken
       )) AS executions
FROM incidents i
LEFT JOIN runbook_executions re ON re.incident_id = i.id
WHERE i.created_at > NOW() - INTERVAL '24 hours'
GROUP BY i.id
ORDER BY i.first_alert_at DESC;

-- 2. KPI: MTTR (Mean Time To Remediate) per runbook
SELECT runbook_name,
       ROUND(AVG(execution_time_ms) / 1000.0, 1) AS avg_mttr_seconds,
       COUNT(*) AS total_runs,
       success_rate_pct
FROM runbook_stats;

-- 3. Pending approvals (approval dashboard)
SELECT a.*, re.runbook_name, re.target_resource, re.target_namespace,
       ae.alertname, ae.annotations
FROM approvals a
JOIN runbook_executions re ON a.execution_id = re.id
JOIN alert_events ae ON re.alert_id = ae.id
WHERE a.status = 'pending'
ORDER BY a.requested_at ASC;

-- 4. Auto-resolved vs human-resolved ratio
SELECT
    COUNT(*) FILTER (WHERE resolution_method = 'auto_remediated') AS auto_resolved,
    COUNT(*) FILTER (WHERE resolution_method = 'human_resolved') AS human_resolved,
    COUNT(*) FILTER (WHERE status = 'escalated') AS escalated,
    COUNT(*) AS total
FROM incidents
WHERE created_at > NOW() - INTERVAL '30 days';

-- 5. Feedback analysis: are runbooks making correct decisions?
SELECT re.runbook_name,
       COUNT(*) FILTER (WHERE f.is_correct = true) AS correct,
       COUNT(*) FILTER (WHERE f.is_correct = false) AS incorrect,
       ROUND(
           COUNT(*) FILTER (WHERE f.is_correct = true)::numeric /
           NULLIF(COUNT(*), 0) * 100, 1
       ) AS accuracy_pct
FROM feedback f
JOIN runbook_executions re ON f.execution_id = re.id
GROUP BY re.runbook_name;
```

---

## 7. Caching & Messaging — Redis vs Kafka vs RabbitMQ vs Zookeeper

### VERDICT: Redis Streams — Valkey 9.1 (or Redis 8.x) — 4 Purposes

**Purpose 1 — Alert Deduplication**
```
Alertmanager may send the same alert multiple times (grouping, re-firing).
On alert receive:
  Key: "alert:dedup:{fingerprint}" → SET NX, TTL 300s (5 min)
  If SET succeeds → new alert, process it
  If SET fails → duplicate, skip it
```

**Purpose 2 — Runbook Execution Queue (Redis Streams)**
```
Stream: "runbook-executions"
Producer: Webhook handler enqueues:
  XADD runbook-executions * runbook crashloop_rollback alertId 42 approval none
Consumer: Executor picks up:
  XREADGROUP GROUP executors worker-1 COUNT 1 BLOCK 5000

Why queue (even for < 100 events/hour)?
- Prevents concurrent execution of conflicting runbooks (two rollbacks on same deployment)
- Enables retry: if K8s API call fails, message stays pending for retry
- Decouples webhook response from execution (webhook returns 200 immediately)
- Dashboard shows "queued" → "executing" → "completed" progression
```

**Purpose 3 — Approval State Cache**

> ⚠️ **Rev 2 correction (defect D3): approval state must NOT live only in Redis.** A TTL on a safety control means the safety control disappears when the cache restarts — and the executor cannot then distinguish “expired” from “lost”. In Rev 2 **PostgreSQL owns the approval decision** (`status` + an authoritative `expires_at`, reaped every 30s), Redis is a read-through cache for the Slack callback path, and `can_execute()` **fails closed**. See §C.3.
```
Key: "approval:{executionId}" → { status: "pending", requested_at: timestamp }
TTL: 600 seconds (10 min timeout)

On Slack button click:
  Update key: { status: "approved", approved_by: "alice" }
  Executor checks this key before executing approval-required runbooks
  
On TTL expiry → key disappears → executor detects timeout → escalate
```

**Purpose 4 — Dashboard Cache**
```
Key: "dashboard:timeline"        → Cached incident timeline (TTL: 30s)
Key: "dashboard:stats"           → Cached KPI cards (TTL: 60s)
Key: "runbook:stats:{name}"      → Per-runbook statistics (TTL: 5 min)
```

**Why NOT Kafka?** "Under 100 alerts per hour. Kafka's 3-broker cluster for 1-2 messages per minute is orders of magnitude over-provisioned."

**Why NOT RabbitMQ?** "Redis already handles dedup, approval state, and dashboard cache. Adding RabbitMQ for one feature (execution queue) means a separate Erlang service. Redis Streams handles it natively."

---

## 8. Design Patterns Used

### 8.1 Chain of Responsibility (Alert Processing Pipeline)

```python
class AlertHandler(ABC):
    def __init__(self):
        self._next: Optional[AlertHandler] = None

    def set_next(self, handler: 'AlertHandler') -> 'AlertHandler':
        self._next = handler
        return handler

    async def handle(self, alert: dict) -> Optional[dict]:
        result = await self.process(alert)
        if result is None and self._next:
            return await self._next.handle(alert)
        return result

    @abstractmethod
    async def process(self, alert: dict) -> Optional[dict]: ...

# Chain: Validate → Deduplicate → Classify → Route → Execute
chain = ValidationHandler()
chain.set_next(DeduplicationHandler()) \
     .set_next(ClassificationHandler()) \
     .set_next(RoutingHandler()) \
     .set_next(ExecutionHandler())

# On webhook: pass alert through the chain
result = await chain.handle(alert_payload)
```

**Why:** Each handler has a single responsibility. Deduplication doesn't know about classification. Routing doesn't know about validation. Adding a new handler (rate limiting, metric enrichment) = insert into the chain.

### 8.2 Strategy Pattern (Runbook Selection)

```python
class RunbookRegistry:
    def __init__(self):
        self._runbooks: List[Runbook] = []

    def register(self, runbook: Runbook):
        self._runbooks.append(runbook)

    def find_runbook(self, alert: dict) -> Optional[Runbook]:
        for runbook in self._runbooks:
            if runbook.can_handle(alert):
                return runbook
        return None  # No runbook → escalate to human

# Registration
registry = RunbookRegistry()
registry.register(CrashLoopRollbackRunbook())
registry.register(DiskCleanupRunbook())
registry.register(CPUScaleUpRunbook())
registry.register(OOMKillMemoryRunbook())
registry.register(NodeDrainRunbook())
```

### 8.3 Command Pattern (K8s Operations as Commands)

```python
@dataclass
class K8sCommand:
    action: str
    resource: str
    namespace: str
    params: dict

    async def execute(self, k8s: K8sClient) -> dict: ...
    async def undo(self, k8s: K8sClient) -> dict: ...
    def describe(self) -> str: ...

class RollbackCommand(K8sCommand):
    async def execute(self, k8s):
        return await k8s.rollout_undo(self.resource, self.namespace)
    async def undo(self, k8s):
        return await k8s.rollout_undo(self.resource, self.namespace)  # undo the undo
    def describe(self):
        return f"kubectl rollout undo {self.resource} -n {self.namespace}"

class ScaleCommand(K8sCommand):
    async def execute(self, k8s):
        return await k8s.scale(self.resource, self.namespace, self.params['replicas'])
    async def undo(self, k8s):
        return await k8s.scale(self.resource, self.namespace, self.params['original_replicas'])
```

### 8.4 Observer Pattern (Execution Event Propagation)

```python
# When a runbook completes, multiple systems react
event_bus.on('execution:completed', persist_to_database)
event_bus.on('execution:completed', send_slack_notification)
event_bus.on('execution:completed', update_incident_status)
event_bus.on('execution:completed', emit_prometheus_metric)
event_bus.on('execution:completed', invalidate_dashboard_cache)
event_bus.on('execution:failed', escalate_to_oncall)
```

### 8.5 Circuit Breaker (K8s API + Slack API)

```python
from circuitbreaker import circuit

@circuit(failure_threshold=3, recovery_timeout=60)
async def k8s_rollout_undo(deployment, namespace):
    return await k8s_client.patch_namespaced_deployment(...)

# If K8s API is unhealthy, circuit opens → runbooks fail fast
# → alerts escalate to human instead of timing out
```

### 8.6 Template Method (Runbook Execution Lifecycle)

The `Runbook` base class defines the execution template:
```
capture_pre_state() → execute() → wait() → verify() → capture_post_state() → log()
```
Subclasses override `execute()` and `verify()`. The lifecycle steps are fixed.

---

## 9. Docker & Kubernetes Deployment

### 9.1 Docker Compose (Local Dev)

```yaml
version: '3.8'

services:
  engine:
    build: ./engine
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    environment:
      - DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/healops
      - REDIS_URL=redis://redis:6379
      - SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN}
      - SLACK_SIGNING_SECRET=${SLACK_SIGNING_SECRET}
      - AWS_ACCESS_KEY_ID=minioadmin
      - AWS_SECRET_ACCESS_KEY=minioadmin
      - S3_ENDPOINT=http://minio:9000
      - S3_BUCKET=healops-archives
      - KUBECONFIG=/root/.kube/config
    volumes:
      - ~/.kube/config:/root/.kube/config:ro

  dashboard:
    build: ./dashboard
    ports:
      - "3000:3000"
    depends_on:
      - postgres
      - redis
    environment:
      - DATABASE_URL=postgresql://postgres:postgres@postgres:5432/healops
      - REDIS_URL=redis://redis:6379
      - ENGINE_URL=http://engine:8000

  postgres:
    image: postgres:18.6-alpine
    ports:
      - "5432:5432"
    environment:
      - POSTGRES_DB=healops
      - POSTGRES_USER=postgres
      - POSTGRES_PASSWORD=postgres
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./engine/sql/init.sql:/docker-entrypoint-initdb.d/01-init.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 10s

  redis:
    image: valkey/valkey:9-alpine
    ports:
      - "6379:6379"
    command: redis-server --maxmemory 128mb --maxmemory-policy allkeys-lru
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]

  minio:
    image: minio/minio:latest
    ports:
      - "9000:9000"
      - "9001:9001"
    environment:
      - MINIO_ROOT_USER=minioadmin
      - MINIO_ROOT_PASSWORD=minioadmin
    command: server /data --console-address ":9001"

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3001:3000"
    volumes:
      - ./monitoring/grafana:/etc/grafana/provisioning

volumes:
  postgres_data:
```

### 9.2 Helm Deployment

```yaml
# charts/healops/values.yaml

engine:
  replicaCount: 2
  image:
    repository: yourdockerhub/healops-engine
    tag: latest
  serviceAccount:
    create: true
    clusterRole: healops-remediator  # RBAC from Section 5.4
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      cpu: 500m
      memory: 512Mi

dashboard:
  replicaCount: 1
  image:
    repository: yourdockerhub/healops-dashboard
    tag: latest
  ingress:
    enabled: true
    host: healops.example.com

postgresql:
  enabled: true
  persistence:
    size: 5Gi

redis:
  enabled: true
  architecture: standalone

alertmanager:
  webhookUrl: "http://healops-engine:8000/api/webhooks/alertmanager"

slack:
  botToken:
    secretName: healops-slack
    secretKey: bot-token
  signingSecret:
    secretName: healops-slack
    secretKey: signing-secret

s3:
  endpoint: "https://s3.amazonaws.com"
  bucket: "healops-archives"
  credentials:
    secretName: healops-aws
```

---

## 10. CI/CD Pipeline

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]

jobs:
  test-engine:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:18.6-alpine
        env: { POSTGRES_DB: healops_test, POSTGRES_USER: postgres, POSTGRES_PASSWORD: postgres }
        ports: [5432:5432]
      redis:
        image: valkey/valkey:9-alpine
        ports: [6379:6379]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: cd engine && pip install -r requirements.txt -r requirements-dev.txt
      - run: cd engine && pytest tests/ -v --cov=src

  test-dashboard:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: '20' }
      - run: cd dashboard && npm ci && npm run lint && npm run test

  deploy:
    needs: [test-engine, test-dashboard]
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with:
          username: ${{ secrets.DOCKER_USERNAME }}
          password: ${{ secrets.DOCKER_PASSWORD }}
      - run: |
          docker build -t ${{ secrets.DOCKER_USERNAME }}/healops-engine:${{ github.sha }} ./engine
          docker build -t ${{ secrets.DOCKER_USERNAME }}/healops-dashboard:${{ github.sha }} ./dashboard
          docker push ${{ secrets.DOCKER_USERNAME }}/healops-engine:${{ github.sha }}
          docker push ${{ secrets.DOCKER_USERNAME }}/healops-dashboard:${{ github.sha }}
      - name: Helm deploy
        run: |
          helm upgrade --install healops ./charts/healops \
            --set engine.image.tag=${{ github.sha }} \
            --set dashboard.image.tag=${{ github.sha }} \
            --namespace healops --create-namespace
```

```yaml
# .github/workflows/chaos-test.yml — Integration: trigger faults, verify remediation
name: Chaos Integration Test
on:
  workflow_dispatch:
  schedule:
    - cron: '0 2 * * 1'  # Weekly at 2 AM Monday

jobs:
  chaos-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Create kind cluster
        uses: helm/kind-action@v1
      - name: Deploy full stack
        run: |
          helm install prometheus prometheus-community/kube-prometheus-stack
          helm install healops ./charts/healops
          helm install chaos-app ./charts/chaos-app
          kubectl wait --for=condition=ready pod -l app=healops-engine --timeout=120s
      - name: Trigger CrashLoop fault
        run: |
          kubectl set env deployment/crashy-api CRASH_AFTER=10s -n chaos-app
          echo "Waiting 5 minutes for alert to fire and remediation to execute..."
          sleep 300
      - name: Verify remediation
        run: |
          # Check that a rollback execution exists with SUCCESS status
          EXEC_STATUS=$(curl -s http://localhost:8000/api/executions?runbook=crashloop_rollback | jq -r '.[0].status')
          if [ "$EXEC_STATUS" != "success" ]; then
            echo "FAILED: Expected execution status 'success', got '$EXEC_STATUS'"
            exit 1
          fi
          echo "PASSED: CrashLoop was auto-remediated"
```

---

## 11. Monitoring — The "Scaling Decision" to Document

> **Rev 2 adds a second scaling decision (§16.5): bounded automation.** The decision below solves *conflicts* (two runbooks, one object) with per-resource locking and is still correct. §16.5 solves *volume* — correlation, a per-namespace remediation budget, and a global circuit breaker — because locks serialise a stampede rather than preventing it. Bring both; they are different problems and interviewers notice when you can tell them apart.

> **Scaling Decision: Synchronous K8s API Calls with Circuit Breaker over Async Event-Driven Remediation**
>
> **Problem:** When multiple alerts fire simultaneously (e.g., node failure triggers both `KubeNodeNotReady` AND `KubePodCrashLooping` for all pods on that node), the runbook engine could get overwhelmed making K8s API calls.
>
> **Option A — Synchronous with concurrency control:** Process runbooks sequentially per resource (no two runbooks modify the same deployment simultaneously). Use Redis distributed lock per resource. Circuit breaker on K8s API.
>
> **Option B — Fully async event-driven:** Each alert spawns an independent async task. Maximum parallelism but risk of conflicting operations (two runbooks scaling the same deployment simultaneously).
>
> **Decision:** Option A with per-resource locking. When CrashLoop AND OOMKill alerts fire for the same pod, the Redis lock `"lock:deployment:{name}"` ensures only one runbook modifies it at a time. The second waits or skips if the first already fixed the issue. Circuit breaker on the K8s API client prevents cascade failures if the API server is overloaded during a multi-pod incident.
>
> **Why not Option B:** Two runbooks simultaneously patching the same deployment creates a race condition — one rollbacks while the other increases memory. The final state is unpredictable. Sequential per-resource execution is deterministic.

---

## 12. Interview Prep — Top Questions & Answers

> **§27 adds 14 more Q&As** covering the Rev 2 features — including the three you are most likely to be asked: “isn’t this just Robusta?”, “how do you know the remediation actually worked?”, and “doesn’t self-healing hide real bugs?”

**Q1: "Walk me through the architecture."**

> "Prometheus monitors all pods in the cluster and evaluates alert rules every 15 seconds. When an alert fires — say KubePodCrashLooping — it transitions to FIRING after the configured `for` duration, then Alertmanager receives it, groups related alerts, and sends a webhook POST to my FastAPI-based runbook engine. The engine deduplicates the alert using Redis, classifies it by matching the alertname to a registered runbook, and checks if the runbook requires human approval. For safe operations like rollback, it executes immediately via the Kubernetes Python client, captures before/after state, and sends a Slack notification. For risky operations like increasing memory limits, it sends a Slack approval request with interactive buttons and waits up to 10 minutes. Every action is audit-logged in PostgreSQL with pre/post K8s state, execution time, and outcome."

**Q2: "Why do some runbooks need approval and others don't?"**

> "It's about reversibility and blast radius. A `rollout undo` is safe — if the rollback is wrong, we can roll forward again. Scaling up replicas is safe — we can scale back down. These execute automatically. But increasing a memory limit restarts the pod with new parameters — if the limit is wrong, we've just disrupted a running service. Draining a node moves ALL pods off that node — if the remaining nodes can't handle the load, we've caused a wider outage. These require human judgment. The design principle is: automate what's safe, escalate what's risky. This mirrors how real SRE teams operate."

**Q3: "How do you prevent two runbooks from conflicting?"**

> "Redis distributed locks per resource. Before any runbook modifies a deployment, it acquires `lock:deployment:{namespace}/{name}` with a 120-second TTL. If another runbook for the same deployment is already executing, the second one waits or skips. This prevents race conditions like one runbook rolling back while another increases memory on the same deployment."

**Q4: "Why PostgreSQL over TimescaleDB?"**

> "Our event rate is under 100 alerts per hour — that's low-frequency event data, not high-frequency time-series metrics. TimescaleDB's hypertables, continuous aggregates, and compression are designed for millions of data points with rolling-window aggregation. My queries are relational: 'which runbook executions were approved by Alice?' or 'what's the MTTR for CrashLoop remediations?' Standard SQL JOINs handle these perfectly. In my KubeThrifty project, where I had 40,000+ metric rows per pod per week, TimescaleDB was the right choice. Here it would be unnecessary complexity."

**Q5: "How does the Slack approval flow work technically?"**

> "When a runbook requires approval, I send a Slack Block Kit message with interactive buttons (Approve/Reject) via the Slack Bolt SDK. The button payload includes a callback_id containing the execution_id. When a user clicks Approve, Slack sends a POST to my `/api/approvals/{executionId}` endpoint with the user's identity and action. I verify the Slack signing secret, update the approval state in both Redis and PostgreSQL, and trigger the runbook execution. If nobody responds within 10 minutes, the Redis key expires (TTL: 600s) — the executor detects the timeout and escalates with an @channel mention. Every approval, rejection, and timeout is audit-logged."

**Q6: "What happens when a runbook fails?"**

> "Three things. First, the execution is marked as FAILED in PostgreSQL with the error message and partial state. Second, a Slack notification is sent: 'Auto-remediation FAILED for CrashLoop on api-gateway. Error: timeout waiting for rollout. Escalating to on-call.' Third, the incident is escalated — if there's a PagerDuty or on-call integration, it triggers. The key insight: a failed auto-remediation is BETTER than no auto-remediation, because we now have diagnostic data — pre-state, the exact error, partial post-state — that an on-call engineer can use to fix the issue faster."

**Q7: "What RBAC permissions does HealOps need?"**

> "A ClusterRole with specific, minimal permissions. It can GET and LIST pods, deployments, and nodes for state capture. It can PATCH deployments for rollback, scaling, and resource limit changes. It can CREATE pod evictions for node drain. It CANNOT delete deployments, modify RBAC, or access secrets. This follows the principle of least privilege — HealOps can remediate but can't do anything destructive that a runbook doesn't explicitly need."

**Q8: "The killer demo question — describe your demo."**

> "I trigger 5 faults in sequence. CrashLoop: within 5 minutes, HealOps rolls back automatically and Slack shows success. Disk pressure: logs archived to S3 in 30 seconds, Slack shows '4.2 GB archived, disk 85% → 62%.' High CPU: deployment scaled from 2 to 4 replicas, CPU drops below threshold. OOMKill: Slack approval request appears — I click Approve — memory limit increases, pod restarts healthy. Node NotReady: Slack approval appears — I click Approve — node cordoned, pods drained to healthy nodes. Final scorecard: 3 auto-resolved, 2 human-approved. The system fixed 3 out of 5 incidents without waking anyone up. That's the demo moment."

---

## 13. Deployment Checklist

- [ ] K8s cluster with Prometheus stack deployed
- [ ] All 5 alert rules firing correctly in Prometheus
- [ ] Alertmanager routing webhooks to HealOps
- [ ] All 5 runbooks executing correctly
- [ ] Slack notifications working (auto + approval)
- [ ] Approval workflow: approve, reject, timeout all tested
- [ ] Audit log: every action stored with pre/post state
- [ ] Dashboard: incident timeline, KPIs, runbook stats
- [ ] CI/CD pipeline green
- [ ] Chaos integration test passing
- [ ] Helm chart deploys cleanly
- [ ] Demo script runs all 5 faults → 3 auto-fix, 2 approved
- [ ] README: architecture diagram, screenshots, setup steps
- [ ] Record demo video showing the "3 out of 5" moment

### Cost Estimate

| Service | Provider | Cost |
|---|---|---|
| GKE Autopilot (small cluster) | Google Cloud | ~$70/mo (free $300 credit) |
| OR kind on EC2 t3.medium | AWS | ~$30/mo |
| OR local minikube | Local | $0 |
| Slack App | Slack | Free |
| MinIO (dev S3) | Self-hosted | Free |
| **Total** | | **$0-70/month** |

---

## C. Rev 2 Corrections — The Five Defects

Rev 1 was reviewed as if a staff SRE were signing off on giving it write access to production. Five findings were real defects, not polish. Fixing them in public — in the README and in the interview — is worth more than never having had them.

### C.1 Summary

| # | Where | Defect | What happens in production | Fix |
|---|---|---|---|---|
| **D1** | §5.2, §5.3 | **No verification.** `execute()` calls the K8s API, gets a 200, writes `status=SUCCESS`, posts to Slack | A `rollout undo` that rolls back to an equally broken revision is recorded as a **success**. The pod keeps crash-looping, the alert re-fires, dedup suppresses it for 5 minutes, and the dashboard says "auto-resolved". This is worse than no automation, because it produces false confidence | Closed-loop verification + compensation (**§15**) |
| **D2** | §2 "why not ArgoCD", §5.3 | **GitOps-blind imperative fixes.** `kubectl rollout undo` / `scale` on a workload Argo CD or Flux manages | Drift detection reverts the fix (often within 3 minutes), or the next sync silently re-deploys the bad image. Rev 1's "why not ArgoCD" row says "different trigger, different use case" — which misses that the two systems **fight each other** | Ephemeral-vs-durable classification, sync-window awareness, PR for durable fixes (**§17**) |
| **D3** | §7 Purpose 3 | **Approval state only in Redis with a TTL.** `approval:{executionId}` TTL 600s; "on TTL expiry → key disappears → executor detects timeout" | Restart Redis (or evict under memory pressure) and every pending approval vanishes. The executor cannot distinguish *"expired"* from *"never existed"* from *"lost"* — so it either fails open (executes a disruptive action nobody approved) or silently drops incidents | PostgreSQL is the source of truth; Redis is a cache; expiry is a DB timestamp reaped by a job; the executor **fails closed** (**§C.3**) |
| **D4** | §3 Runbook 3 | **Unbounded scale-up that can fight an HPA.** "Scale deployment replicas from N to N+2" | If the workload has an HPA, the HPA owns `spec.replicas` and reverts your scale within one sync — or oscillates against it. If it has no HPA, repeated firings walk replicas up with no ceiling, and a CPU alert becomes a cost incident | Autoscaler ownership detection, adjust the *target* not the replicas, hard ceiling, cost guard (**§C.4**) |
| **D5** | §7 Purpose 1, §11 | **Storm amplification.** Dedup is per-fingerprint (5 min); conflicts are handled with per-resource locks | One node failure produces `KubeNodeNotReady` **plus** `KubePodCrashLooping` for all 40 pods on it. Fingerprints all differ, so nothing dedups; locks are per-*resource*, so all 40 execute — just politely, one at a time. You have automated a stampede | Cause-first correlation, incident grouping, remediation budget, circuit breaker (**§16**) |

> Interview soundbite: *"The most useful review I did was on my own project. The worst thing I found was that my 'success' label meant 'the API call returned 200' — not 'the incident is over.' A rollback to an equally broken revision was recorded as a successful auto-remediation. That's not a bug in a runbook; that's a bug in the definition of done. So Rev 2 verifies the alert cleared and the golden signals recovered before it calls anything a success, and it reverts itself when they don't."*

### C.2 D1 — what "success" must mean

```
Rev 1:  execute() -> API 200 -> status = SUCCESS -> Slack "fixed!"
Rev 2:  plan() -> policy gate -> execute() -> verify(window) -> {
             recovered      -> status = REMEDIATED       (alert cleared + signals healthy)
             not recovered  -> compensate() -> status = REVERTED, escalate to human
             uncertain      -> status = INCONCLUSIVE, escalate (never SUCCESS)
        }
```

Three states, not two, and the third one matters: an honest `INCONCLUSIVE` is what keeps the scorecard trustworthy. Full implementation in **§15**.

### C.3 D3 — approval state, corrected

Approvals are a **safety control**. A safety control cannot live only in a cache.

```python
# The rule: PostgreSQL is the source of truth. Redis is a read-through cache and a notifier.
# Expiry is a TIMESTAMP in the database, not a TTL that deletes the evidence.

# approvals table (see §C.6)
#   status IN ('pending','approved','rejected','expired')
#   expires_at TIMESTAMPTZ NOT NULL          -- authoritative deadline
#   decided_by TEXT, decided_at TIMESTAMPTZ  -- who, when (audit)
#   slack_message_ts TEXT                    -- so we can update the original message

def can_execute(execution_id: str) -> tuple[bool, str]:
    """Fail CLOSED. An unknown or unreadable approval is a refusal, never a permission."""
    row = db.fetch_approval(execution_id)          # Postgres, not Redis
    if row is None:
        return False, "no approval record — refusing (fail closed)"
    if row.status == "approved" and row.decided_at <= row.expires_at:
        return True, f"approved by {row.decided_by}"
    if row.status == "pending" and utcnow() > row.expires_at:
        db.mark_expired(execution_id)              # reaper also does this every 30s
        return False, "approval window expired — escalating"
    return False, f"approval status={row.status}"
```

Redis keeps `approval:{id}` as a 10-minute cache purely so the Slack callback path is fast, and the **reaper** (a 30-second loop, or `pg_cron`) transitions `pending → expired` in the database so the state machine is driven by durable data. PostgreSQL 18's **temporal constraints** are a neat fit here: model the approval's validity as a range and let the database refuse two overlapping approvals for the same execution.

**Why this matters in an interview:** it's the difference between "I used Redis TTL for timeouts" (fine) and "I realised a TTL on a safety control means the safety control disappears when the cache restarts, so the database owns the decision and the executor fails closed" (senior).

### C.4 D4 — the scale-up runbook, corrected

```python
# runbooks/cpu_scale_up.py — decision logic (execution details in §14.4)
MAX_REPLICAS_CEILING = 10          # hard cap; a CPU alert must never become a cost incident
MAX_SCALE_FACTOR = 2.0             # never more than double in one action
COST_APPROVAL_THRESHOLD_INR = 500  # projected extra ₹/month above this -> require approval

def plan_scale_up(target, alert):
    hpa = find_autoscaler(target.namespace, target.name)     # HPA or KEDA-owned HPA

    if hpa and hpa.metric in ("cpu", "memory"):
        # The HPA owns spec.replicas. Patching replicas is a no-op at best and an
        # oscillation at worst. Act on the CONTROL, not on the OUTPUT.
        return Plan(
            action="patch_hpa_target",
            detail=f"lower averageUtilization {hpa.target}% -> {max(50, hpa.target - 15)}%,"
                   f" raise maxReplicas if it is the binding constraint",
            reversible=True,
            note="HPA owns spec.replicas; scaling it directly would be reverted within one sync",
        )

    if hpa and hpa.metric == "external":
        return Plan.refuse("workload scales on an external metric (queue depth) — "
                           "CPU-based scale-up would fight the real signal")

    desired = min(int(target.replicas * MAX_SCALE_FACTOR),
                  target.replicas + 2, MAX_REPLICAS_CEILING)
    if desired <= target.replicas:
        return Plan.refuse(f"already at the ceiling ({MAX_REPLICAS_CEILING} replicas) — "
                           "this is a capacity decision, not an incident")

    projected = cost_estimate(target, desired - target.replicas)   # ₹/month, pinned price list
    return Plan(
        action="scale", desired_replicas=desired, reversible=True,
        blast_radius=f"{desired - target.replicas} new pods in {target.namespace}",
        requires_approval=projected > COST_APPROVAL_THRESHOLD_INR,
        cost_note=f"≈ ₹{projected:.0f}/month if left in place; ephemeral TTL 2h (§17)",
    )
```

Three ideas an interviewer will notice: **act on the control loop, not its output** (patch the HPA target, not `replicas`); **every automated scale-up has a ceiling**; and **cost is a first-class approval criterion**, because "automation with no budget" is how a self-healing system produces a surprise invoice.

### C.5 D5 — the storm, illustrated

```
Node ip-10-0-3-14 goes NotReady at 02:14.

Rev 1 behaviour:
  KubeNodeNotReady           -> Runbook 5 (cordon+drain)      -> approval requested
  KubePodCrashLooping × 40   -> Runbook 1 (rollout undo) × 40 -> 40 rollbacks, serialised
  Result: 40 unnecessary rollbacks of healthy deployments whose only problem was
          living on a dead node. Every one of them "succeeded".

Rev 2 behaviour:
  Correlate by topology: all 40 pod alerts have node=ip-10-0-3-14, which has an
  active NodeNotReady alert -> they are SYMPTOMS.
  -> One incident. One plan: cordon + drain the node (approval).
  -> The 40 pod alerts are attached to the incident as evidence, not as triggers.
  -> If the node recovers, the symptom alerts clear on their own and nothing was touched.
```

That is **cause-first remediation** (§16), and it is the single most convincing thing you can say about storm handling, because every fresher's demo works with one alert at a time.

### C.6 Amendments to §6.2 — new tables (complete migration)

```sql
-- migrations/002_rev2.sql   (PostgreSQL 18)
-- Rev 2 adds: plans, verifications, the hash-chained ledger, promotion state, budgets.

-- 1. RemediationPlan (§14): what we intend to do, before we do it.
CREATE TABLE remediation_plans (
    id                UUID PRIMARY KEY DEFAULT uuidv7(),     -- PG18: timestamp-ordered
    incident_id       BIGINT NOT NULL REFERENCES incidents(id),
    runbook           TEXT NOT NULL,
    target_kind       TEXT NOT NULL,                          -- Deployment | Node | Pod
    target_namespace  TEXT,
    target_name       TEXT NOT NULL,
    actions           JSONB NOT NULL,                         -- ordered API calls, exact payloads
    compensation      JSONB NOT NULL,                         -- how to undo, decided BEFORE acting
    reversibility     TEXT NOT NULL CHECK (reversibility IN
                          ('reversible','partially_reversible','irreversible')),
    blast_radius      JSONB NOT NULL,                         -- {pods:n, nodes:n, namespaces:[...]}
    durability        TEXT NOT NULL CHECK (durability IN ('ephemeral','durable')),  -- §17
    ttl_seconds       INT,                                    -- ephemeral fixes expire
    expected_signal   JSONB NOT NULL,                         -- what recovery looks like (§15)
    confidence        NUMERIC(4,3) NOT NULL,                  -- calibrated (§19)
    policy_decision   TEXT NOT NULL CHECK (policy_decision IN ('allow','require_approval','deny')),
    policy_reasons    TEXT[] NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON remediation_plans (incident_id, created_at DESC);

-- 2. Verification outcome (§15): did it actually work?
CREATE TABLE verifications (
    id              UUID PRIMARY KEY DEFAULT uuidv7(),
    execution_id    BIGINT NOT NULL REFERENCES runbook_executions(id) ON DELETE CASCADE,
    window_seconds  INT NOT NULL,
    alert_cleared   BOOLEAN,                                  -- NULL = not observed
    signals_before  JSONB NOT NULL,
    signals_after   JSONB NOT NULL,
    outcome         TEXT NOT NULL CHECK (outcome IN
                        ('remediated','not_recovered','inconclusive')),
    compensated     BOOLEAN NOT NULL DEFAULT FALSE,
    reasons         TEXT[] NOT NULL DEFAULT '{}',
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON verifications (execution_id);

-- 3. The tamper-evident ledger (§20). Append-only, hash-chained, monthly partitions.
CREATE TABLE audit_ledger (
    seq             BIGSERIAL,
    id              UUID NOT NULL DEFAULT uuidv7(),
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor           TEXT NOT NULL,                            -- 'healops' | slack user id
    event_type      TEXT NOT NULL,                            -- plan|policy|execute|verify|compensate|approve|promote
    incident_id     BIGINT,
    execution_id    BIGINT,
    payload         JSONB NOT NULL,
    prev_hash       CHAR(64) NOT NULL,
    hash            CHAR(64) NOT NULL,                        -- sha256(prev_hash || canonical(payload))
    bundle_sha256   CHAR(64),                                 -- signed evidence bundle, if any
    PRIMARY KEY (seq, occurred_at)
) PARTITION BY RANGE (occurred_at);
CREATE TABLE audit_ledger_2026_08 PARTITION OF audit_ledger
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
CREATE UNIQUE INDEX ON audit_ledger (hash);
-- Append-only enforcement: no UPDATE, no DELETE for the app role.
REVOKE UPDATE, DELETE ON audit_ledger FROM healops_app;

-- 4. Promotion ladder state (§19): per (runbook, namespace).
CREATE TABLE runbook_promotion (
    runbook             TEXT NOT NULL,
    namespace           TEXT NOT NULL,
    mode                TEXT NOT NULL CHECK (mode IN ('shadow','approval','auto')),
    consecutive_good    INT NOT NULL DEFAULT 0,
    total_executions    INT NOT NULL DEFAULT 0,
    total_reverted      INT NOT NULL DEFAULT 0,
    last_outcome        TEXT,
    last_changed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_reason      TEXT,
    PRIMARY KEY (runbook, namespace)
);

-- 5. Remediation budget (§16): token bucket per namespace, plus the global breaker.
CREATE TABLE remediation_budget (
    scope           TEXT PRIMARY KEY,                         -- 'global' | 'ns:<name>'
    window_start    TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_seconds  INT NOT NULL DEFAULT 3600,
    max_actions     INT NOT NULL,
    used_actions    INT NOT NULL DEFAULT 0,
    breaker_state   TEXT NOT NULL DEFAULT 'closed'
                        CHECK (breaker_state IN ('closed','open','half_open')),
    breaker_reason  TEXT,
    breaker_until   TIMESTAMPTZ
);

-- 6. Recurrence tracking for the anti-masking guard (§16.4).
CREATE TABLE remediation_recurrence (
    runbook         TEXT NOT NULL,
    target_key      TEXT NOT NULL,                            -- ns/kind/name
    window_start    DATE NOT NULL,
    occurrences     INT NOT NULL DEFAULT 1,
    issue_url       TEXT,                                     -- GitHub issue we filed
    PRIMARY KEY (runbook, target_key, window_start)
);

-- 7. Executions gain the Rev 2 fields.
ALTER TABLE runbook_executions
    ADD COLUMN IF NOT EXISTS plan_id UUID REFERENCES remediation_plans(id),
    ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'auto',   -- shadow|approval|auto
    ADD COLUMN IF NOT EXISTS durability TEXT,                     -- ephemeral|durable
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,              -- ephemeral fix TTL (§17)
    ADD COLUMN IF NOT EXISTS pr_url TEXT,                         -- durable fix (§17)
    ADD COLUMN IF NOT EXISTS gitops_managed BOOLEAN NOT NULL DEFAULT FALSE;
```

Note the deliberate choices to defend: **`uuidv7()` for ledger and plan IDs** (timestamp-ordered, so index locality is good and a hash chain reads chronologically); **partitioning the ledger by month** (an append-only table that only ever grows should be partitioned before it needs to be); and **`REVOKE UPDATE, DELETE`** — an audit table the application can rewrite is not an audit table.

### C.7 Amendment to §7 — the Zookeeper question, answered

The brief asked about Redis vs Kafka vs **Zookeeper** vs RabbitMQ, and Rev 1 skipped Zookeeper. The answer is a category correction, which is the best kind:

| Option | What it actually is | Verdict for HealOps |
|---|---|---|
| **Redis / Valkey Streams** | In-memory data store with a log type, consumer groups, `SET NX` locks, TTLs | ✅ **Chosen.** One dependency covers the queue, dedup, locks, and the dashboard cache at < 100 alerts/hour |
| Kafka | Distributed, partitioned, replicated commit log | ❌ Built for millions of events/sec and durable replay across consumers. At 1–2 messages/minute a 3-broker cluster is a punchline. (Worth knowing: **Kafka no longer needs Zookeeper** — KRaft replaced it) |
| **Zookeeper** | **Not a message queue at all** — a coordination service (consensus, leader election, distributed config) | ❌ **Category error to compare.** If I needed leader election across HealOps replicas I'd use the **Kubernetes `Lease` API**, which is already in the cluster, instead of running a Zookeeper ensemble |
| RabbitMQ | AMQP broker with rich routing, per-message ack, DLQ | ❌ A reasonable fit technically, but it adds an Erlang service to do one job Redis Streams already does — and I'd still need Redis for locks, dedup, and cache |

> Interview soundbite: *"Zookeeper isn't a queue — it's a coordination service, and comparing it to Redis or RabbitMQ is a category error. If I needed leader election between HealOps replicas, I wouldn't add Zookeeper; I'd use the Kubernetes Lease API that's already in the cluster. Worth noting that Kafka itself dropped Zookeeper for KRaft."*

### C.8 Amendments to §8 — four patterns Rev 2 adds

| Pattern | Rev 2 use | Why it earns its place |
|---|---|---|
| **Saga / compensating transaction** | Every plan registers its `compensation` **before** executing; verification failure triggers it; an expiry watchdog triggers it if the process dies | The textbook answer to "a distributed action you must be able to undo without a rollback log" |
| **Policy as code (CEL)** | The plan gate evaluates declarative rules against the plan object (§14.3) | Rules are testable and reviewable in git; adding a guardrail doesn't mean editing runbook code |
| **Event sourcing / hash-chained ledger** | Every state transition is an append-only ledger event; current state is a projection | Tamper-evidence and perfect replay; the audit story becomes a design property rather than a log file |
| **Token bucket + circuit breaker (as policy, not just transport)** | Remediation budget per namespace and a global breaker (§16.3) | Applies a rate limit to *actions on production*, not just to HTTP calls — the idea most auto-remediation projects miss |

---

## 14. Plan-Then-Apply — RemediationPlan, Blast Radius, and the Policy Gate

### 14.1 The idea

Every existing auto-remediation tool is a **trigger → action** pipeline: match an alert, run a script. HealOps inserts a stage that changes the whole safety story:

```
alert -> correlate -> PLAN -> policy gate -> [shadow | approval | execute] -> VERIFY -> compensate?
```

A **RemediationPlan** is a data object, not code: the exact API calls it will make, the compensating action, a reversibility class, the blast radius, the expected recovery signal, a calibrated confidence, and a TTL if the fix is ephemeral. Because it's data, three things become possible that a script can't offer:

1. **It can be reviewed before it runs** — by a policy engine, or by a human in Slack, who sees *"scale api-gateway 3 → 5 replicas in prod, reversible, blast radius 2 new pods, ≈₹430/month, expected recovery: p95 CPU < 70% within 5 min"* rather than *"HealOps wants to run cpu_scale_up"*.
2. **It can be executed in shadow mode** — recorded, not applied — which is how a new runbook earns trust (§19).
3. **It can be replayed and audited** — the plan, the policy decision, and the outcome are one immutable record (§20).

> Interview soundbite: *"I built `terraform plan` for incident response. Every runbook produces a plan object before it touches the cluster — the API calls, the undo, the blast radius, and what recovery should look like. The plan is what the policy engine checks, what a human approves in Slack, and what lands in the audit ledger. It also means a brand-new runbook can run in shadow mode: it produces plans for real incidents and I compare them against what I'd have done, without it having any power to act."*

### 14.2 The Plan object

```python
# engine/src/plan.py
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import StrEnum
from typing import Any, Optional


class Reversibility(StrEnum):
    REVERSIBLE = "reversible"                    # exact prior state restorable (scale, rollout undo)
    PARTIAL = "partially_reversible"             # side effects persist (logs moved to S3, pods rescheduled)
    IRREVERSIBLE = "irreversible"                # cannot be undone (delete PVC) -> always denied by policy


class Durability(StrEnum):
    EPHEMERAL = "ephemeral"                      # imperative, TTL'd, expires (§17)
    DURABLE = "durable"                          # must land in git as a PR (§17)


@dataclass(frozen=True)
class ApiCall:
    """One concrete Kubernetes/AWS operation. Rendered into Slack and the ledger verbatim."""
    verb: str                                    # patch | scale | evict | cordon | resize | s3_put
    resource: str                                # apps/v1/Deployment
    namespace: Optional[str]
    name: str
    body: dict[str, Any] = field(default_factory=dict)
    subresource: Optional[str] = None            # e.g. "resize" (§18)


@dataclass(frozen=True)
class ExpectedSignal:
    """What recovery looks like. Verification (§15) is a PromQL question, defined up front."""
    alert_should_clear: str                       # alertname
    queries: dict[str, str]                       # name -> PromQL
    thresholds: dict[str, float]                  # name -> max acceptable value
    window_seconds: int = 300


@dataclass(frozen=True)
class BlastRadius:
    pods: int
    nodes: int
    namespaces: list[str]
    user_facing: bool                              # does the target serve external traffic?

    def score(self) -> int:
        """Cheap ordinal used by policy rules; deliberately simple and explainable."""
        return self.pods + 10 * self.nodes + (20 if self.user_facing else 0)


@dataclass(frozen=True)
class RemediationPlan:
    runbook: str
    incident_id: int
    target_kind: str
    target_namespace: Optional[str]
    target_name: str
    actions: list[ApiCall]
    compensation: list[ApiCall]                    # decided BEFORE acting; empty only if PARTIAL/none
    reversibility: Reversibility
    durability: Durability
    blast_radius: BlastRadius
    expected_signal: ExpectedSignal
    confidence: float                              # calibrated (§19)
    summary: str                                   # one line for Slack
    cost_delta_inr_month: float = 0.0
    ttl_seconds: Optional[int] = None               # required when durability == EPHEMERAL
    refusal_reason: Optional[str] = None            # set -> this is a REFUSAL, not a plan

    @property
    def is_refusal(self) -> bool:
        return self.refusal_reason is not None

    def to_dict(self) -> dict:
        return asdict(self)


def refuse(runbook: str, incident_id: int, reason: str) -> RemediationPlan:
    """A refusal is a first-class outcome and is audited exactly like an action."""
    return RemediationPlan(
        runbook=runbook, incident_id=incident_id, target_kind="", target_namespace=None,
        target_name="", actions=[], compensation=[], reversibility=Reversibility.REVERSIBLE,
        durability=Durability.EPHEMERAL, blast_radius=BlastRadius(0, 0, [], False),
        expected_signal=ExpectedSignal("", {}, {}), confidence=1.0,
        summary=f"refused: {reason}", refusal_reason=reason,
    )
```

### 14.3 The policy gate (CEL — policy as code, in git)

```yaml
# policy/remediation.yaml — evaluated against the plan object with cel-python.
# Rules are ordered; the first DENY wins, then any REQUIRE_APPROVAL, else ALLOW.
version: 1
rules:
  - id: deny-irreversible
    effect: deny
    expr: 'plan.reversibility == "irreversible"'
    message: "irreversible actions are never automated"

  - id: deny-outside-allowed-namespaces
    effect: deny
    expr: '!(plan.target_namespace in ["shop","payments","default","chaos"])'
    message: "namespace not in the automation allow-list"

  - id: deny-protected-workloads
    effect: deny
    expr: 'plan.target_name.startsWith("db-") || plan.target_kind == "StatefulSet"'
    message: "stateful workloads are excluded from automated remediation"

  - id: deny-kill-switch
    effect: deny
    expr: 'target.annotations["healops.io/remediation"] == "disabled"'
    message: "workload opted out via annotation"

  - id: deny-single-replica-disruptive
    effect: deny
    expr: 'target.replicas < 2 && plan.reversibility != "reversible"'
    message: "no safe blast radius on a single-replica workload"

  - id: deny-budget-exhausted
    effect: deny
    expr: 'budget.breaker_state == "open" || budget.used_actions >= budget.max_actions'
    message: "remediation budget exhausted or breaker open (§16)"

  - id: approve-large-blast-radius
    effect: require_approval
    expr: 'plan.blast_radius.score() > 25'
    message: "blast radius above the auto threshold"

  - id: approve-costly
    effect: require_approval
    expr: 'plan.cost_delta_inr_month > 500.0'
    message: "projected cost above the auto threshold"

  - id: approve-low-confidence
    effect: require_approval
    expr: 'plan.confidence < 0.80'
    message: "confidence below the auto threshold"

  - id: approve-not-promoted
    effect: require_approval
    expr: 'promotion.mode != "auto"'
    message: "runbook not yet promoted to auto in this namespace (§19)"
```

Two properties worth stating out loud: the **auto-vs-approval split is now computed from plan properties**, not hardcoded per runbook (Rev 1's table becomes the *default* outcome of these rules), and **the policy file is reviewed in git** — adding a guardrail is a PR, not a code change.

### 14.4 The runbook contract, rewritten (replaces §5.2)

```python
# engine/src/runbooks/base.py
"""
Rev 2 runbook contract. Five methods, and the framework — not the runbook — decides
whether to execute. A runbook that cannot describe its own undo cannot be registered.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional

from ..plan import RemediationPlan
from ..signals import SignalWindow


@dataclass
class ExecutionResult:
    ok: bool
    detail: str
    state_before: dict
    state_after: dict


class Runbook(abc.ABC):
    name: str
    triggers: tuple[str, ...]                 # alertnames this runbook answers

    # 1. Describe intent. Pure-ish: reads cluster state, mutates nothing.
    @abc.abstractmethod
    def plan(self, incident, target) -> RemediationPlan: ...

    # 2. Apply the plan's actions. Called ONLY after the policy gate (and approval, if required).
    @abc.abstractmethod
    def execute(self, plan: RemediationPlan) -> ExecutionResult: ...

    # 3. Undo. Must be idempotent — the watchdog may call it after execute() already did.
    @abc.abstractmethod
    def compensate(self, plan: RemediationPlan) -> ExecutionResult: ...

    # 4. Optional runbook-specific health probe, on top of the generic signal check (§15).
    def extra_verification(self, plan: RemediationPlan, after: SignalWindow) -> Optional[str]:
        return None

    # 5. Self-test contract enforced in CI (§21): a runbook must ship a chaos fixture.
    @property
    @abc.abstractmethod
    def chaos_fixture(self) -> str: ...        # name of the fault injector that proves this runbook
```

And the orchestrator that everything flows through — the piece that makes safety structural rather than a matter of each runbook remembering:

```python
# engine/src/orchestrator.py
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
    if mode == "shadow":                                    # §19: record, do not act
        store.record_shadow(plan, decision); slack.post_shadow(plan); return

    if decision.effect == "require_approval":
        approval = approvals.request(plan, decision)        # Postgres source of truth (§C.3)
        if not approval.granted:                            # rejected, expired, or timed out
            slack.post_escalation(plan, approval); return

    # --- the only mutating window in the whole system ---
    with locks.per_resource(plan.target_kind, plan.target_namespace, plan.target_name):
        before = signals.window(plan, seconds=plan.expected_signal.window_seconds)
        store.register_compensation(plan)                   # BEFORE acting (saga, §22)
        result = runbook.execute(plan)
        ledger.append("execute", incident_id=incident.id, payload=result.__dict__)

    verifier.schedule(plan, result, after=plan.expected_signal.window_seconds)   # §15
    budget.consume(plan.target_namespace)
    recurrence.record(runbook.name, plan)                   # §16.4 anti-masking
```

Every branch is audited, including the two that do nothing — **refusals and denials are outcomes worth logging**, because "the system correctly decided not to act" is a data point the scorecard needs.

---

## 15. Closed-Loop Verification, Compensation, and the Efficacy Scorecard

### 15.1 What "verified" means

The definition of a successful remediation is **not** "the API call succeeded". It is:

1. The triggering alert **cleared** (Alertmanager no longer lists it firing), **and**
2. The workload's **golden signals recovered** — no new OOM events, restarts stable, CPU throttle ratio and PSI stall within threshold, and (where present) the service's error rate back below its SLO burn rate, **and**
3. No **new** alert appeared on the same target that the action could have caused.

If (1) and (2) hold → `REMEDIATED`. If they don't → **compensate and escalate**. If the signals are unavailable → `INCONCLUSIVE` and escalate. Never a silent success.

```python
# engine/src/verifier.py
from dataclasses import dataclass

THROTTLE_RATIO_CEILING = 0.05     # 5% of CFS periods throttled
PSI_FULL_CEILING = 0.05           # 5% of wall time fully stalled
RESTART_TOLERANCE = 0


@dataclass
class Verdict:
    outcome: str                  # remediated | not_recovered | inconclusive
    reasons: list[str]
    evidence: dict


def verify(plan, before, after, alert_still_firing: bool | None) -> Verdict:
    reasons, evidence = [], {"before": before.as_dict(), "after": after.as_dict()}

    if alert_still_firing is None:
        return Verdict("inconclusive", ["could not query Alertmanager"], evidence)
    if alert_still_firing:
        reasons.append(f"{plan.expected_signal.alert_should_clear} is still firing")

    if after.oom_kills > before.oom_kills:
        reasons.append("new OOM kills after the action")
    if after.restarts - before.restarts > RESTART_TOLERANCE:
        reasons.append("container restarted after the action")
    if after.throttle_ratio is not None and after.throttle_ratio > THROTTLE_RATIO_CEILING:
        reasons.append(f"CPU throttle ratio {after.throttle_ratio:.1%} above ceiling")
    if after.psi_full is not None and after.psi_full > PSI_FULL_CEILING:
        reasons.append(f"PSI full stall {after.psi_full:.1%} above ceiling")
    if after.new_alerts:
        reasons.append(f"new alerts appeared on the target: {after.new_alerts}")

    # A missing signal is never treated as a healthy signal.
    if any(v is None for v in (after.throttle_ratio, after.psi_full)) and not reasons:
        return Verdict("inconclusive", ["signals unavailable — cannot confirm recovery"], evidence)

    return Verdict("not_recovered" if reasons else "remediated", reasons, evidence)
```

The PromQL behind `SignalWindow` — note the **ratio**, because raw throttled-period counters move with replica count and window length and would make the check meaningless:

```promql
# throttle ratio (dimensionless)
sum by (namespace,pod,container) (rate(container_cpu_cfs_throttled_periods_total{...}[5m]))
/
sum by (namespace,pod,container) (rate(container_cpu_cfs_periods_total{...}[5m]))

# OOM by COUNTER, not the flapping last_terminated_reason gauge
sum by (namespace,pod,container) (increase(container_oom_events_total{...}[10m]))

# PSI: share of wall time fully stalled (beta 1.34, GA 1.36)
sum by (namespace,pod,container) (rate(container_pressure_memory_stalled_seconds_total{...}[5m]))

# is the alert still firing? Alertmanager API v2 (v1 was removed in 0.27)
GET /api/v2/alerts?filter=alertname%3D"KubePodCrashLooping"&filter=namespace%3D"shop"
```

### 15.2 Compensation — registered before the action, executed by two paths

```python
# engine/src/compensator.py
def on_verification(plan, verdict) -> None:
    if verdict.outcome == "remediated":
        store.close(plan, "remediated")
        if plan.durability == "durable":
            git.open_pr(plan)                      # make the fix permanent (§17)
        slack.post_success(plan, verdict)
        return

    # not_recovered or inconclusive -> undo what we did, then escalate to a human.
    result = runbooks.get(plan.runbook).compensate(plan)
    ledger.append("compensate", payload={"plan": plan.to_dict(), "result": result.__dict__})
    store.close(plan, "reverted" if result.ok else "revert_failed")
    slack.post_escalation(plan, verdict, result)   # @oncall, with full evidence + bundle hash
```

```python
# engine/src/watchdog.py — CronJob every minute. The second path.
def sweep() -> int:
    """Compensate anything whose verification never ran, and expire ephemeral fixes (§17)."""
    n = 0
    for row in store.executions_awaiting_verification(older_than_seconds=900):
        compensator.force_revert(row, reason="verification never completed")
        n += 1
    for row in store.expired_ephemeral_fixes():        # expires_at < now()
        compensator.expire(row)                        # returns the resource to its declared state
        n += 1
    return n     # -> healops_watchdog_actions_total (alert if > 0)
```

The watchdog exists because a compensation owned by the process that can crash is not a compensation. This is the **saga pattern** with a persisted compensating transaction, and saying that sentence in an interview is worth the two hours it takes to build.

### 15.3 The efficacy scorecard — the demo, quantified

Rev 1's demo claim was "3 of 5". Rev 2 makes it a **measured metric with an honest denominator**:

| Metric | Definition | Why this definition |
|---|---|---|
| **Auto-resolution rate** | `remediated_without_human / total_incidents` | The headline. Human-approved fixes are excluded — they needed a human |
| **Assisted-resolution rate** | `remediated_after_approval / total_incidents` | Automation still did the work; a human authorised it |
| **Escalation rate** | `escalated / total_incidents` | The system's own admission of "not my problem" — a *good* number, not a failure |
| **False-remediation rate** | `reverted_after_verification / executed` | The number nobody else reports. **This is the trust metric** |
| **MTTR (auto)** | median(`remediated_at − alert_started_at`) for auto path | Compare against MTTR for escalated incidents to show the value |
| **Time-to-detect-failure** | median(`verification_at − executed_at`) | How fast the system notices its own mistake |
| **Refusal rate** | `refused_by_policy / plans_created` | Shows the guardrails are load-bearing, not decorative |

```text
# Prometheus metrics exported by the engine (self-monitoring)
healops_incidents_total{outcome="auto|approved|escalated|refused"}
healops_remediation_duration_seconds{runbook,outcome}      # histogram
healops_verification_outcome_total{runbook,outcome="remediated|not_recovered|inconclusive"}
healops_compensations_total{runbook,trigger="verification|watchdog|expiry"}
healops_budget_used_ratio{namespace}
healops_breaker_state{scope}                                # 0 closed, 1 half_open, 2 open
healops_promotion_mode{runbook,namespace}                   # 0 shadow, 1 approval, 2 auto
healops_recurrence_count{runbook,target}
```

> The demo line becomes: *"3 of 5 auto-resolved, 2 escalated after approval, **zero false remediations** — and the false-remediation rate is a metric on my Grafana dashboard, not a claim. The system also reverted itself once during testing, in 90 seconds, which is the number I'm proudest of."*

### 15.4 Why this beats "the alert stopped firing"

An interviewer who has run production will ask: *"couldn't you just check if the alert cleared?"* The answer:

- **Alerts clear for the wrong reasons.** A CrashLooping pod alert clears if the pod is deleted, if the deployment scales to zero, or if the metric goes stale. None of those mean the incident is fixed.
- **`for:` durations lag.** An alert takes minutes to clear even after recovery, and minutes to re-fire after a relapse. Signal checks are immediate.
- **The action can cause a *different* problem.** Scaling up fixes CPU and starts an OOM cascade on a memory-tight node. Only a signal check on the *whole* target catches that, which is why `after.new_alerts` is part of the verdict.

### 15.5 Dashboard panels this adds

Incident timeline with the plan/policy/execute/verify/compensate stages as a horizontal state trail · auto vs assisted vs escalated stacked over time · **false-remediation rate** as a single big number (green when zero) · MTTR auto vs escalated · budget usage per namespace with the breaker state · promotion-ladder table (runbook × namespace → shadow/approval/auto) · recurrence leaderboard ("workloads automation keeps fixing" — §16.4).

---

## 16. Cause-First Correlation, Storm Control, and the Anti-Masking Guard

### 16.1 Correlation: one incident, not forty

```python
# engine/src/correlate.py
"""
Group alerts into incidents by TOPOLOGY and CAUSALITY, not by fingerprint.

Rules, in order:
  1. Node-level cause dominates pod-level symptoms on that node.
  2. Deployment-level cause dominates pod-level symptoms of that deployment's pods.
  3. Alerts on the same target within the correlation window join the same incident.
  4. Everything else is its own incident.
"""
CORRELATION_WINDOW_S = 300

CAUSE_PRECEDENCE = {                     # lower number = closer to the root cause
    "KubeNodeNotReady": 0,
    "NodeDiskPressure": 0,
    "KubeDeploymentReplicasMismatch": 1,
    "KubePodOOMKilled": 2,
    "KubePodCrashLooping": 2,
    "HighPodCPU": 3,
}


def assign(alert, open_incidents) -> tuple[Incident, str]:
    node = alert.labels.get("node") or topology.node_of_pod(alert.labels.get("pod"))

    # 1. Is there an active node-level cause for this alert's node?
    for inc in open_incidents:
        if inc.cause_scope == "node" and inc.cause_target == node:
            return inc, "symptom_of_node_cause"

    # 2. Is there an active workload-level cause for this pod's owner?
    owner = topology.owner_of_pod(alert.labels.get("pod"))          # ReplicaSet -> Deployment
    for inc in open_incidents:
        if inc.cause_scope == "workload" and inc.cause_target == owner:
            return inc, "symptom_of_workload_cause"

    # 3. Same target, inside the window -> same incident.
    for inc in open_incidents:
        if inc.target_key == target_key(alert) and inc.age_seconds < CORRELATION_WINDOW_S:
            return inc, "same_target"

    return Incident.new(alert, precedence=CAUSE_PRECEDENCE.get(alert.name, 5)), "new"
```

Consequences that make the demo sing: symptom alerts are **attached as evidence** and never trigger their own runbook; the incident's *cause* alert selects the runbook; and if a node recovers on its own, forty symptom alerts clear with **zero actions taken** — which the scorecard reports as "1 incident, 0 remediations, self-resolved", the honest outcome.

Also use Alertmanager's own **`inhibit_rules`** as the first line of defence (a `KubeNodeNotReady` on a node inhibits `KubePodCrashLooping` for pods on it) — then do correlation in HealOps as the second line, because inhibition can't express owner-reference topology.

### 16.2 Remediation budget (token bucket over *actions on production*)

```python
# engine/src/budget.py
DEFAULT_BUDGETS = {           # actions per rolling hour
    "global": 20,
    "ns:payments": 3,         # tighter where it matters more
    "ns:shop": 8,
    "ns:chaos": 50,           # the demo namespace can be noisy
}

def check(scope: str) -> tuple[bool, str]:
    b = db.budget(scope)
    if b.breaker_state == "open":
        return False, f"circuit breaker open until {b.breaker_until}: {b.breaker_reason}"
    if b.used_actions >= b.max_actions:
        return False, (f"budget exhausted ({b.used_actions}/{b.max_actions} in "
                       f"{b.window_seconds}s) — escalating instead of automating")
    return True, "within budget"
```

The framing that lands: **an error budget for automation.** *"If my system needs to act more than 20 times an hour, the cluster has a systemic problem and a human should be looking at it — so automation stops and pages instead of grinding on."*

### 16.3 The global circuit breaker

Trips on any of: **> 3 compensations in 15 minutes** (the system keeps getting it wrong), **> 2 consecutive verification failures for the same runbook** (that runbook is broken), **Kubernetes API error rate > 20%** (the cluster is sick — automating into it is reckless), or **manual trip** (`healopsctl freeze --reason "release in progress"`). Recovery is `open → half_open` after 30 minutes, where exactly one plan is allowed through and its outcome decides `closed` or `open` again.

`healopsctl freeze` is worth ten minutes of build time: *"during a deploy or an incident review, one command turns all automation off, and the freeze itself is a ledger entry with a reason and an owner."*

### 16.4 The anti-masking guard — automation that hides a bug files a bug

This is the criticism every self-healing system deserves and almost none answer: **if you automatically fix the same failure every day, you have converted a bug into a chore and hidden it.**

```python
# engine/src/recurrence.py
RECURRENCE_THRESHOLD = 4          # same runbook, same target, within 7 days
def record(runbook: str, plan) -> None:
    key = f"{plan.target_namespace}/{plan.target_kind}/{plan.target_name}"
    count = db.increment_recurrence(runbook, key, window_days=7)
    if count < RECURRENCE_THRESHOLD:
        return

    # 1. Stop auto-remediating: demote this (runbook, namespace) to approval mode (§19).
    promotion.demote(runbook, plan.target_namespace,
                     reason=f"masking guard: {count} remediations of {key} in 7 days")

    # 2. File the bug WITH the evidence, and link every ledger entry.
    issue = github.create_issue(
        title=f"[HealOps] {runbook} has remediated {key} {count}× in 7 days",
        body=render_masking_report(runbook, key, count),   # timeline, plans, verifications, bundles
        labels=["healops", "recurring-incident", "needs-root-cause"],
    )
    db.attach_issue(runbook, key, issue.url)
    slack.post_masking_warning(runbook, key, count, issue.url)
```

> Interview soundbite: *"The honest criticism of self-healing is that it hides bugs. So HealOps counts how often it fixes the same thing. On the fourth time in a week it stops auto-fixing, drops that runbook back to approval mode for that namespace, and opens a GitHub issue with the full timeline and evidence attached. Automation that keeps papering over a defect files a bug report against it. I haven't seen an open-source remediation tool that does that."*

### 16.5 The second "scaling decision" to document

> **Decision: bounded automation (token bucket + circuit breaker) over unbounded event-driven remediation.**
>
> **Problem:** remediation throughput scales with alert volume, and alert volume spikes exactly when the cluster is least healthy. An unbounded system does its most aggressive work at the worst possible moment.
>
> **Option A — scale the workers.** More concurrency, faster drain of the alert queue. Rejected: the bottleneck isn't compute, it's *risk*. Executing 40 changes in 90 seconds during a partial outage is the definition of a self-inflicted incident.
>
> **Option B — bounded automation.** Correlate to incidents, cap actions per namespace per hour, trip a global breaker on repeated failure, and escalate the overflow to humans. **Chosen.**
>
> **Trade-off accepted:** in a genuine mass event HealOps deliberately fixes *less* and pages *sooner*. That's the correct behaviour, and it's the opposite of what a naive throughput-first design does. Rev 1's per-resource locking (still present) solves *conflicts*; this solves *volume*, and they are different problems.

---

## 17. GitOps-Aware Remediation — Ephemeral vs Durable Fixes

### 17.1 The problem nobody handles

Almost every "self-healing Kubernetes" tutorial ends at `kubectl rollout undo`. In any cluster with Argo CD or Flux — which is most clusters worth automating — that command is **temporary at best and a fight at worst**:

| What you did imperatively | What the GitOps controller does |
|---|---|
| `kubectl rollout undo deployment/api` | Detects drift from git and **re-applies the bad image**, usually within its sync interval (3 min by default) |
| `kubectl scale --replicas=5` | Reverts to the replica count in git |
| `kubectl patch` a memory limit | Reverted; and if `selfHeal` is on, immediately |
| Anything, on a synced app | Marks the app **OutOfSync** — your remediation now shows up as a compliance violation on someone's dashboard |

Rev 1 dismissed this: *"ArgoCD handles GitOps deployment, not incident-triggered rollbacks. Different trigger, different use case."* That's the defect: they aren't different use cases, they're **two controllers with write access to the same object**.

### 17.2 The model: every fix is ephemeral or durable

```python
# engine/src/gitops.py
"""
Classify targets, then choose how the fix reaches the cluster.

  UNMANAGED  -> imperative fix is fine; still TTL it, because an untracked permanent
                change is how clusters drift from their manifests.
  MANAGED    -> two-track:
                  EPHEMERAL mitigation: imperative + TTL + tell the GitOps controller
                                        to leave it alone for the TTL window.
                  DURABLE fix:          a pull request against the manifest repo.
"""
def classify(target) -> str:
    labels, anns = target.labels or {}, target.annotations or {}
    if labels.get("app.kubernetes.io/managed-by") == "Helm" and "argocd.argoproj.io/instance" in labels:
        return "argocd"
    if "kustomize.toolkit.fluxcd.io/name" in labels:
        return "flux"
    if "argocd.argoproj.io/instance" in labels:
        return "argocd"
    return "unmanaged"


def prepare(plan, target) -> RemediationPlan:
    mgr = classify(target)
    if mgr == "unmanaged":
        return replace(plan, durability="ephemeral", ttl_seconds=plan.ttl_seconds or 7200)

    if plan.durability == "ephemeral":
        # Ask the controller not to fight us for the TTL window.
        #   Argo CD: annotate the Application with a sync window / set selfHeal=false, or
        #            annotate the resource argocd.argoproj.io/compare-options: IgnoreExtraneous
        #            plus a temporary ignoreDifferences entry for the field we changed.
        #   Flux:    suspend the Kustomization (flux suspend ks <name>) for the window.
        gitops.pause(target, reason=f"healops:{plan.runbook}", ttl=plan.ttl_seconds)
        return replace(plan, gitops_managed=True)

    # Durable: the cluster is not the source of truth. Git is.
    return replace(plan, durability="durable", gitops_managed=True)
```

### 17.3 Durable fixes are pull requests

```python
def open_remediation_pr(plan, verification) -> str:
    """Called AFTER verification proves the ephemeral mitigation worked (§15.2)."""
    branch = f"healops/{plan.runbook}/{plan.target_name}-{plan.incident_id}"
    edit_manifest(repo, plan)                       # e.g. bump memory limit 512Mi -> 768Mi
    return github.create_pr(
        branch=branch,
        title=f"[HealOps] {plan.summary}",
        body=render_pr_body(plan, verification),    # evidence table + ledger hash + revert command
        labels=["healops", "incident-remediation", f"runbook:{plan.runbook}"],
        reviewers=codeowners_for(plan.target_namespace),
    )
```

The PR body carries the incident timeline, the before/after signals, the ledger hash, and *"this change was already validated live for 42 minutes under real traffic before this PR was opened"* — which is a far better review packet than a human filing the same change from memory at 3 a.m.

### 17.4 The three-sentence version for the interview

> *"Most self-healing demos run `kubectl rollout undo` and call it fixed. In a GitOps cluster that fix gets reverted by drift detection within a few minutes, and the bad image comes back on the next sync. So HealOps classifies every target: if it's Argo CD or Flux managed, the imperative action is an explicit **ephemeral mitigation** with a TTL, and HealOps tells the controller to stand down for exactly that window — then, only after verification proves the mitigation worked, it opens a **pull request** so git becomes the source of truth again. The mitigation buys minutes; the PR is the fix. Nothing in my system pretends an imperative patch is permanent."*

### 17.5 Expiry is a first-class state

Every ephemeral fix has `expires_at`. When it expires, the watchdog (§15.2) removes the imperative change, resumes GitOps reconciliation, and posts to Slack: *"the ephemeral mitigation on api-gateway expired; the workload is back to its declared state. The durable fix is PR #128 (still open)."* Automation that quietly leaves permanent hand-edits in a cluster is worse than no automation — this is the mechanism that guarantees it doesn't.

---

## 18. Non-Disruptive Remediation with In-Place Pod Resize (GA 1.35)

### 18.1 Why this changes Runbook 4

Rev 1's OOMKill runbook patches the Deployment's memory limit and restarts the pod, which is why it required approval: **a fix that restarts your service is disruptive, so a human should own it.** In-place pod resize went **GA in Kubernetes 1.35**, so that's no longer the only option:

| | Rev 1 approach | Rev 2 approach |
|---|---|---|
| Mechanism | patch Deployment template → rollout → pod restart | patch **`pods/resize`** subresource on the affected pod |
| Disruption | container restarts, connections drop | **no restart**; the cgroup limit is raised in place |
| Reversibility | roll forward the template | resize back — exact prior state |
| Durability | durable (but Argo CD will fight it, §17) | **ephemeral** mitigation + a PR for the durable template change |
| Approval | required | **auto-eligible** (reversible, zero blast radius beyond one pod), with the *template* change still going through a PR |
| Speed | ~30–60 s including rollout | ~2–5 s |

That is a genuinely better incident response, and it exists because of a platform capability that is three releases old. It's also the crispest possible demonstration that you track the platform: *"my remediation got less disruptive because a Kubernetes feature went GA, and I noticed."*

### 18.2 Implementation

```python
# engine/src/runbooks/oom_memory_bump.py
MEMORY_BUMP_FACTOR = 1.5
MEMORY_CEILING_MIB = 4096

class OomMemoryBump(Runbook):
    name = "oom_memory_bump"
    triggers = ("KubePodOOMKilled",)
    chaos_fixture = "hungry-worker"

    def plan(self, incident, target) -> RemediationPlan:
        if not cluster.supports_in_place_resize():          # server >= 1.35
            return self._legacy_template_plan(incident, target)   # restart-based, needs approval

        peak = metrics.memory_peak_mib(target)             # cgroup memory.peak or max_over_time
        current = target.memory_limit_mib
        new = min(max(int(current * MEMORY_BUMP_FACTOR), int(peak * 1.25)), MEMORY_CEILING_MIB)
        if new <= current:
            return refuse(self.name, incident.id,
                          f"already at ceiling {MEMORY_CEILING_MIB}Mi — this needs a code fix, "
                          "not more memory")

        # resizePolicy must not be RestartContainer for memory, or "no restart" is a lie.
        if target.memory_resize_policy == "RestartContainer":
            return refuse(self.name, incident.id,
                          "container declares resizePolicy RestartContainer for memory; "
                          "an in-place bump would restart it — escalating instead")

        return RemediationPlan(
            runbook=self.name, incident_id=incident.id,
            target_kind="Pod", target_namespace=target.namespace, target_name=target.pod,
            actions=[ApiCall("resize", "v1/Pod", target.namespace, target.pod,
                             subresource="resize",
                             body={"spec": {"containers": [{
                                 "name": target.container,
                                 "resources": {"requests": {"memory": f"{new}Mi"},
                                               "limits": {"memory": f"{new}Mi"}}}]}})],
            compensation=[ApiCall("resize", "v1/Pod", target.namespace, target.pod,
                                  subresource="resize",
                                  body={"spec": {"containers": [{
                                      "name": target.container,
                                      "resources": {"requests": {"memory": f"{current}Mi"},
                                                    "limits": {"memory": f"{current}Mi"}}}]}})],
            reversibility=Reversibility.REVERSIBLE,
            durability=Durability.EPHEMERAL, ttl_seconds=7200,
            blast_radius=BlastRadius(pods=1, nodes=0, namespaces=[target.namespace],
                                     user_facing=target.user_facing),
            expected_signal=ExpectedSignal(
                alert_should_clear="KubePodOOMKilled",
                queries={"oom": f'increase(container_oom_events_total{{pod="{target.pod}"}}[10m])',
                         "psi": f'rate(container_pressure_memory_stalled_seconds_total{{pod="{target.pod}"}}[5m])'},
                thresholds={"oom": 0.0, "psi": 0.05}, window_seconds=600),
            confidence=0.9,
            summary=f"raise {target.pod} memory {current}Mi → {new}Mi in place (no restart)",
        )

    def execute(self, plan) -> ExecutionResult:
        before = k8s.actual_resources(plan.target_namespace, plan.target_name)
        k8s.patch_resize(plan.actions[0])                  # PATCH .../pods/{name}/resize
        applied, conditions = k8s.await_resize(plan, timeout_s=60)
        if not applied:
            # PodResizePending: Infeasible -> the node cannot satisfy it. Do NOT hang.
            return ExecutionResult(False, f"resize not applied: {conditions}", before, before)
        return ExecutionResult(True, "resized in place, no restart",
                               before, k8s.actual_resources(plan.target_namespace, plan.target_name))
```

Details that make this credible in a technical interview: mutation goes through the **`resize` subresource** (not the pod spec); success is asserted on **`status.containerStatuses[].resources`**, not on the HTTP 200; `PodResizePending` with reason **`Infeasible`** ends the attempt immediately instead of waiting forever; `resizePolicy: RestartContainer` for memory is a **refusal**, because otherwise "no restart" would be false; and the typed Python client may not expose the subresource, so it's called through the generic API path.

### 18.3 The two-stage fix (the pattern to reuse everywhere)

```
Stage 1 (seconds, ephemeral, auto):  in-place resize -> OOM stops -> service stays up
Stage 2 (minutes, durable, human):   PR raising the memory limit in the manifest,
                                     carrying the evidence that stage 1 worked
```

**Mitigate automatically, fix durably through review.** That sentence is the philosophy of the whole project, and Runbook 4 is where it's most visible. Apply the same split to Runbook 1 (rollback now, PR pinning the good image later) and Runbook 3 (scale/target now, PR adjusting the HPA later).

---

## 19. The Promotion Ladder + Calibrated Confidence (the MLOps Wrapper)

### 19.1 "Was this correct?" that actually changes behaviour

Rev 1 sends a *"was this correct?"* button and logs the answer. Rev 2 makes the answer **do something**: every `(runbook, namespace)` pair sits on a three-rung ladder, and it climbs or falls on evidence.

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
                            -> the masking guard tripped (§16.4)
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

**Why this is the answer to the strongest objection you'll get.** An interviewer will say: *"how do you know your automation is safe enough to run unattended?"* The weak answer is "I only automated the safe runbooks." The strong answer:

> *"I don't decide that up front — I earn it per namespace. A new runbook starts in shadow mode: it writes the plan it would have executed, and I compare it to what the on-call actually did. When it agrees with humans 90% of the time over at least ten incidents, it graduates to approval mode. After twenty consecutive verified-good outcomes in that namespace, it graduates to auto. One bad outcome — a failed verification, or a human clicking 'No' on 'was this correct?' — demotes it immediately, and any change to its code or its policy sends it back to approval. It's progressive delivery, applied to automation itself. Notice the blast radius is per-namespace: a runbook can be fully automatic in `shop` and still approval-only in `payments`."*

### 19.2 Calibrated confidence

Every plan carries a `confidence`. That number is worthless unless it's **measured**, so HealOps grades it exactly the way a forecast is graded:

| Metric | Gate | Meaning |
|---|---|---|
| **Top-1 accuracy** | ≥ 0.90 | the chosen runbook matches the labelled correct action |
| **Brier score** | ≤ 0.08 | confidence numbers track reality |
| **Expected Calibration Error (ECE)** | ≤ 0.10 | when it says 0.9, it's right about 90% of the time |
| **Determinism digest** | exact match | same incident bundle → byte-identical plan |
| **Refusal recall** | 100% of "must refuse" cases | policy denials and refusals are graded like verdicts |

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

**The corpus is generated, not hand-written.** The chaos harness (§21) drives a `kind` cluster, induces each fault, captures the resulting alert payloads plus cluster state as a bundle, and labels it with the correct action. Target ≥ 25 cases across the five runbooks, and **at least four "must refuse" cases**: a StatefulSet OOMKill (policy denies), a single-replica CrashLoop (no safe blast radius), a node alert during a planned drain (freeze active), and an alert on an annotated opt-out workload. A remediation engine that never refuses is a liability, so refusal is graded like everything else.

### 19.3 Shadow mode is also how you demo trust

The demo gains a second act that nobody else will have:

> *"Here's runbook 6, which I wrote last week. It's in shadow mode. Watch — I trigger the fault, and HealOps posts to Slack: 'SHADOW: I would have patched the readiness probe timeout 1s → 3s on checkout-api. Blast radius 1 deployment, reversible.' It didn't touch anything. Here's the shadow log next to what I actually did: 8 of 9 agreements. It needs one more before I'll let it ask for approval."*

### 19.4 Why this is the "MLOps wrapper" without pretending to be ML

The brief asked for a DevOps/MLOps wrapper. HealOps' decision layer is **deliberately deterministic** — a rules engine, not a model — but it gets the full MLOps treatment, and that contrast is the point:

| MLOps practice | HealOps equivalent |
|---|---|
| Versioned model artefact | `PLANNER_VERSION` + policy file version, recorded on every plan |
| Labelled dataset | chaos-generated incident corpus, committed to the repo |
| Offline evaluation gating CI | `decision_eval.py` — accuracy, calibration, determinism, refusal recall |
| Shadow deployment | shadow mode per (runbook, namespace) |
| Progressive rollout | the promotion ladder |
| Drift monitoring | agreement rate between plans and human actions, exported to Prometheus |
| Rollback | demotion, plus `healopsctl freeze` |
| Model card | `RUNBOOKS.md`: each runbook's trigger, action, undo, blast radius, refusals, and current promotion state per namespace |

> Interview soundbite: *"I did use an LLM, but only to write postmortems from the evidence bundle — never to decide anything. The decision engine is deterministic rules, because I need to unit-test it, replay it, and hash it. What I borrowed from ML engineering is the discipline: a labelled corpus, an offline eval that gates CI on accuracy **and calibration**, shadow deployment, and progressive rollout. Those practices are more valuable here than a model would be."*

### 19.5 The optional LLM layer (and its hard boundary)

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

The system works with no API key and no network. The LLM writes prose that a human edits; it never touches production. State that boundary before an interviewer asks, because in 2026 "AI SRE agent" products are everywhere and "my automation is deterministic and my *writing* is AI-assisted" is the more defensible position.

---

## 20. Tamper-Evident Audit Ledger, Signed Evidence, Offline Replay

### 20.1 Why an audit log isn't enough

Rev 1 logs everything to PostgreSQL. Good — but a table the application can `UPDATE` is a log, not an audit. The question a senior engineer (or an auditor) asks is: *"how would anyone know if a record had been altered?"*

```python
# engine/src/ledger.py
"""
Append-only, hash-chained ledger. Each entry commits to its predecessor, so altering or
deleting any historical entry breaks every hash after it and is detectable in one pass.

  hash_n = sha256( hash_{n-1} || canonical_json(entry_n) )

The app role has INSERT and SELECT only (UPDATE/DELETE revoked in the migration, §C.6).
"""
import hashlib, json

GENESIS = "0" * 64

def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))

def append(event_type: str, payload: dict, **ids) -> str:
    prev = db.last_ledger_hash() or GENESIS
    entry = {"event_type": event_type, "payload": payload, "ids": ids,
             "occurred_at": db.now_iso()}                     # DB clock, one source of time
    h = hashlib.sha256((prev + canonical(entry)).encode()).hexdigest()
    db.insert_ledger(entry, prev_hash=prev, hash=h)
    return h

def verify_chain(since=None) -> tuple[bool, str | None]:
    """Run in CI and hourly in-cluster; exported as healops_ledger_valid (0/1)."""
    prev = GENESIS
    for row in db.iter_ledger(since):
        expect = hashlib.sha256((prev + canonical(row.entry)).encode()).hexdigest()
        if expect != row.hash:
            return False, f"chain broken at seq={row.seq}"
        prev = row.hash
    return True, None
```

### 20.2 The evidence bundle

Every incident produces one **content-addressed** bundle: the alerts, the correlation decision, the plan, the policy decision, the approval (who clicked, when, from which Slack workspace), the execution result, the before/after signals, the verification verdict, any compensation, and the ledger hashes. `sha256` of the canonical JSON **is** the incident's identity, it's linked from the Slack message and the PR body, and it's signed:

```yaml
# .github/workflows/sign.yml (excerpt) — keyless OIDC, no key material to lose
- uses: sigstore/cosign-installer@v3
- run: |
    cosign sign --yes ghcr.io/OWNER/healops-engine@${DIGEST}
    cosign attest --yes --predicate bundles/${SHA}.json \
      --type https://healops.io/evidence/v1 ghcr.io/OWNER/healops-engine@${DIGEST}
```

### 20.3 Offline replay

```bash
$ healopsctl replay 9f2c8ab4…            # no cluster, no network, no database
incident   : shop/api-gateway  KubePodCrashLooping  2026-08-16T02:14:07Z
correlation: cause=KubePodCrashLooping (no node-level cause active)
plan       : rollout undo deployment/api-gateway  (reversible, ephemeral ttl=2h,
             blast radius: 3 pods / 0 nodes / user-facing)
policy     : allow  [promotion=auto, blast_radius=23<=25, confidence=0.94>=0.80]
execute    : ok  revision 41 -> 40  in 1.8s
verify     : REMEDIATED  alert cleared in 94s; restarts stable; throttle 0.4%; PSI 0.0%
durable    : PR #128 "pin api-gateway to image sha256:… (known good)"
ledger     : 14 entries, chain VALID, root 3b91f0…
```

Two demo moments come out of this: replaying a real incident on a laptop with the cluster disconnected and getting byte-identical output, and running `healopsctl audit --verify-chain` to show tamper-evidence. Then deliberately `UPDATE`-ing a row as the DB superuser and watching `verify_chain` name the exact sequence number where the chain breaks.

### 20.4 The compliance framing (this is what wins the senior room)

> *"The real blocker for auto-remediation in any regulated company isn't technical, it's change management: an automated change to production still needs a change record, an approver, and a rollback plan. So every HealOps action produces one: a hash-chained ledger entry, an approver identity from Slack (or an explicit 'automated under policy X, promotion state auto' when there's no human), the compensating action registered before execution, and a signed evidence bundle you can replay offline. I can hand an auditor a single hash and they can reconstruct the entire incident — and prove nobody edited it afterwards. That's the difference between a tool that fixes things and a tool a bank would actually turn on."*

---

## 21. Chaos-Tested Runbooks — The Runbook Contract in CI

### 21.1 The contract

Rev 1 has a chaos-test workflow, which is already ahead of most projects. Rev 2 turns it into a **merge requirement with three tests per runbook**:

| Test | Asserts | Why |
|---|---|---|
| **Fix test** | inject the fault in `kind` → the runbook's plan is chosen → after execution the fault clears **and verification returns `remediated`** | proves the runbook works end-to-end, including the verification path |
| **Compensation test** | after a successful execution, force compensation → cluster state is **byte-identical** to the pre-incident state | proves the undo is real, which is what makes auto-execution defensible |
| **Refusal test** | present the case the runbook must refuse (StatefulSet, single replica, opt-out annotation, ceiling reached) → **plan is a refusal or policy denies** | proves the guardrails, which are the part reviewers care about |

```python
# tests/chaos/test_crashloop_rollback.py
@pytest.mark.chaos
def test_fix(kind_cluster, chaos):
    chaos.deploy("crashy-api", crash_after="10s")             # good rev, then a bad rev
    chaos.push_bad_revision("crashy-api")
    incident = wait_for_incident("KubePodCrashLooping", timeout=180)

    plan = engine.plan_for(incident)
    assert plan.runbook == "crashloop_rollback"
    assert plan.compensation, "a runbook with no undo must not be registered"

    engine.execute_and_verify(plan)
    assert store.verification(plan).outcome == "remediated"
    assert k8s.deployment_revision("crashy-api") == 1
    assert alert_cleared("KubePodCrashLooping", within=180)


@pytest.mark.chaos
def test_compensation_restores_exact_state(kind_cluster, chaos):
    before = k8s.snapshot("crashy-api")                       # image, replicas, resources, annotations
    plan = engine.plan_for(make_incident("KubePodCrashLooping", "crashy-api"))
    engine.execute(plan)
    engine.compensate(plan)
    assert k8s.snapshot("crashy-api") == before               # exact, not "close enough"


@pytest.mark.chaos
@pytest.mark.parametrize("case", ["statefulset", "single_replica", "opt_out_annotation"])
def test_refuses(kind_cluster, chaos, case):
    target = chaos.deploy_case(case)
    plan = engine.plan_for(make_incident("KubePodCrashLooping", target))
    decision = policy.evaluate(plan, target, budget.snapshot(target.namespace),
                               promotion.get("crashloop_rollback", target.namespace))
    assert plan.is_refusal or decision.effect == "deny"
```

### 21.2 The CI pipeline (Rev 2 shape)

```yaml
# .github/workflows/ci.yml — the gates that can fail a merge
jobs:
  unit:                     # pure planner/policy/verifier logic, no cluster
    steps:
      - run: pytest -m "not chaos" --cov=engine --cov-fail-under=80

  decision-gate:            # §19.2 — accuracy + calibration + determinism + refusal recall
    steps:
      - run: python -m evals.decision_eval
      - run: python -m evals.ledger_verify        # hash chain over the committed corpus

  policy-gate:              # policy file is code: it gets tested
    steps:
      - run: python -m evals.policy_eval          # every rule has a must-allow and must-deny case

  chaos:                    # §21.1 — real kind cluster, real faults
    steps:
      - uses: helm/kind-action@v1
        with: { node_image: "kindest/node:v1.36.0" }
      - run: helm install kps prometheus-community/kube-prometheus-stack --version <PINNED>
      - run: helm install healops charts/healops --set mode=test
      - run: pytest -m chaos --runbook=all        # fix + compensation + refusal, per runbook
      - if: failure()
        run: kubectl cluster-info dump --output-directory=/tmp/dump   # artefact for debugging

  build-sign-deploy:
    needs: [unit, decision-gate, policy-gate, chaos]
    steps:
      - run: docker buildx build --tag ghcr.io/OWNER/healops-engine:${{ github.sha }} --push .
      - uses: sigstore/cosign-installer@v3
      - run: cosign sign --yes ghcr.io/OWNER/healops-engine@${DIGEST}
      - run: helm upgrade --install healops charts/healops --wait --atomic \
               --set image.tag=${{ github.sha }}
```

Two notes for Helm 4: `--wait` now needs the **`watch`** verb in the CI service account's role, and server-side apply is the default for new installs, so expect explicit conflict errors instead of silent overwrites — which is what you want when Argo CD also touches these objects.

### 21.3 The line to say

> *"Every runbook ships three tests, and CI runs them against a real `kind` cluster with real faults: it fixes the fault, its compensation restores the cluster byte-for-byte, and it refuses the cases it's supposed to refuse. A runbook without those three tests doesn't merge. That's how I can hand this thing write access to a cluster and sleep — the same reason you don't let a migration into production without a rollback test."*

---

## 22. Resilience Patterns

Every pattern maps to a failure this system will actually have. That mapping is the interview answer — a list of pattern names without failures attached is a flashcard, not engineering.

| Pattern | Where HealOps uses it | Failure it prevents |
|---|---|---|
| **Saga / compensating transaction** | `compensation` is persisted **before** `execute()`; run by the verifier or the watchdog | A partially-applied remediation with no way back |
| **Dead-man's-switch watchdog** | CronJob (1 min) reverts executions whose verification never completed and expires TTL'd fixes | Engine crashes mid-remediation, leaving a hand-edited cluster |
| **Circuit breaker (transport)** | K8s API, Slack API, S3, Alertmanager — `opossum`-style breaker with fallback | A sick API server turning every runbook into a 30-second timeout |
| **Circuit breaker (policy)** | Global remediation breaker on repeated compensations or high K8s error rate (§16.3) | Automating *into* an outage |
| **Token bucket** | Remediation budget per namespace per hour (§16.2) | Storm amplification and cost incidents |
| **Distributed lock** | Redis `SET NX` `lock:{kind}:{ns}/{name}`, TTL 120s, fenced by plan id | Two runbooks mutating the same object concurrently (Rev 1's original insight — kept) |
| **Lease-based leader election** | Kubernetes `coordination.k8s.io/Lease` when the engine runs >1 replica | Two engine replicas both executing the same incident |
| **Idempotency keys** | `(fingerprint, runbook, incident_id)`; Redis Streams `XACK` only after the ledger write | Alertmanager re-delivery or a re-queued message double-remediating |
| **Fail-closed authorisation** | `can_execute()` returns False on any unreadable approval state (§C.3) | Executing a disruptive action nobody approved |
| **Timeout everywhere** | 8s K8s calls, 60s resize wait, 10min approval, 15min verification | Unbounded waits masquerading as "in progress" |
| **Retry with exponential backoff + full jitter** | Transient K8s/S3/Slack errors only — **never** on a mutation that may have partially applied | Thundering-herd retries; double-applied changes |
| **Dead-letter queue** | `remediation-jobs-dlq` after max retries, with alerting on depth | Silent message loss |
| **Bulkhead** | Separate worker pools (and Redis consumer groups) for `execute` vs `verify` | A slow verification backlog blocking new incident handling |
| **Graceful degradation** | No Prometheus → verification returns `inconclusive` and escalates; no Slack → ledger + dashboard still record, engine still acts within policy; no S3 → disk runbook refuses instead of deleting | A dependency outage turning into either paralysis or recklessness |
| **Kill switch** | `healopsctl freeze`, namespace label, workload annotation, global Helm value | Needing a redeploy to stop automation during a release |
| **Self-healing of self** | HealOps' own pods have probes, PDB, and are excluded from its own automation (`healops.io/remediation: disabled`) | The engine remediating itself into a loop |

**The exclusion rule deserves its own sentence in the interview:** *"HealOps refuses to remediate HealOps. A system that can restart itself mid-decision can lose the incident it was handling, so its own namespace is on the deny list — and that rule is in the policy file, not in code."*

---

## 23. Availability & Consistency Patterns

### 23.1 The CAP position, stated deliberately

HealOps is **CP for decisions and AP for observation**, and being able to split it that way is the answer:

| Concern | Model | Why |
|---|---|---|
| **Approvals, plans, ledger** | **CP — strong consistency.** Single-writer PostgreSQL, transactional, fail-closed on partition | An approval must never be read as granted when it isn't. Better to refuse and page than to guess |
| **Alert ingestion** | **AP.** Webhook returns `202` after a durable enqueue; processing is async | Alertmanager should never block on our processing, and a slow decision must not drop the alert |
| **Dashboard reads** | **AP.** Cache-aside with stale-while-revalidate | A dashboard that errors during an incident is worse than one showing 30-second-old data |
| **Cluster state** | **Eventually consistent, by nature.** The K8s API is the truth and it lags | Verification uses windows and thresholds, never instantaneous reads |

### 23.2 Availability mechanics

- **Engine:** 2 replicas, but only one *acts* — a `Lease` gives leadership; the follower serves webhooks and enqueues. Webhook availability (which Alertmanager needs) is decoupled from execution capacity (which must be serialised).
- **Alertmanager:** run its HA mode (a 3-replica gossip cluster in the kube-prometheus-stack values) so a single pod restart can't drop notifications — and note that Alertmanager's own dedup across replicas means your webhook **must be idempotent**, which it is, via `(fingerprint, runbook, incident_id)`.
- **PostgreSQL:** single primary is correct at this scale; take `pg_dump` on a CronJob to S3 plus WAL archiving, and be explicit that **RPO/RTO for the audit trail matters more than for the runbooks** — losing the ledger loses the compliance story.
- **Redis/Valkey:** treated as **disposable**. Everything in it is either a cache, a lock, or a queue whose loss is recoverable from Postgres. Say that out loud: *"if Redis is wiped, HealOps loses in-flight queue position and cold caches. It does not lose approvals, plans, executions, or the audit chain."*
- **Degraded mode:** if PostgreSQL is unreachable, the engine **stops acting** (fail-closed) and posts to Slack. No ledger write means no action — the ledger isn't a side effect, it's a precondition.

### 23.3 Consistency mechanics

- **One transaction per state transition**, with the ledger append in the same transaction as the state change. This is the single most important consistency rule in the system: state and audit cannot diverge.
- **Optimistic concurrency on Kubernetes writes:** use `resourceVersion` preconditions so a stale read can't clobber a concurrent change; on conflict, re-plan rather than retry blindly.
- **Idempotent runbooks:** `compensate()` must be safe to call twice (both the verifier and the watchdog may call it).
- **Monotonic incident state machine:** `open → planning → awaiting_approval → executing → verifying → {remediated | reverted | escalated}`; illegal transitions raise, and the raise is a ledger entry.

---

## 24. Mitigation Strategies — Failure-Mode Table

| # | Failure mode | Blast radius | Detection | Mitigation |
|---|---|---|---|---|
| 1 | Alertmanager sends duplicate/re-fired alerts | Duplicate remediation | Fingerprint + Redis `SET NX` | Dedup window 5 min; idempotency key `(fingerprint, runbook, incident_id)` |
| 2 | Alert storm from one root cause | Mass unnecessary changes | Alerts/min rate; correlation output | Alertmanager `inhibit_rules` + cause-first correlation (§16.1) + budget (§16.2) |
| 3 | Two runbooks target the same object | Unpredictable final state | Lock acquisition metrics | Per-resource Redis lock with plan-id fencing |
| 4 | Remediation makes it worse | One workload, possibly user-facing | Verification (§15) | Compensation runs automatically; escalate with full evidence |
| 5 | Verification never runs (engine crash) | Cluster left hand-edited | Watchdog scan of `awaiting_verification` | Force-revert after 15 min; alert on `healops_watchdog_actions_total` |
| 6 | Rollback lands on an equally broken revision | Incident persists, looks fixed | Verification + `new_alerts` check | Marked `not_recovered`, compensated, escalated; recurrence counter increments |
| 7 | GitOps controller reverts the fix | Fix silently disappears | `gitops_managed` + sync status | Ephemeral/durable classification (§17); controller paused for the TTL; PR for durable |
| 8 | Durable PR never merged | Ephemeral fix expires, incident returns | PR age metric | Nag in Slack at 24h; recurrence counter; masking guard escalates |
| 9 | Approval nobody answers | Incident unmitigated | DB `expires_at` reaper | 10-min expiry → escalate with `@oncall`; never auto-execute on timeout |
| 10 | Redis restart loses pending approvals | Could execute unapproved action | Postgres is source of truth | Fail-closed `can_execute()` (§C.3) |
| 11 | Slack API outage | No notifications or approvals | Breaker on Slack client | Actions requiring approval are **held**, not executed; ledger + dashboard keep working; digest posted when Slack returns |
| 12 | Kubernetes API degraded | All remediation | Error-rate metric | Transport breaker → policy breaker trips → escalate to humans |
| 13 | In-place resize returns `Infeasible` | One pod, no change | Pod conditions | Abandon immediately (never wait), fall back to the template plan **with approval** |
| 14 | Scale-up fights an HPA | Replica oscillation, cost | Autoscaler detection; replica timeline | Patch the HPA target instead, or refuse (§C.4) |
| 15 | Scale-up cost runaway | Cloud bill | Ceiling + projected-cost policy rule | Hard `MAX_REPLICAS_CEILING`; approval above ₹500/month; ephemeral TTL returns it |
| 16 | Disk cleanup deletes something needed | Data loss (partially irreversible) | Dry-run listing in the plan; S3 archive first | Archive-then-delete, never delete-only; 7-day floor; refuse if S3 write fails |
| 17 | Node drain overloads remaining nodes | Cluster-wide | Capacity precheck in `plan()` | Refuse if remaining allocatable < 120% of drained requests; respect PDBs; approval always |
| 18 | Automation masks a recurring bug | Long-term reliability debt | Recurrence counter (§16.4) | Demote to approval + open a GitHub issue with evidence |
| 19 | Ledger tampering or corruption | Audit integrity | Hourly `verify_chain` + `healops_ledger_valid` | Append-only grants (`REVOKE UPDATE, DELETE`), signed bundles, offsite backup |
| 20 | Runbook regression from a code change | Wrong actions at scale | CI decision gate + chaos tests | Merge blocked; any runbook change demotes it to approval mode (§19) |
| 21 | Secret leak (Slack token / GitHub PAT) | External blast radius | Least-privilege scopes; secret scanning in CI | Fine-grained PAT (contents + PR on one repo), Slack app scoped to one channel, rotation runbook documented |
| 22 | Prometheus/PSI metrics missing on some nodes | Verification quality | Empty series detection | `inconclusive` + escalate; never treat a missing signal as healthy |

---

## 25. Deployment Strategies

### 25.1 Environments and promotion

| Environment | Cluster | HealOps mode | Purpose |
|---|---|---|---|
| **local** | `kind` (1.36) + chaos app | all runbooks `shadow` | Development; the chaos harness lives here |
| **staging** | small cloud cluster (or a second `kind` namespace) | `approval` everywhere | Where runbooks earn their promotion evidence |
| **prod (demo)** | GKE/EKS small cluster | `auto` for runbooks 1–3 in `shop`; `approval` elsewhere | The public live link on the résumé |

Promotion of the **software** is `git push` → CI → Helm; promotion of the **automation authority** is the ladder in §19. Those are two different pipelines, and separating them is the sophisticated bit: *"shipping a new version of the engine never silently grants a runbook more power — any runbook change resets it to approval mode."*

### 25.2 How HealOps itself is deployed

- **Rolling update, `maxUnavailable: 0`, `maxSurge: 1`**, readiness on `/healthz` (checks Postgres, Redis, K8s API, Alertmanager reachability). The webhook must stay reachable during a deploy or Alertmanager retries pile up.
- **`helm upgrade --install --atomic --wait`** so a bad release rolls itself back. With Helm 4, remember the `watch` RBAC verb for `--wait`.
- **Drain semantics:** on `SIGTERM` the engine stops consuming new stream messages, finishes any in-flight execution, and lets the leader `Lease` expire. `terminationGracePeriodSeconds: 60`. An engine killed mid-execution is exactly what the watchdog exists for, but it shouldn't happen on a routine deploy.
- **DB migrations: expand-contract.** Additive migration ships with release N; destructive change waits for N+1 once no old pods remain. Alembic, forward-only, with a tested down-path for the additive step.
- **Canary for the engine itself:** deploy the new version with `mode=shadow-only` for one incident cycle in staging (it plans but does not act), compare its plans against the current version's — a **plan diff** between engine versions. That's regression testing for a decision system, and it's the natural extension of shadow mode.
- **Rollback:** `helm rollback healops <REV>`; the ledger records the deploy events, so an incident can be correlated with the engine version that handled it (`planner_version` is on every plan).

### 25.3 Public deployment (the résumé link)

Two-tier, so the public link is safe:

1. **Live demo dashboard** (Vercel or the cluster's ingress): read-only, seeded with real recorded incidents from the demo cluster, plus `healopsctl replay` output rendered as pages. No cluster credentials, no write path.
2. **The real engine**, only in the demo cluster with the chaos app, behind auth. The public site links a **2-minute video** of the live "5 faults → 3 auto-fixed" run and the replayable evidence bundles.

Never expose an endpoint that can trigger remediation from the public internet. Say that during the interview — *"the public demo is read-only by design; the only write path to the cluster is inside the cluster"* — because someone will ask.

---

## 26. README Blueprint with Architecture Diagram

````markdown
# HealOps — Self-Healing Infrastructure with Automated Remediation Runbooks

> Fix the incident before the engineer wakes up — and prove it worked.

[![CI](badge)](link) [![chaos tests](badge)](link) [![decision gate](badge)](link)
**Live demo:** https://healops.<you>.dev · **2-min video:** <link> · **Replay an incident:** `healopsctl replay 9f2c8ab4`

## What it does
Prometheus detects a problem → Alertmanager posts a webhook → HealOps **correlates** alerts into one
incident, **plans** a remediation (with blast radius, undo, and expected recovery signal), checks the
plan against **policy**, executes it (or asks a human in Slack), then **verifies** that the incident
actually cleared — and **reverts itself** if it didn't. Every step is a hash-chained audit record.

**Demo scorecard: 5 faults injected → 3 auto-remediated → 2 escalated for approval → 0 false remediations.**

## Architecture
```
                    ┌──────────────┐   scrape    ┌────────────────────┐
                    │ chaos app +  │────────────▶│ Prometheus 3.x     │
                    │ your workloads│             │ + alert rules      │
                    └──────────────┘             └─────────┬──────────┘
                                                           │ firing
                                                  ┌────────▼──────────┐
                                                  │ Alertmanager 0.33 │
                                                  │ group + inhibit   │
                                                  └────────┬──────────┘
                                                           │ webhook (v2 payload)
┌──────────────────────────────────────────────────────────▼──────────────────────────┐
│ HealOps engine (FastAPI, Python 3.14)                                               │
│                                                                                     │
│  correlate ─▶ plan ─▶ policy gate ─▶ [shadow│approval│execute] ─▶ verify ─▶ compensate│
│      │          │         │(CEL)          │                        │          │      │
│      │          │         │               │                        │          │      │
│  incidents   RemediationPlan          Redis Streams            Prometheus   K8s API  │
│      └──────────┴─────────┴───────────────┴────────────────────────┴──────────┘      │
│                                    │                                                │
│         PostgreSQL 18 (plans · executions · approvals · hash-chained ledger)         │
│         Valkey 9.1 (queue · locks · dedup · cache)                                   │
└───────┬───────────────────────────┬───────────────────────────┬─────────────────────┘
        │ Slack (Block Kit)         │ GitHub (durable fix PRs)  │ S3 (log archive)
        ▼                           ▼                           ▼
  approve / "was this correct?"   PR #128                    archived logs
        │
        ▼
  Next.js 16 dashboard: incident timeline · efficacy scorecard · promotion ladder
```

## Quick start
```bash
kind create cluster --config k8s/kind/cluster.yaml      # Kubernetes 1.36
helm install kps prometheus-community/kube-prometheus-stack --version <PINNED> -f k8s/prometheus/values.yaml
helm install healops charts/healops -f charts/healops/values-local.yaml
helm install chaos  charts/chaos-app                    # the app we break on purpose
make demo                                               # injects 5 faults in sequence
```

## The five runbooks
| # | Trigger | Mitigation (ephemeral) | Durable fix | Reversible | Default mode |
|---|---|---|---|---|---|
| 1 | KubePodCrashLooping | `rollout undo` | PR pinning the known-good image | yes | auto |
| 2 | NodeDiskPressure | archive logs > 7d to S3, then delete | PR adding log rotation | partial | auto |
| 3 | HighPodCPU | scale replicas **or** patch the HPA target | PR adjusting requests/HPA | yes | auto |
| 4 | KubePodOOMKilled | **in-place memory resize (no restart)** | PR raising the limit | yes | auto* |
| 5 | KubeNodeNotReady | cordon + drain | PR / node pool change | partial | approval |

\* auto only where the promotion ladder has earned it; `approval` in `payments` by default.

## Screenshots
1. Incident timeline with the plan → policy → execute → verify trail
2. Slack: an auto-remediation card with the plan, blast radius, and "was this correct?"
3. Slack: an approval request for the node drain, with the capacity precheck shown
4. The efficacy scorecard (auto-resolution rate, MTTR, **false-remediation rate: 0**)
5. The promotion ladder table (runbook × namespace → shadow / approval / auto)
6. `healopsctl replay <sha>` output — offline, cluster disconnected
7. A masking-guard GitHub issue: "crashloop_rollback has remediated shop/api-gateway 4× in 7 days"

## Design decisions
- **Why plain PostgreSQL, not TimescaleDB** → §6
- **Why Redis Streams, not Kafka** (and why Zookeeper isn't a queue) → §7
- **Scaling decision 1:** per-resource locking over unbounded parallelism → §11
- **Scaling decision 2:** bounded automation — budget + circuit breaker → §16.5
- **Why every fix is ephemeral or durable** (GitOps drift) → §17
- **How a runbook earns the right to run unattended** → §19

## What HealOps deliberately does NOT do
Multi-cluster federation · LLM-driven decisions (the LLM only writes postmortems) · arbitrary
user-defined runbooks · database or network remediation · anything irreversible.
````

---

## 27. Interview Prep — Rev 2 Additions

**Q: Isn't this just Robusta?**
No, and knowing where they overlap is the point. Robusta is excellent at alert enrichment, smart grouping, change tracking and a self-healing rule engine, and it's more mature than anything I'd write in seven weeks. What I built are the parts that follow the action rather than precede it: a **plan object** with blast radius and a registered undo, **closed-loop verification with automatic compensation**, a **promotion ladder** that turns "was this correct?" feedback into real authority changes, a **hash-chained signed audit ledger** with offline replay, **GitOps-aware ephemeral-versus-durable classification**, and a **masking guard** that files a bug when automation keeps hiding one. If I were doing this at work I'd probably run Robusta for detection and enrichment and build my verification and audit layers on top.

**Q: How do you know a remediation actually worked?**
I don't accept "the API call returned 200" as success. Verification asks three questions over a window: did the alert clear in Alertmanager, did the golden signals recover (no new OOM events, restarts stable, throttle ratio and PSI under threshold), and did any new alert appear on the same target that my action could have caused. If any answer is wrong, the compensating action I registered *before* executing runs automatically and the incident escalates to a human. If the signals aren't available, the outcome is `inconclusive` — never success.

**Q: What if the automation makes things worse?**
That's the case the design is built around. The compensation is decided and persisted before the action, so undoing never depends on the engine still being alive; a watchdog CronJob reverts anything whose verification never completed. Beyond one incident, three compensations in fifteen minutes trips a global breaker and automation stops entirely until a human clears it. And `false-remediation rate` is a metric on the dashboard, so the failure mode is visible rather than anecdotal.

**Q: Doesn't self-healing hide real bugs?**
Yes — that's the honest criticism, and it's why I built the recurrence guard. HealOps counts remediations per (runbook, target) over a rolling week. On the fourth, it stops auto-fixing that workload, demotes the runbook to approval mode for that namespace, and opens a GitHub issue containing the full timeline, every plan, and the evidence bundles. Automation that keeps papering over a defect files a bug report against it.

**Q: What happens in a GitOps cluster — won't Argo CD undo your fix?**
It would, and that's a defect I found in my own first revision. Now every target is classified. If Argo CD or Flux manages it, the imperative action is an explicit **ephemeral mitigation** with a TTL, and HealOps pauses reconciliation for exactly that window. Then, only after verification proves the mitigation worked, it opens a **pull request** so git becomes the source of truth again. The mitigation buys minutes; the PR is the fix.

**Q: Why is the OOMKill runbook automatic now when it needed approval before?**
Because the mechanism changed. In-place pod resize went GA in Kubernetes 1.35, so I raise the memory limit through the `pods/resize` subresource with **no container restart** — the action became reversible with a blast radius of one pod, which is exactly the auto-eligible profile. The durable template change still goes through a PR. If the container declares `resizePolicy: RestartContainer` for memory, the runbook refuses and escalates, because otherwise "no restart" would be a lie.

**Q: How do you decide what to automate?**
I don't decide it up front — it's earned per namespace. New runbooks start in shadow mode, producing plans they can't execute; I diff those against what on-call actually did. Ninety percent agreement over ten incidents promotes to approval mode; twenty consecutive verified-good outcomes promotes to auto. One bad outcome demotes immediately, and any change to the runbook's code or the policy file resets it to approval.

**Q: Where does the confidence number come from, and why should I believe it?**
It comes from deterministic rules — evidence strength, historical outcomes for that runbook and target, data completeness. You should believe it because it's graded: CI replays a labelled corpus of chaos-generated incidents and fails the build if top-1 accuracy drops below 0.90, if the Brier score exceeds 0.08, if expected calibration error exceeds 0.10, or if the planner stops being deterministic. A confidence score nobody scores is decoration.

**Q: How would a bank let a bot change production?**
Change management, which is a paperwork problem before it's a technical one. Every action produces a change record: a hash-chained ledger entry, an approver identity from Slack or an explicit "automated under policy X with promotion state auto", the compensating action registered beforehand, and a cosign-signed evidence bundle. Hand me a hash and I'll replay the entire incident offline and prove nobody edited the record afterwards.

**Q: Why PostgreSQL here when you used TimescaleDB on your other project?**
Because the data is different. Here it's under 100 alerts an hour — low-frequency events with a strongly relational shape: alert → incident → plan → execution → approval → verification. Foreign keys and transactions are the requirement, and the audit trail must be ACID. In KubeThrifty the data was 40,000+ metric rows per pod per week needing rolling-window percentile aggregation, which is exactly what hypertables and continuous aggregates are for. Same engineer, different problem, different tool — and I upgraded to PostgreSQL 18 specifically for `uuidv7()`, because timestamp-ordered primary keys are what you want for an append-only ledger.

**Q: Your engine has 2 replicas but only one acts. Isn't that wasted capacity?**
It's deliberate. Webhook availability and execution capacity are different requirements: Alertmanager must always find someone to accept a POST, but remediation must be serialised or two replicas will act on the same incident. So both replicas serve webhooks and enqueue, and a Kubernetes `Lease` elects the one that executes. The bottleneck I'm protecting isn't CPU, it's *risk*.

**Q: What's the hardest bug you hit?**
Approval state. I originally kept it in Redis with a 10-minute TTL and treated "key missing" as "timed out". Then I restarted Redis during testing and realised the executor couldn't distinguish "expired" from "lost" — which meant it could either drop an incident silently or, worse, execute something nobody approved. Now PostgreSQL owns the decision with an explicit `expires_at`, Redis is only a cache, and the authorisation check fails closed. It taught me that a safety control can't live in a cache.

**Q: What would you build next?**
Multi-cluster with a federated ledger, a plan-diff regression harness between engine versions, and runbooks defined as declarative YAML with the same plan/verify/compensate contract so other teams could add their own without touching the engine. I'd also want SLO-burn-rate as a policy input, so remediation urgency is tied to error budget rather than to alert severity labels.

---

## 28. Build Order, Competitive Grid, Schedule, Demo Script, Fact-Check

### 28.1 What to actually build in 7 weeks

| Tier | Feature | Effort | Interview value | Decision |
|---|---|---|---|---|
| 0 | MVP: Alertmanager → webhook → 5 runbooks → Slack → Postgres → dashboard → Helm → CI | 4 weeks | Table stakes | **Ship** |
| 1 | **§14 Plan-then-apply + policy gate (CEL)** | 3–4 days | Very high — reframes the whole project | **Ship first** |
| 1 | **§15 Verification + compensation + scorecard** | 4–5 days | **Highest** — this is the credibility feature | **Ship** |
| 1 | **§16 Correlation + budget + breaker + masking guard** | 3 days | Very high, cheap; the storm demo | **Ship** |
| 1 | **§21 Chaos tests per runbook (fix/compensate/refuse)** | 3 days | Very high — proves everything above | **Ship** |
| 2 | **§17 GitOps-aware ephemeral/durable + PR generation** | 3 days | High — the "nobody thinks of this" feature | **Ship if week 6 is on time** |
| 2 | **§18 In-place resize for Runbook 4** | 1–2 days | High per hour spent; very current | **Ship** |
| 2 | **§19 Promotion ladder + shadow mode** | 3 days | High — answers the hardest question | **Ship (ladder before calibration)** |
| 3 | **§19.2 Calibration gate + generated corpus** | 3 days | High for AI/ML-flavoured roles | Ship if time; otherwise ship the corpus + accuracy gate only |
| 3 | **§20 Hash-chained ledger + signed bundles + replay** | 2–3 days | High in enterprise interviews | **Ship the chain + replay**; signing is one CI step |
| 4 | LLM postmortem narrator | 1 day | Medium | Optional, off by default |
| 4 | Multi-cluster, YAML-defined runbooks, PagerDuty, SLO-driven policy | — | Low now | README roadmap |

**Rule:** if a feature can't appear in the 5-minute demo (§28.4) or in one README screenshot, it belongs in the roadmap.

### 28.2 Head-to-head grid (put this in the README)

| Capability | Robusta | StackStorm / Rundeck | Argo Workflows via webhook | k8sgpt / AI SRE agents | **HealOps** |
|---|---|---|---|---|---|
| Alert enrichment & grouping | ✅ (best in class) | ❌ | ❌ | partial | ✅ (correlation by topology) |
| Runbook automation | ✅ | ✅ (very mature) | ✅ | partial | ✅ |
| **Plan object reviewed before execution** | ❌ | ❌ | ❌ | ❌ | ✅ §14 |
| **Blast-radius + reversibility classification** | ❌ | ❌ | ❌ | ❌ | ✅ §14 |
| **Policy-as-code gate on every action** | partial | partial | ❌ | ❌ | ✅ §14.3 (CEL, in git) |
| **Closed-loop verification of recovery** | ❌ | ❌ | ❌ | ❌ | ✅ §15 |
| **Automatic compensation when it didn't work** | ❌ | ❌ | ❌ | ❌ | ✅ §15.2 |
| **False-remediation rate reported** | ❌ | ❌ | ❌ | ❌ | ✅ §15.3 |
| Human approval in chat | ✅ | ✅ | partial | ✅ | ✅ |
| **Approval feedback changes future authority** | ❌ | ❌ | ❌ | ❌ | ✅ §19 (promotion ladder) |
| **Shadow mode for new automation** | ❌ | partial | ❌ | ❌ | ✅ §19.3 |
| **Calibrated confidence gated in CI** | ❌ | ❌ | ❌ | ❌ | ✅ §19.2 |
| **GitOps drift-aware (ephemeral vs durable)** | ❌ | ❌ | ❌ | ❌ | ✅ §17 |
| **Durable fix as a pull request** | ❌ | ❌ | ❌ | ❌ | ✅ §17.3 |
| **Non-disruptive memory fix (in-place resize)** | ❌ | ❌ | ❌ | ❌ | ✅ §18 |
| **Remediation budget + circuit breaker** | ❌ | ❌ | ❌ | ❌ | ✅ §16 |
| **Anti-masking: files a bug when it hides one** | ❌ | ❌ | ❌ | ❌ | ✅ §16.4 |
| **Tamper-evident ledger + offline replay** | ❌ | audit logs | ❌ | ❌ | ✅ §20 |
| Chaos-tested remediations in CI | ❌ | ❌ | ❌ | ❌ | ✅ §21 |
| Maturity, integrations, support | ✅✅ | ✅✅ | ✅ | ✅ | ❌ (7-week project) |

Then the honest paragraph, because a table with no losses reads as marketing:

> **Where they win:** Robusta has years of production hardening, dozens of integrations, and enrichment I didn't attempt. StackStorm is a general-purpose automation platform with a huge action library. Argo Workflows gives you durable, retryable DAGs for free. AI-agent products investigate open-ended problems mine won't touch. HealOps is not a replacement for any of them — it's a demonstration of the *safety envelope* I think automated remediation should have: plan, gate, act, verify, revert, audit, and earn authority over time.

### 28.3 Revised 7-week schedule

| Week | Focus | Deliverable that proves it |
|---|---|---|
| **1** | kind 1.36 + kube-prometheus-stack (pinned) + Alertmanager 0.33 (**API v2**, UTF-8 matchers, `inhibit_rules`) + chaos app with all 5 faults | 5 alerts firing; a webhook receiver logging v2 payloads |
| **2** | FastAPI engine: webhook → **correlation (§16.1)** → incident model → Postgres 18 schema + ledger (§20.1) → Redis Streams queue | One node fault produces **one** incident, not 40; ledger chain verifies |
| **3** | **Plan-then-apply (§14)**: Plan object, CEL policy file, runbooks 1–3 with `plan/execute/compensate` | `healopsctl plan <incident>` prints a plan; policy denies a StatefulSet |
| **4** | **Verification + compensation (§15)** + Slack Bolt (cards, approval, "was this correct?") + approvals in Postgres (§C.3) | A deliberately bad rollback is **auto-reverted in 90s** and escalated |
| **5** | Runbooks 4–5: **in-place resize (§18)**, cordon+drain with capacity precheck; **budget + breaker + masking guard (§16)** | OOM fixed with `restartCount` unchanged; storm demo; a masking issue filed |
| **6** | **GitOps awareness + durable PRs (§17)** + **promotion ladder & shadow mode (§19)** + Next.js 16 dashboard (timeline, scorecard, ladder) | Argo CD-managed app: ephemeral fix + PR #1; a runbook promoted on evidence |
| **7** | **Chaos tests in CI (§21)** + decision/calibration gate (§19.2) + `healopsctl replay` + Helm 4 chart + public deploy + README/video | CI red when a runbook's compensation stops restoring state; live link |

If you fall behind, cut in this order: calibration gate → signing → LLM narrator → promotion ladder (keep shadow mode) → GitOps PRs (keep ephemeral TTL). **Never cut verification or compensation** — they are the project.

### 28.4 The 5-minute demo script (rehearse out loud)

1. **(30s) The premise.** Dashboard, zero incidents. *"Five faults, in sequence. Watch what the system does and — more importantly — what it refuses to do."*
2. **(60s) Auto-remediation with proof.** Trigger CrashLoop. Slack card appears: plan, blast radius, undo. Then the *verification* line 90 seconds later: alert cleared, restarts stable. *"Success means the incident is over, not that my API call returned 200."*
3. **(45s) The storm.** Taint a node → 12 pod alerts. Dashboard shows **one** incident. *"Cause-first correlation. A naive system would have rolled back twelve healthy deployments."*
4. **(45s) The non-disruptive fix.** Trigger OOMKill → in-place resize. `kubectl get pod -w` alongside: memory limit changes, `restartCount` stays 0. *"In-place resize went GA in 1.35, so this fix no longer restarts the service."*
5. **(45s) The refusal.** Trigger CrashLoop on a StatefulSet. Slack: *"DENIED by policy: stateful workloads are excluded."* *"Knowing what not to automate is the feature."*
6. **(45s) Self-correction.** Force a bad rollback. Verification fails → compensation runs → escalation with evidence. *"That's the number I'm proudest of: it caught itself in 90 seconds."*
7. **(45s) The audit.** `healopsctl replay <sha>` with the cluster disconnected → identical output. Then `--verify-chain`. *"Signed, replayable, tamper-evident. This is how a regulated company would let a bot change production."*
8. **(15s) The scorecard.** *"3 of 5 auto-resolved, 2 escalated, 1 policy refusal, 0 false remediations. It pages me only for what needs judgement."*

### 28.5 Résumé and README one-liners

- **Résumé bullet:** *"HealOps — self-healing Kubernetes platform (K8s 1.36, Python 3.14, PostgreSQL 18, Helm 4). Correlates Prometheus alerts into incidents, generates reviewable remediation plans with blast radius and undo, executes within a policy gate, then **verifies recovery and auto-reverts** when it fails; ships GitOps-safe ephemeral fixes plus durable PRs, a hash-chained signed audit ledger with offline replay, and chaos-tested runbooks in CI. Live: <link>"*
- **The two sentences to close on:** *"Every self-healing demo you'll see runs `kubectl rollout undo` and calls it fixed. Mine plans the fix, checks it against policy, applies it, then verifies the incident is actually over — and reverts itself in 90 seconds when it isn't. And when it notices it's fixed the same thing four times in a week, it stops and files a bug, because automation that hides a defect is a liability, not a feature."*

### 28.6 Fact-Check Appendix — every version claim, with where to re-verify

Bring this. *"Pinned as of 18 Aug 2026, here's where each number came from"* is a credibility multiplier — and it stops a stale claim becoming an embarrassing one.

| Claim | Status / date | Re-verify at |
|---|---|---|
| **PostgreSQL 18.6** current (released 11 Aug 2026); 18.4 was 14 May 2026; **PG 19 in Beta 3** | Aug 2026 | postgresql.org/support/versioning + release notes |
| PG 18 features used: **`uuidv7()`**, virtual generated columns, **temporal constraints** (range PK/UNIQUE/FK), async I/O subsystem | PG 18 GA | postgresql.org/docs/18/release-18.html |
| PostgreSQL 14 stops receiving fixes **12 Nov 2026** | Aug 2026 | postgresql.org versioning policy |
| **Alertmanager 0.33.1** (4 Jul 2026); **API v1 removed in 0.27**; UTF-8 matcher parser is the modern path | Aug 2026 | github.com/prometheus/alertmanager/releases + CHANGELOG |
| **Prometheus 3.x** (3.11.2 current in Apr 2026 — **check the current patch**) | Apr 2026 | prometheus.io/download |
| **Kubernetes 1.36** current, **1.35** supported floor; 1.34 → maintenance 27 Aug 2026 | Aug 2026 | kubernetes.io/releases |
| **In-place pod resize GA in 1.35**; `pods/resize` subresource; `PodResizePending{Deferred,Infeasible}`; `resizePolicy` | Dec 2025 → Aug 2026 | K8s 1.35 release blog + "Resize CPU and Memory Resources assigned to Containers" |
| **cgroup v2 required by K8s 1.35+**; **PSI metrics** beta 1.34 → GA 1.36 | Aug 2026 | K8s release notes; kubernetes.io PSI docs |
| **Helm 4.2.x**; **Helm 3 bug fixes ended 8 Jul 2026**, security-only to 10 Feb 2027; SSA default, `--wait` needs `watch`, post-renderer → plugin | Aug 2026 | helm.sh/blog/helm-v3-end-of-life + Helm 4 notes |
| **Python 3.14** current stable; **slack-bolt 1.28.x** added 3.14 support; **FastAPI 0.128.x** current line | Aug 2026 | python.org; github.com/slackapi/bolt-python/releases; pypi |
| **Node 24 LTS** (EOL Apr 2028); **Next.js 16.3.x**; Next 15 EOL 21 Oct 2026 | Aug 2026 | nodejs.org/en/about/previous-releases; nextjs.org/blog |
| **Valkey 9.1** (19 May 2026, BSD-3); **Redis 8.x** current | Aug 2026 | github.com/valkey-io/valkey/releases |
| **Grafana 13.0** (Apr 2026); kube-prometheus-stack chart ~88.x | Aug 2026 | grafana.com release notes; prometheus-community/helm-charts releases |
| **Kafka no longer requires Zookeeper** (KRaft) | evergreen | kafka.apache.org documentation |
| Competitive claims about Robusta / StackStorm / Argo Workflows / k8sgpt | as of Aug 2026 | **Open each project's README before publishing the grid in §28.2** — narrow any row you can't verify |

**Maintenance habit to mention:** Renovate opens the version-bump PRs, CI runs the decision gate and chaos tests against them, and this appendix gets a date bump each time. *"My dependency currency is automated; my claims about it are dated."*

---

## Final Word — Why This Project Wins DevOps Interviews (Rev 2)

HealOps covers every box on a DevOps/SRE scorecard, and Rev 2 adds the thing a scorecard can't capture: evidence of judgement about *when not to act*.

**Kubernetes depth:** programmatic API use rather than YAML — deployments, rollout history, evictions with PDB awareness, cordon/drain with a capacity precheck, the **`pods/resize` subresource** and what actually restarts, `resourceVersion` preconditions for optimistic concurrency, `Lease`-based leader election, minimal RBAC where the only mutating verbs are the ones a runbook provably needs, and the interaction between your controller and *someone else's* controller (Argo CD/Flux drift).

**Observability depth:** Prometheus alert rules with `for:` semantics, Alertmanager grouping and — the part most people skip — **`inhibit_rules`**, the v2 API, PSI and cgroup-based OOM signals rather than flapping status gauges, ratios instead of raw counters, and self-monitoring of the automation itself (efficacy, budget, breaker state, promotion mode).

**CI/CD and testing:** lint → unit → **decision gate** (accuracy, calibration, determinism, refusal recall) → **policy gate** → **chaos tests on a real `kind` cluster** (fix, compensation, refusal per runbook) → build → cosign sign → `helm upgrade --atomic`. The output artefact of the durable path is a **pull request**, which is the point.

**Data and state:** plain PostgreSQL 18 chosen deliberately over TimescaleDB with the reasoning stated, `uuidv7()` for an append-only hash-chained ledger, monthly partitions, `REVOKE UPDATE, DELETE` so the audit table can't be rewritten by the app, expand-contract migrations, and Redis/Valkey treated as genuinely disposable.

**SRE reasoning — the part that separates you:**
- Success means *the incident is over*, not *the API call returned 200*.
- The compensation is decided before the action, and a watchdog owns it — because a compensation held by the process that can crash is not a compensation.
- One root cause is one incident. Automating a stampede is worse than automating nothing.
- Automation needs an error budget: if it fires more than twenty times an hour, the cluster has a systemic problem and a human should be looking at it.
- In a GitOps cluster, an imperative fix is a *mitigation with a TTL*, and the real fix is a pull request.
- Self-healing that hides a bug is a liability, so on the fourth repeat it stops and files the bug.
- A confidence number nobody grades is decoration; grade it in CI.
- Authority is earned per namespace: shadow → approval → auto, with instant demotion.
- Knowing what your automation refuses to touch is a feature, and refusals belong in the audit trail.

**The DevOps maturity hierarchy, updated:**
Level 1: *"I deployed to Kubernetes."*
Level 2: *"I set up monitoring and alerting."*
Level 3: *"My system fixes the incidents that monitoring detects."*
**Level 4 (you):** *"My system fixes them, **proves the fix worked**, reverts itself when it didn't, refuses what it shouldn't touch, files a bug when it notices it's masking one, and can hand you a signed, replayable record of every decision it made."*

**The sentence that wins:** *"I built a self-healing platform that fixed 3 of 5 incidents automatically in my demo and paged me for the 2 that needed judgement — but the part I'd actually want to talk about is that it verifies recovery and reverts itself when it's wrong, treats an imperative fix in a GitOps cluster as a TTL'd mitigation with a pull request behind it, and stops auto-fixing anything it has already fixed four times this week."*

**The follow-up that closes it:** *"I also found five real defects in my own first revision. The worst was that 'success' meant the Kubernetes API returned 200 — so a rollback to an equally broken revision was recorded as a successful auto-remediation. That's not a bug in a runbook, it's a bug in the definition of done. All five are in the CHANGELOG, because finding your own bugs before an interviewer does is the whole job."*

No other fresher in that interview room will demo automated incident response that grades itself.

---

*Document prepared as a complete implementation guide for HealOps — Self-Healing Infrastructure with Automated Remediation Runbooks. Revision 2, 18 August 2026. Version claims dated and sourced in §28.6; re-verify before quoting any of them after ~November 2026.*

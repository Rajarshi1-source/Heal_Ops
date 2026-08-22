---
name: healops-kubernetes-platform
description: "Build and maintain the HealOps cluster platform, everything outside the engine that makes remediation observable, deployable, and testable. Use for ANY work on Prometheus alert rules and PromQL, Alertmanager config with inhibit_rules and UTF-8 matchers, least-privilege RBAC, Helm 4 charts and values, the kind local cluster, the chaos app and its fault injectors, docker-compose, GitHub Actions CI and its merge gates, the kind-based chaos job, cosign signing, Grafana dashboards as JSON, and deployment strategy including expand-contract migrations and the read-only public demo. Trigger on Prometheus, PrometheusRule, PromQL, Alertmanager config, inhibit_rules, RBAC, ClusterRole, Helm, values.yaml, kind, kindest/node, chaos app, fault injector, docker-compose, GitHub Actions, CI gates, cosign, or Grafana dashboard. MANDATE: Kubernetes 1.36 demo with 1.35 as the floor, Helm 4.2.x, Alertmanager 0.33.1 on API v2, Grafana 13, and every image tag pinned exactly, never latest."
---

# HealOps Kubernetes Platform

Everything outside the engine: how alerts are produced, how HealOps gets permission to act, how it ships, and how CI proves it works.

## Version floor

| Component | Pin | Why |
|---|---|---|
| Kubernetes | **1.36** demo (`kindest/node:v1.36.x`), **1.35 floor** | 1.34 entered maintenance Aug 2026. In-place pod resize is GA in 1.35 — that is the floor's whole reason. 1.35+ requires **cgroup v2** |
| Helm | **4.2.x** | Helm 3 bug fixes ended July 2026 (security-only to Feb 2027) |
| Alertmanager | **0.33.1** | **API v1 removed in 0.27** — v2 only. UTF-8 matcher parser is the default path now |
| Prometheus | **3.x, chart-pinned** via `kube-prometheus-stack` (~88.x line) | Never `latest`; pin the chart version explicitly |
| Grafana | **13.0.x**, dashboards provisioned as JSON in-repo | Reproducibility |
| PostgreSQL | **18.6** (`postgres:18.6-alpine`) | `uuidv7()` for the ledger |
| Valkey / Redis | **valkey/valkey:9-alpine** or Redis 8.x | Streams are the queue |

### Three Helm 4 migration gotchas

1. **Server-side apply is the default for new installs**, so expect explicit conflict errors instead of silent overwrites — which is exactly what you want when Argo CD also touches these objects.
2. **`--wait` needs the `watch` RBAC verb** in the CI service account's role. This breaks CI silently on upgrade if missed.
3. **`--post-renderer` must now be a plugin.**

## Pin every tag — including the ones that are easy to forget

The plan's own §B says never `latest`, and the local-dev and chart files are where that rule gets broken. When touching `docker-compose.yml` or `charts/healops/values.yaml`, check for and fix:

- `minio/minio:latest` → a pinned release tag
- `grafana/grafana:latest` → `grafana/grafana:13.0.x`
- `engine.image.tag: latest` and `dashboard.image.tag: latest` → the git SHA that CI pushes
- The obsolete top-level `version: '3.8'` key in Compose — Compose v2 ignores it and warns
- If the `redis` service uses the `valkey/valkey:9-alpine` image, verify the `command:` and `healthcheck:` binaries actually exist in that image before relying on `redis-server` / `redis-cli`; prefer `valkey-server` / `valkey-cli` unless the compatibility symlinks are confirmed for the pinned tag

A document that mandates pinning while shipping `:latest` is the first thing a sharp reviewer notices.

## RBAC — least privilege, and be able to justify every verb

`k8s/rbac/healops-clusterrole.yaml` grants only what the five runbooks need:

| Resource | Verbs | Which runbook needs it |
|---|---|---|
| `deployments`, `deployments/scale` | get, list, watch, patch, update | Rollback (1), scale-up (3) |
| `replicasets` | get, list | Revision history for rollback |
| `pods` | get, list, watch, delete | State capture, restart paths |
| `pods/resize` | patch | In-place memory bump (4) — verify the subresource is exposed |
| `pods/log`, `pods/eviction` | get / create | Evidence capture; drain (5) |
| `nodes` | get, list, watch, patch | Cordon and uncordon (5) |
| `horizontalpodautoscalers` | get, list, patch | HPA-aware scale-up (3) |
| `events` | create | Kubernetes-native audit alongside the ledger |
| `coordination.k8s.io/leases` | get, create, update | Leader election between engine replicas |

What is **deliberately absent** is the interview answer: no `delete` on `persistentvolumeclaims`, no `secrets` read beyond its own namespace, no cluster-admin, no wildcard verbs. *"Every verb in that ClusterRole maps to a runbook I can name, and the destructive ones aren't there because the policy file denies those actions anyway — defence in depth."*

HealOps' own namespace carries `healops.io/remediation: disabled` so the engine cannot remediate itself.

## Prometheus rules and Alertmanager

Alert rules live in `charts/alerting-rules/templates/prometheusrule.yaml` as a `PrometheusRule` CR — five rules matching the five runbooks: `KubePodCrashLooping`, `NodeDiskPressure`, `HighPodCPU`, `KubePodOOMKilled`, `KubeNodeNotReady`.

Two PromQL habits carried from the verifier and worth applying to the rules themselves:

- Use the **dimensionless throttle ratio**, not raw throttled-period counters, wherever CPU pressure is expressed.
- Use `container_oom_events_total` (a counter) rather than the flapping `last_terminated_reason` gauge.

Alertmanager config (`k8s/prometheus/alertmanager-config.yaml`):

- **Webhook receiver targets `http://healops-engine:8000/api/webhooks/alertmanager`** with a shared secret.
- **`inhibit_rules` are the first line of storm defence** — a `KubeNodeNotReady` on a node inhibits `KubePodCrashLooping` for pods on it. Engine-side correlation is the second line, because inhibition cannot express owner-reference topology.
- **Enable UTF-8 strict mode** on a new config, since the UTF-8 matcher parser is now the default path.
- **Run HA mode** (3-replica gossip cluster in the `kube-prometheus-stack` values) so a pod restart cannot drop notifications. Alertmanager's cross-replica dedup means the webhook **must be idempotent** — it is, via `(fingerprint, runbook, incident_id)`.

Install Prometheus via `kube-prometheus-stack` with an explicit chart version, and keep custom alert routing in a values file under version control.

## The chaos app

A deliberately fragile three-to-five service app, one fault per alert type:

| Service | Fault | Trigger | Alert |
|---|---|---|---|
| `crashy-api` | CrashLoopBackOff | env `CRASH_AFTER=30s` | `KubePodCrashLooping` |
| `hungry-worker` | OOM kill | Allocates until killed | `KubePodOOMKilled` |
| `cpu-burner` | CPU spike | Tight loop on demand | `HighPodCPU` |
| `log-spammer` | Disk pressure | Writes 1 GB/min to `/var/log` | `NodeDiskPressure` |
| `flaky-node` (simulated) | Node not ready | Node taint injection | `KubeNodeNotReady` |

Each service name is the `chaos_fixture` value on the runbook that answers it — that string is the contract enforced in CI, so keep the names in sync.

The chaos harness has a second job beyond the demo: it **generates the labelled incident corpus** for the decision eval (`healops-mlops-promotion`) by capturing alert payloads plus cluster state as JSON bundles. Build the capture path when you build the injectors; retrofitting it is tedious.

LitmusChaos stays optional — custom Python injectors are the demo, Litmus is the "what I'd use at scale" answer.

## CI pipeline — the gates that can fail a merge

```yaml
jobs:
  unit:                     # pure planner/policy/verifier logic, no cluster
    - run: pytest -m "not chaos" --cov=engine --cov-fail-under=80

  decision-gate:            # accuracy + calibration + determinism + refusal recall
    - run: python -m evals.decision_eval
    - run: python -m evals.ledger_verify        # hash chain over the committed corpus

  policy-gate:              # the policy file is code, so it gets tested
    - run: python -m evals.policy_eval          # every rule: one must-allow, one must-deny

  chaos:                    # real kind cluster, real faults
    - uses: helm/kind-action@v1
      with: { node_image: "kindest/node:v1.36.0" }
    - run: helm install kps prometheus-community/kube-prometheus-stack --version <PINNED>
    - run: helm install healops charts/healops --set mode=test
    - run: pytest -m chaos --runbook=all        # fix + compensation + refusal, per runbook
    - if: failure()
      run: kubectl cluster-info dump --output-directory=/tmp/dump

  build-sign-deploy:
    needs: [unit, decision-gate, policy-gate, chaos]
    - run: docker buildx build --tag ghcr.io/OWNER/healops-engine:${{ github.sha }} --push .
    - uses: sigstore/cosign-installer@v3
    - run: cosign sign --yes ghcr.io/OWNER/healops-engine@${DIGEST}
    - run: helm upgrade --install healops charts/healops --wait --atomic \
             --set image.tag=${{ github.sha }}
```

Four things to preserve: separate gate jobs so a failure names its own cause; the chaos job needs a **real kind cluster** with the chart version pinned; the cluster dump on failure is what makes a flaky chaos test debuggable; and `build-sign-deploy` **needs all four gates**.

The chaos job is the slowest, so keep the unit and eval gates fast and independent — a developer should be able to fail cheaply before waiting on a cluster.

## Monitoring and the first scaling decision

Grafana 13, dashboards as JSON in `monitoring/grafana/`, provisioned rather than clicked. HealOps monitors itself: remediation latency, success rate, approval times.

Panels the safety pipeline adds: the incident timeline showing the plan → policy → execute → verify → compensate stages as a horizontal state trail; auto vs assisted vs escalated stacked over time; **false-remediation rate as a single big number, green when zero**; MTTR auto vs escalated; budget usage per namespace with breaker state; the promotion-ladder table (runbook × namespace → shadow/approval/auto); and the recurrence leaderboard of workloads automation keeps fixing.

The metric names are in `healops-python-engine`'s reference file. Both scaling decisions belong in the README: the first is documented in the plan's §11, the second is bounded automation (`healops-remediation-safety`).

## Deployment strategy

| Environment | Cluster | HealOps mode | Purpose |
|---|---|---|---|
| local | `kind` 1.36 + chaos app | all runbooks `shadow` | Development; the chaos harness lives here |
| staging | small cloud cluster or a second kind namespace | `approval` everywhere | Where runbooks earn promotion evidence |
| prod (demo) | small GKE/EKS cluster | `auto` for runbooks 1–3 in `shop`; `approval` elsewhere | The public live link |

**How HealOps itself deploys:**

- **Rolling update, `maxUnavailable: 0`, `maxSurge: 1`**, readiness on `/healthz` (checks PostgreSQL, Redis, K8s API, Alertmanager reachability). The webhook must stay reachable during a deploy or Alertmanager retries pile up.
- **`helm upgrade --install --atomic --wait`** so a bad release rolls itself back. Remember the Helm 4 `watch` verb.
- **Drain semantics:** on `SIGTERM` stop consuming new stream messages, finish any in-flight execution, let the leader `Lease` expire. `terminationGracePeriodSeconds: 60`.
- **Migrations are expand-contract**, forward-only via Alembic: additive with release N, destructive with N+1 once no old pods remain.
- **Canary the engine via shadow mode:** deploy the new version with `mode=shadow-only` for one incident cycle in staging and **diff its plans against the current version's**. That is regression testing for a decision system — the natural extension of shadow mode.
- **Rollback:** `helm rollback healops <REV>`. `planner_version` is on every plan, so an incident can be correlated with the engine version that handled it.

Two pipelines, deliberately separate: promotion of the **software** is `git push → CI → Helm`; promotion of the **automation authority** is the ladder. *"Shipping a new engine version never silently grants a runbook more power."*

## Public deployment — two-tier, so the link is safe

1. **Live demo dashboard** (Vercel or cluster ingress): read-only, seeded with real recorded incidents plus `healopsctl replay` output rendered as pages. No cluster credentials, no write path.
2. **The real engine**, only in the demo cluster with the chaos app, behind auth. The public site links a 2-minute video of the "5 faults → 3 auto-fixed" run and the replayable evidence bundles.

**Never expose an endpoint that can trigger remediation from the public internet.** Say it before being asked: *"the public demo is read-only by design; the only write path to the cluster is inside the cluster."*

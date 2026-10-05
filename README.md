# HealOps — Self-Healing Infrastructure with Automated Remediation Runbooks

> Fix the incident before the engineer wakes up — and prove it worked.

[![ci](https://github.com/Rajarshi1-source/Heal_Ops/actions/workflows/ci.yml/badge.svg)](https://github.com/Rajarshi1-source/Heal_Ops/actions/workflows/ci.yml)

**Replay a recorded incident:** `uv run python -m cli.healopsctl replay <sha>`, offline, using the
bundles in [`evidence/`](evidence/). **Model card:** [`RUNBOOKS.md`](RUNBOOKS.md).

## What it does

1. Prometheus detects a problem, and Alertmanager posts a webhook.
2. HealOps **correlates** the alerts into one incident.
3. It **plans** a remediation: blast radius, the undo, and the signal that would prove recovery.
4. It checks the plan against **policy** in git.
5. It records a shadow plan, asks a human, or acts.
6. It **verifies** that the incident actually cleared, and **reverts itself** when it did not.

Every step is a hash-chained audit record. That includes the steps where it decides *not* to act.

```
alert -> correlate -> PLAN -> policy gate -> [shadow | approval | execute] -> VERIFY -> compensate?
```

Success means the incident is over, never that an API call returned 200. A rollback onto an equally
broken revision is a failure, and it is recorded as one.

### Measured, not scripted

The scorecard below comes from the chaos suite running against a real kind cluster (Kubernetes
1.36.4, three workers). Every fault is real except the disk alert: kind cannot fill a disk. Every
verdict is the verifier's, read back through the engine's own API. The incidents are committed in
[`evidence/`](evidence/) as content-addressed bundles, each with its replay.

The final run, on 2026-10-05, started from a fresh install with an empty database. **All 16 chaos
tests passed.** Five faults went in:

| Fault (real unless noted) | Runbook | Authority | Verdict | Change applied after | Verified after | Evidence |
|---|---|---|---|---|---|---|
| CPU saturation from request load | `cpu_scale_up` 2 → 4 replicas | auto | **remediated** | 1 s | 312 s | `9c268ad7c325` |
| Bad config rollout, crash loop | `crashloop_rollback` | auto | **remediated** | 0 s | 309 s | `b91d29f8bca7` |
| Disk pressure (alert delivered by the suite) | `disk_cleanup`, 9 files, 61 MiB archived then deleted | approval | **remediated** | 21 s | 328 s | `acbf9f6efc1d` |
| Kubelet stopped, node NotReady | `node_drain` | approval | **remediated** | 8 s | 318 s | `fa26534659b0` |
| OOM kill | `oom_memory_bump` 256 → 384 Mi in place, no restart | auto | **remediated** | 2 s | 736 s | `00d82a6129c9` |

Both times are measured from when the incident opened. "Change applied after" is when the change
landed. "Verified after" is when the verifier called it remediated. Each verifier waits out its
committed window: 300 s, or 720 s for OOM, because that alert looks back ten minutes.

**The scorecard, as the engine itself computes it (`GET /api/stats`):**

| Metric | Value |
|---|---|
| Auto-resolved | **3** |
| Assisted (approved by a human) | **2** |
| False-remediation rate | **0 of 5** |
| MTTR, auto | 312 s |
| MTTR, assisted | 323 s |
| Refused | 43 of 48 incidents |

The 43 refusals are the four refusal fixtures, which crash-loop and OOM on purpose for the whole
run, refused every time they alerted:
- a StatefulSet;
- a single replica with nothing to roll back to;
- an opted-out workload;
- a container whose resize policy would restart it.

None of them was ever touched.

## Architecture

```
                    ┌───────────────┐   scrape   ┌────────────────────────┐
                    │ chaos app +   │───────────▶│ Prometheus 3.x         │
                    │ your workloads│            │ + HealOps alert rules  │
                    └───────────────┘            └───────────┬────────────┘
                                                             │ firing
                                                 ┌───────────▼────────────┐
                                                 │ Alertmanager 0.33.1 HA │
                                                 │ group · inhibit · v2   │
                                                 └───────────┬────────────┘
                                                             │ webhook (shared secret)
┌────────────────────────────────────────────────────────────▼────────────────────────┐
│ HealOps engine (FastAPI, Python 3.14) — 202 after a durable write; a Lease picks    │
│ the one replica that acts                                                           │
│                                                                                     │
│  correlate ─▶ plan ─▶ policy (CEL) ─▶ [shadow│approval│execute] ─▶ verify ─▶ compensate│
│                                                                                     │
│  PostgreSQL 18.6  plans · executions · approvals · hash-chained, append-only ledger │
│  Valkey 9.1       streams (exec / verify groups) · fenced locks · dedup             │
└──────┬────────────────┬──────────────────┬──────────────────┬──────────────────────┘
       │ Kubernetes API │ node-agent Jobs  │ S3 (log archive) │ Slack · GitHub (optional)
       ▼                ▼                  ▼                  ▼
  rollback · scale ·  archive-then-delete  SeaweedFS in kind  approvals · durable-fix PRs
  pods/resize · drain  on the node itself                     · masking issues

  Next.js 16.3 dashboard: incident trail · replay · scorecard · promotion ladder
```

The engine ingests and persists; **it never decides whether to act**. That is the policy gate's
job: deterministic rules in git, unit-tested, replayable and hashed. Any LLM writes prose only and
never alters a plan, a policy decision or a verdict.

## Quick start

You need Docker, kind 0.32, kubectl, Helm 4.2.x, [uv](https://docs.astral.sh/uv/) and Node 24 LTS.
The `make` targets expect a POSIX shell (Git Bash or WSL on Windows).

```bash
make install                       # uv sync
make test                          # hermetic unit suite, coverage gate 80%
make gates                         # decision, policy and ledger gates -- no cluster needed

make cluster observability         # kind 1.36.4 (by digest) + kube-prometheus-stack 88.4.0
make images deploy                 # build, kind load, alert rules + chaos app + HealOps chart
make chaos                         # fix + compensation + refusal per runbook, against the cluster
make corpus                        # harvest evidence + cases from that run, capture scenarios, freeze
make public-tier                   # rehearse the read-only public tier, seeded from this cluster
make demo                          # the five faults in sequence
```

Every runbook starts in **shadow** in every namespace. To let one act, grant it authority on the
record:

```bash
kubectl -n healops exec deploy/healops-engine -- \
  python -m cli.healopsctl promote crashloop_rollback chaos --to auto --reason "staging evidence"
```

The chaos suite does exactly this for each fix test, and puts the runbook back afterwards.

## The five runbooks

Full detail is in [`RUNBOOKS.md`](RUNBOOKS.md): triggers, undo, blast radius, refusals, recovery
criteria and chaos tests.

| # | Trigger | Mitigation (ephemeral, TTL'd) | Durable fix | Reversible | Usual effect once promoted |
|---|---|---|---|---|---|
| 1 | `KubePodCrashLooping` | Restore the previous pod template (JSON-patch replace, as `rollout undo` does) | PR pinning the verified image | yes | auto |
| 2 | `NodeDiskPressure` | Archive logs older than 7 days to S3, verify, then delete, on the node | none (nothing git declares) | partial | approval (node-scoped) |
| 3 | `HighPodCPU` | Scale out, or lower the HPA's target when an HPA owns the count | PR adjusting replicas / HPA | yes | auto under ₹500/month, else approval |
| 4 | `KubePodOOMKilled` | **In-place memory resize via `pods/resize`, no restart** | PR raising the template limit | yes | auto |
| 5 | `KubeNodeNotReady` | Cordon and drain through the Eviction API (PDB-aware, capacity precheck) | none | partial | always approval |

The auto/approval split is **computed by policy from plan properties**, never hardcoded per runbook.
A runbook earns authority per namespace on the promotion ladder:
- shadow → approval after 10 shadow plans with ≥90% agreement;
- approval → auto after 20 consecutive verified fixes;
- one bad outcome demotes it.

## How it is held to account

Four gates fail a merge, each in its own CI job, so a failure names its own cause
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)):

| Gate | What it proves |
|---|---|
| `unit` | ruff, mypy, and the hermetic suite (≥ 80% coverage) against real PostgreSQL 18.6 and Valkey 9.1 service containers |
| `decision-gate` | The planner, replayed over the [labelled corpus](corpus/): accuracy ≥ 0.90, Brier ≤ 0.08, ECE ≤ 0.10, **100% refusal recall**, and plan digests matching the frozen set for this `PLANNER_VERSION`. Then `ledger_verify` re-hashes every committed evidence bundle |
| `policy-gate` | Every rule in `policy/remediation.yaml` has a must-allow and a must-deny case |
| `chaos` | A real kind cluster with real faults: three tests per runbook, with a cluster dump on failure |

`build-sign-deploy` needs all four, plus the dashboard and chart checks. It then:
1. pushes images tagged with the commit;
2. signs them with **cosign keyless** (the workflow's OIDC identity is the signer);
3. attests the decision-eval report and the evidence index, and signs every evidence bundle;
4. deploys both tiers with `helm upgrade --rollback-on-failure --wait` (Helm 4's name for
   `--atomic`).

**The corpus is generated, not hand-written**
([`chaos-app/injectors/capture_corpus.py`](chaos-app/injectors/capture_corpus.py)), in two ways:
- `harvest` turns the incidents the chaos suite caused into cases, using the planner input the
  engine recorded on its own ledger.
- `scenarios` captures live cluster states the faults do not reach: an HPA, a replica ceiling, a
  GitOps-managed workload, a frozen breaker, a cordoned pool.

Labels come from the policy and runbook contracts, never from what the planner happened to decide.

**Every decision can be replayed and audited offline:**

```bash
uv run python -m cli.healopsctl replay 9f2c8ab4          # byte-identical every run; re-plans too
uv run python -m cli.healopsctl audit --bundle 9f2c8ab4   # exit 4 and the seq on any break
kubectl -n healops exec deploy/healops-engine -- python -m cli.healopsctl audit --verify-chain
```

Edit any ledger row as the database superuser and `audit --verify-chain` names the sequence number
where the chain breaks. Edit a bundle and it no longer matches its own address. Re-address the
forgery and the chain inside it still breaks at the edited entry.

## What the chaos suite caught

This is the reason the chaos suite exists. Each item below passed its unit tests, then broke against
a real API server or a real cluster. Each now has a regression test.

- **The rollback didn't roll back.** A strategic-merge patch cannot remove an env var the bad
  revision added, so pods kept crashing behind a "successful" rollback. It is now a JSON-patch
  `replace`, as `kubectl rollout undo` does.
- **The undo couldn't find its revision.** Kubernetes renumbers a reused ReplicaSet, so "revision 8"
  had become revision 10 by the time compensation ran. Templates are now identified by digest.
- **The crashloop runbook refused bad config rollouts.** "Previous revision has the same image" was
  treated as "nothing to roll back to". It now compares the whole pod template.
- **Every in-process resize raised before reaching the server.** The kubernetes client 36 renamed a
  keyword in `call_api`. A test now checks the call against the installed client's signature.
- **Every in-cluster disk scan read as "no result".** The client returns pod logs as `str(bytes)`,
  and the agent was also missing the ConfigMap holding the S3 endpoint.
- **The disk alert and its recovery check matched nothing.** kube-prometheus-stack's node-exporter
  series carry no `node` label until relabelled, and a kind node has no `/` mountpoint. The
  verifier rightly called a cleanup that freed 60 MiB inconclusive, and undid it.
- **The OOM alert could never fire on kind.** cAdvisor's counter sat at 0 through real OOM kills,
  because it reads them from a kernel log a nested container cannot see. The fallback is still a
  counter: restarts qualified by an `OOMKilled` termination.
- **A scale-out that worked was reverted.**
  - A `resourceVersion` precondition failed while pods became ready. It is now preconditioned on
    `/spec/replicas`.
  - A slow stack alert that the fault had started was blamed on the fix. Alerts already pending
    before acting are no longer counted against it.
  - Its fixture was wrong too. A 200m limit throttled up to 9% of periods even at half load, so the
    burner now has a 400m limit. kube-proxy's random choice per connection bunched requests into
    bursts, so the load driver now balances across the pods itself. And the driver stays
    open-loop instead of dropping requests as latency rose.
- **A rollback that worked was reverted for "22% CPU throttling".** CFS counts a period only while
  a container is runnable, so for an idle HTTP server the ratio measured Python starting up. The
  ratio is now computed only for containers that are actually busy, and an observed idle one
  reads 0.
- **An in-place resize that held was reverted.** The verifier judged at 600 s, but the alert
  counts OOM kills over a 10-minute lookback, so the kill that triggered the fix was still inside
  it. Verification now outlasts the lookback.
- **A restart looked like a template change.** A template differing only by kubectl's
  `restartedAt` stamp digested as different (`annotations: {}` against no key at all), so rolling
  back a restart was planned as a fix.
- **Restores were wrong.** Restored logs came back root-owned and dated today.
- **Drains of dead nodes were judged wrongly.**
  - Pods stuck Terminating on the dead kubelet counted as "left behind".
  - Deliberately broken neighbours counted against the moved workloads.
- **One Deployment was rolled back twice, the second time onto the broken revision.** Its pods
  on two nodes arrived as two Alertmanager groups 0.3 ms apart. Correlated concurrently, neither
  saw the other's new incident. Correlation is now serialised by an advisory lock, and only one
  remediation per target may be in flight until its verdict.
- **A flapping alert stranded its incident.** Its second firing opened an incident that got no plan
  job. Dedup is now keyed on the incident, and a reaper re-enqueues stranded incidents.
- **Alerts never reached the engine.** The webhook secret was never mounted, a Valkey read timed out
  on every poll, and every follower replica reported the ledger "broken".

## Public deployment — two tiers, so the link is safe

1. **The read-only tier**
   ([`charts/healops/values-public.yaml`](charts/healops/values-public.yaml)):
   - the dashboard (the read-only build, which contains no approval control at all);
   - an engine with `readOnly` set: every write path refused, no worker, no Lease;
   - **no Kubernetes permissions whatsoever**: CI fails if this tier renders any RBAC object;
   - seeded with recorded incidents and serving their replays. `make public-tier` rehearses it
     inside kind from this cluster's own database.
2. **The real engine** lives only in the demo cluster with the chaos app, behind auth.

No endpoint that can trigger remediation is ever public. The only write path to the cluster is
inside the cluster.

## Design decisions

These are covered in [`HealOps_Implementation_Plan_Rev2.md`](HealOps_Implementation_Plan_Rev2.md):

- **Plain PostgreSQL, not TimescaleDB** (§6), and **Redis Streams, not Kafka** (§7).
- **Per-resource locking** over unbounded parallelism (§11).
- **Bounded automation** (§16): a token-bucket budget plus a circuit breaker. In a mass event,
  HealOps fixes less and pages sooner.
- **Ephemeral vs durable** (§17): in a GitOps cluster an imperative fix is a TTL'd mitigation with
  reconciliation paused, and the pull request is the fix.
- **Authority is earned** (§19): software promotion is `git push → CI → Helm`, and authority
  promotion is the ladder.

## How it compares

This is positioning from the plan's §28.2, dated August 2026. The other projects' columns have not
been re-verified.

| Capability | Robusta | StackStorm / Rundeck | Argo Workflows via webhook | k8sgpt / AI SRE agents | **HealOps** |
|---|---|---|---|---|---|
| Alert enrichment & grouping | ✅ (best in class) | ❌ | ❌ | partial | ✅ (correlation by topology) |
| Runbook automation | ✅ | ✅ (very mature) | ✅ | partial | ✅ |
| Plan object reviewed before execution | ❌ | ❌ | ❌ | ❌ | ✅ |
| Blast-radius + reversibility classification | ❌ | ❌ | ❌ | ❌ | ✅ |
| Policy-as-code gate on every action | partial | partial | ❌ | ❌ | ✅ (CEL, in git) |
| Closed-loop verification of recovery | ❌ | ❌ | ❌ | ❌ | ✅ |
| Automatic compensation when it didn't work | ❌ | ❌ | ❌ | ❌ | ✅ |
| False-remediation rate reported | ❌ | ❌ | ❌ | ❌ | ✅ |
| Human approval | ✅ | ✅ | partial | ✅ | ✅ |
| Approval feedback changes future authority | ❌ | ❌ | ❌ | ❌ | ✅ (promotion ladder) |
| Shadow mode for new automation | ❌ | partial | ❌ | ❌ | ✅ |
| Calibrated confidence gated in CI | ❌ | ❌ | ❌ | ❌ | ✅ |
| GitOps drift-aware (ephemeral vs durable) | ❌ | ❌ | ❌ | ❌ | ✅ |
| Durable fix as a pull request | ❌ | ❌ | ❌ | ❌ | ✅ (not exercised on kind: needs a GitHub token) |
| Non-disruptive memory fix (in-place resize) | ❌ | ❌ | ❌ | ❌ | ✅ |
| Remediation budget + circuit breaker | ❌ | ❌ | ❌ | ❌ | ✅ |
| Anti-masking: files a bug when it hides one | ❌ | ❌ | ❌ | ❌ | ✅ |
| Tamper-evident ledger + offline replay | ❌ | audit logs | ❌ | ❌ | ✅ |
| Chaos-tested remediations in CI | ❌ | ❌ | ❌ | ❌ | ✅ |
| Maturity, integrations, support | ✅✅ | ✅✅ | ✅ | ✅ | ❌ (a 7-week project) |

**Where they win.** Robusta has years of production hardening, dozens of integrations, and alert
enrichment this project does not attempt. StackStorm is a general-purpose automation platform with a
huge action library. Argo Workflows gives durable, retryable DAGs for free. AI-agent products
investigate open-ended problems HealOps will not touch. HealOps replaces none of them. It
demonstrates the *safety envelope* automated remediation should have: plan, gate, act, verify,
revert, audit, and earn authority over time.

## What HealOps deliberately does not do

- multi-cluster federation;
- LLM-driven decisions (an LLM may only write postmortems);
- arbitrary user-defined runbooks;
- database or network remediation;
- anything irreversible;
- remediating itself — denied in the policy file, routed to a null receiver, and the namespace is
  labelled.

## Honest status

**Proven on a real cluster:**
- **Chaos suite:** all 16 tests passed on a fresh kind install (see the scorecard above).
- **The chart:** `helm upgrade --install --rollback-on-failure --wait` works on a fresh cluster, and
  so does an upgrade whose pre-upgrade hook migrates a live database.
- **Read-only public tier:** rehearsed in kind with `make public-tier`. It served the recorded
  incidents, rejected the webhook and the approval API, had no Kubernetes access and no Lease, and
  its restored ledger re-verified (203 entries, chain VALID).

**Proven offline, and gated in CI:**
- **The decision corpus:** 33 cases, 20 of them must-refuse. Ten were harvested from that run's real
  incidents; 23 are live-captured states with synthesised alerts. Accuracy 1.000, Brier 0.011,
  ECE 0.072, refusal recall 1.000, and digests frozen for planner 1.0.0.
- **The evidence:** 48 bundles, each re-hashed by `ledger_verify` and replayed byte for byte against
  its golden file.

**Not yet exercised:**
- **CI itself.** The workflow is written but has not run on GitHub yet. Its first run will be the
  first time cosign keyless signing, the `kind-action` chaos job and the Helm deploy step execute
  there, and the deploy step skips until a `DEMO_KUBECONFIG` secret exists.
- **The integrations.** Slack, GitHub durable-fix pull requests and anti-masking issues are
  unit-tested against fakes; nothing on kind had real tokens for them. The kind cluster also has no
  GitOps controller.
- **Hosting.** There is no hosted public URL yet, and no demo video.
- **Grafana.** No dashboards are provisioned yet: `monitoring/grafana/` does not exist, and
  `make dev` mounts empty directories.

**Known limits:**
- **Synthetic disk alert.** kind cannot fill a disk, so the disk alert is delivered by the suite.
  The scan, archive, deletion, restore and verdict are real.
- **Config-only rollbacks.** A rollback of a config-only bad rollout has no image to pin in its
  durable PR; git still holds the bad config.
- **Calibration.** The CPU fault is calibrated on this laptop's kind cluster. A much faster or
  slower CI runner may need a different `cpuLoad.RATE_PER_SEC`, never different verifier
  thresholds.

## Repository layout

```
engine/      Python 3.14 engine: FastAPI, Alembic, runbooks, unit and chaos tests
dashboard/   Next.js 16.3 audit dashboard (read-only build by default)
chaos-app/   Fragile services and fault injectors, plus the corpus generator
charts/      Helm 4 charts: healops (both tiers), chaos-app, alerting-rules
k8s/         kind config (platform + general pools), kube-prometheus-stack values, CI deployer RBAC
policy/      remediation.yaml -- the CEL policy gate
evals/       decision_eval, policy_eval, ledger_verify -- the CI gates
corpus/      The labelled, chaos-generated decision corpus
evidence/    Recorded incidents: content-addressed bundles and their golden replays
cli/         healopsctl -- plan, replay, audit, export, freeze, promote
```

**Pins:**
- Kubernetes 1.36.4 (`kindest/node` by digest), with 1.35 as the floor;
- Python 3.14, FastAPI 0.141.1 (the plan's 0.128.x floor, moved on), PostgreSQL 18.6,
  Valkey 9.1;
- Alertmanager 0.33.1 (API v2 only), Prometheus 3.11 via kube-prometheus-stack 88.4.0,
  Grafana 13.0.3, Helm 4.2.4;
- Node 24.12 LTS, Next.js 16.3.8, SeaweedFS 4.48.

Nothing is `latest`. SeaweedFS replaces MinIO, because MinIO no longer publishes community images.
§9.1 and §10 of the Rev 2 plan still contain stale Rev 1 snippets; the skills and this repository
supersede them.

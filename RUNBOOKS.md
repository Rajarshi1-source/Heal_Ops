# HealOps runbooks: the model card

Each runbook is a deterministic rule set, not a model. This page gives the same facts for each one:
- what triggers it
- what it does, and the undo it registers *before* acting
- its blast radius and reversibility
- what it refuses
- what "recovered" means for it
- the chaos tests that prove it

Every alert's `runbook_url` points at a section here.

Authority is earned, never assumed. Each `(runbook, namespace)` pair starts in **shadow**: it records
the plan it would have run and touches nothing. It climbs the ladder on evidence. Ten agreeing
shadow plans earn **approval**, where a human clicks before anything runs. Twenty consecutive
verified fixes earn **auto**. A single bad outcome demotes it, and so does a change to its code or
to the policy file. The current rung is a live value, not a property of the code. Read it with:

```bash
uv run python -m cli.healopsctl promote --help        # and the dashboard's Runbooks page
```

## How every runbook is held to account

- **Policy decides, not the runbook.** `policy/remediation.yaml` is CEL, in git, and tested by
  `evals/policy_eval.py`. Every rule there has a must-allow case and a must-deny case.
  - **Deny rules (first one wins):** irreversible actions, HealOps itself, namespaces outside the
    allow list, StatefulSets and `db-*` workloads, the `healops.io/remediation: disabled` kill
    switch, disruptive plans on a single replica, an exhausted budget, an open breaker.
  - **Approval rules:** the target is not promoted, the blast radius score is above 25, the plan is
    node-scoped, it costs more than ₹500/month, confidence is below 0.80, it is only partially
    reversible, the fix is durable, the breaker is half-open, or 80% of the budget is used.
- **Success means the incident is over.** "Remediated" requires all three:
  - the promised alert cleared;
  - the golden signals recovered (restarts, OOM counter, throttle *ratio*, PSI);
  - no new alert appeared on the target.

  A missing signal gives `inconclusive`, never a pass.
- **One remediation per target at a time.** The per-resource lock covers the API calls. A second
  plan for a target whose change is still awaiting its verdict is blocked and escalated, because it
  was made against a target in the middle of a fix.
- **Compensation is registered before the action runs.** It runs on a failed verdict or from the
  watchdog, which is a CronJob deliberately outside the engine process.
- **Every decision is a ledger entry,** refusals included. The planner's whole input is on the
  record too, so `healopsctl replay` can re-plan an incident offline and show whether the recorded
  plan still reproduces.
- **Three chaos tests per runbook, or it doesn't merge.** They live in `engine/tests/chaos/`, and
  `engine/tests/test_chaos_contract.py` fails the unit gate when one is missing.
- **Calibration.** Confidence is graded on the labelled corpus: Brier ≤ 0.08 and ECE ≤ 0.10, plus
  100% refusal recall.

| Runbook | Trigger | Action | Undo | Reversibility | Usual effect |
|---|---|---|---|---|---|
| [crashloop_rollback](#crashloop_rollback) | `KubePodCrashLooping` | Restore the previous pod template | Roll forward to the template we came from | reversible | auto (when promoted) |
| [disk_cleanup](#disk_cleanup) | `NodeDiskPressure` | Archive old logs to S3, verify, then delete | Restore from S3 with mtime, mode and owner | partially reversible | approval |
| [cpu_scale_up](#cpu_scale_up) | `HighPodCPU` | Scale out, or lower the HPA's target | Scale back, or restore the HPA | reversible | auto under ₹500/month |
| [oom_memory_bump](#oom_memory_bump) | `KubePodOOMKilled` | Raise the memory limit in place (`pods/resize`) | Resize back to the old limit | reversible | auto (when promoted) |
| [node_drain](#node_drain) | `KubeNodeNotReady` | Cordon the node and evict its pods | Uncordon (the evictions stay) | partially reversible | always approval |

---

## crashloop_rollback

**Trigger:** `KubePodCrashLooping`. It is state-based (the container is waiting in
`CrashLoopBackOff` for `crashLoopFor`), so it clears once the pods are healthy. The chaos fixture
is `crashy-api`.

**Action:** restores the previous ReplicaSet's pod template with a JSON-patch `replace` of
`/spec/template`, preconditioned on `resourceVersion`. This is exactly what `kubectl rollout undo`
sends. A strategic merge cannot remove an env var the bad revision added, so it would "roll back"
while changing nothing. The chaos suite found that.

**Undo:** roll forward to the revision we came from. It is decided at planning time and is
idempotent.

| Property | Value |
|---|---|
| Blast radius | The workload's replicas, plus 20 if it is user-facing (a `tier` of frontend, web, api, gateway or edge, or `healops.io/user-facing=true`) |
| Durability | Ephemeral, TTL 2h. On a GitOps-managed target, reconciliation is paused for that window and a PR pins the verified image afterwards |
| Confidence | 0.90 base. +0.05 if the previous revision runs a different image, +0.03 for ≥ 5 restarts, +0.02 if nothing is ready, −0.05 if GitOps-managed |

**Refuses:**
- anything that is not a Deployment;
- a workload on its first revision;
- a previous revision with the **same pod template** — it compares template digests, ignoring the
  `restartedAt` stamp, so rolling back a restart restores nothing.

A bad *config* rollout on an unchanged image is rolled back. The old image-only rule refused it,
which meant refusing the commonest bad deploy there is.

**Recovered:** the alert cleared, no restarts in this workload's pods after the settle period, and
the generic signals.

**Known limit:** for a config-only rollback the durable PR has no image to pin. Git still holds the
bad config, and reverting that commit is a human's job.

**Chaos tests (`test_crashloop_rollback.py`):**
- **Fix:** a bad config rollout is remediated end to end.
- **Compensation:** the declared state is byte-identical after the undo; the controller's revision
  counter is excluded, because Kubernetes never reuses a revision number.
- **Refusal:** with the runbook on **auto**, a StatefulSet, a single replica with nothing to roll
  back to, and an opted-out workload are each refused or denied end to end.

---

## disk_cleanup

**Trigger:** `NodeDiskPressure`. It fires on the kubelet's `DiskPressure` condition or on root
filesystem use above 85%. The chaos fixture is `log-spammer`.

**Action:** runs **archive-then-delete, never delete-only**, in a node-agent Job pinned to the node,
because the engine cannot see another node's disk.
- The Job mounts only the archive roots (`/var/log`), with every capability dropped except
  `DAC_OVERRIDE`, `FOWNER` and `CHOWN`.
- Each file is uploaded, its size is read back from S3, and only then is it deleted.
- The plan carries the dry-run listing: files past the **7-day floor**, oldest first, capped at
  5 GiB.

**Undo:** restores from S3 with the file's mtime, mode and owner. A log restored as a fresh
root-owned file is not the file that was archived, and the chaos suite found that too.

| Property | Value |
|---|---|
| Blast radius | One node |
| Reversibility | Partially reversible: the bytes come back, the deletion happened |
| Durability | Ephemeral |
| Approval | Always required, because the plan is node-scoped and partially reversible |

**Refuses:**
- no candidates past the floor;
- less than 32 MiB worth archiving;
- any path outside the archive roots;
- **S3 unreachable** — nothing is deleted. Trading a disk incident for data loss is never the fix.

**Recovered:** the alert cleared, and the filesystem the archived logs lived on is below 80%. That
filesystem is chosen by longest mountpoint (`/var/log`, else `/var`, else `/`), never `/` by
assumption: a kind node does not expose `/` at all, and on a real node with a separate `/var`,
judging `/` judges the wrong disk.

**Chaos tests (`test_disk_cleanup.py`):**
- **Fix:** end to end with a human approving. kind shares the host's disk, so the suite delivers
  the alert itself; the scan, the archive and the verdict are real.
- **Compensation:** bytes, size, mtime, owner and mode are all `==`.
- **Refusal:** S3 scaled to zero and nothing deleted.

---

## cpu_scale_up

**Trigger:** `HighPodCPU`. It fires when a container uses more than 80% of its CPU limit for 5m.
The chaos fixture is `cpu-burner`, driven by request load from `cpu-load` through the Service.

**Action:** without an HPA, it scales `min(2×, +2, ceiling 10)` replicas through the `scale`
subresource. The JSON-patch precondition is on `/spec/replicas`, not `resourceVersion`: pod
readiness rewrites status, and therefore the version, every few seconds, and a version precondition
failed every compensation straight after a scale-out. With an HPA that scales on CPU, it lowers the
HPA's target by 15 points (floor 50%) and raises `maxReplicas` when that is the binding constraint.
It acts on the control loop, not its output.

**Undo:** restores the previous count or HPA settings. It does so only if the count is still ours:
a number a human chose since stands.

| Property | Value |
|---|---|
| Blast radius | The replicas added |
| Durability | Ephemeral, TTL 2h. A PR adjusts the replicas or HPA durably after verification |
| Cost | Each added replica is priced by what it **reserves** (₹1,800 per vCPU-month, ₹300 per GiB-month; unsized containers are priced as 1 vCPU / 1 GiB). Two 50m replicas cost about ₹200, under the line; two one-core replicas cost thousands, and a human approves |

**Refuses:**
- anything that is not a Deployment;
- an HPA on an **external** metric, because lowering a CPU target would fight the real signal;
- an HPA already at the replica ceiling;
- an HPA target already at the floor;
- a workload already at the ceiling.

**Recovered:**
- the alert cleared;
- the per-pod CPU usage ratio below 0.8 and the throttle *ratio* below 5% — dimensionless, because
  this runbook changes the replica count, which would make raw throttle counters meaningless;
- the generic signals.

**Chaos tests (`test_cpu_scale_up.py`):**
- **Fix:** at 260 req/s, balanced across the pods, two 400m pods sit at 99–100% of their
  limit, and four sit at 38–43% with at most 0.2% of periods throttled.
- **Compensation:** the whole snapshot is `==`, revision included, because a scale touches no
  template.
- **Refusal:** an external-metric HPA, captured live.

---

## oom_memory_bump

**Trigger:** `KubePodOOMKilled`, from a **counter**, never the flapping last-terminated gauge
on its own. It takes the larger of two counts:
- cAdvisor's `container_oom_events_total`;
- restarts qualified by "last termination was `OOMKilled`".

cAdvisor learns of OOM kills from the kernel log, which a nested container such as a kind node
cannot read, so there its counter exists and stays at 0 through real kills. The chaos fixture is
`hungry-worker`.

**Action:** raises the memory limit **in place** through the `pods/resize` subresource (GA in
Kubernetes 1.35, which is why 1.35 is the floor). The new limit is
`min(max(1.5 × limit, 1.25 × observed peak), 4096 Mi)`. Success is read from
`status.containerStatuses[].resources`, never from the HTTP 200. `PodResizePending: Infeasible`
ends the attempt at once. Below 1.35, it falls back to a template change that restarts pods and
asks a human.

**Undo:** resize back. The action and the undo are the same call with different values, so the
undo is exact.

| Property | Value |
|---|---|
| Blast radius | One pod |
| Reversibility | Reversible |
| Durability | Ephemeral, TTL 2h. A PR raises the template's limit after verification |
| Confidence | 0.90 with an observed peak, 0.75 without one, which sends it to a human |

**Refuses:**
- `resizePolicy: RestartContainer` for memory, because "no restart" would be a lie;
- a limit already at the 4 Gi ceiling, because that needs a code fix;
- a container it cannot identify.

A StatefulSet's pod is planned, then denied by policy.

**Recovered:**
- the alert cleared;
- the OOM counter flat;
- the resize still in effect on the same pod — if the pod was replaced, its successor runs the old
  limit;
- the generic signals.

**Chaos tests (`test_oom_memory_bump.py`):**
- **Fix:** a real OOM kill, resized in place, after which the restart count holds still for a
  minute.
- **Compensation:** the *actual* resources are `==`.
- **Refusal:** `RestartContainer`, end to end, with full authority.

---

## node_drain

**Trigger:** `KubeNodeNotReady`, after 3m NotReady. The chaos fixture is `flaky-node`, which stops
the kubelet for real.

**Action:** cordon, then evict one pod at a time through the Eviction API, which respects
PodDisruptionBudgets.
- A PDB that says no stops the drain. **The node stays cordoned and the incident escalates; it is
  never uncordoned automatically.** A half-drained node is a human's call.
- **Capacity precheck:** healthy, schedulable peers must absorb 120% of the requests being moved,
  or the plan refuses. Draining onto nodes that cannot fit the load turns one sick node into a
  cluster outage.

**Undo:** uncordon. The evictions cannot be undone, and the plan says so.

| Property | Value |
|---|---|
| Blast radius | The pods moved, plus 10 per node |
| Reversibility | Partially reversible |
| Durability | Ephemeral, TTL 2h on the cordon |
| Approval | Always required, because the plan is node-scoped |
| Self-remediation | A drain whose pods include HealOps' own is denied |

**Refuses:** no healthy peer, too little capacity, or a target that is not a node with captured
capacity.

**Recovered:** draining does not make a node Ready, so the plan **promises nothing about
`KubeNodeNotReady`**. It is judged on two checks committed at planning:
1. **No movable pods left on the node.** A pod with a deletion timestamp is leaving, not left: on a
   dead kubelet an evicted pod stays Terminating until the node returns.
2. **No unavailable replicas in the workloads the drain moved.** Those workloads, not their whole
   namespaces, which also hold things broken before the drain.

Every term is guarded by kube-state-metrics' own `up`, so a dead exporter reads as `inconclusive`,
never as a drained node.

**Chaos tests (`test_node_drain.py`):**
- **Fix:** a genuinely dead kubelet, approved by a human, remediated while the node is still down
  and still cordoned.
- **Compensation:** the node is `==` after uncordon.
- **Refusal:** every peer cordoned.
- **The PDB stop:** the node stays cordoned and nothing the budget protects is evicted.

---

## Promotion state

The rung is live state, recorded in PostgreSQL. Every transition is a ledger entry, so an auditor
can see when a runbook was granted authority and on what evidence. The defaults are:

| Runbook | Every namespace |
|---|---|
| crashloop_rollback | shadow |
| disk_cleanup | shadow |
| cpu_scale_up | shadow |
| oom_memory_bump | shadow |
| node_drain | shadow |

Shipping a new engine version never grants a runbook more power. Software promotion is
`git push → CI → Helm`, and authority promotion is the ladder: two pipelines, deliberately separate.

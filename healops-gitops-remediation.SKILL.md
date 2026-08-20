---
name: healops-gitops-remediation
description: "Build the HealOps two-stage fix: how a remediation reaches the cluster without being reverted, and how it becomes permanent. Use for ANY GitOps or non-disruptive-remediation work: classifying targets as Argo CD, Flux, or unmanaged; the ephemeral-versus-durable model; TTLs and expiry as first-class state; pausing reconciliation via sync windows, selfHeal, ignoreDifferences, or flux suspend; the durable fix as a pull request carrying evidence; and the OOMKill runbook that raises memory through the pods/resize subresource with no restart. Trigger on GitOps, Argo CD, Flux, drift detection, selfHeal, OutOfSync, ephemeral fix, durable fix, remediation PR, in-place pod resize, pods/resize, or resizePolicy. MANDATE: never pretend an imperative patch is permanent. On a managed target the imperative action is an ephemeral TTL'd mitigation and the controller stands down for that window; the durable fix is a PR opened only after verification passes. In-place resize needs Kubernetes 1.35 or later."
---

# HealOps GitOps-Aware Remediation and In-Place Resize

Two features, one philosophy: **mitigate automatically, fix durably through review.**

```
Stage 1 (seconds, ephemeral, auto):  in-place resize / rollback / scale
                                     -> incident stops -> service stays up
Stage 2 (minutes, durable, human):   PR changing the manifest, carrying the
                                     evidence that stage 1 actually worked
```

Apply the split everywhere: Runbook 1 rolls back now and opens a PR pinning the good image later; Runbook 3 scales or patches the HPA now and opens a PR adjusting the HPA later; Runbook 4 resizes in place now and opens a PR raising the template limit later.

## Part 1 — The problem nobody handles

Almost every "self-healing Kubernetes" tutorial ends at `kubectl rollout undo`. In any cluster running Argo CD or Flux — which is most clusters worth automating — that command is temporary at best and a fight at worst:

| Imperative action | What the GitOps controller does |
|---|---|
| `kubectl rollout undo deployment/api` | Detects drift from git and **re-applies the bad image**, typically within its sync interval (3 min default) |
| `kubectl scale --replicas=5` | Reverts to the replica count in git |
| `kubectl patch` a memory limit | Reverted; immediately if `selfHeal` is on |
| Anything, on a synced app | Marks the app **OutOfSync** — your remediation now shows as a compliance violation on someone's dashboard |

The framing that matters: these are not different use cases, they are **two controllers with write access to the same object**. Rev 1 dismissed this as "different trigger, different use case" — that dismissal was the defect.

## Classification

`engine/src/gitops.py`. Classify the target, then choose how the fix reaches the cluster.

```python
def classify(target) -> str:
    labels, anns = target.labels or {}, target.annotations or {}
    if labels.get("app.kubernetes.io/managed-by") == "Helm" and "argocd.argoproj.io/instance" in labels:
        return "argocd"
    if "kustomize.toolkit.fluxcd.io/name" in labels:
        return "flux"
    if "argocd.argoproj.io/instance" in labels:
        return "argocd"
    return "unmanaged"
```

```python
def prepare(plan, target) -> RemediationPlan:
    mgr = classify(target)
    if mgr == "unmanaged":
        # Still TTL it. An untracked permanent change is how clusters drift from their manifests.
        return replace(plan, durability="ephemeral", ttl_seconds=plan.ttl_seconds or 7200)

    if plan.durability == "ephemeral":
        gitops.pause(target, reason=f"healops:{plan.runbook}", ttl=plan.ttl_seconds)
        return replace(plan, gitops_managed=True)

    # Durable: the cluster is not the source of truth. Git is.
    return replace(plan, durability="durable", gitops_managed=True)
```

**Unmanaged targets still get a TTL.** That is deliberate: an imperative change nobody tracks is the mechanism by which clusters drift away from their manifests, GitOps or not.

## Pausing reconciliation for exactly the mitigation window

Three mechanisms, all scoped and all reversible:

- **Argo CD:** a sync window on the Application, or `selfHeal=false`, or annotate the resource `argocd.argoproj.io/compare-options: IgnoreExtraneous` plus a **temporary `ignoreDifferences` entry for the specific field changed**. Prefer the narrowest option that works — a field-scoped `ignoreDifferences` beats disabling self-heal for the whole app.
- **Flux:** `flux suspend ks <name>` for the window.
- **Always:** record the pause in the ledger with the reason and the TTL, and resume it from the watchdog rather than from the request path. A pause that depends on the process that set it surviving is not a pause.

## Expiry is a first-class state

Every ephemeral fix carries `expires_at`. When it expires the watchdog removes the imperative change, **resumes GitOps reconciliation**, and posts to Slack:

> *"The ephemeral mitigation on api-gateway expired; the workload is back to its declared state. The durable fix is PR #128 (still open)."*

Automation that quietly leaves permanent hand-edits in a cluster is worse than no automation. This is the mechanism that guarantees it doesn't — which is why `ttl_seconds` is required when `durability == EPHEMERAL`, and why the failure mode "durable PR never merged" has its own mitigation (nag in Slack at 24 h, recurrence counter, masking guard escalates).

## Durable fixes are pull requests

```python
def open_remediation_pr(plan, verification) -> str:
    """Called AFTER verification proves the ephemeral mitigation worked."""
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

The PR body is the differentiator, not the PR. It carries the incident timeline, before/after signals, the ledger hash, the revert command, and the line *"this change was already validated live for 42 minutes under real traffic before this PR was opened"* — a far better review packet than a human filing the same change from memory at 3 a.m.

Ordering matters: **never open the PR before verification passes.** A PR proposing a change that didn't work is worse than no PR.

Credentials: a **fine-grained GitHub PAT scoped to contents plus pull requests on one repository**. The secret-leak failure mode is real and the mitigation is least privilege plus a documented rotation runbook.

## The interview version

> *"Most self-healing demos run `kubectl rollout undo` and call it fixed. In a GitOps cluster that fix gets reverted by drift detection within a few minutes, and the bad image comes back on the next sync. So HealOps classifies every target: if it's Argo CD or Flux managed, the imperative action is an explicit ephemeral mitigation with a TTL, and HealOps tells the controller to stand down for exactly that window — then, only after verification proves the mitigation worked, it opens a pull request so git becomes the source of truth again. The mitigation buys minutes; the PR is the fix. Nothing in my system pretends an imperative patch is permanent."*

---

## Part 2 — Non-disruptive remediation with in-place pod resize

In-place pod resize (`InPlacePodVerticalScaling`) went **GA in Kubernetes 1.35**. That is why 1.35 is the supported floor, and it changes Runbook 4 fundamentally:

| | Restart-based approach | In-place approach |
|---|---|---|
| Mechanism | Patch Deployment template → rollout → pod restart | Patch the **`pods/resize`** subresource on the affected pod |
| Disruption | Container restarts, connections drop | **No restart**; the cgroup limit is raised in place |
| Reversibility | Roll forward the template | Resize back — exact prior state |
| Durability | Durable, but Argo CD will fight it | **Ephemeral** mitigation + a PR for the template change |
| Approval | Required | **Auto-eligible** (reversible, blast radius of one pod) |
| Speed | ~30–60 s including rollout | ~2–5 s |

Requires **cgroup v2** (mandatory on 1.35+), which is also what makes the OOM and pressure signals trustworthy.

### Implementation

```python
# engine/src/runbooks/oom_memory_bump.py
MEMORY_BUMP_FACTOR = 1.5
MEMORY_CEILING_MIB = 4096

class OomMemoryBump(Runbook):
    name = "oom_memory_bump"
    triggers = ("KubePodOOMKilled",)
    chaos_fixture = "hungry-worker"

    def plan(self, incident, target) -> RemediationPlan:
        if not cluster.supports_in_place_resize():        # server >= 1.35
            return self._legacy_template_plan(incident, target)   # restart-based, needs approval

        peak = metrics.memory_peak_mib(target)           # cgroup memory.peak or max_over_time
        current = target.memory_limit_mib
        new = min(max(int(current * MEMORY_BUMP_FACTOR), int(peak * 1.25)), MEMORY_CEILING_MIB)
        if new <= current:
            return refuse(self.name, incident.id,
                          f"already at ceiling {MEMORY_CEILING_MIB}Mi — this needs a code fix, "
                          "not more memory")

        if target.memory_resize_policy == "RestartContainer":
            return refuse(self.name, incident.id,
                          "container declares resizePolicy RestartContainer for memory; "
                          "an in-place bump would restart it — escalating instead")
        ...
```

The plan's action and compensation are both `resize` calls with `subresource="resize"`, differing only in the memory value — which is what makes the compensation exact rather than approximate.

```python
    def execute(self, plan) -> ExecutionResult:
        before = k8s.actual_resources(plan.target_namespace, plan.target_name)
        k8s.patch_resize(plan.actions[0])                # PATCH .../pods/{name}/resize
        applied, conditions = k8s.await_resize(plan, timeout_s=60)
        if not applied:
            # PodResizePending: Infeasible -> the node cannot satisfy it. Do NOT hang.
            return ExecutionResult(False, f"resize not applied: {conditions}", before, before)
        return ExecutionResult(True, "resized in place, no restart",
                               before, k8s.actual_resources(plan.target_namespace, plan.target_name))
```

### Five details that make this credible under questioning

1. **Mutation goes through the `resize` subresource**, not the pod spec.
2. **Success is asserted on `status.containerStatuses[].resources`**, never on the HTTP 200. This is the whole verification philosophy in miniature.
3. **`PodResizePending` with reason `Infeasible` ends the attempt immediately** — the node cannot satisfy it, so waiting is pointless. Fall back to the template plan **with approval** (failure-mode row 13).
4. **`resizePolicy: RestartContainer` for memory is a refusal**, because otherwise "no restart" is a lie.
5. **The typed Python client may not expose the subresource** — the client minor lags new subresources, so call it through the generic `call_api` path and pin the client minor to the cluster minor.

### Why this belongs in the interview

> *"My remediation got less disruptive because a Kubernetes feature went GA, and I noticed. Memory now goes up through the resize subresource with no container restart, which changes the approval calculus: the mitigation is reversible and auto-eligible, and only the durable template change needs a human. That's the two-stage pattern I use everywhere — mitigate automatically, fix durably through review."*

## Chaos coverage for these paths

Both features need the three-test contract from `healops-remediation-safety`, plus specifics:

- **Fix:** in-place resize applied, no restart observed (`restartCount` unchanged), OOM counter flat afterwards.
- **Compensation:** resize back → `status.containerStatuses[].resources` byte-identical to the snapshot.
- **Refusal:** `resizePolicy: RestartContainer`, ceiling reached, and a cluster below 1.35 all produce refusal or the legacy approval path.
- **GitOps:** a target labelled as Argo CD managed produces an ephemeral plan with a TTL and a pause call; expiry restores the declared state and resumes reconciliation.

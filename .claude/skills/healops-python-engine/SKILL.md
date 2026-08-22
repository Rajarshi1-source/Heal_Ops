---
name: healops-python-engine
description: "Build and maintain the HealOps remediation engine, the Python 3.14 / FastAPI 0.128 service that receives Alertmanager alerts and drives remediation. Use for ANY engine-side work: the Alertmanager webhook receiver (API v2, shared secret, 202-then-enqueue), incident correlation, Redis/Valkey Streams queues and consumer groups, per-resource distributed locks, leader election via the Kubernetes Lease API, SQLAlchemy async models and Alembic migrations on PostgreSQL 18, the Slack Bolt AsyncApp mounted on FastAPI, the Kubernetes and S3 clients, resilience wiring, and unit tests. Trigger on FastAPI, webhook receiver, Alertmanager payload, Redis Streams, consumer group, distributed lock, Lease, SQLAlchemy, Alembic, asyncpg, slack-bolt, boto3, kubernetes client, DLQ, or bulkhead. MANDATE: Python 3.14, FastAPI 0.128.x, PostgreSQL 18.6, Valkey 9.1 or Redis 8.x, Alertmanager API v2 only since v1 was removed in 0.27. The engine ingests and persists; it never decides whether to act."
---

# HealOps Python Engine

The engine is the ingestion and persistence half of HealOps. It receives alerts, turns them into incidents, persists everything, and hands work to the safety pipeline. **It never decides whether an action is allowed** — that is `healops-remediation-safety`. Keeping that boundary is what makes the decision layer unit-testable without a cluster.

Read `references/data-model-and-endpoints.md` for the full PostgreSQL schema, the REST surface, and the resilience pattern table before writing persistence or API code.

## Version floor — non-negotiable

| Component | Pin | What breaks otherwise |
|---|---|---|
| Python | **3.14** (`python:3.14-slim`, non-root) | `requires-python = ">=3.14"`; slack-bolt added 3.14 support in the 1.28 line |
| FastAPI | **0.128.x** + `uvicorn[standard]` | Pin exactly; let Renovate bump |
| PostgreSQL | **18.6** (`postgres:18.6-alpine`) | `uuidv7()` and temporal constraints are used by the ledger and approvals. PG 19 is beta — never ship it |
| Cache/queue | **Valkey 9.1** (default) or **Redis 8.x** | Streams are the queue either way; pin the minor |
| Alertmanager | **0.33.1** | **API v1 is gone.** Webhook contract and any silence automation must be v2 |
| slack-bolt | **1.28.x** + `slack_sdk` | Use `AsyncApp` + the FastAPI adapter, not a second web server |
| boto3 | pinned exactly | MinIO for local S3 |
| kubernetes client | the minor matching the cluster minor | The typed client lags new subresources — expect `call_api` for `pods/resize` |

## Project layout

Follow this tree; it is the plan's §5.1 and other skills reference these paths.

```
engine/src/
├── main.py                 # FastAPI entrypoint; mounts the Slack AsyncApp adapter
├── config.py               # env-var config, no literals scattered in modules
├── api/                    # webhooks.py  approvals.py  health.py  runbooks.py
├── core/                   # alert_classifier  runbook_registry  runbook_executor
│                           # approval_manager  deduplicator  incident_correlator
├── runbooks/               # base.py + the five runbooks (see healops-remediation-safety)
├── k8s/                    # client  deployment_ops  pod_ops  node_ops  state_capture
├── integrations/           # slack_client  slack_messages  s3_client  prometheus_client
├── db/                     # connection (async engine)  models  queries
└── cache/                  # redis_client  dedup_store  approval_store
```

## The webhook path

Alertmanager must never block on HealOps. The receiver does the minimum and returns.

```python
# api/webhooks.py
@router.post("/api/webhooks/alertmanager", status_code=202)
async def receive(payload: AlertmanagerV2Payload, request: Request) -> dict:
    verify_shared_secret(request)                      # constant-time compare
    stored = await alerts.persist_batch(payload)        # durable BEFORE we ack
    await stream.xadd_batch(stored)                     # remediation-jobs
    return {"accepted": len(stored)}
```

Three properties to preserve:

- **Durable-then-ack.** Persist to PostgreSQL before adding to the stream, and `XACK` only after the ledger write lands. Alertmanager retries on non-2xx, so a lost alert is worse than a duplicated one — and duplicates are handled by the idempotency key.
- **Idempotency key is `(fingerprint, runbook, incident_id)`.** Alertmanager runs an HA gossip cluster whose own dedup can still deliver twice; the receiver must be idempotent rather than assume it won't.
- **Parse v2 shape only.** `status`, `alerts[].labels`, `alerts[].annotations`, `alerts[].startsAt/endsAt`, `alerts[].fingerprint`, `commonLabels`, `groupKey`. Store the complete payload in a `JSONB` column — reconstructing an incident later needs the raw thing.

## Correlation happens at ingestion, not at execution

One node failure must produce **one** incident, not forty. `core/incident_correlator.py` implements topology-and-causality grouping and its rules are owned by `healops-remediation-safety` (§16.1) — call into it here, don't reimplement. Symptom alerts attach to the cause incident as evidence and never select their own runbook.

Belt and braces: configure Alertmanager `inhibit_rules` as the first line (a `KubeNodeNotReady` inhibits `KubePodCrashLooping` for pods on that node), then correlate in the engine as the second, because inhibition cannot express owner-reference topology.

## Queue, locks, leadership

```python
# cache/ — Valkey/Redis is disposable. Say this out loud in review:
# if it is wiped, HealOps loses in-flight queue position and cold caches.
# It does not lose approvals, plans, executions, or the audit chain.
```

- **Queue:** Redis Streams with consumer groups. **Separate consumer groups for `execute` and `verify`** — a slow verification backlog must not block new incident handling (bulkhead).
- **Locks:** `SET NX lock:{kind}:{ns}/{name}`, TTL 120 s, **fenced by plan id** so a resumed worker can tell its lock from a successor's.
- **Leadership:** two engine replicas, but only one *acts*. A `coordination.k8s.io/Lease` grants leadership; the follower still serves webhooks and enqueues. Webhook availability and execution capacity are deliberately decoupled.
- **DLQ:** `remediation-jobs-dlq` after max retries, with an alert on depth. Silent message loss is the failure this prevents.

## Approval state lives in PostgreSQL, never only in Redis

This was a real defect in Rev 1 and the corrected shape matters:

```
approvals table is the source of truth
  status       IN ('pending','approved','rejected','expired')
  expires_at   TIMESTAMPTZ NOT NULL     -- authoritative deadline, not a Redis TTL
  decided_by, decided_at                -- who, when
  slack_message_ts                      -- so the original message can be updated
```

Redis is a read-through cache and a notifier. `can_execute()` **fails closed** — any unreadable approval state returns `False`. A TTL that deletes the row destroys the evidence and leaves the executor unable to distinguish "expired" from "lost". Timeout is 10 minutes and expiry **escalates**; it never auto-executes.

## Slack integration

`AsyncApp` from slack-bolt mounted on the FastAPI app via the adapter — one web server, one port. `integrations/slack_messages.py` holds Block Kit builders and nothing else, so message shape can be unit-tested without a Slack client.

Messages the engine sends: plan-for-approval (with the rendered plan summary, blast radius, cost delta, expected recovery signal), success with the verification verdict, escalation with the evidence-bundle hash, refusal, denial, shadow-mode notice, promotion/demotion, and the masking-guard warning. Every one of those is also a ledger entry — see `healops-audit-ledger`.

When Slack is down, actions requiring approval are **held, not executed**, the ledger and dashboard keep working, and a digest posts when Slack returns.

## Resilience wiring

Implement these here; the full table with the failure each one prevents is in `references/data-model-and-endpoints.md`.

- Timeouts everywhere: 8 s Kubernetes calls, 60 s resize wait, 10 min approval, 15 min verification. An unbounded wait masquerading as "in progress" is the bug this kills.
- Retry with exponential backoff **and full jitter** on transient K8s/S3/Slack errors only — **never** on a mutation that may have partially applied.
- Transport circuit breakers on the K8s API, Slack, S3, and Alertmanager clients, each with a defined fallback.
- Graceful degradation with explicit semantics: no Prometheus → verification returns `inconclusive` and escalates; no Slack → engine still acts within policy; no S3 → the disk runbook **refuses** rather than deleting.
- Fail-closed on PostgreSQL: no ledger write means no action. The ledger is a precondition, not a side effect.

## Consistency rules that are easy to get wrong

1. **One transaction per state transition, with the ledger append inside it.** State and audit cannot diverge. This is the single most important consistency rule in the system.
2. **Optimistic concurrency on Kubernetes writes** via `resourceVersion` preconditions. On conflict, **re-plan** — never retry blindly.
3. **Monotonic incident state machine:** `open → planning → awaiting_approval → executing → verifying → {remediated | reverted | escalated}`. Illegal transitions raise, and the raise is itself a ledger entry.
4. **`compensate()` must be idempotent** — both the verifier and the watchdog may call it.

## Testing

`pytest -m "not chaos" --cov=engine --cov-fail-under=80` is a merge gate. Unit tests use committed fixtures (`tests/fixtures/alert_payloads/`, `tests/fixtures/k8s_states/`) and need no cluster — that hermetic property is what lets the decision logic be graded in CI.

Test the receiver against real captured v2 payloads, not hand-written dicts. Test dedup, lock fencing, approval fail-closed, and the state machine's illegal transitions. Cluster-dependent behaviour belongs in the chaos suite (`healops-kubernetes-platform`).

## Interview framing to keep in the code comments

> *"The webhook returns 202 after a durable enqueue, so Alertmanager never waits on my decision logic. Two replicas serve webhooks; a Kubernetes Lease means only one of them acts. And approvals live in PostgreSQL, not Redis — if Redis is wiped I lose queue position and cold caches, not the record of who approved what."*

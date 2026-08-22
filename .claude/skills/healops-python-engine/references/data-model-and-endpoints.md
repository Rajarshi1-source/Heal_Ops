# HealOps — Data Model, REST Surface, and Resilience Table

Contents:
1. Database choice and why not the alternatives
2. Core tables (Rev 1 base)
3. Rev 2 tables — plans, verifications, ledger, promotion, budget, recurrence
4. REST endpoints
5. Resilience patterns, each mapped to the failure it prevents
6. Queue and cache key conventions

---

## 1. Database choice and why not the alternatives

**Verdict: plain PostgreSQL 18.6 — deliberately NOT TimescaleDB.**

The data is low-frequency *events* (under 100 alerts/hour), not high-frequency metrics. Hypertables and continuous aggregates would add operational complexity for zero benefit at this rate. This is the sharpest database answer in the portfolio precisely because a sibling project (KubeThrifty) chose TimescaleDB for genuinely high-frequency metric snapshots — same engineer, different problem, different tool.

| Rejected | Why |
|---|---|
| TimescaleDB | Low-frequency events; hypertables buy nothing at 100 alerts/hour |
| MongoDB | Alert chains are relational: alert → runbook → execution → approval → outcome. Foreign keys and JOINs model this naturally; Mongo needs denormalisation or `$lookup` chains |
| Cassandra | Write-optimised partition-key store for a workload with no write-throughput problem |
| CosmosDB | Vendor lock-in and cost for a single-region relational workload |
| Generic NoSQL | Loses referential integrity on exactly the relationships the audit trail depends on |

PostgreSQL 18 features that earn their place: **`uuidv7()`** (timestamp-ordered UUIDs — ideal primary keys for an append-only hash-chained ledger, good index locality, chronological reads), **temporal constraints** (range-based `PRIMARY KEY`/`UNIQUE`/`FOREIGN KEY` — models "this approval is valid for this window" natively), and the new async I/O subsystem.

`JSONB` is used for alert payloads, plan bodies, and signal snapshots — schema-flexible data hanging off strongly-typed relational spines.

---

## 2. Core tables (Rev 1 base)

```sql
CREATE TABLE alert_events (
    id                BIGSERIAL PRIMARY KEY,
    fingerprint       VARCHAR(64) NOT NULL,      -- Alertmanager fingerprint, for dedup
    alertname         VARCHAR(200) NOT NULL,
    status            VARCHAR(20)  NOT NULL,      -- firing | resolved
    severity          VARCHAR(20),
    namespace         VARCHAR(200),
    pod_name          VARCHAR(200),
    node_name         VARCHAR(200),
    labels            JSONB NOT NULL DEFAULT '{}',
    annotations       JSONB NOT NULL DEFAULT '{}',
    full_payload      JSONB NOT NULL,             -- complete v2 payload, verbatim
    starts_at         TIMESTAMPTZ NOT NULL,
    ends_at           TIMESTAMPTZ,
    received_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX ON alert_events (fingerprint, received_at DESC);
CREATE INDEX ON alert_events (namespace, alertname, received_at DESC);
```

`incidents` groups correlated alerts and carries `cause_scope` (`node` | `workload` | `pod`), `cause_target`, `target_key`, `precedence`, and the state-machine column. `runbook_executions` records each attempt with `state_before` / `state_after` JSONB snapshots and timing. `approvals` is described in the SKILL body — PostgreSQL is the source of truth, `expires_at` is authoritative, `can_execute()` fails closed.

---

## 3. Rev 2 tables (migration `002_rev2.sql`)

```sql
-- 1. RemediationPlan (§14): what we intend to do, before we do it.
CREATE TABLE remediation_plans (
    id                UUID PRIMARY KEY DEFAULT uuidv7(),
    incident_id       BIGINT NOT NULL REFERENCES incidents(id),
    runbook           TEXT NOT NULL,
    target_kind       TEXT NOT NULL,                 -- Deployment | Node | Pod
    target_namespace  TEXT,
    target_name       TEXT NOT NULL,
    actions           JSONB NOT NULL,                -- ordered API calls, exact payloads
    compensation      JSONB NOT NULL,                -- how to undo, decided BEFORE acting
    reversibility     TEXT NOT NULL CHECK (reversibility IN
                          ('reversible','partially_reversible','irreversible')),
    blast_radius      JSONB NOT NULL,
    durability        TEXT NOT NULL CHECK (durability IN ('ephemeral','durable')),
    ttl_seconds       INT,
    expected_signal   JSONB NOT NULL,
    confidence        NUMERIC(4,3) NOT NULL,
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
    alert_cleared   BOOLEAN,                        -- NULL = not observed
    signals_before  JSONB NOT NULL,
    signals_after   JSONB NOT NULL,
    outcome         TEXT NOT NULL CHECK (outcome IN
                        ('remediated','not_recovered','inconclusive')),
    compensated     BOOLEAN NOT NULL DEFAULT FALSE,
    reasons         TEXT[] NOT NULL DEFAULT '{}',
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON verifications (execution_id);

-- 3. Tamper-evident ledger (§20). Append-only, hash-chained, monthly partitions.
CREATE TABLE audit_ledger (
    seq             BIGSERIAL,
    id              UUID NOT NULL DEFAULT uuidv7(),
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor           TEXT NOT NULL,                  -- 'healops' | slack user id
    event_type      TEXT NOT NULL,                  -- plan|policy|execute|verify|compensate|approve|promote
    incident_id     BIGINT,
    execution_id    BIGINT,
    payload         JSONB NOT NULL,
    prev_hash       CHAR(64) NOT NULL,
    hash            CHAR(64) NOT NULL,
    bundle_sha256   CHAR(64),
    PRIMARY KEY (seq, occurred_at)
) PARTITION BY RANGE (occurred_at);
CREATE TABLE audit_ledger_2026_08 PARTITION OF audit_ledger
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
CREATE UNIQUE INDEX ON audit_ledger (hash);
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
    scope           TEXT PRIMARY KEY,               -- 'global' | 'ns:<name>'
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
    target_key      TEXT NOT NULL,                  -- ns/kind/name
    window_start    DATE NOT NULL,
    occurrences     INT NOT NULL DEFAULT 1,
    issue_url       TEXT,
    PRIMARY KEY (runbook, target_key, window_start)
);

-- 7. Executions gain the Rev 2 fields.
ALTER TABLE runbook_executions
    ADD COLUMN IF NOT EXISTS plan_id UUID REFERENCES remediation_plans(id),
    ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'auto',   -- shadow|approval|auto
    ADD COLUMN IF NOT EXISTS durability TEXT,
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS pr_url TEXT,
    ADD COLUMN IF NOT EXISTS gitops_managed BOOLEAN NOT NULL DEFAULT FALSE;
```

Three choices to be able to defend on sight: `uuidv7()` for ledger and plan IDs (timestamp-ordered, so the chain reads chronologically and index locality is good); **monthly partitions** on the ledger (an append-only table that only grows should be partitioned before it needs to be); and **`REVOKE UPDATE, DELETE`** — an audit table the application can rewrite is not an audit table.

Migrations are **expand-contract**, forward-only via Alembic: additive migration ships with release N, the destructive change waits for N+1 once no old pods remain.

---

## 4. REST endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/api/webhooks/alertmanager` | Alertmanager **v2** payload. Shared-secret header, constant-time compare. Returns **202** after durable enqueue |
| POST | `/api/approvals/{id}` | Slack interactivity target; also the dashboard's approve path. Idempotent — a double click is one decision |
| GET | `/api/runbooks` | Registry listing: name, triggers, reversibility class, chaos fixture, promotion mode per namespace |
| GET | `/api/incidents` | Paginated, filterable by namespace/outcome/window |
| GET | `/api/incidents/{id}` | Full timeline: alerts, correlation reason, plan, policy decision, approval, execution, verification, compensation, ledger hashes |
| GET | `/api/stats` | Efficacy scorecard numbers for the dashboard KPI cards |
| GET | `/api/health` and `/healthz` | Readiness checks PostgreSQL, Redis, K8s API, and Alertmanager reachability |
| GET | `/metrics` | Prometheus exposition of the `healops_*` series |

The dashboard's own Route Handlers proxy these — see `healops-nextjs-dashboard`. **No endpoint that can trigger remediation is ever exposed to the public internet.** The public demo is read-only; the only write path to the cluster is inside the cluster.

---

## 5. Resilience patterns and the failure each prevents

A list of pattern names without failures attached is a flashcard, not engineering. Keep the right column.

| Pattern | Where | Failure it prevents |
|---|---|---|
| Saga / compensating transaction | `compensation` persisted **before** `execute()`; run by verifier or watchdog | A partially-applied remediation with no way back |
| Dead-man's-switch watchdog | CronJob every minute; reverts executions whose verification never completed, expires TTL'd fixes | Engine crashes mid-remediation, leaving a hand-edited cluster |
| Circuit breaker (transport) | K8s API, Slack, S3, Alertmanager clients, each with a fallback | A sick API server turning every runbook into a 30-second timeout |
| Circuit breaker (policy) | Global remediation breaker on repeated compensations or high K8s error rate | Automating *into* an outage |
| Token bucket | Remediation budget per namespace per hour | Storm amplification and cost incidents |
| Distributed lock | Redis `SET NX lock:{kind}:{ns}/{name}`, TTL 120 s, fenced by plan id | Two runbooks mutating the same object concurrently |
| Lease-based leader election | `coordination.k8s.io/Lease` when the engine runs more than one replica | Two replicas both executing the same incident |
| Idempotency keys | `(fingerprint, runbook, incident_id)`; `XACK` only after the ledger write | Alertmanager re-delivery double-remediating |
| Fail-closed authorisation | `can_execute()` returns False on any unreadable approval state | Executing a disruptive action nobody approved |
| Timeout everywhere | 8 s K8s, 60 s resize wait, 10 min approval, 15 min verification | Unbounded waits masquerading as "in progress" |
| Retry with backoff + full jitter | Transient errors only — never a possibly-partial mutation | Thundering herd; double-applied changes |
| Dead-letter queue | `remediation-jobs-dlq`, alert on depth | Silent message loss |
| Bulkhead | Separate worker pools and consumer groups for `execute` vs `verify` | A verification backlog blocking new incidents |
| Graceful degradation | No Prometheus → `inconclusive` + escalate; no Slack → still acts within policy; no S3 → disk runbook refuses | A dependency outage becoming either paralysis or recklessness |
| Kill switch | `healopsctl freeze`, namespace label, workload annotation, global Helm value | Needing a redeploy to stop automation during a release |
| Self-exclusion | HealOps' own namespace carries `healops.io/remediation: disabled` | The engine remediating itself into a loop |

The self-exclusion rule deserves its own sentence: *"HealOps refuses to remediate HealOps. A system that can restart itself mid-decision can lose the incident it was handling — and that rule lives in the policy file, not in code."*

### CAP position

HealOps is **CP for decisions and AP for observation**, and splitting it that way is the answer:

| Concern | Model | Why |
|---|---|---|
| Approvals, plans, ledger | **CP.** Single-writer PostgreSQL, transactional, fail-closed on partition | An approval must never read as granted when it isn't |
| Alert ingestion | **AP.** 202 after durable enqueue, async processing | Alertmanager must not block; a slow decision must not drop the alert |
| Dashboard reads | **AP.** Cache-aside, stale-while-revalidate | A dashboard that errors during an incident is worse than one 30 seconds stale |
| Cluster state | Eventually consistent by nature | The K8s API is the truth and it lags — verification uses windows, never instantaneous reads |

Availability mechanics: engine 2 replicas with one leader; Alertmanager in its 3-replica HA gossip mode; PostgreSQL single primary with `pg_dump` CronJob to S3 plus WAL archiving (**RPO/RTO for the audit trail matters more than for the runbooks** — losing the ledger loses the compliance story); Valkey/Redis treated as disposable.

---

## 6. Queue and cache key conventions

```
stream   remediation-jobs                 consumer groups: exec, verify
stream   remediation-jobs-dlq             alert on XLEN
lock     lock:{kind}:{namespace}/{name}   SET NX, TTL 120s, value = plan id (fence)
dedup    dedup:{fingerprint}              SET NX, TTL 300s
cache    stats:scorecard                  TTL 30s, stale-while-revalidate
cache    approval:{id}                    read-through only; Postgres is truth
```

Metrics the engine exports (self-monitoring is a first-class feature, not an afterthought):

```
healops_incidents_total{outcome="auto|approved|escalated|refused"}
healops_remediation_duration_seconds{runbook,outcome}          # histogram
healops_verification_outcome_total{runbook,outcome}
healops_compensations_total{runbook,trigger="verification|watchdog|expiry"}
healops_budget_used_ratio{namespace}
healops_breaker_state{scope}                                   # 0 closed, 1 half_open, 2 open
healops_promotion_mode{runbook,namespace}                      # 0 shadow, 1 approval, 2 auto
healops_recurrence_count{runbook,target}
healops_watchdog_actions_total
healops_ledger_valid                                           # 0/1, hourly chain check
```

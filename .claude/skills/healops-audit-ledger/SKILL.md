---
name: healops-audit-ledger
description: "Build the HealOps tamper-evident audit layer, the answer to how a regulated company would ever let a bot change production. Use for ANY work on the hash-chained append-only ledger, the sha256 chain and its verification pass, canonical JSON serialisation, uuidv7 keys and monthly partitioning, revoking UPDATE and DELETE from the application role, content-addressed evidence bundles, cosign keyless signing and attestation in CI, and the healopsctl replay, audit, freeze and plan commands. Trigger on audit ledger, hash chain, tamper-evident, append-only, prev_hash, canonical JSON, verify_chain, evidence bundle, content-addressed, cosign, sigstore, attestation, offline replay, healopsctl, change record, or compliance framing. MANDATE: the ledger is a precondition for action, not a side effect, so no ledger write means no action. Every state transition appends inside the same transaction as the state change. The application role has INSERT and SELECT only; a table the application can rewrite is not an audit table."
---

# HealOps Tamper-Evident Audit Ledger

Logging everything to PostgreSQL is good. But **a table the application can `UPDATE` is a log, not an audit.** The question a senior engineer or an auditor asks is: *how would anyone know if a record had been altered?*

This skill is the answer, and it is the feature that wins enterprise rooms.

## The chain

```python
# engine/src/ledger.py
"""
Append-only, hash-chained ledger. Each entry commits to its predecessor, so altering or
deleting any historical entry breaks every hash after it and is detectable in one pass.

  hash_n = sha256( hash_{n-1} || canonical_json(entry_n) )

The app role has INSERT and SELECT only (UPDATE/DELETE revoked in the migration).
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

### Four details that are easy to break

1. **Canonical JSON is load-bearing.** `sort_keys=True` and `separators=(",", ":")` — no spaces, deterministic key order. Any change to serialisation invalidates every historical hash, so treat `canonical()` as a frozen contract. If it must ever change, version the chain rather than rewriting it.
2. **The database clock is the single source of time.** `db.now_iso()`, never the application's clock. Two replicas with skewed clocks would produce a chain whose timestamps go backwards.
3. **The chain is sequential**, so appends serialise. That is acceptable at under 100 alerts/hour and is worth stating as a deliberate trade-off rather than discovering it as a surprise. If throughput ever mattered, the answer is per-namespace chains with a periodic root, not abandoning the chain.
4. **`verify_chain` runs hourly in-cluster and in CI**, exported as `healops_ledger_valid` (0/1) with an alert on 0. A tamper-evidence mechanism nobody checks is decoration.

## Schema shape

```sql
CREATE TABLE audit_ledger (
    seq             BIGSERIAL,
    id              UUID NOT NULL DEFAULT uuidv7(),
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor           TEXT NOT NULL,        -- 'healops' | slack user id
    event_type      TEXT NOT NULL,        -- plan|policy|execute|verify|compensate|approve|promote
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
```

Three choices to defend on sight:

- **`uuidv7()` keys** (PostgreSQL 18) — timestamp-ordered, so index locality is good and the chain reads chronologically. This is the specific reason the project moved to PG 18.
- **Monthly partitions** — an append-only table that only ever grows should be partitioned before it needs to be. Add a partition-creation CronJob or use `pg_partman`; a missing partition is an insert failure, which under the fail-closed rule means **no remediation**. Test that path.
- **`REVOKE UPDATE, DELETE`** — enforcement at the grant level, not by convention. The migration that creates the table performs the revoke; a later migration must never grant them back.

## The rule that matters most

**The ledger append happens in the same transaction as the state change it records.** State and audit cannot diverge. And because no ledger write means no action, **the ledger is a precondition, not a side effect** — if PostgreSQL is unreachable the engine stops acting and posts to Slack. Fail-closed.

Events to append, at minimum: `plan_requested`, `plan`, `policy`, `approve`, `execute`, `verify`, `compensate`, `promote`, plus freeze/unfreeze and GitOps pause/resume. **Refusals and denials are appended too** — "the system correctly decided not to act" is part of the record.

## Evidence bundles

Every incident produces one **content-addressed** bundle containing: the alerts, the correlation decision, the plan, the policy decision, the approval (who clicked, when, from which Slack workspace), the execution result, the before/after signals, the verification verdict, any compensation, and the ledger hashes.

The `sha256` of the canonical JSON **is** the incident's identity. Link it from the Slack message and the PR body — a single hash that reconstructs an entire incident is the whole point.

```yaml
# .github/workflows/sign.yml (excerpt) — keyless OIDC, no key material to lose
- uses: sigstore/cosign-installer@v3
- run: |
    cosign sign --yes ghcr.io/OWNER/healops-engine@${DIGEST}
    cosign attest --yes --predicate bundles/${SHA}.json \
      --type https://healops.io/evidence/v1 ghcr.io/OWNER/healops-engine@${DIGEST}
```

Keyless OIDC is the right call for a portfolio project: no key material to lose, and the CI identity is the signer. Signing is one CI step, so ship it even if the rest of the ledger work is trimmed for time.

## Offline replay

`healopsctl replay <sha>` reconstructs an incident with **no cluster, no network, no database** — from the bundle alone.

```
$ healopsctl replay 9f2c8ab4…
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

Keep this output format stable — the dashboard renders replay output as pages for the public read-only demo, and the format doubles as a golden-file test. Replay must be **byte-identical** on repeat runs, which is the same determinism property the decision eval anchors.

### `healopsctl` surface

| Command | Purpose |
|---|---|
| `healopsctl replay <sha>` | Offline reconstruction from a bundle |
| `healopsctl audit --verify-chain` | Full chain walk; exit non-zero on a break |
| `healopsctl freeze --reason "..."` | Global kill switch; the freeze is itself a ledger entry with a reason and an owner |
| `healopsctl plan <incident>` | Print the plan a runbook would produce, without executing |

`freeze` is worth ten minutes of build time: *"during a deploy or an incident review, one command turns all automation off, and the freeze itself is auditable."*

## Two demo moments to build for

1. **Replay a real incident on a laptop with the cluster disconnected** and get byte-identical output.
2. **Deliberately `UPDATE` a row as the DB superuser**, then run `healopsctl audit --verify-chain` and watch it name the exact sequence number where the chain breaks. Rehearse this one — it lands harder than any diagram.

## Backup and retention

`pg_dump` on a CronJob to S3 plus WAL archiving. Be explicit that **RPO/RTO for the audit trail matters more than for the runbooks** — losing runbook state costs an incident; losing the ledger costs the compliance story. Bundles go to object storage independently of the database, so the chain and the evidence have separate failure domains.

## The compliance framing

> *"The real blocker for auto-remediation in any regulated company isn't technical, it's change management: an automated change to production still needs a change record, an approver, and a rollback plan. So every HealOps action produces one — a hash-chained ledger entry, an approver identity from Slack (or an explicit 'automated under policy X, promotion state auto' when there's no human), the compensating action registered before execution, and a signed evidence bundle you can replay offline. I can hand an auditor a single hash and they can reconstruct the entire incident, and prove nobody edited it afterwards. That's the difference between a tool that fixes things and a tool a bank would actually turn on."*

Note the three-part structure: change record, approver identity, rollback plan. That maps onto real change-management vocabulary, which is why it works on senior interviewers who have lived through an audit.

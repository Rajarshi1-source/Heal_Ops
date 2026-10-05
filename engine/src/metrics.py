"""Prometheus metrics.

HealOps monitors itself. That is a first-class feature rather than an afterthought: automation you
cannot observe is automation you cannot trust, and several of these series are the *only* way an
operator learns that a guardrail fired.

Series names are fixed -- the Grafana dashboards and the alert rules in
`charts/alerting-rules` both reference them, so renaming one breaks both.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# --- incidents and outcomes ---------------------------------------------------------------------
incidents_total = Counter(
    "healops_incidents_total",
    "Incidents by terminal outcome.",
    ["outcome"],  # auto | approved | escalated | refused | denied | shadow
)

remediation_duration = Histogram(
    "healops_remediation_duration_seconds",
    "Wall time from alert start to terminal outcome.",
    ["runbook", "outcome"],
    buckets=(5, 15, 30, 60, 90, 120, 300, 600, 900, 1800),
)

# --- verification: the credibility metrics ------------------------------------------------------
verification_outcome_total = Counter(
    "healops_verification_outcome_total",
    "Verification verdicts. `inconclusive` is a real verdict, never folded into success.",
    ["runbook", "outcome"],  # remediated | not_recovered | inconclusive
)

compensations_total = Counter(
    "healops_compensations_total",
    "Compensating actions run, by what triggered them.",
    ["runbook", "trigger"],  # verification | watchdog | expiry
)

time_to_detect_failure = Histogram(
    "healops_time_to_detect_failure_seconds",
    "How fast the system notices its own mistake: verification time minus execution time.",
    ["runbook"],
    buckets=(30, 60, 90, 120, 300, 600, 900),
)

# --- guardrails ---------------------------------------------------------------------------------
plans_total = Counter(
    "healops_plans_total",
    "Plans created, by policy decision. Refusals prove the guardrails are load-bearing.",
    ["runbook", "decision"],  # allow | require_approval | deny | refuse
)

budget_used_ratio = Gauge(
    "healops_budget_used_ratio",
    "Fraction of the hourly remediation budget consumed.",
    ["namespace"],
)

breaker_state = Gauge(
    "healops_breaker_state",
    "Remediation circuit breaker: 0 closed, 1 half_open, 2 open.",
    ["scope"],
)

promotion_mode = Gauge(
    "healops_promotion_mode",
    "Authority per (runbook, namespace): 0 shadow, 1 approval, 2 auto.",
    ["runbook", "namespace"],
)

recurrence_count = Gauge(
    "healops_recurrence_count",
    "Times this runbook has fixed this target inside the masking-guard window.",
    ["runbook", "target"],
)

shadow_agreement_ratio = Gauge(
    "healops_shadow_agreement_ratio",
    "Agreement between shadow plans and human actions. This is the drift monitor.",
    ["runbook", "namespace"],
)

# --- the watchdog and the audit chain -----------------------------------------------------------
# A gauge read from the ledger on every engine scrape, not a counter incremented in the watchdog:
# the watchdog is a CronJob that lives for seconds and is never scraped, so a counter in its process
# would always read zero and the alert on it could never fire. The count only ever grows, so
# `increase()` over it behaves exactly as it would on a counter.
watchdog_actions_total = Gauge(
    "healops_watchdog_actions_total",
    "Force-reverts and expiries performed by the watchdog. Any value above zero is worth a look.",
)

ledger_valid = Gauge(
    "healops_ledger_valid",
    "1 when the hash chain verifies, 0 when it does not. Alerting on 0 is what makes the "
    "tamper-evidence real rather than decorative.",
)

# Unchecked is neither valid nor broken. A gauge starting at 0 would page "chain broken" from every
# follower replica -- only the leader walks the chain -- so it starts as NaN, which `== 0` never
# matches, and staleness is its own alert on the timestamp below.
ledger_valid.set(float("nan"))

ledger_entries = Gauge("healops_ledger_entries", "Entries in the audit chain.")

ledger_checked = Gauge(
    "healops_ledger_checked_timestamp_seconds",
    "When this replica last walked the chain. A check nobody runs is decoration too.",
)

# --- queue and transport ------------------------------------------------------------------------
queue_depth = Gauge("healops_queue_depth", "Pending messages on the remediation stream.")
dlq_depth = Gauge("healops_dlq_depth", "Messages in the dead-letter queue.")

webhook_alerts_received = Counter(
    "healops_webhook_alerts_received_total",
    "Alerts accepted from Alertmanager.",
    ["status"],  # firing | resolved
)

webhook_rejections = Counter(
    "healops_webhook_rejections_total",
    "Webhook requests refused, by reason.",
    ["reason"],  # bad_secret | malformed | read_only
)

alerts_deduplicated = Counter(
    "healops_alerts_deduplicated_total",
    "Alerts suppressed by the dedup window.",
)

alerts_correlated = Counter(
    "healops_alerts_correlated_total",
    "Alerts attached to an existing incident, by correlation reason. The `symptom_of_*` reasons "
    "are the remediations that did NOT happen.",
    ["reason"],
)

dependency_up = Gauge(
    "healops_dependency_up",
    "Reachability of each dependency: 1 up, 0 down.",
    ["dependency"],  # postgres | redis | kubernetes | alertmanager | prometheus | slack
)

is_leader = Gauge(
    "healops_is_leader",
    "1 on the replica holding the Lease. Both replicas serve webhooks; only the leader acts.",
)

k8s_api_errors = Counter(
    "healops_k8s_api_errors_total",
    "Kubernetes API failures. Feeds the breaker's error-rate trip condition.",
    ["verb"],
)

k8s_api_calls = Counter(
    "healops_k8s_api_calls_total",
    "Kubernetes API calls attempted.",
    ["verb"],
)


BREAKER_STATE_VALUES = {"closed": 0, "half_open": 1, "open": 2}
PROMOTION_MODE_VALUES = {"shadow": 0, "approval": 1, "auto": 2}

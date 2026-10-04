/**
 * Types mirroring the engine's read API (engine/src/api/incidents.py, approvals.py, runbooks.py).
 *
 * `Outcome` is the whole vocabulary, as a closed union. Every component that renders an outcome
 * switches over it exhaustively, so adding an outcome to the engine without styling it here is a
 * compile error -- never a silently grey badge.
 */

export type Outcome =
  | "remediated"
  | "not_recovered"
  | "inconclusive"
  | "reverted"
  | "revert_failed"
  | "escalated"
  | "refused"
  | "denied"
  | "shadow"
  | "pending";

export type StageName =
  | "correlate"
  | "plan"
  | "policy"
  | "gate"
  | "execute"
  | "verify"
  | "compensate"
  | "outcome";

/** "corrected" is a revert: the system catching its own mistake, rendered as such. */
export type StageState = "passed" | "stopped" | "corrected" | "failed" | "pending" | "skipped";

export interface Stage {
  stage: StageName;
  state: StageState;
  label: string;
  detail: string;
}

export interface IncidentSummary {
  id: number;
  state: string;
  outcome: Outcome;
  outcome_detail: string | null;
  cause_alertname: string;
  cause_scope: "node" | "workload" | "pod";
  target_key: string;
  namespace: string | null;
  opened_at: string;
  alert_count: number;
  correlation_reason: string;
  runbook: string | null;
  summary: string | null;
  mode: "shadow" | "approval" | "auto" | null;
  trail: Stage[];
}

export interface IncidentPage {
  items: IncidentSummary[];
  total: number;
}

export interface Alert {
  id: number;
  alertname: string;
  status: string;
  role: "cause" | "symptom";
  severity: string | null;
  fingerprint: string;
  namespace: string | null;
  pod: string | null;
  node: string | null;
  starts_at: string;
  received_at: string;
}

export interface ApiCall {
  verb: string;
  resource: string;
  name: string;
  namespace: string | null;
  body: Record<string, unknown>;
  subresource: string | null;
}

export interface Plan {
  id: string;
  runbook: string;
  planner_version: string;
  summary: string;
  target_key: string;
  actions: ApiCall[];
  compensation: ApiCall[];
  reversibility: "reversible" | "partially_reversible" | "irreversible";
  durability: "ephemeral" | "durable";
  ttl_seconds: number | null;
  blast_radius: { pods: number; nodes: number; namespaces: string[]; user_facing: boolean; score: number };
  expected_signal: {
    alert_should_clear?: string;
    queries?: Record<string, string>;
    thresholds?: Record<string, number>;
    window_seconds?: number;
  };
  confidence: number;
  cost_delta_inr_month: number;
  refusal_reason: string | null;
  policy_decision: "allow" | "require_approval" | "deny";
  policy_reasons: string[];
  created_at: string;
}

export interface Execution {
  id: number;
  plan_id: string | null;
  runbook: string;
  status: string;
  mode: "shadow" | "approval" | "auto";
  detail: string | null;
  state_before: Record<string, unknown>;
  state_after: Record<string, unknown>;
  signals_before: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
  expires_at: string | null;
  durability: string | null;
  gitops_managed: boolean;
  gitops_paused: boolean;
  compensated: boolean;
  pr_url: string | null;
}

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired";

export interface Approval {
  execution_id: number;
  incident_id: number;
  status: ApprovalStatus;
  requested_at: string;
  expires_at: string;
  decided_by: string | null;
  decided_at?: string | null;
  reason: string | null;
}

export interface Verification {
  execution_id: number;
  outcome: "remediated" | "not_recovered" | "inconclusive";
  alert_cleared: boolean | null;
  signals_before: Record<string, number | null>;
  signals_after: Record<string, number | null>;
  reasons: string[];
  compensated: boolean;
  window_seconds: number;
  checked_at: string;
}

export interface LedgerEntry {
  seq: number;
  occurred_at: string;
  event_type: string;
  actor: string;
  execution_id: number | null;
  payload: Record<string, unknown>;
  prev_hash: string;
  hash: string;
}

export interface IncidentDetail extends IncidentSummary {
  closed_at: string | null;
  alerts: Alert[];
  plans: Plan[];
  executions: Execution[];
  approvals: Approval[];
  verifications: Verification[];
  ledger: LedgerEntry[];
}

export interface Ratio {
  value: number | null;
  numerator: number;
  denominator: number;
}

export interface Scorecard {
  incidents: number;
  auto_resolution: Ratio;
  assisted_resolution: Ratio;
  escalation: Ratio;
  false_remediation: Ratio;
  refusal: Ratio;
  mttr_auto_seconds: number | null;
  mttr_assisted_seconds: number | null;
  time_to_detect_failure_seconds: number | null;
}

export interface PendingApproval extends Approval {
  plan: {
    runbook: string;
    summary: string;
    target_key: string;
    actions: string[];
    compensation: string[];
    reversibility: string;
    blast_radius_score: number;
    blast_radius: { pods: number; nodes: number; user_facing: boolean };
    cost_delta_inr_month: number;
    confidence: number;
    expected_alert: string | null;
    window_seconds: number | null;
  } | null;
}

export type PromotionMode = "shadow" | "approval" | "auto";

export interface Rung {
  namespace: string;
  mode: PromotionMode;
  consecutive_good: number;
  total_executions: number;
  total_reverted: number;
  shadow_judged: number;
  shadow_agreements: number;
  last_changed_at: string;
  changed_reason: string | null;
}

export interface RunbookEntry {
  name: string;
  triggers: string[];
  chaos_fixture: string;
  promotion: Rung[];
}

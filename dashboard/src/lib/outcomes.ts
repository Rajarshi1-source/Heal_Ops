/**
 * The meaning of every outcome, in one place.
 *
 * `satisfies Record<Outcome, ...>` is what makes the vocabulary closed: add an outcome to the
 * union and this object stops compiling until it says how the new outcome looks and reads.
 *
 * Tone is a design decision, not decoration. Escalation, refusal and denial are the system admitting
 * "not mine" or "not allowed" -- good outcomes -- so none of them is red. A revert is the system
 * catching its own mistake: legible and calm, never alarming. Red is reserved for the one state with
 * nothing automated left to try.
 */

import type { Outcome, PromotionMode, StageState } from "./types.ts";

export interface OutcomeMeta {
  label: string;
  tone: "success" | "correction" | "critical" | "neutral" | "guarded" | "shadow" | "warning" | "pending";
  description: string;
}

export const OUTCOMES = {
  remediated: {
    label: "Remediated",
    tone: "success",
    description: "Alert cleared, golden signals recovered, no new alerts. The incident is over.",
  },
  not_recovered: {
    label: "Not recovered",
    tone: "warning",
    description: "Verification showed the fix did not work; compensation follows.",
  },
  inconclusive: {
    label: "Inconclusive",
    tone: "warning",
    description: "A signal recovery depends on was unavailable. Unproven, so it was undone.",
  },
  reverted: {
    label: "Reverted",
    tone: "correction",
    description: "The system caught its own mistake and restored the pre-remediation state.",
  },
  revert_failed: {
    label: "Revert failed",
    tone: "critical",
    description: "The fix and its undo both failed. A human is needed; nothing automated is left.",
  },
  escalated: {
    label: "Escalated",
    tone: "neutral",
    description: "Handed to a human. The system admitting \"not mine\" is a good outcome.",
  },
  refused: {
    label: "Refused",
    tone: "guarded",
    description: "The runbook declined. Knowing what not to automate is the feature.",
  },
  denied: {
    label: "Denied",
    tone: "guarded",
    description: "A policy rule blocked it. Guardrails live in policy/remediation.yaml.",
  },
  shadow: {
    label: "Shadow",
    tone: "shadow",
    description: "Recorded what it would have done. Touched nothing.",
  },
  pending: {
    label: "In progress",
    tone: "pending",
    description: "Still moving through the pipeline.",
  },
} as const satisfies Record<Outcome, OutcomeMeta>;

export function outcomeMeta(outcome: Outcome): OutcomeMeta {
  return OUTCOMES[outcome];
}

export const STAGE_STATE_TONE = {
  passed: "success",
  stopped: "guarded",
  corrected: "correction",
  failed: "warning",
  pending: "pending",
  skipped: "neutral",
} as const satisfies Record<StageState, OutcomeMeta["tone"]>;

export const PROMOTION_TONE = {
  shadow: "shadow",
  approval: "guarded",
  auto: "success",
} as const satisfies Record<PromotionMode, OutcomeMeta["tone"]>;

/** Exhaustiveness helper for switch statements over the unions above. */
export function assertNever(value: never): never {
  throw new Error(`unhandled case: ${String(value)}`);
}

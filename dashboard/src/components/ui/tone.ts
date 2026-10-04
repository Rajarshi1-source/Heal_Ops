import type { OutcomeMeta } from "@/lib/outcomes";

type Tone = OutcomeMeta["tone"];

/** Tone -> utility classes. Exhaustive, so a new tone is a compile error here too. */
export const TONE_CLASSES = {
  success: "bg-tone-success/12 text-tone-success ring-tone-success/30",
  correction: "bg-tone-correction/12 text-tone-correction ring-tone-correction/30",
  critical: "bg-tone-critical/15 text-tone-critical ring-tone-critical/40",
  neutral: "bg-tone-neutral/12 text-tone-neutral ring-tone-neutral/30",
  guarded: "bg-tone-guarded/12 text-tone-guarded ring-tone-guarded/30",
  shadow: "bg-transparent text-tone-shadow ring-0 border border-dashed border-tone-shadow/60",
  warning: "bg-tone-warning/12 text-tone-warning ring-tone-warning/30",
  pending: "bg-tone-pending/10 text-tone-pending ring-tone-pending/25",
} as const satisfies Record<Tone, string>;

export const TONE_DOT = {
  success: "bg-tone-success",
  correction: "bg-tone-correction",
  critical: "bg-tone-critical",
  neutral: "bg-tone-neutral",
  guarded: "bg-tone-guarded",
  shadow: "border border-dashed border-tone-shadow bg-transparent",
  warning: "bg-tone-warning",
  pending: "bg-tone-pending animate-pulse",
} as const satisfies Record<Tone, string>;

export type { Tone };

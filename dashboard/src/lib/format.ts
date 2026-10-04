/** Formatting helpers. Pure, so they render identically on the server and in the browser. */

import type { Ratio } from "./types.ts";

export function percent(ratio: Ratio): string {
  return ratio.value === null ? "—" : `${Math.round(ratio.value * 100)}%`;
}

/** "3 of 5": the honest denominator behind every rate. */
export function denominator(ratio: Ratio): string {
  return `${ratio.numerator} of ${ratio.denominator}`;
}

export function duration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(Math.round(seconds), 0);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export function relative(iso: string, now: number = Date.now()): string {
  const delta = Math.round((now - new Date(iso).getTime()) / 1000);
  if (delta < 0) return `in ${duration(-delta)}`;
  if (delta < 5) return "just now";
  return `${duration(delta)} ago`;
}

export function timestamp(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toISOString().replace("T", " ").slice(0, 19) + " UTC";
}

export function shortHash(hash: string, length = 12): string {
  return hash.slice(0, length);
}

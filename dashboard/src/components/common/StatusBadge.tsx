import { Badge } from "@/components/ui/badge";
import { OUTCOMES } from "@/lib/outcomes";
import type { Outcome } from "@/lib/types";

/**
 * One badge per outcome, driven by the closed `Outcome` union. There is no fallback branch: an
 * outcome the dashboard does not know about cannot type-check, so it cannot render as a vague grey
 * pill that nobody notices.
 */
export function StatusBadge({ outcome, detail }: { outcome: Outcome; detail?: string | null }) {
  const meta = OUTCOMES[outcome];
  const suffix = detail && detail !== outcome ? ` (${detail.replaceAll("_", " ")})` : "";
  return (
    <Badge tone={meta.tone} title={meta.description + suffix}>
      {meta.label}
      {suffix && <span className="text-[0.65rem] opacity-75">{suffix}</span>}
    </Badge>
  );
}

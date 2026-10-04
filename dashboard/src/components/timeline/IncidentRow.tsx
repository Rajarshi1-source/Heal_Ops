import Link from "next/link";

import { StatusBadge } from "@/components/common/StatusBadge";
import { Card } from "@/components/ui/card";
import { relative } from "@/lib/format";
import type { IncidentSummary } from "@/lib/types";

import { StateTrail } from "./StateTrail";

/** One incident: what fired, what it was correlated into, and how far the pipeline got. */
export function IncidentRow({ incident, now }: { incident: IncidentSummary; now: number }) {
  return (
    <Card className="transition-colors hover:border-subtle">
      <Link
        href={`/incidents/${incident.id}`}
        className="flex flex-col gap-3 px-5 py-4 outline-none focus-visible:ring-2 focus-visible:ring-accent rounded-xl"
      >
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <StatusBadge outcome={incident.outcome} detail={incident.outcome_detail} />
          <span className="font-mono text-sm">{incident.target_key}</span>
          <span className="text-xs text-muted">{incident.cause_alertname}</span>
          {incident.alert_count > 1 && (
            <span className="text-xs text-subtle" title={incident.correlation_reason}>
              {incident.alert_count} alerts → 1 incident
            </span>
          )}
          <span className="ml-auto text-xs text-subtle" title={incident.opened_at} suppressHydrationWarning>
            #{incident.id} · {relative(incident.opened_at, now)}
          </span>
        </div>
        {incident.summary && <p className="text-sm text-muted">{incident.summary}</p>}
        <StateTrail stages={incident.trail} compact />
      </Link>
    </Card>
  );
}

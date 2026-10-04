"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { duration } from "@/lib/format";
import type { PendingApproval } from "@/lib/types";

/**
 * What the Slack card shows, on the dashboard: the plan, its blast radius and undo, the cost, the
 * recovery it promises, and the time left.
 *
 * The countdown is read from `expires_at` as the API returned it. PostgreSQL owns the deadline, so
 * nothing here computes one from a duration -- a browser clock would disagree with the database,
 * and on expiry the database escalates regardless of what this page shows.
 *
 * `writable` is the build-time flag. In the read-only build the buttons are not rendered at all.
 */
export function ApprovalPending({ approval, writable }: { approval: PendingApproval; writable: boolean }) {
  const remaining = useRemaining(approval.expires_at);
  const [result, setResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const plan = approval.plan;

  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    try {
      const response = await fetch(`/api/approvals/${approval.execution_id}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ decision }),
      });
      const body = (await response.json().catch(() => ({}))) as { detail?: string; status?: string };
      setResult(response.ok ? `Recorded: ${body.status ?? decision}` : (body.detail ?? `Failed (${response.status})`));
    } finally {
      setBusy(false);
    }
  }

  const expired = remaining !== null && remaining <= 0;
  return (
    <Card className="flex flex-col gap-4 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium">{plan?.summary ?? `Execution ${approval.execution_id}`}</p>
          <p className="mt-1 font-mono text-xs text-muted">
            {plan?.target_key} ·{" "}
            <Link className="underline decoration-subtle underline-offset-2" href={`/incidents/${approval.incident_id}`}>
              incident #{approval.incident_id}
            </Link>
          </p>
        </div>
        <Badge tone={expired ? "neutral" : "pending"} aria-live="polite">
          {remaining === null ? "…" : expired ? "Expired — escalated" : `${duration(remaining)} left`}
        </Badge>
      </div>

      {plan && (
        <dl className="grid grid-cols-2 gap-3 text-xs md:grid-cols-4">
          <Fact label="Blast radius" value={`score ${plan.blast_radius_score}`} hint={`${plan.blast_radius.pods} pods / ${plan.blast_radius.nodes} nodes`} />
          <Fact label="Reversibility" value={plan.reversibility.replaceAll("_", " ")} />
          <Fact label="Cost delta" value={plan.cost_delta_inr_month ? `₹${Math.round(plan.cost_delta_inr_month)}/mo` : "none"} />
          <Fact
            label="Expected recovery"
            value={plan.expected_alert || "plan's own checks"}
            hint={plan.window_seconds ? `within ${duration(plan.window_seconds)}` : undefined}
          />
        </dl>
      )}

      {plan && (
        <div className="grid gap-3 md:grid-cols-2">
          <CallList title="Actions" calls={plan.actions} />
          <CallList title="Registered undo (persisted before acting)" calls={plan.compensation} />
        </div>
      )}

      {approval.reason && <p className="text-xs text-muted">Why approval is needed: {approval.reason}</p>}

      {writable ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="primary" size="sm" disabled={busy || expired} onClick={() => decide("approve")}>
            Approve
          </Button>
          <Button size="sm" disabled={busy || expired} onClick={() => decide("reject")}>
            Reject
          </Button>
          {result && (
            <span role="status" className="text-xs text-muted">
              {result}
            </span>
          )}
        </div>
      ) : (
        <p className="text-xs text-subtle">Read-only view. Decisions are made from the Slack card.</p>
      )}
    </Card>
  );
}

function useRemaining(expiresAt: string): number | null {
  const [remaining, setRemaining] = useState<number | null>(null);
  useEffect(() => {
    const deadline = new Date(expiresAt).getTime();
    const tick = () => setRemaining(Math.max((deadline - Date.now()) / 1000, 0));
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [expiresAt]);
  return remaining;
}

function Fact({ label, value, hint }: { label: string; value: string; hint?: string | undefined }) {
  return (
    <div>
      <dt className="text-subtle">{label}</dt>
      <dd className="mt-0.5 text-foreground">{value}</dd>
      {hint && <dd className="text-subtle">{hint}</dd>}
    </div>
  );
}

function CallList({ title, calls }: { title: string; calls: string[] }) {
  return (
    <div>
      <p className="mb-1 text-xs text-subtle">{title}</p>
      <ul className="flex flex-col gap-1">
        {calls.map((call) => (
          <li key={call} className="rounded bg-background/60 px-2 py-1 font-mono text-[0.7rem] text-muted">
            {call}
          </li>
        ))}
      </ul>
    </div>
  );
}

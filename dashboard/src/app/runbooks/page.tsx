import type { Metadata } from "next";

import { EmptyState, ErrorState } from "@/components/common/States";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { engine } from "@/lib/engine";
import { PROMOTION_TONE } from "@/lib/outcomes";
import type { Rung, RunbookEntry } from "@/lib/types";

export const metadata: Metadata = { title: "Runbooks" };
export const dynamic = "force-dynamic";

// The ladder's thresholds, as the engine enforces them (engine/src/promotion.py).
const SHADOW_MIN_PLANS = 10;
const SHADOW_MIN_AGREEMENT = 0.9;
const APPROVAL_MIN_CONSECUTIVE_GOOD = 20;

/**
 * The runbook catalog: the model card rendered as UI. Each runbook's triggers, the fault injector
 * that proves it, and -- the part that matters -- how much authority it has earned in each
 * namespace, and how far it is from the next rung.
 */
export default async function RunbooksPage() {
  const { data, error } = await engine<{ runbooks: RunbookEntry[] }>("/api/runbooks", { revalidate: 30 });
  if (!data) return <ErrorState error={error ?? "unknown"} />;

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Runbooks</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted">
          Authority is earned per namespace, on evidence: <Badge tone="shadow">shadow</Badge> plans without acting,{" "}
          <Badge tone="guarded">approval</Badge> acts when a human agrees, <Badge tone="success">auto</Badge> acts
          within policy. Promotion takes twenty good outcomes; demotion takes one bad one.
        </p>
      </header>

      {data.runbooks.length === 0 ? (
        <EmptyState title="No runbooks registered">The engine reported an empty registry.</EmptyState>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {data.runbooks.map((runbook) => (
            <Card key={runbook.name} className="flex flex-col gap-4 p-5">
              <div>
                <h2 className="font-mono text-sm font-semibold">{runbook.name}</h2>
                <p className="mt-1 text-xs text-muted">
                  answers <span className="text-foreground">{runbook.triggers.join(", ")}</span> · proven by{" "}
                  <code className="font-mono text-foreground">{runbook.chaos_fixture}</code>
                </p>
              </div>
              {runbook.promotion.length === 0 ? (
                <p className="text-xs text-subtle">
                  No authority anywhere yet. Its first plan in a namespace is recorded in shadow mode.
                </p>
              ) : (
                <ul className="flex flex-col gap-3">
                  {runbook.promotion.map((rung) => (
                    <RungRow key={rung.namespace} rung={rung} />
                  ))}
                </ul>
              )}
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

function RungRow({ rung }: { rung: Rung }) {
  const progress = next(rung);
  return (
    <li className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2 text-sm">
        <span className="font-mono">{rung.namespace === "-" ? "cluster" : rung.namespace}</span>
        <Badge tone={PROMOTION_TONE[rung.mode]}>{rung.mode}</Badge>
        <span className="ml-auto text-xs text-subtle">
          {rung.total_executions} acted · {rung.total_reverted} reverted
        </span>
      </div>
      {progress && (
        <div>
          <div className="h-1.5 overflow-hidden rounded-full bg-surface-raised" aria-hidden>
            <div className="h-full rounded-full bg-tone-guarded/70" style={{ width: `${Math.round(progress.fraction * 100)}%` }} />
          </div>
          <p className="mt-1 text-[0.7rem] text-subtle">{progress.label}</p>
        </div>
      )}
      {rung.changed_reason && <p className="text-[0.7rem] text-subtle">Last change: {rung.changed_reason}</p>}
    </li>
  );
}

function next(rung: Rung): { fraction: number; label: string } | null {
  switch (rung.mode) {
    case "shadow": {
      const agreement = rung.shadow_judged ? rung.shadow_agreements / rung.shadow_judged : 0;
      return {
        fraction: Math.min(rung.shadow_judged / SHADOW_MIN_PLANS, 1),
        label:
          `${rung.shadow_judged}/${SHADOW_MIN_PLANS} plans judged by a human, ` +
          `${Math.round(agreement * 100)}% agreement (needs ${SHADOW_MIN_AGREEMENT * 100}%) to reach approval`,
      };
    }
    case "approval":
      return {
        fraction: Math.min(rung.consecutive_good / APPROVAL_MIN_CONSECUTIVE_GOOD, 1),
        label: `${rung.consecutive_good}/${APPROVAL_MIN_CONSECUTIVE_GOOD} consecutive verified remediations to reach auto`,
      };
    case "auto":
      return null;
  }
}

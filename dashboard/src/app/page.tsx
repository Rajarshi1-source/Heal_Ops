import { KPICards } from "@/components/dashboard/KPICards";
import { EmptyState, ErrorState, Freshness } from "@/components/common/States";
import { LiveIncidentFeed } from "@/components/timeline/LiveIncidentFeed";
import { engine } from "@/lib/engine";
import type { IncidentPage, Scorecard } from "@/lib/types";

// Rendered per request, from the engine's data cache: fresh within seconds, and still served from
// the last good response if the engine is briefly unreachable.
export const dynamic = "force-dynamic";

export default async function Home() {
  const [stats, incidents] = await Promise.all([
    engine<Scorecard>("/api/stats"),
    engine<IncidentPage>("/api/incidents?limit=25"),
  ]);

  return (
    <div className="flex flex-col gap-8">
      <section aria-labelledby="scorecard" className="flex flex-col gap-3">
        <div className="flex items-end justify-between gap-4">
          <div>
            <h1 id="scorecard" className="text-xl font-semibold tracking-tight">
              Efficacy scorecard
            </h1>
            <p className="text-sm text-muted">
              Success means the incident is over — not that an API call returned 200.
            </p>
          </div>
          <Freshness fetchedAt={stats.fetchedAt} />
        </div>
        {stats.data ? <KPICards card={stats.data} /> : <ErrorState error={stats.error ?? "unknown"} />}
      </section>

      <section aria-labelledby="incidents" className="flex flex-col gap-3">
        <div className="flex items-end justify-between gap-4">
          <h2 id="incidents" className="text-lg font-semibold tracking-tight">
            Incidents
          </h2>
          <Freshness fetchedAt={incidents.fetchedAt} />
        </div>
        {incidents.error && !incidents.data && <ErrorState error={incidents.error} />}
        {incidents.data && incidents.data.items.length === 0 && (
          <EmptyState title="No incidents yet — the normal state of a healthy cluster">
            Inject the five demo faults with <code className="font-mono text-foreground">make demo</code>{" "}
            and watch each one move through correlate, plan, policy, act, verify — including the
            ones HealOps refuses.
          </EmptyState>
        )}
        {incidents.data && incidents.data.items.length > 0 && <LiveIncidentFeed initial={incidents.data} />}
      </section>
    </div>
  );
}

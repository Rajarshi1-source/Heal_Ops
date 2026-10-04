import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { JsonDiff } from "@/components/common/JsonDiff";
import { ErrorState, Freshness } from "@/components/common/States";
import { StatusBadge } from "@/components/common/StatusBadge";
import { StateTrail } from "@/components/timeline/StateTrail";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { engine } from "@/lib/engine";
import { duration, shortHash, timestamp } from "@/lib/format";
import { OUTCOMES } from "@/lib/outcomes";
import type { Alert, ApiCall, Execution, IncidentDetail, LedgerEntry, Plan, Verification } from "@/lib/types";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ id: string }> };

async function load(id: string) {
  return engine<IncidentDetail>(`/api/incidents/${encodeURIComponent(id)}`);
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { id } = await params;
  const { data } = await load(id);
  if (!data) return { title: `Incident #${id}` };
  const outcome = OUTCOMES[data.outcome].label;
  return {
    title: `#${data.id} ${outcome} · ${data.target_key}`,
    description: `${data.cause_alertname} on ${data.target_key}: ${data.summary ?? outcome}.`,
  };
}

export default async function IncidentPage({ params }: Props) {
  const { id } = await params;
  const result = await load(id);
  if (result.status === 404) notFound();
  if (!result.data) return <ErrorState error={result.error ?? "unknown"} />;
  const incident = result.data;

  return (
    <article className="flex flex-col gap-6">
      <header className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <StatusBadge outcome={incident.outcome} detail={incident.outcome_detail} />
          <h1 className="font-mono text-lg">{incident.target_key}</h1>
          <span className="text-sm text-muted">{incident.cause_alertname}</span>
          <span className="ml-auto text-xs text-subtle">
            Incident #{incident.id} · opened {timestamp(incident.opened_at)}
          </span>
        </div>
        <p className="text-sm text-muted">{OUTCOMES[incident.outcome].description}</p>
        <Card className="px-5 py-4">
          <StateTrail stages={incident.trail} />
        </Card>
        <Freshness fetchedAt={result.fetchedAt} />
      </header>

      <Alerts alerts={incident.alerts} reason={incident.correlation_reason} />
      {incident.plans.map((plan) => (
        <PlanCard key={plan.id} plan={plan} />
      ))}
      {incident.executions.map((execution) => (
        <ExecutionCard
          key={execution.id}
          execution={execution}
          verifications={incident.verifications.filter((v) => v.execution_id === execution.id)}
        />
      ))}
      <Ledger entries={incident.ledger} />
    </article>
  );
}

// --- sections -----------------------------------------------------------------------------------

function Alerts({ alerts, reason }: { alerts: Alert[]; reason: string }) {
  const causes = alerts.filter((a) => a.role === "cause");
  const symptoms = alerts.filter((a) => a.role === "symptom");
  return (
    <Card>
      <CardHeader>
        <CardTitle>Alerts</CardTitle>
        <CardDescription>
          The cause selects the runbook. Symptoms attach as evidence and never trigger their own — that
          is how forty alerts become one incident.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {causes.map((alert) => (
          <div key={alert.id}>
            <AlertLine alert={alert} />
            {symptoms.length > 0 && (
              <ul className="mt-2 ml-4 flex flex-col gap-1 border-l border-border pl-4">
                <li className="text-xs text-subtle">
                  {symptoms.length} symptom alert{symptoms.length > 1 ? "s" : ""} correlated as evidence ·{" "}
                  <span className="font-mono">{reason}</span>
                </li>
                {symptoms.map((s) => (
                  <li key={s.id}>
                    <AlertLine alert={s} />
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function AlertLine({ alert }: { alert: Alert }) {
  const where = alert.pod ?? alert.node ?? alert.namespace ?? "";
  return (
    <p className="flex flex-wrap items-center gap-2 text-sm">
      <Badge tone={alert.role === "cause" ? "neutral" : "pending"}>{alert.role}</Badge>
      <span>{alert.alertname}</span>
      <span className="font-mono text-xs text-muted">{where}</span>
      <span className="text-xs text-subtle">{alert.status}</span>
    </p>
  );
}

function describe(call: ApiCall): string {
  const target = call.namespace ? `${call.namespace}/${call.name}` : call.name;
  return `${call.verb} ${call.resource}${call.subresource ? `/${call.subresource}` : ""} ${target}`;
}

function PlanCard({ plan }: { plan: Plan }) {
  const signal = plan.expected_signal;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Plan · {plan.runbook}</CardTitle>
        <CardDescription>
          A plan is data, not code: reviewed before it runs, with its undo decided before acting.
          Planner {plan.planner_version}.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {plan.refusal_reason ? (
          <p className="text-sm text-tone-guarded">Refused: {plan.refusal_reason}</p>
        ) : (
          <p className="text-sm">{plan.summary}</p>
        )}

        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Badge tone={plan.policy_decision === "deny" ? "guarded" : plan.policy_decision === "allow" ? "success" : "neutral"}>
            policy: {plan.policy_decision.replaceAll("_", " ")}
          </Badge>
          {plan.policy_reasons.map((rule) => (
            <code key={rule} className="rounded bg-background/60 px-1.5 py-0.5 font-mono text-muted">
              {rule}
            </code>
          ))}
        </div>

        {plan.actions.length > 0 && (
          <div className="grid gap-4 md:grid-cols-2">
            <Calls title="Actions" calls={plan.actions} />
            <Calls title="Registered undo" calls={plan.compensation} />
          </div>
        )}

        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-xs md:grid-cols-4">
          <Fact label="Blast radius" value={`score ${plan.blast_radius.score}`} hint={`${plan.blast_radius.pods} pods · ${plan.blast_radius.nodes} nodes`} />
          <Fact label="Reversibility" value={plan.reversibility.replaceAll("_", " ")} />
          <Fact
            label="Durability"
            value={plan.durability}
            hint={plan.durability === "ephemeral" && plan.ttl_seconds ? `expires after ${duration(plan.ttl_seconds)}` : "needs a pull request"}
          />
          <Fact label="Confidence" value={`${Math.round(plan.confidence * 100)}%`} hint="graded in CI" />
          {plan.cost_delta_inr_month > 0 && <Fact label="Cost delta" value={`₹${Math.round(plan.cost_delta_inr_month)}/month`} />}
          <Fact
            label="Recovery promised"
            value={signal.alert_should_clear || "the plan's own checks"}
            hint={signal.window_seconds ? `within ${duration(signal.window_seconds)}` : undefined}
          />
        </dl>

        {signal.thresholds && Object.keys(signal.thresholds).length > 0 && (
          <div className="text-xs">
            <p className="mb-1 text-subtle">Committed recovery checks</p>
            <ul className="flex flex-col gap-1">
              {Object.entries(signal.thresholds).map(([name, ceiling]) => (
                <li key={name} className="font-mono text-muted">
                  {name} ≤ {ceiling}
                  <span className="block truncate text-subtle" title={signal.queries?.[name]}>
                    {signal.queries?.[name]}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Calls({ title, calls }: { title: string; calls: ApiCall[] }) {
  return (
    <div>
      <p className="mb-1 text-xs text-subtle">{title}</p>
      <ul className="flex flex-col gap-1.5">
        {calls.map((call, i) => (
          <li key={i} className="rounded bg-background/60 px-2 py-1.5 font-mono text-[0.72rem]">
            <span className="text-foreground">{describe(call)}</span>
            {Object.keys(call.body).length > 0 && (
              <pre className="mt-1 whitespace-pre-wrap break-all text-subtle">{JSON.stringify(call.body)}</pre>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ExecutionCard({ execution, verifications }: { execution: Execution; verifications: Verification[] }) {
  const shadow = execution.mode === "shadow";
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Execution #{execution.id} · {execution.mode}
          <span className="ml-2 font-normal text-muted">{execution.status.replaceAll("_", " ")}</span>
        </CardTitle>
        {execution.detail && <CardDescription>{execution.detail}</CardDescription>}
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-xs md:grid-cols-4">
          <Fact label="Applied" value={timestamp(execution.finished_at)} />
          <Fact label="Expires" value={timestamp(execution.expires_at)} hint={execution.durability ?? undefined} />
          <Fact
            label="GitOps"
            value={execution.gitops_managed ? (execution.gitops_paused ? "managed · reconciliation paused" : "managed") : "unmanaged"}
          />
          <Fact label="Compensated" value={execution.compensated ? "yes" : "no"} />
        </dl>
        {execution.pr_url && (
          <p className="text-sm">
            Durable fix:{" "}
            <a className="text-accent underline underline-offset-2" href={execution.pr_url} rel="noreferrer" target="_blank">
              {execution.pr_url}
            </a>
          </p>
        )}
        {!shadow && (
          <JsonDiff before={execution.state_before} after={execution.state_after} label="Cluster state before → after" />
        )}
        {verifications.map((v) => (
          <VerificationBlock key={v.checked_at} verification={v} />
        ))}
      </CardContent>
    </Card>
  );
}

function VerificationBlock({ verification }: { verification: Verification }) {
  const names = [...new Set([...Object.keys(verification.signals_before), ...Object.keys(verification.signals_after)])].sort();
  return (
    <div className="rounded-lg border border-border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-subtle">Verification</span>
        <StatusBadge outcome={verification.outcome} />
        {verification.compensated && <StatusBadge outcome="reverted" />}
        <span className="ml-auto text-xs text-subtle">
          {timestamp(verification.checked_at)} · window {duration(verification.window_seconds)}
        </span>
      </div>
      <ul className="mt-2 list-disc pl-5 text-sm text-muted">
        {verification.reasons.map((r) => (
          <li key={r}>{r}</li>
        ))}
      </ul>
      {names.length > 0 && (
        <table className="mt-3 w-full text-left text-xs">
          <thead className="text-subtle">
            <tr>
              <th className="py-1 font-normal">Signal</th>
              <th className="py-1 font-normal">Before</th>
              <th className="py-1 font-normal">After</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {names.map((name) => (
              <tr key={name} className="border-t border-border/60">
                <td className="py-1 text-muted">{name}</td>
                <td className="py-1">{show(verification.signals_before[name])}</td>
                <td className="py-1">{show(verification.signals_after[name])}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** A missing signal is never a healthy signal, so it is never rendered as a number. */
function show(value: number | null | undefined): string {
  return value === null || value === undefined ? "not observed" : String(Number(value.toFixed(4)));
}

function Ledger({ entries }: { entries: LedgerEntry[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Audit ledger</CardTitle>
        <CardDescription>
          Hash-chained and append-only: each entry commits to the one before it. Verify offline with{" "}
          <code className="font-mono">healopsctl audit --verify-chain</code>.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ol className="flex flex-col divide-y divide-border/60">
          {entries.map((entry) => (
            <li key={entry.seq} className="py-2">
              <details>
                <summary className="flex cursor-pointer flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                  <span className="font-mono text-subtle">#{entry.seq}</span>
                  <span className="font-medium">{entry.event_type}</span>
                  <span className="text-muted">{entry.actor}</span>
                  <span className="text-subtle">{timestamp(entry.occurred_at)}</span>
                  <code className="ml-auto font-mono text-subtle" title={entry.hash}>
                    {shortHash(entry.prev_hash)} → {shortHash(entry.hash)}
                  </code>
                </summary>
                <pre className="mt-2 overflow-x-auto rounded bg-background/60 p-2 font-mono text-[0.7rem] text-muted">
                  {JSON.stringify(entry.payload, null, 2)}
                </pre>
              </details>
            </li>
          ))}
        </ol>
      </CardContent>
    </Card>
  );
}

function Fact({ label, value, hint }: { label: string; value: string; hint?: string | undefined }) {
  return (
    <div>
      <dt className="text-subtle">{label}</dt>
      <dd className="mt-0.5">{value}</dd>
      {hint && <dd className="text-subtle">{hint}</dd>}
    </div>
  );
}

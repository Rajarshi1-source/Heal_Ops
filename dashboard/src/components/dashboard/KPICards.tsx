import { Card } from "@/components/ui/card";
import { denominator, duration, percent } from "@/lib/format";
import type { Ratio, Scorecard } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * The efficacy scorecard. Every rate carries its honest denominator, because "3 of 5" and "60%"
 * are different claims and only one of them can be checked.
 *
 * Escalation and refusal are styled neutrally on purpose: a red escalation card would contradict
 * the thesis that the system handing an incident to a human is a good outcome.
 */
export function KPICards({ card }: { card: Scorecard }) {
  const falseRate = card.false_remediation;
  const clean = falseRate.numerator === 0;

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,2fr)]">
      <Card className="flex flex-col justify-between p-5">
        <div>
          <p className="text-xs font-medium uppercase tracking-wider text-muted">False-remediation rate</p>
          <p
            className={cn(
              "mt-2 font-mono text-6xl font-semibold tabular-nums tracking-tight",
              clean ? "text-tone-success" : "text-tone-warning",
            )}
          >
            {falseRate.denominator === 0 ? "0%" : percent(falseRate)}
          </p>
        </div>
        <p className="mt-3 text-sm text-muted">
          {denominator(falseRate)} executed changes had to be taken back after verification.{" "}
          <span className="text-subtle">The trust metric — green at zero.</span>
        </p>
      </Card>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
        <Rate label="Auto-resolved" ratio={card.auto_resolution} note="no human involved" />
        <Rate label="Assisted" ratio={card.assisted_resolution} note="automation acted, a human approved" />
        <Rate label="Escalated" ratio={card.escalation} note={`handed to a human — "not mine"`} />
        <Rate label="Refused or denied" ratio={card.refusal} note="of plans; guardrails doing work" />
        <Metric
          label="MTTR auto vs assisted"
          value={`${duration(card.mttr_auto_seconds)} / ${duration(card.mttr_assisted_seconds)}`}
          note="alert to verified recovery"
        />
        <Metric
          label="Time to detect failure"
          value={duration(card.time_to_detect_failure_seconds)}
          note="how fast it notices its own mistake"
        />
      </div>
    </div>
  );
}

function Rate({ label, ratio, note }: { label: string; ratio: Ratio; note: string }) {
  return (
    <Metric
      label={label}
      value={percent(ratio)}
      note={`${denominator(ratio)} · ${note}`}
      title={`${ratio.numerator} / ${ratio.denominator}`}
    />
  );
}

function Metric({ label, value, note, title }: { label: string; value: string; note: string; title?: string }) {
  return (
    <Card className="p-4" title={title}>
      <p className="text-xs text-muted">{label}</p>
      <p className="mt-1 font-mono text-2xl font-semibold tabular-nums">{value}</p>
      <p className="mt-1 text-[0.7rem] leading-snug text-subtle">{note}</p>
    </Card>
  );
}

import { TONE_DOT } from "@/components/ui/tone";
import { STAGE_STATE_TONE } from "@/lib/outcomes";
import type { Stage, StageState } from "@/lib/types";
import { cn } from "@/lib/utils";

const STATE_TEXT = {
  passed: "text-foreground",
  stopped: "text-tone-guarded",
  corrected: "text-tone-correction",
  failed: "text-tone-warning",
  pending: "text-muted",
  skipped: "text-subtle",
} as const satisfies Record<StageState, string>;

/**
 * The pipeline as it actually ran: correlate -> plan -> policy -> gate -> execute -> verify ->
 * outcome. This trail *is* the project's differentiator made visible, so every stage shows its own
 * outcome -- including the stages that did nothing. A refusal or a shadow record ends the trail as
 * a deliberate stop, not as an error, and a revert reads as a correction.
 */
export function StateTrail({ stages, compact = false }: { stages: Stage[]; compact?: boolean }) {
  return (
    <ol aria-label="Pipeline trail" className="flex flex-wrap items-start gap-y-2">
      {stages.map((stage, i) => {
        const tone = STAGE_STATE_TONE[stage.state];
        const description = `${stage.stage}: ${stage.label}${stage.detail ? ` — ${stage.detail}` : ""}`;
        return (
          <li key={`${stage.stage}-${i}`} className="flex items-center">
            {i > 0 && (
              <span
                aria-hidden
                className={cn(
                  "mx-1.5 h-px w-4 sm:w-6",
                  stage.state === "pending" ? "bg-border" : "bg-subtle",
                )}
              />
            )}
            <span
              tabIndex={0}
              title={description}
              className="group flex items-center gap-1.5 rounded-md px-1 py-0.5 outline-none"
            >
              <span aria-hidden className={cn("size-2.5 shrink-0 rounded-full", TONE_DOT[tone])} />
              <span className="flex flex-col leading-tight">
                {!compact && (
                  <span className="text-[0.62rem] font-medium uppercase tracking-wider text-subtle">
                    {stage.stage}
                  </span>
                )}
                <span className={cn("text-xs", STATE_TEXT[stage.state])}>{stage.label}</span>
              </span>
              <span className="sr-only">{description}</span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}

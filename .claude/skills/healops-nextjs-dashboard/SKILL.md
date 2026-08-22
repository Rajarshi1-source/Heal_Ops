---
name: healops-nextjs-dashboard
description: "Build the HealOps audit dashboard, the Next.js 16.3 App Router front end on Node 24 LTS styled with Tailwind CSS and shadcn/ui in a dark-first operations theme. Use for ANY dashboard work: pages and layouts under app/, Server versus Client Components, Route Handlers proxying the engine API, the incident timeline showing the plan-policy-execute-verify-compensate state trail, KPI cards including false-remediation rate, the before-and-after JSON diff viewer, pending-approval cards, the runbook catalog with promotion state, the live incident feed, status badges, and empty states. Trigger on App Router, server component, client component, generateMetadata, route handler, incident timeline, KPI cards, JsonDiff, StatusBadge, Tailwind, shadcn, Radix, dark mode, or make this look less generic. MANDATE: Next.js 16.3 App Router with TypeScript strict on Node 24 LTS, Server-Components-first, no Pages Router. shadcn/ui components are copied into components/ui and edited there, never an npm dependency."
---

# HealOps Audit Dashboard

The dashboard's job is to make a claim checkable: *the system fixed three of five incidents automatically, and here is the evidence for each one.* Every design decision serves that.

## Version floor

| Component | Pin | Why |
|---|---|---|
| Node.js | **24 LTS** | Node 20 reached end of life in April 2026 |
| Next.js | **16.3.x**, App Router | Next 14 is EOL; Next 15 hits EOL Oct 2026. Turbopack is the default bundler in 16 |
| TypeScript | strict | Discriminated unions carry the incident state machine |
| Tailwind + shadcn/ui | current, copied in | Dark-mode-first; operations dashboards are always dark |

## Layout

```
dashboard/src/
├── app/
│   ├── layout.tsx                    # dark theme root, nav
│   ├── page.tsx                      # main incident timeline
│   ├── incidents/[id]/page.tsx       # incident detail
│   ├── runbooks/page.tsx             # catalog + stats + promotion state
│   ├── approvals/page.tsx            # pending approvals
│   └── api/                          # incidents  executions  stats  health
├── components/
│   ├── timeline/                     # IncidentTimeline  TimelineEvent  ExecutionDetail
│   ├── dashboard/                    # KPICards  RunbookSuccessChart
│   │                                 # IncidentFrequency  ApprovalPending
│   ├── common/                       # StatusBadge  JsonDiff  LoadingSkeleton
│   └── ui/                           # shadcn/ui, COPIED IN and edited here
├── hooks/                            # useIncidents  useExecutions  useStats
└── types/index.ts
```

## Server-first, and where that breaks

Default to Server Components. Reach for `'use client'` only for genuine interactivity:

- The live incident feed (polling interval or an SSE subscription)
- `JsonDiff` expand/collapse state
- Timeline filters and the approval buttons
- Anything holding `useState`

Incident lists and detail pages fetch on the server. The detail page is the heaviest read in the app and it is fully static per incident once the incident closes — cache accordingly rather than re-fetching a settled record.

Route Handlers under `app/api/` proxy the engine rather than talking to PostgreSQL directly. That keeps one query layer, one set of types, and — importantly — means the public deployment can point at a read-only seeded API with no cluster credentials anywhere in the front end.

Add `generateMetadata` on the incident detail route so a shared link previews as the incident it is.

## The incident timeline is the centrepiece

Render the pipeline as a **horizontal state trail** on each incident, because that trail *is* the differentiator made visible:

```
correlate → plan → policy → [shadow|approval] → execute → verify → {remediated|reverted|escalated}
```

Every stage shows its own outcome. Design for the stages that do nothing:

- **Refused** — the runbook declined, with the reason. A first-class outcome, not an error state.
- **Denied** — policy blocked it, with the rule id and message that fired.
- **Shadow** — "would have done X", visually distinct from anything that acted.
- **Reverted** — verification failed and compensation ran. This is the state the project is proudest of, so make it legible rather than alarming: it is the system catching its own mistake.

Symptom alerts attach to the cause incident as evidence. Show them nested under the cause with the correlation reason (`symptom_of_node_cause`, `symptom_of_workload_cause`, `same_target`), never as separate rows — the whole point of correlation is that forty alerts are one incident.

## KPI cards

Mirror the efficacy scorecard, with honest denominators visible on hover or in a caption:

| Card | Note |
|---|---|
| Auto-resolution rate | Excludes human-approved fixes |
| Assisted-resolution rate | Automation acted, a human authorised |
| Escalation rate | Present it neutrally — this is a good number, the system admitting "not mine" |
| **False-remediation rate** | **Single big number, green at zero.** The trust metric |
| MTTR auto vs escalated | Side by side; the contrast is the value argument |
| Time-to-detect-failure | How fast the system notices its own mistake |
| Refusal rate | Proves guardrails are load-bearing |

Do not style escalation or refusal as failures. A red "escalation rate" card would contradict the project's entire thesis.

## Components worth getting right

**`JsonDiff`** renders `state_before` versus `state_after` from an execution. Highlight only changed paths, collapse unchanged subtrees by default, and make the changed lines copyable. This is how a reviewer confirms a compensation restored state exactly.

**`StatusBadge`** covers the full outcome vocabulary — `REMEDIATED`, `NOT_RECOVERED`, `INCONCLUSIVE`, `REVERTED`, `REVERT_FAILED`, `ESCALATED`, `REFUSED`, `DENIED`, `SHADOW`, `PENDING`. Drive it from a discriminated union on the outcome type so a new outcome is a compile error, not a silently grey badge.

**`ApprovalPending`** shows what Slack shows: the plan summary, blast radius, cost delta, expected recovery signal, and the countdown to `expires_at`. Read the deadline from the API — PostgreSQL is the source of truth, so never compute expiry client-side from a duration.

**Runbook catalog** lists each runbook with its triggers, reversibility class, chaos fixture, and **promotion mode per namespace** (shadow / approval / auto). This is the model card rendered as UI.

## Styling

Dark-first, because operations dashboards are. Theme through semantic tokens in CSS variables, never hardcoded colours — the outcome vocabulary above needs consistent semantics across badges, timeline nodes, and charts, and that only holds if there is one source of colour truth.

shadcn/ui components are **copied into `components/ui` and edited there**, never installed as an npm dependency. Preserve Radix accessibility primitives when editing: keyboard navigation on the timeline, focus management in the approval dialog, and `aria-live` on the incident feed so a screen reader announces new incidents.

Charts: keep them legible at a glance over decorative. A stacked area of auto/assisted/escalated over time and a per-runbook success bar are enough; the timeline carries the narrative.

## States that actually happen

Build all four for every data view, not just the happy path:

- **Loading** — `LoadingSkeleton` matching the final layout, so nothing jumps.
- **Empty** — "no incidents yet" is the *normal* state of a healthy cluster and the first thing a visitor sees. Make it say something useful (how to trigger the demo).
- **Error** — the engine or its database is unreachable. Degrade to cached data with a staleness note rather than a blank page: *a dashboard that errors during an incident is worse than one showing 30-second-old data.*
- **Stale** — cache-aside with stale-while-revalidate; show the age when data is served stale.

## Read-only in public, always

The public demo is seeded with real recorded incidents plus rendered `healopsctl replay` output. The approval buttons must be **absent or disabled** there, not merely unwired — an approval control that 404s invites someone to find out why. Gate write affordances behind a build-time flag so the read-only build cannot render them at all.

Keep the replay page's output format aligned with the CLI's, since that format doubles as a golden-file test.

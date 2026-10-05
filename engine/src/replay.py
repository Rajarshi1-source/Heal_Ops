"""`healopsctl replay`: an incident, reconstructed from its evidence bundle alone.

No cluster, no network, no database -- the bundle is the input and this module is a pure function
of it. Two properties are load-bearing:

* **The output is byte-identical on every run.** No clock, no locale, no dict-ordering dependence.
  Replay on a laptop with the cluster disconnected must print exactly what it printed on the day.
* **The format is stable.** It is a golden-file test, and the public read-only dashboard renders it
  as a page, so changing a line is a reviewed change to two consumers, not a cosmetic tweak.
"""

from __future__ import annotations

from typing import Any

import ledger
import planner
from bundle import IncidentBundle
from plan import digest_of

LABEL_WIDTH = 11


def render(bundle: dict[str, Any], sha256: str | None = None) -> str:
    lines: list[tuple[str, str]] = []
    add = lines.append

    add(("incident", f"{bundle['target_key']}  {bundle['cause_alertname']}  {bundle['opened_at']}"))

    alerts = bundle.get("alerts") or []
    symptoms = sum(1 for a in alerts if a.get("role") == "symptom")
    add(
        (
            "correlation",
            f"cause={bundle['cause_alertname']} ({bundle.get('correlation_reason')}); "
            f"{len(alerts)} alert(s), {symptoms} symptom(s) attached as evidence",
        )
    )

    plans = bundle.get("plans") or []
    plan = plans[-1] if plans else None
    if plan is None:
        add(("plan", "none -- escalated without a runbook"))
    elif plan.get("refusal_reason"):
        add(("plan", f"REFUSED by {plan['runbook']}: {plan['refusal_reason']}"))
    else:
        radius = plan["blast_radius"]
        lifetime = (
            f"ephemeral ttl={_duration(plan.get('ttl_seconds'))}"
            if plan["durability"] == "ephemeral"
            else "durable"
        )
        add(("plan", f"{plan['summary']}"))
        add(
            (
                "",
                f"({plan['reversibility']}, {lifetime}, blast radius: {radius['pods']} pods / "
                f"{radius['nodes']} nodes / "
                f"{'user-facing' if radius.get('user_facing') else 'internal'}, "
                f"confidence {float(plan['confidence']):.2f})",
            )
        )
    add(("replanned", _replanned(bundle)))
    if plan is not None and not plan.get("refusal_reason"):
        reasons = ", ".join(plan.get("policy_reasons") or []) or "no rule fired"
        add(("policy", f"{plan['policy_decision']}  [{reasons}]"))

    approvals = bundle.get("approvals") or []
    for approval in approvals:
        who = approval.get("decided_by") or "nobody"
        add(("approval", f"{approval['status']} by {who}; deadline {approval['expires_at']}"))

    for execution in bundle.get("executions") or []:
        if execution["mode"] == "shadow":
            add(("execute", "SHADOW -- recorded what it would have done, touched nothing"))
            continue
        took = _elapsed(execution.get("started_at"), execution.get("finished_at"))
        # What the execute entry recorded at the time -- not the execution row's final detail, which
        # a later revert overwrites with its own story.
        applied = _entry(bundle, "execute", execution["id"])
        if applied is None:
            add(("execute", f"{execution['status']}  (no execute entry recorded)"))
        else:
            add(
                (
                    "execute",
                    f"{'ok' if applied.get('ok') else 'FAILED'}  {applied.get('detail') or ''}"
                    + (f"  in {took}" if took else ""),
                )
            )
        if execution.get("gitops_managed"):
            add(("gitops", "managed; reconciliation paused for the mitigation window"))
        if execution.get("pr_url"):
            add(("durable", f"PR {execution['pr_url']}"))

    for verification in bundle.get("verifications") or []:
        add(
            (
                "verify",
                f"{verification['outcome'].upper()}  "
                + "; ".join(verification.get("reasons") or []),
            )
        )

    for entry in bundle.get("ledger") or []:
        if entry["event_type"] == "compensate":
            payload = entry.get("payload") or {}
            add(
                (
                    "compensate",
                    f"{'ok' if payload.get('ok') else 'FAILED'}  ({payload.get('trigger')})  "
                    f"{payload.get('detail', '')}",
                )
            )

    add(
        (
            "outcome",
            f"{bundle['outcome']}"
            + (
                f" ({bundle['outcome_detail']})"
                if bundle.get("outcome_detail") not in (None, bundle["outcome"])
                else ""
            ),
        )
    )

    entries = bundle.get("ledger") or []
    status = ledger.verify_links(list(entries))
    root = entries[-1]["hash"][:12] if entries else "-"
    verdict = "VALID" if status.valid else f"BROKEN: {status.detail}"
    add(
        (
            "ledger",
            f"{len(entries)} entries, chain {verdict}, root {root}",
        )
    )
    if sha256:
        add(("bundle", f"sha256 {sha256}"))

    return (
        "\n".join(
            f"{label:<{LABEL_WIDTH}}: {text}" if label else f"{'':<{LABEL_WIDTH}}  {text}"
            for label, text in lines
        )
        + "\n"
    )


def _duration(seconds: Any) -> str:
    if not seconds:
        return "-"
    s = int(seconds)
    if s % 3600 == 0:
        return f"{s // 3600}h"
    if s % 60 == 0:
        return f"{s // 60}m"
    return f"{s}s"


def _elapsed(start: str | None, end: str | None) -> str | None:
    if not start or not end:
        return None
    from datetime import datetime

    seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    return f"{seconds:.1f}s" if seconds >= 0 else None


def _replanned(bundle: dict[str, Any]) -> str:
    """Re-run the planner on the input it was given and prove the recorded plan reproduces.

    Offline and pure: the input is on the record, and planning reads nothing else. A divergence is
    not corruption -- the chain says whether anyone edited the record -- it means the planner has
    changed since the incident, which is exactly what a reviewer of a planner change needs to see.
    """
    # Ingestion writes a `plan_requested` of its own when it opens the incident; the one that
    # carries the planner's input is the orchestrator's.
    requested = next(
        (
            e
            for e in bundle.get("ledger") or []
            if e["event_type"] == "plan_requested" and "input" in (e.get("payload") or {})
        ),
        None,
    )
    recorded = next(
        (e for e in reversed(bundle.get("ledger") or []) if e["event_type"] == "plan"), None
    )
    raw_input = ((requested or {}).get("payload") or {}).get("input")
    if raw_input is None or recorded is None:
        return "not possible -- the planner input was not recorded"
    plan = planner.replan(IncidentBundle.from_dict(raw_input))
    if plan is None:
        return "DIVERGED -- no runbook answers this alert any more"
    expected = digest_of(recorded.get("payload") or {})
    actual = plan.digest()
    if actual == expected:
        return f"MATCH  recorded input reproduces the plan, digest {actual[:12]}"
    return f"DIVERGED  recorded {expected[:12]}, planner now produces {actual[:12]}"


def _entry(bundle: dict[str, Any], event_type: str, execution_id: int) -> dict[str, Any] | None:
    for entry in bundle.get("ledger") or []:
        if entry["event_type"] == event_type and entry.get("execution_id") == execution_id:
            payload: dict[str, Any] = entry.get("payload") or {}
            return payload
    return None

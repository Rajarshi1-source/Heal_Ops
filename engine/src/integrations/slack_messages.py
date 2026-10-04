"""Block Kit message builders -- and nothing else.

No Slack client, no network, no I/O. Every function here is a pure `dict` builder, which is the
whole reason this module is separate from `slack_client.py`: message shape can be unit-tested
without a Slack workspace, a token, or a mock HTTP layer.

Each message the engine sends has a builder here, including the ones that report **inaction**:
refusals, denials and shadow records. Those are first-class outcomes, so they get first-class cards
rather than being squeezed into an error format.

A note on tone, which is a design decision rather than a cosmetic one: escalation and refusal are
**not** styled as failures. A red "escalated" card would contradict the project's thesis -- the
system admitting "not mine" is a good outcome. `REVERTED` gets the same treatment: it is the system
catching its own mistake, and that is the number the project is proudest of.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from plan import Durability, RemediationPlan

if TYPE_CHECKING:
    from compensator import CompensationOutcome
    from policy import PolicyDecision
    from verifier import Verdict

Block = dict[str, Any]

#: Emoji by outcome. Deliberately not red for escalation, refusal or revert.
_ICON = {
    "remediated": ":white_check_mark:",
    "reverted": ":leftwards_arrow_with_hook:",
    "revert_failed": ":rotating_light:",
    "escalated": ":raising_hand:",
    "inconclusive": ":grey_question:",
    "refused": ":no_entry_sign:",
    "denied": ":lock:",
    "shadow": ":eyes:",
    "pending": ":hourglass_flowing_sand:",
    "promoted": ":arrow_up:",
    "demoted": ":arrow_down:",
    "masking": ":mag:",
}


def _section(text: str) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _header(text: str) -> Block:
    # Header text is plain_text and Slack truncates at 150 characters.
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": True}}


def _context(text: str) -> Block:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _fields(pairs: list[tuple[str, str]]) -> Block:
    # Slack renders at most 10 fields per section, in two columns.
    return {
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"*{label}*\n{value}"} for label, value in pairs[:10]
        ],
    }


def _plan_fields(plan: RemediationPlan) -> list[tuple[str, str]]:
    """The facts a human needs before approving, in the order they need them."""
    radius = plan.blast_radius
    durability = str(plan.durability)
    if plan.durability is Durability.EPHEMERAL and plan.ttl_seconds:
        durability += f" (expires in {plan.ttl_seconds // 60}m)"

    fields = [
        ("Target", f"`{plan.target_key}`"),
        ("Runbook", f"`{plan.runbook}`"),
        (
            "Blast radius",
            f"{radius.pods} pods / {radius.nodes} nodes"
            + (" / user-facing" if radius.user_facing else "")
            + f"\nscore {radius.score()}",
        ),
        ("Reversibility", str(plan.reversibility)),
        ("Durability", durability),
        ("Confidence", f"{plan.confidence:.0%}"),
    ]
    if plan.cost_delta_inr_month:
        fields.append(("Cost delta", f"₹{plan.cost_delta_inr_month:.0f}/month"))
    if plan.expected_signal:
        fields.append(
            (
                "Expected recovery",
                f"`{plan.expected_signal.alert_should_clear}` clears within "
                f"{plan.expected_signal.window_seconds // 60}m",
            )
        )
    return fields


def _actions_block(plan: RemediationPlan) -> Block:
    """The literal API calls. A reviewer approves what will happen, not a description of it."""
    lines = "\n".join(f"• `{action.describe()}`" for action in plan.actions)
    undo = "\n".join(f"• `{call.describe()}`" for call in plan.compensation)
    text = f"*Actions*\n{lines}"
    if undo:
        text += f"\n\n*Registered undo* (persisted before acting)\n{undo}"
    return _section(text)


# --- approval -------------------------------------------------------------------------------------


def approval_request(
    plan: RemediationPlan,
    execution_id: int,
    *,
    reasons: tuple[str, ...] = (),
    expires_at: str | None = None,
) -> list[Block]:
    """The card a human acts on.

    Carries the plan summary, blast radius, cost delta, expected recovery signal and the deadline.
    The deadline is rendered from the value the database supplies -- never computed here from a
    duration, because PostgreSQL owns expiry and a client-side clock would disagree with it.
    """
    blocks: list[Block] = [
        _header(f"{_ICON['pending']} Approval needed"),
        _section(f"*{plan.summary}*"),
        _fields(_plan_fields(plan)),
        _actions_block(plan),
    ]

    if reasons:
        # Why a human is being asked, in the policy's own words.
        blocks.append(
            _section("*Why this needs approval*\n" + "\n".join(f"• {r}" for r in reasons))
        )

    if expires_at:
        blocks.append(
            _context(
                f":alarm_clock: Expires at {expires_at}. On expiry this escalates — "
                "it never auto-executes."
            )
        )

    blocks.append(
        {
            "type": "actions",
            "block_id": f"healops_approval_{execution_id}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve", "emoji": True},
                    "action_id": "healops_approve",
                    "value": str(execution_id),
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject", "emoji": True},
                    "action_id": "healops_reject",
                    "value": str(execution_id),
                },
            ],
        }
    )
    return blocks


def approval_resolved(plan: RemediationPlan, *, approved: bool, decided_by: str) -> list[Block]:
    """Replaces the original card once decided, so the buttons cannot be clicked twice."""
    verdict = "Approved" if approved else "Rejected"
    icon = ":white_check_mark:" if approved else ":x:"
    return [
        _section(f"{icon} *{verdict}* by <@{decided_by}>\n{plan.summary}"),
        _context(f"`{plan.target_key}` · runbook `{plan.runbook}`"),
    ]


def approval_expired(plan: RemediationPlan) -> list[Block]:
    return [
        _section(
            f"{_ICON['escalated']} *Approval window expired* — escalating to on-call.\n"
            f"{plan.summary}"
        ),
        _context(
            "Nothing was executed. Silence is not consent, so HealOps never acts on a timeout."
        ),
    ]


# --- outcomes -------------------------------------------------------------------------------------


def verification_success(
    plan: RemediationPlan, verdict: Verdict, *, execution_id: int | None = None
) -> list[Block]:
    """Success, with the evidence.

    The verification line is the point of this card. "Fixed" on its own is what Rev 1 posted; this
    says *how it knows*. The feedback buttons ride along because this is the moment a human can
    still say "that was the wrong fix".
    """
    blocks = [
        _header(f"{_ICON['remediated']} Incident remediated"),
        _section(f"*{plan.summary}*"),
        _section("*Verified*\n" + "\n".join(f"• {reason}" for reason in verdict.reasons)),
        _fields(
            [
                ("Target", f"`{plan.target_key}`"),
                ("Runbook", f"`{plan.runbook}`"),
                (
                    "Alert cleared",
                    "yes" if verdict.alert_cleared else "not observed",
                ),
            ]
        ),
        _context("Success means the incident is over, not that the API call returned 200."),
    ]
    if execution_id is not None:
        blocks.append(feedback_prompt(execution_id))
    return blocks


def verification_reverted(
    plan: RemediationPlan, verdict: Verdict, compensation: CompensationOutcome
) -> list[Block]:
    """The card the project is proudest of: the system catching its own mistake.

    Framed as a correction rather than a failure, because that is what it is. `inconclusive` gets
    the same card with different words: a change that could not be shown to work is not left in
    production on trust, and the reader should know which of the two happened.
    """
    if verdict.outcome == "inconclusive":
        headline = (
            "The fix was applied, but recovery could not be confirmed — a signal it depends on was "
            "unavailable. A change that cannot be shown to work is not left in production, so the "
            "undo registered before acting has been executed."
        )
        why = "*Why recovery could not be confirmed*"
    else:
        headline = (
            "The fix was applied, verification found it had not worked, and the compensation "
            "registered before acting has been executed."
        )
        why = "*Why it failed*"

    return [
        _header(f"{_ICON['reverted']} Remediation reverted itself"),
        _section(f"*{plan.summary}*\n{headline}"),
        _section(why + "\n" + "\n".join(f"• {r}" for r in verdict.reasons)),
        _section(f"*Compensation*\n{compensation.detail}"),
        _fields(
            [
                ("Target", f"`{plan.target_key}`"),
                ("Runbook", f"`{plan.runbook}`"),
                ("Verdict", f"`{verdict.outcome}`"),
                ("Outcome", "reverted — escalating to a human"),
            ]
        ),
        _context(
            "cc <!subteam^oncall> · the cluster is back to its pre-remediation state"
            + _evidence(compensation)
        ),
    ]


def watchdog_reverted(plan: RemediationPlan, compensation: CompensationOutcome) -> list[Block]:
    """Verification never finished, so the dead-man's switch undid the change.

    Something upstream failed -- most likely the engine died mid-window -- so this pages rather
    than informs. The revert itself is the system working as designed.
    """
    return [
        _header(f"{_ICON['reverted']} Watchdog reverted an unverified change"),
        _section(
            f"*{plan.summary}*\nVerification never completed, so the change could not be shown to "
            "work. The undo registered before acting has been executed."
        ),
        _section(f"*Compensation*\n{compensation.detail}"),
        _fields([("Target", f"`{plan.target_key}`"), ("Runbook", f"`{plan.runbook}`")]),
        _context(
            "cc <!subteam^oncall> · the watchdog acting means the engine did not finish its own "
            "work — worth finding out why" + _evidence(compensation)
        ),
    ]


def execution_failed(plan: RemediationPlan, detail: str) -> list[Block]:
    """The action did not apply -- distinct from a fix that applied and did not work."""
    return [
        _header(f"{_ICON['escalated']} Remediation could not be applied"),
        _section(f"*{plan.summary}*"),
        _section(f"*Detail*\n{detail}"),
        _context(
            f"`{plan.target_key}` · nothing was verified because nothing was changed. Escalated."
        ),
    ]


def revert_failed(plan: RemediationPlan, compensation: CompensationOutcome) -> list[Block]:
    """The worst case: the fix failed AND the undo failed. Page immediately."""
    return [
        _header(f"{_ICON['revert_failed']} COMPENSATION FAILED"),
        _section(
            f"*{plan.summary}*\n"
            "The remediation did not work and the compensation also failed. The cluster is in a "
            "state neither the plan nor its undo describes."
        ),
        _section(f"*Compensation error*\n`{compensation.detail}`"),
        _fields([("Target", f"`{plan.target_key}`"), ("Runbook", f"`{plan.runbook}`")]),
        _context(
            "cc <!subteam^oncall> — manual intervention required. Nothing automated is left."
            + _evidence(compensation)
        ),
    ]


def _evidence(compensation: CompensationOutcome) -> str:
    if not compensation.ledger_hash:
        return ""
    return f" · ledger `{compensation.ledger_hash[:16]}`"


def refusal(plan: RemediationPlan) -> list[Block]:
    """A runbook declined. An outcome worth reporting, not an error to hide."""
    return [
        _header(f"{_ICON['refused']} Runbook refused"),
        _section(f"*{plan.runbook}* on `{plan.target_key}`"),
        _section(f"*Reason*\n{plan.refusal_reason}"),
        _context("Nothing was executed. Knowing what not to automate is a feature."),
    ]


def policy_denied(plan: RemediationPlan, decision: PolicyDecision) -> list[Block]:
    """Policy blocked it, with the rule id so the guardrail is traceable to its pull request."""
    rules = "\n".join(
        f"• `{rule}` — {message}"
        for rule, message in zip(decision.reasons, decision.messages, strict=False)
    )
    return [
        _header(f"{_ICON['denied']} Denied by policy"),
        _section(f"*{plan.summary}*"),
        _section(f"*Rules that fired*\n{rules}"),
        _context(
            f"`{plan.target_key}` · guardrails live in `policy/remediation.yaml`, so changing this "
            "is a pull request rather than a code change."
        ),
    ]


def shadow_notice(plan: RemediationPlan) -> list[Block]:
    """ "I would have done X." Visually distinct from anything that acted."""
    actions = "\n".join(f"• `{a.describe()}`" for a in plan.actions)
    return [
        _header(f"{_ICON['shadow']} SHADOW — no action taken"),
        _section(f"*I would have:* {plan.summary}"),
        _section(f"*Actions not executed*\n{actions}"),
        _fields(
            [
                ("Target", f"`{plan.target_key}`"),
                ("Blast radius", f"score {plan.blast_radius.score()}"),
                ("Confidence", f"{plan.confidence:.0%}"),
            ]
        ),
        _context(
            "This runbook has not earned authority in this namespace yet. Its plans are being "
            "compared against what humans actually did."
        ),
    ]


def expiry_notice(plan: RemediationPlan, *, pr_url: str | None = None) -> list[Block]:
    """The ephemeral mitigation's window closed and the workload is back to declared state."""
    text = (
        f"The ephemeral mitigation on `{plan.target_key}` expired; the workload is back to its "
        "declared state."
    )
    if pr_url:
        text += f"\nThe durable fix is {pr_url} (still open)."
    return [
        _section(f":hourglass: *Mitigation expired*\n{text}"),
        _context(
            "Automation that leaves permanent hand-edits in a cluster is worse than no automation. "
            "This is the mechanism that prevents it."
        ),
    ]


# --- feedback and authority -----------------------------------------------------------------------


def feedback_prompt(execution_id: int) -> Block:
    """ "Was this correct?" -- and the answer changes future authority.

    Rev 1 sent this button and logged the answer, which is a survey rather than a feedback loop. A
    `No` here demotes the runbook, which is why the button is worth having at all.
    """
    return {
        "type": "actions",
        "block_id": f"healops_feedback_{execution_id}",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Was this correct? 👍", "emoji": True},
                "action_id": "healops_feedback_yes",
                "value": str(execution_id),
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "No 👎", "emoji": True},
                "action_id": "healops_feedback_no",
                "value": str(execution_id),
            },
        ],
    }


def promotion_notice(runbook: str, namespace: str, *, to_mode: str, evidence: str) -> list[Block]:
    direction: Literal["promoted", "demoted"] = "promoted" if to_mode == "auto" else "demoted"
    return [
        _section(
            f"{_ICON[direction]} *`{runbook}` {direction} to `{to_mode}` in `{namespace}`*\n"
            f"{evidence}"
        ),
        _context(
            "Authority is per-namespace and earned on evidence. Promotion takes twenty good "
            "outcomes; demotion takes one bad one."
        ),
    ]


def masking_guard_warning(
    runbook: str, target: str, occurrences: int, issue_url: str | None
) -> list[Block]:
    """Automation that keeps papering over a defect files a bug against it."""
    text = (
        f"{_ICON['masking']} *`{runbook}` has now fixed `{target}` {occurrences} times "
        "this week.*\nThat is no longer an incident, it is a defect being hidden by automation. "
        "This runbook is demoted to approval mode for this namespace."
    )
    if issue_url:
        text += f"\nIssue filed: {issue_url}"
    return [
        _section(text),
        _context("Self-healing that hides a bug is a liability, not a feature."),
    ]


def fallback_text(blocks: list[Block]) -> str:
    """Plain-text fallback for notifications and screen readers.

    Slack requires it, and omitting it means push notifications arrive blank.
    """
    for block in blocks:
        if block.get("type") == "header":
            return str(block["text"]["text"])
    for block in blocks:
        if block.get("type") == "section" and "text" in block:
            return str(block["text"]["text"])[:200]
    return "HealOps notification"

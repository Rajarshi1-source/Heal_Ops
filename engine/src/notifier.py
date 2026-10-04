"""Which card each outcome gets, posted after the fact.

Every function here runs **after** the transaction that recorded the outcome has committed. The
ledger entry is the record; the Slack message is a notification about it. Posting first would let a
Slack outage roll back a decision, or announce one that then failed to commit.

Nothing here raises. `slack_client` already swallows transport failures; this module additionally
guards its own logic, because a malformed card must not take down the worker loop that called it.
When Slack is unconfigured every function is a quiet no-op, and approvals are held rather than
executed -- which falls out of `can_execute()` failing closed, not out of anything here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from cache import approval_store
from integrations import slack_client, slack_messages
from integrations.slack_client import PostedMessage
from plan import RemediationPlan

if TYPE_CHECKING:
    from compensator import CompensationOutcome
    from orchestrator import HandleResult
    from promotion import Transition
    from recurrence import MaskingEvent
    from verifier import Verdict

log = structlog.get_logger(__name__)


async def orchestrated(
    result: HandleResult, *, expires_at: str | None = None
) -> PostedMessage | None:
    """The card for what the orchestrator decided. Returns the approval card's handle, if one."""
    plan = result.plan
    try:
        match result.outcome:
            case "refused":
                await slack_client.post(slack_messages.refusal(plan))
            case "denied" if result.decision is not None:
                await slack_client.post(slack_messages.policy_denied(plan, result.decision))
            case "shadow":
                await slack_client.post(
                    slack_messages.shadow_notice(plan, execution_id=result.execution_id)
                )
            case "awaiting_approval":
                return await _approval_card(result, expires_at)
            case "execute_failed" | "blocked":
                await slack_client.post(slack_messages.execution_failed(plan, result.detail))
            case _:
                # "executed" is deliberately silent. Acting is not news; the verdict is, and it
                # follows within the verification window.
                pass
    except Exception as exc:  # noqa: BLE001
        log.warning("notification failed", outcome=result.outcome, error=str(exc))
    return None


async def _approval_card(result: HandleResult, expires_at: str | None) -> PostedMessage | None:
    if result.execution_id is None:
        return None
    try:
        first = await approval_store.mark_notified(result.execution_id)
    except Exception:  # noqa: BLE001
        # Cannot tell whether the card was already posted. A duplicate card is harmless -- the
        # decision is idempotent -- whereas no card means nobody is ever asked.
        first = True
    if not first:
        return None

    reasons = result.decision.messages if result.decision else ()
    return await slack_client.post(
        slack_messages.approval_request(
            result.plan, result.execution_id, reasons=reasons, expires_at=expires_at
        )
    )


async def verified(
    plan: RemediationPlan,
    verdict: Verdict,
    outcome: CompensationOutcome,
    *,
    execution_id: int,
) -> None:
    """Success, self-correction, or the case where even the undo failed."""
    try:
        if outcome.failed_to_revert:
            blocks = slack_messages.revert_failed(plan, outcome)
        elif outcome.reverted:
            blocks = slack_messages.verification_reverted(plan, verdict, outcome)
        else:
            blocks = slack_messages.verification_success(plan, verdict, execution_id=execution_id)
        await slack_client.post(blocks)
    except Exception as exc:  # noqa: BLE001
        log.warning("verification notification failed", error=str(exc))


async def watchdog_acted(
    plan: RemediationPlan, outcome: CompensationOutcome, *, pr_url: str | None = None
) -> None:
    try:
        if outcome.failed_to_revert:
            blocks = slack_messages.revert_failed(plan, outcome)
        elif outcome.trigger == "expiry":
            blocks = slack_messages.expiry_notice(plan, pr_url=pr_url)
        else:
            blocks = slack_messages.watchdog_reverted(plan, outcome)
        await slack_client.post(blocks)
    except Exception as exc:  # noqa: BLE001
        log.warning("watchdog notification failed", error=str(exc))


async def approval_expired(
    plan: RemediationPlan, *, channel: str | None, message_ts: str | None
) -> None:
    """Retire the approval card in place, or post the expiry if the card was never posted."""
    try:
        blocks = slack_messages.approval_expired(plan)
        if channel and message_ts:
            await slack_client.update(PostedMessage(channel=channel, ts=message_ts), blocks)
        else:
            await slack_client.post(blocks)
    except Exception as exc:  # noqa: BLE001
        log.warning("approval expiry notification failed", error=str(exc))


async def masking(event: MaskingEvent, issue_url: str | None) -> None:
    """The anti-masking guard fired: the runbook was demoted and a bug was filed against it."""
    try:
        await slack_client.post(
            slack_messages.masking_guard_warning(
                event.runbook, event.target_key, event.occurrences, issue_url
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("masking notification failed", error=str(exc))


async def promoted(transition: Transition) -> None:
    """A rung changed: authority granted on evidence, or withdrawn on one strike."""
    try:
        await slack_client.post(
            slack_messages.promotion_notice(
                transition.runbook,
                transition.namespace,
                to_mode=transition.to_mode,
                evidence=transition.reason,
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("promotion notification failed", error=str(exc))


async def durable_fix(plan: RemediationPlan, url: str | None, reason: str) -> None:
    try:
        await slack_client.post(slack_messages.durable_fix(plan, pr_url=url, reason=reason))
    except Exception as exc:  # noqa: BLE001
        log.warning("durable-fix notification failed", error=str(exc))

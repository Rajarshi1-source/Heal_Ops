"""The Slack Bolt `AsyncApp`, mounted on the FastAPI app.

**One web server, one port.** Bolt can run its own server; doing so would mean two Services, two
ingress rules, two readiness probes and two ports to reason about, for one process. The FastAPI
adapter avoids all of that.

The handlers here are deliberately thin. They verify the request (Bolt does the signature check),
translate the interaction into a call on `approval_manager` or `promotion`, and update the original
message. No decision logic lives in this file: a Slack payload is an *input*, not an authority, and
the record of who decided what belongs in PostgreSQL and the ledger rather than in a callback.

`ack()` is called first in every handler because Slack requires a response within three seconds.
Doing the database work before acknowledging would make a slow query look like a broken button.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from db.connection import transaction
from db.models import RunbookExecution
from plan import RemediationPlan

log = structlog.get_logger(__name__)

APPROVE_ACTION = "healops_approve"
REJECT_ACTION = "healops_reject"
FEEDBACK_YES = "healops_feedback_yes"
FEEDBACK_NO = "healops_feedback_no"


def build_app() -> Any:
    """Construct the Bolt `AsyncApp` and register handlers."""
    from slack_bolt.async_app import AsyncApp

    settings = get_settings()
    token, secret = settings.slack_bot_token, settings.slack_signing_secret
    if token is None or secret is None:
        # Without the signing secret Bolt cannot verify a request, and an unverified button click
        # must never reach the approval path.
        raise RuntimeError("slack is not configured: needs both the bot token and signing secret")

    bolt = AsyncApp(
        token=token.get_secret_value(),
        signing_secret=secret.get_secret_value(),
        # The adapter owns the HTTP layer, so Bolt must not try to start its own.
        process_before_response=True,
    )

    @bolt.action(APPROVE_ACTION)
    async def handle_approve(ack: Any, body: dict[str, Any], respond: Any) -> None:
        await ack()
        await _record_decision(body, respond, approved=True)

    @bolt.action(REJECT_ACTION)
    async def handle_reject(ack: Any, body: dict[str, Any], respond: Any) -> None:
        await ack()
        await _record_decision(body, respond, approved=False)

    @bolt.action(FEEDBACK_YES)
    async def handle_feedback_yes(ack: Any, body: dict[str, Any], respond: Any) -> None:
        await ack()
        await _record_feedback(body, respond, correct=True)

    @bolt.action(FEEDBACK_NO)
    async def handle_feedback_no(ack: Any, body: dict[str, Any], respond: Any) -> None:
        await ack()
        await _record_feedback(body, respond, correct=False)

    return bolt


async def _record_decision(body: dict[str, Any], respond: Any, *, approved: bool) -> None:
    """Translate a button click into a recorded decision."""
    from cache import streams
    from core import approval_manager
    from integrations import slack_messages

    execution_id = _execution_id(body)
    user = _user_id(body)
    if execution_id is None or user is None:
        log.warning("malformed slack interaction", body_keys=sorted(body))
        return

    async with transaction() as session:
        execution = await session.get(RunbookExecution, execution_id, with_for_update=True)
        if execution is None:
            await respond(text=f"No execution {execution_id} — it may have been cleaned up.")
            return

        resolution = await approval_manager.resolve(
            session,
            execution,
            approved=approved,
            decided_by=user,
            slack_message_ts=_message_ts(body),
            slack_channel=_channel_id(body),
        )
        plan = await _plan_for(session, execution)
        incident_id = execution.incident_id

    if resolution.enqueue:
        # Enqueue rather than execute inline: the click has already been acknowledged, and a
        # Kubernetes call in this handler would block the Slack response path.
        await streams.enqueue(
            "execute_approved", {"execution_id": execution_id, "incident_id": incident_id}
        )

    authorisation = resolution.authorisation
    if plan is not None and resolution.execution_status in ("queued", approval_manager.REJECTED):
        # Replace the card so the buttons cannot be clicked again on a settled question.
        await respond(
            blocks=slack_messages.approval_resolved(
                plan, approved=authorisation.allowed, decided_by=authorisation.decided_by or user
            ),
            replace_original=True,
        )
    else:
        await respond(text=authorisation.reason, replace_original=False)

    log.info(
        "slack approval handled",
        execution_id=execution_id,
        approved=approved,
        decided_by=user,
        allowed=authorisation.allowed,
        enqueued=resolution.enqueue,
    )


async def _record_feedback(body: dict[str, Any], respond: Any, *, correct: bool) -> None:
    """ "Was this correct?" -- and a `No` must actually change future authority.

    Logging the answer alone would make the button theatre. The real behaviour change lives in
    `promotion.on_outcome`, which is wired in Phase 6; until then the answer is recorded in the
    ledger so no feedback is lost in the meantime.
    """
    import ledger

    execution_id = _execution_id(body)
    user = _user_id(body)
    if execution_id is None or user is None:
        return

    async with transaction() as session:
        execution = await session.get(RunbookExecution, execution_id)
        if execution is None:
            return
        await ledger.append(
            session,
            "verify",
            {
                "stage": "human_feedback",
                "execution_id": execution_id,
                "was_correct": correct,
                "answered_by": user,
            },
            actor=user,
            incident_id=execution.incident_id,
            execution_id=execution_id,
        )

    await respond(
        text=(
            "Thanks — recorded. "
            + (
                "This runbook keeps its current authority."
                if correct
                else "This will demote the runbook for this namespace."
            )
        ),
        replace_original=False,
    )


async def _plan_for(session: AsyncSession, execution: RunbookExecution) -> RemediationPlan | None:
    """Rehydrate the plan so the resolved card can restate what was approved."""
    if execution.plan_id is None:
        return None

    import plan_store
    from db.models import RemediationPlanRow

    row = await session.get(RemediationPlanRow, execution.plan_id)
    return plan_store.rehydrate(row) if row is not None else None


def _execution_id(body: dict[str, Any]) -> int | None:
    actions = body.get("actions") or []
    if not actions:
        return None
    try:
        return int(actions[0].get("value"))
    except TypeError, ValueError:
        return None


def _user_id(body: dict[str, Any]) -> str | None:
    user = body.get("user") or {}
    return user.get("id")


def _channel_id(body: dict[str, Any]) -> str | None:
    channel = body.get("channel") or {}
    return channel.get("id")


def _message_ts(body: dict[str, Any]) -> str | None:
    message = body.get("message") or {}
    return message.get("ts")


def mount_slack(app: FastAPI) -> None:
    """Attach Bolt's request handler to the FastAPI app.

    Called from `main.py` only when Slack is configured, so the engine starts and runs with no Slack
    credentials at all -- approvals are then held rather than executed.
    """
    from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

    bolt = build_app()
    handler = AsyncSlackRequestHandler(bolt)

    @app.post("/slack/events", include_in_schema=False)
    async def slack_events(request: Request) -> Any:
        # Bolt verifies the signing secret here; the engine never trusts an unverified payload.
        return await handler.handle(request)

    @app.post("/slack/interactivity", include_in_schema=False)
    async def slack_interactivity(request: Request) -> Any:
        return await handler.handle(request)

    log.info("slack adapter mounted", routes=["/slack/events", "/slack/interactivity"])

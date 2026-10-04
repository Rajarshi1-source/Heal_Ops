"""Slack transport.

Everything here is best-effort by design. **Slack being down must never stop the engine from
recording what happened**, and it must never cause an action to execute that would otherwise have
waited for approval. So:

* every send returns `None` on failure instead of raising;
* a failed post is logged and counted, and the ledger entry for the underlying event has already
  landed in PostgreSQL regardless;
* when Slack is unavailable, actions requiring approval are **held, not executed**, which falls out
  of `can_execute()` failing closed rather than needing special handling here.

The one thing this module will not do is swallow a failure silently. `healops_dependency_up` carries
Slack's state so an operator can see that notifications are missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

import metrics
from config import get_settings
from integrations.slack_messages import Block, fallback_text

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PostedMessage:
    """Identifies a posted message, so it can be updated in place later.

    The approval card is updated rather than re-posted once decided: leaving a live "Approve" button
    under a decision already made invites a second click on a closed question.
    """

    channel: str
    ts: str


_client: Any | None = None


def _get_client() -> Any | None:
    global _client  # noqa: PLW0603
    settings = get_settings()
    if not settings.slack_enabled:
        return None
    if _client is None:
        from slack_sdk.web.async_client import AsyncWebClient

        token = settings.slack_bot_token
        if token is None:  # unreachable while slack_enabled holds; stated rather than asserted
            return None
        _client = AsyncWebClient(token=token.get_secret_value())
    return _client


def reset_client() -> None:
    """For tests, and for a credential rotation without a restart."""
    global _client  # noqa: PLW0603
    _client = None


async def post(
    blocks: list[Block], *, channel: str | None = None, thread_ts: str | None = None
) -> PostedMessage | None:
    """Post a message. Returns None when Slack is unconfigured or unreachable."""
    settings = get_settings()
    client = _get_client()
    if client is None:
        # Not an error. The engine runs fine with no Slack, and saying so once per message would
        # drown the logs during a demo.
        log.debug("slack not configured; skipping post")
        return None

    target = channel or settings.slack_channel
    try:
        response = await client.chat_postMessage(
            channel=target,
            blocks=blocks,
            text=fallback_text(blocks),
            thread_ts=thread_ts,
            unfurl_links=False,
        )
    except Exception as exc:  # noqa: BLE001
        # Deliberately broad: a Slack outage must not propagate into the remediation path.
        metrics.dependency_up.labels(dependency="slack").set(0)
        log.warning("slack post failed", channel=target, error=str(exc))
        return None

    metrics.dependency_up.labels(dependency="slack").set(1)
    return PostedMessage(channel=str(response["channel"]), ts=str(response["ts"]))


async def update(message: PostedMessage, blocks: list[Block]) -> bool:
    """Replace a posted message in place.

    Used to retire the approval buttons once a decision is recorded, so the card reflects the
    decision rather than still offering it.
    """
    client = _get_client()
    if client is None:
        return False

    try:
        await client.chat_update(
            channel=message.channel,
            ts=message.ts,
            blocks=blocks,
            text=fallback_text(blocks),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("slack update failed", ts=message.ts, error=str(exc))
        return False
    return True


async def ping() -> bool:
    """Reachability, for the readiness probe's `degraded` list.

    Slack is never a *required* dependency, so this result never makes the pod unready.
    """
    client = _get_client()
    if client is None:
        return False
    try:
        await client.auth_test()
    except Exception:  # noqa: BLE001 - readiness must report, never raise
        metrics.dependency_up.labels(dependency="slack").set(0)
        return False
    metrics.dependency_up.labels(dependency="slack").set(1)
    return True

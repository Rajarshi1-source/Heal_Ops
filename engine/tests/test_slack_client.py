"""Slack transport: best-effort, never raising, never silent.

A Slack outage must not reach the remediation path, and an unconfigured Slack is a supported mode,
not an error. Both are asserted here with a fake web client, so no workspace is needed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import SecretStr

from config import get_settings
from integrations import slack_client
from integrations.slack_client import PostedMessage


class FakeWebClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:  # noqa: N802
        if self.fail:
            raise ConnectionError("slack is down")
        self.posted.append(kwargs)
        return {"channel": "C1", "ts": "1700.1"}

    async def chat_update(self, **kwargs: Any) -> None:
        if self.fail:
            raise ConnectionError("slack is down")
        self.updated.append(kwargs)

    async def auth_test(self) -> None:
        if self.fail:
            raise ConnectionError("slack is down")


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    settings = get_settings()
    monkeypatch.setattr(settings, "slack_bot_token", SecretStr("xoxb-test"))
    monkeypatch.setattr(settings, "slack_signing_secret", SecretStr("signing"))
    slack_client.reset_client()
    yield
    slack_client.reset_client()


BLOCKS = [{"type": "header", "text": {"type": "plain_text", "text": "hello"}}]


async def test_unconfigured_slack_is_a_quiet_no_op() -> None:
    slack_client.reset_client()
    assert await slack_client.post(BLOCKS) is None
    assert await slack_client.update(PostedMessage("C1", "1"), BLOCKS) is False
    assert await slack_client.ping() is False


async def test_post_returns_a_handle_for_later_updates(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeWebClient()
    monkeypatch.setattr(slack_client, "_client", fake)

    posted = await slack_client.post(BLOCKS)

    assert posted == PostedMessage(channel="C1", ts="1700.1")
    assert fake.posted[0]["text"] == "hello", "fallback text, or notifications arrive blank"
    assert fake.posted[0]["channel"] == get_settings().slack_channel


async def test_a_slack_outage_returns_none_instead_of_raising(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(slack_client, "_client", FakeWebClient(fail=True))

    assert await slack_client.post(BLOCKS) is None
    assert await slack_client.update(PostedMessage("C1", "1"), BLOCKS) is False
    assert await slack_client.ping() is False


async def test_update_and_ping_succeed_when_slack_is_up(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeWebClient()
    monkeypatch.setattr(slack_client, "_client", fake)

    assert await slack_client.update(PostedMessage("C1", "1"), BLOCKS) is True
    assert fake.updated[0]["ts"] == "1"
    assert await slack_client.ping() is True


def test_the_real_client_is_built_lazily_from_the_token(configured: None) -> None:
    client = slack_client._get_client()
    assert client is not None
    assert slack_client._get_client() is client, "one client per process"

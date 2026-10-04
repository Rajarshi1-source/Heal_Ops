"""`healopsctl freeze` / `unfreeze` / `breaker-status`: one command turns all automation off.

The breaker transitions themselves are tested against PostgreSQL in `test_budget_and_breaker.py`;
here the subject is the command line -- that it passes the reason and owner through, and says what
it did.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from typer.testing import CliRunner

import breaker
from breaker import BreakerView
from cli.healopsctl import main

runner = CliRunner()


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    recorded: list[tuple[str, dict[str, Any]]] = []

    async def freeze(_session: Any, **kwargs: Any) -> None:
        recorded.append(("freeze", kwargs))

    async def unfreeze(_session: Any, **kwargs: Any) -> None:
        recorded.append(("unfreeze", kwargs))

    async def view(_session: Any, **_kwargs: Any) -> BreakerView:
        return BreakerView(state="open", reason="release in progress", owner="alice")

    monkeypatch.setattr(breaker, "freeze", freeze)
    monkeypatch.setattr(breaker, "unfreeze", unfreeze)
    monkeypatch.setattr(breaker, "view", view)
    monkeypatch.setattr(main, "_with_session", lambda operation: asyncio.run(operation(None)))
    return recorded


def test_freeze_records_the_reason_and_the_owner(calls: list[Any]) -> None:
    result = runner.invoke(
        main.app, ["freeze", "--reason", "release in progress", "--owner", "alice"]
    )

    assert result.exit_code == 0, result.output
    assert calls == [("freeze", {"reason": "release in progress", "owner": "alice"})]
    assert "FROZEN" in result.output and "unfreeze" in result.output


def test_freeze_requires_a_reason(calls: list[Any]) -> None:
    """A freeze with no reason is a mystery to whoever finds automation off at 3 a.m."""
    assert runner.invoke(main.app, ["freeze"]).exit_code != 0
    assert calls == []


def test_unfreeze_defaults_the_owner_to_the_login(calls: list[Any]) -> None:
    result = runner.invoke(main.app, ["unfreeze"])
    assert result.exit_code == 0
    assert calls[0][0] == "unfreeze" and calls[0][1]["owner"]


def test_breaker_status_shows_why_and_who(calls: list[Any]) -> None:
    result = runner.invoke(main.app, ["breaker-status"])
    assert result.exit_code == 0
    assert "open" in result.output and "release in progress" in result.output
    assert "alice" in result.output

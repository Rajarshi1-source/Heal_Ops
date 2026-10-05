"""Replay is a golden file; audit is an exit code. Both are contracts other things depend on.

* `healopsctl replay` must print, byte for byte, what it printed the day the incident was recorded:
  the dashboard renders it as a page for the public demo and the evidence in `evidence/replay/` is
  its golden copy. A change to the format is a reviewed change to both, never a cosmetic tweak.
* `healopsctl audit` exits 0 on an intact record and 4 on any break, naming where. CI and the
  tamper demo both read the exit code, not the colour of the text.

The bundles are the real ones committed in `evidence/` -- harvested from incidents the chaos suite
caused on a kind cluster -- so this is a test against recorded reality, not a fixture of it.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import evidence
import ledger
import replay
from cli.healopsctl import main

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = REPO_ROOT / "evidence"
GOLDEN = EVIDENCE / "replay"
BUNDLES = sorted(EVIDENCE.glob("*.json"))

runner = CliRunner()


def test_there_is_committed_evidence_to_replay() -> None:
    assert BUNDLES, "evidence/ is empty: harvest it from a chaos run (capture_corpus.py harvest)"


@pytest.mark.parametrize("path", BUNDLES, ids=lambda p: p.stem[:12])
def test_replay_matches_its_golden_file_byte_for_byte(path: Path) -> None:
    digest, bundle = evidence.load(str(path), EVIDENCE)
    golden = (GOLDEN / f"{digest}.txt").read_text(encoding="utf-8")

    assert replay.render(bundle, digest) == golden
    # And again: no clock, no locale, no ordering may leak in between two runs.
    assert replay.render(bundle, digest) == golden


@pytest.mark.parametrize("path", BUNDLES, ids=lambda p: p.stem[:12])
def test_every_committed_bundle_still_reproduces_its_own_plan(path: Path) -> None:
    """The recorded planner input, re-planned today, gives the recorded plan -- or the planner has
    changed since, and that change must be reviewed alongside new golden files."""
    digest, bundle = evidence.load(str(path), EVIDENCE)
    line = next(line for line in replay.render(bundle, digest).splitlines() if "replanned" in line)
    assert "DIVERGED" not in line, line


def test_the_cli_prints_the_golden_file_and_exits_zero() -> None:
    digest, _ = evidence.load(str(BUNDLES[0]), EVIDENCE)
    result = runner.invoke(main.app, ["replay", digest[:16], "--dir", str(EVIDENCE)])

    assert result.exit_code == main.EXIT_OK, result.output
    assert result.stdout == (GOLDEN / f"{digest}.txt").read_text(encoding="utf-8")


# --- tampering -----------------------------------------------------------------------------------


def _copy(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    source = BUNDLES[0]
    target = tmp_path / source.name
    shutil.copy(source, target)
    return target, json.loads(target.read_text(encoding="utf-8"))


def _edit_an_entry(bundle: dict[str, Any]) -> int:
    """The tamper demo: change one recorded fact in one ledger entry, the way a superuser could."""
    entry = bundle["ledger"][len(bundle["ledger"]) // 2]
    entry["payload"] = {**(entry.get("payload") or {}), "tampered": True}
    return int(entry["seq"])


def test_an_edited_bundle_no_longer_matches_its_address(tmp_path: Path) -> None:
    path, bundle = _copy(tmp_path)
    _edit_an_entry(bundle)
    path.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")

    for command in (["audit", "--bundle", path.stem], ["replay", path.stem]):
        result = runner.invoke(main.app, [*command, "--dir", str(tmp_path)])
        assert result.exit_code == main.EXIT_ERROR, command
        # Rich wraps at the terminal width; the words matter, not where the line breaks.
        assert "does not match its own address" in " ".join(result.output.split())


def test_a_re_addressed_forgery_is_caught_by_the_chain_and_named(tmp_path: Path) -> None:
    """Renaming the edited file to its new hash beats content addressing -- and not the chain."""
    path, bundle = _copy(tmp_path)
    seq = _edit_an_entry(bundle)
    path.unlink()
    forged = evidence.write(bundle, tmp_path)

    audit = runner.invoke(main.app, ["audit", "--bundle", forged.stem, "--dir", str(tmp_path)])
    assert audit.exit_code == main.EXIT_ERROR
    assert f"BROKEN at seq={seq}" in audit.output

    replayed = runner.invoke(main.app, ["replay", forged.stem, "--dir", str(tmp_path)])
    assert replayed.exit_code == main.EXIT_ERROR
    assert "BROKEN" in replayed.stdout  # the replay itself says so, on the ledger line


def test_an_intact_bundle_audits_clean() -> None:
    result = runner.invoke(main.app, ["audit", "--bundle", BUNDLES[0].stem, "--dir", str(EVIDENCE)])
    assert result.exit_code == main.EXIT_OK, result.output
    assert "chain VALID" in result.output


# --- the live chain ------------------------------------------------------------------------------


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch) -> list[ledger.ChainStatus]:
    """What `verify_chain` will report; the walk itself is tested against PostgreSQL elsewhere."""
    status: list[ledger.ChainStatus] = []

    async def verify_chain(_session: Any) -> ledger.ChainStatus:
        return status[0]

    monkeypatch.setattr(ledger, "verify_chain", verify_chain)
    monkeypatch.setattr(main, "_with_session", lambda operation: asyncio.run(operation(None)))
    return status


def test_verify_chain_exits_zero_on_an_intact_chain(chain: list[ledger.ChainStatus]) -> None:
    chain.append(ledger.ChainStatus(valid=True, entries=812))
    result = runner.invoke(main.app, ["audit", "--verify-chain"])

    assert result.exit_code == main.EXIT_OK
    assert "812 entries, chain VALID" in result.output


def test_verify_chain_exits_four_and_names_the_sequence(chain: list[ledger.ChainStatus]) -> None:
    chain.append(
        ledger.ChainStatus(valid=False, entries=23, broken_at_seq=23, detail="hash mismatch")
    )
    result = runner.invoke(main.app, ["audit", "--verify-chain"])

    assert result.exit_code == main.EXIT_ERROR
    assert "BROKEN at seq=23" in result.output


def test_audit_with_nothing_to_audit_is_an_error_not_a_pass() -> None:
    result = runner.invoke(main.app, ["audit"])
    assert result.exit_code == main.EXIT_ERROR

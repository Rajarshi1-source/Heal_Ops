"""`healopsctl plan`.

Two things are under test. The **exit codes** are a contract -- CI and shell scripts branch on them
to assert that HealOps declines what it should, so a refusal exiting 0 would silently break that.

And the whole command runs **offline**: no cluster, no database, no network. That is not a
convenience, it is the demonstration that `plan()` is a pure function of the bundle -- the same
property the CI decision gate and the determinism digest depend on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli.healopsctl.main import EXIT_DENIED, EXIT_ERROR, EXIT_OK, EXIT_REFUSED, app

CORPUS = Path(__file__).resolve().parents[2] / "corpus"
runner = CliRunner()


def run(*args: str) -> object:
    return runner.invoke(app, ["plan", *args])


# --- the allow path ------------------------------------------------------------------------------


def test_allowed_plan_exits_zero() -> None:
    result = run(str(CORPUS / "crashloop-allow.json"))
    assert result.exit_code == EXIT_OK, result.output


def test_allowed_plan_shows_the_action_and_the_undo() -> None:
    """A reviewer should see the literal patch and how it would be reversed."""
    output = run(str(CORPUS / "crashloop-allow.json")).output

    assert "ALLOW" in output
    assert "rollout_undo" in output
    assert "undo" in output
    assert "nothing was executed" in output


def test_allowed_plan_shows_the_safety_metadata() -> None:
    output = run(str(CORPUS / "crashloop-allow.json")).output

    assert "blast radius" in output
    assert "reversible" in output
    # An ephemeral fix must advertise that it expires; a reader should not have to infer it.
    assert "ephemeral" in output
    assert "expires in" in output
    assert "confidence" in output


def test_allowed_plan_shows_the_expected_recovery_signal() -> None:
    # Committing to the recovery criterion up front is what stops verification hunting for any
    # metric that happens to look better afterwards.
    output = run(str(CORPUS / "crashloop-allow.json")).output
    assert "alert should clear" in output
    assert "KubePodCrashLooping" in output


def test_allowed_plan_shows_the_determinism_digest() -> None:
    output = run(str(CORPUS / "crashloop-allow.json")).output
    assert "determinism digest" in output


# --- the refusal path ----------------------------------------------------------------------------


def test_runbook_refusal_exits_two() -> None:
    """A refusal is a distinct outcome, not a success and not a crash."""
    result = run(str(CORPUS / "refuse-statefulset.json"))
    assert result.exit_code == EXIT_REFUSED


def test_refusal_states_its_reason() -> None:
    output = run(str(CORPUS / "refuse-statefulset.json")).output

    assert "REFUSED" in output
    assert "not rollable" in output
    # The framing matters: a refusal is an outcome the scorecard counts.
    assert "first-class outcome" in output


def test_refusal_produces_no_actions() -> None:
    result = runner.invoke(app, ["plan", str(CORPUS / "refuse-statefulset.json"), "--json"])
    payload = json.loads(result.output)
    assert payload["plan"]["actions"] == []
    assert payload["plan"]["refusal_reason"]


# --- the policy denial path ----------------------------------------------------------------------


def test_policy_denial_exits_three() -> None:
    """Distinct from a refusal: the runbook was willing, the policy was not."""
    result = run(str(CORPUS / "refuse-opt-out.json"))
    assert result.exit_code == EXIT_DENIED


def test_denial_names_the_rule_and_shows_its_message() -> None:
    output = run(str(CORPUS / "refuse-opt-out.json")).output

    assert "DENY" in output.upper()
    assert "deny-kill-switch" in output
    # The message is used verbatim in Slack and the ledger, so it has to be legible here too.
    assert "opted it out" in output


def test_denial_still_shows_the_plan_it_would_have_run() -> None:
    """Seeing what was blocked is the point of showing a denial rather than just refusing."""
    output = run(str(CORPUS / "refuse-opt-out.json")).output
    assert "rollout_undo" in output
    assert "nothing was executed" in output


# --- the promotion ladder ------------------------------------------------------------------------


def test_shadow_promotion_requires_approval() -> None:
    """The ladder is enforcement, not bookkeeping.

    The same bundle that is auto-approved at `auto` needs a human at `shadow`.
    """
    output = run(str(CORPUS / "crashloop-allow.json"), "--promotion", "shadow").output
    assert "REQUIRE_APPROVAL" in output
    assert "approve-not-promoted" in output


def test_auto_promotion_allows_the_same_bundle() -> None:
    output = run(str(CORPUS / "crashloop-allow.json"), "--promotion", "auto").output
    assert "ALLOW" in output


# --- machine-readable output ---------------------------------------------------------------------


def test_json_output_carries_the_plan_and_the_decision() -> None:
    result = runner.invoke(app, ["plan", str(CORPUS / "crashloop-allow.json"), "--json"])
    payload = json.loads(result.output)

    assert payload["plan"]["runbook"] == "crashloop_rollback"
    assert payload["plan"]["blast_radius_score"] == 3
    assert payload["policy"]["decision"] == "allow"


def test_json_output_is_reusable_as_evidence() -> None:
    # The same canonical shape the ledger and the plans table store, so a CLI dump and a database
    # row describe the incident identically.
    result = runner.invoke(app, ["plan", str(CORPUS / "crashloop-allow.json"), "--json"])
    plan = json.loads(result.output)["plan"]

    for field in (
        "runbook",
        "actions",
        "compensation",
        "reversibility",
        "durability",
        "confidence",
    ):
        assert field in plan


# --- error handling ------------------------------------------------------------------------------


def test_missing_bundle_exits_with_an_error(tmp_path: Path) -> None:
    result = run(str(tmp_path / "nope.json"))
    assert result.exit_code == EXIT_ERROR
    assert "no such bundle" in result.output


def test_unreadable_bundle_exits_with_an_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = run(str(bad))
    assert result.exit_code == EXIT_ERROR


def test_unknown_alert_escalates_rather_than_guessing(tmp_path: Path) -> None:
    """An alert no runbook answers must escalate, not be force-fitted to the nearest runbook."""
    bundle = json.loads((CORPUS / "crashloop-allow.json").read_text(encoding="utf-8"))
    bundle["cause_alert"]["alertname"] = "SomethingNobodyPlannedFor"
    path = tmp_path / "unknown.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = run(str(path))
    assert result.exit_code == EXIT_REFUSED
    assert "no runbook answers" in result.output
    assert "escalate" in result.output


# --- the corpus itself ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["crashloop-allow", "refuse-statefulset", "refuse-opt-out"])
def test_committed_bundles_are_loadable_and_labelled(name: str) -> None:
    """Every committed bundle must be labelled, or the decision eval cannot grade it."""
    from bundle import IncidentBundle

    bundle = IncidentBundle.from_json((CORPUS / f"{name}.json").read_text(encoding="utf-8"))
    assert bundle.is_labelled, f"{name} has no expected_runbook/expected_effect"
    assert bundle.label_note, f"{name} has no note explaining why"


def test_the_corpus_contains_must_refuse_cases() -> None:
    """A remediation engine that never refuses is a liability.

    Refusal recall is graded at 100% rather than averaged, so the corpus has to actually contain
    cases that must be declined.
    """
    from bundle import IncidentBundle

    effects = [
        IncidentBundle.from_json(p.read_text(encoding="utf-8")).expected_effect
        for p in CORPUS.glob("*.json")
    ]
    assert effects.count("deny") + effects.count("refuse") >= 2


def test_planning_is_deterministic_across_runs() -> None:
    """Same bundle in, byte-identical plan out. This is the determinism gate's anchor."""
    first = runner.invoke(app, ["plan", str(CORPUS / "crashloop-allow.json"), "--json"])
    second = runner.invoke(app, ["plan", str(CORPUS / "crashloop-allow.json"), "--json"])

    assert json.loads(first.output)["plan"] == json.loads(second.output)["plan"]

"""The decision gate's own logic: grading, calibration arithmetic, and per-case policy context.

A gate nobody tests is a gate that passes by accident. These are hermetic: an inline bundle, no
corpus, no cluster.
"""

from __future__ import annotations

import json
from pathlib import Path

from bundle import BundleAlert, ContainerState, IncidentBundle, Target
from evals import decision_eval as de


def _crashloop_bundle(**labels: str) -> IncidentBundle:
    target = Target(
        kind="Deployment",
        name="api",
        namespace="shop",
        replicas=3,
        containers=(ContainerState(name="api", image="api:v2", restart_count=7),),
        revision=4,
        previous_revision=3,
        previous_image="api:v1",
        template_digest="a" * 64,
        previous_template_digest="b" * 64,
    )
    return IncidentBundle(
        incident_id="eval-crashloop",
        cause_alert=BundleAlert(alertname="KubePodCrashLooping", fingerprint="f", namespace="shop"),
        target=target,
        **labels,  # type: ignore[arg-type]
    )


def test_deny_and_refuse_are_one_outcome_class() -> None:
    assert de.outcome_class("deny") == de.outcome_class("refuse") == "refused"
    assert de.outcome_class("allow") == "allow"


def test_a_case_grades_under_the_context_it_was_labelled_in() -> None:
    bundle = _crashloop_bundle(expected_runbook="crashloop_rollback", expected_effect="allow")

    assert de.grade(de.CorpusCase(bundle, de.Context())).effect == "allow"
    # The same incident during a freeze is a different decision, and must grade as one.
    frozen = de.grade(de.CorpusCase(bundle, de.Context(breaker_state="open")))
    assert frozen.effect == "deny"
    nearly_spent = de.grade(de.CorpusCase(bundle, de.Context(used_actions=17)))
    assert nearly_spent.effect == "require_approval"


def test_context_is_read_from_the_case_file(tmp_path: Path) -> None:
    case = _crashloop_bundle(
        expected_runbook="crashloop_rollback", expected_effect="deny"
    ).to_dict()
    case["context"] = {"breaker_state": "open"}
    (tmp_path / "frozen.json").write_text(json.dumps(case), encoding="utf-8")

    [loaded] = de.load_corpus(tmp_path)

    assert loaded.context == de.Context(breaker_state="open")
    assert de.grade(loaded).correct


def test_a_perfectly_calibrated_corpus_has_zero_calibration_error() -> None:
    cases = [
        de.Case("a", "r", "allow", "r", "allow", 1.0, "d", ""),
        de.Case("b", "r", "deny", "r", "deny", 1.0, "d", ""),
    ]
    assert de.brier(cases) == 0.0
    assert de.expected_calibration_error(cases) == 0.0


def test_overconfidence_is_what_brier_and_ece_punish() -> None:
    wrong_but_sure = [de.Case("a", "r", "allow", "r", "deny", 0.95, "d", "")]
    assert de.brier(wrong_but_sure) > de.MAX_BRIER
    assert de.expected_calibration_error(wrong_but_sure) > de.MAX_ECE


def test_a_must_refuse_case_that_acts_fails_the_gate_however_good_the_rest() -> None:
    labelled = _crashloop_bundle(expected_runbook="crashloop_rollback", expected_effect="deny")
    report = de.evaluate([de.CorpusCase(labelled, de.Context())], frozen={})
    assert any("refusal recall" in f for f in report.failures)

"""Decision gate: replay the labelled corpus through the planner and grade the DECISIONS.

A CI gate, not a unit test. Unit tests check that code does what its author meant; this checks that
the decisions are right -- the runbook and the effect -- and that the confidence
number on every plan means something.

    accuracy          >= 0.90   the chosen runbook and effect match the label
    Brier score       <= 0.08   confidence tracks reality
    ECE               <= 0.10   when it says 0.9, it is right about 90% of the time
    refusal recall    == 1.00   every must-refuse case is refused or denied -- graded, not averaged
    determinism       exact     the same bundle yields a byte-identical plan, matching the frozen
                                digest for this PLANNER_VERSION

Hermetic: the bundles are committed JSON and `planner.plan_for` is a pure function of the bundle,
so CI needs no cluster. When a deliberate logic change moves the digests, rerun with `--freeze` and
commit the new `frozen_digests.json` in the same pull request -- that is what makes every
decision-affecting change visible in review.

    python -m evals.decision_eval
    python -m evals.decision_eval --freeze      # after a deliberate decision change
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from evals._paths import CORPUS, ENGINE_SRC, REPO_ROOT

sys.path.insert(0, str(ENGINE_SRC))

from bundle import IncidentBundle  # noqa: E402
from config import get_settings  # noqa: E402
from planner import plan_for  # noqa: E402
from policy import BudgetSnapshot, PromotionSnapshot  # noqa: E402
from runbooks import load_all  # noqa: E402

TARGET_ACCURACY = 0.90
MAX_BRIER = 0.08
MAX_ECE = 0.10
REQUIRED_REFUSAL_RECALL = 1.0
MIN_CASES = 25
MIN_MUST_REFUSE = 4
ECE_BINS = 10

FROZEN = REPO_ROOT / "evals" / "frozen_digests.json"
REFUSING = frozenset({"deny", "refuse"})


def outcome_class(effect: str) -> str:
    """A runbook refusing and policy denying are the same outcome for grading: nothing acts.

    The refusal contract is "the plan is a refusal OR policy denies" -- which guardrail stops a
    StatefulSet rollback first is an implementation detail, and grading the difference would reward
    weakening the earlier guardrail so the later one gets the credit.
    """
    return "refused" if effect in REFUSING else effect


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    expected_runbook: str
    expected_effect: str
    runbook: str | None
    effect: str
    confidence: float
    digest: str
    note: str

    @property
    def correct(self) -> bool:
        return self.runbook == self.expected_runbook and outcome_class(
            self.effect
        ) == outcome_class(self.expected_effect)

    @property
    def must_refuse(self) -> bool:
        return self.expected_effect in REFUSING

    @property
    def refused(self) -> bool:
        return self.effect in REFUSING


@dataclass(frozen=True, slots=True)
class Report:
    cases: list[Case]
    accuracy: float
    brier: float
    ece: float
    refusal_recall: float
    deterministic: bool
    digest_drift: list[str]
    failures: list[str]

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True, slots=True)
class Context:
    """The stateful policy inputs a case was labelled under.

    Defaults are "promoted, empty budget, breaker closed", so a case grades the decision logic
    rather than whatever rung a ladder happened to be on. A case sets them only when the state IS
    the point -- a node alert during a freeze must be refused because of the freeze.
    """

    promotion_mode: str = "auto"
    breaker_state: str = "closed"
    used_actions: int = 0
    max_actions: int = 20


@dataclass(frozen=True, slots=True)
class CorpusCase:
    bundle: IncidentBundle
    context: Context


def load_corpus(path: Path = CORPUS) -> list[CorpusCase]:
    cases: list[CorpusCase] = []
    for p in sorted(path.glob("*.json")):
        raw = json.loads(p.read_text(encoding="utf-8"))
        cases.append(
            CorpusCase(IncidentBundle.from_dict(raw), Context(**(raw.get("context") or {})))
        )
    unlabelled = [c.bundle.incident_id for c in cases if not c.bundle.is_labelled]
    if unlabelled:
        raise SystemExit(f"corpus bundles without labels: {unlabelled}")
    return cases


def _decide(case: CorpusCase):  # noqa: ANN202 - planner.Decision
    ctx = case.context
    return plan_for(
        case.bundle,
        promotion=PromotionSnapshot(mode=ctx.promotion_mode),
        budget=BudgetSnapshot(
            used_actions=ctx.used_actions,
            max_actions=ctx.max_actions,
            breaker_state=ctx.breaker_state,
        ),
    )


def grade(case: CorpusCase) -> Case:
    bundle = case.bundle
    decision = _decide(case)
    # Determinism, in process: a second run must be byte-identical before the frozen set is even
    # consulted. Hidden state -- a clock, a dict ordering, a cache -- shows up here first.
    again = _decide(case)
    if decision.plan.digest() != again.plan.digest():
        raise SystemExit(f"{bundle.incident_id}: planning is not deterministic within one process")
    return Case(
        id=bundle.incident_id,
        expected_runbook=bundle.expected_runbook or "",
        expected_effect=bundle.expected_effect or "",
        runbook=decision.runbook,
        effect=decision.effect,
        confidence=float(decision.plan.confidence),
        digest=decision.plan.digest(),
        note=bundle.label_note or "",
    )


def brier(cases: list[Case]) -> float:
    return sum((c.confidence - float(c.correct)) ** 2 for c in cases) / len(cases)


def expected_calibration_error(cases: list[Case], bins: int = ECE_BINS) -> float:
    """Weighted gap between stated confidence and observed accuracy, per confidence bin."""
    total = len(cases)
    ece = 0.0
    for b in range(bins):
        low, high = b / bins, (b + 1) / bins
        members = [
            c for c in cases if low < c.confidence <= high or (b == 0 and c.confidence == 0.0)
        ]
        if not members:
            continue
        accuracy = sum(c.correct for c in members) / len(members)
        confidence = sum(c.confidence for c in members) / len(members)
        ece += abs(accuracy - confidence) * len(members) / total
    return ece


def evaluate(corpus: list[CorpusCase], frozen: dict[str, str] | None) -> Report:
    cases = [grade(c) for c in corpus]
    failures: list[str] = []

    if len(cases) < MIN_CASES:
        failures.append(f"corpus has {len(cases)} cases; the gate requires at least {MIN_CASES}")
    must_refuse = [c for c in cases if c.must_refuse]
    if len(must_refuse) < MIN_MUST_REFUSE:
        failures.append(
            f"corpus has {len(must_refuse)} must-refuse cases; the gate requires {MIN_MUST_REFUSE}"
        )
    uncovered = sorted({r.name for r in load_all().all()} - {c.expected_runbook for c in cases})
    if uncovered:
        failures.append(f"no corpus case exercises: {', '.join(uncovered)}")

    accuracy = sum(c.correct for c in cases) / len(cases) if cases else 0.0
    score = brier(cases) if cases else 1.0
    ece = expected_calibration_error(cases) if cases else 1.0
    recall = sum(c.refused for c in must_refuse) / len(must_refuse) if must_refuse else 0.0

    if accuracy < TARGET_ACCURACY:
        failures.append(f"accuracy {accuracy:.3f} < {TARGET_ACCURACY}")
    if score > MAX_BRIER:
        failures.append(f"Brier score {score:.4f} > {MAX_BRIER}")
    if ece > MAX_ECE:
        failures.append(f"ECE {ece:.4f} > {MAX_ECE}")
    if recall < REQUIRED_REFUSAL_RECALL:
        missed = [c.id for c in must_refuse if not c.refused]
        failures.append(f"refusal recall {recall:.3f}: these must-refuse cases acted: {missed}")

    drift: list[str] = []
    if frozen is None:
        failures.append(
            f"no frozen digests for planner {get_settings().planner_version}; "
            "run with --freeze and commit evals/frozen_digests.json"
        )
    else:
        drift = sorted(c.id for c in cases if frozen.get(c.id) != c.digest) + sorted(
            set(frozen) - {c.id for c in cases}
        )
        if drift:
            failures.append(
                f"determinism digest changed for {drift}: a decision changed. If deliberate, rerun "
                "with --freeze and commit the new digests in the same PR"
            )

    return Report(
        cases=cases,
        accuracy=accuracy,
        brier=score,
        ece=ece,
        refusal_recall=recall,
        deterministic=frozen is not None and not drift,
        digest_drift=drift,
        failures=failures,
    )


def read_frozen(version: str) -> dict[str, str] | None:
    if not FROZEN.is_file():
        return None
    data: dict[str, dict[str, str]] = json.loads(FROZEN.read_text(encoding="utf-8"))
    return data.get(version)


def write_frozen(version: str, cases: list[Case]) -> None:
    data: dict[str, dict[str, str]] = (
        json.loads(FROZEN.read_text(encoding="utf-8")) if FROZEN.is_file() else {}
    )
    data[version] = {c.id: c.digest for c in sorted(cases, key=lambda c: c.id)}
    FROZEN.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def render(report: Report) -> str:
    width = max(len(c.id) for c in report.cases) if report.cases else 10
    header = f"{'case':<{width}}  {'expected':<32} {'got':<32} conf  ok"
    lines = [header]
    for c in report.cases:
        expected = f"{c.expected_runbook}/{c.expected_effect}"
        got = f"{c.runbook}/{c.effect}"
        lines.append(
            f"{c.id:<{width}}  {expected:<32} {got:<32} {c.confidence:4.2f}  "
            f"{'ok' if c.correct else 'WRONG'}"
        )
    must = sum(c.must_refuse for c in report.cases)
    lines += [
        "",
        f"cases            {len(report.cases)} ({must} must-refuse)",
        f"accuracy         {report.accuracy:.3f}   (gate >= {TARGET_ACCURACY})",
        f"Brier            {report.brier:.4f}  (gate <= {MAX_BRIER})",
        f"ECE              {report.ece:.4f}  (gate <= {MAX_ECE})",
        f"refusal recall   {report.refusal_recall:.3f}   (gate == {REQUIRED_REFUSAL_RECALL})",
        f"determinism      {'frozen digests match' if report.deterministic else 'NOT VERIFIED'}",
        "",
        "PASS" if report.passed else "FAIL\n  - " + "\n  - ".join(report.failures),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--freeze", action="store_true", help="record the current digests")
    parser.add_argument("--json", type=Path, help="also write the report as JSON")
    args = parser.parse_args(argv)

    version = get_settings().planner_version
    corpus = load_corpus()
    if args.freeze:
        write_frozen(version, [grade(c) for c in corpus])
        print(
            f"froze {len(corpus)} digests for planner {version} in {FROZEN.relative_to(REPO_ROOT)}"
        )

    report = evaluate(corpus, read_frozen(version))
    print(render(report))
    if args.json:
        args.json.write_text(
            json.dumps({**asdict(report), "passed": report.passed}, indent=2, default=str),
            encoding="utf-8",
        )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

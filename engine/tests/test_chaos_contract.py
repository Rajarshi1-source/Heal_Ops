"""Three chaos tests per runbook, or it doesn't merge.

The chaos suite itself needs a cluster, so it cannot gate every merge. This can: it reads the suite
without running it and fails when a registered runbook is missing its fix, compensation or refusal
test. A runbook added without them is caught by the hermetic unit gate, not by its first incident.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from runbooks import load_all

CHAOS = Path(__file__).parent / "chaos"

#: How each kind of test announces itself. Names are the contract because they are what a reviewer
#: reads in the CI log -- "test_refuses_..." failing says what broke without opening the file.
KINDS = {
    "fix": lambda name: name.endswith("_end_to_end"),
    "compensation": lambda name: name.startswith("test_compensation"),
    "refusal": lambda name: name.startswith("test_refuses") or "_stops_" in name,
}


def _tests_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    ]


@pytest.mark.parametrize("runbook", sorted(rb.name for rb in load_all().all()))
def test_every_runbook_ships_its_three_chaos_tests(runbook: str) -> None:
    module = CHAOS / f"test_{runbook}.py"
    assert module.is_file(), f"{runbook} has no chaos suite at {module}"
    names = _tests_in(module)
    missing = [kind for kind, matches in KINDS.items() if not any(matches(n) for n in names)]
    assert not missing, f"{runbook} is missing its {', '.join(missing)} chaos test(s): {names}"


def test_the_chaos_suite_is_never_part_of_the_unit_gate() -> None:
    """Every chaos test is marked, whether or not its module remembered: the conftest says so."""
    conftest = (CHAOS / "conftest.py").read_text(encoding="utf-8")
    assert "pytest.mark.chaos" in conftest

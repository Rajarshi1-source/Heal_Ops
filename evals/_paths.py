"""Paths shared by the gates. The gates run from the repo root with `python -m evals.<gate>`."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_SRC = REPO_ROOT / "engine" / "src"
CORPUS = REPO_ROOT / "corpus"
EVIDENCE = REPO_ROOT / "evidence"
POLICY_FILE = REPO_ROOT / "policy" / "remediation.yaml"

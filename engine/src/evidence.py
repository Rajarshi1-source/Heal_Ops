"""Evidence bundles: one content-addressed file that reconstructs an entire incident.

A bundle holds the alerts, the correlation decision, the plan, the policy decision, the approval
(who decided, when), the execution with its before/after state, the verification verdict with its
signals, any compensation, and the incident's ledger entries with their hashes. **The sha256 of its
canonical JSON is the incident's identity**: it is what an auditor is handed, what the PR body and
the Slack escalation quote, and what `healopsctl replay` takes.

Canonical JSON is the ledger's own frozen contract (`sort_keys`, no whitespace), so a bundle's hash
is stable across machines and Python versions. Every datetime is rendered with `isoformat()` before
hashing, which is also what lets each ledger entry inside the bundle be re-hashed offline.

The bundle and the database are separate failure domains on purpose: bundles go to object storage
(and into git, for the demo), so losing the database does not lose the evidence, and editing the
database does not edit the evidence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

import ledger

FORMAT_VERSION: Final[int] = 1


class EvidenceError(ValueError):
    """A bundle could not be found, or does not match its own address."""


def _plain(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


async def build(session: AsyncSession, incident_id: int) -> dict[str, Any] | None:
    """Assemble the bundle from what was recorded. Read-only."""
    from api.incidents import incident_detail

    detail = await incident_detail(session, incident_id)
    if detail is None:
        return None
    return {"format_version": FORMAT_VERSION, **_plain(detail)}


def canonical(bundle: dict[str, Any]) -> str:
    return ledger.canonical(bundle)


def sha256(bundle: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(bundle).encode("utf-8")).hexdigest()


def write(bundle: dict[str, Any], directory: Path) -> Path:
    """Store under its own address. Pretty-printed for review; the hash is over canonical JSON."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sha256(bundle)}.json"
    path.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load(ref: str, directory: Path) -> tuple[str, dict[str, Any]]:
    """Find a bundle by path or by sha256 prefix, and prove it matches its address."""
    candidate = Path(ref)
    if candidate.suffix == ".json" and candidate.is_file():
        path = candidate
    else:
        matches = sorted(directory.glob(f"{ref}*.json"))
        if not matches:
            raise EvidenceError(f"no evidence bundle matches {ref!r} in {directory}")
        if len(matches) > 1:
            raise EvidenceError(f"{ref!r} is ambiguous: {', '.join(m.stem[:12] for m in matches)}")
        path = matches[0]

    bundle: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    digest = sha256(bundle)
    if digest != path.stem:
        # The file was edited after it was written: its content no longer hashes to its name.
        raise EvidenceError(
            f"{path.name} does not match its own address (content hashes to {digest[:16]}): "
            "the bundle was altered"
        )
    return digest, bundle


def verify(bundle: dict[str, Any]) -> ledger.ChainStatus:
    """Re-hash every ledger entry the bundle carries. No database, no network."""
    return ledger.verify_links(list(bundle.get("ledger") or []))

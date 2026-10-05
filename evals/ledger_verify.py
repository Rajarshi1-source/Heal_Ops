"""Ledger gate: the committed evidence bundles are intact, and so is the chain they came from.

Offline by default, so it runs in CI with no database:

* every bundle in `evidence/` hashes to its own filename (content addressing: an edited bundle no
  longer matches its name);
* every ledger entry inside every bundle re-hashes from its own content and its stated predecessor,
  and adjacent entries link (`ledger.verify_links`).

With `--database`, it also walks the whole chain from GENESIS against PostgreSQL (`verify_chain`),
which is what the in-cluster hourly check and `healopsctl audit --verify-chain` do. A tamper-
evidence mechanism nobody checks is decoration; this is the check.

    python -m evals.ledger_verify
    python -m evals.ledger_verify --database      # also walk the live chain
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from evals._paths import ENGINE_SRC, EVIDENCE, REPO_ROOT

sys.path.insert(0, str(ENGINE_SRC))

import evidence  # noqa: E402
import ledger  # noqa: E402

MIN_BUNDLES = 1


def verify_bundles() -> list[str]:
    problems: list[str] = []
    paths = sorted(EVIDENCE.glob("*.json"))
    if len(paths) < MIN_BUNDLES:
        problems.append(f"no evidence bundles in {EVIDENCE.relative_to(REPO_ROOT)}")
    for path in paths:
        try:
            digest, bundle = evidence.load(str(path), EVIDENCE)
        except evidence.EvidenceError as exc:
            problems.append(str(exc))
            continue
        status = evidence.verify(bundle)
        verdict = "VALID" if status.valid else f"BROKEN ({status.detail})"
        print(
            f"{digest[:16]}  incident #{bundle.get('id')}  {status.entries:>3} entries  {verdict}"
        )
        if not status.valid:
            problems.append(f"{path.name}: {status.detail}")
    return problems


async def verify_database() -> list[str]:
    from db import connection

    try:
        async with connection.session_scope() as session:
            status = await ledger.verify_chain(session)
    finally:
        await connection.dispose()
    print(f"database chain: {status.entries} entries, {'VALID' if status.valid else status.detail}")
    return [] if status.valid else [f"database chain broken at seq={status.broken_at_seq}"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", action="store_true", help="also verify the live chain")
    args = parser.parse_args(argv)

    problems = verify_bundles()
    if args.database:
        problems += asyncio.run(verify_database())
    for problem in problems:
        print(f"  - {problem}")
    print("PASS" if not problems else "FAIL")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())

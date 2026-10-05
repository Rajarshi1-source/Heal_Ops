"""Selects which fault to run.

`SERVICE` (or the first argument) names the chaos fixture. The names here are a **contract**: each
one is the `chaos_fixture` value on the runbook that answers it, and CI asserts that every
registered runbook names a fixture that exists in this table. `cpu-load` is the one exception: it
is not a fixture but the load that makes cpu-burner's fault real.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

from services import cpu_burner, cpu_load, crashy_api, hungry_worker, log_spammer

SERVICES: dict[str, Callable[[], None]] = {
    "crashy-api": crashy_api.main,
    "hungry-worker": hungry_worker.main,
    "cpu-burner": cpu_burner.main,
    "cpu-load": cpu_load.main,
    "log-spammer": log_spammer.main,
}


def main() -> None:
    name = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SERVICE", "")).strip()
    entry = SERVICES.get(name)
    if entry is None:
        print(
            f"unknown SERVICE {name!r}; expected one of {', '.join(sorted(SERVICES))}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(f"chaos-app: starting {name}", flush=True)
    entry()


if __name__ == "__main__":
    main()

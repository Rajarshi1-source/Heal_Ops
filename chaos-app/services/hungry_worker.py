"""hungry-worker -- allocates until the kernel OOM-kills it.

Chaos fixture for runbook `oom_memory_bump`.

Two properties make this a fair test of the in-place resize path:

1. It allocates up to `TARGET_MIB` and then *stops*. A worker that grows without bound could never
   be fixed by raising the limit, so a runbook that "fixed" it would be lying. With a target, a
   memory limit above the target genuinely resolves the incident.
2. Pages are actually written, not just reserved. `bytearray(n)` alone can sit in lazily-mapped
   pages on some allocators; touching each page forces the resident set to grow so the cgroup
   accounts for it and `container_oom_events_total` increments for real.
"""

from __future__ import annotations

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
TARGET_MIB = int(os.environ.get("TARGET_MIB", "512"))
GROWTH_MIB_PER_SEC = int(os.environ.get("GROWTH_MIB_PER_SEC", "16"))
START_DELAY_S = float(os.environ.get("START_DELAY_S", "10"))
PAGE = 4096

_held: list[bytearray] = []
_allocated_mib = 0
_lock = threading.Lock()


def _grow() -> None:
    global _allocated_mib
    time.sleep(START_DELAY_S)
    print(
        f"hungry-worker: growing to {TARGET_MIB}MiB at {GROWTH_MIB_PER_SEC}MiB/s",
        flush=True,
    )
    while True:
        with _lock:
            if _allocated_mib >= TARGET_MIB:
                break
            chunk = bytearray(GROWTH_MIB_PER_SEC * 1024 * 1024)
            # Touch one byte per page so the pages are resident and the cgroup counts them.
            for off in range(0, len(chunk), PAGE):
                chunk[off] = 1
            _held.append(chunk)
            _allocated_mib += GROWTH_MIB_PER_SEC
        time.sleep(1)
    print(f"hungry-worker: holding {_allocated_mib}MiB", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/metrics"):
            body = (
                "# HELP chaos_hungry_allocated_mib Resident memory deliberately held.\n"
                "# TYPE chaos_hungry_allocated_mib gauge\n"
                f"chaos_hungry_allocated_mib {_allocated_mib}\n"
                "# HELP chaos_hungry_target_mib Allocation target.\n"
                "# TYPE chaos_hungry_target_mib gauge\n"
                f"chaos_hungry_target_mib {TARGET_MIB}\n"
            )
        else:
            body = f'{{"allocated_mib": {_allocated_mib}, "target_mib": {TARGET_MIB}}}'
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> None:
    threading.Thread(target=_grow, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()

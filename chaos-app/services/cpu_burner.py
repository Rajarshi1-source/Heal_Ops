"""cpu-burner -- saturates its CPU limit on demand.

Chaos fixture for runbook `cpu_scale_up`.

Burning is on demand (`POST /burn?seconds=600&workers=2`) as well as on start, because the storm
demo needs faults to begin on cue rather than whenever a pod happened to schedule.

The burn loop runs in *threads*, not processes, and each thread does integer work in a tight loop.
Python's GIL means one thread saturates roughly one core; `workers` above the container's CPU limit
just deepens throttling, which is exactly the signal the runbook keys on.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "8080"))
BURN_ON_START = os.environ.get("BURN_ON_START", "false").lower() == "true"
DEFAULT_WORKERS = int(os.environ.get("BURN_WORKERS", "2"))
DEFAULT_SECONDS = int(os.environ.get("BURN_SECONDS", "900"))

_burn_until = 0.0
_workers = 0
_lock = threading.Lock()


def _spin(worker_id: int) -> None:
    global _workers
    with _lock:
        _workers += 1
    try:
        x = 0
        while time.time() < _burn_until:
            # A tight arithmetic loop with no allocation and no syscalls: pure CPU.
            for _ in range(200_000):
                x = (x * 31 + 17) & 0xFFFFFFFF
    finally:
        with _lock:
            _workers -= 1
        print(f"cpu-burner: worker {worker_id} stopped", flush=True)


def _start_burn(seconds: int, workers: int) -> None:
    global _burn_until
    _burn_until = time.time() + seconds
    for i in range(workers):
        threading.Thread(target=_spin, args=(i,), daemon=True).start()
    print(f"cpu-burner: burning with {workers} workers for {seconds}s", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/burn"):
            self._json(404, {"error": "not found"})
            return
        q = parse_qs(parsed.query)
        seconds = int(q.get("seconds", [str(DEFAULT_SECONDS)])[0])
        workers = int(q.get("workers", [str(DEFAULT_WORKERS)])[0])
        _start_burn(seconds, workers)
        self._json(202, {"burning": True, "seconds": seconds, "workers": workers})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/stop"):
            global _burn_until
            _burn_until = 0.0
            self._json(200, {"burning": False})
            return
        remaining = max(0.0, _burn_until - time.time())
        if parsed.path.startswith("/metrics"):
            body = (
                "# HELP chaos_cpu_burn_workers Threads currently spinning.\n"
                "# TYPE chaos_cpu_burn_workers gauge\n"
                f"chaos_cpu_burn_workers {_workers}\n"
                "# HELP chaos_cpu_burn_remaining_seconds Seconds left in the current burn.\n"
                "# TYPE chaos_cpu_burn_remaining_seconds gauge\n"
                f"chaos_cpu_burn_remaining_seconds {remaining:.1f}\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(body.encode())
            return
        self._json(200, {"burning": remaining > 0, "workers": _workers, "remaining_s": remaining})

    def _json(self, code: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> None:
    if BURN_ON_START:
        _start_burn(DEFAULT_SECONDS, DEFAULT_WORKERS)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()

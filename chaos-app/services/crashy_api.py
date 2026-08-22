"""crashy-api -- fails on a timer to produce CrashLoopBackOff.

Chaos fixture for runbook `crashloop_rollback`. The name must stay in sync with that runbook's
`chaos_fixture` property; CI asserts the pairing.

The fault is deliberately *version-dependent*: when `CRASH_AFTER` is set the process exits non-zero
after that delay, and when it is unset the process stays healthy. That is what makes `rollout undo`
a real fix rather than a coincidence -- the previous ReplicaSet has no `CRASH_AFTER`, so rolling
back genuinely stops the crash loop and verification can honestly report `remediated`.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
CRASH_AFTER = os.environ.get("CRASH_AFTER", "").strip()
EXIT_CODE = int(os.environ.get("CRASH_EXIT_CODE", "1"))
STARTED_AT = time.time()


def _parse_duration(raw: str) -> float | None:
    """Accept `30s`, `2m`, or a bare number of seconds."""
    if not raw:
        return None
    raw = raw.lower()
    try:
        if raw.endswith("ms"):
            return float(raw[:-2]) / 1000
        if raw.endswith("s"):
            return float(raw[:-1])
        if raw.endswith("m"):
            return float(raw[:-1]) * 60
        return float(raw)
    except ValueError:
        print(f"crashy-api: unparseable CRASH_AFTER={raw!r}, staying healthy", flush=True)
        return None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/healthz"):
            self._json(200, {"status": "ok", "uptime_s": round(time.time() - STARTED_AT, 1)})
        elif self.path.startswith("/metrics"):
            body = (
                "# HELP chaos_crashy_uptime_seconds Seconds since this process started.\n"
                "# TYPE chaos_crashy_uptime_seconds gauge\n"
                f"chaos_crashy_uptime_seconds {time.time() - STARTED_AT:.3f}\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self._json(200, {"service": "crashy-api", "crash_after": CRASH_AFTER or None})

    def _json(self, code: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        # Default logging writes to stderr and makes kubectl logs noisy during a crash loop.
        return


def _suicide(delay: float) -> None:
    time.sleep(delay)
    print(
        f"crashy-api: CRASH_AFTER={CRASH_AFTER} elapsed, exiting {EXIT_CODE}",
        file=sys.stderr,
        flush=True,
    )
    # os._exit skips interpreter cleanup so the container dies immediately, which is what a real
    # crash looks like to the kubelet.
    os._exit(EXIT_CODE)


def main() -> None:
    delay = _parse_duration(CRASH_AFTER)
    if delay is not None:
        print(f"crashy-api: will exit {EXIT_CODE} in {delay}s", flush=True)
        threading.Thread(target=_suicide, args=(delay,), daemon=True).start()
    else:
        print("crashy-api: healthy mode (no CRASH_AFTER set)", flush=True)

    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()

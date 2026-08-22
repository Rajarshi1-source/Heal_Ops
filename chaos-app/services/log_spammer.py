"""log-spammer -- fills a volume to trigger disk pressure.

Chaos fixture for runbook `disk_cleanup`.

Files are written with **back-dated mtimes spread across several weeks**. That detail is what makes
the runbook's contract testable: `disk_cleanup` archives files older than a 7-day floor and must
leave newer ones alone, so a directory of uniformly-fresh files could not distinguish a correct
implementation from one that deletes everything.
"""

from __future__ import annotations

import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT", "8080"))
LOG_DIR = Path(os.environ.get("LOG_DIR", "/var/log/chaos"))
RATE_MIB_PER_MIN = int(os.environ.get("RATE_MIB_PER_MIN", "64"))
MAX_TOTAL_MIB = int(os.environ.get("MAX_TOTAL_MIB", "2048"))
SEED_OLD_FILES = int(os.environ.get("SEED_OLD_FILES", "12"))
START_DELAY_S = float(os.environ.get("START_DELAY_S", "5"))

_written_mib = 0
_line = ("chaos log line " + "x" * 200 + "\n").encode()


def _seed_backdated_files() -> None:
    """Write files dated 1-30 days old so the 7-day archive floor has something to discriminate."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for i in range(SEED_OLD_FILES):
        age_days = random.uniform(1, 30)  # noqa: S311 - fixture data, not security
        path = LOG_DIR / f"archived-{i:03d}.log"
        with path.open("wb") as fh:
            fh.write(_line * 4096)  # ~1 MiB
        mtime = now - age_days * 86400
        os.utime(path, (mtime, mtime))
    print(f"log-spammer: seeded {SEED_OLD_FILES} back-dated files in {LOG_DIR}", flush=True)


def _spam() -> None:
    global _written_mib
    time.sleep(START_DELAY_S)
    _seed_backdated_files()

    chunk = _line * 4096  # ~1 MiB
    interval = 60.0 / max(RATE_MIB_PER_MIN, 1)
    idx = 0
    while _written_mib < MAX_TOTAL_MIB:
        path = LOG_DIR / f"spam-{idx // 64:04d}.log"
        with path.open("ab") as fh:
            fh.write(chunk)
            fh.flush()
            # Without fsync the pages stay dirty in the page cache and the filesystem usage the
            # node exporter reports lags the write by long enough to break the demo timing.
            os.fsync(fh.fileno())
        _written_mib += 1
        idx += 1
        time.sleep(interval)
    print(f"log-spammer: reached cap of {MAX_TOTAL_MIB}MiB, stopping", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        try:
            files = len(list(LOG_DIR.glob("*.log")))
        except OSError:
            files = -1
        body = (
            "# HELP chaos_log_written_mib Mebibytes written by the spammer.\n"
            "# TYPE chaos_log_written_mib counter\n"
            f"chaos_log_written_mib {_written_mib}\n"
            "# HELP chaos_log_files Files currently in the log directory.\n"
            "# TYPE chaos_log_files gauge\n"
            f"chaos_log_files {files}\n"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> None:
    threading.Thread(target=_spam, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()

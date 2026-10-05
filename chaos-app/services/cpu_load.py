"""cpu-load -- drives a fixed request rate at cpu-burner, so its CPU comes from LOAD.

The load driver for runbook `cpu_scale_up`'s fault, not a fixture of its own: it is how the fault
is injected (`kubectl scale deployment/cpu-load --replicas=1`) and how it is withdrawn (scale to 0).

Why load and not a self-inflicted burn: the runbook's fix is more replicas, and more replicas only
help a workload whose CPU is spent serving requests the Service spreads across them. A pod spinning
on its own would stay pinned at its limit however many siblings it had, so a fix test against it
could never honestly pass -- and one that did would be lying.

Open loop, deliberately. A fixed rate keeps the offered load constant as replicas change, which is
what makes "per-pod usage halved when replicas doubled" a measurement. A closed-loop driver would
slow down when the burner saturates and quietly stop being a fault. When every slot is busy a
request is dropped and counted rather than queued, so a saturated burner cannot grow the backlog
without bound.

Balanced client-side, round robin across the burner pods it resolves from a headless Service --
re-resolved every two seconds, so pods the runbook adds start taking load at once. kube-proxy's
random choice per connection turned an even rate into bursts per pod, and a burst against a 100ms
CFS period is throttling even at half the limit: the fix test then judged the scale-out on noise.
Even spacing per pod means a pod is throttled only when its average demand really exceeds the
quota -- which is the claim the runbook makes and the verifier checks.
"""

from __future__ import annotations

import http.client
import itertools
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", "8080"))
TARGET_URL = os.environ.get("TARGET_URL", "http://cpu-burner-headless:8080/work?ms=2")
RATE_PER_SEC = float(os.environ.get("RATE_PER_SEC", "260"))
MAX_IN_FLIGHT = int(os.environ.get("MAX_IN_FLIGHT", "256"))
TIMEOUT_S = float(os.environ.get("TIMEOUT_S", "3"))

_slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)
_counts = {"sent": 0, "ok": 0, "failed": 0, "dropped": 0}
_lock = threading.Lock()


def _bump(key: str) -> None:
    with _lock:
        _counts[key] += 1


def _request(host: str, port: int, path: str) -> None:
    try:
        conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT_S)
        conn.request("GET", path, headers={"Connection": "close"})
        status = conn.getresponse().status
        conn.close()
        _bump("ok" if status == 200 else "failed")
    except OSError:
        _bump("failed")
    finally:
        _slots.release()


_pods: list[str] = []


def _resolve(host: str, port: int) -> None:
    """Keep the list of burner pod IPs current; a failed lookup keeps the last good list."""
    global _pods
    while True:
        try:
            found = sorted(
                {info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
            )
            if found != _pods:
                print(f"cpu-load: balancing over {len(found)} pod(s)", flush=True)
            _pods = found
        except OSError as exc:
            print(f"cpu-load: resolve failed ({exc}); keeping {len(_pods)} pod(s)", flush=True)
        time.sleep(2)


def _drive() -> None:
    target = urlparse(TARGET_URL)
    host, port = target.hostname or "localhost", target.port or 80
    path = target.path + (f"?{target.query}" if target.query else "")
    interval = 1.0 / RATE_PER_SEC
    threading.Thread(target=_resolve, args=(host, port), daemon=True).start()
    print(f"cpu-load: {RATE_PER_SEC:g} req/s at {TARGET_URL}", flush=True)
    counter = itertools.count()
    # Scheduled against the clock rather than sleeping a fixed interval after each send, so time
    # spent starting threads does not quietly lower the offered rate.
    next_at = time.monotonic()
    while True:
        next_at += interval
        pods = _pods
        if pods and _slots.acquire(blocking=False):
            _bump("sent")
            pod = pods[next(counter) % len(pods)]
            threading.Thread(target=_request, args=(pod, port, path), daemon=True).start()
        else:
            _bump("dropped")
        delay = next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_at = time.monotonic()  # fell behind; do not burst to catch up


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        with _lock:
            snapshot = dict(_counts)
        body = "".join(
            f"# TYPE chaos_cpu_load_{k}_total counter\nchaos_cpu_load_{k}_total {v}\n"
            for k, v in snapshot.items()
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> None:
    threading.Thread(target=_drive, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()

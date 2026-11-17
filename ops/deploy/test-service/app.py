"""aiops-test-svc: a disposable service with three real, independently fixable failure modes.

  /health  200 "ok"                 healthy
           500 "error: ..."         bad release (v2 ships a config regression)   -> rollback_test_deployment
           503 "cache full: ..."    /cache above its limit                        -> clear_test_cache
           503 "saturated: ..."     demand above live replica capacity            -> scale_test_service
           (restart_test_service fixes a stopped or wedged container)

Replicas share /state: each one heartbeats state/replicas/<name>; capacity = live heartbeats.
Faults are injected only by the evaluation harness (docker exec), never by the platform."""
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = os.environ.get("VERSION", "v1")
NAME = os.environ.get("NAME", "aiops-test-svc")
STATE, CACHE = Path("/state"), Path("/cache")
CACHE_LIMIT_MB = int(os.environ.get("CACHE_LIMIT_MB", "50"))
HEARTBEAT_S, STALE_S = 2, 8


def heartbeat() -> None:
    (STATE / "replicas").mkdir(parents=True, exist_ok=True)
    while True:
        (STATE / "replicas" / NAME).write_text(VERSION)
        time.sleep(HEARTBEAT_S)


def cache_mb() -> float:
    return sum(f.stat().st_size for f in CACHE.rglob("*") if f.is_file()) / 2**20 if CACHE.exists() else 0.0


def capacity() -> int:
    d = STATE / "replicas"
    return sum(1 for f in d.iterdir() if time.time() - f.stat().st_mtime < STALE_S) if d.exists() else 0


def demand() -> int:
    try:
        return max(1, int((STATE / "demand").read_text().strip()))
    except (OSError, ValueError):
        return 1


def health() -> tuple[int, str]:
    if VERSION == "v2":
        return 500, "error: v2 regression: KeyError 'upstream.timeout_ms' in request handler (config key renamed in v2)"
    mb = cache_mb()
    if mb > CACHE_LIMIT_MB:
        return 503, f"cache full: {mb:.0f} MB used of {CACHE_LIMIT_MB} MB limit in /cache; requests rejected"
    d, c = demand(), capacity()
    if d > c:
        return 503, f"saturated: demand needs {d} replicas, {c} running; requests queueing then rejected"
    return 200, f"ok {VERSION}"


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        code, body = health() if self.path.startswith("/health") else (200, VERSION) if self.path == "/version" else (404, "not found")
        data = (body + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        code = args[1] if len(args) > 1 else "-"
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {NAME} {VERSION} {self.command} {self.path} {code}"
        if str(code) != "200":
            line += " " + health()[1]
        print(line, flush=True)


if __name__ == "__main__":
    print(f"{NAME} {VERSION} starting (cache limit {CACHE_LIMIT_MB} MB)", flush=True)
    threading.Thread(target=heartbeat, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()

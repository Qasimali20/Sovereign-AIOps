#!/usr/bin/env python3
"""Live failover test for the model gateway.

Starts a *separate* gateway instance (127.0.0.1:18790, the production gateway is
not touched) that sees the real Node-2 replica of the chat model plus a fake
replica on 127.0.0.1:18101. The fake passes discovery (/v1/models) but fails
every real request: first with HTTP 503, then by dropping the connection. Every
client request must still succeed, and the fake must be put in backoff rather
than hammered.

  set -a; . ./.env; set +a; uv run ops/evaluation/scenarios/gateway_failover.py
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
MODEL = "qwen3.6-35b-a3b-fp8"
FAKE_PORT, GW_PORT = 18101, 18790
KEY = os.environ["AIOPS_GATEWAY_KEY"]
H = {"Authorization": f"Bearer {KEY}"}
mode = {"value": "503", "hits": 0}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": MODEL, "max_model_len": 1}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        mode["hits"] += 1
        self.rfile.read(int(self.headers.get("content-length", 0)))
        if mode["value"] == "drop":
            self.connection.shutdown(socket.SHUT_RDWR)       # connection reset, no response
            return
        self.send_response(503)
        self.send_header("content-length", "0")
        self.end_headers()


def wait_port(port: int, timeout: float = 30) -> None:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.3)
    raise SystemExit(f"port {port} did not open")


async def burst(c: httpx.AsyncClient, n: int, concurrency: int) -> list[httpx.Response]:
    sem = asyncio.Semaphore(concurrency)

    async def one(i):
        async with sem:
            return await c.post(f"http://127.0.0.1:{GW_PORT}/v1/chat/completions", headers=H, timeout=120, json={
                "model": "chat", "max_tokens": 8, "messages": [{"role": "user", "content": f"Say the number {i}."}]})
    return await asyncio.gather(*(one(i) for i in range(n)))


async def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = os.environ | {"AIOPS_GATEWAY_STATIC_UPSTREAMS": json.dumps([{"node": "FAKE", "base_url": f"http://127.0.0.1:{FAKE_PORT}"}])}
    gw = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.llmops.gateway:app", "--host", "127.0.0.1", "--port", str(GW_PORT),
                           "--no-access-log", "--log-level", "warning"], cwd=ROOT, env=env)
    checks: dict[str, bool] = {}
    try:
        wait_port(GW_PORT)
        async with httpx.AsyncClient() as c:
            st = (await c.get(f"http://127.0.0.1:{GW_PORT}/gateway/stats", headers=H)).json()
            reps = [u for u in st["upstreams"] if u["model"] == MODEL]
            print("replicas of", MODEL, "->", [(u["node"], u["state"]) for u in reps])
            checks["fake replica discovered alongside Node-2"] = {u["node"] for u in reps} >= {"Node-2", "FAKE"}

            for phase, m in (("HTTP 503", "503"), ("connection drop", "drop")):
                mode.update(value=m, hits=0)
                t = time.time()
                rs = await burst(c, 24, 8)
                ok = [r for r in rs if r.status_code == 200]
                nodes = {r.headers.get("x-aiops-node") for r in ok}
                print(f"{phase:16} {len(ok)}/24 ok in {time.time() - t:.1f}s, served by {nodes}, fake hit {mode['hits']}x")
                checks[f"{phase}: every request succeeded"] = len(ok) == 24
                checks[f"{phase}: answered only by the real replica"] = nodes == {"Node-2"}
                checks[f"{phase}: fake put in backoff (<= 8 hits for 24 requests)"] = mode["hits"] <= 8
                await asyncio.sleep(65)                              # let the breaker expire before the next phase

            st = (await c.get(f"http://127.0.0.1:{GW_PORT}/gateway/stats", headers=H)).json()
            fake = next(u for u in st["upstreams"] if u["node"] == "FAKE")
            print("fake replica after test:", {k: fake[k] for k in ("state", "fails", "errors", "requests", "last_error")})
            checks["failures recorded on the fake replica"] = fake["errors"] >= 2
    finally:
        gw.terminate()
        gw.wait(10)
        srv.shutdown()
    print()
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    if __name__ == "__main__":
        sys.path.insert(0, str(ROOT))
        from backend.core import evals
        evals.record("gateway-failover", sum(checks.values()), len(checks), checks, {"requests": 48})
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

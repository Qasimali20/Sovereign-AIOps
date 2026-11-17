#!/usr/bin/env python3
"""Cross-node failover: the primary chat model is completely down, so the `chat` alias
must fall through to the REAL second model on Node-3.

Runs a separate gateway instance (127.0.0.1:18790; production untouched) whose `chat`
alias is ["broken-primary", "qwen-chat"]: broken-primary is a fake server on Node-2 that
passes discovery but fails every request; qwen-chat is Node-3's real Qwen3-30B.

  set -a; . ./.env; set +a; uv run ops/evaluation/scenarios/gateway_cross_node_failover.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from collections import Counter
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway_failover as base  # noqa: E402  (fake server + helpers)

base.MODEL = "broken-primary"
H = base.H


async def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", base.FAKE_PORT), base.Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base.mode.update(value="drop", hits=0)
    env = os.environ | {
        "AIOPS_GATEWAY_STATIC_UPSTREAMS": json.dumps([{"node": "Node-2 (broken)", "base_url": f"http://127.0.0.1:{base.FAKE_PORT}"}]),
        "AIOPS_GATEWAY_ALIASES": json.dumps({"chat": ["broken-primary", "qwen-chat"]}),
    }
    gw = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.llmops.gateway:app", "--host", "127.0.0.1", "--port",
                           str(base.GW_PORT), "--no-access-log", "--log-level", "warning"], cwd=base.ROOT, env=env)
    checks = {}
    try:
        base.wait_port(base.GW_PORT)
        async with httpx.AsyncClient() as c:
            rs = await base.burst(c, 12, 4)                       # Node-3 serves 4 sequences at a time
        ok = [r for r in rs if r.status_code == 200]
        served = Counter((r.headers.get("x-aiops-node"), r.headers.get("x-aiops-model")) for r in ok)
        print(f"{len(ok)}/12 ok; served by {dict(served)}; broken primary hit {base.mode['hits']}x")
        print("sample answer:", ok[0].json()["choices"][0]["message"]["content"][:60] if ok else None)
        checks["every request succeeded with the primary down"] = len(ok) == 12
        checks["answered by Node-3's real model"] = set(served) == {("Node-3", "qwen-chat")}
        checks["clients still asked for 'chat'"] = all(r.headers.get("x-aiops-requested") == "chat" for r in ok)
    finally:
        gw.terminate()
        gw.wait(10)
        srv.shutdown()
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    sys.path.insert(0, str(base.ROOT))
    from backend.core import evals
    evals.record("gateway-cross-node-failover", sum(checks.values()), len(checks), checks, {"requests": 12})
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Record a read-only snapshot of every API response the console reads, for the offline demo build.

    .venv/bin/python ops/scripts/demo_snapshot.py            # writes frontend/demo/snapshot.json

Keys are the exact /api paths the frontend requests (same encoding as encodeURIComponent), so the demo build can
answer each request from the snapshot. Nothing is written to the platform; the script fails if a response looks like
it carries a secret.
"""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "frontend" / "demo" / "snapshot.json"
ENV = dict(ln.split("=", 1) for ln in (ROOT / ".env").read_text().splitlines() if "=" in ln and not ln.startswith("#"))
BASE = "http://127.0.0.1:5170/api"
HEAD = {"x-approver-token": ENV["AIOPS_APPROVER_TOKEN"], "X-Requested-With": "aiops"}
NODES = ["Node-1", "Node-2", "Node-3", "Node-4"]
RANGES = ["1h", "6h", "24h", "7d", "30d"]
q = lambda s: quote(s, safe="-_.!~*'()")              # encodeURIComponent
SECRET = re.compile(r"(?i)(sk-[a-z0-9]{12,}|bearer\s+[a-z0-9._-]{16,}|\"(password|api_key|token|secret)\"\s*:\s*\"[^\"]{4,}"
                    r"|[a-z0-9._%+-]+@(?!\d+\.service\b)[a-z0-9.-]+\.[a-z]{2,})")   # user@1000.service is a systemd unit, not an address

client = httpx.Client(headers=HEAD, timeout=60)


def get(path: str):
    r = client.get(BASE + path)
    r.raise_for_status()
    return r.json()


def main() -> None:
    paths = ["/me", "/fleet", "/services", "/system", "/policy", "/incidents?limit=1000", "/incidents/summary",
             "/audit/integrity", "/users", "/security/overview?hours=24",
             "/overview?days=7&origin=all", "/overview?days=7&origin=real",
             "/audit?limit=1000&actor=&event=&incidents_only=false",
             "/metrics/board/operations?range=24h"]
    paths += [f"/approvals?status={s}" for s in ("pending", "approved", "rejected", "expired")]
    paths += [f"/metrics/board/{b}?range={r}" for b in ("models", "fleet") for r in RANGES]
    paths += [f"/metrics/scorecard?range={r}" for r in RANGES]
    paths += [f"/fleet/{n}/history?range={r}" for n in NODES for r in ("15m", "1h", "6h", "24h", "7d", "30d")]
    incidents = get("/incidents?limit=1000")
    paths += [f"/incidents/{q(i['id'])}" for i in incidents]
    # LLMOps and AgentOps pages (request filters are applied client-side by the demo from the unfiltered list)
    paths += ["/agent/overview?days=30", "/agent/runs?limit=500", "/llm/evals", "/llm/registry"]
    paths += [f"/agent/runs/{q(r['incident_id'])}" for r in get("/agent/runs?limit=500")]
    for r in ("1h", "6h", "24h", "7d"):
        paths += [f"/llm/summary?range={r}", f"/llm/energy?range={r}",
                  f"/llm/requests?range={r}&model=&client=&outcome=&limit=200"]
    paths += [f"/llm/evals/history?model={q(m['model'])}" for m in get("/llm/evals")["models"]]
    first = get(f"/metrics/scorecard?range=6h")
    models = sorted({m["model"] for m in first} | {m["id"] for m in get(f"/metrics/model?model={q(first[0]['model'])}&range=6h")["models"]})
    paths += [f"/metrics/model?model={q(m)}&range={r}" for m in models for r in ("1h", "6h", "24h")]

    snap: dict[str, object] = {}
    failed = []

    def one(p):
        try:
            return p, get(p)
        except Exception as e:
            failed.append(f"{p}: {e}")
            return p, None
    with ThreadPoolExecutor(4) as pool:                    # gentle on the live platform
        for p, body in pool.map(one, paths):
            if body is not None:
                snap[p] = body

    text = json.dumps({"taken": time.time(), "responses": snap}, separators=(",", ":"))
    leaks = sorted({m.group(0)[:40] for m in SECRET.finditer(text)})
    if leaks:
        sys.exit(f"refusing to write: possible secrets in the snapshot: {leaks}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    print(f"{len(snap)} responses, {len(text) / 1e6:.1f} MB -> {OUT.relative_to(ROOT)}"
          + (f"\n{len(failed)} failed:\n  " + "\n  ".join(failed) if failed else ""))


if __name__ == "__main__":
    main()

"""Recorded evaluation results (security tests, failover drills, replays, unit tests).

Evaluation scripts run against scratch databases, so results are appended to their own file,
data/evaluations.jsonl (one line per run); the latest run of each evaluation is the last line with its name.
The dashboard reads these; nothing on it is typed in by hand."""
from __future__ import annotations

import json
import time

from backend.core.config import ROOT

FILE = ROOT / "data" / "evaluations.jsonl"
CATALOGUE = {   # name -> what it proves (shown on the dashboard)
    "unit-tests": "Policy, executor, races, HTTP security, gateway and injection unit tests",
    "prompt-injection": "Live attacks planted in container logs; hard guarantees must hold",
    "gpu-hang-replay": "Real Nov 13 Xid 119 hang replayed through the full workflow",
    "gateway-failover": "Broken replica beside a healthy one; every request must succeed",
    "gateway-cross-node-failover": "Primary model down; chat alias must fail over to Node-3",
    "soc-lab": "Lab attacks (logins, malware, C2, CVE) + a benign-admin false positive through the security agent",
    "sre-test-service": "Four real faults on the test service: right fix chosen, approved, executed, verified",
    "observability-recovery": "Prometheus restarted: detection continues, history persists, targets recover",
    "backup-restore": "Nightly backup restored to scratch: integrity, audit chain, row counts, off-node copy",
}


def record(name: str, passed: int, total: int, checks: dict | None = None, meta: dict | None = None) -> dict:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    run = {"name": name, "t": time.time(), "passed": passed, "total": total, "ok": passed == total,
           "checks": checks or {}, "meta": meta or {}}
    with FILE.open("a") as f:
        f.write(json.dumps(run, default=str) + "\n")
    return run


def _last_runs() -> dict[str, dict]:
    last: dict[str, dict] = {}
    if FILE.exists():
        for line in FILE.read_text().splitlines():
            try:
                run = json.loads(line)
            except ValueError:                  # a line cut short by a crash mid-write
                continue
            last[run["name"]] = run
    return last


def latest() -> list[dict]:
    out, last = [], _last_runs()
    for name, what in CATALOGUE.items():
        run = last.get(name)
        out.append({"name": name, "description": what, "run": run and {k: run[k] for k in ("t", "passed", "total", "ok", "meta")}})
    return out

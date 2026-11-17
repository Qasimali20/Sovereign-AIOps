#!/usr/bin/env python3
"""Observability recovery drill: restart the platform's Prometheus on Node-3 and check that
  1. the platform keeps detecting while Prometheus is down (it does not depend on it),
  2. history recorded before the restart is still queryable afterwards (persistent volume),
  3. every scrape target is healthy again within two minutes.
Touches only the aiops-prometheus container. Recorded as evaluation 'observability-recovery'.
  .venv/bin/python ops/evaluation/scenarios/observability_recovery.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from backend.core import config, evals  # noqa: E402

PROM = config.PROMETHEUS_URL


def q(expr: str, t: float | None = None) -> list:
    r = httpx.get(f"{PROM}/api/v1/query", params={"query": expr, **({"time": t} if t else {})}, timeout=5)
    return r.json()["data"]["result"]


def targets_up() -> tuple[int, int]:
    d = httpx.get(f"{PROM}/api/v1/targets?state=active", timeout=5).json()["data"]["activeTargets"]
    return sum(t["health"] == "up" for t in d), len(d)


def platform_scanning() -> bool:
    r = httpx.get("http://127.0.0.1:5170/metrics", timeout=5).text
    last = next(float(l.split()[-1]) for l in r.splitlines() if l.startswith("aiops_detector_last_scan_timestamp_seconds"))
    return time.time() - last < 60


def main() -> int:
    t_before = time.time() - 120
    before = q("count(aiops_node_up)", t_before)
    checks = {"history exists before the drill": bool(before)}
    subprocess.run(["ssh", "-o", "BatchMode=yes", "tmc@spark-3", "docker stop -t 10 aiops-prometheus"], check=True,
                   capture_output=True, timeout=60)
    time.sleep(25)
    checks["platform keeps detecting while Prometheus is down"] = platform_scanning()
    subprocess.run(["ssh", "-o", "BatchMode=yes", "tmc@spark-3", "docker start aiops-prometheus"], check=True,
                   capture_output=True, timeout=60)
    t0, up, total = time.time(), 0, 0
    while time.time() - t0 < 120:
        try:
            up, total = targets_up()
            if total and up == total:
                break
        except Exception:
            pass
        time.sleep(5)
    checks["all scrape targets healthy within 2 minutes"] = bool(total) and up == total
    checks["pre-restart history still queryable"] = bool(q("count(aiops_node_up)", t_before))
    recovered_s = round(time.time() - t0)
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print(f"targets {up}/{total} after {recovered_s}s")
    evals.record("observability-recovery", sum(checks.values()), len(checks), checks,
                 {"targets": f"{up}/{total}", "recovery_s": recovered_s})
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())

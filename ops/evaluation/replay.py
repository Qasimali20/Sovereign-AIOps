#!/usr/bin/env python3
"""Replay the diagnosis agent against an incident's *recorded* evidence.

Read-only: nothing is written to the incident store. Use it to evaluate prompt,
model or tool changes against real past incidents before they reach production.

  uv run ops/evaluation/replay.py INC-xxxx [--runs N] [--expect "phrase" ...] [--forbid "phrase" ...]

--expect/--forbid are case-insensitive substrings checked against the
highest-confidence hypothesis; the exit code is non-zero if any run fails.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.incidents import graph
from backend.core import store  # noqa: E402

RANK = {"high": 2, "medium": 1, "low": 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("incident")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--expect", action="append", default=[])
    ap.add_argument("--forbid", action="append", default=[])
    a = ap.parse_args()

    b = store.incident_bundle(a.incident)
    if not b:
        print(f"no such incident {a.incident}")
        return 2
    evidence = [{"id": e["id"], "tool": e["tool"], "source": e["source"], "retrieved_at": e["created"],
                 "data": e["data"]} for e in b["evidence_records"]]
    failures = 0
    for i in range(a.runs):
        diag, meta = graph.run_diagnosis(b["incident"], evidence)
        top = max(diag["hypotheses"], key=lambda h: RANK[h["confidence"]])
        text = top["cause"].lower()
        missing = [x for x in a.expect if x.lower() not in text]
        present = [x for x in a.forbid if x.lower() in text]
        ok = not missing and not present
        failures += not ok
        print(f"run {i + 1}: {'PASS' if ok else 'FAIL'}  ({meta['latency_s']}s)  [{top['confidence']}] {top['cause']}")
        if missing:
            print(f"   missing expected: {missing}")
        if present:
            print(f"   contains forbidden: {present}")
    print(f"{a.runs - failures}/{a.runs} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

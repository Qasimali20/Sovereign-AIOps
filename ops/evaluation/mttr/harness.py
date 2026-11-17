#!/usr/bin/env python3
"""MTTR baseline: the same controlled faults handled with and without AI assistance, measured the same way.

Scenarios (ground truth: root cause -> the one fix that resolves it), all on the designated test service:
  cache_full   cache volume filled            -> clear_test_cache
  saturated    demand above one replica       -> scale_test_service replicas=2
  bad_release  v2 shipped with a config bug   -> rollback_test_deployment
  stopped      container stopped              -> restart_test_service

Every run, either arm:
  t_fault     the harness injects the fault
  t_detect    first failing poll of the detector's own rule, at the detector's cadence (20 s) and random phase
  t_diag      AI: diagnosis recorded by the agent  |  manual: the operator says they know the cause
  t_decision  AI: approval of the proposed fix     |  manual: the operator picks a fix
  t_fixed     the fix finished executing (same restricted test-service functions in both arms)
  t_recovered first of two consecutive healthy checks, observed by the harness itself (not the platform)
Scored: chose the correct fix (diagnosis accuracy), service recovered (remediation success), and, AI arm only,
whether the platform's verifier verdict matched the harness's own observation (verification accuracy).
Control windows (--control) run with no fault: any detection there is a false positive.

AI arm: the real workflow (evidence -> runbooks -> diagnosis -> plan -> policy -> approval -> executor -> verifier)
in a SCRATCH database, approved by a labelled evaluation approver; the live approval queue is never touched.
Manual arm: an operator investigates in the app with the live AI paused for the test service, then picks the fix.
In both arms the live platform's AI is paused for the test service (audited), so nothing leaks between arms.

  .venv/bin/python ops/evaluation/mttr/harness.py --arm ai --repeats 3
  .venv/bin/python ops/evaluation/mttr/harness.py --arm manual --repeats 2 --operator alice   (interactive)
  .venv/bin/python ops/evaluation/mttr/harness.py --control 10                                (false-positive windows)
  .venv/bin/python ops/evaluation/mttr/harness.py --report
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for _l in (ROOT / ".env").read_text().splitlines():          # same environment as the platform service (Loki, LLM, keys)
    if "=" in _l and not _l.lstrip().startswith("#"):
        os.environ.setdefault(*_l.strip().split("=", 1))
scratch = Path(tempfile.mkdtemp(prefix="aiops-mttr-"))
os.environ["AIOPS_DB"] = str(scratch / "mttr.db")          # AI-arm workflow state; set before importing backend
sys.path.insert(0, str(ROOT))
from backend.core import evals, store
from backend.incidents import graph, testsvc  # noqa: E402
from backend.incidents.tools import check_endpoint_health, inspect_container  # noqa: E402

RID, NODE = "aiops-test-svc", "Node-2"
EVAL_APPROVER = "eval-harness"
DETECT_PERIOD_S = 20                       # the live detector's scan interval
RUNS = ROOT / "data" / "mttr-runs.jsonl"
REPORT = ROOT / "ops" / "bench" / "results" / "mttr-baseline.md"   # summarised in docs/evaluation.md
SCENARIOS = {
    "cache_full": ("clear_test_cache", lambda: _sh("docker", "exec", RID, "sh", "-c", "dd if=/dev/zero of=/cache/blob bs=1M count=64 2>/dev/null")),
    "saturated": ("scale_test_service", lambda: _sh("docker", "exec", RID, "sh", "-c", "echo 2 > /state/demand")),
    "bad_release": ("rollback_test_deployment", lambda: testsvc.deploy("v2", EVAL_APPROVER, "mttr: ship the bad release")),
    "stopped": ("restart_test_service", lambda: _sh("docker", "stop", "--timeout", "5", RID)),
}
FIXES = {   # the same restricted operations the executor uses, for the manual arm
    "restart_test_service": lambda by: testsvc.restart(by),
    "clear_test_cache": lambda by: testsvc.clear_cache(by),
    "scale_test_service": lambda by: testsvc.scale(2, by),
    "rollback_test_deployment": lambda by: testsvc.rollback(by),
}


def _sh(*cmd: str) -> None:
    subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def healthy() -> bool:
    return bool(check_endpoint_health(RID)["data"]["ok"])


def reset() -> None:
    """Known-good baseline: v1, one replica, demand 1, empty cache, running."""
    running = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", RID], capture_output=True, text=True).stdout.strip()
    if testsvc.current_release() != "v1" or running != "true":
        testsvc.deploy("v1", EVAL_APPROVER, "mttr reset")
    testsvc.scale(1, EVAL_APPROVER)
    _sh("docker", "exec", RID, "sh", "-c", "echo 1 > /state/demand; rm -rf /cache/*")
    for _ in range(30):
        if healthy():
            return
        time.sleep(1)
    raise RuntimeError("test service did not return to a healthy baseline")


def live_ai_pause(minutes: float | None, reason: str) -> None:
    """Pause/resume the LIVE platform's AI for the test service through its audited API (CLI token)."""
    env = dict(l.strip().split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
    req = urllib.request.Request(f"http://127.0.0.1:{env.get('AIOPS_API_PORT', '5170')}/api/evaluation/ai-pause", method="POST",
                                 data=json.dumps({"resource": RID, "minutes": minutes, "reason": reason}).encode(),
                                 headers={"Content-Type": "application/json", "X-Requested-With": "aiops",
                                          "x-approver-token": env["AIOPS_APPROVER_TOKEN"]})
    urllib.request.urlopen(req, timeout=10).read()


def detect(timeout_s: float = 300) -> float | None:
    """The detector's rule at the detector's cadence, from a random phase (the fault lands anywhere in a scan cycle)."""
    time.sleep(random.uniform(0, DETECT_PERIOD_S))
    end = time.time() + timeout_s
    while time.time() < end:
        if not healthy():
            return time.time()
        time.sleep(DETECT_PERIOD_S)
    return None


def recovered_at(timeout_s: float = 180) -> float | None:
    """First of two consecutive healthy observations, 5 s apart."""
    end, first = time.time() + timeout_s, None
    while time.time() < end:
        if healthy():
            if first is not None:
                return first
            first = time.time()
        else:
            first = None
        time.sleep(5)
    return None


def first_audit(iid: str, event: str) -> float | None:
    with store.conn() as c:
        r = c.execute("SELECT MIN(created) FROM audit_events WHERE incident_id=? AND event=?", (iid, event)).fetchone()
    return r[0]


def run_ai(scenario: str) -> dict:
    expect, inject = SCENARIOS[scenario]
    reset()
    inject()
    t_fault = time.time()
    t_detect = detect()
    rec = {"arm": "ai", "scenario": scenario, "t_fault": t_fault, "t_detect": t_detect}
    if t_detect is None:
        return rec | {"error": "not detected"}
    iid, _ = store.open_incident("service_unhealthy", NODE, RID, f"Test service {RID} failing health check",
                                 f"mttr:{scenario}:{t_fault}", {"health": check_endpoint_health(RID)["data"],
                                                                "container": inspect_container(RID)["data"]})
    first = graph.run_incident(iid)
    p = first.get("proposal") or {}
    rec |= {"incident": iid, "t_diag": first_audit(iid, "diagnosis"), "chosen": p.get("operation"),
            "policy": first.get("policy_decision"), "correct": p.get("operation") == expect}
    pending = [a for a in store.list_approvals("pending") if a["incident_id"] == iid]
    if pending and p.get("operation") not in (None, "none", "escalate_to_human"):
        store.decide_approval(pending[0]["id"], True, EVAL_APPROVER, "MTTR harness (scratch database)")
        rec["t_decision"] = time.time()
        final = graph.resume_incident(iid, pending[0]["id"])
        b = store.incident_bundle(iid)
        rec["t_fixed"] = max((e["created"] for e in b["execution_records"]), default=None)
        rec["verdict"] = (b["verification_results"] or [{}])[-1].get("outcome")
        rec["final"] = final.get("final_status")
    rec["t_recovered"] = recovered_at()
    if rec.get("verdict"):
        rec["verification_correct"] = (rec["verdict"] == "resolved") == (rec["t_recovered"] is not None)
    with store.conn() as c:
        rec["policy_blocks"] = c.execute(
            "SELECT (SELECT COUNT(*) FROM audit_events WHERE incident_id=? AND event='evidence_request_refused') + "
            "(SELECT COUNT(*) FROM policy_decisions d JOIN action_proposals p ON p.id = d.action_id "
            " WHERE p.incident_id=? AND d.decision='deny')", (iid, iid)).fetchone()[0]
    return rec


def run_manual(scenario: str, operator: str) -> dict:
    expect, inject = SCENARIOS[scenario]
    reset()
    inject()
    t_fault = time.time()
    print("\n  A fault has been injected into the test service. Waiting for the detector's rule to see it ...")
    t_detect = detect()
    rec = {"arm": "manual", "scenario": scenario, "operator": operator, "t_fault": t_fault, "t_detect": t_detect}
    if t_detect is None:
        return rec | {"error": "not detected"}
    print("  DETECTED. The AI is paused for the test service. Investigate in the app now: Incidents, Logs,\n"
          "  Services, Metric history (Node-2, aiops-test-svc). Do not look at this script's source.")
    input("  Press Enter when you know the root cause ... ")
    rec["t_diag"] = time.time()
    ops = list(FIXES)
    for i, op in enumerate(ops, 1):
        print(f"    {i}. {op}")
    print(f"    {len(ops) + 1}. not sure / escalate")
    choice = input("  Your fix: ").strip()
    op = ops[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(ops) else None
    rec |= {"t_decision": time.time(), "chosen": op, "correct": op == expect}
    if op:
        FIXES[op](f"human:{operator} (mttr manual arm)")
        rec["t_fixed"] = time.time()
    rec["t_recovered"] = recovered_at()
    print(f"  {'Recovered' if rec['t_recovered'] else 'NOT recovered'}.")
    return rec


def control(minutes: float) -> dict:
    """No fault: the detector's rule should stay quiet. Any detection is a false positive."""
    reset()
    end, polls, hits = time.time() + minutes * 60, 0, 0
    time.sleep(random.uniform(0, DETECT_PERIOD_S))
    while time.time() < end:
        polls += 1
        hits += not healthy()
        time.sleep(DETECT_PERIOD_S)
    return {"arm": "control", "minutes": minutes, "polls": polls, "false_positives": hits, "t_fault": time.time()}


def save(rec: dict) -> None:
    RUNS.parent.mkdir(parents=True, exist_ok=True)
    with RUNS.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("  " + json.dumps({k: (round(v - rec["t_fault"], 1) if k.startswith("t_") and k != "t_fault" and v else v)
                             for k, v in rec.items() if k != "t_fault"}))


# ---------------------------------------------------------------------------------------------------- report
def _dist(xs: list[float]) -> str:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return "–"
    p90 = xs[min(len(xs) - 1, int(0.9 * (len(xs) - 1) + 0.5))]
    return f"{statistics.median(xs):.0f} s (p90 {p90:.0f}, n={len(xs)})"


def report() -> str:
    runs = [json.loads(l) for l in RUNS.read_text().splitlines()] if RUNS.exists() else []
    lines = ["# MTTR baseline: manual vs AI-assisted", "",
             f"Generated {time.strftime('%Y-%m-%d %H:%M')} from `{RUNS.relative_to(ROOT)}` by `ops/evaluation/mttr/harness.py --report`.",
             "Method, scenarios and timestamp definitions: see the harness docstring. Times are medians with p90.", ""]
    rows = []
    for arm in ("manual", "ai"):
        rs = [r for r in runs if r["arm"] == arm and "error" not in r]
        if not rs:
            rows.append((arm, "no runs yet"))
            continue
        d = lambda a, b: [r[b] - r[a] for r in rs if r.get(a) and r.get(b)]
        corr = [r["correct"] for r in rs]
        fixed = [r for r in rs if r.get("t_fixed")]
        ver = [r["verification_correct"] for r in rs if "verification_correct" in r]
        rows.append((arm, {
            "runs": len(rs),
            "MTTD (fault → detected)": _dist(d("t_fault", "t_detect")),
            "MTTI (detected → diagnosis)": _dist(d("t_detect", "t_diag")),
            "Decision (diagnosis → fix chosen/approved)": _dist(d("t_diag", "t_decision")),
            "MTTR (fault → recovered)": _dist(d("t_fault", "t_recovered")),
            "MTTR from detection": _dist(d("t_detect", "t_recovered")),
            "Diagnosis accuracy (correct fix)": f"{sum(corr)}/{len(corr)}",
            "Remediation success (recovered / fixes run)": f"{sum(1 for r in fixed if r.get('t_recovered'))}/{len(fixed)}",
            "Verification accuracy (verdict = observed)": f"{sum(ver)}/{len(ver)}" if ver else "n/a (no platform verifier in this arm)",
            "Policy blocks (refused/denied requests)": sum(r.get("policy_blocks", 0) for r in rs),
        }))
    ctl = [r for r in runs if r["arm"] == "control"]
    polls, fps = sum(r["polls"] for r in ctl), sum(r["false_positives"] for r in ctl)
    keys = list(next((m for _, m in rows if isinstance(m, dict)), {}).keys())
    lines += ["| Metric | Manual | AI-assisted |", "|---|---|---|"]
    get = lambda arm, k: (dict(rows)[arm][k] if isinstance(dict(rows)[arm], dict) else "–")
    lines += [f"| {k} | {get('manual', k)} | {get('ai', k)} |" for k in keys] or ["| (no runs) | | |"]
    lines += ["", f"**False positives (no-fault control):** {fps} detections in {polls} polls"
              + (f" over {sum(r['minutes'] for r in ctl):.0f} min" if ctl else " (no control windows run yet)") + ".", "",
              "## Limitations", "",
              "- One service, four fault types, small samples: report distributions, not a single headline factor.",
              "- The AI arm's approval is scripted (instant) in a scratch database; the manual arm's decision time is human. "
              "Compare *detected → diagnosis* and *MTTR from detection* with that in mind.",
              "- Manual runs depend on the operator and improve with repetition; scenario order is shuffled to limit learning.",
              "- Detection is identical in both arms (same deterministic rule), so MTTD differences are sampling noise.",
              "- Runs go back to back and evidence covers 30 min before each incident, so earlier runs' events are visible. "
              "The one AI misdiagnosis (a stopped container treated as a bad release, 2026-11-16) came from exactly that: "
              "the previous runs' v2 deploy and rollback in the history outweighed the container's exit code 137. "
              "The platform's verifier correctly reported it not resolved.", ""]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines))
    ai = dict(rows).get("ai")
    if isinstance(ai, dict):
        c = [r["correct"] for r in runs if r["arm"] == "ai" and "error" not in r]
        evals.record("mttr-baseline", sum(c), len(c), {"ai": ai, "manual": dict(rows).get("manual")},
                     {"false_positives": f"{fps}/{polls} polls"})
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--arm", choices=("ai", "manual"))
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--operator", default="operator")
    ap.add_argument("--control", type=float, metavar="MINUTES", help="run a no-fault control window")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        print(report())
        return 0
    store.init()
    plan = [s for s in a.scenarios.split(",") for _ in range(a.repeats)] if a.arm else []
    random.shuffle(plan)
    live_ai_pause(240, f"MTTR harness ({a.arm or 'control'})")
    try:
        if a.control:
            save(control(a.control))
        for i, s in enumerate(plan, 1):
            print(f"\n=== run {i}/{len(plan)}: {a.arm} · {s}")
            save(run_ai(s) if a.arm == "ai" else run_manual(s, a.operator))
    finally:
        reset()                                          # always leave the test service healthy on v1
        live_ai_pause(None, "MTTR harness finished")
    print("\n" + report())
    return 0


if __name__ == "__main__":
    sys.exit(main())

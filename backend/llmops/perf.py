"""Inference benchmarks and performance regression detection for chat and vision models.

Every INTERVAL_S each ready chat/vision server (vLLM or SGLang) gets a small, fixed, streamed benchmark sent straight
to the server (not through the gateway, so the engine is measured, not routing):
  short        ~40-token prompt, exactly 128 output tokens  -> time to first token, decode speed (tokens/s)
  long_prompt  ~2,000-token prompt, exactly 16 output tokens -> prefill time and prefill speed
Each scenario runs REPS times at concurrency 1 and the median is kept: 4 requests per server, a few seconds of GPU.
Every prompt starts with a fresh nonce, so the prefix cache cannot make a run look faster than the last.

The benchmark measures a quiet server or nothing: a node whose GPU is at or above MAX_TEMP_C or busier than
MAX_GPU_UTIL, or a server with requests in flight, is skipped with the reason recorded (real traffic is never slowed
down by a benchmark, and a busy GPU would make the numbers meaningless). The fleet limit is 75 C.

Regression: a server's newest run is compared with the median of its previous runs (up to BASELINE_RUNS within
BASELINE_DAYS). Decode speed down by more than DECODE_DROP, or time to first token up by more than TTFT_RISE (and by
at least TTFT_MIN_S), on two runs in a row, raises a model_perf_regression incident. When the server's identity in the
model registry (engine version, weights, context length) changed between the baseline and now, the incident says so.
"""
from __future__ import annotations

import json
import socket
import statistics
import threading
import time
import uuid

import httpx

from backend.core import config, store

INTERVAL_S = 6 * 3600
REPS = 2
MAX_TEMP_C = 70.0
MAX_GPU_UTIL = 30.0
BASELINE_RUNS = 10
BASELINE_DAYS = 14
MIN_BASELINE = 3
DECODE_DROP = 0.25
TTFT_RISE = 0.5
TTFT_MIN_S = 0.15
KINDS = ("LLM", "VLM")
EXCLUDED = {("Node-3", "qwen-chat")}      # another team's stack on Node-3: observed, never loaded
SCENARIOS = {"short": (40, 128), "long_prompt": (1500, 16)}       # (approximate prompt words, output tokens)

SCHEMA = """
CREATE TABLE IF NOT EXISTS perf_bench (
  id TEXT PRIMARY KEY, run_id TEXT, t REAL, node TEXT, model TEXT, port INTEGER, scenario TEXT,
  ttft_s REAL, decode_tps REAL, prefill_tps REAL, e2e_s REAL, prompt_tokens INTEGER, output_tokens INTEGER,
  version_id TEXT, engine_version TEXT, gpu_temp REAL, skipped TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS perf_bench_srv ON perf_bench(node, model, scenario, t);
"""
_run_lock = threading.Lock()
_FILLER = ("The platform keeps an append-only audit trail of every action, approval and verification so that "
           "operators can reconstruct what happened during an incident. ")


def _init() -> None:
    with store.conn() as c:
        c.executescript(SCHEMA)


def prompt(scenario: str) -> str:
    words, _ = SCENARIOS[scenario]
    nonce = uuid.uuid4().hex[:12]                     # first tokens differ every time: no prefix-cache reuse
    if scenario == "short":
        return f"[{nonce}] Write a short paragraph about how a GPU cluster is monitored."
    body = (_FILLER * (words // len(_FILLER.split()) + 1))
    return f"[{nonce}] Summarise the following notes in one sentence.\n\n{body}"


def measure(base: str, model: str, scenario: str, timeout: float = 120) -> dict:
    """One streamed request. TTFT = first generated token (content or reasoning); decode = tokens after the first
    over the time after the first."""
    _, out_tokens = SCENARIOS[scenario]
    body = {"model": model, "messages": [{"role": "user", "content": prompt(scenario)}], "max_tokens": out_tokens,
            "temperature": 0, "stream": True, "ignore_eos": True, "stream_options": {"include_usage": True}}
    t0 = time.perf_counter()
    first = None
    chunks = 0
    usage: dict = {}
    with httpx.stream("POST", f"{base}/v1/chat/completions", json=body, timeout=timeout) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:") or line.strip() == "data: [DONE]":
                continue
            d = json.loads(line[5:])
            usage = d.get("usage") or usage
            for ch in d.get("choices") or []:
                delta = ch.get("delta") or {}
                if delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning"):
                    chunks += 1
                    if first is None:
                        first = time.perf_counter()
    end = time.perf_counter()
    if first is None:
        raise RuntimeError("no tokens returned")
    out = int(usage.get("completion_tokens") or chunks)
    pt = int(usage.get("prompt_tokens") or 0)
    ttft = first - t0
    return {"ttft_s": ttft, "e2e_s": end - t0, "output_tokens": out, "prompt_tokens": pt,
            "decode_tps": (out - 1) / (end - first) if out > 1 and end > first else None,
            "prefill_tps": pt / ttft if pt and ttft > 0 else None}


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def _version(node: str, port: int, model: str) -> tuple[str | None, str | None]:
    try:
        with store.conn() as c:
            r = c.execute("SELECT id, engine_version FROM model_versions WHERE node=? AND port=? AND model=? "
                          "ORDER BY last_seen DESC LIMIT 1", (node, port, model)).fetchone()
    except Exception:
        return None, None
    return (r[0], r[1]) if r else (None, None)


def targets(fleet: dict[str, dict | None]) -> list[dict]:
    """Chat and vision servers, each with why it would be skipped now (None: benchmark it)."""
    out = []
    for n in config.NODES:
        d = fleet.get(n.name) or {}
        gpu = d.get("gpu") or {}
        for s in d.get("vllm") or []:
            if s.get("kind") not in KINDS or s.get("engine", "vllm") not in ("vllm", "sglang") or not s.get("port"):
                continue
            why = None
            if (n.name, s["name"]) in EXCLUDED:
                why = "another team's server: not benchmarked"
            elif s.get("status") != "ready":
                why = f"not ready ({s.get('status')})"
            elif (gpu.get("temp") or 0) >= MAX_TEMP_C:
                why = f"GPU at {gpu['temp']:.0f} C (limit {MAX_TEMP_C:.0f} C)"
            elif (gpu.get("util") or 0) > MAX_GPU_UTIL:
                why = f"GPU busy ({gpu['util']:.0f}%)"
            elif (s.get("running") or 0) > 0 or (s.get("waiting") or 0) > 0:
                why = "serving requests"
            out.append({"node": n.name, "host": n.lan_ip, "model": s["name"], "port": int(s["port"]),
                        "gpu_temp": gpu.get("temp"), "skip": why})
    return out


def _record(c, run_id: str, t: dict, scenario: str, res: dict | None, skipped: str | None, error: str | None) -> None:
    vid, ev = _version(t["node"], t["port"], t["model"])
    r = res or {}
    c.execute("INSERT INTO perf_bench VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (store.new_id("PB"), run_id, time.time(), t["node"], t["model"], t["port"], scenario,
               r.get("ttft_s"), r.get("decode_tps"), r.get("prefill_tps"), r.get("e2e_s"), r.get("prompt_tokens"),
               r.get("output_tokens"), vid, ev, t.get("gpu_temp"), skipped, error))


def run_all(by: str = "schedule") -> dict | None:
    """One benchmark pass over the fleet, one server at a time. Returns a summary, or None if a pass is running."""
    from backend.llmops import model_evals
    from backend.monitoring import metrics
    if not _run_lock.acquire(blocking=False):
        return None
    try:
        _init()
        run_id = store.new_id("PBRUN")
        store.audit(None, by, "perf_bench_started", {"run_id": run_id})
        done = skipped = failed = 0
        fleet = metrics.fleet_raw()
        for t in targets(fleet):
            if t["skip"] is None and model_evals.running():
                t["skip"] = "quality evaluation running"
            if t["skip"] is None and not _reachable(t["host"], t["port"]):
                t["skip"] = "port not reachable from the platform (firewalled or localhost-only)"
            if t["skip"]:
                with store.conn() as c:
                    for sc in SCENARIOS:
                        _record(c, run_id, t, sc, None, t["skip"], None)
                skipped += 1
                continue
            base = f"http://{t['host']}:{t['port']}"
            for sc in SCENARIOS:
                reps, err = [], None
                for _ in range(REPS):
                    try:
                        reps.append(measure(base, t["model"], sc))
                    except Exception as e:      # noqa: BLE001  any failure is recorded, never fatal to the pass
                        err = str(e)[:200]
                        break
                med = None
                if reps:
                    med = {}
                    for k in ("ttft_s", "decode_tps", "prefill_tps", "e2e_s", "prompt_tokens", "output_tokens"):
                        vals = [r[k] for r in reps if r.get(k) is not None]
                        med[k] = statistics.median(vals) if vals else None
                with store.conn() as c:
                    _record(c, run_id, t, sc, med, None, None if reps else err)
                failed += 0 if reps else 1
            done += 1
        summary = {"run_id": run_id, "benchmarked": done, "skipped": skipped, "failed_scenarios": failed}
        store.audit(None, by, "perf_bench_finished", summary)
        return summary
    finally:
        _run_lock.release()


def running() -> bool:
    return _run_lock.locked()


def last_run_at() -> float | None:
    _init()
    with store.conn() as c:
        r = c.execute("SELECT MAX(t) FROM perf_bench").fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------- regression (pure over rows)
def _worse(row: dict, base: dict, scenario: str) -> list[str]:
    out = []
    if scenario == "short" and row.get("decode_tps") and base.get("decode_tps") \
            and row["decode_tps"] < base["decode_tps"] * (1 - DECODE_DROP):
        out.append(f"decode {row['decode_tps']:.1f} tok/s vs usual {base['decode_tps']:.1f}")
    if row.get("ttft_s") and base.get("ttft_s") and row["ttft_s"] > base["ttft_s"] * (1 + TTFT_RISE) \
            and row["ttft_s"] - base["ttft_s"] >= TTFT_MIN_S:
        out.append(f"first token {row['ttft_s']:.2f} s vs usual {base['ttft_s']:.2f} s")
    return out


def assess(rows: list[dict], scenario: str) -> dict | None:
    """rows: one server's successful runs of one scenario, oldest first. A regression needs the two newest runs worse
    than the median of the runs before them."""
    if len(rows) < MIN_BASELINE + 2:
        return None
    prior = rows[:-2][-BASELINE_RUNS:]
    base = {k: statistics.median([r[k] for r in prior if r.get(k)] or [0]) or None for k in ("ttft_s", "decode_tps")}
    last, prev = rows[-1], rows[-2]
    why, why_prev = _worse(last, base, scenario), _worse(prev, base, scenario)
    if not (why and why_prev):
        return None
    versions = {r.get("version_id") for r in prior} - {None}
    changed = last.get("version_id") and versions and last["version_id"] not in versions
    return {"scenario": scenario, "findings": why, "baseline": base, "latest": {k: last.get(k) for k in ("ttft_s", "decode_tps", "t")},
            "baseline_runs": len(prior), "version_changed": bool(changed),
            "engine_version": {"before": sorted({r.get("engine_version") for r in prior} - {None}), "now": last.get("engine_version")}}


def history(days: int = BASELINE_DAYS) -> list[dict]:
    _init()
    with store.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM perf_bench WHERE t >= ? ORDER BY t", (time.time() - days * 86400,))]


def regressions() -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for r in history():
        if r["skipped"] is None and r["error"] is None:
            groups.setdefault((r["node"], r["model"], r["port"], r["scenario"]), []).append(r)
    out = []
    for (node, model, port, sc), rows in groups.items():
        a = assess(rows, sc)
        if a:
            out.append({"node": node, "model": model, "port": port} | a)
    return out


def regression_alerts() -> list:
    from backend.monitoring.services import ServiceAlert
    out = []
    for r in regressions():
        why = "; ".join(r["findings"]) + (" (after a version change)" if r["version_changed"] else "")
        out.append(ServiceAlert("model_perf_regression", r["node"], f"{r['model']}@{r['port']}:{r['scenario']}",
                                f"{r['model']} on {r['node']} slower than usual: {why}", r))
    return out


def latest() -> list[dict]:
    """Each server's newest result per scenario, with its baseline for comparison."""
    groups: dict[tuple, list[dict]] = {}
    for r in history(30):
        groups.setdefault((r["node"], r["model"], r["port"], r["scenario"]), []).append(r)
    out = []
    for (node, model, port, sc), rows in sorted(groups.items()):
        ok = [r for r in rows if r["skipped"] is None and r["error"] is None]
        prior = ok[:-1][-BASELINE_RUNS:]
        base = {k: statistics.median([r[k] for r in prior if r.get(k)] or [0]) or None for k in ("ttft_s", "decode_tps", "prefill_tps")}
        last = rows[-1]
        out.append({"node": node, "model": model, "port": port, "scenario": sc, "t": last["t"], "skipped": last["skipped"],
                    "error": last["error"], "ttft_s": last["ttft_s"], "decode_tps": last["decode_tps"],
                    "prefill_tps": last["prefill_tps"], "prompt_tokens": last["prompt_tokens"],
                    "engine_version": last["engine_version"], "runs": len(ok), "baseline": base,
                    "series": [[r["t"], r["ttft_s"], r["decode_tps"]] for r in ok[-40:]]})
    return out

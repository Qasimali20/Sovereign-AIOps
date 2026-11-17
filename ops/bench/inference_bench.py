"""Priority-lane and overload benchmark for qwen3.6 on Node-2, through the platform gateway.

Method (repeatable; results are written as JSON and a Markdown report):
  - Probe requests: identical prompt (~300 tokens, unique prefix per request so the prompt cache cannot help),
    streamed, max_tokens 128, sent one at a time, alternating the agent lane (client aiops-platform) and the normal
    lane (client load-test). TTFT = first content chunk; ITL = (e2e - TTFT) / (chunks - 1).
  - Idle baseline: probes with nothing else running.
  - Contention runs (R of them): BG normal-lane background clients, unique ~1.5k-token prompts, max_tokens 256,
    for RUN_S seconds; probes start after WARMUP_S. The gateway admits at most 8 normal-lane requests to this model
    at once (GATEWAY_SHARED_INFLIGHT_BY_MODEL); agents bypass that cap.
  - Sampled every 5 s: vLLM running/waiting, KV-cache use, preemptions; GPU temperature, power, utilisation.
  - Thermal guard (Node-2 runs hot): wait for <= COOL_C before each run, abort the run at >= ABORT_C.

Usage: python ops/bench/inference_bench.py <normal-key-file> [runs] [background-clients]
The agent-lane key is the platform's own (AIOPS_GATEWAY_KEY in .env).
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import statistics
import string
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
URL = "http://127.0.0.1:8790/v1/chat/completions"
VLLM_METRICS = "http://127.0.0.1:8001/metrics"
MODEL = "qwen3.6-35b-a3b-fp8"
RUN_S, WARMUP_S, IDLE_PROBES = 90, 15, 10
COOL_C, ABORT_C = 62, 75
WORDS = ("latency replica queue kernel node request token cache service deploy error timeout throughput memory "
         "scheduler gateway upstream retry health probe batch prefill decode").split()


def gpu() -> dict:
    out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu,power.draw,utilization.gpu", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.strip().split(",")
    try:
        return {"temp": float(out[0]), "power": float(out[1]), "util": float(out[2])}
    except (ValueError, IndexError):
        return {"temp": None, "power": None, "util": None}


def vllm() -> dict:
    try:
        t = httpx.get(VLLM_METRICS, timeout=3).text
    except httpx.HTTPError:
        return {}
    def val(name):
        m = re.search(rf"^{re.escape(name)}\{{[^}}]*\}} ([0-9.e+-]+)$", t, re.M)
        return float(m.group(1)) if m else None
    return {"running": val("vllm:num_requests_running"), "waiting": val("vllm:num_requests_waiting"),
            "kv": val("vllm:kv_cache_usage_perc"), "preemptions": val("vllm:num_preemptions_total"),
            "queue_sum": val("vllm:request_queue_time_seconds_sum"), "queue_count": val("vllm:request_queue_time_seconds_count")}


def doc(lines: int) -> str:
    tag = "".join(random.choices(string.ascii_lowercase, k=12))
    body = "\n".join(f"[{tag}] " + " ".join(random.choices(WORDS, k=18)) for _ in range(lines))
    return f"Incident log {tag}:\n{body}\n\nSummarise the likely root cause in three bullet points."


async def probe(c: httpx.AsyncClient, key: str, lane: str) -> dict:
    t0 = time.monotonic()
    ttft = None
    chunks = 0
    status = 0
    try:
        async with c.stream("POST", URL, headers={"Authorization": f"Bearer {key}"}, timeout=300,
                            json={"model": MODEL, "stream": True, "max_tokens": 128, "temperature": 0.7,
                                  "messages": [{"role": "user", "content": doc(16)}]}) as r:
            status = r.status_code
            async for line in r.aiter_lines():
                if line.startswith("data: ") and '"content"' in line:
                    chunks += 1
                    if ttft is None:
                        ttft = time.monotonic() - t0
    except httpx.HTTPError as e:
        return {"lane": lane, "status": 0, "error": type(e).__name__, "t": time.time()}
    e2e = time.monotonic() - t0
    itl = (e2e - ttft) / (chunks - 1) if ttft is not None and chunks > 1 else None
    return {"lane": lane, "status": status, "ttft": ttft, "e2e": e2e, "itl": itl, "chunks": chunks, "t": time.time()}


async def background(c: httpx.AsyncClient, key: str, end: float, stats: dict) -> None:
    while time.time() < end:
        try:
            r = await c.post(URL, headers={"Authorization": f"Bearer {key}"}, timeout=300,
                             json={"model": MODEL, "max_tokens": 256, "temperature": 0.7,
                                   "messages": [{"role": "user", "content": doc(80)}]})
            stats[r.status_code] = stats.get(r.status_code, 0) + 1
        except httpx.HTTPError as e:
            stats[type(e).__name__] = stats.get(type(e).__name__, 0) + 1


async def sampler(end: float, out: list, abort: asyncio.Event) -> None:
    while time.time() < end and not abort.is_set():
        g = gpu()
        out.append({"t": time.time(), **g, **vllm()})
        if g["temp"] is not None and g["temp"] >= ABORT_C:
            print(f"  thermal guard: GPU {g['temp']} C >= {ABORT_C} C, stopping this run", flush=True)
            abort.set()
        await asyncio.sleep(5)


def cool_down() -> None:
    t = gpu()["temp"]
    waited = 0
    while t is not None and t > COOL_C and waited < 900:
        print(f"  cooling: GPU {t} C, waiting for <= {COOL_C} C", flush=True)
        time.sleep(20); waited += 20
        t = gpu()["temp"]


async def run(prio_key: str, norm_key: str, bg: int, label: str) -> dict:
    cool_down()
    samples: list = []
    probes: list = []
    bg_stats: dict = {}
    abort = asyncio.Event()
    start = time.time()
    end = start + (RUN_S if bg else 10 ** 6)
    before = vllm()
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=bg + 8)) as c:
        tasks = [asyncio.create_task(background(c, norm_key, end, bg_stats)) for _ in range(bg)]
        samp = asyncio.create_task(sampler(end if bg else time.time() + 600, samples, abort))
        if bg:
            await asyncio.sleep(WARMUP_S)
        i = 0
        while not abort.is_set() and (time.time() < end if bg else i < IDLE_PROBES):
            probes.append(await probe(c, prio_key if i % 2 == 0 else norm_key, "agent" if i % 2 == 0 else "normal"))
            i += 1
        abort.set() if not bg else None
        end = time.time()            # stop the background as soon as probing ends (or the guard fires)
        for t in tasks:
            t.cancel()
        samp.cancel()
        await asyncio.gather(*tasks, samp, return_exceptions=True)
    after = vllm()
    q_n = (after.get("queue_count") or 0) - (before.get("queue_count") or 0)
    q_s = (after.get("queue_sum") or 0) - (before.get("queue_sum") or 0)
    res = {"label": label, "background_clients": bg, "seconds": round(time.time() - start, 1), "probes": probes,
           "background_status": {str(k): v for k, v in bg_stats.items()}, "samples": samples,
           "vllm_queue_mean_s": round(q_s / q_n, 3) if q_n else None,
           "preemptions": (after.get("preemptions") or 0) - (before.get("preemptions") or 0),
           "thermal_abort": any(s.get("temp") and s["temp"] >= ABORT_C for s in samples)}
    print(f"  {label}: {len(probes)} probes, background {res['background_status']}, "
          f"max GPU {max((s['temp'] or 0) for s in samples) if samples else '?'} C", flush=True)
    return res


def pct(v: list[float], q: float):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 3)


def summarise(runs: list[dict]) -> dict:
    out = {}
    for phase, rs in (("idle", [r for r in runs if not r["background_clients"]]),
                      ("contention", [r for r in runs if r["background_clients"]])):
        for lane in ("agent", "normal"):
            ps = [p for r in rs for p in r["probes"] if p["lane"] == lane]
            ok = [p for p in ps if p.get("status") == 200]
            out[f"{phase}/{lane}"] = {
                "n": len(ps), "failed": len(ps) - len(ok),
                **{f"{m}_{q}": pct([p.get(m) for p in ok], qv) for m in ("ttft", "itl", "e2e")
                   for q, qv in (("p50", .5), ("p95", .95), ("p99", .99))}}
    cont = [r for r in runs if r["background_clients"]]
    smp = [s for r in cont for s in r["samples"]]
    bg = {}
    for r in cont:
        for k, v in r["background_status"].items():
            bg[k] = bg.get(k, 0) + v
    out["contention/system"] = {
        "runs": len(cont), "background_requests": bg,
        "per_run_agent_ttft_p50": [pct([p.get("ttft") for p in r["probes"] if p["lane"] == "agent" and p.get("status") == 200], .5) for r in cont],
        "per_run_normal_ttft_p50": [pct([p.get("ttft") for p in r["probes"] if p["lane"] == "normal" and p.get("status") == 200], .5) for r in cont],
        "vllm_queue_mean_s": [r["vllm_queue_mean_s"] for r in cont],
        "kv_max_pct": round(100 * max((s.get("kv") or 0) for s in smp), 1) if smp else None,
        "running_max": max((s.get("running") or 0) for s in smp) if smp else None,
        "waiting_max": max((s.get("waiting") or 0) for s in smp) if smp else None,
        "preemptions": sum(r["preemptions"] for r in cont),
        "gpu_temp_max": max((s.get("temp") or 0) for s in smp) if smp else None,
        "gpu_power_mean": round(statistics.mean(s["power"] for s in smp if s.get("power")), 1) if smp else None,
        "thermal_aborts": sum(r["thermal_abort"] for r in cont),
    }
    return out


def main() -> None:
    norm_key = open(sys.argv[1]).read().strip()
    runs_n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    bg = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    env = dict(l.strip().split("=", 1) for l in open(ROOT / ".env") if "=" in l and not l.startswith("#"))
    prio_key = os.environ.get("AIOPS_GATEWAY_KEY") or env["AIOPS_GATEWAY_KEY"]
    stamp = time.strftime("%Y%m%d-%H%M")
    runs = []
    print("idle baseline", flush=True)
    runs.append(asyncio.run(run(prio_key, norm_key, 0, "idle")))
    for i in range(runs_n):
        print(f"contention run {i + 1}/{runs_n}", flush=True)
        runs.append(asyncio.run(run(prio_key, norm_key, bg, f"contention-{i + 1}")))
    summary = summarise(runs)
    meta = {"model": MODEL, "node": "Node-2", "engine": "vLLM (systemd vllm-qwen36, TP=1, FP8)", "started": stamp,
            "runs": runs_n, "background_clients": bg, "run_seconds": RUN_S, "warmup_seconds": WARMUP_S,
            "probe": "streamed, ~300-token prompt with unique prefix, max_tokens 128",
            "background": "~1.5k-token unique prompts, max_tokens 256",
            "shared_cap": 8, "thermal_guard": {"cool_to_c": COOL_C, "abort_at_c": ABORT_C}}
    out = ROOT / "ops" / "bench" / "results" / f"inference-{stamp}.json"
    out.write_text(json.dumps({"meta": meta, "summary": summary, "runs": runs}, indent=1))
    print(json.dumps(summary, indent=1))
    print("written", out)


if __name__ == "__main__":
    main()

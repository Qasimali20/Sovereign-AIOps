#!/usr/bin/env python3
"""Gateway overhead benchmark: the same streaming chat workload sent directly to the
replica and through the gateway, at several concurrency levels.

Fixed-length outputs (ignore_eos) so both paths generate the same number of tokens.
Writes a Markdown report to ops/bench/results/ (summarised in docs/evaluation.md).

  set -a; . ./.env; set +a; uv run ops/evaluation/bench_gateway.py [--levels 1,8,32] [--tokens 128]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.config import NODE_BY_NAME  # noqa: E402  fleet addresses come from .env
NODE2 = NODE_BY_NAME["Node-2"].lan_ip
DIRECT = ("direct", f"http://{NODE2}:8001/v1", "qwen3.6-35b-a3b-fp8", {})
GATEWAY = ("gateway", f"http://{NODE2}:8790/v1", "chat", {"Authorization": f"Bearer {os.environ['AIOPS_GATEWAY_KEY']}"})
PROMPT = "Explain in plain words how a site reliability engineer investigates an outage, step by step."


async def one(c: httpx.AsyncClient, base: str, model: str, headers: dict, tokens: int) -> dict:
    body = {"model": model, "stream": True, "max_tokens": tokens, "temperature": 0, "ignore_eos": True,
            "stream_options": {"include_usage": True}, "messages": [{"role": "user", "content": PROMPT}]}
    t0 = time.perf_counter()
    ttft, out_tokens = None, 0
    async with c.stream("POST", f"{base}/chat/completions", json=body, headers=headers, timeout=300) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            ev = json.loads(line[6:])
            if ttft is None and ev.get("choices") and ev["choices"][0].get("delta", {}).get("content"):
                ttft = time.perf_counter() - t0
            if ev.get("usage"):
                out_tokens = ev["usage"]["completion_tokens"]
    total = time.perf_counter() - t0
    return {"ttft": ttft or total, "total": total, "tokens": out_tokens,
            "itl": (total - (ttft or total)) / max(1, out_tokens - 1)}


async def level(target, concurrency: int, tokens: int) -> dict:
    name, base, model, headers = target
    n = max(8, concurrency * 2)
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=concurrency + 4)) as c:
        await one(c, base, model, headers, 8)                             # warm the path
        async def run():
            async with sem:
                return await one(c, base, model, headers, tokens)
        t0 = time.perf_counter()
        rs = await asyncio.gather(*(run() for _ in range(n)))
        wall = time.perf_counter() - t0
    p = lambda xs, q: sorted(xs)[min(len(xs) - 1, int(q * len(xs)))]
    ttft = [r["ttft"] for r in rs]
    return {"path": name, "concurrency": concurrency, "requests": n, "tokens_per_request": statistics.mean(r["tokens"] for r in rs),
            "throughput_tok_s": sum(r["tokens"] for r in rs) / wall, "ttft_p50": p(ttft, .5), "ttft_p95": p(ttft, .95),
            "itl_ms_p50": 1000 * p([r["itl"] for r in rs], .5), "latency_p50": p([r["total"] for r in rs], .5)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="1,8,32")
    ap.add_argument("--tokens", type=int, default=128)
    a = ap.parse_args()
    rows = []
    for conc in map(int, a.levels.split(",")):
        for target in (DIRECT, GATEWAY):                                 # interleaved so drift hits both
            r = await level(target, conc, a.tokens)
            rows.append(r)
            print(f"{r['path']:8} c={conc:<3} {r['throughput_tok_s']:7.1f} tok/s  TTFT p50 {r['ttft_p50']*1000:6.0f} ms  "
                  f"p95 {r['ttft_p95']*1000:6.0f} ms  ITL p50 {r['itl_ms_p50']:5.1f} ms  latency p50 {r['latency_p50']:.2f}s")
    lines = [f"# Model gateway overhead benchmark", "",
             f"{datetime.now():%Y-%m-%d %H:%M} · model `qwen3.6-35b-a3b-fp8` on Node-2 · streaming, {a.tokens} output tokens "
             "per request (ignore_eos) · client on Node-2 · gateway and model on the same node, called via the LAN IP", "",
             "| Concurrency | Path | Throughput (tok/s) | TTFT p50 | TTFT p95 | ITL p50 | Latency p50 |",
             "|---:|---|---:|---:|---:|---:|---:|"]
    for r in rows:
        lines.append(f"| {r['concurrency']} | {r['path']} | {r['throughput_tok_s']:.1f} | {r['ttft_p50']*1000:.0f} ms | "
                     f"{r['ttft_p95']*1000:.0f} ms | {r['itl_ms_p50']:.1f} ms | {r['latency_p50']:.2f} s |")
    lines += ["", "## Gateway overhead (gateway − direct)", "", "| Concurrency | ΔTTFT p50 | ΔThroughput |", "|---:|---:|---:|"]
    for conc in map(int, a.levels.split(",")):
        d, g = (next(r for r in rows if r["concurrency"] == conc and r["path"] == p) for p in ("direct", "gateway"))
        lines.append(f"| {conc} | {1000 * (g['ttft_p50'] - d['ttft_p50']):+.1f} ms | "
                     f"{100 * (g['throughput_tok_s'] / d['throughput_tok_s'] - 1):+.1f}% |")
    out = ROOT / "ops" / "bench" / "results" / f"gateway-{datetime.now():%Y%m%d-%H%M}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    print(f"\nreport: {out}")


if __name__ == "__main__":
    asyncio.run(main())

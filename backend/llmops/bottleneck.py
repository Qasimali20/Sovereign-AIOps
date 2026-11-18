"""Where each chat/vision model server spends its time, from its own vLLM metrics in Prometheus (passive, no load).

For each server over a window: request rate, concurrency, KV-cache use, preemptions, the split of request time into
queue / prefill / decode, time to first token, inter-token latency, prompt and output lengths and prefix-cache hit
rate; for its node: GPU utilisation and the other servers producing tokens at the same time. `classify` turns those
numbers into findings with the evidence and what to change. It is pure, so every rule is testable without a fleet.

GB10 notes the rules rely on: decode speed is bound by memory bandwidth (about 273 GB/s, shared by every server on
the node), and all servers on a node share one GPU, so a busy neighbour slows decode for everyone.
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor

import httpx

from backend.core import config

WINDOWS = {"1h": "1h", "6h": "6h", "24h": "1d", "7d": "7d"}
MIN_REQUESTS = 5                 # fewer completed requests than this in the window: too little traffic to judge
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="bottleneck")

_R = lambda m, w: f"sum by (node, model) (rate(vllm:{m}[{w}]))"                             # noqa: E731
_MEAN = lambda m, w: f"({_R(m + '_sum', w)}) / ({_R(m + '_count', w)})"                       # noqa: E731
QUERIES = {
    "requests": lambda w: f"sum by (node, model) (increase(vllm:request_success_total[{w}]))",
    "req_per_s": lambda w: _R("request_success_total", w),
    "running_max": lambda w: f"max by (node, model) (max_over_time(vllm:num_requests_running[{w}]))",
    "running_avg": lambda w: f"max by (node, model) (avg_over_time(vllm:num_requests_running[{w}]))",
    "waiting_max": lambda w: f"max by (node, model) (max_over_time(vllm:num_requests_waiting[{w}]))",
    "waiting_avg": lambda w: f"max by (node, model) (avg_over_time(vllm:num_requests_waiting[{w}]))",
    "kv_max": lambda w: f"max by (node, model) (max_over_time(vllm:kv_cache_usage_perc[{w}]))",
    "kv_avg": lambda w: f"max by (node, model) (avg_over_time(vllm:kv_cache_usage_perc[{w}]))",
    "preemptions": lambda w: f"sum by (node, model) (increase(vllm:num_preemptions_total[{w}]))",
    "queue_s": lambda w: _MEAN("request_queue_time_seconds", w),
    "prefill_s": lambda w: _MEAN("request_prefill_time_seconds", w),
    "decode_s": lambda w: _MEAN("request_decode_time_seconds", w),
    "e2e_s": lambda w: _MEAN("e2e_request_latency_seconds", w),
    "ttft_s": lambda w: _MEAN("time_to_first_token_seconds", w),
    "ttft_p95_s": lambda w: f"histogram_quantile(0.95, sum by (le, node, model) (rate(vllm:time_to_first_token_seconds_bucket[{w}])))",
    "itl_s": lambda w: _MEAN("inter_token_latency_seconds", w),
    "prompt_len": lambda w: _MEAN("request_prompt_tokens", w),
    "output_len": lambda w: _MEAN("request_generation_tokens", w),
    "gen_tps": lambda w: _R("generation_tokens_total", w),
    "prompt_tps": lambda w: _R("prompt_tokens_total", w),
    "prefix_hit": lambda w: f"({_R('prefix_cache_hits_total', w)}) / ({_R('prefix_cache_queries_total', w)})",
}
NODE_QUERIES = {
    "gpu_avg": lambda w: f"max by (node) (avg_over_time(aiops_gpu_utilization_percent[{w}]))",
    "gpu_max": lambda w: f"max by (node) (max_over_time(aiops_gpu_utilization_percent[{w}]))",
}


def _query(expr: str) -> list[dict]:
    r = httpx.get(f"{config.PROMETHEUS_URL}/api/v1/query", params={"query": expr}, timeout=15)
    r.raise_for_status()
    return r.json()["data"]["result"]


def _num(v: str) -> float | None:
    x = float(v)
    return None if math.isnan(x) or math.isinf(x) else x


def collect(window: str = "1h") -> dict:
    """{(node, model): stats} and {node: gpu stats} for chat/vision servers over the window."""
    w = WINDOWS[window]
    names = list(QUERIES) + list(NODE_QUERIES)
    exprs = [QUERIES[k](w) for k in QUERIES] + [NODE_QUERIES[k](w) for k in NODE_QUERIES]
    results = dict(zip(names, _pool.map(_query, exprs)))
    servers: dict[tuple[str, str], dict] = {}
    nodes: dict[str, dict] = {}
    for k in QUERIES:
        for s in results[k]:
            key = (s["metric"].get("node"), s["metric"].get("model"))
            servers.setdefault(key, {})[k] = _num(s["value"][1])
    for k in NODE_QUERIES:
        for s in results[k]:
            nodes.setdefault(s["metric"].get("node"), {})[k] = _num(s["value"][1])
    return {"servers": servers, "nodes": nodes}


# ---------------------------------------------------------------- rules (pure)
def classify(s: dict, node: dict, neighbours: list[tuple[str, float]], bench_decode_tps: float | None = None) -> list[dict]:
    """Findings for one server, most important first. `neighbours`: (model, tokens/s) of other servers on its node."""
    g = lambda k: s.get(k) or 0.0                                                             # noqa: E731
    if g("requests") < MIN_REQUESTS:
        return [{"rule": "idle", "severity": "info", "title": "Too little traffic to judge",
                 "detail": f"{g('requests'):.0f} requests completed in the window."}]
    out = []
    e2e = g("e2e_s") or (g("queue_s") + g("prefill_s") + g("decode_s")) or None
    share = (lambda k: g(k) / e2e) if e2e else (lambda k: 0.0)
    if share("queue_s") > 0.2 or g("waiting_avg") > 0.5:
        out.append({"rule": "queueing", "severity": "warning", "title": "Requests wait for a slot",
                    "detail": f"{share('queue_s'):.0%} of request time is spent queued ({g('queue_s'):.2f} s on average); "
                              f"up to {g('waiting_max'):.0f} waiting while {g('running_max'):.0f} ran.",
                    "action": "The server is at its concurrency or KV-cache limit. If KV use is high, see below; otherwise "
                              "raise --max-num-seqs, or add a replica on another node (the gateway balances them)."})
    if g("kv_max") >= 0.9 or g("preemptions") > 0:
        out.append({"rule": "kv_pressure", "severity": "warning", "title": "KV cache full",
                    "detail": f"KV cache peaked at {g('kv_max'):.0%}; {g('preemptions'):.0f} preemptions (requests evicted "
                              "and recomputed).",
                    "action": "Give the server more memory (--gpu-memory-utilization, if the node has room: see Capacity), "
                              "or lower --max-model-len or --max-num-seqs."})
    busy = [(m, t) for m, t in neighbours if t >= 5]
    if busy and (node.get("gpu_max") or 0) >= 90:
        out.append({"rule": "gpu_shared", "severity": "warning" if share("decode_s") > 0.5 else "info",
                    "title": "GPU shared with other busy servers",
                    "detail": f"GPU peaked at {node['gpu_max']:.0f}% while " + ", ".join(f"{m} ({t:.0f} tok/s)" for m, t in busy)
                              + " also generated tokens on this node.",
                    "action": "Every server on a GB10 shares one GPU and its memory bandwidth. Move the busiest neighbour "
                              "to a less loaded node, or keep latency-sensitive models apart."})
    if bench_decode_tps and g("itl_s"):
        live = 1 / g("itl_s")
        if live < bench_decode_tps * 0.6:
            out.append({"rule": "slow_under_load", "severity": "info", "title": "Decode slower under load than idle",
                        "detail": f"{live:.0f} tok/s per request in production against {bench_decode_tps:.0f} tok/s in the "
                                  f"idle benchmark; {g('running_avg'):.1f} requests in flight on average.",
                        "action": "Expected with batching: throughput rises while each request slows. If per-request speed "
                                  "matters more, lower --max-num-seqs."})
    if share("prefill_s") > 0.5:
        hit = s.get("prefix_hit")
        out.append({"rule": "prefill_bound", "severity": "info", "title": "Prompt processing dominates",
                    "detail": f"{share('prefill_s'):.0%} of request time is prefill; prompts average {g('prompt_len'):.0f} "
                              f"tokens, outputs {g('output_len'):.0f}" + (f"; prefix cache hit rate {hit:.0%}" if hit is not None else "") + ".",
                    "action": "Long prompts: keep shared instructions at the start of the prompt so the prefix cache can "
                              "reuse them (--enable-prefix-caching), or shorten the context sent."})
    elif share("decode_s") > 0.7:
        out.append({"rule": "decode_bound", "severity": "info", "title": "Token generation dominates (normal)",
                    "detail": f"{share('decode_s'):.0%} of request time is decode at {1 / g('itl_s'):.0f} tok/s per request; "
                              f"outputs average {g('output_len'):.0f} tokens." if g("itl_s") else
                              f"{share('decode_s'):.0%} of request time is decode.",
                    "action": "On GB10 decode speed is bound by memory bandwidth. Shorter outputs (max_tokens), a smaller "
                              "or more quantised model, or speculative decoding are what make it faster."})
    if not out:
        out.append({"rule": "healthy", "severity": "info", "title": "No bottleneck",
                    "detail": f"{share('queue_s'):.0%} queued, KV peak {g('kv_max'):.0%}, first token {g('ttft_s'):.2f} s on average."})
    rank = {"warning": 0, "info": 1}
    return sorted(out, key=lambda f: rank[f["severity"]])


def analyse(window: str = "1h") -> list[dict]:
    """Every chat/vision server Prometheus knows, with its numbers and findings."""
    from backend.llmops import perf
    from backend.monitoring import metrics
    data = collect(window)
    kinds = {(n, s.get("name")): s.get("kind") for n, d in metrics.fleet_raw().items() for s in (d or {}).get("vllm") or []}
    bench = {(r["node"], r["model"]): (r["baseline"] or {}).get("decode_tps") or r["decode_tps"]
             for r in perf.latest() if r["scenario"] == "short"}
    out = []
    for (node, model), s in sorted(data["servers"].items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")):
        kind = kinds.get((node, model))
        if kind not in perf.KINDS:
            continue                         # not serving now, or embedding / rerank / speech: no decode to analyse
        neighbours = [(m, x.get("gen_tps") or 0.0) for (n, m), x in data["servers"].items() if n == node and m != model]
        out.append({"node": node, "model": model, "kind": kind, "window": window, "stats": s,
                    "gpu": data["nodes"].get(node, {}), "findings": classify(s, data["nodes"].get(node, {}), neighbours,
                                                                             bench.get((node, model)))})
    return out

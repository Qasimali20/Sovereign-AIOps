"""Read-side client for the platform's Prometheus (Node-3): long-range history for the web UI."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from backend.core import config

RANGES = {"6h": (6 * 3600, 120), "7d": (7 * 86400, 1800), "30d": (30 * 86400, 7200)}      # range -> (seconds, step)
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="promql")


def query_range(expr: str, start: float, end: float, step: int) -> list[dict]:
    r = httpx.get(f"{config.PROMETHEUS_URL}/api/v1/query_range",
                  params={"query": expr, "start": start, "end": end, "step": step}, timeout=10)
    r.raise_for_status()
    return r.json()["data"]["result"]


def _series(result: list[dict]) -> dict[float, float]:
    return {float(t): float(v) for s in result[:1] for t, v in s["values"]}


def node_history(node: str, range_: str) -> dict:
    """Same shape as Spark Monitor's /history: points [t, gpu, cpu, mem%]; model points
    [t, tok/s, prompt tok/s, running, waiting, kv, ttft_p95, e2e_p95, req/min] (fields Prometheus lacks are null)."""
    span, step = RANGES[range_]
    end = time.time() // step * step
    start = end - span
    sel = f'{{node="{node}"}}'
    node_q = [f"aiops_gpu_utilization_percent{sel}", f"aiops_node_cpu_percent{sel}",
              f"100 * aiops_node_memory_used_bytes{sel} / aiops_node_memory_total_bytes{sel}"]
    # GPU and CPU: the peak in each step (a burst shorter than the step must still show); memory: the average.
    # max by (node): one line per node even when the scrape target changed address (e.g. the console moved port)
    fns = ["max_over_time", "max_over_time", "avg_over_time"]
    cols = list(_pool.map(lambda fq: _series(query_range(f"max by (node) ({fq[0]}(({fq[1]})[{step}s:]))", start, end, step)),
                          zip(fns, node_q)))
    ts = sorted(set().union(*cols))
    points = [[t, *(c.get(t) for c in cols)] for t in ts]
    model_fields = ["aiops_model_generation_tokens_per_second", None, "aiops_model_requests_running",
                    "aiops_model_requests_waiting", "aiops_model_kv_cache_usage_ratio", "aiops_model_ttft_p95_seconds",
                    None, "aiops_model_requests_per_minute"]
    models: dict[str, dict] = {}
    for f in [m for m in model_fields if m]:
        for s in query_range(f"avg_over_time({f}{sel}[{step}s])", start, end, step):
            lab = s["metric"]
            key = f'{lab.get("model")}@{lab.get("port")}'
            m = models.setdefault(key, {"name": lab.get("model"), "kind": lab.get("kind"), "port": int(lab.get("port") or 0), "cols": {}})
            m["cols"][f] = {float(t): float(v) for t, v in s["values"]}
    out_models = {}
    for key, m in models.items():
        mts = sorted(set().union(*m["cols"].values()))
        out_models[key] = {"name": m["name"], "kind": m["kind"], "port": m["port"],
                           "points": [[t, *((m["cols"].get(f) or {}).get(t) if f else None for f in model_fields)] for t in mts]}
    return {"points": points, "models": out_models}


# ---------------------------------------------------------------- incident evidence
WINDOW_STEP = 60
WINDOW_MAX_POINTS = 24


def _summary(values: list[tuple[float, float]]) -> dict:
    v = [x for _, x in values]
    every = max(1, len(values) // WINDOW_MAX_POINTS)
    return {"min": round(min(v), 3), "max": round(max(v), 3), "avg": round(sum(v) / len(v), 3), "last": round(v[-1], 3),
            "points": [[time.strftime("%H:%M", time.localtime(t)), round(x, 3)] for t, x in values[::every]]}


def incident_window(node: str, start: float, end: float) -> dict:
    """Fixed PromQL for one node over an incident window: host load plus every model server on it.
    Series are summarised (min/max/avg/last + <=24 points) so evidence stays small."""
    if node not in config.NODE_BY_NAME:
        raise ValueError(f"{node!r} is not a known node")
    n = f'node="{node}"'
    q95 = lambda m: f'histogram_quantile(0.95, sum by (model, le) (rate(vllm:{m}_bucket{{{n}}}[5m])))'
    queries = {
        "gpu_util_percent": f"aiops_gpu_utilization_percent{{{n}}}",
        "cpu_percent": f"aiops_node_cpu_percent{{{n}}}",
        "memory_used_percent": f"100 * aiops_node_memory_used_bytes{{{n}}} / aiops_node_memory_total_bytes{{{n}}}",
        "node_up": f"aiops_node_up{{{n}}}",
        "model_requests_running": f"sum by (model) (vllm:num_requests_running{{{n}}})",
        "model_requests_waiting": f"sum by (model) (vllm:num_requests_waiting{{{n}}})",
        "model_requests_per_min": f"sum by (model) (rate(vllm:request_success_total{{{n}}}[5m])) * 60",
        "model_ttft_p95_s": q95("time_to_first_token_seconds"),
        "model_e2e_p95_s": q95("e2e_request_latency_seconds"),
        "model_kv_cache_percent": f"100 * max by (model) (vllm:kv_cache_usage_perc{{{n}}})",
    }
    end = end // WINDOW_STEP * WINDOW_STEP + WINDOW_STEP
    out: dict = {}
    missing = []

    def run(item):
        name, expr = item
        try:
            return name, query_range(expr, start, end, WINDOW_STEP), None
        except Exception as e:  # one failing query is reported, not hidden
            return name, [], f"{type(e).__name__}: {e}"
    for name, result, err in _pool.map(run, queries.items()):
        if err:
            missing.append(f"{name}: {err}")
            continue
        series = {}
        for s in result:
            vals = [(float(t), float(v)) for t, v in s["values"] if v not in ("NaN", "+Inf", "-Inf")]
            if vals:
                series[s["metric"].get("model", node)] = _summary(vals)
        out[name] = series or "no data in window"
    latest = last_sample(node)
    return {"node": node, "window": {"start": start, "end": end, "step_s": WINDOW_STEP},
            "data_until": latest, "queries": queries, "series": out, "errors": missing}


def last_sample(node: str) -> float | None:
    """Time of the newest scrape Prometheus holds for this node (None if it cannot say)."""
    try:
        r = httpx.get(f"{config.PROMETHEUS_URL}/api/v1/query",
                      params={"query": f'timestamp(aiops_node_up{{node="{node}"}})'}, timeout=5)
        res = r.json()["data"]["result"]
        return float(res[0]["value"][1]) if res else None
    except Exception:
        return None


def wait_for_scrape(node: str, after: float, max_wait_s: float = 75.0) -> float | None:
    """Prometheus scrapes every 15-20 s, the detector reads live data: wait (bounded) until Prometheus has
    a sample from after `after`, so an incident's metric window is not cut off before the problem shows."""
    deadline = time.time() + max_wait_s
    while True:
        t = last_sample(node)
        if (t is not None and t >= after) or time.time() >= deadline:
            return t
        time.sleep(5)

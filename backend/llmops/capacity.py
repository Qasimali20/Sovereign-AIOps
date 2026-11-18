"""Capacity: how much room each node has now, how fast it is filling, and how busy each model is getting.

Memory (GB10 unified memory, shared by CPU, GPU and every model server on the node):
  used now, the 7-day peak, free now, and the largest --gpu-memory-utilization a new or restarted vLLM server could
  ask for right now. vLLM refuses to start when free memory is below that share of the total; this is what stopped
  Qwen3.6 on Node-4 after the 18 Nov reboot, when other servers had started first. The reserved shares the servers
  were started with are listed, but measured free memory is what decides: a multi-stage server (vLLM-Omni) can hold
  more than its flag says.
Disk: the typical daily change over the last 7 days (median of the day-to-day changes, so a one-off model download
  does not read as a daily rate), days until 85% (the detector's limit) at that pace, and the largest single-day
  jump with how many more jumps like it fit below 85%. Model weights arrive in jumps of tens to hundreds of GB.
Models: requests per day for the last 7 days, the trend, and the busiest the server has been (peak requests in
  flight and KV-cache use over 7 days), which is how close it came to its limit.

Forecasts extrapolate a week of history: they say where the trend leads, not what will happen.
"""
from __future__ import annotations

import math
import time

import httpx

from backend.core import config

GIB = 1024 ** 3
DISK_FORECAST_WARN_DAYS = 14          # disk projected to reach DISK_PCT_WARN sooner than this: an incident
START_MARGIN = 0.02                   # vLLM's own estimate is not exact: keep 2% back when saying what fits
MEM_TIGHT_GB = 8.0                    # less free than this: a restart of any model server may not fit


def _q(expr: str) -> dict[str, float]:
    """Instant query -> {node or node|model: value}."""
    r = httpx.get(f"{config.PROMETHEUS_URL}/api/v1/query", params={"query": expr}, timeout=15)
    r.raise_for_status()
    out = {}
    for s in r.json()["data"]["result"]:
        m = s["metric"]
        key = m.get("node", "") + (f"|{m['model']}" if "model" in m else "")
        v = float(s["value"][1])
        if not (math.isnan(v) or math.isinf(v)):
            out[key] = v
    return out


def _q_range(expr: str, days: int = 7) -> dict[str, list[float]]:
    end = time.time() // 86400 * 86400 + 86400
    r = httpx.get(f"{config.PROMETHEUS_URL}/api/v1/query_range",
                  params={"query": expr, "start": end - days * 86400, "end": end, "step": 86400}, timeout=20)
    r.raise_for_status()
    out = {}
    for s in r.json()["data"]["result"]:
        m = s["metric"]
        out[m.get("node", "") + (f"|{m['model']}" if "model" in m else "")] = [float(v) for _, v in s["values"]]
    return out


# ---------------------------------------------------------------- pure
def disk_forecast(used: float, total: float, daily: list[float]) -> dict:
    """`daily`: disk used (bytes) at the end of each of the last days, oldest first."""
    pct = 100 * used / total if total else None
    warn_at = total * config.DISK_PCT_WARN / 100
    deltas = [b - a for a, b in zip(daily, daily[1:])]
    typical = sorted(deltas)[len(deltas) // 2] if len(deltas) >= 3 else None
    jump = max(deltas, default=0.0)
    out = {"used_gb": used / 1e9, "total_gb": total / 1e9, "pct": pct, "days_of_history": len(deltas),
           "growth_gb_per_day": None if typical is None else typical / 1e9, "largest_jump_gb": jump / 1e9 if deltas else None,
           "jumps_left": int((warn_at - used) // jump) if jump > 1e9 and used < warn_at else None,
           "days_to_warn": None, "days_to_full": None}
    if typical and typical > 0 and total:
        out["days_to_warn"] = max(0.0, (warn_at - used) / typical)
        out["days_to_full"] = max(0.0, (total - used) / typical)
    return out


def memory_room(used: float, total: float, peak: float | None, reserved: list[dict]) -> dict:
    free = total - used
    fits = max(0.0, free / total - START_MARGIN) if total else 0.0
    known = [r["share"] for r in reserved if r["share"] is not None]
    return {"used_gb": used / GIB, "total_gb": total / GIB, "free_gb": free / GIB,
            "peak_gb_7d": None if peak is None else peak / GIB,
            "max_new_share": round(fits, 2), "max_new_gb": fits * total / GIB,
            "reserved": reserved, "reserved_share": round(sum(known), 2) if known else None,
            "unknown_shares": [r["model"] for r in reserved if r["share"] is None],
            "tight": free / GIB < MEM_TIGHT_GB}


def trend(daily: list[float]) -> dict:
    """Requests per day (oldest first, today partial and left out of the fit): least-squares slope per day."""
    full = daily[:-1] if len(daily) > 1 else daily
    n = len(full)
    if n < 5 or not any(full):             # a trend needs most of a week
        return {"per_day": full, "slope_per_day": None, "change_pct": None}
    xs = range(n)
    mx, my = (n - 1) / 2, sum(full) / n
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, full)) / sum((x - mx) ** 2 for x in xs)
    return {"per_day": full, "slope_per_day": slope, "change_pct": 100 * slope * (n - 1) / my if my else None}


# ---------------------------------------------------------------- assemble
def report() -> dict:
    from backend.monitoring import metrics
    fleet = metrics.fleet_raw()
    disk_used, disk_total = _q("max by (node) (aiops_node_disk_used_bytes)"), _q("max by (node) (aiops_node_disk_total_bytes)")
    disk_daily = _q_range('max by (node) (max_over_time(aiops_node_disk_used_bytes{node=~"Node-.*"}[1d]))', days=8)
    mem_peak = _q("max by (node) (max_over_time(aiops_node_memory_used_bytes[7d]))")
    nodes = {}
    for n in config.NODES:
        d = fleet.get(n.name) or {}
        mem = d.get("memory") or {}
        reserved = [{"model": s.get("name"), "share": s.get("mem_fraction"), "status": s.get("status")}
                    for s in d.get("vllm") or [] if s.get("engine", "vllm") in ("vllm", "sglang")]
        nodes[n.name] = {
            "memory": memory_room(mem["used"], mem["total"], mem_peak.get(n.name), reserved) if mem.get("total") else None,
            "disk": disk_forecast(disk_used[n.name], disk_total[n.name], disk_daily.get(n.name, []))
            if n.name in disk_used and n.name in disk_total else None}
    daily = _q_range("sum by (node, model) (increase(vllm:request_success_total[1d]))")
    run_peak = _q("max by (node, model) (max_over_time(vllm:num_requests_running[7d]))")
    kv_peak = _q("max by (node, model) (max_over_time(vllm:kv_cache_usage_perc[7d]))")
    serving = {(n, s.get("name")): s.get("kind") for n, d in fleet.items() for s in (d or {}).get("vllm") or []}
    models = []
    for key, series in sorted(daily.items()):
        node, _, model = key.partition("|")
        if (node, model) not in serving:
            continue
        models.append({"node": node, "model": model, "kind": serving[(node, model)], **trend(series),
                       "peak_running_7d": run_peak.get(key), "peak_kv_7d": kv_peak.get(key)})
    return {"generated": time.time(), "nodes": nodes, "models": models,
            "disk_warn_pct": config.DISK_PCT_WARN, "disk_forecast_warn_days": DISK_FORECAST_WARN_DAYS}


def alerts(rep: dict) -> list:
    """Disk projected to reach the detector's limit within DISK_FORECAST_WARN_DAYS."""
    from backend.monitoring.services import ServiceAlert
    out = []
    for node, r in rep["nodes"].items():
        d = r.get("disk")
        if d and d["days_to_warn"] is not None and d["days_to_warn"] < DISK_FORECAST_WARN_DAYS and d["pct"] < config.DISK_PCT_WARN:
            out.append(ServiceAlert("capacity_disk_forecast", node, "disk",
                                    f"{node} disk projected to reach {config.DISK_PCT_WARN:.0f}% in {d['days_to_warn']:.0f} days",
                                    d | {"basis": "linear fit of disk use over the last 7 days"}))
    return out

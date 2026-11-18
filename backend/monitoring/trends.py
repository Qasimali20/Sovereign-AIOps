"""Predictive maintenance: slow trends that end in a failure, caught before the hard limits fire.

Rule-based on purpose: four nodes have too few failures to train a model, and each rule here names the evidence a
person can check. Each rule compares a recent window with the node's own earlier history and reports
"insufficient history" until that history exists, never a guess.

  memory_creep    the node's memory floor (lowest use in each hour) rising with no model server started or stopped
                  and projected to reach the detector's limit within MEMORY_HORIZON_D days: a leak
  gpu_idle_warmer the GPU's idle temperature (power under IDLE_W) over the last day well above the week before:
                  cooling getting worse (dust, fan, airflow) before it shows under load
  xid_recurring   GPU driver errors (Xid) on several days of the last week: hardware or driver degrading
  restart_recurring the same unit or container restarting on several different days of the last week
  wifi_weakening  Wi-Fi signal over the last day well below the week before
"""
from __future__ import annotations

import math
import time

import httpx

from backend.core import config, store

MEMORY_HORIZON_D = 3.0
MEMORY_MIN_GB_PER_DAY = 3.0
IDLE_W = 20.0
IDLE_RISE_C = 8.0
WIFI_DROP_DB = 6.0
RECUR_DAYS = 3


def _q(expr: str) -> dict[str, float]:
    r = httpx.get(f"{config.PROMETHEUS_URL}/api/v1/query", params={"query": expr}, timeout=20)
    r.raise_for_status()
    out = {}
    for s in r.json()["data"]["result"]:
        v = float(s["value"][1])
        if not (math.isnan(v) or math.isinf(v)):
            out[s["metric"].get("node", "")] = v
    return out


# ---------------------------------------------------------------- rules (pure)
def memory_creep(used: float, total: float, gb_per_day: float | None, stable_servers: bool, uptime_d: float) -> dict:
    if gb_per_day is None or uptime_d < 1:
        return {"status": "insufficient", "detail": "needs a day of uptime"}
    limit = total * config.MEM_PCT_WARN / 100
    days = (limit - used) / gb_per_day if gb_per_day > 0 else None
    d = {"gb_per_day": round(gb_per_day, 2), "days_to_limit": None if days is None else round(days, 1)}
    if stable_servers and gb_per_day >= MEMORY_MIN_GB_PER_DAY and days is not None and days < MEMORY_HORIZON_D:
        return d | {"status": "warn", "detail": f"memory floor rising {gb_per_day:.1f} GB/day with no model server change; "
                                               f"{config.MEM_PCT_WARN:.0f}% in about {days:.1f} days"}
    return d | {"status": "ok", "detail": f"floor {gb_per_day:+.1f} GB/day"}


def gpu_idle_warmer(recent_c: float | None, before_c: float | None) -> dict:
    if recent_c is None or before_c is None:
        return {"status": "insufficient", "detail": "needs idle readings from the last day and the week before"}
    rise = recent_c - before_c
    d = {"idle_c": round(recent_c, 1), "idle_c_before": round(before_c, 1)}
    if rise >= IDLE_RISE_C:
        return d | {"status": "warn", "detail": f"idle GPU {recent_c:.0f} °C against {before_c:.0f} °C the week before: check fans and airflow"}
    return d | {"status": "ok", "detail": f"idle GPU {recent_c:.0f} °C ({rise:+.1f})"}


def recurring(days_with_events: dict[str, int], what: str) -> list[dict]:
    """{resource: distinct days with events in the last 7}: a warning per resource seen on RECUR_DAYS or more days."""
    return [{"resource": r, "status": "warn", "days": n, "detail": f"{what} on {n} different days this week"}
            for r, n in sorted(days_with_events.items()) if n >= RECUR_DAYS]


def wifi_weakening(recent_dbm: float | None, before_dbm: float | None) -> dict:
    if recent_dbm is None or before_dbm is None:
        return {"status": "insufficient", "detail": "needs a day and the week before"}
    drop = before_dbm - recent_dbm
    d = {"signal_dbm": round(recent_dbm, 1), "signal_dbm_before": round(before_dbm, 1)}
    if drop >= WIFI_DROP_DB:
        return d | {"status": "warn", "detail": f"Wi-Fi {recent_dbm:.0f} dBm against {before_dbm:.0f} dBm the week before"}
    return d | {"status": "ok", "detail": f"Wi-Fi {recent_dbm:.0f} dBm ({-drop:+.1f})"}


# ---------------------------------------------------------------- assemble
def _days_with(kinds: tuple[str, ...]) -> dict[tuple[str, str], int]:
    ph = ",".join("?" * len(kinds))
    with store.conn() as c:
        rows = c.execute(f"SELECT node, resource, COUNT(DISTINCT CAST(created / 86400 AS INT)) FROM incidents "
                         f"WHERE kind IN ({ph}) AND created >= ? GROUP BY node, resource", (*kinds, time.time() - 7 * 86400))
        return {(r[0], r[1]): r[2] for r in rows}


def report() -> dict:
    gib = 1024 ** 3
    used, total = _q("aiops_node_memory_used_bytes"), _q("aiops_node_memory_total_bytes")
    floor_rate = _q("deriv((min_over_time(aiops_node_memory_used_bytes[1h]))[24h:10m]) * 86400")
    server_changes = _q("changes((sum by (node) (aiops_model_engine_info))[24h:5m])")
    uptime = _q("aiops_node_uptime_seconds")
    idle_now = _q(f"avg_over_time((aiops_gpu_temp_celsius and aiops_gpu_power_watts < {IDLE_W})[24h:5m])")
    idle_before = _q(f"avg_over_time((aiops_gpu_temp_celsius and aiops_gpu_power_watts < {IDLE_W})[6d:5m] offset 1d)")
    wifi_now = _q("avg_over_time(aiops_wifi_signal_dbm[24h])")
    wifi_before = _q("avg_over_time(aiops_wifi_signal_dbm[6d] offset 1d)")
    xid = _days_with(("gpu_xid",))
    restarts = _days_with(("svc_unit_restarting", "svc_unit_failed", "svc_container_restarting"))
    nodes = {}
    for n in (x.name for x in config.NODES):
        rules = {
            "memory_creep": memory_creep(used.get(n, 0) / gib, total.get(n, 0) / gib,
                                         None if n not in floor_rate else floor_rate[n] / gib,
                                         server_changes.get(n, 1) == 0, uptime.get(n, 0) / 86400),
            "gpu_idle_warmer": gpu_idle_warmer(idle_now.get(n), idle_before.get(n)),
            "wifi_weakening": wifi_weakening(wifi_now.get(n), wifi_before.get(n)),
        }
        rec = recurring({r: d for (nn, r), d in xid.items() if nn == n}, "GPU driver errors") \
            + recurring({r: d for (nn, r), d in restarts.items() if nn == n}, "restarts")
        nodes[n] = {"rules": rules, "recurring": rec}
    return {"generated": time.time(), "nodes": nodes}


def alerts(rep: dict) -> list:
    from backend.monitoring.services import ServiceAlert
    out = []
    for node, r in rep["nodes"].items():
        for rule, x in r["rules"].items():
            if x["status"] == "warn":
                out.append(ServiceAlert(f"trend_{rule}", node, rule, f"{node}: {x['detail']}", x))
        for x in r["recurring"]:
            kind = "trend_xid_recurring" if x["detail"].startswith("GPU") else "trend_restart_recurring"
            out.append(ServiceAlert(kind, node, x["resource"], f"{x['resource']} on {node}: {x['detail']}", x))
    return out

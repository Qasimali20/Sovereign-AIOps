"""LLMOps read side: what the gateway served (from its request log) and what it cost in GPU energy.

Requests come from backend/llmops/requestlog.py (metadata only). Energy is an estimate from Prometheus GPU power:
for each node, the energy above its idle draw over the range is shared among the models it served in proportion to
the time each model spent answering (request seconds), then divided by that model's tokens and requests. GPU only (no
CPU or system power), and only traffic that went through the gateway is attributed: say so wherever it is shown.
"""
from __future__ import annotations

import os
import sqlite3
import time
from collections import defaultdict
from typing import Literal

import httpx

from backend.core import config
from backend.llmops import requestlog

Range = Literal["1h", "6h", "24h", "7d"]
SPAN = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400}
BUCKET = {"1h": 60, "6h": 300, "24h": 900, "7d": 3 * 3600}
PRICE_PER_KWH = float(os.environ.get("AIOPS_ENERGY_PRICE_PER_KWH") or 0) or None   # optional, in AIOPS_ENERGY_CURRENCY
CURRENCY = os.environ.get("AIOPS_ENERGY_CURRENCY", "")


def _rows(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    if not requestlog.PATH.exists():
        return []
    with requestlog.connect() as c:
        return c.execute(sql, args).fetchall()


def _pct(xs: list[float], q: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 4) if xs else None


def _group(rows: list[sqlite3.Row], key: str) -> list[dict]:
    g: dict[str, list] = defaultdict(list)
    for r in rows:
        g[r[key] or "—"].append(r)
    out = []
    for k, rs in g.items():
        ok = [r for r in rs if r["outcome"] == "ok"]
        out.append({key: k, "requests": len(rs), "errors": len(rs) - len(ok),
                    "error_rate": round((len(rs) - len(ok)) / len(rs), 4),
                    "p50_s": _pct([r["latency_s"] for r in ok], 0.5), "p95_s": _pct([r["latency_s"] for r in ok], 0.95),
                    "ttft_p95_s": _pct([r["ttft_s"] for r in ok], 0.95),
                    "prompt_tokens": sum(r["prompt_tokens"] or 0 for r in ok),
                    "completion_tokens": sum(r["completion_tokens"] or 0 for r in ok),
                    "audio_s": round(sum(r["audio_s"] or 0 for r in ok), 1),
                    "busy_s": round(sum(r["latency_s"] or 0 for r in ok), 1),
                    "endpoints": sorted({r["endpoint"] or "other" for r in rs})})
    return sorted(out, key=lambda x: -x["requests"])


def summary(range_: Range, now: float | None = None) -> dict:
    """Totals, per model, per client, and a requests/errors series for the range."""
    now = now or time.time()
    since, step = now - SPAN[range_], BUCKET[range_]
    rows = _rows("SELECT * FROM requests WHERE t >= ?", (since,))
    series: dict[int, list[int]] = {}
    for r in rows:
        b = int(r["t"] // step * step)
        s = series.setdefault(b, [0, 0])
        s[0] += 1
        s[1] += r["outcome"] != "ok"
    ok = [r for r in rows if r["outcome"] == "ok"]
    return {
        "range": range_, "since": since, "until": now, "step": step,
        "total": {"requests": len(rows), "errors": len(rows) - len(ok),
                  "error_rate": round((len(rows) - len(ok)) / len(rows), 4) if rows else None,
                  "p95_s": _pct([r["latency_s"] for r in ok], 0.95),
                  "tokens": sum((r["prompt_tokens"] or 0) + (r["completion_tokens"] or 0) for r in ok),
                  "clients": len({r["client"] for r in rows})},
        "by_model": _group([r for r in rows if r["model"]], "model"),
        "by_client": _group(rows, "client"),
        "outcomes": {o: sum(r["outcome"] == o for r in rows) for o in sorted({r["outcome"] for r in rows})},
        "series": [[t, *series[t]] for t in sorted(series)],
    }


def requests(range_: Range, model: str = "", client: str = "", outcome: str = "", limit: int = 200) -> list[dict]:
    """The newest requests matching the filters (metadata only)."""
    where, args = ["t >= ?"], [time.time() - SPAN[range_]]
    for col, v in (("model", model), ("client", client)):
        if v:
            where.append(f"{col} = ?")
            args.append(v)
    if outcome == "errors":
        where.append("outcome != 'ok'")
    elif outcome:
        where.append("outcome = ?")
        args.append(outcome)
    return [dict(r) for r in _rows(f"SELECT * FROM requests WHERE {' AND '.join(where)} ORDER BY t DESC LIMIT ?",
                                   (*args, min(limit, 1000)))]


def _prom(expr: str) -> dict[str, float]:
    try:
        r = httpx.get(f"{config.PROMETHEUS_URL}/api/v1/query", params={"query": expr}, timeout=8)
        r.raise_for_status()
        return {x["metric"].get("node", ""): float(x["value"][1]) for x in r.json()["data"]["result"]}
    except (httpx.HTTPError, ValueError, KeyError):
        return {}


def energy(range_: Range, now: float | None = None) -> dict:
    """GPU energy per node over the range, and the active share attributed to each model the gateway served."""
    now = now or time.time()
    # the energy window is the part of the range the request log covers: dividing a whole range's energy by a few
    # minutes of recorded requests would inflate every per-request figure
    first = _rows("SELECT MIN(t) FROM requests WHERE t >= ?", (now - SPAN[range_],))
    start = first[0][0] if first and first[0][0] else now - SPAN[range_]
    span = max(60, int(now - start))
    hours = span / 3600
    w = f"[{span}s]"
    avg = _prom(f"max by (node) (avg_over_time(aiops_gpu_power_watts{w}))")
    idle = _prom(f"max by (node) (quantile_over_time(0.05, aiops_gpu_power_watts{w}))")
    nodes = {n: {"node": n, "avg_w": round(avg[n], 1), "idle_w": round(idle.get(n, 0), 1),
                 "total_wh": round(avg[n] * hours, 1), "active_wh": round(max(0.0, avg[n] - idle.get(n, 0)) * hours, 1)}
             for n in sorted(avg)}
    by_node_model: dict[str, list[dict]] = defaultdict(list)
    for m in _group([r for r in _rows("SELECT * FROM requests WHERE t >= ?", (start,)) if r["model"]], "model"):
        rows = _rows("SELECT node, SUM(latency_s) busy, COUNT(*) n, SUM(prompt_tokens) p, SUM(completion_tokens) c "
                     "FROM requests WHERE model = ? AND t >= ? AND outcome = 'ok' GROUP BY node", (m["model"], start))
        for r in rows:
            by_node_model[r["node"]].append({"model": m["model"], "node": r["node"], "busy_s": r["busy"] or 0,
                                             "requests": r["n"], "tokens": (r["p"] or 0) + (r["c"] or 0),
                                             "completion_tokens": r["c"] or 0})
    models = []
    for node, ms in by_node_model.items():
        busy = sum(x["busy_s"] for x in ms) or 1
        active = nodes.get(node, {}).get("active_wh", 0)
        for x in ms:
            wh = active * x["busy_s"] / busy
            models.append(x | {"share": round(x["busy_s"] / busy, 3), "wh": round(wh, 2),
                               "wh_per_request": round(wh / x["requests"], 4) if x["requests"] else None,
                               "wh_per_1k_tokens": round(1000 * wh / x["tokens"], 3) if x["tokens"] else None,
                               "cost": round(wh / 1000 * PRICE_PER_KWH, 4) if PRICE_PER_KWH else None})
    total_active = sum(n["active_wh"] for n in nodes.values())
    return {"range": range_, "hours": round(hours, 3), "since": start, "nodes": list(nodes.values()),
            "models": sorted(models, key=lambda x: -x["wh"]),
            "total_wh": round(sum(n["total_wh"] for n in nodes.values()), 1), "active_wh": round(total_active, 1),
            "price_per_kwh": PRICE_PER_KWH, "currency": CURRENCY,
            "method": "GPU power above each node's idle draw, shared among the models it served by time spent answering "
                      "(gateway traffic only; GPU only, not CPU or system power)"}

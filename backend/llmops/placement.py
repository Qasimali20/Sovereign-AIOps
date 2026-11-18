"""Where a model server should run. Recommendations only: the platform never moves a production model.

`rank(need_gb, model)` orders the nodes for a new server needing `need_gb` of unified memory: it must fit in measured
free memory with a margin; among those, prefer a node without a replica of the same model (a second replica on the
same node is no failover), then the least busy GPU over the last day, then the fewest other servers producing
tokens, then the coolest GPU. `advice()` looks at the fleet as it is: gateway routes and platform components that
depend on a single server (no failover), and nodes too full to restart their own servers.
"""
from __future__ import annotations

import math

import httpx

from backend.core import config

MARGIN_GB = 2.5          # vLLM's start-up estimate is not exact


def _q(expr: str) -> dict[str, float]:
    r = httpx.get(f"{config.PROMETHEUS_URL}/api/v1/query", params={"query": expr}, timeout=15)
    r.raise_for_status()
    return {s["metric"].get("node", ""): float(s["value"][1]) for s in r.json()["data"]["result"]
            if not math.isnan(float(s["value"][1]))}


def load() -> dict[str, dict]:
    gpu, temp = _q("max by (node) (avg_over_time(aiops_gpu_utilization_percent[1d]))"), _q("max by (node) (max_over_time(aiops_gpu_temp_celsius[1d]))")
    busy = _q("count by (node) (sum by (node, model) (rate(vllm:generation_tokens_total[1d])) > 1)")
    return {n.name: {"gpu_avg_1d": gpu.get(n.name), "gpu_temp_max_1d": temp.get(n.name), "busy_servers": int(busy.get(n.name, 0))}
            for n in config.NODES}


# ---------------------------------------------------------------- pure
def rank(need_gb: float, nodes: dict[str, dict], hosts: dict[str, set[str]], model: str | None = None) -> list[dict]:
    """nodes: {node: {"free_gb", "gpu_avg_1d", "gpu_temp_max_1d", "busy_servers"}}; hosts: {node: served model names}."""
    out = []
    for n, x in nodes.items():
        free = x.get("free_gb")
        fits = free is not None and free - need_gb >= MARGIN_GB
        same = bool(model and model in hosts.get(n, set()))
        why = [f"{free:.0f} GB free" if free is not None else "memory unknown"]
        if not fits:
            why.append(f"needs {need_gb + MARGIN_GB:.0f} GB with margin")
        if same:
            why.append(f"already runs {model}: no failover gain")
        why += [f"GPU {x['gpu_avg_1d']:.0f}% busy over a day" if x.get("gpu_avg_1d") is not None else "GPU load unknown",
                f"{x.get('busy_servers', 0)} other busy server(s)"]
        out.append({"node": n, "fits": fits, "same_model": same, "free_gb": free, "reasons": why,
                    "_key": (not fits, same, round(x.get("gpu_avg_1d") or 100, -1), x.get("busy_servers", 9),
                             x.get("gpu_temp_max_1d") or 99)})
    out.sort(key=lambda r: r.pop("_key"))
    for i, r in enumerate(out):
        r["rank"] = i + 1 if r["fits"] else None
    return out


def single_points(view: dict) -> list[dict]:
    """Routes and platform components that lose service if one model server fails."""
    from backend.monitoring import topology
    g = view["graph"]
    out = []
    for vid, v in g.items():
        if v.type not in ("route", "component"):
            continue
        for sid in (s for s in g if g[s].type == "server"):
            if any(x["id"] == vid and x["state"] == topology.DOWN for x in view["impacts"].get(sid, [])):
                out.append({"what": v.label, "id": vid, "server": sid.split(":", 1)[1], "node": g[sid].node})
    return out


def advice(capacity: dict, graph: dict, loads: dict[str, dict]) -> list[dict]:
    from backend.monitoring import topology
    servers = [s for s in graph if graph[s].type == "server"]
    view = {"graph": graph, "impacts": {s: topology.impact(graph, s) for s in servers}}
    hosts: dict[str, set[str]] = {}
    for s in servers:
        hosts.setdefault(graph[s].node, set()).add(s.split("/", 1)[1].rsplit("@", 1)[0])
    nodes = {n: {"free_gb": (r.get("memory") or {}).get("free_gb"), **loads.get(n, {})} for n, r in capacity["nodes"].items()}
    shares = {f"{n}/{x['model']}": x["share"] for n, r in capacity["nodes"].items() for x in ((r.get("memory") or {}).get("reserved") or [])}
    out = []
    for sp in single_points(view):
        model = sp["server"].split("/", 1)[1].rsplit("@", 1)[0]
        share = shares.get(f"{sp['node']}/{model}")
        need = share * (capacity["nodes"][sp["node"]]["memory"] or {}).get("total_gb", 0) if share else None
        best = next((r for r in rank(need or 0, nodes, hosts, model) if r["fits"] and not r["same_model"]), None) if need else None
        out.append({"kind": "single_point", "severity": "warning",
                    "title": f"{sp['what']} depends on one server: {model} on {sp['node']}",
                    "action": (f"Add a replica on {best['node']} ({need:.0f} GB, {best['free_gb']:.0f} GB free there); the gateway "
                               "balances replicas and fails over between them.") if best else
                              "No node has room for a second replica now" + ("" if need else " (its memory share is not reported)") + "."})
    for n, x in nodes.items():
        if x["free_gb"] is not None and x["free_gb"] < 8:
            dest = next((r for r in rank(0, nodes, hosts) if r["fits"] and r["node"] != n), None)
            out.append({"kind": "tight_node", "severity": "info", "title": f"{n} has {x['free_gb']:.0f} GB free",
                        "action": "Restarting one of its model servers may not fit if another grew meanwhile (the 18 Nov "
                                  "Qwen3.6 failure on Node-4). " + (f"{dest['node']} has the most room ({dest['free_gb']:.0f} GB) "
                                                                    "for the next model." if dest else "")})
    return out

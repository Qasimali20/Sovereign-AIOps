import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.core import analytics, config, llm, store
from backend.monitoring import fleet
from backend.incidents import tools
from backend.incidents.runtime import runtime
from backend.web.security import Admin, Viewer

router = APIRouter(tags=["infrastructure"])


@router.get("/fleet")
def fleet_snapshot(user: Viewer):
    return fleet.snapshot()


@router.get("/metrics/board/{name}")
def metrics_board(name: Literal["fleet", "models", "operations"], user: Viewer,
                  range: Literal["1h", "6h", "24h", "7d", "30d"] = "6h"):
    """Metric history (Prometheus on Node-3): fixed panels, same views as the Grafana dashboards."""
    from backend.monitoring import boards
    try:
        return boards.board(name, range)
    except Exception as e:
        raise HTTPException(502, f"prometheus: {e}") from e


@router.get("/metrics/model")
def metrics_model(user: Viewer, model: str, range: Literal["1h", "6h", "24h", "7d", "30d"] = "6h"):
    """One model server in depth: p50/p95/p99 latencies, cache, load, errors, and its incidents."""
    from backend.monitoring import boards
    try:
        return boards.model_board(model, range)
    except KeyError as e:
        raise HTTPException(404, f"unknown model {model!r}") from e
    except Exception as e:
        raise HTTPException(502, f"prometheus: {e}") from e


@router.get("/metrics/scorecard")
def metrics_scorecard(user: Viewer, range: Literal["1h", "6h", "24h", "7d", "30d"] = "24h"):
    """Per-model traffic and quality over a range (Prometheus): the Models page scorecard."""
    from backend.monitoring import boards
    try:
        return boards.scorecard(range)
    except Exception as e:
        raise HTTPException(502, f"prometheus: {e}") from e


@router.get("/metrics/models")
def metrics_models(user: Viewer):
    from backend.monitoring import boards
    return boards.models()


def promql_ranges():
    from backend.monitoring import promql
    return promql.RANGES


@router.get("/fleet/{node}/history")
def node_history(node: str, user: Viewer, range: Literal["15m", "1h", "6h", "24h", "7d", "30d"] = "1h"):
    if node not in config.NODE_BY_NAME:
        raise HTTPException(404)
    if range in promql_ranges():                     # 6h and longer come from Prometheus (30-day retention)
        from backend.monitoring import promql
        try:
            return {"node": node, "range": range, "source": "prometheus", **promql.node_history(node, range)}
        except Exception as e:
            raise HTTPException(502, f"prometheus: {e}") from e
    try:
        d, src = tools._monitor_get(node, f"/history?range={range}", timeout=6)
    except tools.ToolError as e:
        raise HTTPException(502, str(e)) from e
    return {"node": node, "range": range, "source": src, "points": d.get("points", []), "models": d.get("models", {})}


@router.get("/system")
def system(user: Viewer):
    return {
        "uptime_s": round(time.time() - runtime.started),
        "detector": runtime.scan_info | {"interval_s": runtime.scan_seconds},
        "workflows_running": runtime.running,
        "llm_endpoints": [{"name": e.name, "model": e.model, "base_url": e.base_url, "healthy": llm._healthy(e)}
                          for e in config.LLM_ENDPOINTS],
        "event_recorder": {"events_recorded": analytics.table_counts()["container_events"]},
        "kernel_coverage": runtime.kernel_coverage,
        "grafana_url": config.GRAFANA_URL,
        "grafana_tailnet_url": config.GRAFANA_TAILNET_URL,
    }


@router.get("/services")
def services_view(user: Viewer):
    from backend.monitoring import services as svc
    out = {}
    for n in config.NODES:
        snap = runtime.services.get(n.name) or {"ok": False, "error": "not collected yet"}
        out[n.name] = snap | {"summary": svc.summarize(snap)} | {
            "containers": [c for c in snap.get("containers", []) if not svc.OWN_CONTAINERS.match(c["name"])]}
    return out


# ---------------------------------------------------------------- configuration drift
@router.get("/drift")
def drift_view(user: Viewer):
    """Each node's configuration against its accepted baseline, and the host versions where the nodes disagree."""
    from backend.monitoring import drift
    base = drift.load_baseline()
    nodes = {}
    for n in config.NODES:
        snap = runtime.drift.get(n.name) or {"ok": False, "error": "not checked yet"}
        b = base.get(n.name) or {}
        items = snap.get("items") or {}
        nodes[n.name] = {"ok": snap["ok"], "error": snap.get("error"), "checked": snap.get("checked"),
                         "baseline_at": b.get("at"), "baseline_by": b.get("by"),
                         "updated": b.get("updated"), "updated_by": b.get("updated_by"),
                         "items": len(items), "changes": runtime.drift_changes.get(n.name),
                         "inventory": {k: v["value"] for k, v in sorted(items.items())}}
    return {"nodes": nodes, "inconsistent": drift.fleet_consistency(runtime.drift), "interval_s": drift.CHECK_INTERVAL_S}


class DriftAccept(BaseModel):
    node: str = Field(pattern=r"^Node-[1-4]$")
    keys: list[str] | None = Field(default=None, max_length=200)     # None: every current difference on the node
    reason: str = Field(min_length=3, max_length=300)


@router.post("/drift/accept")
def drift_accept(body: DriftAccept, user: Admin):
    """Make the node's current configuration (for the given items) its baseline. Audited per item."""
    from backend.monitoring import drift
    snap = runtime.drift.get(body.node)
    if not snap or not snap.get("ok"):
        raise HTTPException(409, f"{body.node} has no current configuration reading; try again after the next check")
    with runtime._scan_lock:
        done = drift.accept(body.node, snap["items"], body.keys, f"human:{user['username']}", body.reason)
        base = drift.load_baseline()[body.node]["items"]
        runtime.drift_changes[body.node] = drift.compare(base, snap["items"])
    return {"accepted": done, "remaining": runtime.drift_changes[body.node]}


@router.post("/drift/check")
def drift_check(user: Admin):
    """Re-read every node's configuration now instead of waiting for the next 5-minute check."""
    with runtime._scan_lock:                    # not while a detector scan is reading the same state
        runtime.check_drift()
    return {k: (len(v) if v is not None else None) for k, v in runtime.drift_changes.items()}

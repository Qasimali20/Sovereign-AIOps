import time
from typing import Literal

from fastapi import APIRouter, HTTPException

from backend.core import analytics, config, llm, store
from backend.monitoring import fleet
from backend.incidents import tools
from backend.incidents.runtime import runtime
from backend.web.security import Viewer

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

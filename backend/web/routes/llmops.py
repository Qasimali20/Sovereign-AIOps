import threading

from fastapi import APIRouter, HTTPException

from backend.llmops import summary as llmops, model_evals, model_registry
from backend.web.security import Approver, Viewer

router = APIRouter(tags=["llmops"])


@router.get("/llm/summary")
def summary(user: Viewer, range: llmops.Range = "24h"):
    return llmops.summary(range)


@router.get("/llm/requests")
def requests(user: Viewer, range: llmops.Range = "24h", model: str = "", client: str = "", outcome: str = "", limit: int = 200):
    return llmops.requests(range, model[:200], client[:100], outcome[:20], max(1, min(limit, 1000)))


@router.get("/llm/energy")
def energy(user: Viewer, range: llmops.Range = "24h"):
    return llmops.energy(range)


@router.get("/llm/evals")
def evals(user: Viewer):
    return {"running": model_evals.running(), "last_run": model_evals.last_run_at(), "interval_s": model_evals.INTERVAL_S,
            "models": model_evals.latest()}


@router.get("/llm/evals/history")
def eval_history(user: Viewer, model: str, days: int = 30):
    return model_evals.history(model[:200], max(1, min(days, 365)))


@router.post("/llm/evals/run", status_code=202)
def run_evals(user: Approver):
    """Start an evaluation run now (about a minute, a few requests per model)."""
    if model_evals.running():
        raise HTTPException(409, "an evaluation run is already in progress")
    threading.Thread(target=model_evals.run_all, args=(f"human:{user['username']}",), daemon=True, name="model-evals").start()
    return {"started": True}


@router.get("/llm/registry")
def registry(user: Viewer):
    return {"refresh_s": model_registry.REFRESH_S, "models": model_registry.models(), "changes": model_registry.changes()}


@router.get("/llm/registry/history")
def registry_history(user: Viewer, node: str, port: int, model: str):
    return model_registry.history(node[:40], port, model[:200])


# ---------------------------------------------------------------- inference intelligence
@router.get("/llm/perf")
def perf_view(user: Viewer):
    """Latest benchmark per server and scenario, with baselines, and the current performance regressions."""
    from backend.llmops import perf
    return {"running": perf.running(), "last_run": perf.last_run_at(), "interval_s": perf.INTERVAL_S,
            "results": perf.latest(), "regressions": perf.regressions(),
            "guard": {"max_temp_c": perf.MAX_TEMP_C, "max_gpu_util": perf.MAX_GPU_UTIL}}


@router.post("/llm/perf/run", status_code=202)
def run_perf(user: Approver):
    """Benchmark every quiet chat/vision server now (4 short requests each; busy or hot nodes are skipped)."""
    from backend.llmops import perf
    if perf.running() or model_evals.running():
        raise HTTPException(409, "a benchmark or evaluation run is already in progress")
    threading.Thread(target=perf.run_all, args=(f"human:{user['username']}",), daemon=True, name="perf-bench").start()
    return {"started": True}


@router.get("/llm/bottlenecks")
def bottlenecks(user: Viewer, window: str = "1h"):
    from backend.llmops import bottleneck
    if window not in bottleneck.WINDOWS:
        raise HTTPException(422, f"window must be one of {list(bottleneck.WINDOWS)}")
    try:
        return bottleneck.analyse(window)
    except Exception as e:
        raise HTTPException(502, f"prometheus: {e}") from e


@router.get("/capacity")
def capacity_view(user: Viewer):
    """Memory room, disk forecast and model traffic trends (recomputed at most every 10 minutes)."""
    import time
    from backend.incidents.runtime import runtime
    from backend.llmops import capacity
    rep = runtime.capacity_report
    if not rep or time.time() - rep["generated"] > 600:
        try:
            rep = runtime.capacity_report = capacity.report()
        except Exception as e:
            if not rep:
                raise HTTPException(502, f"prometheus: {e}") from e
    return rep

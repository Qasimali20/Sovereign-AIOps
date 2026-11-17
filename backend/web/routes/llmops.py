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

from typing import Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.core import analytics, config, store
from backend.incidents.runtime import runtime
from backend.web.security import Approver, Viewer

router = APIRouter(tags=["incidents"])
BUSY = ("awaiting_approval", "executing", "verifying", "closed")


@router.get("/incidents")
def incidents(user: Viewer, limit: int = 500):
    rows = store.list_incidents(min(limit, 5000))
    pending = {a["incident_id"] for a in store.list_approvals("pending")}
    running = set(runtime.running)
    for r in rows:
        r["pending_approval"] = r["id"] in pending
        r["running"] = r["id"] in running
    return rows


@router.get("/incidents/summary")
def incidents_summary(user: Viewer):
    return analytics.active_counts()


@router.get("/incidents/{incident_id}")
def incident(incident_id: str, user: Viewer):
    b = store.incident_bundle(incident_id)
    if not b:
        raise HTTPException(404)
    b["report"] = next((s["data"] for s in reversed(b["agent_steps"]) if s["node"] == "report"), None)
    b["running"] = runtime.is_running(incident_id)
    return b


class CloseBody(BaseModel):
    note: str = Field(min_length=3, max_length=500)
    # the human verdict feeds the false-positive rate (blueprint 8.7)
    disposition: Literal["true_positive", "false_positive", "expected_activity"] | None = None


@router.post("/incidents/{incident_id}/close")
def close_incident(incident_id: str, body: CloseBody, user: Approver):
    inc = store.get_incident(incident_id)
    if not inc:
        raise HTTPException(404)
    if runtime.is_running(incident_id) or inc["status"] in BUSY:
        raise HTTPException(409, f"cannot close while {inc['status']}")
    actor = f"human:{user['username']}"
    store.audit(incident_id, actor, "closed", {"note": body.note, "previous": inc["status"], "disposition": body.disposition})
    store.set_status(incident_id, "closed", actor=actor)
    return {"ok": True}


@router.post("/incidents/{incident_id}/rerun")
def rerun_investigation(incident_id: str, user: Approver):
    """Re-run a read-only investigation that could not finish (e.g. the diagnosis model was unavailable).
    Never for an incident whose fix reached the executor: that history must not be overwritten."""
    from backend.incidents import graph
    inc = store.get_incident(incident_id)
    if not inc:
        raise HTTPException(404)
    if inc["status"] != "needs_human":
        raise HTTPException(409, f"only incidents that need a person can be re-investigated (this one is {inc['status']})")
    if store.has_execution(incident_id):
        raise HTTPException(409, "an action for this incident already ran; open a new incident instead")
    if runtime.is_running(incident_id):
        raise HTTPException(409, "already running")
    actor = f"human:{user['username']}"
    store.audit(incident_id, actor, "investigation_rerun", {"previous": inc["status"]})
    store.set_status(incident_id, "open", actor=actor)
    runtime.spawn(graph.rerun_incident, incident_id)
    return {"ok": True}


class MaintenanceBody(BaseModel):
    node: str = Field(pattern=r"^(Node-[1-4]|\*)$")
    minutes: float = Field(gt=0, le=24 * 60)
    reason: str = Field(min_length=3, max_length=300)


class AiPauseBody(BaseModel):
    resource: str
    minutes: float | None = Field(default=None, ge=0, le=240)       # None or 0 resumes
    reason: str = Field(min_length=3, max_length=300)


@router.post("/evaluation/ai-pause")
def ai_pause(body: AiPauseBody, user: Approver):
    """Pause the AI for one registered test resource during an evaluation (incidents are still detected)."""
    if body.resource not in config.TEST_RESOURCES:
        raise HTTPException(400, "only registered test resources can be paused")
    store.set_ai_pause(body.resource, body.minutes or None, body.reason, f"human:{user['username']}")
    return {"ok": True, "paused": store.ai_paused(body.resource)}


@router.get("/maintenance")
def maintenance(user: Viewer):
    return store.maintenance_windows()


@router.post("/maintenance")
def add_maintenance(body: MaintenanceBody, user: Approver):
    """Announce planned work: incidents it causes on that node are tagged 'planned', apart from real incidents."""
    return store.add_maintenance(body.node, body.minutes, body.reason, f"human:{user['username']}")


@router.post("/scan")
def scan(user: Approver):
    store.audit(None, f"human:{user['username']}", "manual_scan", None)
    return {"opened": runtime.scan_once()}


@router.get("/approvals")
def approvals(user: Viewer, status: str = "pending"):
    return store.approvals_with_context(status)


class Decision(BaseModel):
    approve: bool
    comment: str = Field(default="", max_length=500)


@router.post("/approvals/{approval_id}")
def decide(approval_id: str, d: Decision, user: Approver):
    name = user["username"]                 # from the session or the token's bound user, never from the request body
    if name.lower() in config.NON_HUMAN_ACTORS:
        raise HTTPException(400, "approver must be a person")
    res = store.decide_approval(approval_id, d.approve, name, d.comment)
    if not res:
        raise HTTPException(409, "approval not found or already decided")
    runtime.resume(res["incident_id"], approval_id)
    return {"ok": True, "status": res["status"], "incident_id": res["incident_id"], "approver": name}

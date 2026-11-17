from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.agentops import runs as agentops
from backend.web.security import Approver, Viewer

router = APIRouter(tags=["agentops"])


class FeedbackBody(BaseModel):
    verdict: agentops.Verdict
    note: str = Field(default="", max_length=500)


@router.get("/agent/overview")
def overview(user: Viewer, days: int = 30):
    return agentops.overview(max(1, min(days, 365)))


@router.get("/agent/runs")
def runs(user: Viewer, limit: int = 200, origin: Literal["all", "real", "drill", "planned"] = "all"):
    return agentops.runs(max(1, min(limit, 1000)), origin)


@router.get("/agent/runs/{incident_id}")
def run(incident_id: str, user: Viewer):
    r = agentops.run(incident_id)
    if r is None:
        raise HTTPException(404, "no agent run for this incident")
    return r


@router.post("/agent/runs/{incident_id}/feedback")
def feedback(incident_id: str, body: FeedbackBody, user: Approver):
    """An approver's verdict on the diagnosis: correct, partly right, or wrong (feeds the accuracy figure)."""
    try:
        return agentops.set_feedback(incident_id, body.verdict, body.note.strip(), f"human:{user['username']}")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

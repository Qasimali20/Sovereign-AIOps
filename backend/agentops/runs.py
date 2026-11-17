"""AgentOps: the incident agent's runs, read back from what the workflow already records.

A run is one investigation of one incident: the agent_steps rows of that incident, in order. Each step says what the
agent did (collect evidence, retrieve runbooks, diagnose, ask for more evidence, report); diagnose steps also carry the
model call's metadata (endpoint, model, latency, attempts, token usage, endpoints skipped). Nothing here calls a model.

Diagnosis feedback is a human's verdict on whether the diagnosis was right; it is stored once per incident (a later
verdict replaces it), audited, and gives the accuracy figure.
"""
from __future__ import annotations

import json
import time
from typing import Any, Literal

from backend.core import store

Verdict = Literal["correct", "partly", "wrong"]
STEP_LABEL = {"collect_evidence": "Collected evidence", "retrieve_runbooks": "Read runbooks", "diagnose": "Diagnosed",
              "gather_more": "Asked for more evidence", "plan": "Proposed an action", "report": "Reported"}


def _load(v: str | None) -> Any:
    try:
        return json.loads(v) if v else {}
    except ValueError:
        return {}


def _usage(meta: dict) -> tuple[int, int]:
    """(prompt, completion) tokens over every attempt of one model call."""
    p = c = 0
    for u in meta.get("usage") or []:
        if isinstance(u, dict):
            p += int(u.get("prompt_tokens") or 0)
            c += int(u.get("completion_tokens") or 0)
    return p, c


def _summarise(steps: list[dict]) -> dict:
    calls = [s["data"].get("meta") or {} for s in steps if s["node"] == "diagnose"]
    prompt = completion = 0
    for m in calls:
        p, c = _usage(m)
        prompt, completion = prompt + p, completion + c
    return {
        "started": steps[0]["created"], "ended": steps[-1]["created"],
        "duration_s": round(steps[-1]["created"] - steps[0]["created"], 1),
        "steps": len(steps), "model_calls": len(calls),
        "model_seconds": round(sum(float(m.get("latency_s") or 0) for m in calls), 1),
        "attempts": sum(int(m.get("attempts") or 1) for m in calls),
        "fallbacks": sum(len(m.get("skipped") or []) for m in calls),
        "prompt_tokens": prompt, "completion_tokens": completion,
        "models": sorted({str(m.get("model")) for m in calls if m.get("model")}),
    }


def _steps(c, incident_ids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {i: [] for i in incident_ids}
    if not incident_ids:
        return out
    ph = ",".join("?" * len(incident_ids))
    for r in c.execute(f"SELECT incident_id, created, node, data FROM agent_steps WHERE incident_id IN ({ph}) "
                       "ORDER BY created", incident_ids):
        out[r["incident_id"]].append({"created": r["created"], "node": r["node"], "data": _load(r["data"])})
    return out


def runs(limit: int = 200, origin: str = "all") -> list[dict]:
    """Newest runs first, one row per incident the agent worked on."""
    only = "AND i.origin = ?" if origin != "all" else ""
    args: list[Any] = [origin] if origin != "all" else []
    with store.conn() as c:
        rows = c.execute(
            "SELECT i.id, i.title, i.kind, i.node, i.status, i.origin, MAX(s.created) AS last FROM agent_steps s "
            f"JOIN incidents i ON i.id = s.incident_id WHERE 1=1 {only} GROUP BY i.id ORDER BY last DESC LIMIT ?",
            [*args, min(limit, 1000)]).fetchall()
        ids = [r["id"] for r in rows]
        steps = _steps(c, ids)
        fb = {r["incident_id"]: r["verdict"] for r in c.execute(
            f"SELECT incident_id, verdict FROM diagnosis_feedback WHERE incident_id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)}
    return [{"incident_id": r["id"], "title": r["title"], "kind": r["kind"], "node": r["node"], "status": r["status"],
             "origin": r["origin"], "feedback": fb.get(r["id"]), **_summarise(steps[r["id"]])}
            for r in rows if steps[r["id"]]]


def run(incident_id: str) -> dict | None:
    """One run, step by step, with each model call's metadata and a short readable summary of what the step did."""
    with store.conn() as c:
        inc = c.execute("SELECT id, title, kind, node, status, origin FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if not inc:
            return None
        steps = _steps(c, [incident_id])[incident_id]
        fb = c.execute("SELECT verdict, note, by, created FROM diagnosis_feedback WHERE incident_id = ?", (incident_id,)).fetchone()
    if not steps:
        return None
    # a step is recorded when it finishes: its time is the gap since the previous step finished (unknown for the first)
    t0 = steps[0]["created"]
    out = []
    for i, s in enumerate(steps):
        d, meta = s["data"], s["data"].get("meta") or {}
        p, comp = _usage(meta)
        out.append({"node": s["node"], "label": STEP_LABEL.get(s["node"], s["node"]), "done_at_s": round(s["created"] - t0, 1),
                    "duration_s": round(s["created"] - steps[i - 1]["created"], 1) if i else None, "detail": _detail(s["node"], d),
                    "call": {"endpoint": meta.get("endpoint"), "model": meta.get("model"), "request_id": meta.get("request_id"),
                             "latency_s": meta.get("latency_s"), "attempts": meta.get("attempts"),
                             "prompt_tokens": p, "completion_tokens": comp, "skipped": meta.get("skipped") or []} if meta else None})
    return {"incident": dict(inc), "summary": _summarise(steps), "steps": out,
            "feedback": dict(fb) if fb else None}


def _detail(node: str, d: dict) -> str:
    """One readable line per step (the full record stays on the incident page)."""
    if node == "collect_evidence":
        return f"{d.get('count', len(d.get('tools') or []))} evidence records: " + ", ".join(sorted({t[0] for t in d.get("tools") or []}))
    if node == "retrieve_runbooks":
        return f"{len(d.get('chunks') or [])} runbook sections ({d.get('method', '?')})"
    if node == "gather_more":
        reqs = d.get("requests") or []
        return f"asked for {len(reqs)}, {d.get('new_records', 0)} new: " + ", ".join(r.get("tool", "?") for r in reqs)
    if node == "diagnose":
        diag = d.get("diagnosis") or {}
        text = str(diag.get("summary") or "") if isinstance(diag, dict) else str(diag)
        return text if len(text) <= 600 else text[:600].rsplit(" ", 1)[0] + "…"
    if node == "report":
        return f"final status {d.get('final_status')}; evidence support {d.get('observation_evidence_support')}"
    return ""


def set_feedback(incident_id: str, verdict: Verdict, note: str, by: str) -> dict:
    """Record (or replace) a human verdict on the diagnosis; audited with the verdict it replaced."""
    with store.conn() as c:
        c.execute("BEGIN IMMEDIATE")
        if not c.execute("SELECT 1 FROM agent_steps WHERE incident_id = ? AND node = 'diagnose' LIMIT 1", (incident_id,)).fetchone():
            raise ValueError("this incident has no diagnosis to rate")
        prev = c.execute("SELECT verdict FROM diagnosis_feedback WHERE incident_id = ?", (incident_id,)).fetchone()
        now = time.time()
        c.execute("INSERT INTO diagnosis_feedback (incident_id, created, verdict, note, by) VALUES (?, ?, ?, ?, ?) "
                  "ON CONFLICT(incident_id) DO UPDATE SET created=excluded.created, verdict=excluded.verdict, "
                  "note=excluded.note, by=excluded.by", (incident_id, now, verdict, note, by))
        store._audit(c, incident_id, by, "diagnosis_feedback",
                     {"verdict": verdict, "note": note, "replaces": prev["verdict"] if prev else None})
    return {"incident_id": incident_id, "verdict": verdict, "note": note, "by": by, "created": now}


def overview(days: int = 30) -> dict:
    """Headline figures: runs, model time and tokens, and diagnosis accuracy from human verdicts."""
    since = time.time() - days * 86400
    recent = [r for r in runs(1000) if r["ended"] >= since]
    with store.conn() as c:
        fb = [r["verdict"] for r in c.execute("SELECT verdict FROM diagnosis_feedback")]
    rated = len(fb)
    score = (fb.count("correct") + 0.5 * fb.count("partly")) / rated if rated else None
    durations = sorted(r["duration_s"] for r in recent)
    return {
        "days": days, "runs": len(recent),
        "median_run_s": durations[len(durations) // 2] if durations else None,
        "model_calls": sum(r["model_calls"] for r in recent),
        "tokens": sum(r["prompt_tokens"] + r["completion_tokens"] for r in recent),
        "retries": sum(r["attempts"] - r["model_calls"] for r in recent),
        "fallbacks": sum(r["fallbacks"] for r in recent),
        "rated": rated, "accuracy": score,
        "verdicts": {v: fb.count(v) for v in ("correct", "partly", "wrong")},
    }

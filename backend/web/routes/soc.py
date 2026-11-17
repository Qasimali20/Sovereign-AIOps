"""Security operations (SOC) views and the isolated lab controls."""
import json
import time

from fastapi import APIRouter, HTTPException

from backend.core import store
from backend.incidents.runtime import runtime
from backend.security import attack, lab
from backend.security.events import classify_ip
from backend.web.security import Admin, Approver, Viewer

router = APIRouter(prefix="/security", tags=["security"])


@router.get("/overview")
def overview(user: Viewer, hours: int = 24):
    now = time.time()
    since = now - max(1, min(hours, 24 * 30)) * 3600
    with store.conn() as c:
        # the platform's own SSH checks are counted separately so real sign-ins are not buried under them
        people = "t >= ? AND source != 'platform'"
        by_kind = dict(c.execute(f"SELECT kind, COUNT(*) FROM security_events WHERE {people} GROUP BY kind", (since,)).fetchall())
        by_node = dict(c.execute(f"SELECT node, COUNT(*) FROM security_events WHERE {people} GROUP BY node", (since,)).fetchall())
        platform_checks = c.execute("SELECT COUNT(*) FROM security_events WHERE t >= ? AND source = 'platform'", (since,)).fetchone()[0]
        sources = [dict(r) for r in c.execute(
            "SELECT src_ip, SUM(kind IN ('auth_failure','invalid_user')) failures, SUM(kind='auth_success') successes, "
            "COUNT(DISTINCT user) users, GROUP_CONCAT(DISTINCT node) nodes, MAX(t) last FROM security_events "
            f"WHERE {people} AND src_ip IS NOT NULL GROUP BY src_ip ORDER BY failures DESC, last DESC LIMIT 12", (since,))]
        recent = [dict(r) for r in c.execute(
            f"SELECT t, node, kind, user, src_ip, method, detail FROM security_events WHERE {people} ORDER BY t DESC LIMIT 60", (since,))]
        incs = [dict(r) for r in c.execute(
            "SELECT id, created, status, kind, node, resource, title FROM incidents WHERE kind LIKE 'sec_%' ORDER BY created DESC LIMIT 25")]
        # latest triage per incident: the final report if there is one, else the latest diagnosis
        # (incidents awaiting approval have a diagnosis but no report yet)
        reports = c.execute(
            "SELECT s.incident_id, s.node, s.data FROM agent_steps s JOIN incidents i ON i.id = s.incident_id "
            "WHERE i.kind LIKE 'sec_%' AND s.node IN ('report','diagnose') ORDER BY s.created").fetchall()
    for s in sources:
        s["classification"] = classify_ip(s["src_ip"])
        s["nodes"] = (s["nodes"] or "").split(",")
    techniques: dict[str, dict] = {}
    assessment: dict[str, str] = {}
    latest: dict[str, dict] = {}
    for iid, node, data in reports:
        d = json.loads(data)
        d = d if node == "report" else (d.get("diagnosis") or {})
        if d.get("assessment") and (node == "report" or latest.get(iid, {}).get("_from") != "report"):
            latest[iid] = d | {"_from": node}
    for iid, rep in latest.items():
        assessment[iid] = rep.get("assessment")
        for t in rep.get("attack_techniques") or []:
            e = techniques.setdefault(t["technique_id"], attack.describe(t["technique_id"]) | {"incidents": 0, "high": 0})
            e["incidents"] += 1
            e["high"] += t.get("confidence") == "high"
    for i in incs:
        i["assessment"] = assessment.get(i["id"])
    s = lab.state()
    return {"hours": hours, "events_by_kind": by_kind, "events_by_node": by_node, "events_total": sum(by_kind.values()), "platform_checks": platform_checks,
            "top_sources": sources, "recent": recent, "incidents": incs,
            "techniques": sorted(techniques.values(), key=lambda t: -t["incidents"]),
            "coverage": runtime.sec_coverage,
            "lab": {"host": lab.HOST, "accounts": list(lab.ACCOUNTS), "scenarios": lab.SCENARIOS,
                    "blocked_ips": s["blocked_ips"], "disabled_accounts": s["disabled_accounts"],
                    "isolated_endpoints": s["isolated_endpoints"], "quarantined_files": s["quarantined_files"],
                    "vulnerabilities": {n: [v for v in r.get("results", []) if v["status"] == "vulnerable"]
                                        for n, r in runtime.vulns.items()}}}


@router.post("/lab/{scenario}")
def run_lab(scenario: str, user: Approver):
    if scenario not in lab.SCENARIOS:
        raise HTTPException(404, "unknown scenario")
    n = lab.run(scenario)
    store.audit(None, f"human:{user['username']}", "lab_scenario_started", {"scenario": scenario, "events": n})
    return {"ok": True, "events": n, "opened": runtime.scan_once()}


@router.post("/lab-reset")
def reset_lab(user: Admin):
    lab.reset()
    store.audit(None, f"human:{user['username']}", "lab_reset", None)
    return {"ok": True}

"""Read-only reporting over the incident store: dashboard and pipeline aggregates, the safety record,
the plain-language recent-incidents feed. Nothing here writes; store.py owns all writes."""
from __future__ import annotations

import json
import time

from backend.core.config import NON_HUMAN_ACTORS
from backend.core.store import FINAL_STATUSES, _FINAL_PH, conn


def active_counts() -> dict:
    """Unfinished real incidents, by what is happening to them (for the sidebar and the overview)."""
    with conn() as c:
        rows = dict(c.execute(f"SELECT status, COUNT(*) FROM incidents WHERE origin = 'real' AND status NOT IN ({_FINAL_PH}) "
                              "GROUP BY status", FINAL_STATUSES).fetchall())
    working = sum(rows.get(s, 0) for s in ("open", "investigating", "executing", "verifying"))
    return {"active": sum(rows.values()), "in_progress": working, "awaiting_approval": rows.get("awaiting_approval", 0),
            "needs_person": sum(n for s, n in rows.items() if s in ("needs_human", "diagnosed", "not_resolved", "action_denied"))}


# What each incident kind is about, for reporting (one definition; the detector's kinds follow these prefixes).
CATEGORY_SQL = ("CASE WHEN kind LIKE 'sec\\_%' ESCAPE '\\' THEN 'Security' "
                "WHEN kind LIKE 'model\\_%' ESCAPE '\\' THEN 'Models' "
                "WHEN kind LIKE 'gpu\\_%' ESCAPE '\\' THEN 'GPU' "
                "WHEN kind LIKE 'svc\\_%' ESCAPE '\\' OR kind = 'service_unhealthy' THEN 'Services' "
                "WHEN kind IN ('node_unreachable', 'node_rebooted', 'high_cpu', 'high_memory') "
                "OR kind LIKE 'disk\\_%' ESCAPE '\\' OR kind LIKE 'net\\_%' ESCAPE '\\' THEN 'Nodes' "
                "ELSE 'Other' END")


def pipeline_counts() -> dict:
    """Cheap aggregate counts for the metrics exporter (all indexed or small tables)."""
    with conn() as c:
        inc = c.execute(f"SELECT CASE WHEN kind LIKE 'sec\\_%' ESCAPE '\\' THEN 'security' ELSE 'sre' END d, {CATEGORY_SQL} cat, "
                        "status, COUNT(*) FROM incidents GROUP BY d, cat, status").fetchall()
        return {
            "incidents": {(r[0], r[1], r[2]): r[3] for r in inc},
            "approvals_pending": c.execute("SELECT COUNT(*) FROM approval_requests WHERE status='pending'").fetchone()[0],
            "policy": dict(c.execute("SELECT decision, COUNT(*) FROM policy_decisions GROUP BY decision").fetchall()),
            "executions": {("ok" if k else "failed"): v for k, v in
                           c.execute("SELECT ok, COUNT(*) FROM execution_records GROUP BY ok").fetchall()},
            "verifications": dict(c.execute("SELECT outcome, COUNT(*) FROM verification_results GROUP BY outcome").fetchall()),
        }


def stats(now: float) -> dict:
    """Dashboard aggregates, computed in SQL (one connection, no row materialisation)."""
    week, day = now - 7 * 86400, now - 86400
    start = int(now // 3600) * 3600 - 23 * 3600
    ph = ",".join("?" * len(FINAL_STATUSES))
    with conn() as c:
        by_status = dict(c.execute("SELECT status, COUNT(*) FROM incidents GROUP BY status").fetchall())
        by_kind = dict(c.execute("SELECT kind, COUNT(*) FROM incidents GROUP BY kind").fetchall())
        mttr = c.execute("SELECT AVG(updated - created) FROM incidents WHERE status='resolved' AND created >= ?",
                         (week,)).fetchone()[0]
        ttd = c.execute(
            "SELECT AVG(a.t - i.created) FROM incidents i JOIN (SELECT incident_id, MIN(created) AS t FROM audit_events "
            "WHERE event='diagnosis' GROUP BY incident_id) a ON a.incident_id = i.id WHERE i.created >= ?", (week,)).fetchone()[0]
        resolved_24h = c.execute("SELECT COUNT(*) FROM incidents WHERE status='resolved' AND updated >= ?", (day,)).fetchone()[0]
        opened_h = dict(c.execute("SELECT CAST((created - ?) / 3600 AS INT), COUNT(*) FROM incidents WHERE created >= ? "
                                  "GROUP BY 1", (start, start)).fetchall())
        resolved_h = dict(c.execute("SELECT CAST((updated - ?) / 3600 AS INT), COUNT(*) FROM incidents "
                                    "WHERE status='resolved' AND updated >= ? GROUP BY 1", (start, start)).fetchall())
        open_n = c.execute(f"SELECT COUNT(*) FROM incidents WHERE status NOT IN ({ph})", FINAL_STATUSES).fetchone()[0]
    return {
        "total": sum(by_status.values()), "open": open_n,
        "awaiting_approval": by_status.get("awaiting_approval", 0), "resolved_24h": resolved_24h,
        "mttr_s": round(mttr, 1) if mttr is not None else None,
        "time_to_diagnosis_s": round(ttd, 1) if ttd is not None else None,
        "by_status": by_status, "by_kind": by_kind,
        "hourly": [{"t": start + h * 3600, "opened": opened_h.get(h, 0), "resolved": resolved_h.get(h, 0)} for h in range(24)],
    }


def table_counts() -> dict:
    with conn() as c:
        out = {}
        for t in ("policy_decisions", "execution_records", "verification_results", "approval_requests"):
            out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        out["policy_deny"] = c.execute("SELECT COUNT(*) FROM policy_decisions WHERE decision='deny'").fetchone()[0]
        out["verified_resolved"] = c.execute("SELECT COUNT(*) FROM verification_results WHERE outcome='resolved'").fetchone()[0]
        out["approved"] = c.execute("SELECT COUNT(*) FROM approval_requests WHERE status='approved'").fetchone()[0]
        out["rejected"] = c.execute("SELECT COUNT(*) FROM approval_requests WHERE status='rejected'").fetchone()[0]
        out["container_events"] = c.execute("SELECT COUNT(*) FROM container_events").fetchone()[0]
    return out


def _median(xs: list[float]) -> float | None:
    xs = sorted(x for x in xs if x is not None and x >= 0)
    if not xs:
        return None
    m = len(xs) // 2
    return round(xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2, 1)


def overview(now: float, days: int = 7, real_only: bool = True) -> dict:
    """The autonomy pipeline, response times and guardrail record for the dashboard.
    Everything is derived from recorded workflow state; nothing is estimated.
    `real_only` keeps genuine incidents: no drills, nothing raised in a maintenance window, and a recurrence chain
    counts once. The guardrail record is all-time over every execution, drills included: it is a guarantee."""
    since = now - days * 86400
    only = "AND origin = 'real'" if real_only else ""
    with conn() as c:
        # the window's incidents in a temp table: every query below is a constant string (no IN-list surgery)
        c.execute(f"CREATE TEMP TABLE win AS SELECT id, created, updated, status, {CATEGORY_SQL} AS category FROM incidents "
                  f"WHERE created >= ? {only}", (since,))
        incs = [dict(r) for r in c.execute("SELECT * FROM win")]
        W = "(SELECT id FROM win)"
        rows = lambda sql, *a: c.execute(sql, a).fetchall()
        diagnosed = {r[0] for r in rows(f"SELECT DISTINCT incident_id FROM agent_steps WHERE node='diagnose' AND incident_id IN {W} "
                                         "AND data LIKE '%\"diagnosis\"%'")}
        first = lambda ev: dict(rows(f"SELECT incident_id, MIN(created) FROM audit_events WHERE event=? AND incident_id IN {W} "
                                     "GROUP BY incident_id", ev))
        t_diag, t_prop = first("diagnosis"), first("action_proposed")
        props = dict(rows(f"SELECT incident_id, id FROM action_proposals WHERE incident_id IN {W}"))
        policy = dict(rows(f"SELECT d.decision, COUNT(*) FROM policy_decisions d JOIN action_proposals p ON p.id = d.action_id "
                           f"WHERE p.incident_id IN {W} GROUP BY d.decision"))
        appr = [dict(r) for r in rows(f"SELECT incident_id, status, created, decided, approver FROM approval_requests WHERE incident_id IN {W}")]
        execs = [dict(r) for r in rows(f"SELECT e.action_id, e.ok, e.created, p.incident_id FROM execution_records e "
                                       f"JOIN action_proposals p ON p.id = e.action_id WHERE p.incident_id IN {W}")]
        verif = [dict(r) for r in rows(f"SELECT incident_id, outcome, created FROM verification_results WHERE incident_id IN {W}")]
        refused = rows(f"SELECT COUNT(*) FROM audit_events WHERE event='evidence_request_refused' AND incident_id IN {W}")[0][0]
        reports = [json.loads(r[0]) for r in rows(f"SELECT data FROM agent_steps WHERE node='report' AND incident_id IN {W}")]
        closes = [json.loads(r[0] or "{}") for r in rows(f"SELECT data FROM audit_events WHERE event='closed' AND incident_id IN {W}")]
        usage_rows = rows(f"SELECT incident_id, data FROM agent_steps WHERE node='diagnose' AND incident_id IN {W} "
                          f"UNION ALL SELECT incident_id, data FROM action_proposals WHERE incident_id IN {W}")
        # all-time guarantee: every execution has an approved, human approval for the same action
        unapproved = c.execute(
            "SELECT COUNT(*) FROM execution_records e LEFT JOIN approval_requests a ON a.action_id = e.action_id "
            f"AND a.status='approved' AND a.approver IS NOT NULL AND lower(a.approver) NOT IN ({','.join('?' * len(NON_HUMAN_ACTORS))}) "
            "WHERE a.id IS NULL", NON_HUMAN_ACTORS).fetchone()[0]
        refusals = c.execute("SELECT COUNT(*) FROM execution_records WHERE data LIKE '%\"refused\"%'").fetchone()[0]
        all_exec = c.execute("SELECT COUNT(*) FROM execution_records").fetchone()[0]
        days_rows = c.execute(f"SELECT CAST((created - ?) / 86400 AS INT) d, COUNT(*) FROM incidents WHERE created >= ? {only} GROUP BY d",
                              (int(now // 86400) * 86400 - (days - 1) * 86400,) * 2).fetchall()
        res_rows = c.execute(f"SELECT CAST((updated - ?) / 86400 AS INT) d, COUNT(*) FROM incidents WHERE status='resolved' AND updated >= ? "
                             f"{only} GROUP BY d", (int(now // 86400) * 86400 - (days - 1) * 86400,) * 2).fetchall()

    created = {i["id"]: i["created"] for i in incs}
    tokens: dict[str, int] = {}
    for iid, raw in usage_rows:
        d = json.loads(raw or "{}")
        for u in (d.get("meta") or d.get("model_meta") or {}).get("usage") or []:
            tokens[iid] = tokens.get(iid, 0) + int((u or {}).get("total_tokens") or 0)
    support = [r["observation_evidence_support"] for r in reports if r.get("observation_evidence_support") is not None]
    verdicts = [c.get("disposition") for c in closes if c.get("disposition")]
    n_appr, n_rej = sum(a["status"] == "approved" for a in appr), sum(a["status"] == "rejected" for a in appr)
    decided = {a["incident_id"]: a for a in appr if a["decided"]}
    verified_at = {v["incident_id"]: v["created"] for v in verif}
    day0 = int(now // 86400) * 86400 - (days - 1) * 86400
    opened, resolved = dict(days_rows), dict(res_rows)
    count = lambda k: dict(sorted({v: sum(i[k] == v for i in incs) for v in {i[k] for i in incs}}.items(), key=lambda x: -x[1]))
    return {
        "window_days": days,
        "real_only": real_only,
        "by_status": count("status"),
        "by_category": count("category"),
        "pipeline": {
            "detected": len(incs),
            "diagnosed": len(diagnosed),
            "proposed": len(props),
            "policy": {"allow": policy.get("allow", 0), "requires_approval": policy.get("requires_approval", 0),
                       "deny": policy.get("deny", 0)},
            "approval": {s: sum(a["status"] == s for a in appr) for s in ("pending", "approved", "rejected")},
            "executed": sum(e["ok"] for e in execs),
            "verified": {o: sum(v["outcome"] == o for v in verif) for o in ("resolved", "not_resolved")},
            "closed_by_human": sum(i["status"] == "closed" for i in incs),
            "open": sum(i["status"] not in FINAL_STATUSES for i in incs),
            "recovered": sum(i["status"] == "recovered" for i in incs),
        },
        "timing": {
            "detect_to_diagnosis_s": _median([t_diag[i] - created[i] for i in t_diag if i in created]),
            "detect_to_proposal_s": _median([t_prop[i] - created[i] for i in t_prop if i in created]),
            "approval_wait_s": _median([a["decided"] - a["created"] for a in decided.values()]),
            "approval_to_verified_s": _median([verified_at[i] - decided[i]["decided"] for i in decided if i in verified_at]),
        },
        "quality": {
            "proposal_acceptance": round(n_appr / (n_appr + n_rej), 3) if n_appr + n_rej else None,
            "evidence_support_mean": round(sum(support) / len(support), 3) if support else None,
            "false_positive_rate": round(verdicts.count("false_positive") / len(verdicts), 3) if verdicts else None,
            "human_verdicts": len(verdicts),
            "tokens_per_incident_median": _median(list(tokens.values())),
            "tokens_total": sum(tokens.values()),
        },
        "guardrails": {
            "policy_denials": policy.get("deny", 0),
            "refused_tool_requests": refused,
            "executor_refusals": refusals,
            "executions_total": all_exec,
            "executions_without_human_approval": unapproved,
        },
        "daily": [{"t": day0 + d * 86400, "opened": opened.get(d, 0), "resolved": resolved.get(d, 0)} for d in range(days)],
        "recent": recent_incidents(8),
    }


def recent_incidents(limit: int = 8) -> list[dict]:
    """Latest incidents with what the agent proposed and how policy ruled, for a plain-language feed."""
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT i.id, i.created, i.updated, i.status, i.kind, i.node, i.resource, i.title, i.occurrences, "
            "(SELECT data FROM action_proposals p WHERE p.incident_id = i.id ORDER BY created DESC LIMIT 1) proposal, "
            "(SELECT d.decision FROM policy_decisions d JOIN action_proposals p ON p.id = d.action_id "
            " WHERE p.incident_id = i.id ORDER BY d.created DESC LIMIT 1) policy "
            "FROM incidents i WHERE i.origin = 'real' ORDER BY i.updated DESC LIMIT ?", (limit,))]
    for r in rows:
        p = json.loads(r.pop("proposal") or "null") or {}
        r["operation"] = p.get("operation")
        r["target"] = (p.get("target") or {}).get("resource_id")
    return rows

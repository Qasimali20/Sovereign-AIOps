"""Incident, workflow and audit store (SQLite for the first slice; schema maps 1:1
onto the blueprint's Postgres entities so it can be migrated later)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from contextlib import contextmanager
from typing import Any

from backend.core.config import DB_PATH, NON_HUMAN_ACTORS

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY, created REAL, updated REAL, status TEXT,
  kind TEXT, node TEXT, resource TEXT, title TEXT, fingerprint TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS incidents_fp ON incidents(fingerprint, status);
CREATE TABLE IF NOT EXISTS evidence_records (
  id TEXT PRIMARY KEY, incident_id TEXT, created REAL, source TEXT, tool TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS agent_steps (
  id TEXT PRIMARY KEY, incident_id TEXT, created REAL, node TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS action_proposals (
  id TEXT PRIMARY KEY, incident_id TEXT, created REAL, data TEXT
);
CREATE TABLE IF NOT EXISTS policy_decisions (
  id TEXT PRIMARY KEY, action_id TEXT, created REAL, decision TEXT, reasons TEXT
);
CREATE TABLE IF NOT EXISTS approval_requests (
  id TEXT PRIMARY KEY, action_id TEXT, incident_id TEXT, created REAL,
  status TEXT, approver TEXT, decided REAL, comment TEXT
);
CREATE TABLE IF NOT EXISTS execution_records (
  id TEXT PRIMARY KEY, action_id TEXT, created REAL, ok INTEGER, data TEXT
);
CREATE TABLE IF NOT EXISTS verification_results (
  id TEXT PRIMARY KEY, incident_id TEXT, action_id TEXT, created REAL, outcome TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS container_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, container TEXT, action TEXT, attrs TEXT
);
CREATE INDEX IF NOT EXISTS container_events_ct ON container_events(container, t);
CREATE TABLE IF NOT EXISTS deployments (
  id TEXT PRIMARY KEY, created REAL, updated REAL, status TEXT, by TEXT, playbook TEXT, nodes TEXT,
  git_commit TEXT, reason TEXT, preview TEXT, result TEXT
);
CREATE INDEX IF NOT EXISTS incidents_created ON incidents(created);
CREATE INDEX IF NOT EXISTS evidence_incident ON evidence_records(incident_id);
CREATE INDEX IF NOT EXISTS steps_incident ON agent_steps(incident_id);
CREATE INDEX IF NOT EXISTS proposals_incident ON action_proposals(incident_id);
CREATE INDEX IF NOT EXISTS approvals_status ON approval_requests(status, created);
CREATE INDEX IF NOT EXISTS approvals_incident ON approval_requests(incident_id);
CREATE INDEX IF NOT EXISTS policy_action ON policy_decisions(action_id);
CREATE INDEX IF NOT EXISTS exec_action ON execution_records(action_id);
CREATE INDEX IF NOT EXISTS verify_incident ON verification_results(incident_id);
CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY, created REAL, incident_id TEXT, actor TEXT, event TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS audit_incident ON audit_events(incident_id, created);
CREATE INDEX IF NOT EXISTS audit_created ON audit_events(created);
CREATE INDEX IF NOT EXISTS audit_event ON audit_events(event, created);
CREATE TABLE IF NOT EXISTS model_evals (
  id TEXT PRIMARY KEY, run_id TEXT, t REAL, model TEXT, suite TEXT, node TEXT, score REAL, passed INTEGER,
  total INTEGER, latency_s REAL, error TEXT, cases TEXT
);
CREATE INDEX IF NOT EXISTS model_evals_model_t ON model_evals(model, t);
CREATE TABLE IF NOT EXISTS diagnosis_feedback (
  incident_id TEXT PRIMARY KEY, created REAL, verdict TEXT, note TEXT, by TEXT
);
"""

# An incident in one of these states is finished: a recurrence opens a new incident.
FINAL_STATUSES = ("resolved", "recovered", "closed", "action_rejected")
_FINAL_PH = ",".join("?" * len(FINAL_STATUSES))


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@contextmanager
def conn():
    """Short-lived connection per unit of work. WAL lets readers run alongside the
    single writer; busy_timeout absorbs brief write contention between threads."""
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


GENESIS = "0" * 64


def _chain_hash(prev: str, seq: int, r) -> str:
    """Canonical hash of one audit row chained to its predecessor."""
    body = json.dumps([seq, r["id"], repr(r["created"]), r["incident_id"], r["actor"], r["event"], r["data"]],
                      separators=(",", ":"))
    return hashlib.sha256((prev + body).encode()).hexdigest()


def _migrate_incident_origin(c) -> None:
    """origin: real | drill | planned (exercises and planned work are kept apart from real incidents);
    occurrences: how many times a flapping problem came back into the same incident."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(incidents)")}
    if "origin" not in cols:
        c.execute("ALTER TABLE incidents ADD COLUMN origin TEXT NOT NULL DEFAULT 'real'")
    if "occurrences" not in cols:
        c.execute("ALTER TABLE incidents ADD COLUMN occurrences INTEGER NOT NULL DEFAULT 1")
    if "recurrence_of" not in cols:
        c.execute("ALTER TABLE incidents ADD COLUMN recurrence_of TEXT")


def _migrate_audit_chain(c) -> None:
    """Add the hash chain to audit_events (once), chain any unchained rows, then make the table append-only."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(audit_events)")}
    if "hash" not in cols:
        c.execute("ALTER TABLE audit_events ADD COLUMN seq INTEGER")
        c.execute("ALTER TABLE audit_events ADD COLUMN hash TEXT")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS audit_seq ON audit_events(seq)")
    rows = c.execute("SELECT rowid AS rid, * FROM audit_events WHERE seq IS NULL ORDER BY created, rowid").fetchall()
    if rows:
        c.execute("DROP TRIGGER IF EXISTS audit_no_update")
        last = c.execute("SELECT seq, hash FROM audit_events WHERE seq IS NOT NULL ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev = (last["seq"], last["hash"]) if last else (0, GENESIS)
        for r in rows:
            seq += 1
            prev = _chain_hash(prev, seq, r)
            c.execute("UPDATE audit_events SET seq=?, hash=? WHERE rowid=?", (seq, prev, r["rid"]))
    c.executescript("""
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
  BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
  BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;""")


def verify_audit_chain(db_path: Path | None = None, after_seq: int = 0, after_hash: str = GENESIS) -> dict:
    """Recompute the links. Any edited, deleted or reordered row breaks the chain from that point on.
    `db_path` verifies another copy (a backup). `after_seq/after_hash` continue from a point already verified
    (incremental check of new rows only); the default verifies the whole chain."""
    c = sqlite3.connect(db_path or DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute("SELECT * FROM audit_events WHERE seq > ? ORDER BY seq", (after_seq,)).fetchall()
    finally:
        c.close()
    prev = after_hash
    for i, r in enumerate(rows, after_seq + 1):
        if r["seq"] != i:
            return {"ok": False, "events": len(rows), "first_bad_seq": i, "reason": "gap or reordering in sequence"}
        prev_expected = _chain_hash(prev, i, r)
        if r["hash"] != prev_expected:
            return {"ok": False, "events": len(rows), "first_bad_seq": i, "reason": "hash mismatch (row altered)"}
        prev = r["hash"]
    return {"ok": True, "events": after_seq + len(rows), "checked": len(rows), "head": prev, "head_seq": after_seq + len(rows)}


EVIDENCE_RETENTION_DAYS = 90       # after the incident closed; open incidents keep everything
EVENT_RETENTION_DAYS = 30          # raw security and container events


def prune(now: float | None = None) -> dict:
    """Retention. Never touches audit_events (hash-chained, append-only) or incidents themselves."""
    now = now or time.time()
    old_closed = (f"SELECT id FROM incidents WHERE status IN ({_FINAL_PH}) AND updated < ?",
                  (*FINAL_STATUSES, now - EVIDENCE_RETENTION_DAYS * 86400))
    out = {}
    with conn() as c:
        for table in ("evidence_records", "agent_steps"):
            out[table] = c.execute(f"DELETE FROM {table} WHERE incident_id IN ({old_closed[0]})", old_closed[1]).rowcount
        cut = now - EVENT_RETENTION_DAYS * 86400
        out["security_events"] = c.execute("DELETE FROM security_events WHERE t < ?", (cut,)).rowcount
        out["container_events"] = c.execute("DELETE FROM container_events WHERE t < ?", (cut,)).rowcount
    if any(out.values()):
        audit(None, "retention", "pruned", out)
    return out


def init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.executescript(SCHEMA)
        _migrate_incident_origin(c)
        _migrate_audit_chain(c)
    from backend.security import events as _sec
    _sec.init()


def _j(v: Any) -> str:
    return json.dumps(v, default=str)


def audit(incident_id: str | None, actor: str, event: str, data: Any = None) -> None:
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")          # serialise writers so the chain has one head
        _audit(c, incident_id, actor, event, data)


def _audit(c, incident_id: str | None, actor: str, event: str, data: Any = None) -> None:
    """Append one chained audit row on `c`, which must already hold the write lock (BEGIN IMMEDIATE): a state
    change and its audit record then commit together or not at all."""
    row = {"id": new_id("AUD"), "created": time.time(), "incident_id": incident_id, "actor": actor,
           "event": event, "data": _j(data)}
    last = c.execute("SELECT seq, hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
    seq, prev = (last["seq"] + 1, last["hash"]) if last else (1, GENESIS)
    c.execute("INSERT INTO audit_events (id, created, incident_id, actor, event, data, seq, hash) "
              "VALUES (?,?,?,?,?,?,?,?)", (*row.values(), seq, _chain_hash(prev, seq, row)))


# ---------------------------------------------------------------- incidents
FLAP_WINDOW_S = 6 * 3600       # a problem back within this long after clearing is a recurrence of the same incident


def open_incident(kind: str, node: str, resource: str, title: str, fingerprint: str,
                  detail: dict) -> tuple[str, bool]:
    """Create an incident unless an open one with the same fingerprint exists. Returns (incident_id, created).

    A problem that comes back within FLAP_WINDOW_S of clearing by itself gets a new incident (its own evidence,
    proposal, approval and execution) chained to the previous one: `recurrence_of` and a running `occurrences`
    count. The previous one is marked `merged`, so lists show the problem once, with how often it recurred."""
    now = time.time()
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")          # take the write lock before the dedup check
        row = c.execute(f"SELECT id FROM incidents WHERE fingerprint=? AND status NOT IN ({_FINAL_PH})",
                        (fingerprint, *FINAL_STATUSES)).fetchone()
        if row:
            return row["id"], False
        prev = c.execute("SELECT id, occurrences, origin FROM incidents WHERE fingerprint=? AND status='recovered' "
                         "AND updated>? ORDER BY updated DESC LIMIT 1", (fingerprint, now - FLAP_WINDOW_S)).fetchone()
        iid = new_id("INC")
        origin = classify_origin(kind, node, resource)
        c.execute("INSERT INTO incidents (id, created, updated, status, kind, node, resource, title, fingerprint, detail, "
                  "origin, occurrences, recurrence_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (iid, now, now, "open", kind, node, resource, title, fingerprint, _j(detail), origin,
                   prev["occurrences"] + 1 if prev else 1, prev["id"] if prev else None))
        _audit(c, iid, "detector", "incident_opened", {"kind": kind, "title": title, "detail": detail, "origin": origin,
                                                      "recurrence_of": prev["id"] if prev else None})
        if prev:
            c.execute("UPDATE incidents SET origin='merged' WHERE id=?", (prev["id"],))
            _audit(c, prev["id"], "detector", "recurred", {"incident": iid, "previous_origin": prev["origin"]})
    return iid, True




def _maint_path():
    return DB_PATH.with_name("maintenance.json")      # next to the database (tests point DB_PATH elsewhere)


def maintenance_windows() -> list[dict]:
    try:
        return json.loads(_maint_path().read_text())
    except (FileNotFoundError, ValueError):
        return []


def add_maintenance(node: str, minutes: float, reason: str, by: str) -> dict:
    """Announce planned work on a node ('*' = all): incidents it causes are tagged planned, not real."""
    now = time.time()
    w = {"node": node, "start": now, "end": now + minutes * 60, "reason": reason, "by": by}
    keep = [x for x in maintenance_windows() if x["end"] > now - 7 * 86400]
    _maint_path().write_text(json.dumps(keep + [w], indent=1))
    audit(None, by, "maintenance_window", w)
    return w


def _pause_path():
    return DB_PATH.with_name("ai_paused.json")


def ai_paused(resource: str) -> dict | None:
    """An evaluation can pause the AI for one resource: incidents are still detected and recorded, but no agent
    investigates them (the manual arm of an MTTR comparison must not see AI output)."""
    try:
        p = json.loads(_pause_path().read_text()).get(resource)
    except (FileNotFoundError, ValueError):
        return None
    return p if p and p.get("until", 0) > time.time() else None


def set_ai_pause(resource: str, minutes: float | None, reason: str, by: str) -> None:
    try:
        cur = json.loads(_pause_path().read_text())
    except (FileNotFoundError, ValueError):
        cur = {}
    if minutes:
        cur[resource] = {"until": time.time() + minutes * 60, "reason": reason, "by": by}
    else:
        cur.pop(resource, None)
    _pause_path().write_text(json.dumps(cur, indent=1))
    audit(None, by, "ai_paused" if minutes else "ai_resumed", {"resource": resource, "minutes": minutes, "reason": reason})


def classify_origin(kind: str, node: str, resource: str) -> str:
    from backend.core import config
    if node == "LAB" or resource in config.TEST_RESOURCES:
        return "drill"                                    # the isolated lab and the fault-injection test service
    now = time.time()
    if any(w["start"] <= now <= w["end"] and w["node"] in (node, "*") for w in maintenance_windows()):
        return "planned"
    return "real"


def approvals_with_context(status: str) -> list[dict]:
    """Approvals with their proposal and the incident's headline fields, in one query (no evidence payloads)."""
    with conn() as c:
        rows = c.execute(
            "SELECT a.*, p.data AS proposal, i.title, i.node, i.kind, i.status AS incident_status, i.origin "
            "FROM approval_requests a LEFT JOIN action_proposals p ON p.id = a.action_id "
            "LEFT JOIN incidents i ON i.id = a.incident_id WHERE a.status = ? ORDER BY a.created DESC", (status,)).fetchall()
    out = []
    for r in map(dict, rows):
        inc = {k: r.pop(k) for k in ("title", "node", "kind", "origin")} | {"id": r["incident_id"], "status": r.pop("incident_status")}
        out.append(r | {"proposal": json.loads(r["proposal"]) if r["proposal"] else None, "incident": inc})
    return out


def decided_but_parked() -> list[tuple[str, str]]:
    """(incident, approval) pairs where a person decided but the incident is still awaiting_approval."""
    with conn() as c:
        rows = c.execute("SELECT i.id, a.id FROM incidents i JOIN approval_requests a ON a.incident_id = i.id "
                         "WHERE i.status = 'awaiting_approval' AND a.status IN ('approved', 'rejected') "
                         "AND a.decided = (SELECT MAX(decided) FROM approval_requests WHERE incident_id = i.id)").fetchall()
    return [(r[0], r[1]) for r in rows]


def latest_status(fingerprint: str) -> str | None:
    with conn() as c:
        r = c.execute("SELECT status FROM incidents WHERE fingerprint=? ORDER BY updated DESC LIMIT 1", (fingerprint,)).fetchone()
    return r["status"] if r else None


def set_status(incident_id: str, status: str, actor: str = "orchestrator", keep_final: bool = False) -> bool:
    """keep_final: do not overwrite a final status (a person closed it, or the detector saw it recover)."""
    guard = f" AND status NOT IN ({_FINAL_PH})" if keep_final else ""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        n = c.execute(f"UPDATE incidents SET status=?, updated=? WHERE id=?{guard}",
                      (status, time.time(), incident_id, *(FINAL_STATUSES if keep_final else ()))).rowcount
        if n:
            _audit(c, incident_id, actor, "status", {"status": status})
    return bool(n)


def open_incidents_of(kinds: tuple[str, ...]) -> list[dict]:
    kp = ",".join("?" * len(kinds))
    with conn() as c:
        rows = c.execute(f"SELECT id,created,status,kind,node,resource,fingerprint FROM incidents "
                         f"WHERE kind IN ({kp}) AND status NOT IN ({_FINAL_PH})", (*kinds, *FINAL_STATUSES)).fetchall()
    return [dict(r) for r in rows]


def mark_recovered(incident_id: str, created: float, evidence: str) -> bool:
    """Condition observed cleared by the detector. Compare-and-set so a concurrent human close wins."""
    now = time.time()
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        n = c.execute(f"UPDATE incidents SET status='recovered', updated=? WHERE id=? AND status NOT IN ({_FINAL_PH})",
                      (now, incident_id, *FINAL_STATUSES)).rowcount
        if n:
            _audit(c, incident_id, "detector", "condition_cleared", {"evidence": evidence, "duration_s": round(now - created)})
            _audit(c, incident_id, "detector", "status", {"status": "recovered"})
    return bool(n)


def get_incident(incident_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["detail"] = json.loads(d["detail"])
    return d


def list_incidents(limit: int = 50) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT id,created,updated,status,kind,node,resource,title,origin,occurrences,recurrence_of FROM incidents "
                         "ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def add_evidence(incident_id: str, source: str, tool: str, data: Any) -> str:
    eid = new_id("EV")
    with conn() as c:
        c.execute("INSERT INTO evidence_records VALUES (?,?,?,?,?,?)",
                  (eid, incident_id, time.time(), source, tool, _j(data)))
    return eid


def evidence_ids(incident_id: str) -> set[str]:
    with conn() as c:
        return {r["id"] for r in c.execute("SELECT id FROM evidence_records WHERE incident_id=?",
                                            (incident_id,)).fetchall()}


def add_step(incident_id: str, node: str, data: Any) -> None:
    with conn() as c:
        c.execute("INSERT INTO agent_steps VALUES (?,?,?,?,?)",
                  (new_id("STEP"), incident_id, time.time(), node, _j(data)))


def save_proposal(incident_id: str, proposal: dict) -> None:
    with conn() as c:
        c.execute("INSERT INTO action_proposals VALUES (?,?,?,?)",
                  (proposal["action_id"], incident_id, time.time(), _j(proposal)))
    audit(incident_id, "planning_agent", "action_proposed", proposal)


def annotate_proposal(action_id: str, fields: dict) -> None:
    """Merge server-side annotations (policy verdict) into the stored proposal."""
    with conn() as c:
        row = c.execute("SELECT data FROM action_proposals WHERE id=?", (action_id,)).fetchone()
        if row:
            c.execute("UPDATE action_proposals SET data=? WHERE id=?",
                      (_j(json.loads(row["data"]) | fields), action_id))


def save_policy(action_id: str, incident_id: str, decision: str, reasons: list[str]) -> None:
    with conn() as c:
        c.execute("INSERT INTO policy_decisions VALUES (?,?,?,?,?)",
                  (new_id("POL"), action_id, time.time(), decision, _j(reasons)))
    audit(incident_id, "policy_engine", "policy_decision",
          {"action_id": action_id, "decision": decision, "reasons": reasons})


def create_approval(action_id: str, incident_id: str) -> str:
    aid = new_id("APR")
    with conn() as c:
        c.execute("INSERT INTO approval_requests VALUES (?,?,?,?,?,?,?,?)",
                  (aid, action_id, incident_id, time.time(), "pending", None, None, None))
    audit(incident_id, "orchestrator", "approval_requested", {"approval_id": aid, "action_id": action_id})
    return aid


def decide_approval(approval_id: str, approve: bool, approver: str, comment: str) -> dict | None:
    status = "approved" if approve else "rejected"
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        # compare-and-set: of two concurrent decisions exactly one wins
        cur = c.execute("UPDATE approval_requests SET status=?, approver=?, decided=?, comment=? "
                        "WHERE id=? AND status='pending'", (status, approver, time.time(), comment, approval_id))
        if cur.rowcount != 1:
            return None
        out = dict(c.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone())
        _audit(c, out["incident_id"], f"human:{approver}", f"approval_{status}",
               {"approval_id": approval_id, "action_id": out["action_id"], "comment": comment})
    return out


def expire_pending(incident_id: str, reason: str) -> int:
    """The problem cleared before anyone decided: the pending approval can no longer be granted
    (compare-and-set, so a concurrent human decision either wins or finds it expired)."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        n = c.execute("UPDATE approval_requests SET status='expired', decided=?, comment=? "
                      "WHERE incident_id=? AND status='pending'", (time.time(), reason, incident_id)).rowcount
        if n:
            _audit(c, incident_id, "detector", "approval_expired", {"reason": reason})
    return n


def get_approval(approval_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
    return dict(r) if r else None


def list_approvals(status: str = "pending") -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM approval_requests WHERE status=? ORDER BY created",
                         (status,)).fetchall()
    return [dict(r) for r in rows]


def save_execution(action_id: str, incident_id: str, ok: bool, data: Any) -> None:
    with conn() as c:
        c.execute("INSERT INTO execution_records VALUES (?,?,?,?,?)",
                  (new_id("EXE"), action_id, time.time(), int(ok), _j(data)))
    audit(incident_id, "restricted_executor", "executed", {"action_id": action_id, "ok": ok, "data": data})


def save_verification(incident_id: str, action_id: str | None, outcome: str, data: Any) -> None:
    with conn() as c:
        c.execute("INSERT INTO verification_results VALUES (?,?,?,?,?,?)",
                  (new_id("VER"), incident_id, action_id, time.time(), outcome, _j(data)))
    audit(incident_id, "verifier", "verification", {"outcome": outcome, "data": data})


def incident_bundle(incident_id: str) -> dict | None:
    """Everything recorded for one incident: the audit trail / incident report."""
    inc = get_incident(incident_id)
    if not inc:
        return None
    out: dict[str, Any] = {"incident": inc}
    with conn() as c:
        for table, key in [("evidence_records", "incident_id"), ("agent_steps", "incident_id"),
                           ("action_proposals", "incident_id"), ("approval_requests", "incident_id"),
                           ("verification_results", "incident_id"), ("audit_events", "incident_id")]:
            rows = [dict(r) for r in c.execute(f"SELECT * FROM {table} WHERE {key}=? ORDER BY created",
                                               (incident_id,)).fetchall()]
            for r in rows:
                if "data" in r and r["data"]:
                    r["data"] = json.loads(r["data"])
            out[table] = rows
        aids = [p["id"] for p in out["action_proposals"]]
        marks = ",".join("?" * len(aids))
        for table in ("policy_decisions", "execution_records"):
            rows = [dict(r) for r in c.execute(
                f"SELECT * FROM {table} WHERE action_id IN ({marks}) ORDER BY created", aids).fetchall()] if aids else []
            for r in rows:
                if r.get("data"):
                    r["data"] = json.loads(r["data"])
            out[table] = rows
    return out


def add_container_event(t: float, container: str, action: str, attrs: dict) -> None:
    with conn() as c:
        c.execute("INSERT INTO container_events (t, container, action, attrs) VALUES (?,?,?,?)",
                  (t, container, action, _j(attrs)))


def container_events(container: str, since: float, limit: int = 40) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT t, action, attrs FROM container_events WHERE container=? AND t>=? "
                         "ORDER BY t DESC LIMIT ?", (container, since, limit)).fetchall()
    return [{"t": r["t"], "action": r["action"], **json.loads(r["attrs"])} for r in reversed(rows)]


def audit_log(limit: int = 200, actor: str = "", event: str = "", incident_id: str = "",
              incidents_only: bool = False) -> list[dict]:
    q, args = "SELECT * FROM audit_events WHERE 1=1", []
    if incidents_only:
        q += " AND incident_id IS NOT NULL"
    if actor:
        q += " AND actor LIKE ?"; args.append(f"%{actor}%")
    if event:
        q += " AND event = ?"; args.append(event)
    if incident_id:
        q += " AND incident_id = ?"; args.append(incident_id)
    q += " ORDER BY created DESC LIMIT ?"; args.append(limit)
    with conn() as c:
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
    for r in rows:
        r["data"] = json.loads(r["data"]) if r["data"] else None
    return rows




def has_execution(incident_id: str) -> bool:
    """True once any action for this incident reached the executor (successfully or not)."""
    with conn() as c:
        return c.execute("SELECT 1 FROM execution_records e JOIN action_proposals p ON p.id = e.action_id "
                         "WHERE p.incident_id=? LIMIT 1", (incident_id,)).fetchone() is not None


# ---------------------------------------------------------------- deployments (controlled changes to the fleet)
def start_deployment(playbook: str, nodes: list[str], commit: str, reason: str, preview: dict, by: str) -> str:
    did, now = new_id("DEP"), time.time()
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO deployments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (did, now, now, "running", by, playbook, _j(nodes), commit, reason, _j(preview), None))
        _audit(c, None, by, "deployment_started", {"deployment": did, "playbook": playbook, "nodes": nodes,
                                                  "commit": commit, "reason": reason, "preview": preview})
    return did


def finish_deployment(did: str, ok: bool, result: dict, by: str) -> bool:
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        n = c.execute("UPDATE deployments SET status=?, updated=?, result=? WHERE id=? AND status='running'",
                      ("succeeded" if ok else "failed", time.time(), _j(result), did)).rowcount
        if n:
            _audit(c, None, by, "deployment_finished", {"deployment": did, "ok": ok, "result": result})
    return bool(n)


def deployments(limit: int = 100) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM deployments ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) | {k: json.loads(r[k]) if r[k] else None for k in ("nodes", "preview", "result")} for r in rows]

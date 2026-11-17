"""LLMOps model registry: what each model server is, and when that changed. Track and compare only: the platform
never deploys, upgrades or rolls back a production model.

Every REFRESH_S the registry reads each server the fleet reports (node, port, model, engine, start time) and asks the
server itself for its identity where it answers on the LAN: engine version (vLLM /version), model path and context
length (/v1/models), and for Ollama the model digest and quantisation (/api/tags). Servers that listen on localhost
only keep what the fleet reports.

A version is one identity of one server slot (node + port + model name). A new version is recorded when the
identity changes (different model path, engine version, context length or digest); a restart with the same identity
is counted, not versioned. Each version links to the model's evaluation scores before and after it appeared.
"""
from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from backend.core import config, store
from backend.monitoring import fleet

REFRESH_S = 600
# what makes a version: not the engine name, which the fleet monitor reports inconsistently (vllm / vllm-omni for the
# same TTS server); the engine version covers a real engine change
IDENTITY = ("model", "engine_version", "root", "max_model_len", "digest", "quantization")
SCHEMA = """
CREATE TABLE IF NOT EXISTS model_versions (
  id TEXT PRIMARY KEY, node TEXT, port INTEGER, model TEXT, kind TEXT, engine TEXT, engine_version TEXT, root TEXT,
  max_model_len INTEGER, digest TEXT, quantization TEXT, fingerprint TEXT, first_seen REAL, last_seen REAL,
  started REAL, restarts INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS model_versions_slot ON model_versions(node, port, model, first_seen);
"""
_lock = threading.Lock()
_last = 0.0


def _init() -> None:
    with store.conn() as c:
        c.executescript(SCHEMA)


def _get(url: str) -> dict | None:
    try:
        r = httpx.get(url, timeout=3)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def _base(node: config.Node, port: int) -> str:
    """The LAN address; on the console's own node a localhost-only server (Ollama here) is still reachable."""
    if node.host == socket.gethostname() and not _get(f"http://{node.lan_ip}:{port}/"):
        return f"http://127.0.0.1:{port}"
    return f"http://{node.lan_ip}:{port}"


def _probe(node: config.Node, s: dict) -> dict:
    """The server's own account of itself, where it answers. A field it did not report is None: unknown, not changed."""
    base = _base(node, s["port"])
    out: dict = {}
    if s.get("engine") == "ollama":
        tags = _get(f"{base}/api/tags") or {}
        m = next((m for m in tags.get("models", []) if m.get("name") == s["name"]), None)
        if m:
            out |= {"digest": (m.get("digest") or "")[:16] or None, "quantization": (m.get("details") or {}).get("quantization_level")}
        out["engine_version"] = (_get(f"{base}/api/version") or {}).get("version")
        return out
    out["engine_version"] = (_get(f"{base}/version") or {}).get("version")
    models = (_get(f"{base}/v1/models") or {}).get("data") or []
    m = next((m for m in models if m.get("id") == s["name"]), models[0] if models else None)
    if m:
        out |= {"root": m.get("root"), "max_model_len": m.get("max_model_len")}
    return out


def _fingerprint(ident: dict) -> str:
    return hashlib.sha256(json.dumps([ident.get(k) for k in IDENTITY], default=str).encode()).hexdigest()[:16]


def observe(node: str, s: dict, probed: dict, now: float) -> str:
    """Record one sighting of a server. Returns 'new' (first version of this slot), 'changed', 'restarted' or 'same'.

    Only a field known both before and now, with a different value, is a change. A field the server did not report this
    time (a probe timed out) keeps its known value; a field reported for the first time fills in the current version."""
    ident = {"model": s["name"], "engine": s.get("engine"), **probed}
    with store.conn() as c:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute("SELECT * FROM model_versions WHERE node = ? AND port = ? AND model = ? ORDER BY first_seen DESC LIMIT 1",
                        (node, s["port"], s["name"])).fetchone()
        if cur:
            ident = {k: ident.get(k) if ident.get(k) is not None else cur[k] for k in ("engine", *IDENTITY)}
        fp = _fingerprint(ident)
        differs = cur and any(cur[k] is not None and ident.get(k) is not None and cur[k] != ident.get(k) for k in IDENTITY)
        if cur and not differs:
            restarted = bool(s.get("started") and cur["started"] and abs(s["started"] - cur["started"]) > 5)
            c.execute("UPDATE model_versions SET last_seen = ?, started = COALESCE(?, started), restarts = restarts + ?, "
                      "engine_version = ?, root = ?, max_model_len = ?, digest = ?, quantization = ?, fingerprint = ? WHERE id = ?",
                      (now, s.get("started"), int(restarted), ident.get("engine_version"), ident.get("root"),
                       ident.get("max_model_len"), ident.get("digest"), ident.get("quantization"), fp, cur["id"]))
            if restarted:
                store._audit(c, None, "model_registry", "model_restarted", {"node": node, "port": s["port"], "model": s["name"]})
            return "restarted" if restarted else "same"
        vid = store.new_id("MV")
        c.execute("INSERT INTO model_versions (id, node, port, model, kind, engine, engine_version, root, max_model_len, digest, "
                  "quantization, fingerprint, first_seen, last_seen, started, restarts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                  (vid, node, s["port"], s["name"], s.get("kind"), s.get("engine"), ident.get("engine_version"), ident.get("root"),
                   ident.get("max_model_len"), ident.get("digest"), ident.get("quantization"), fp, now, now, s.get("started")))
        if cur:
            changes = {k: [cur[k], ident.get(k)] for k in IDENTITY if k != "model" and cur[k] is not None
                       and ident.get(k) is not None and cur[k] != ident.get(k)}
            store._audit(c, None, "model_registry", "model_version_changed",
                         {"node": node, "port": s["port"], "model": s["name"], "from": cur["id"], "to": vid, "changes": changes})
            return "changed"
        return "new"


def refresh(now: float | None = None) -> dict:
    """Read every serving model's identity once; returns counts of what was observed."""
    now = now or time.time()
    _init()
    snap = fleet.snapshot()
    jobs = [(config.NODE_BY_NAME[n], s) for n, d in snap.items() if d.get("ok") and n in config.NODE_BY_NAME
            for s in d.get("servers") or [] if s.get("status") in ("ready", "idle")]
    with ThreadPoolExecutor(8) as pool:
        probed = list(pool.map(lambda j: _probe(*j), jobs))
    seen: dict[str, int] = {}
    for (node, s), p in zip(jobs, probed):
        r = observe(node.name, s, p, now)
        seen[r] = seen.get(r, 0) + 1
    return seen


def refresh_if_due() -> None:
    """Called from the detector loop: refresh in the background at most every REFRESH_S."""
    global _last
    if time.time() - _last < REFRESH_S or not _lock.acquire(blocking=False):
        return
    _last = time.time()

    def run():
        try:
            refresh()
        except Exception as e:                       # never let a probe failure reach the detector
            store.audit(None, "model_registry", "error", {"error": repr(e)[:300]})
        finally:
            _lock.release()
    threading.Thread(target=run, daemon=True, name="model-registry").start()


def _scores(c, model: str, after: float, before: float | None) -> dict:
    q = "SELECT score FROM model_evals WHERE model = ? AND t >= ?" + (" AND t < ?" if before else "")
    xs = [r[0] for r in c.execute(q, (model, after, before) if before else (model, after))]
    return {"runs": len(xs), "mean": round(sum(xs) / len(xs), 4) if xs else None}


FIRST_SEEN_NOTE = "first version the registry saw: the server may be older, so all earlier evaluations count"


def models(stale_s: float = 86400) -> list[dict]:
    """Current version of every server seen in the last day, with its history length and eval scores before/after."""
    _init()
    with store.conn() as c:
        rows = c.execute("SELECT * FROM model_versions WHERE last_seen >= ? ORDER BY node, port, model, first_seen",
                         (time.time() - stale_s,)).fetchall()
        current: dict[tuple, dict] = {}
        for r in rows:
            current[(r["node"], r["port"], r["model"])] = dict(r)
        out = []
        for key, v in current.items():
            versions = c.execute("SELECT COUNT(*) FROM model_versions WHERE node = ? AND port = ? AND model = ?", key).fetchone()[0]
            prev = c.execute("SELECT first_seen FROM model_versions WHERE node = ? AND port = ? AND model = ? AND first_seen < ? "
                             "ORDER BY first_seen DESC LIMIT 1", (*key, v["first_seen"])).fetchone()
            # the first version of a slot predates the registry: its evaluations start whenever evaluations began
            out.append(v | {"versions": versions, "first_known": prev is None,
                            "evals_since": _scores(c, v["model"], v["first_seen"] if prev else 0, None),
                            "evals_before": _scores(c, v["model"], prev[0], v["first_seen"]) if prev else None})
    return sorted(out, key=lambda x: (x["node"], x["port"]))


def history(node: str, port: int, model: str) -> list[dict]:
    _init()
    with store.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM model_versions WHERE node = ? AND port = ? AND model = ? "
                                           "ORDER BY first_seen DESC", (node, port, model))]


def changes(days: int = 90) -> list[dict]:
    """Recent identity changes across the fleet (from the audit trail), limited to versions the registry still holds."""
    _init()
    with store.conn() as c:
        known = {r[0] for r in c.execute("SELECT id FROM model_versions")}
    return [e for e in store.audit_log(500, event="model_version_changed")
            if e["created"] >= time.time() - days * 86400 and (e.get("data") or {}).get("to") in known]

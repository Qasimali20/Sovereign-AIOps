"""Tamper evidence of the audit trail: append-only triggers plus a verifiable hash chain."""
import sqlite3
import threading

import pytest

from backend.core import store


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init()


def test_chain_verifies_and_is_append_only():
    for i in range(5):
        store.audit(None, "tester", "e", {"i": i})
    v = store.verify_audit_chain()
    assert v["ok"] and v["events"] == 5
    with sqlite3.connect(store.DB_PATH) as c, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        c.execute("UPDATE audit_events SET actor='mallory' WHERE seq=2")
    with sqlite3.connect(store.DB_PATH) as c, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        c.execute("DELETE FROM audit_events WHERE seq=3")


def test_tampering_that_bypasses_the_trigger_is_detected():
    for i in range(4):
        store.audit(None, "tester", "e", {"i": i})
    with sqlite3.connect(store.DB_PATH) as c:
        c.execute("DROP TRIGGER audit_no_update")
        c.execute("""UPDATE audit_events SET data='{"i": 99}' WHERE seq=3""")
    v = store.verify_audit_chain()
    assert not v["ok"] and v["first_bad_seq"] == 3


def test_deletion_is_detected():
    for i in range(4):
        store.audit(None, "tester", "e", {"i": i})
    with sqlite3.connect(store.DB_PATH) as c:
        c.execute("DROP TRIGGER audit_no_delete")
        c.execute("DELETE FROM audit_events WHERE seq=2")
    assert store.verify_audit_chain()["first_bad_seq"] == 2


def test_concurrent_writers_keep_one_chain():
    ts = [threading.Thread(target=lambda: [store.audit(None, "t", "e", {}) for _ in range(20)]) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    v = store.verify_audit_chain()
    assert v["ok"] and v["events"] == 120


def test_existing_unchained_rows_are_chained_on_migration(tmp_path, monkeypatch):
    p = tmp_path / "old.db"
    with sqlite3.connect(p) as c:
        c.execute("CREATE TABLE audit_events (id TEXT PRIMARY KEY, created REAL, incident_id TEXT, actor TEXT, event TEXT, data TEXT)")
        c.executemany("INSERT INTO audit_events VALUES (?,?,?,?,?,?)", [(f"A{i}", 1000.0 + i, None, "x", "e", "{}") for i in range(3)])
    monkeypatch.setattr(store, "DB_PATH", p)
    store.init()
    store.audit(None, "x", "after", {})
    v = store.verify_audit_chain()
    assert v["ok"] and v["events"] == 4


def test_incremental_check_continues_from_the_verified_head(db):
    for i in range(3):
        store.audit(None, "t", "e", {"i": i})
    full = store.verify_audit_chain()
    store.audit(None, "t", "e", {"i": 3})
    inc = store.verify_audit_chain(after_seq=full["head_seq"], after_hash=full["head"])
    assert inc["ok"] and inc["checked"] == 1 and inc["head_seq"] == full["head_seq"] + 1
    assert store.verify_audit_chain()["head"] == inc["head"]            # same result as a full check
    bad = store.verify_audit_chain(after_seq=full["head_seq"], after_hash="0" * 64)
    assert not bad["ok"]                                                # a wrong starting point is detected


def test_retention_prunes_old_closed_evidence_but_not_open_or_audit(db):
    import time
    old = time.time() - (store.EVIDENCE_RETENTION_DAYS + 1) * 86400
    closed, _ = store.open_incident("node_unreachable", "Node-3", "h", "down", "fp-ret-1", {})
    opened, _ = store.open_incident("node_unreachable", "Node-1", "h", "down", "fp-ret-2", {})
    store.add_evidence(closed, "s", "t", {}); store.add_evidence(opened, "s", "t", {})
    store.set_status(closed, "closed")
    with store.conn() as c:
        c.execute("UPDATE incidents SET updated=? WHERE id=?", (old, closed))
    audits = len(store.audit_log(limit=1000))
    out = store.prune()
    assert out["evidence_records"] == 1 and store.evidence_ids(opened) and not store.evidence_ids(closed)
    assert len(store.audit_log(limit=1000)) == audits + 1               # only appended (the prune record)
    assert store.verify_audit_chain()["ok"]

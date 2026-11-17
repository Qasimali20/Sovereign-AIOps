"""A platform restart must not strand incidents: investigations continue, interrupted changes go to a person."""
import pytest

from backend.incidents import graph
from backend.core import store
from backend.incidents.runtime import Runtime


def test_restart_resumes_investigations_and_hands_interrupted_changes_to_a_person(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init()
    a, _ = store.open_incident("node_rebooted", "Node-4", "boot-1", "Node-4 restarted", "fp-a", {})
    b, _ = store.open_incident("service_unhealthy", "Node-2", "aiops-test-svc", "Test service failing", "fp-b", {})
    store.set_status(a, "investigating"); store.set_status(b, "executing")
    spawned = []
    rt = Runtime(20)
    monkeypatch.setattr(rt, "spawn", lambda fn, iid, *x: spawned.append((fn, iid)) or True)
    got = dict(rt.recover_interrupted())
    assert got == {a: "resumed", b: "needs_human"}
    assert spawned == [(graph.continue_incident, a)]                 # nothing re-runs the interrupted change
    assert store.incident_bundle(b)["incident"]["status"] == "needs_human"


def test_rerun_only_for_needs_human_without_execution(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from backend.core import store
    from backend.web.routes import incidents as r
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "rr.db")
    store.init()
    spawned = []
    monkeypatch.setattr(r.runtime, "spawn", lambda fn, iid, *a: spawned.append(iid) or True)
    user = {"username": "alice", "role": "approver"}
    iid, _ = store.open_incident("model_latency_high", "Node-2", "m@8001", "slow", "fp-r", {})
    store.set_status(iid, "awaiting_approval")
    with pytest.raises(HTTPException) as e:
        r.rerun_investigation(iid, user)
    assert e.value.status_code == 409
    store.set_status(iid, "needs_human")
    assert r.rerun_investigation(iid, user) == {"ok": True} and spawned == [iid]
    assert store.get_incident(iid)["status"] == "open"

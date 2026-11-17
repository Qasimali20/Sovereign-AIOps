"""Regression tests for the bugs found in the 2026-11-16 code review (one per bug)."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend.security import auth
from backend.core import config, store
from backend.incidents import detector, graph
from backend.incidents.runtime import Runtime
from backend.security import lab
from backend.web import create_app, security


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init()


def _awaiting(decided: bool) -> tuple[str, str]:
    iid, _ = store.open_incident("service_unhealthy", "Node-2", "aiops-test-svc", "t", f"fp-{time.time()}", {})
    store.save_proposal(iid, {"incident_id": iid, "action_id": "ACT-r", "operation": "restart_test_service",
                              "target": {"resource_type": "test_service", "resource_id": "aiops-test-svc"}})
    aid = store.create_approval("ACT-r", iid)
    store.set_status(iid, "awaiting_approval")
    if decided:
        store.decide_approval(aid, True, "alice", "")
    return iid, aid


# 1. a decision that arrives before the workflow parks must not be lost
def test_decided_approval_is_resumed_once_parked(db, monkeypatch):
    rt = Runtime(3600)
    iid, aid = _awaiting(decided=True)
    calls = []
    monkeypatch.setattr(graph, "parked_at_approval", lambda i: True)
    monkeypatch.setattr(graph, "resume_incident", lambda i, a: calls.append((i, a)))
    assert rt.resume_decided() == [iid]
    for _ in range(50):
        if calls:
            break
        time.sleep(0.02)
    assert calls == [(iid, aid)]


def test_resume_retries_until_the_workflow_parks(db, monkeypatch):
    rt = Runtime(3600)
    iid, aid = _awaiting(decided=True)
    parked = {"v": False}
    calls = []
    monkeypatch.setattr(graph, "parked_at_approval", lambda i: parked["v"])
    monkeypatch.setattr(graph, "resume_incident", lambda i, a: calls.append(i))
    rt.resume(iid, aid)                       # not parked yet: the old code dropped this silently
    time.sleep(0.2)
    assert calls == []
    parked["v"] = True
    for _ in range(60):
        if calls:
            break
        time.sleep(0.05)
    assert calls == [iid]


# 2. scans never interleave
def test_scans_are_serialised(monkeypatch):
    rt = Runtime(3600)
    active, peak = [0], [0]

    def slow():
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        active[0] -= 1
        return []
    monkeypatch.setattr(rt, "_scan", slow)
    ts = [threading.Thread(target=rt.scan_once) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 1


# 3. lab reset really resets, and baselines never change
def test_lab_reset_clears_blocks_and_keeps_baselines(db, tmp_path, monkeypatch):
    monkeypatch.setattr(lab, "STATE", tmp_path / "lab.json")
    base = (dict(lab.BASE_PACKAGES), {k: list(v) for k, v in lab.BASE_PATCHED.items()})
    lab.block_ip("203.0.113.9", "human:alice", "INC-x")
    lab.state()["packages"]["sudo"] = "tampered"           # callers mutating a returned state must not leak
    lab.reset()
    s = lab.state()
    assert s["blocked_ips"] == {} and lab._empty()["blocked_ips"] == {}
    assert (lab.BASE_PACKAGES, lab.BASE_PATCHED) == base


# 4. the CLI token approves as its bound user, never as a name the client supplies
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "session.key")
    monkeypatch.setattr(security, "client_allowed", lambda host: True)
    monkeypatch.setattr(config, "APPROVER_TOKEN", "t0ken-for-tests")
    store.init()
    auth.set_user("alice", "password123", "approver")
    return TestClient(create_app())


def test_unbound_token_cannot_approve(client, monkeypatch):
    monkeypatch.setattr(config, "APPROVER_TOKEN_USER", "")
    _, aid = _awaiting(decided=False)
    r = client.post(f"/api/approvals/{aid}", json={"approve": True}, headers={"x-approver-token": "t0ken-for-tests", "x-requested-with": "aiops"})
    assert r.status_code == 403


def test_token_approval_is_recorded_under_the_bound_user(client, monkeypatch):
    monkeypatch.setattr(config, "APPROVER_TOKEN_USER", "alice")
    monkeypatch.setattr("backend.incidents.runtime.runtime.resume", lambda *a: None)
    _, aid = _awaiting(decided=False)
    r = client.post(f"/api/approvals/{aid}", json={"approve": True, "approver": "the-ceo"},
                    headers={"x-approver-token": "t0ken-for-tests", "x-requested-with": "aiops"})
    assert r.status_code == 200 and r.json()["approver"] == "alice"
    assert store.get_approval(aid)["approver"] == "alice"


# 5. journal cursors advance even when nothing happens
def test_kernel_cursor_advances_on_empty_reads(monkeypatch):
    node = config.NODES[0]
    monkeypatch.setattr(config, "NODES", [node])
    monkeypatch.setattr(detector, "monitor_metrics", lambda n: (_ for _ in ()).throw(detector.ToolError("x")))
    monkeypatch.setattr(detector.kernel, "read_gpu_events", lambda n, since: {"ok": True, "xids": [], "bad_register_reads": 0, "access": "ssh", "error": None, "checked": time.time()})
    monkeypatch.setattr(detector, "check_endpoint_health", lambda rid: {"data": {"ok": True}})
    state = detector.DetectorState()
    detector.scan(state)
    first = state.kernel_cursor[node.name]
    assert first > time.time() - detector.CURSOR_OVERLAP_S - 5     # not pinned an hour back at startup
    time.sleep(0.01)
    detector.scan(state)
    assert state.kernel_cursor[node.name] >= first


def test_ai_pause_skips_investigation_but_keeps_the_incident(db):
    store.set_ai_pause("aiops-test-svc", 10, "mttr manual arm", "human:alice")
    assert store.ai_paused("aiops-test-svc") and not store.ai_paused("other")
    store.set_ai_pause("aiops-test-svc", None, "done", "human:alice")
    assert store.ai_paused("aiops-test-svc") is None


def test_investigations_are_capped_but_resumes_are_not(db, monkeypatch):
    monkeypatch.setattr(config, "MAX_CONCURRENT_INVESTIGATIONS", 2)
    rt = Runtime(3600)
    active, peak, gate = [0], [0], threading.Event()

    def investigate(iid):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        gate.wait(2)
        active[0] -= 1
    resumed = []
    monkeypatch.setattr(graph, "run_incident", investigate)
    monkeypatch.setattr(graph, "resume_incident", lambda iid, aid: resumed.append(iid))
    for i in range(5):
        rt.spawn(graph.run_incident, f"INC-{i}")
    rt.spawn(graph.resume_incident, "INC-r", "APR-1")            # not an investigation: runs straight away
    time.sleep(0.3)
    assert peak[0] == 2 and resumed == ["INC-r"]
    gate.set()


def test_logout_revokes_the_session_and_password_change_revokes_all(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "session.key")
    auth.set_user("alice", "password123", "approver")
    a, b = auth.make_session({"username": "alice"}), auth.make_session({"username": "alice"})
    assert auth.read_session(a) and auth.read_session(b)
    auth.revoke_session(a)                                   # logout of one browser
    assert auth.read_session(a) is None and auth.read_session(b)
    auth.set_user("alice", "new-password-9", "approver")     # password change: every session ends
    assert auth.read_session(b) is None
    assert auth.read_session(auth.make_session({"username": "alice"}))
    auth.revoke_session("forged.token")                       # forged tokens cannot touch the revoked list
    assert not (tmp_path / "revoked_sessions.json").read_text().count("forged")


def test_unverifiable_kinds_are_never_reported_resolved(monkeypatch):
    from backend.incidents import verify
    monkeypatch.setattr(verify.detector, "scan", lambda st: [])          # a fresh scan sees nothing: must not count
    for kind in ("high_cpu", "gpu_stalled", "model_latency_high", "model_server_missing", "node_unreachable", "svc_unit_failed"):
        outcome, data = verify.verify_incident({"kind": kind, "resource": "r", "fingerprint": "f"}, wait_s=0)
        assert outcome == "not_verifiable", kind
    outcome, _ = verify.verify_incident({"kind": "gpu_temperature", "resource": "h", "fingerprint": "f"}, wait_s=0, interval_s=0)
    assert outcome == "resolved"


def test_a_model_keeps_one_colour_across_charts():
    from backend.monitoring import boards
    mk = lambda names, sec="Chat": {"by": "model", "section": sec, "series": [{"key": f"s{i}", "name": n, "slot": None if n == "Other" else i} for i, n in enumerate(names)]}
    panels = [mk(["qwen3.6", "qwen-chat", "paddleocr"]), mk(["paddleocr", "qwen3.6", "Other"]), mk(["whisper"], "Speech")]
    boards._pin_colours(panels)
    slot = lambda p, n: next(x["slot"] for x in p["series"] if x["name"] == n)
    for n in ("qwen3.6", "paddleocr"):
        assert slot(panels[0], n) == slot(panels[1], n)
    assert slot(panels[1], "Other") is None and slot(panels[2], "whisper") == 0


def test_one_chart_never_repeats_a_colour_even_with_more_models_than_slots():
    from backend.monitoring import boards
    names = ["a", "b", "c", "d", "e", "f", "g"]
    mk = lambda ns: {"by": "model", "section": "S", "series": [{"key": f"s{i}", "name": n, "slot": i} for i, n in enumerate(ns)]}
    panels = [mk(names[:4]), mk(["e", "a", "f", "b"]), mk(["c", "g"])]
    boards._pin_colours(panels)
    for p in panels:
        slots = [x["slot"] for x in p["series"]]
        assert len(set(slots)) == len(slots)
    assert panels[0]["series"][0]["slot"] == panels[1]["series"][1]["slot"]      # "a" keeps its colour


def test_peak_panels_keep_a_burst_shorter_than_the_step(monkeypatch):
    """A 30 s burst between two 5-minute steps must still show: peak panels ask for the max within each step."""
    from backend.monitoring import boards, promql
    seen = []
    monkeypatch.setattr(promql, "query_range", lambda expr, *a, **k: seen.append(expr) or [])
    cpu = next(p for p in boards.BOARDS["fleet"]["panels"] if p.id == "cpu")
    boards._panel(cpu, 0, 3600, 300)
    assert seen == [f"max_over_time(({cpu.expr})[300s:])"]
    power = next(p for p in boards.BOARDS["fleet"]["panels"] if p.id == "gpu_power")   # stacked totals stay as sampled
    boards._panel(power, 0, 3600, 300)
    assert seen[-1] == power.expr

"""AgentOps: runs read back from recorded agent steps, step timing, token totals, and diagnosis feedback."""
import pytest
from fastapi.testclient import TestClient

from backend.agentops import runs as agentops
from backend.security import auth
from backend.core import store
from backend.web import create_app, security
from tests.test_api import H, login


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init()


def _run(times: list[tuple[float, str, dict]]) -> str:
    """An incident with agent steps recorded at the given times (seconds), as the workflow records them."""
    iid, _ = store.open_incident("model_queue_stuck", "Node-4", "tts@8091", "queue stuck", f"fp-{len(times)}-{times[0][0]}", {})
    with store.conn() as c:
        for t, node, data in times:
            c.execute("INSERT INTO agent_steps VALUES (?,?,?,?,?)", (store.new_id("STEP"), iid, 1000 + t, node, store._j(data)))
    return iid


def _call(latency, prompt, completion, attempts=1, skipped=()):
    return {"diagnosis": {"summary": "the scheduler stalled"},
            "meta": {"endpoint": "gateway", "model": "chat", "request_id": "req-1", "latency_s": latency, "attempts": attempts,
                     "usage": [{"prompt_tokens": prompt, "completion_tokens": completion}] * attempts, "skipped": list(skipped)}}


def test_run_summary_counts_calls_tokens_retries_and_fallbacks(db):
    iid = _run([(0, "collect_evidence", {"tools": [["get_model_logs", "Node-4"]], "count": 1}),
                (1, "retrieve_runbooks", {"chunks": [{}, {}], "method": "dense+rerank"}),
                (31, "diagnose", _call(30, 1000, 100, attempts=2, skipped=["Node-2: timeout"])),
                (32, "gather_more", {"requests": [{"tool": "get_node_metrics"}], "new_records": 1}),
                (52, "diagnose", _call(20, 1200, 150)),
                (53, "report", {"final_status": "diagnosed", "observation_evidence_support": 1.0})])
    [r] = agentops.runs()
    assert r["incident_id"] == iid and r["steps"] == 6 and r["model_calls"] == 2 and r["duration_s"] == 53
    assert r["prompt_tokens"] == 2 * 1000 + 1200 and r["completion_tokens"] == 2 * 100 + 150    # every attempt's usage
    assert r["attempts"] == 3 and r["fallbacks"] == 1 and r["model_seconds"] == 50


def test_step_time_is_the_gap_since_the_previous_step_finished(db):
    iid = _run([(0, "collect_evidence", {}), (1, "retrieve_runbooks", {}), (39, "diagnose", _call(37.9, 10, 1)),
                (40, "report", {})])
    steps = agentops.run(iid)["steps"]
    assert [s["duration_s"] for s in steps] == [None, 1, 38, 1]                 # diagnose owns the 38 s model call
    assert steps[2]["call"]["latency_s"] == 37.9 and steps[0]["call"] is None


def test_feedback_is_stored_replaced_audited_and_scored(db):
    a = _run([(0, "diagnose", _call(1, 1, 1))])
    b = _run([(5, "diagnose", _call(1, 1, 1))])
    agentops.set_feedback(a, "wrong", "it was the network", "human:alice")
    agentops.set_feedback(a, "correct", "", "human:alice")                     # a second verdict replaces the first
    agentops.set_feedback(b, "partly", "", "human:bob")
    o = agentops.overview()
    assert o["rated"] == 2 and o["verdicts"] == {"correct": 1, "partly": 1, "wrong": 0} and o["accuracy"] == 0.75
    trail = [e for e in store.audit_log(50, event="diagnosis_feedback") if e["incident_id"] == a]
    assert len(trail) == 2 and store.verify_audit_chain()["ok"]


def test_feedback_needs_a_diagnosis(db):
    iid = _run([(0, "collect_evidence", {})])
    with pytest.raises(ValueError):
        agentops.set_feedback(iid, "correct", "", "human:alice")


def test_api_lists_runs_and_only_approvers_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "session.key")
    monkeypatch.setattr(security, "client_allowed", lambda host: True)
    store.init()
    auth.set_user("vera", "password123", "viewer")
    auth.set_user("alice", "password123", "approver")
    iid = _run([(0, "diagnose", _call(1, 1, 1))])
    c = TestClient(create_app())
    login(c, "vera")
    assert [r["incident_id"] for r in c.get("/api/agent/runs").json()] == [iid]
    assert c.get(f"/api/agent/runs/{iid}").json()["summary"]["model_calls"] == 1
    assert c.get("/api/agent/runs/INC-none").status_code == 404
    assert c.post(f"/api/agent/runs/{iid}/feedback", json={"verdict": "correct"}, headers=H).status_code == 403
    login(c, "alice")
    assert c.post(f"/api/agent/runs/{iid}/feedback", json={"verdict": "maybe"}, headers=H).status_code == 422
    assert c.post(f"/api/agent/runs/{iid}/feedback", json={"verdict": "correct"}, headers=H).status_code == 200
    assert c.get("/api/agent/overview").json()["accuracy"] == 1.0

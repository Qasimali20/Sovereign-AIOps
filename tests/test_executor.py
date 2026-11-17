import pytest

from backend.core import config, store
from backend.incidents import executor


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init()


def _setup(approver="alice", approve=True, evidence=True):
    iid, _ = store.open_incident("service_unhealthy", "Node-2", "aiops-test-svc", "t", "fp", {})
    ev = store.add_evidence(iid, "x", "check_endpoint_health", {}) if evidence else "EV-fake"
    p = {"incident_id": iid, "action_id": "ACT-1", "operation": "restart_test_service",
         "target": {"resource_type": "test_service", "resource_id": "aiops-test-svc"},
         "evidence_refs": [ev]}
    store.save_proposal(iid, p)
    aid = store.create_approval("ACT-1", iid)
    return p, aid


def test_refuses_pending_approval(monkeypatch):
    p, aid = _setup()
    with pytest.raises(executor.ExecutionRefused):
        executor.execute(p, aid)


def test_refuses_rejected(monkeypatch):
    p, aid = _setup()
    store.decide_approval(aid, False, "alice", "no")
    with pytest.raises(executor.ExecutionRefused):
        executor.execute(p, aid)


def test_refuses_approval_for_other_action():
    p, aid = _setup()
    store.decide_approval(aid, True, "alice", "")
    with pytest.raises(executor.ExecutionRefused):
        executor.execute(p | {"action_id": "ACT-other"}, aid)


def test_refuses_tampered_target():
    p, aid = _setup()
    store.decide_approval(aid, True, "alice", "")
    bad = p | {"target": {"resource_type": "test_service", "resource_id": "vllm-qwen36"}}
    with pytest.raises(executor.ExecutionRefused):
        executor.execute(bad, aid)


def test_refuses_evidence_not_recorded():
    p, aid = _setup(evidence=False)
    store.decide_approval(aid, True, "alice", "")
    with pytest.raises(executor.ExecutionRefused):
        executor.execute(p, aid)


def test_refuses_agent_as_approver():
    p, aid = _setup()
    store.decide_approval(aid, True, "agent", "")
    with pytest.raises(executor.ExecutionRefused):
        executor.execute(p, aid)


def test_concurrent_decisions_only_one_wins():
    import threading
    p, aid = _setup()
    results = []
    ts = [threading.Thread(target=lambda a=a: results.append(store.decide_approval(aid, a, f"user{a}", ""))) for a in (True, False) * 4]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(r is not None for r in results) == 1


def test_open_incident_dedup():
    a, created_a = store.open_incident("k", "n", "r", "t", "same-fp", {})
    b, created_b = store.open_incident("k", "n", "r", "t", "same-fp", {})
    assert created_a and not created_b and a == b


def test_bundle_decodes_execution_and_policy_records():
    p, aid = _setup()
    store.save_policy("ACT-1", p["incident_id"], "requires_approval", ["r"])
    store.save_execution("ACT-1", p["incident_id"], True, {"ok": True, "returncode": 0})
    b = store.incident_bundle(p["incident_id"])
    assert b["execution_records"][0]["data"]["returncode"] == 0
    assert b["policy_decisions"][0]["decision"] == "requires_approval"


@pytest.mark.parametrize("op,params,call", [
    ("restart_test_service", {}, ("restart",)), ("clear_test_cache", {}, ("clear_cache",)),
    ("rollback_test_deployment", {}, ("rollback",)), ("scale_test_service", {"replicas": "2"}, ("scale", 2)),
])
def test_approved_test_ops_dispatch_to_fixed_functions(monkeypatch, op, params, call):
    from backend.incidents import testsvc
    seen = []
    for fn in ("restart", "clear_cache", "rollback"):
        monkeypatch.setattr(testsvc, fn, lambda by, fn=fn: seen.append((fn,)) or {"ok": True})
    monkeypatch.setattr(testsvc, "scale", lambda n, by: seen.append(("scale", n)) or {"ok": True})
    p, aid = _setup()
    p = p | {"operation": op, "parameters": params}
    store.decide_approval(aid, True, "alice", "ok")
    assert executor.execute(p, aid)["ok"] and seen == [call]

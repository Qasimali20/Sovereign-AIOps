"""Phase 7 authorization-bypass attempts against the real API. Every attempt must be refused."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from backend.security import auth
from backend.core import store
from backend.web import create_app, security
from tests.test_api import H, login


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "session.key")
    monkeypatch.setattr(security, "client_allowed", lambda host: True)
    store.init()
    for name, role in (("vera", "viewer"), ("alice", "approver"), ("root", "admin")):
        auth.set_user(name, "password123", role)
    return TestClient(create_app())


def _incident_with_pending_approval():
    iid, _ = store.open_incident("service_unhealthy", "Node-2", "aiops-test-svc", "t", "fp-x", {})
    store.save_proposal(iid, {"incident_id": iid, "action_id": "ACT-z", "operation": "restart_test_service",
                              "target": {"resource_type": "test_service", "resource_id": "aiops-test-svc"}})
    return iid, store.create_approval("ACT-z", iid)


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/approvals/{aid}", {"approve": True, "comment": "x"}),
    ("post", "/api/incidents/{iid}/close", {"note": "closing"}),
    ("post", "/api/users", {"username": "eve", "password": "password123", "role": "admin"}),
    ("post", "/api/security/lab/password_spray", None),
    ("post", "/api/gateway/keys", {"name": "k"}),
    ("post", "/api/agent/runs/{iid}/feedback", {"verdict": "wrong"}),
    ("post", "/api/llm/evals/run", None),
])
def test_viewer_cannot_act(client, method, path, body):
    iid, aid = _incident_with_pending_approval()
    login(client, "vera")
    r = getattr(client, method)(path.format(iid=iid, aid=aid), json=body, headers=H)
    assert r.status_code == 403, (path, r.status_code, r.text)
    assert store.get_approval(aid)["status"] == "pending"


def test_approver_cannot_administer(client):
    login(client, "alice")
    assert client.post("/api/users", json={"username": "eve", "password": "password123", "role": "admin"}, headers=H).status_code == 403
    assert client.post("/api/users/root/delete", headers=H).status_code == 403


def test_forged_and_tampered_sessions_rejected(client):
    login(client, "vera")
    tok = client.cookies.get(security.COOKIE)
    client.cookies.clear()
    # promote ourselves to admin inside the cookie payload, keep the old signature
    payload, _, sig = tok.rpartition(".")
    try:
        body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        body["role"] = "admin"
        forged = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=") + "." + sig
    except Exception:
        forged = tok[:-4] + "AAAA"
    for bad in (forged, tok[:-4] + ("A" if tok[-4] != "A" else "B") * 4, "garbage", ""):
        client.cookies.set(security.COOKIE, bad)
        assert client.get("/api/me").status_code == 401
        assert client.post("/api/users", json={"username": "eve", "password": "password123", "role": "admin"}, headers=H).status_code in (401, 403)


def test_wrong_approver_token_rejected(client):
    r = client.get("/api/incidents", headers={"x-approver-token": "not-the-token"})
    assert r.status_code == 401


def test_csrf_header_required_for_every_write(client):
    login(client, "root")
    for path in ("/api/scan", "/api/users", "/api/gateway/keys", "/api/security/lab-reset"):
        assert client.post(path, json={}).status_code == 400, path


def test_metrics_endpoint_refuses_non_fleet_clients(client):
    assert client.get("/metrics").status_code == 403          # TestClient's host is not a fleet node
    assert client.get("/metrics/targets").status_code == 403


def test_api_rate_limit(client, monkeypatch):
    from backend.core.ratelimit import TokenBuckets
    monkeypatch.setattr(security, "_api_buckets", TokenBuckets(rate=0.001, burst=3))
    codes = [client.get("/api/incidents").status_code for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]

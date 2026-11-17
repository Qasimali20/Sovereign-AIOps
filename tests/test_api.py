"""HTTP security layer: allowlist, CSRF guard, authentication, roles, approvals."""
import pytest
from fastapi.testclient import TestClient

from backend.security import auth
from backend.core import store
from backend.web import create_app, security
from backend.core import config

# fleet addresses come from .env (placeholders without it)
N1 = config.NODE_BY_NAME["Node-1"].lan_ip
T2 = config.NODE_BY_NAME["Node-2"].ts_ip

H = {"X-Requested-With": "aiops"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "session.key")
    monkeypatch.setattr(security, "client_allowed", lambda host: True)
    store.init()
    for name, role in (("vera", "viewer"), ("alice", "approver"), ("root", "admin")):
        auth.set_user(name, "password123", role)
    return TestClient(create_app())          # no context manager: background runtime not started


def login(c, name):
    r = c.post("/api/login", json={"username": name, "password": "password123"}, headers=H)
    assert r.status_code == 200, r.text


def test_requires_login(client):
    assert client.get("/api/incidents").status_code == 401


def test_wrong_password_and_throttle(client):
    for _ in range(5):
        assert client.post("/api/login", json={"username": "alice", "password": "nope"}, headers=H).status_code == 401
    assert client.post("/api/login", json={"username": "alice", "password": "password123"}, headers=H).status_code == 429
    security.throttle.reset("testclient")


def test_csrf_header_required(client):
    login(client, "alice")
    assert client.post("/api/scan").status_code == 400


def test_network_allowlist(monkeypatch):
    monkeypatch.undo()
    assert security.client_allowed(f"{N1}") and security.client_allowed(f"{T2}")
    assert security.client_allowed("127.0.0.1")
    assert not security.client_allowed("172.17.0.2") and not security.client_allowed("8.8.8.8")
    assert not security.client_allowed("testclient")


def test_viewer_cannot_approve_or_manage_users(client):
    login(client, "vera")
    assert client.post("/api/approvals/APR-x", json={"approve": True}, headers=H).status_code == 403
    assert client.get("/api/users").status_code == 403


def test_approver_cannot_manage_users(client):
    login(client, "alice")
    assert client.get("/api/users").status_code == 403


def test_approval_recorded_under_session_user(client, monkeypatch):
    from backend.incidents.runtime import runtime
    monkeypatch.setattr(runtime, "resume", lambda *a: None)
    iid, _ = store.open_incident("service_unhealthy", "Node-2", "aiops-test-svc", "t", "fp", {})
    store.save_proposal(iid, {"action_id": "ACT-1", "incident_id": iid})
    aid = store.create_approval("ACT-1", iid)
    login(client, "alice")
    r = client.post(f"/api/approvals/{aid}", json={"approve": True, "approver": "someone-else"}, headers=H)
    assert r.status_code == 200 and r.json()["approver"] == "alice"
    assert client.post(f"/api/approvals/{aid}", json={"approve": False}, headers=H).status_code == 409


def test_admin_user_management(client):
    login(client, "root")
    assert client.post("/api/users", json={"username": "bob", "password": "password123", "role": "viewer"}, headers=H).status_code == 200
    assert client.post("/api/users", json={"username": "agent", "password": "password123", "role": "viewer"}, headers=H).status_code == 400
    assert client.post("/api/users/root/delete", headers=H).status_code == 400
    assert client.post("/api/users", json={"username": "root", "password": "password123", "role": "viewer"}, headers=H).status_code == 400
    assert client.post("/api/users/bob/delete", headers=H).status_code == 200


def test_deleted_user_session_invalid(client):
    login(client, "vera")
    assert client.get("/api/me").status_code == 200
    auth.delete_user("vera")
    assert client.get("/api/me").status_code == 401


def test_gateway_keys_admin_only_and_platform_key_protected(client, tmp_path, monkeypatch):
    from backend.llmops.gateway import keys
    monkeypatch.setattr(keys, "PATH", tmp_path / "gk.json")
    platform_key, meta = keys.create("aiops-platform", "system")
    login(client, "alice")
    assert client.get("/api/gateway/keys").status_code == 403
    login(client, "root")
    r = client.post("/api/gateway/keys", json={"name": "chambers"}, headers=H)
    assert r.status_code == 200 and r.json()["key"].startswith("sk-aiops-")
    listed = client.get("/api/gateway/keys").json()
    assert {k["name"] for k in listed} == {"aiops-platform", "chambers"} and all("digest" not in k for k in listed)
    assert client.post(f"/api/gateway/keys/{meta['id']}/revoke", headers=H).status_code == 400
    assert client.post("/api/gateway/keys", json={"name": "aiops-platform"}, headers=H).status_code == 400
    assert keys.verify(r.json()["key"]) is not None
    assert client.post(f"/api/gateway/keys/{r.json()['id']}/revoke", headers=H).status_code == 200
    assert keys.verify(r.json()["key"]) is None

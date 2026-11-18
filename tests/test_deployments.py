"""Controlled deployments: record, maintenance window, finish with drift, roles, and the CLI's recap parsing."""
import importlib.util
from pathlib import Path

from backend.core import store
from backend.incidents import runtime
from backend.monitoring import drift
from tests.test_api import H, client, login  # noqa: F401  (client: the API fixture)

spec = importlib.util.spec_from_file_location("deploy", Path(__file__).resolve().parents[1] / "ops" / "scripts" / "deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)

BODY = {"playbook": "log-agent", "nodes": ["Node-1", "Node-3"], "commit": "abc1234", "reason": "roll out collector config",
        "minutes": 20, "preview": {"Node-1": {"changed": 3}}}


def test_recap_parsing():
    out = ("PLAY RECAP ****\nNode-1   : ok=5    changed=3    unreachable=0    failed=0    skipped=3\n"
           "localhost : ok=2 changed=0 unreachable=0 failed=0\nNode-4 : ok=1 changed=0 unreachable=1 failed=0\n")
    assert deploy.recap(out) == {"Node-1": {"ok": 5, "changed": 3, "unreachable": 0, "failed": 0},
                                 "Node-4": {"ok": 1, "changed": 0, "unreachable": 1, "failed": 0}}


def test_deployment_opens_a_window_and_finishes_with_drift(client, tmp_path, monkeypatch):
    monkeypatch.setattr(drift, "BASELINE_PATH", tmp_path / "drift.json")
    monkeypatch.setattr(runtime.runtime, "check_drift", lambda: None)
    monkeypatch.setitem(runtime.runtime.drift_changes, "Node-1", [{"key": "container:aiops-log-agent"}])
    monkeypatch.setitem(runtime.runtime.drift_changes, "Node-3", [])
    login(client, "vera")
    assert client.post("/api/deployments", json=BODY, headers=H).status_code == 403          # viewers cannot deploy
    login(client, "alice")
    assert client.post("/api/deployments", json=BODY | {"playbook": "rm-rf"}, headers=H).status_code == 422
    did = client.post("/api/deployments", json=BODY, headers=H).json()["id"]
    assert {w["node"] for w in store.maintenance_windows() if did in w["reason"]} == {"Node-1", "Node-3"}
    assert store.classify_origin("svc_container_stopped", "Node-1", "aiops-log-agent") == "planned"
    r = client.post(f"/api/deployments/{did}/finish", json={"ok": True, "recap": {"Node-1": {"changed": 3}}}, headers=H).json()
    assert r["status"] == "succeeded" and r["drift"]["Node-1"] == [{"key": "container:aiops-log-agent"}]
    assert client.post(f"/api/deployments/{did}/finish", json={"ok": True}, headers=H).status_code == 409
    [d] = client.get("/api/deployments").json()
    assert d["status"] == "succeeded" and d["by"] == "human:alice" and d["result"]["drift"] == {"Node-1": ["container:aiops-log-agent"], "Node-3": []}
    events = [e["event"] for e in store.audit_log(limit=20)]
    assert "deployment_started" in events and "deployment_finished" in events and "maintenance_window" in events

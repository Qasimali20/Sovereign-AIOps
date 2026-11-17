"""Service monitoring: parsing, transition-based alerts, known-benign units."""
import json

from backend.monitoring import services

DOCKER = "\n".join(json.dumps(c) for c in [
    {"Names": "mip-postgres-1", "State": "running", "Status": "Up 5 hours (healthy)", "Image": "pgvector/pgvector:pg17"},
    {"Names": "mip-api-1", "State": "running", "Status": "Up 5 hours (unhealthy)", "Image": "mip-backend:latest"},
    {"Names": "qwen3-30b", "State": "running", "Status": "Up 2 hours", "Image": "vllm/vllm-openai"},
    {"Names": "old-thing", "State": "exited", "Status": "Exited (0) 3 months ago", "Image": "x"},
    {"Names": "aiops-test-svc", "State": "exited", "Status": "Exited (137)", "Image": "alpine"},
])
UNITS = "iwpmd.service loaded failed failed iWARP Port Mapper\nworker.service loaded activating auto-restart Worker\n"
RUNNING = "chambers.service loaded active running Chambers\nssh.service loaded active running OpenSSH\n"
OUT = f"{DOCKER}\n{services._SPLIT}\n{UNITS}{services._SPLIT}\n{RUNNING}"


def test_parse():
    p = services.parse(OUT)
    assert p["docker_ok"] and [c["health"] for c in p["containers"] if c["name"].startswith("mip")] == ["unhealthy", "healthy"]
    assert {u["unit"]: bool(u["known"]) for u in p["problem_units"]} == {"iwpmd.service": True, "worker.service": False}
    assert p["app_units"] == ["chambers.service"]                        # OS units filtered out


def test_docker_denied_is_reported_not_hidden():
    p = services.parse(f"DOCKER_DENIED\n{services._SPLIT}\n{services._SPLIT}\n")
    assert p["docker_ok"] is False and p["containers"] == []


def test_alerts_unhealthy_crashloop_known_units_and_own_containers():
    snap = {"ok": True, **services.parse(OUT)}
    kinds = {(a.kind, a.resource) for a in services.alerts("Node-3", snap, services.Baseline())}
    assert ("svc_container_unhealthy", "mip-api-1") in kinds
    assert ("svc_unit_restarting", "worker.service") in kinds
    assert not any(r == "iwpmd.service" for _, r in kinds)               # known benign
    assert not any(r in ("old-thing", "aiops-test-svc") for _, r in kinds)  # never seen running / platform-owned


def test_stopped_alerts_once_per_transition():
    base = services.Baseline()
    up = {"ok": True, "containers": [{"name": "qwen3-30b", "state": "running", "status": "Up", "health": None, "image": "v"}], "problem_units": []}
    down = {"ok": True, "containers": [{"name": "qwen3-30b", "state": "exited", "status": "Exited (1)", "health": None, "image": "v"}], "problem_units": []}
    assert services.alerts("Node-3", up, base) == []
    assert [a.kind for a in services.alerts("Node-3", down, base)] == ["svc_container_stopped"]
    assert services.alerts("Node-3", down, base) == []                   # no repeat while it stays down
    services.alerts("Node-3", up, base)
    assert [a.kind for a in services.alerts("Node-3", down, base)] == ["svc_container_stopped"]   # new transition


def test_planned_stop_does_not_alert(tmp_path, monkeypatch):
    from backend.monitoring import services
    from backend.core import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db"); store.init()
    monkeypatch.setattr(services, "DECOMMISSIONED_PATH", tmp_path / "decom.json")
    base = services.Baseline()
    c = lambda name, state: {"name": name, "state": state, "status": state, "health": "", "image": "x"}
    snap = lambda *cs: {"ok": True, "containers": list(cs), "problem_units": []}
    services.alerts("Node-1", snap(c("db", "running"), c("app", "running")), base)
    services.decommission("Node-1", "db", "human:alice", "unused")
    out = services.alerts("Node-1", snap(c("db", "exited"), c("app", "exited")), base)
    assert [a.resource for a in out] == ["app"]            # the planned stop is silent, the unplanned one alerts
    assert any(e["event"] == "container_decommissioned" for e in store.audit_log(limit=10))

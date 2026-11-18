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


SHOW = """Result=exit-code
NRestarts=4
ExecMainStatus=1
Id=vllm-qwen36.service
ActiveState=activating
SubState=start

Result=success
NRestarts=1
ExecMainStatus=0
Id=qwen3-asr.service
ActiveState=active
SubState=running
"""


def test_parse_restart_counters():
    p = services.parse(f"{OUT}{services._SPLIT}\n{SHOW}")
    assert p["unit_restarts"]["vllm-qwen36.service"] == {"restarts": 4, "result": "exit-code", "exit_status": "1",
                                                         "state": "activating/start"}
    assert services.parse(OUT)["unit_restarts"] == {}                    # older collector output still parses


def _unit_snap(restarts: dict[str, int], problem=()):
    return {"ok": True, "containers": [], "problem_units": [{"unit": u, "sub": "auto-restart", "known": None} for u in problem],
            "unit_restarts": {u: {"restarts": n, "result": "exit-code", "exit_status": "1", "state": "activating/start"}
                              for u, n in restarts.items()}}


def test_crash_loop_caught_by_restart_counter_between_attempts():
    base = services.Baseline()
    # first sight: counts may be days old, no alert
    assert services.alerts("Node-4", _unit_snap({"vllm-qwen36.service": 3, "qwen3-asr.service": 1}), base, now=0) == []
    # the scan lands while the unit is up between attempts: not in the problem list, but the counter moved
    (a,) = services.alerts("Node-4", _unit_snap({"vllm-qwen36.service": 5, "qwen3-asr.service": 1}), base, now=20)
    assert (a.kind, a.resource, a.detail["restarts"], a.detail["last_exit_status"]) == \
        ("svc_unit_restarting", "vllm-qwen36.service", 5, "1")
    # problem list and counter on the same scan: one alert, not two
    out = services.alerts("Node-4", _unit_snap({"vllm-qwen36.service": 6}, problem=["vllm-qwen36.service"]), base, now=40)
    assert [a.resource for a in out] == ["vllm-qwen36.service"] and out[0].detail["restarts"] == 6
    # a unit that appears with a count it did not have before has restarted
    (b,) = services.alerts("Node-4", _unit_snap({"vllm-qwen36.service": 6, "new.service": 1}), base, now=60)
    assert b.resource == "new.service"


def test_unit_counts_as_recovered_only_after_staying_up():
    base = services.Baseline()
    services.alerts("Node-4", _unit_snap({"u.service": 1}), base, now=0)
    services.alerts("Node-4", _unit_snap({"u.service": 2}), base, now=100)        # restarted at t=100
    assert not services.unit_settled(base, "Node-4", "u.service", 100 + services.UNIT_STABLE_S - 1)
    assert services.unit_settled(base, "Node-4", "u.service", 100 + services.UNIT_STABLE_S)
    # after a platform restart nothing is known: wait a full window from the first scan
    fresh = services.Baseline()
    services.alerts("Node-4", _unit_snap({}), fresh, now=1000)
    assert not services.unit_settled(fresh, "Node-4", "u.service", 1000 + 10)

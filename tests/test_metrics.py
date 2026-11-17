"""Exposition format and target discovery for the Prometheus exporter."""
import re

from backend.monitoring import metrics
from backend.core import config

# fleet addresses come from .env (placeholders without it)
N2 = config.NODE_BY_NAME["Node-2"].lan_ip
N3 = config.NODE_BY_NAME["Node-3"].lan_ip

FLEET = {
    "Node-3": {"uptime_s": 100, "cpu": {"percent": 12.5}, "memory": {"used": 10, "total": 100}, "disk": {}, "net": {"rx_bps": 5},
              "gpu": {"util": 40.0, "temp": 51.0, "power": 12.7},
              "vllm": [{"name": 'qwen"chat', "kind": "LLM", "port": 8103, "status": "ready", "running": 1, "waiting": 0}]},
    "Node-4": None,
}
SVC = {"Node-3": {"ok": True, "docker_ok": True, "containers": [{"name": "a", "state": "running", "health": "unhealthy"}],
                 "problem_units": [{"unit": "x", "known": None}, {"unit": "iwpmd.service", "known": "benign"}], "app_units": ["y"]},
       "Node-4": {"ok": False}}
PIPE = {"incidents": {("sre", "Services", "resolved"): 2}, "approvals_pending": 1, "policy": {"requires_approval": 3},
        "executions": {"ok": 2}, "verifications": {"resolved": 2}}
LINE = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{([a-zA-Z_]+="([^"\\]|\\.)*",?)*\})? -?[0-9.e+-]+$')


def test_render_is_valid_exposition():
    out = metrics.render(FLEET, SVC, PIPE, {"last": 1.0, "scans": 3, "error": None, "workflows_running": 0})
    for l in out.splitlines():
        assert l.startswith("# ") or LINE.match(l), l
    assert 'aiops_node_up{node="Node-4"} 0' in out and 'aiops_node_up{node="Node-3"} 1' in out
    assert 'model="qwen\\"chat"' in out                               # label escaping
    assert 'aiops_containers_unhealthy{node="Node-3"} 1' in out
    assert 'aiops_systemd_units_failed{node="Node-3"} 1' in out          # known-benign excluded
    assert "aiops_node_disk_used_bytes{" not in out                    # unknown is absent, not zero
    assert out.count("# TYPE aiops_node_up ") == 1                     # one HELP/TYPE per family


def test_vllm_targets_use_node_lan_ip():
    (t,) = metrics.vllm_targets(FLEET, reachable=lambda a: True)
    assert t["targets"] == [f"{N3}:8103"] and t["labels"]["node"] == "Node-3"
    # a port the node firewalls (or binds to localhost) is scraped through the platform's SSH relay instead
    (r,) = metrics.vllm_targets(FLEET, reachable=lambda a: False)
    assert r["targets"] == [f"{N2}:5170"] and r["labels"]["__metrics_path__"] == "/metrics/relay/Node-3/8103"
    assert r["labels"]["instance"] == f"{N3}:8103" and r["labels"]["via"] == "relay"


def test_relay_only_for_discovered_model_servers(monkeypatch):
    called = []
    monkeypatch.setattr(metrics, "_relay_cache", {})
    import backend.monitoring.kernel as k
    monkeypatch.setattr(k, "run", lambda acc, cmd: called.append(cmd) or type("R", (), {"returncode": 0, "stdout": "vllm:x 1\n"})())
    assert metrics.relay("Node-3", 22, FLEET) is None and metrics.relay("Node-9", 8103, FLEET) is None and not called
    assert metrics.relay("Node-3", 8103, FLEET) == "vllm:x 1\n" and called == ["curl -s -m 5 http://127.0.0.1:8103/metrics"]

"""Bounded log search: query construction, limits, redaction, and honest failure when Loki is down."""
import time

import httpx
import pytest

from backend.monitoring import logs
from backend.incidents import tools


def test_query_is_built_from_validated_parts_only():
    assert logs.build_query("Node-2") == '{node="Node-2"}'
    q = logs.build_query("Node-4", service="vllm-tts", contains='say "hi"', problems_only=True)
    assert q.startswith('{node="Node-4", service="vllm-tts"} |= "say \\"hi\\""')
    assert 'severity=~' in q


@pytest.mark.parametrize("node,service", [
    ("Node-9", None), ('Node-1"} or {node=~".+', None),          # unknown node / selector injection
    ("Node-1", 'x"} |= "'), ("Node-1", "a b"), ("Node-1", ""),   # service must be a plain name
])
def test_invalid_selectors_are_refused(node, service):
    with pytest.raises(ValueError):
        logs.build_query(node, service=service)


def test_search_text_is_bounded():
    with pytest.raises(ValueError):
        logs.build_query("Node-1", contains="x" * 121)


def test_window_is_clamped_and_centred_on_the_incident():
    s, e = logs.window_for(1000.0, now=10_000.0)
    assert (s, e) == (1000.0 - logs.DEFAULT_BEFORE_S, 1000.0 + logs.DEFAULT_AFTER_S)
    s, e = logs._clamp(0, 100_000)
    assert e - s == logs.MAX_WINDOW_S


def test_redaction_masks_secrets_a_second_time():
    out = logs.redact('login password=hunter2 token: "abc123def" Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abc')
    assert "hunter2" not in out and "abc123def" not in out and "eyJhbGci" not in out
    assert out.count("<redacted>") == 3


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text = status, body or {}, str(body)

    def json(self):
        return self._body


def stream(service, *lines):
    return {"stream": {"service": service, "severity": "error", "component": "container"},
            "values": [[str(int((time.time() - i) * 1e9)), l] for i, l in enumerate(lines)]}


def test_search_caps_lines_and_masks(monkeypatch):
    monkeypatch.setattr(logs, "LOKI_PASSWORD", "x")
    seen = {}

    def fake_get(url, params, auth, timeout):
        seen.update(params, timeout=timeout)
        return FakeResp(body={"status": "success", "data": {"result": [
            stream("aiops-test-svc", *[f"error {i} password=s3cret" for i in range(500)])]}})
    monkeypatch.setattr(httpx, "get", fake_get)
    r = logs.search("Node-2", 0, 10 ** 9, limit=10_000)
    assert seen["limit"] == logs.MAX_LINES and r["returned"] == logs.MAX_LINES and r["truncated"]
    assert seen["timeout"] == logs.TIMEOUT_S
    assert int(seen["end"]) - int(seen["start"]) <= logs.MAX_WINDOW_S * 1e9
    assert all("s3cret" not in e["line"] for e in r["entries"])


@pytest.mark.parametrize("failure", ["down", "500", "unconfigured"])
def test_loki_failure_is_an_error_not_an_empty_result(monkeypatch, failure):
    monkeypatch.setattr(logs, "LOKI_PASSWORD", "" if failure == "unconfigured" else "x")

    def fake_get(*a, **k):
        if failure == "down":
            raise httpx.ConnectError("refused")
        return FakeResp(500, {"status": "error"})
    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(tools.ToolError, match="not evidence of health"):
        tools.run_readonly("get_node_logs", "Node-2", {"created": time.time()})


def test_service_logs_only_for_services_that_exist(monkeypatch):
    monkeypatch.setattr(logs, "services", lambda node, hours=24: ["vllm-tts", "sshd"])
    with pytest.raises(tools.ToolError, match="has no logs"):
        tools.run_readonly("get_service_logs", "Node-4:../../etc/passwd")
    with pytest.raises(tools.ToolError, match="Node-n:service"):
        tools.run_readonly("get_service_logs", "Node-9:vllm-tts")


def test_resource_logs_only_for_registered_resources():
    with pytest.raises(tools.ToolError, match="registered resource"):
        tools.run_readonly("get_resource_logs", "mip-postgres-1")


def test_metric_window_fails_loudly_when_prometheus_is_down(monkeypatch):
    from backend.monitoring import promql

    def boom(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(promql, "query_range", boom)
    monkeypatch.setattr(promql, "wait_for_scrape", lambda *a, **k: None)
    with pytest.raises(tools.ToolError, match="not evidence of health"):
        tools.run_readonly("get_metric_window", "Node-2", {"created": time.time()})


def test_metric_window_only_for_known_nodes():
    with pytest.raises(tools.ToolError, match="known node"):
        tools.run_readonly("get_metric_window", 'Node-1"} or vector(1) or {x="')


@pytest.mark.parametrize("target", ["Node-2:0", "Node-2:70000", "Node-2:80;id", "Node-9:8001", "Node-2"])
def test_model_logs_target_must_be_node_and_port(target):
    with pytest.raises(tools.ToolError):
        tools.run_readonly("get_model_logs", target)


def test_model_logs_resolve_the_serving_unit(monkeypatch):
    import subprocess
    from backend.monitoring import kernel
    out = "docker-abc123def4567890.scope\n---\nabc123def4567890aaaa qwen-embed\nffff other\n"
    monkeypatch.setattr(kernel, "run", lambda acc, cmd: subprocess.CompletedProcess(cmd, 0, out, ""))
    assert tools._service_on_port("Node-3", 8105) == "qwen-embed"
    monkeypatch.setattr(kernel, "run", lambda acc, cmd: subprocess.CompletedProcess(cmd, 0, "vllm-qwen36.service\n---\n", ""))
    assert tools._service_on_port("Node-2", 8001) == "vllm-qwen36"
    monkeypatch.setattr(kernel, "run", lambda acc, cmd: subprocess.CompletedProcess(cmd, 0, "docker.service\n---\n", ""))
    assert tools._service_on_port("Node-2", 8001) is None

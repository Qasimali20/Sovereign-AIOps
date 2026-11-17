"""Contract tests against the live services the platform depends on.

They catch drift that unit tests cannot: an engine upgrade renaming a metric, a PromQL expression Prometheus no longer
accepts, Loki's auth gate opening up, the gateway serving models without a key. Read-only and light: instant
queries, one small log search, no model calls. Each group is skipped when its service is unreachable.
"""
from pathlib import Path

import httpx
import pytest

from backend.monitoring import boards, logs, promql
from backend.core import config

ENV = {k: v for k, _, v in (ln.partition("=") for ln in (Path(__file__).parents[1] / ".env").read_text().splitlines()
                            if "=" in ln and not ln.lstrip().startswith("#"))} if (Path(__file__).parents[1] / ".env").exists() else {}


def _reachable(url: str) -> bool:
    try:
        httpx.get(url, timeout=3)
        return True
    except httpx.HTTPError:
        return False


prom = pytest.mark.skipif(not _reachable(config.PROMETHEUS_URL + "/-/ready"), reason="Prometheus unreachable")
loki = pytest.mark.skipif(not _reachable(logs.LOKI_URL + "/ready"), reason="Loki unreachable")
gw = pytest.mark.skipif(not _reachable(config.GATEWAY_URL + "/healthz"), reason="gateway unreachable")


def _instant(expr: str) -> dict:
    r = httpx.get(config.PROMETHEUS_URL + "/api/v1/query", params={"query": expr}, timeout=10)
    return r.json()


# ---------------------------------------------------------------- Prometheus
def _expressions():
    for name, b in boards.BOARDS.items():
        for s in b["stats"]:
            yield f"{name}:stat:{s.title}", s.expr
        for p in b["panels"]:
            yield f"{name}:{p.id}", p.expr


@prom
@pytest.mark.parametrize("label,expr", list(_expressions()), ids=[x[0] for x in _expressions()])
def test_every_board_expression_is_valid_promql(label, expr):
    body = _instant(expr)
    assert body.get("status") == "success", f"{label}: {body.get('error')}"


@prom
@pytest.mark.parametrize("metric", ["aiops_node_up", "aiops_node_cpu_percent", "aiops_gpu_utilization_percent",
                                    "aiops_gpu_temp_celsius", "aiops_model_ready", "aiops_detector_last_scan_timestamp_seconds"])
def test_platform_exporter_series_exist(metric):
    nodes = {r["metric"].get("node") for r in _instant(metric)["data"]["result"]}
    assert nodes >= {n.name for n in config.NODES} or metric.startswith(("aiops_detector", "aiops_model")), (metric, nodes)
    assert nodes, f"{metric} has no series"


@prom
@pytest.mark.parametrize("metric", ["vllm:request_success_total", "vllm:time_to_first_token_seconds_bucket",
                                    "vllm:e2e_request_latency_seconds_bucket", "vllm:kv_cache_usage_perc",
                                    "vllm:num_requests_running", "vllm:num_requests_waiting"])
def test_vllm_series_carry_the_labels_the_boards_group_by(metric):
    res = _instant(f"count by (model, kind, node) ({metric})")["data"]["result"]
    assert res, f"no {metric} series: did a vLLM upgrade rename it?"
    for r in res:
        assert {"model", "kind", "node"} <= set(r["metric"]), (metric, r["metric"])
        assert r["metric"]["kind"] in {"LLM", "VLM", "OCR", "Embed", "Rerank", "ASR", "TTS"}, r["metric"]


@prom
def test_range_queries_have_the_shape_the_boards_parse():
    import time
    end = time.time() // 60 * 60
    res = promql.query_range("aiops_node_up", end - 600, end, 60)
    assert res and all("metric" in s and s["values"] and len(s["values"][0]) == 2 for s in res)


# ---------------------------------------------------------------- Loki
@loki
def test_loki_refuses_anonymous_queries():
    r = httpx.get(logs.LOKI_URL + "/loki/api/v1/labels", timeout=5)
    assert r.status_code == 401


@loki
def test_query_credentials_cannot_push(monkeypatch):
    pw = ENV.get("AIOPS_LOKI_PASSWORD") or pytest.skip("Loki query password not configured")
    r = httpx.post(logs.LOKI_URL + "/loki/api/v1/push", json={"streams": []}, auth=(logs.LOKI_USER, pw), timeout=5)
    assert r.status_code in (401, 403)


@loki
def test_log_search_contract(monkeypatch):
    monkeypatch.setattr(logs, "LOKI_PASSWORD", ENV.get("AIOPS_LOKI_PASSWORD") or pytest.skip("Loki query password not configured"))
    import time
    node = config.NODES[0].name
    assert logs.services(node, hours=1), f"no log streams from {node} in the last hour: is Alloy shipping?"
    out = logs.search(node, time.time() - 900, time.time(), limit=5)
    assert {"query", "window", "returned", "entries"} <= set(out) and out["returned"] <= 5
    for e in out["entries"]:
        assert {"ts", "service", "line", "time"} <= set(e) and len(e["line"]) <= logs.MAX_LINE_CHARS


@loki
def test_every_node_ships_logs(monkeypatch):
    monkeypatch.setattr(logs, "LOKI_PASSWORD", ENV.get("AIOPS_LOKI_PASSWORD") or pytest.skip("Loki query password not configured"))
    silent = [n.name for n in config.NODES if not logs.services(n.name, hours=1)]
    assert not silent, f"no logs in the last hour from {silent}"


# ---------------------------------------------------------------- gateway
@gw
@pytest.mark.parametrize("method,path", [("GET", "/v1/models"), ("POST", "/v1/chat/completions"), ("GET", "/gateway/stats")])
def test_gateway_requires_a_key(method, path):
    r = httpx.request(method, config.GATEWAY_URL + path, json={"model": "x", "messages": []}, timeout=5)
    assert r.status_code == 401 and r.json()["error"]["type"] == "authentication_error"


@gw
def test_gateway_rejects_a_wrong_key():
    r = httpx.get(config.GATEWAY_URL + "/v1/models", headers={"authorization": "Bearer sk-not-a-real-key"}, timeout=5)
    assert r.status_code == 401


@gw
def test_gateway_lists_models_in_openai_shape():
    key = ENV.get("AIOPS_GATEWAY_KEY") or pytest.skip("gateway key not configured")
    r = httpx.get(config.GATEWAY_URL + "/v1/models", headers={"authorization": f"Bearer {key}"}, timeout=5)
    body = r.json()
    assert r.status_code == 200 and body["object"] == "list" and body["data"]
    assert all({"id", "object", "owned_by"} <= set(m) for m in body["data"])
    assert any(m.get("healthy_replicas") for m in body["data"]), "no model has a healthy replica"
    for alias in config.MODEL_ALIASES:
        assert any(m["id"] == alias for m in body["data"]), f"alias {alias} not served"


@gw
def test_gateway_metrics_exposition():
    text = httpx.get(config.GATEWAY_URL + "/metrics", timeout=5).text
    for family in ("aiops_gateway_requests_total", "aiops_gateway_errors_total", "aiops_gateway_up",
                   "aiops_gateway_inflight", "aiops_gateway_failovers_total"):
        assert f"# TYPE {family} " in text, family
    ups = [ln for ln in text.splitlines() if ln.startswith("aiops_gateway_up{")]
    assert ups and all('model="' in ln and 'node="' in ln for ln in ups)

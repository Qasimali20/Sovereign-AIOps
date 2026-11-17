"""SGLang and Ollama discovery: rows in Spark Monitor's shape, and every consumer handling them correctly."""
import time

from backend.core import config
from backend.incidents import detector
from backend.monitoring import engines, metrics

PROBE_OUT = {
    "omni_pids": [111],
    "sglang": [{"pid": 200, "port": 30000, "name": "/models/Qwen2.5-7B-Instruct", "started": 1.0, "health": 200,
                "is_generation": True, "metrics": True, "bind": "any",
                "values": {"sglang:num_running_reqs": 2.0, "sglang:num_queue_reqs": 1.0, "sglang:token_usage": 0.4,
                           "sglang:gen_throughput": 55.0, "sglang:cache_hit_rate": 0.3}},
               {"pid": 201, "port": 30001, "name": "bge", "started": 1.0, "health": 503, "is_generation": False,
                "metrics": False, "bind": "loopback", "values": {}}],
    "ollama": [{"pid": 300, "port": 11434, "started": 1.0, "api": True, "version": "0.31.2", "bind": "loopback", "models": [
        {"name": "qwen3.5:35b-a3b", "capabilities": ["completion", "vision"], "loaded": True, "size_vram": 24_000_000_000},
        {"name": "bge-m3:latest", "capabilities": ["embedding"], "loaded": False},
        {"name": "qllama/bge-reranker-v2-m3:latest", "capabilities": ["embedding"], "loaded": False}]}],
}


def test_rows_have_spark_monitor_shape_and_engine():
    r = {x["name"]: x for x in engines.rows(PROBE_OUT)}
    sg = r["Qwen2.5-7B-Instruct"]
    assert (sg["engine"], sg["kind"], sg["status"], sg["running"], sg["waiting"], sg["kv_cache"]) == ("sglang", "LLM", "ready", 2.0, 1.0, 0.4)
    assert (r["bge"]["kind"], r["bge"]["status"]) == ("Embed", "loading")
    assert (r["qwen3.5:35b-a3b"]["status"], r["qwen3.5:35b-a3b"]["kind"], r["qwen3.5:35b-a3b"]["mem_gb"]) == ("ready", "VLM", 24.0)
    assert (r["bge-m3:latest"]["status"], r["bge-m3:latest"]["kind"]) == ("idle", "Embed")
    assert r["qllama/bge-reranker-v2-m3:latest"]["kind"] == "Rerank"


def test_ollama_api_down_is_one_error_row():
    rows = engines.rows({"ollama": [{"pid": 1, "port": 11434, "api": False, "models": []}]})
    assert [(x["name"], x["status"]) for x in rows] == [("ollama", "error")]


def test_merge_labels_vllm_engines_and_appends_others(monkeypatch):
    monkeypatch.setattr(engines, "_state", {"Node-2": (time.time(), PROBE_OUT)})
    monkeypatch.setattr(engines, "start", lambda: None)
    d = engines.merge("Node-2", {"vllm": [{"name": "a", "port": 8001, "pid": 110}, {"name": "tts", "port": 8091, "pid": 111}]})
    eng = {s["name"]: s["engine"] for s in d["vllm"]}
    assert eng["a"] == "vllm" and eng["tts"] == "vllm-omni" and eng["Qwen2.5-7B-Instruct"] == "sglang"
    assert sum(s["engine"] == "ollama" for s in d["vllm"]) == 3


def test_stale_scan_drops_rows(monkeypatch):
    monkeypatch.setattr(engines, "_state", {"Node-2": (time.time() - engines.STALE_S - 1, PROBE_OUT)})
    monkeypatch.setattr(engines, "start", lambda: None)
    assert engines.servers("Node-2") == []


def test_prometheus_targets_skip_ollama_and_sglang_without_metrics(monkeypatch):
    fleet = {"Node-2": {"vllm": engines.rows(PROBE_OUT)}}
    t = metrics.vllm_targets(fleet, reachable=lambda a: True)
    assert [x["labels"]["model"] for x in t] == ["Qwen2.5-7B-Instruct"]
    assert t[0]["labels"]["engine"] == "sglang"


def test_ollama_unloading_is_not_a_disappearance():
    node = config.NODE_BY_NAME["Node-2"]
    seen: set = set()
    loaded = {"uptime_s": 1e6, "vllm": engines.rows(PROBE_OUT)}
    assert detector.missing_model_alerts(node, loaded, seen, "t") == []
    unloaded = {"uptime_s": 1e6, "vllm": [dict(s, status="idle") if s["engine"] == "ollama" else s for s in engines.rows(PROBE_OUT)]}
    assert detector.missing_model_alerts(node, unloaded, seen, "t") == []          # still present, just not in memory
    gone = {"uptime_s": 1e6, "vllm": [s for s in engines.rows(PROBE_OUT) if s["engine"] != "ollama"]}
    assert [a.resource for a in detector.missing_model_alerts(node, gone, seen, "t")] == ["qwen3.5:35b-a3b@11434"]


def test_engine_not_answering_alerts():
    node = config.NODE_BY_NAME["Node-2"]
    rows = engines.rows({"ollama": [{"pid": 1, "port": 11434, "api": False, "models": []}]})
    alerts = detector.metric_alerts(node, {"gpu": {"temp": 40}, "vllm": rows, "mem": {}, "disk": {}}, "t", time.time())
    assert "model_engine_down" in [a.kind for a in alerts]


def test_probe_ignores_itself():
    assert "AIOPS_PROBE" in engines.PROBE


def test_full_model_name_comes_from_the_loaded_weights():
    from backend.incidents.tools import full_name
    assert full_name({"name": "qwen-embed", "model": "/models/Qwen3-Embedding-0.6B"}) == "qwen3-embedding-0.6b"
    assert full_name({"name": "qwen-chat", "model": "Qwen/Qwen3-30B-A3B-Instruct-2507-FP8"}) == "qwen3-30b-a3b-instruct-2507-fp8"
    assert full_name({"name": "qwen3-asr", "model": "Qwen/Qwen3-ASR-0.6B"}) == "qwen3-asr-0.6b"
    assert full_name({"name": "qwen3.5:35b-a3b", "model": None}) == "qwen3.5:35b-a3b"      # Ollama: the tag is the name

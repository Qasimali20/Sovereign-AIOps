"""Phase 4: dependency graph and impact, trend rules, placement."""
from backend.core import config
from backend.llmops import placement
from backend.monitoring import topology, trends

SRV = lambda name, port, status="ready", share=0.1: {"name": name, "port": port, "status": status, "kind": "LLM", "mem_fraction": share}  # noqa: E731


def fleet(n4_chat="ready"):
    return {"Node-1": {"vllm": [SRV("qwen3-asr", 8092)]},
            "Node-2": {"vllm": [SRV("qwen3.6-35b-a3b-fp8", 8001), SRV("qwen3-embedding-4b", 8093), SRV("qwen3-reranker-4b", 8094)]},
            "Node-3": {"vllm": [SRV("qwen-chat", 8000)]},
            "Node-4": {"vllm": [SRV("qwen3.6-35b-a3b-fp8", 8002, n4_chat)]}}


SVC = {"Node-3": {"containers": [{"name": "mip-api-1", "state": "running", "image": "mip"},
                                 {"name": "qwen-embed", "state": "running", "image": "vllm/vllm-openai"},
                                 {"name": "aiops-log-agent", "state": "running", "image": "grafana/alloy"}]}}


def ids(rows, state=None):
    return {r["id"] for r in rows if state is None or r["state"] == state}


def test_graph_shape():
    g = topology.build(fleet(), SVC)
    assert "stack:Node-3/mip" in g and "stack:Node-3/aiops" not in g and "stack:Node-3/qwen" not in g
    assert g["model:qwen3.6-35b-a3b-fp8"].reqs[0].any_of == ["server:Node-2/qwen3.6-35b-a3b-fp8@8001",
                                                             "server:Node-4/qwen3.6-35b-a3b-fp8@8002"]
    st = topology.evaluate(g)
    assert st["route:tts"]["state"] == "down"                                       # no TTS server in this fleet
    assert all(v["state"] == "up" for k, v in st.items() if k not in ("route:tts", "component:evals"))


def test_replicas_and_fallbacks_turn_failure_into_degradation():
    g = topology.build(fleet(), SVC)
    i = topology.impact(g, "node:Node-4")
    assert {"model:qwen3.6-35b-a3b-fp8", "route:chat"} <= ids(i, "degraded") and "component:agent" not in ids(i, "down")
    i2 = topology.impact(g, "node:Node-2")
    assert {"component:gateway", "route:embed", "component:agent"} <= ids(i2, "down")
    rag = next(r for r in topology.impact(g, "server:Node-2/qwen3-embedding-4b@8093") if r["id"] == "component:rag")
    assert rag["state"] == "degraded" and "keyword" in rag["why"]                  # retrieval has a fallback


def test_impact_is_relative_to_what_is_already_broken():
    g = topology.build(fleet(n4_chat="loading"), SVC)                               # Node-4's replica already down
    i = topology.impact(g, "server:Node-2/qwen3.6-35b-a3b-fp8@8001")
    assert "model:qwen3.6-35b-a3b-fp8" in ids(i, "down")                            # the last replica
    assert "route:chat" in ids(i, "degraded")                                       # falls back to qwen-chat


def test_incident_maps_to_a_vertex():
    assert topology.for_incident("gpu_xid", "Node-4", "xid-43") == "node:Node-4"
    assert topology.for_incident("model_perf_regression", "Node-4", "qwen3.6@8002:short") == "server:Node-4/qwen3.6@8002"
    assert topology.for_incident("svc_container_unhealthy", "Node-3", "mip-api-1") == "stack:Node-3/mip"
    assert topology.for_incident("sec_bruteforce", "Node-1", "x") is None


def test_trend_rules():
    assert trends.memory_creep(100, 121.7, 5.0, True, 2)["status"] == "warn"        # 12 GB to the limit at 5/day
    assert trends.memory_creep(100, 121.7, 5.0, False, 2)["status"] == "ok"         # a server started: not a leak
    assert trends.memory_creep(60, 121.7, 5.0, True, 2)["status"] == "ok"           # far from the limit
    assert trends.memory_creep(100, 121.7, 5.0, True, 0.5)["status"] == "insufficient"
    assert trends.gpu_idle_warmer(56, 47)["status"] == "warn" and trends.gpu_idle_warmer(49, 47)["status"] == "ok"
    assert trends.gpu_idle_warmer(None, 47)["status"] == "insufficient"
    assert trends.wifi_weakening(-72, -64)["status"] == "warn" and trends.wifi_weakening(-66, -64)["status"] == "ok"
    assert [r["resource"] for r in trends.recurring({"xid-43": 3, "xid-13": 2}, "GPU driver errors")] == ["xid-43"]


def test_trend_alert_kinds():
    rep = {"nodes": {"Node-4": {"rules": {"memory_creep": {"status": "warn", "detail": "d"}, "wifi_weakening": {"status": "ok", "detail": ""}},
                                "recurring": [{"resource": "xid-43", "detail": "GPU driver errors on 3 different days this week"},
                                              {"resource": "vllm-tts.service", "detail": "restarts on 4 different days this week"}]}}}
    assert sorted(a.kind for a in trends.alerts(rep)) == ["trend_memory_creep", "trend_restart_recurring", "trend_xid_recurring"]


NODES = {"Node-1": {"free_gb": 10, "gpu_avg_1d": 1, "busy_servers": 0, "gpu_temp_max_1d": 50},
         "Node-3": {"free_gb": 44, "gpu_avg_1d": 5, "busy_servers": 0, "gpu_temp_max_1d": 48},
         "Node-2": {"free_gb": 60, "gpu_avg_1d": 40, "busy_servers": 2, "gpu_temp_max_1d": 70}}


def test_rank_fits_then_anti_affinity_then_load():
    r = placement.rank(30, NODES, {"Node-2": {"m"}})
    assert [x["node"] for x in r] == ["Node-3", "Node-2", "Node-1"] and r[2]["rank"] is None
    r2 = placement.rank(30, NODES | {"Node-2": NODES["Node-2"] | {"gpu_avg_1d": 0}}, {"Node-2": {"m"}}, "m")
    assert r2[0]["node"] == "Node-3" and r2[1]["same_model"]                          # a replica beside itself is no failover


def test_advice_finds_single_points_of_failure():
    g = topology.build(fleet(), SVC)
    cap = {"nodes": {n.name: {"memory": {"free_gb": {"Node-3": 44}.get(n.name, 5), "total_gb": 121.7,
                                         "reserved": [{"model": "qwen3-embedding-4b", "share": 0.1}] if n.name == "Node-2" else []}}
                     for n in config.NODES}}
    adv = placement.advice(cap, g, {})
    embed = next(a for a in adv if "'embed'" in a["title"])
    assert "Node-3" in embed["action"] and "12 GB" in embed["action"]
    assert not any("'chat'" in a["title"] for a in adv)                               # two replicas plus a fallback model
    assert {a["title"] for a in adv if a["kind"] == "tight_node"} >= {"Node-1 has 5 GB free"}

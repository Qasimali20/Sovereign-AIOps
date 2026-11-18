"""Inference intelligence: benchmark guard and regression, bottleneck rules, capacity maths."""
from backend.core import config
from backend.incidents import detector, runtime
from backend.llmops import bottleneck, capacity, perf


# ---------------------------------------------------------------- benchmarks
def fleet(temp=45, util=0, running=0, status="ready", kind="LLM"):
    return {"Node-4": {"gpu": {"temp": temp, "util": util},
                       "vllm": [{"name": "qwen3.6", "kind": kind, "port": 8002, "status": status, "running": running, "waiting": 0},
                                {"name": "embed", "kind": "Embed", "port": 8003, "status": "ready"}]}}


def test_benchmark_targets_only_quiet_chat_servers():
    [t] = perf.targets(fleet())
    assert (t["model"], t["skip"]) == ("qwen3.6", None)                       # the embedding server is not a target
    assert "GPU at 72" in perf.targets(fleet(temp=72))[0]["skip"]
    assert "busy" in perf.targets(fleet(util=80))[0]["skip"]
    assert perf.targets(fleet(running=1))[0]["skip"] == "serving requests"
    assert "not ready" in perf.targets(fleet(status="loading"))[0]["skip"]


def test_prompts_never_share_a_prefix():
    a, b = perf.prompt("long_prompt"), perf.prompt("long_prompt")
    assert a[:14] != b[:14] and len(a.split()) > 1000


def runs(decodes, ttfts=None, version="v1", last_version=None):
    ttfts = ttfts or [0.1] * len(decodes)
    rows = [{"decode_tps": d, "ttft_s": t, "version_id": version, "engine_version": "0.11", "t": i} for i, (d, t) in enumerate(zip(decodes, ttfts))]
    if last_version:
        rows[-1]["version_id"] = last_version
    return rows


def test_regression_needs_two_slow_runs_against_the_baseline():
    assert perf.assess(runs([56, 55, 57, 56, 30]), "short") is None                 # one slow run: noise
    a = perf.assess(runs([56, 55, 57, 56, 30, 31]), "short")
    assert a and "decode 31.0 tok/s vs usual 56.0" in a["findings"][0] and not a["version_changed"]
    assert perf.assess(runs([56, 55, 57, 50, 49]), "short") is None                 # within 25%
    assert perf.assess(runs([56, 57, 30, 31]), "short") is None                     # too few runs for a baseline
    v = perf.assess(runs([56, 55, 57, 56, 30, 31], last_version="v2"), "short")
    assert v["version_changed"]


def test_first_token_regression_needs_a_real_absolute_rise():
    assert perf.assess(runs([56] * 6, [0.10, 0.10, 0.11, 0.10, 0.20, 0.21]), "short") is None   # +0.1 s: below the floor
    a = perf.assess(runs([56] * 6, [0.40, 0.42, 0.41, 0.40, 0.90, 0.95]), "long_prompt")
    assert a and a["findings"] == ["first token 0.95 s vs usual 0.41 s"]


def test_perf_regression_clears_when_not_firing():
    i = {"kind": "model_perf_regression", "node": "Node-4", "resource": "qwen3.6@8002:short", "fingerprint": "f"}
    det = detector.DetectorState()
    det.reachable["Node-4"] = True
    assert "back to the usual level" in runtime.condition_cleared(i, det, {}, set())
    assert runtime.condition_cleared(i, det, {}, {"f"}) is None


# ---------------------------------------------------------------- bottlenecks
BASE = {"requests": 200, "e2e_s": 10.0, "queue_s": 0.1, "prefill_s": 1.0, "decode_s": 8.9, "itl_s": 0.02,
        "kv_max": 0.3, "preemptions": 0, "waiting_avg": 0, "waiting_max": 0, "running_max": 4, "running_avg": 1.5,
        "prompt_len": 500, "output_len": 400, "ttft_s": 1.1}


def rules(s, node=None, neighbours=(), bench=None):
    return [f["rule"] for f in bottleneck.classify(BASE | s, node or {}, list(neighbours), bench)]


def test_bottleneck_rules():
    assert rules({"requests": 2}) == ["idle"]
    assert rules({}) == ["decode_bound"]
    assert rules({"queue_s": 4.0, "decode_s": 5.0, "waiting_avg": 2, "waiting_max": 9})[0] == "queueing"
    assert "kv_pressure" in rules({"kv_max": 0.97})
    assert "kv_pressure" in rules({"preemptions": 3})
    assert rules({"prefill_s": 7.0, "decode_s": 2.9}) == ["prefill_bound"]
    assert "gpu_shared" in rules({}, {"gpu_max": 99}, [("omni", 40.0)])
    assert "gpu_shared" not in rules({}, {"gpu_max": 99}, [("omni", 0.5)])          # an idle neighbour does not count
    assert "slow_under_load" in rules({"itl_s": 0.05}, bench=56)                     # 20 tok/s live vs 56 idle
    warnings_first = bottleneck.classify(BASE | {"kv_max": 0.95}, {}, [], None)
    assert warnings_first[0]["severity"] == "warning"


# ---------------------------------------------------------------- capacity
GB = 1e9


def test_disk_forecast_ignores_one_off_downloads():
    daily = [436, 436, 438, 441, 788, 788, 789]
    d = capacity.disk_forecast(789 * GB, 4032 * GB, [x * GB for x in daily])
    assert round(d["largest_jump_gb"]) == 347 and 0 < d["growth_gb_per_day"] < 3       # the jump is not the pace
    assert d["days_to_warn"] > 365 and d["jumps_left"] == 7
    assert capacity.disk_forecast(789 * GB, 4032 * GB, [789 * GB])["growth_gb_per_day"] is None   # not enough history


def test_memory_room_is_measured_free_memory():
    gib = 1024 ** 3
    m = capacity.memory_room(114.3 * gib, 121.7 * gib, 114.6 * gib,
                             [{"model": "qwen3.6", "share": 0.4, "status": "ready"}, {"model": "omni", "share": None, "status": "ready"}])
    assert m["tight"] and m["max_new_share"] == 0.04 and m["unknown_shares"] == ["omni"] and m["reserved_share"] == 0.4


def test_traffic_trend_needs_most_of_a_week():
    assert capacity.trend([700, 720, 90])["change_pct"] is None
    t = capacity.trend([100, 110, 120, 130, 140, 150, 20])                            # today (partial) left out
    assert round(t["slope_per_day"]) == 10 and round(t["change_pct"]) == 40


def test_disk_forecast_alert_only_inside_the_horizon():
    rep = {"nodes": {"Node-1": {"disk": {"pct": 60.0, "days_to_warn": 9.0}}, "Node-2": {"disk": {"pct": 20.0, "days_to_warn": 900.0}},
                     "Node-3": {"disk": {"pct": 90.0, "days_to_warn": 0.0}}, "Node-4": {"disk": None}}}
    assert [a.node for a in capacity.alerts(rep)] == ["Node-1"]          # Node-3 is already past: disk_usage covers it
    assert config.DISK_PCT_WARN == 85.0

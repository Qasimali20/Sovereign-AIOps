"""Metric history boards for the web UI: the same views as the Grafana dashboards, served from Prometheus.

The browser picks a board and a time range; every PromQL expression is fixed here (no client-supplied
queries). Series are grouped by node (fixed colour slot per node) or by model (the 4 busiest models are
shown; the rest are folded into "Other" for additive metrics, or listed as hidden otherwise)."""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from backend.core import config
from backend.monitoring import promql

RANGES = {"1h": (3600, 30), "6h": (6 * 3600, 120), "24h": (86400, 300), "7d": (7 * 86400, 1800), "30d": (30 * 86400, 7200)}
NODES = ("Node-1", "Node-2", "Node-3", "Node-4")
FINAL = "resolved|recovered|closed|action_rejected"
MAX_SERIES = 4
_pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="boards")
_cache: dict[tuple[str, str], tuple[float, dict]] = {}
CACHE_S = 15


@dataclass(frozen=True)
class Panel:
    id: str
    title: str
    expr: str
    unit: str                 # percent | celsius | watt | Bps | s | short | reqps | tps
    by: str | None = "node"   # label that names each series: node | model | domain | status | None (single series)
    additive: bool = False    # sum of the hidden series is meaningful ("Other")
    max: float | None = None
    desc: str = ""
    section: str = ""
    absent: str = ""          # why the chart can be empty (shown when it is)
    kind: str = "line"        # line | stacked | timeline | bars | donut (latest value per series)
    warn: float | None = None # alert threshold drawn as a reference line (same value the detector uses)
    warn_label: str = ""
    peak: bool = False        # plot the highest value in each step, so a burst shorter than the step is not skipped


@dataclass(frozen=True)
class Stat:
    title: str
    expr: str
    unit: str = "short"
    desc: str = ""


q95 = lambda m, sel="", w="5m": f"histogram_quantile(0.95, sum by (le, model) (rate(vllm:{m}_bucket{sel}[{w}])))"
SPARSE_W = "15m"      # speech traffic is a request every few minutes: a 5 m window leaves most points empty
GEN, RET, SPEECH = '{kind=~"LLM|VLM|OCR"}', '{kind=~"Embed|Rerank"}', '{kind=~"ASR|TTS"}'


BOARDS: dict[str, dict] = {
    "fleet": {
        "title": "Fleet",
        "stats": [
            Stat("Nodes reporting", "sum(aiops_node_up)", desc="Nodes whose Spark Monitor answered the last scrape."),
            Stat("Models ready", "sum(aiops_model_ready)", desc="Model servers ready across the fleet."),
            Stat("Containers running", 'sum(aiops_containers{state="running"})'),
            Stat("Failed services", "sum(aiops_systemd_units_failed) + sum(aiops_containers_unhealthy)",
                 desc="Failed systemd units plus unhealthy containers."),
        ],
        "panels": [
            Panel("cpu", "CPU", "aiops_node_cpu_percent", "percent", max=100, warn=config.CPU_PCT_WARN, peak=True,
                  warn_label="incident if sustained", desc="Peak in each interval.", section="Utilisation"),
            Panel("mem", "Memory used", "100 * aiops_node_memory_used_bytes / aiops_node_memory_total_bytes", "percent", max=100,
                  warn=config.MEM_PCT_WARN, warn_label="incident",
                  desc="Unified memory: model weights and KV cache count here.", section="Utilisation"),
            Panel("gpu_temp", "GPU temperature", "aiops_gpu_temp_celsius", "celsius", warn=config.GPU_TEMP_WARN, peak=True,
                  warn_label="incident", desc="Peak in each interval.", section="Utilisation"),
            Panel("gpu_power", "Fleet power draw", "aiops_gpu_power_watts", "watt", kind="stacked",
                  desc="GPU power, stacked: the top edge is the fleet total.", section="Utilisation"),
            Panel("disk", "Disk", "100 * aiops_node_disk_used_bytes / aiops_node_disk_total_bytes", "percent", max=100,
                  kind="bars", warn=config.DISK_PCT_WARN, desc="Used now, and the change over the range.", section="Storage and network"),
            Panel("rx", "Network in", "aiops_node_network_receive_bytes_per_second", "Bps", kind="stacked",
                  desc="Stacked by node: the top edge is the fleet total.", section="Storage and network"),
            Panel("tx", "Network out", "aiops_node_network_transmit_bytes_per_second", "Bps", kind="stacked",
                  desc="Stacked by node.", section="Storage and network"),
            Panel("containers", "Containers running", 'aiops_containers{state="running"}', "short", kind="stacked", section="Services"),
            Panel("scan_age", "Detector scan age", "time() - aiops_detector_last_scan_timestamp_seconds", "s", by=None, warn=120, max=240,
                  warn_label="alert", desc="Prometheus alerts if this exceeds 2 minutes: the watcher of the watcher.", section="Services"),
        ],
    },
    "models": {
        "title": "Model serving",
        "stats": [
            Stat("Requests / min", "(sum(rate(vllm:request_success_total[5m])) or vector(0)) * 60 + (sum(rate(vllm_omni:requests_success_total[5m])) or vector(0)) * 60",
                 "rpm", desc="Completed requests across every model server."),
            Stat("Failed requests", '100 * (sum(rate(vllm:request_success_total{finished_reason=~"abort|error"}[1h])) or vector(0)) / '
                 'clamp_min(sum(rate(vllm:request_success_total[1h])), 1e-9)', "percent", desc="Aborted or errored, last hour."),
            Stat("Chat time to first token", f'histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket{GEN}[15m])))',
                 "s", desc="p95 over chat, vision and OCR models, last 15 minutes."),
            Stat("Generated tokens / s", "sum(rate(vllm:generation_tokens_total[2m]))", "tps"),
        ],
        "panels": [
            # generative: chat, vision, OCR
            Panel("ttft", "Time to first token (p95)", q95("time_to_first_token_seconds", GEN), "s", by="model",
                  warn=config.TTFT_P95_WARN_S, warn_label="degraded", section="Chat, vision and OCR models"),
            Panel("gen_tps", "Generated tokens / s, all requests", f"sum by (model) (rate(vllm:generation_tokens_total{GEN}[2m]))", "tps", by="model",
                  additive=True, kind="stacked", section="Chat, vision and OCR models",
                  desc="Throughput: every token written per second, idle time included. Low when traffic is light."),
            # 1 / mean time per output token: how fast one answer is written, independent of how many arrive
            Panel("write_speed", "Write speed per request", f"sum by (model) (rate(vllm:request_time_per_output_token_seconds_count{GEN}[{SPARSE_W}])) / "
                  f"sum by (model) (rate(vllm:request_time_per_output_token_seconds_sum{GEN}[{SPARSE_W}]))", "tps", by="model",
                  desc="Tokens per second while a single answer is being written: the model's own speed.",
                  absent="no answers finished in this range", section="Chat, vision and OCR models"),
            Panel("kv", "KV cache used", f"100 * max by (model) (vllm:kv_cache_usage_perc{GEN})", "percent", by="model", max=100,
                  warn=90, warn_label="near full", desc="Working memory for requests in flight.", section="Chat, vision and OCR models"),
            Panel("prefix", "Prompt cache hit rate", f"100 * sum by (model) (rate(vllm:prefix_cache_hits_total{GEN}[15m])) / "
                  f"sum by (model) (rate(vllm:prefix_cache_queries_total{GEN}[15m]))", "percent", by="model", max=100,
                  desc="Share of prompt tokens served from cache: higher is faster and cheaper.", section="Chat, vision and OCR models"),
            Panel("running", "Requests in progress", f"sum by (model) (vllm:num_requests_running{GEN})", "short", by="model",
                  additive=True, kind="stacked", section="Chat, vision and OCR models"),
            Panel("waiting", "Requests waiting", f"sum by (model) (vllm:num_requests_waiting{GEN})", "short", by="model", additive=True,
                  section="Chat, vision and OCR models"),
            Panel("tpot", "Time per output token (p95)", q95("request_time_per_output_token_seconds", GEN), "s", by="model",
                  desc="Streaming speed once the answer starts.", section="Chat, vision and OCR models"),
            Panel("queue", "Queue time (p95)", q95("request_queue_time_seconds", GEN), "s", by="model",
                  desc="Waiting before the model starts: rising queue time means the server is full.", section="Chat, vision and OCR models"),
            Panel("e2e", "Whole request (p95)", q95("e2e_request_latency_seconds", GEN), "s", by="model", section="Chat, vision and OCR models"),
            Panel("prompt_tps", "Prompt tokens / s (read)", f"sum by (model) (rate(vllm:prompt_tokens_total{GEN}[2m]))", "tps", by="model",
                  additive=True, kind="stacked", section="Chat, vision and OCR models"),
            Panel("failed", "Failed or aborted requests / min", 'sum by (model) (rate(vllm:request_success_total{kind=~"LLM|VLM|OCR",finished_reason=~"abort|error"}[5m])) * 60',
                  "rpm", by="model", additive=True, section="Chat, vision and OCR models"),
            Panel("preempt", "Preemptions / min", f"sum by (model) (rate(vllm:num_preemptions_total{GEN}[5m])) * 60", "rpm", by="model",
                  additive=True, desc="Requests paused because KV memory ran out: should stay at zero.", section="Chat, vision and OCR models"),
            # embedding and rerank
            Panel("ret_rpm", "Requests / min", f"sum by (model) (rate(vllm:request_success_total{RET}[5m])) * 60", "rpm", by="model",
                  additive=True, kind="stacked", section="Embedding and rerank"),
            Panel("ret_e2e", "Latency (p95)", q95("e2e_request_latency_seconds", RET), "s", by="model", section="Embedding and rerank"),
            # speech
            Panel("sp_rpm", "Requests / min", f"sum by (model) (rate(vllm:request_success_total{SPEECH}[5m])) * 60 "
                  "or sum by (model) (rate(vllm_omni:requests_success_total[5m])) * 60", "rpm", by="model", additive=True, kind="stacked",
                  section="Speech (ASR and TTS)"),
            Panel("sp_e2e", "Latency (p95)", f"{q95('e2e_request_latency_seconds', SPEECH, SPARSE_W)} or "
                  f"histogram_quantile(0.95, sum by (le, model) (rate(vllm_omni:e2e_request_latency_s_bucket[{SPARSE_W}])))", "s", by="model",
                  section="Speech (ASR and TTS)"),
            Panel("sp_rtf", "Speech real-time factor (p50)", "histogram_quantile(0.5, sum by (le, model) (rate(vllm_omni:audio_rtf_bucket[15m])))",
                  "x", by="model", warn=1, warn_label="slower than real time", desc="Seconds of compute per second of audio.",
                  section="Speech (ASR and TTS)"),
            Panel("sp_asr_rtf", "ASR real-time factor (p50)",
                  "histogram_quantile(0.5, sum by (le, model) (rate(aiops_gateway_asr_rtf_bucket[15m])))", "x", by="model",
                  warn=1, warn_label="slower than real time",
                  desc="Processing time / audio length, measured by the gateway on transcriptions routed through it.",
                  absent="only measured for transcriptions sent through the gateway",
                  section="Speech (ASR and TTS)"),
            Panel("sp_ttfa", "Time to first audio (p95)",
                  f"histogram_quantile(0.95, sum by (le, model) (rate(vllm_omni:audio_ttfp_s_bucket[{SPARSE_W}])))", "s", by="model",
                  warn=1, warn_label="1 s", desc="Request arrival to first audio frame: what a caller waits before hearing anything.",
                  absent="the TTS engines on the fleet do not export it",
                  section="Speech (ASR and TTS)"),
            Panel("sp_ttft", "Time to first text (p95)", q95("time_to_first_token_seconds", '{kind="ASR"}', SPARSE_W), "s", by="model",
                  desc="Transcription: audio received to first text token.", section="Speech (ASR and TTS)"),
            Panel("sp_fail", "Failed requests",
                  '100 * sum by (model) (rate(http_requests_total{kind=~"ASR|TTS",handler=~"/v1/audio/(speech|transcriptions|translations)",status=~"4xx|5xx"}[15m]))'
                  ' / sum by (model) (rate(http_requests_total{kind=~"ASR|TTS",handler=~"/v1/audio/(speech|transcriptions|translations)"}[15m]))',
                  "percent", by="model", max=100, warn=2, warn_label="2%",
                  desc="Share of speech requests answered with an HTTP error (bad input or server fault).", section="Speech (ASR and TTS)"),
            Panel("sp_audio", "Audio generated", "sum by (model) (rate(vllm_omni:audio_duration_s_sum[5m])) * 60", "short", by="model",
                  additive=True, kind="stacked", desc="Seconds of speech produced per minute.", section="Speech (ASR and TTS)"),
            Panel("sp_load", "Requests in progress", f"sum by (model) (vllm:num_requests_running{SPEECH})", "short", by="model",
                  additive=True, kind="stacked", section="Speech (ASR and TTS)"),
            Panel("sp_silent", "Empty-audio responses", "sum by (model) (increase(vllm_omni:audio_skipped_requests_total[15m])) or sum by (model) (vllm_omni:audio_rtf_count) * 0", "short",
                  by="model", warn=0.5, warn_label="any",
                  desc="Requests answered 200 OK with no audio (the model rejected its own codec output). Silent failures.",
                  section="Speech (ASR and TTS)"),
        ],
    },
    "operations": {
        "title": "AI operations",              # charts shown on the Dashboard (no Metric history tab)
        "stats": [
            Stat("Open incidents", f'sum(aiops_incidents{{status!~"{FINAL}"}})'),
            Stat("Waiting for approval", "aiops_approvals_pending"),
            Stat("Actions executed", "sum(aiops_executions_total)", desc="All by the restricted executor, all after approval."),
            Stat("Verified fixed", 'sum(aiops_verifications_total{outcome="resolved"})'),
        ],
        "panels": [
            Panel("unhealthy", "Unhealthy containers + failed units", "aiops_containers_unhealthy + on(node) aiops_systemd_units_failed",
                  "short", section="Services"),
            Panel("models_ready", "Models ready", "sum by (node) (aiops_model_ready)", "short", kind="stacked", section="Services"),
        ],
    },
}


def _label(raw: str) -> str:
    """Readable series name: drop the org prefix from model ids; name unlabelled gateway upstreams by node:port."""
    from backend.core import config
    if raw.startswith("DGX-"):                 # history recorded before the nodes were renamed Node-1..4
        return "Node-" + raw[4:]
    if raw.startswith("?"):
        hostport = raw[1:].split("//")[-1].rstrip("/")
        host, _, port = hostport.partition(":")
        node = next((n.name for n in config.NODES if host in (n.lan_ip, n.ts_ip)), host)
        return f"{node}:{port} (no model name)"
    return raw.split("/")[-1]


def _panel(p: Panel, start: float, end: float, step: int) -> dict:
    out = {"id": p.id, "title": p.title, "unit": p.unit, "max": p.max, "desc": p.desc, "section": p.section,
           "by": p.by, "kind": p.kind, "absent": p.absent, "warn": p.warn, "warn_label": p.warn_label,
           "series": [], "points": [], "hidden": [], "events": [], "error": None}
    try:
        expr = f"max_over_time(({p.expr})[{step}s:])" if p.peak else p.expr
        res = promql.query_range(expr, start, end, step)
    except Exception as e:  # one broken panel must not blank the board
        out["error"] = str(e)[:160]
        return out
    cols: dict[str, dict[float, float]] = {}
    for s in res:
        name = (_label(s["metric"].get(p.by) or "(none)") if p.by else p.title)
        col = cols.setdefault(name, {})
        for t, v in s["values"]:
            try:
                fv = float(v)
            except ValueError:
                continue
            if fv == fv and abs(fv) != float("inf"):          # drop NaN / Inf (empty histograms)
                col[float(t)] = col.get(float(t), 0.0) + fv
    if p.by == "node":
        names = [n for n in NODES if n in cols] + sorted(n for n in cols if n not in NODES)
    else:  # busiest first, so the 4 shown are the ones that matter
        names = sorted(cols, key=lambda n: (-sum(cols[n].values()) / max(1, len(cols[n])), n))
    limit = 12 if p.kind == "donut" else MAX_SERIES
    shown, rest = names[:limit], names[limit:]
    if rest and p.additive:
        other: dict[float, float] = {}
        for n in rest:
            for t, v in cols[n].items():
                other[t] = other.get(t, 0.0) + v
        cols["Other"] = other
        shown = shown + ["Other"]
    out["hidden"] = [] if p.additive else rest
    out["series"] = [{"key": f"s{i}", "name": n, "slot": (NODES.index(n) if n in NODES else i) if n != "Other" else None}
                     for i, n in enumerate(shown)]
    # full time grid: a missing sample is an explicit gap (null), so charts never bridge it with a line
    have = set().union(*(cols[n].keys() for n in shown)) if shown else set()
    ts = sorted(have | {float(t) for t in range(int(start), int(end) + 1, step)}) if have else []
    out["points"] = [[t, *(cols[n].get(t) for n in shown)] for t in ts]
    if p.kind == "donut":                       # a ring shows the composition now: use an instant query, not the last step
        now = _instant(p.expr)
        if now is not None:
            if not shown and now:               # series newer than the last range step: take the segments from now
                shown = sorted(now, key=lambda n: -now[n])[:limit]
                out["series"] = [{"key": f"s{i}", "name": n, "slot": i} for i, n in enumerate(shown)]
            out["points"] = [[end, *(now.get(n) for n in shown)]] if shown else []
    if p.kind == "timeline":
        out["events"] = _reboots(shown, start, end, step)
    return out


def _reboots(nodes: list[str], start: float, end: float, step: int) -> list[dict]:
    """Restarts: points where a node's uptime went down. Uses the raw minimum per step so a reboot is not averaged away."""
    try:
        res = promql.query_range(f"min_over_time(aiops_node_uptime_seconds[{step}s])", start, end, step)
    except Exception:
        return []
    out = []
    for s in res:
        node = s["metric"].get("node")
        if node not in nodes:
            continue
        prev = None
        for t, v in s["values"]:
            v = float(v)
            if prev is not None and v + step < prev:
                out.append({"series": nodes.index(node), "t": float(t) - min(v, step), "kind": "restart"})
            prev = v
    return out


def _instant(expr: str) -> dict[str, float] | None:
    try:
        import httpx
        r = httpx.get(f"{config.PROMETHEUS_URL}/api/v1/query", params={"query": expr}, timeout=8)
        r.raise_for_status()
        out = {}
        for x in r.json()["data"]["result"]:
            v = float(x["value"][1])
            if v == v and abs(v) != float("inf"):           # drop NaN (no traffic) so the cell shows empty
                out[_label(next(iter(x["metric"].values()), ""))] = v
        return out
    except Exception:
        return None


def _stat(s: Stat) -> dict:
    try:
        import httpx
        from backend.core import config
        r = httpx.get(f"{config.PROMETHEUS_URL}/api/v1/query", params={"query": s.expr}, timeout=8)
        r.raise_for_status()
        res = r.json()["data"]["result"]
        v = float(res[0]["value"][1]) if res else None
        if v is not None and (v != v or abs(v) == float("inf")):
            v = None
    except Exception:
        v = None
    return {"title": s.title, "unit": s.unit, "desc": s.desc, "value": v}


SCORE_WINDOW = {"1h": "1h", "6h": "6h", "24h": "24h", "7d": "7d", "30d": "30d"}


def scorecard(range_: str) -> list[dict]:
    if range_ not in SCORE_WINDOW:
        raise KeyError(range_)
    hit = _cache.get(("scorecard", range_))
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    rows = _scorecard(range_)
    _cache[("scorecard", range_)] = (time.time(), rows)
    return rows


def _scorecard(range_: str) -> list[dict]:
    """One row per model server: traffic and quality over the chosen range, load right now. Columns that do not
    apply to a model type (time to first token for an embedder) come back empty, not zero."""
    w = SCORE_WINDOW[range_]
    hq = lambda m, q=0.95: f"histogram_quantile({q}, sum by (le, model) (rate(vllm:{m}_bucket[{w}])))"
    cols = {
        "rpm": f"sum by (model) (rate(vllm:request_success_total[{w}])) * 60 or sum by (model) (rate(vllm_omni:requests_success_total[{w}])) * 60" + " or " + f"sum by (model) (rate(sglang:num_requests_total[{w}])) * 60",
        "requests": f"sum by (model) (increase(vllm:request_success_total[{w}])) or sum by (model) (increase(vllm_omni:requests_success_total[{w}]))",
        # vLLM's own abort/error count, or (servers without it, e.g. OmniVoice) the share of 5xx answers
        "failed_pct": f'100 * sum by (model) (rate(vllm:request_success_total{{finished_reason=~"abort|error"}}[{w}])) / sum by (model) (rate(vllm:request_success_total[{w}]))'
                      f' or 100 * (sum by (model) (rate(http_requests_total{{handler=~"/v1/.+",handler!~"/v1/models|/v1/audio/voices",status="5xx"}}[{w}])) or sum by (model) (rate(http_requests_total{{handler=~"/v1/.+",handler!~"/v1/models|/v1/audio/voices"}}[{w}])) * 0)'
                      f' / sum by (model) (rate(http_requests_total{{handler=~"/v1/.+",handler!~"/v1/models|/v1/audio/voices"}}[{w}]))' + " or " + f"sum by (model) (increase(sglang:num_requests_total[{w}]))" + " or " + f"100 * sum by (model) (rate(sglang:num_aborted_requests_total[{w}])) / sum by (model) (rate(sglang:num_requests_total[{w}]))",
        "ttfa_p95": f"histogram_quantile(0.95, sum by (le, model) (rate(vllm_omni:audio_ttfp_s_bucket[{w}])))",
        "ttfa_p99": f"histogram_quantile(0.99, sum by (le, model) (rate(vllm_omni:audio_ttfp_s_bucket[{w}])))",
        # real-time factor: TTS from vLLM-Omni, ASR measured by the gateway
        "rtf_p50": f"histogram_quantile(0.5, sum by (le, model) (rate(vllm_omni:audio_rtf_bucket[{w}]))) or "
                   f"histogram_quantile(0.5, sum by (le, model) (rate(aiops_gateway_asr_rtf_bucket[{w}])))",
        "ttft_p95": hq("time_to_first_token_seconds") + " or " + f"histogram_quantile(0.95, sum by (le, model) (rate(sglang:time_to_first_token_seconds_bucket[{w}])))",
        "ttft_p99": hq("time_to_first_token_seconds", 0.99) + " or " + f"histogram_quantile(0.99, sum by (le, model) (rate(sglang:time_to_first_token_seconds_bucket[{w}])))",
        "e2e_p99": f"{hq('e2e_request_latency_seconds', 0.99)} or histogram_quantile(0.99, sum by (le, model) (rate(vllm_omni:e2e_request_latency_s_bucket[{w}])))" + " or " + f"histogram_quantile(0.99, sum by (le, model) (rate(sglang:e2e_request_latency_seconds_bucket[{w}])))",
        "tpot_p95": hq("request_time_per_output_token_seconds") + " or " + f"histogram_quantile(0.95, sum by (le, model) (rate(sglang:inter_token_latency_seconds_bucket[{w}])))",
        "queue_p95": hq("request_queue_time_seconds") + " or " + f"histogram_quantile(0.95, sum by (le, model) (rate(sglang:queue_time_seconds_bucket[{w}])))",
        "e2e_p95": f"{hq('e2e_request_latency_seconds')} or histogram_quantile(0.95, sum by (le, model) (rate(vllm_omni:e2e_request_latency_s_bucket[{w}])))" + " or " + f"histogram_quantile(0.95, sum by (le, model) (rate(sglang:e2e_request_latency_seconds_bucket[{w}])))",
        "prefix_hit_pct": f"100 * sum by (model) (rate(vllm:prefix_cache_hits_total[{w}])) / sum by (model) (rate(vllm:prefix_cache_queries_total[{w}]))" + " or " + "100 * max by (model) (sglang:cache_hit_rate)",
        "preemptions": f"sum by (model) (increase(vllm:num_preemptions_total[{w}]))",
        "avg_prompt": f"sum by (model) (rate(vllm:request_prompt_tokens_sum[{w}])) / sum by (model) (rate(vllm:request_prompt_tokens_count[{w}]))",
        "kv_pct": "100 * max by (model) (vllm:kv_cache_usage_perc)" + " or " + "100 * max by (model) (sglang:token_usage)",
        "running": "sum by (model) (vllm:num_requests_running) or sum by (model) (vllm_omni:num_requests_running)" + " or " + "sum by (model) (sglang:num_running_reqs)",
        "waiting": "sum by (model) (vllm:num_requests_waiting) or sum by (model) (vllm_omni:num_requests_waiting)" + " or " + "sum by (model) (sglang:num_queue_reqs)",
    }
    who = _instant_rows("max by (model, node, kind, instance, via) (vllm:num_requests_running) or "
                        "max by (model, node, kind, instance, via) (vllm_omni:num_requests_running) or "
                        "max by (model, node, kind, instance, via) (sglang:num_running_reqs)")
    vals = dict(zip(cols, _pool.map(lambda e: _instant(e) or {}, cols.values())))
    order = {"LLM": 0, "VLM": 1, "OCR": 2, "Embed": 3, "Rerank": 4, "ASR": 5, "TTS": 6}
    rows = []
    for m in who:
        name = _label(m.get("model", ""))
        rows.append({"model": name, "node": _label(m.get("node", "")), "kind": m.get("kind", ""), "relayed": m.get("via") == "relay",
                     **{k: vals[k].get(name) for k in cols}})
    return sorted(rows, key=lambda r: (order.get(r["kind"], 9), -(r["rpm"] or 0), r["model"]))


def _instant_rows(expr: str) -> list[dict]:
    try:
        import httpx
        r = httpx.get(f"{config.PROMETHEUS_URL}/api/v1/query", params={"query": expr}, timeout=8)
        r.raise_for_status()
        return [x["metric"] for x in r.json()["data"]["result"]]
    except Exception:
        return []


def _pin_colours(panels: list[dict]) -> None:
    """A model keeps one colour on every chart of its section: slots follow a stable (alphabetical) order of the
    models seen in the section, not each chart's own busiest-first ranking."""
    seen: dict[str, set[str]] = {}
    for p in panels:
        if p["by"] == "model":
            seen.setdefault(p["section"], set()).update(x["name"] for x in p["series"] if x["name"] != "Other")
    order = {sec: {n: i for i, n in enumerate(sorted(names, key=str.lower))} for sec, names in seen.items()}
    for p in panels:
        if p["by"] != "model":
            continue
        rank = order[p["section"]]
        named = sorted((x for x in p["series"] if x["name"] != "Other"), key=lambda x: rank[x["name"]])
        used: set[int] = set()
        for x in named:              # a section with more models than slots: keep the pinned colour where free,
            want = rank[x["name"]] % MAX_SERIES      # never let two series of one chart share a colour
            x["slot"] = want if want not in used else next(s for s in range(MAX_SERIES) if s not in used)
            used.add(x["slot"])


def board(name: str, range_: str) -> dict:
    if name not in BOARDS or range_ not in RANGES:
        raise KeyError(name)
    hit = _cache.get((name, range_))
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    b = BOARDS[name]
    span, step = RANGES[range_]
    end = time.time() // step * step
    start = end - span
    panels = list(_pool.map(lambda p: _panel(p, start, end, step), b["panels"]))
    _pin_colours(panels)
    stats = list(_pool.map(_stat, b["stats"]))
    out = {"board": name, "title": b["title"], "range": range_, "step": step, "start": start, "end": end,
           "stats": stats, "panels": panels, "boards": {k: v["title"] for k, v in BOARDS.items()}}
    _cache[(name, range_)] = (time.time(), out)
    return out


# ---------------------------------------------------------------- one model, in depth
MODEL_RE = re.compile(r"^[A-Za-z0-9._/:@-]{1,120}$")


def models() -> list[dict]:
    """Every model server Prometheus currently sees: raw id, display name, node, kind."""
    # last 24 h, not just now: a model that is down is exactly when someone opens its page
    who = _instant_rows("max by (model, node, kind) (max_over_time(vllm:num_requests_running[24h])) or "
                        "max by (model, node, kind) (max_over_time(vllm_omni:num_requests_running[24h])) or "
                        "max by (model, node, kind) (max_over_time(sglang:num_running_reqs[24h]))")
    out = {(m.get("model", ""), _label(m.get("node", ""))): {"id": m.get("model", ""), "name": _label(m.get("model", "")),
                                                    "node": _label(m.get("node", "")), "kind": m.get("kind", "")}
           for m in who if m.get("model")}
    # engine of each server (exporter), and servers with no Prometheus endpoint of their own (Ollama)
    for m in _instant_rows("max by (model, node, kind, engine) (max_over_time(aiops_model_engine_info[24h]))"):
        key = (m.get("model", ""), _label(m.get("node", "")))
        if key in out:
            out[key]["engine"] = m.get("engine", "vllm")
        elif m.get("model") and m.get("engine") == "ollama":
            out[key] = {"id": m["model"], "name": _label(m["model"]), "node": _label(m.get("node", "")),
                        "kind": m.get("kind", ""), "engine": "ollama"}
    order = {"LLM": 0, "VLM": 1, "OCR": 2, "Embed": 3, "Rerank": 4, "ASR": 5, "TTS": 6}
    return sorted(out.values(), key=lambda m: (order.get(m["kind"], 9), m["name"]))


def _quantiles(metric: str, sel: str, w: str = "5m") -> str:
    """p50, p95 and p99 of one histogram as three series labelled q."""
    return " or ".join(f'label_replace(histogram_quantile({q}, sum by (le) (rate({metric}_bucket{{{sel}}}[{w}]))), "q", "{n}", "", "")'
                       for q, n in ((0.5, "p50"), (0.95, "p95"), (0.99, "p99")))


def _either(vllm_metric: str, sglang_metric: str, sel: str) -> str:
    """p50/p95/p99 from vLLM's histogram, or SGLang's when the model is served by SGLang."""
    return f"({_quantiles('vllm:' + vllm_metric, sel)}) or ({_quantiles('sglang:' + sglang_metric, sel)})"


def _model_panels(kind: str, sel: str) -> list[Panel]:
    L = "Latency"
    http = (f'handler!~"/health|/metrics|/v1/models|/ping|/version|/load|/tokenize|/detokenize|/v1/audio/voices",{sel}')
    common_tail = [
        Panel("http_err", "HTTP errors", f'100 * (sum(rate(http_requests_total{{{http},status=~"4xx|5xx"}}[15m])) or vector(0)) / '
              f"sum(rate(http_requests_total{{{http}}}[15m]))", "percent", by=None, max=100, warn=2, warn_label="2%",
              desc="Share of requests answered 4xx or 5xx.", section="Reliability"),
        Panel("load", "Requests in progress and waiting",
              f'label_replace(sum({{__name__=~"vllm:num_requests_running|sglang:num_running_reqs",{sel}}}), "q", "running", "", "") or '
              f'label_replace(sum({{__name__=~"vllm:num_requests_waiting|sglang:num_queue_reqs",{sel}}}), "q", "waiting", "", "")', "short", by="q", section="Reliability"),
    ]
    rpm = Panel("rpm", "Requests / min", f"sum(rate(vllm:request_success_total{{{sel}}}[5m])) * 60 or "
                f"sum(rate(vllm_omni:requests_success_total{{{sel}}}[5m])) * 60 or "
                f"sum(rate(sglang:num_requests_total{{{sel}}}[5m])) * 60", "rpm", by=None, section="Traffic")
    if kind in ("LLM", "VLM", "OCR"):
        return [
            rpm,
            Panel("tps", "Tokens / s", f'label_replace(sum(rate({{__name__=~"vllm:prompt_tokens_total|sglang:prompt_tokens_total",{sel}}}[2m])), "q", "prompt (read)", "", "") or '
                  f'label_replace(sum(rate({{__name__=~"vllm:generation_tokens_total|sglang:generation_tokens_total",{sel}}}[2m])), "q", "generated", "", "")', "tps", by="q", section="Traffic"),
            Panel("ttft", "Time to first token", _either("time_to_first_token_seconds", "time_to_first_token_seconds", sel), "s", by="q", section=L),
            Panel("itl", "Inter-token latency", _either("inter_token_latency_seconds", "inter_token_latency_seconds", sel), "s", by="q", section=L),
            Panel("e2e", "Whole request", _either("e2e_request_latency_seconds", "e2e_request_latency_seconds", sel), "s", by="q", section=L),
            Panel("queue", "Queue wait", _either("request_queue_time_seconds", "queue_time_seconds", sel), "s", by="q", section=L),
            Panel("kv", "KV cache used", f"100 * max(vllm:kv_cache_usage_perc{{{sel}}}) or 100 * max(sglang:token_usage{{{sel}}})", "percent", by=None, max=100, warn=90,
                  warn_label="90%", section="Memory and cache"),
            Panel("prefix", "Prompt cache hit rate", f"100 * sum(rate(vllm:prefix_cache_hits_total{{{sel}}}[15m])) / "
                  f"sum(rate(vllm:prefix_cache_queries_total{{{sel}}}[15m])) or 100 * max(sglang:cache_hit_rate{{{sel}}})", "percent", by=None, max=100, section="Memory and cache"),
            Panel("preempt", "Preemptions / min", f"sum(rate(vllm:num_preemptions_total{{{sel}}}[5m])) * 60", "rpm", by=None,
                  desc="Requests evicted from the KV cache and recomputed: memory pressure.", section="Memory and cache"),
            Panel("aborted", "Aborted / errored requests", f'sum(increase(vllm:request_success_total{{{sel},finished_reason=~"abort|error"}}[15m]))',
                  "short", by=None, section="Reliability"),
            *common_tail,
        ]
    if kind in ("Embed", "Rerank"):
        return [rpm,
                Panel("e2e", "Whole request", _quantiles("vllm:e2e_request_latency_seconds", sel), "s", by="q", section=L),
                Panel("queue", "Queue wait", _quantiles("vllm:request_queue_time_seconds", sel), "s", by="q", section=L),
                Panel("ptps", "Tokens read / s", f"sum(rate(vllm:prompt_tokens_total{{{sel}}}[2m]))", "tps", by=None, section="Traffic"),
                *common_tail]
    if kind == "ASR":
        return [rpm,
                Panel("audio_in", "Audio transcribed", f"sum(rate(aiops_gateway_audio_input_seconds_total{{{sel}}}[5m])) * 60", "short",
                      by=None, desc="Seconds of audio per minute (requests through the gateway).", section="Traffic"),
                Panel("e2e", "Transcription latency", _quantiles("vllm:e2e_request_latency_seconds", sel), "s", by="q", section=L),
                Panel("ttft", "Time to first text", _quantiles("vllm:time_to_first_token_seconds", sel), "s", by="q", section=L),
                Panel("rtf", "Real-time factor", _quantiles("aiops_gateway_asr_rtf", sel, "15m"), "x", by="q", warn=1,
                      warn_label="slower than real time",
                      desc="Processing time / audio length, measured by the gateway from each response's audio duration.", section=L),
                *common_tail]
    if kind == "TTS":
        return [rpm,
                Panel("audio", "Audio generated", f"sum(rate(vllm_omni:audio_duration_s_sum{{{sel}}}[5m])) * 60", "short", by=None,
                      desc="Seconds of speech per minute.", section="Traffic"),
                Panel("ttfa", "Time to first audio", _quantiles("vllm_omni:audio_ttfp_s", sel), "s", by="q", warn=1, warn_label="1 s",
                      desc="Streamed requests only.", section=L),
                Panel("e2e", "Whole request", _quantiles("vllm_omni:e2e_request_latency_s", sel), "s", by="q", section=L),
                Panel("rtf", "Real-time factor", _quantiles("vllm_omni:audio_rtf", sel, "15m"), "x", by="q", warn=1,
                      warn_label="slower than real time", section=L),
                Panel("silent", "Empty-audio responses", f"sum(increase(vllm_omni:audio_skipped_requests_total{{{sel}}}[15m])) or "
                      f"sum(vllm_omni:audio_rtf_count{{{sel}}}) * 0", "short", by=None, warn=0.5, warn_label="any", section="Reliability"),
                *common_tail]
    return [rpm, *common_tail]


def _ollama_panels(sel: str) -> list[Panel]:
    """Ollama exports no metrics: show whether the model is in memory, and traffic measured by the gateway."""
    return [
        Panel("loaded", "Loaded in memory", f"max(aiops_model_ready{{{sel}}})", "short", by=None, max=1, kind="timeline",
              desc="Ollama loads a model on its first request and unloads it after 5 idle minutes.", section="Availability"),
        Panel("gw_rpm", "Requests / min (through the gateway)", f"sum(rate(aiops_gateway_requests_total{{{sel}}}[5m])) * 60",
              "rpm", by=None, section="Traffic"),
        Panel("gw_lat", "Latency (through the gateway)",
              f'label_replace(max(aiops_gateway_latency_seconds{{{sel},quantile="0.5"}}), "q", "p50", "", "") or '
              f'label_replace(max(aiops_gateway_latency_seconds{{{sel},quantile="0.95"}}), "q", "p95", "", "") or '
              f'label_replace(max(aiops_gateway_latency_seconds{{{sel},quantile="0.99"}}), "q", "p99", "", "")', "s", by="q",
              section="Latency"),
        Panel("gw_err", "Gateway errors / min", f"sum(rate(aiops_gateway_errors_total{{{sel}}}[5m])) * 60", "rpm", by=None,
              section="Reliability"),
    ]


def model_board(model: str, range_: str) -> dict:
    if range_ not in RANGES or not MODEL_RE.match(model):
        raise KeyError(model)
    hit = _cache.get(("model", model, range_))
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    known = models()
    m = next((x for x in known if model in (x["id"], x["name"])), None)
    if not m:
        raise KeyError(model)
    sel = f'model="{m["id"]}"'
    span, step = RANGES[range_]
    end = time.time() // step * step
    start = end - span
    specs = _ollama_panels(sel) if m.get("engine") == "ollama" else _model_panels(m["kind"], sel)
    panels = list(_pool.map(lambda p: _panel(p, start, end, step), specs))
    row = next((r for r in _scorecard(range_) if r["model"] == m["name"]), None)
    from backend.core import store
    incidents = [i for i in store.list_incidents(2000)
                 if m["id"] in (i.get("resource") or "") or m["id"] in (i.get("title") or "")
                 or m["name"] in (i.get("title") or "")][:15]
    out = {"model": m, "models": known, "range": range_, "step": step, "start": start, "end": end, "summary": row,
           "panels": panels, "incidents": [{k: i.get(k) for k in ("id", "kind", "status", "title", "created")} for i in incidents]}
    _cache[("model", model, range_)] = (time.time(), out)
    return out


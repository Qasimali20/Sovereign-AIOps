"""Deterministic detection. No LLM here: code raises an alert, then the agent investigates.

Each scan gathers, for every node in parallel, the Spark Monitor metrics and the
GPU driver events from the kernel journal; the rules below are pure functions
over that data so they can be tested without a fleet.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from backend.core import config
from backend.monitoring import hostio, kernel
from backend.incidents.tools import ToolError, check_endpoint_health, inspect_container, monitor_metrics

_pool = ThreadPoolExecutor(max_workers=2 * len(config.NODES), thread_name_prefix="detect")


@dataclass
class Alert:
    kind: str
    node: str
    resource: str
    title: str
    detail: dict

    @property
    def fingerprint(self) -> str:
        return f"{self.kind}:{self.node}:{self.resource}"


@dataclass
class DetectorState:
    fail_counts: dict[str, int] = field(default_factory=dict)
    seen_models: dict[str, set[str]] = field(default_factory=dict)   # node -> {"name@port"} seen ready
    kernel_cursor: dict[str, float] = field(default_factory=dict)    # node -> last kernel timestamp read
    kernel_coverage: dict[str, dict] = field(default_factory=dict)   # node -> {access, ok, error, checked}
    uptime: dict[str, float] = field(default_factory=dict)           # node -> uptime_s at the last good read
    present: dict[str, set[str]] = field(default_factory=dict)       # node -> {"name@port"} ready right now
    missing: dict[str, set[str]] = field(default_factory=dict)       # node -> {"name@port"} gone right now
    reachable: dict[str, bool] = field(default_factory=dict)         # node -> monitor answered this scan
    streak: dict[str, int] = field(default_factory=dict)             # "rule:node[:res]" -> consecutive scans true
    progress: dict[str, dict] = field(default_factory=dict)          # "node:name@port" -> completions + resting gauges
    io: dict[str, dict] = field(default_factory=dict)                # node -> last good disk/network sample
    io_rates: dict[str, dict] = field(default_factory=dict)          # node -> rates between the last two samples
    io_ok: dict[str, bool] = field(default_factory=dict)             # node -> disk/network sample read this scan
    io_cursor: dict[str, float] = field(default_factory=dict)        # node -> last storage/network kernel message read
    links_up: dict[str, set[str]] = field(default_factory=dict)      # node -> physical interfaces seen up


# ---------------------------------------------------------------- pure rules
def metric_alerts(node: config.Node, d: dict, src: str, now: float) -> list[Alert]:
    out: list[Alert] = []
    mem = d.get("memory", {})
    if mem.get("total"):
        pct = 100 * mem["used"] / mem["total"]
        if pct >= config.MEM_PCT_WARN:
            out.append(Alert("high_memory", node.name, node.host, f"{node.name} memory at {pct:.0f}%",
                             {"mem_pct": round(pct, 1), "source": src}))
    disk = d.get("disk", {})
    if disk.get("total"):
        pct = 100 * disk["used"] / disk["total"]
        if pct >= config.DISK_PCT_WARN:
            out.append(Alert("disk_usage", node.name, node.host, f"{node.name} disk at {pct:.0f}%",
                             {"disk_pct": round(pct, 1), "source": src}))
    gpu = d.get("gpu")
    if gpu is None:
        out.append(Alert("gpu_unavailable", node.name, node.host, f"{node.name} GPU not reporting", {"source": src}))
    elif (gpu.get("temp") or 0) >= config.GPU_TEMP_WARN:
        out.append(Alert("gpu_temperature", node.name, node.host, f"{node.name} GPU at {gpu['temp']}C",
                         {"gpu": gpu, "source": src}))
    for s in d.get("vllm", []):
        key = f'{s["name"]}@{s["port"]}'
        if s.get("status") == "error":         # the engine process runs but its API does not answer (e.g. Ollama)
            out.append(Alert("model_engine_down", node.name, key,
                             f"{s.get('engine', 'model')} server on {node.name}:{s['port']} is running but not answering",
                             {"server": s, "source": src}))
        if s.get("status") == "loading" and s.get("started") and now - s["started"] > config.MODEL_LOADING_MAX_S:
            age = now - s["started"]
            out.append(Alert("model_load_stalled", node.name, key,
                             f"{s['name']} on {node.name} still loading after {age / 60:.0f} min",
                             {"server": s, "loading_s": round(age), "source": src}))
    return out


def sustained(streak: dict[str, int], key: str, cond: bool) -> bool:
    """Count consecutive scans `cond` held; true once it has held SUSTAIN_SCANS times."""
    streak[key] = streak.get(key, 0) + 1 if cond else 0
    return streak[key] >= config.SUSTAIN_SCANS


def stuck_requests(progress: dict[str, dict], node: str, s: dict) -> tuple[float, float]:
    """(running, waiting) a server reports beyond its resting level, counted only while it completes nothing.

    The engine's queue gauges alone are not trusted: vLLM-Omni (two-stage TTS) leaves finished requests counted, so
    after busy periods it reports requests "running" and "waiting" that do not exist. A real hang shows as requests
    in flight while the completed-requests counter stands still. So: any scan where the counter advanced is progress
    (nothing is stuck) and records the gauges then as the resting level; while the counter stands still the resting
    level follows the gauges down, and only requests above it count. Requests that arrive at a hung server push the
    gauges above the resting level and are caught. Not caught: a request that hangs as the last one of a burst with
    no traffic after it. A server without a completed counter is judged on its gauges as before."""
    run, wait = s.get("running") or 0, s.get("waiting") or 0
    total = s.get("requests_total")
    if total is None:
        return run, wait
    key = f'{node}:{s["name"]}@{s["port"]}'
    p = progress.get(key)
    if p is None or total != p["total"]:                # first sight, completions, or a restart (counter reset)
        progress[key] = {"total": total, "run": run, "wait": wait}
        return 0, 0
    p["run"], p["wait"] = min(p["run"], run), min(p["wait"], wait)
    return run - p["run"], wait - p["wait"]


def _stuck_detail(s: dict, progress: dict[str, dict], node: str) -> dict:
    p = progress.get(f'{node}:{s["name"]}@{s["port"]}') or {}
    return {"name": s["name"], "port": s["port"], "running": s.get("running"), "waiting": s.get("waiting"),
            "requests_completed": s.get("requests_total"),
            "resting_level": {"running": p.get("run"), "waiting": p.get("wait")} if p else None,
            "note": "no request has completed during the alert window; counts above the resting level are stuck"}


def sustained_alerts(node: config.Node, d: dict, streak: dict[str, int], src: str,
                     progress: dict[str, dict] | None = None) -> list[Alert]:
    """Rules that must hold for SUSTAIN_SCANS consecutive scans. Updates `streak` and `progress` in place."""
    out: list[Alert] = []
    cpu = (d.get("cpu") or {}).get("percent")
    if sustained(streak, f"cpu:{node.name}", cpu is not None and cpu >= config.CPU_PCT_WARN):
        out.append(Alert("high_cpu", node.name, node.host, f"{node.name} CPU at {cpu:.0f}% for over 3 minutes",
                         {"cpu_percent": cpu, "threshold": config.CPU_PCT_WARN, "source": src}))
    gpu = d.get("gpu") or {}
    progress = {} if progress is None else progress
    excess = {f'{s["name"]}@{s["port"]}': stuck_requests(progress, node.name, s) for s in d.get("vllm", [])}
    busy = [s for s in d.get("vllm", []) if excess[f'{s["name"]}@{s["port"]}'][0] > 0]
    # Baseline deviation: a GPU at 0% while a model has requests in flight is the signature of a hung engine/GPU.
    idle = gpu.get("util") is not None and gpu["util"] == 0
    if sustained(streak, f"gpustall:{node.name}", bool(busy) and idle):
        out.append(Alert("gpu_stalled", node.name, node.host,
                         f"{node.name} GPU idle while {', '.join(s['name'] for s in busy)} has requests in flight",
                         {"gpu": gpu, "busy_servers": [_stuck_detail(s, progress, node.name) for s in busy],
                          "scans": streak[f"gpustall:{node.name}"], "source": src}))
    for s in d.get("vllm", []):
        key = f'{s["name"]}@{s["port"]}'
        run, wait = excess[key]
        if sustained(streak, f"qstuck:{node.name}:{key}", wait > 0 and run <= 0):
            out.append(Alert("model_queue_stuck", node.name, key,
                             f"{s['name']} on {node.name} has waiting requests but none running",
                             {"server": s, **_stuck_detail(s, progress, node.name), "source": src}))
    for s in d.get("vllm", []):
        p95 = s.get("ttft_p95")
        key = f'{s["name"]}@{s["port"]}'
        slow = s.get("kind") in config.LATENCY_KINDS and s.get("status") == "ready" \
            and (s.get("req_per_min") or 0) >= config.LATENCY_MIN_RPM and p95 is not None and p95 > config.TTFT_P95_WARN_S
        if sustained(streak, f"latency:{node.name}:{key}", slow):
            out.append(Alert("model_latency_high", node.name, key,
                             f"{s['name']} on {node.name}: first-token p95 {p95:.1f}s (limit {config.TTFT_P95_WARN_S:.0f}s)",
                             {"server": {k: s.get(k) for k in ("name", "port", "running", "waiting", "req_per_min",
                                                               "ttft_p50", "ttft_p95", "e2e_p95", "kv_cache")},
                              "threshold_s": config.TTFT_P95_WARN_S,
                              "min_req_per_min": config.LATENCY_MIN_RPM, "scans": streak[f"latency:{node.name}:{key}"],
                              "source": src}))
    return out


def missing_model_alerts(node: config.Node, d: dict, seen: set[str], src: str) -> list[Alert]:
    """Servers that were ready earlier and are gone now. Updates `seen` in place.
    Within REBOOT_GRACE_S of a boot, models are still starting: no alert yet; one still gone after that alerts."""
    ready = {f'{s["name"]}@{s["port"]}' for s in d.get("vllm", []) if s.get("status") == "ready"}
    present = {f'{s["name"]}@{s["port"]}' for s in d.get("vllm", [])}
    seen |= ready
    if (d.get("uptime_s") or 1e9) < config.REBOOT_GRACE_S:
        return []
    return [Alert("model_server_missing", node.name, gone, f"{gone} on {node.name} was serving and has disappeared",
                  {"source": src}) for gone in sorted(seen - present)]


def reboot_alert(node: config.Node, d: dict, prev_uptime: float | None, now: float) -> list[Alert]:
    """Uptime went backwards: the node restarted since the last good read (catches reboots too fast to miss polls)."""
    up = d.get("uptime_s")
    if up is None or prev_uptime is None or up >= prev_uptime:
        return []
    boot = round((now - up) / 60) * 60                       # stable across scans despite clock jitter
    return [Alert("node_rebooted", node.name, f"boot-{int(boot)}", f"{node.name} restarted ({up / 60:.0f} min ago)",
                  {"uptime_s": round(up), "previous_uptime_s": round(prev_uptime), "boot_time": boot})]


def kernel_alerts(node: config.Node, events: dict, loading: list[dict]) -> list[Alert]:
    """One alert per Xid code with its count, processes, meaning and operator guidance."""
    out = []
    for g in kernel.summarize(events["xids"]):
        out.append(Alert(
            "gpu_xid", node.name, f"xid-{g['code']}",
            f"GPU Xid {g['code']} on {node.name}: {g['meaning']}",
            {"xid": g["code"], "severity": g["severity"], "count": g["count"], "first": g["first"], "last": g["last"],
             "processes": g["processes"], "example": g["example"], "guidance": g["guidance"],
             "bad_register_reads": events["bad_register_reads"], "model_servers_loading": loading,
             "source": f"kernel journal ({events['access']})"}))
    return out


def io_alerts(node: config.Node, sample: dict, r: dict, streak: dict[str, int], links_up: set[str]) -> list[Alert]:
    """Disk and network rules over one sample and its rates. Updates `streak` and `links_up` in place."""
    out: list[Alert] = []
    n = node.name
    for dev, d in r["disks"].items():
        busy = d["busy_ratio"]
        key = f"diskbusy:{n}:{dev}"
        streak[key] = streak.get(key, 0) + 1 if busy is not None and busy >= config.DISK_BUSY_WARN else 0
        if streak[key] >= config.DISK_BUSY_SCANS:
            out.append(Alert("disk_io_saturated", n, dev, f"{dev} on {n} busy {busy:.0%} for over {streak[key] // 3} min",
                             {"device": dev, **{k: round(v, 3) for k, v in d.items() if v is not None},
                              "threshold": config.DISK_BUSY_WARN, "scans": streak[key]}))
    for nic, d in r["nics"].items():
        errs = (d["rx_errors"] or 0) + (d["tx_errors"] or 0)
        key = f"neterr:{n}:{nic}"
        streak[key] = streak.get(key, 0) + 1 if errs > 0 else 0
        if streak[key] >= config.NET_ERROR_SCANS:
            out.append(Alert("net_errors", n, nic, f"{nic} on {n} reporting receive/transmit errors",
                             {"interface": nic, "errors_last_scan": d, "totals": sample["nics"].get(nic),
                              "scans": streak[key]}))
    links = sample["links"]
    for nic, state in links.items():
        if state == "up":
            links_up.add(nic)
        elif nic in links_up and state in ("down", "lowerlayerdown", "dormant"):
            msgs = [k for k in sample["kernel"] if k["area"] == "net" and k["device"] == nic][-5:]
            out.append(Alert("net_link_down", n, nic, f"{nic} on {n} link is {state}",
                             {"interface": nic, "state": state, "role": _nic_role(nic), "kernel_messages": msgs}))
    for nic, w in sample["wifi"].items():
        sig = w.get("signal_avg_dbm", w.get("signal_dbm"))
        if sustained(streak, f"wifi:{n}:{nic}", sig is not None and sig <= config.WIFI_SIGNAL_WARN_DBM):
            out.append(Alert("net_wifi_degraded", n, nic, f"Wi-Fi on {n} weak: {sig:.0f} dBm",
                             {"interface": nic, "station": w, "threshold_dbm": config.WIFI_SIGNAL_WARN_DBM,
                              "retries_last_scan": r["wifi"].get(nic)}))
    return out


def _nic_role(nic: str) -> str:
    if nic.startswith("wl"):
        return "Wi-Fi: the node's office network and internet connection"
    if nic.startswith("enp1s0") or nic.startswith("enP2p1s0"):
        return "ConnectX port: direct node-to-node link"
    return "wired Ethernet"


def io_kernel_alerts(node: config.Node, events: list[dict]) -> list[Alert]:
    """Storage and NIC kernel messages: one alert per (problem, device), with every matching message."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for e in events:
        if e["what"] == "network link down":       # judged from link state (net_link_down), not from the message
            continue
        groups.setdefault((e["what"], e["device"] or e["area"]), []).append(e)
    out = []
    for (what, dev), es in groups.items():
        kind = "disk_error" if es[0]["area"] == "disk" else "net_nic_reset"
        out.append(Alert(kind, node.name, dev, f"{what} on {node.name} ({dev})",
                         {"problem": what, "device": dev, "count": len(es), "first": es[0]["t"], "last": es[-1]["t"],
                          "messages": [e["message"] for e in es[-10:]], "source": "kernel journal"}))
    return out


# ---------------------------------------------------------------- scan
CURSOR_OVERLAP_S = 60.0


def _gather(node: config.Node, state: DetectorState, now: float):
    try:
        metrics = monitor_metrics(node.name)
    except ToolError as e:
        metrics = e
    since = state.kernel_cursor.get(node.name, now - config.KERNEL_LOOKBACK_S)
    events = kernel.read_gpu_events(node, since)
    # journalctl --since has 1 s resolution: drop anything at or before the exact cursor
    events["xids"] = [x for x in events["xids"] if x["t"] > since]
    io = hostio.collect(node, state.io_cursor.get(node.name, now - config.KERNEL_LOOKBACK_S))
    return node, metrics, events, io


def scan(state: DetectorState) -> list[Alert]:
    now = time.time()
    alerts: list[Alert] = []
    for node, metrics, events, io in _pool.map(lambda n: _gather(n, state, now), config.NODES):
        state.io_ok[node.name] = io["ok"]
        if io["ok"]:
            state.io_rates[node.name] = hostio.rates(state.io.get(node.name), io)
            state.io[node.name] = io
            alerts += io_alerts(node, io, state.io_rates[node.name], state.streak, state.links_up.setdefault(node.name, set()))
            alerts += io_kernel_alerts(node, io["kernel"])
            newest = max((k["t"] for k in io["kernel"]), default=now - CURSOR_OVERLAP_S)
            state.io_cursor[node.name] = max(state.io_cursor.get(node.name, 0.0), newest)
        # kernel journal: advance the cursor only on a successful read, so nothing is skipped
        state.kernel_coverage[node.name] = {k: events[k] for k in ("access", "ok", "error", "checked")}
        loading: list[dict] = []
        if isinstance(metrics, ToolError):
            state.fail_counts[node.name] = state.fail_counts.get(node.name, 0) + 1
            state.reachable[node.name] = False
            if state.fail_counts[node.name] >= config.NODE_UNREACHABLE_AFTER:
                alerts.append(Alert("node_unreachable", node.name, node.host,
                                    f"{node.name} ({node.host}) monitor unreachable",
                                    {"error": str(metrics), "consecutive_failures": state.fail_counts[node.name]}))
        else:
            d, src = metrics
            state.fail_counts[node.name] = 0
            state.reachable[node.name] = True
            alerts += reboot_alert(node, d, state.uptime.get(node.name), now)
            state.uptime[node.name] = d.get("uptime_s") or 0
            alerts += metric_alerts(node, d, src, now)
            alerts += sustained_alerts(node, d, state.streak, src, state.progress)
            seen = state.seen_models.setdefault(node.name, set())
            alerts += missing_model_alerts(node, d, seen, src)
            state.present[node.name] = {f'{s["name"]}@{s["port"]}' for s in d.get("vllm", []) if s.get("status") == "ready"}
            state.missing[node.name] = seen - {f'{s["name"]}@{s["port"]}' for s in d.get("vllm", [])}
            loading = [{"name": s["name"], "port": s["port"], "started": s.get("started")}
                       for s in d.get("vllm", []) if s.get("status") != "ready"]
        if events["ok"]:
            alerts += kernel_alerts(node, events, loading)
            # advance past what was read; with nothing new, to just before this read (overlap absorbs node clock
            # skew). Never backwards, and never left pinned at startup (the window would grow on every scan).
            newest = max((x["t"] for x in events["xids"]), default=now - CURSOR_OVERLAP_S)
            state.kernel_cursor[node.name] = max(state.kernel_cursor.get(node.name, 0.0), newest)

    for rid, res in config.TEST_RESOURCES.items():
        h = check_endpoint_health(rid)
        if not h["data"]["ok"]:
            alerts.append(Alert("service_unhealthy", res.node, rid, f"Test service {rid} failing health check",
                                {"health": h["data"], "container": inspect_container(rid)["data"]}))
    return alerts

"""Prometheus exposition for the platform: fleet, models, services and the agent pipeline.

The exporter republishes what the platform already observes. Node and model figures come from the
Spark Monitor API (read-only; nothing is installed on the nodes, so Node-4 is covered too). Service
health comes from the detector's last scan and pipeline figures from the incident store, so a scrape
never fans out to the nodes more than once per TTL however many Prometheus servers scrape it.

`render` is pure over its inputs, so the exposition format is testable without a fleet."""
from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from backend.core import config
from backend.incidents import tools

TTL_S = 10.0
_pool = ThreadPoolExecutor(max_workers=len(config.NODES), thread_name_prefix="metrics")
_lock = threading.Lock()
_cache: tuple[float, dict] = (0.0, {})


def _raw(n: config.Node) -> tuple[str, dict | None]:
    try:
        return n.name, tools.monitor_metrics(n.name)[0]
    except tools.ToolError:
        return n.name, None


def fleet_raw() -> dict[str, dict | None]:
    """node -> raw Spark Monitor payload (None if unreachable); single-flight, cached for TTL_S."""
    global _cache
    with _lock:
        t, data = _cache
        if time.monotonic() - t >= TTL_S:
            data = dict(_pool.map(_raw, config.NODES))
            _cache = (time.monotonic(), data)
        return data


# ---------------------------------------------------------------- exposition
def _esc(v: object) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class _Out:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._declared: set[str] = set()

    def metric(self, name: str, typ: str, help_: str, value: float | int | None, **labels: object) -> None:
        if name not in self._declared:
            self._declared.add(name)
            self.lines += [f"# HELP {name} {help_}", f"# TYPE {name} {typ}"]
        if value is None:
            return                                   # unknown is absent, never a fake zero
        v = float(value)
        num = str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)     # exact: no %g rounding of byte counts
        lab = ",".join(f'{k}="{_esc(x)}"' for k, x in labels.items())
        self.lines.append(f"{name}{{{lab}}} {num}" if lab else f"{name} {num}")


def render(fleet: dict[str, dict | None], services: dict[str, dict], pipeline: dict, detector: dict,
           io: dict[str, dict] | None = None) -> str:
    o = _Out()
    g = "gauge"
    _render_io(o, io or {})
    for node, d in sorted(fleet.items()):
        o.metric("aiops_node_up", g, "Spark Monitor on the node answered (1) or not (0).", 1 if d else 0, node=node)
        if not d:
            continue
        cpu, mem, disk, net, gpu = (d.get(k) or {} for k in ("cpu", "memory", "disk", "net", "gpu"))
        o.metric("aiops_node_uptime_seconds", g, "Seconds since the node booted.", d.get("uptime_s"), node=node)
        o.metric("aiops_node_cpu_percent", g, "CPU utilisation, percent.", cpu.get("percent"), node=node)
        o.metric("aiops_node_cpu_temp_celsius", g, "CPU temperature.", cpu.get("temp"), node=node)
        o.metric("aiops_node_memory_used_bytes", g, "Memory in use (unified CPU+GPU memory on GB10).", mem.get("used"), node=node)
        o.metric("aiops_node_memory_total_bytes", g, "Total memory.", mem.get("total"), node=node)
        o.metric("aiops_node_disk_used_bytes", g, "Root filesystem bytes used.", disk.get("used"), node=node)
        o.metric("aiops_node_disk_total_bytes", g, "Root filesystem size.", disk.get("total"), node=node)
        o.metric("aiops_node_network_receive_bytes_per_second", g, "Network receive rate.", net.get("rx_bps"), node=node)
        o.metric("aiops_node_network_transmit_bytes_per_second", g, "Network transmit rate.", net.get("tx_bps"), node=node)
        o.metric("aiops_gpu_up", g, "GPU reporting through nvidia-smi (1) or not (0).", 1 if d.get("gpu") else 0, node=node)
        o.metric("aiops_gpu_utilization_percent", g, "GPU utilisation, percent.", gpu.get("util"), node=node)
        o.metric("aiops_gpu_temp_celsius", g, "GPU temperature.", gpu.get("temp"), node=node)
        o.metric("aiops_gpu_power_watts", g, "GPU power draw.", gpu.get("power"), node=node)
        o.metric("aiops_gpu_sm_clock_mhz", g, "GPU SM clock.", gpu.get("clock_sm"), node=node)
        for s in d.get("vllm") or []:
            lab = {"node": node, "model": s.get("name"), "kind": s.get("kind") or "", "port": s.get("port")}
            o.metric("aiops_model_ready", g, "Model server ready (1) or loading/other (0).", 1 if s.get("status") == "ready" else 0, **lab)
            o.metric("aiops_model_engine_info", g, "Serving engine of each model server (value always 1).", 1, **lab,
                     engine=s.get("engine") or "vllm", status=s.get("status") or "")
            o.metric("aiops_model_requests_running", g, "Requests being processed.", s.get("running"), **lab)
            o.metric("aiops_model_requests_waiting", g, "Requests queued.", s.get("waiting"), **lab)
            o.metric("aiops_model_kv_cache_usage_ratio", g, "KV cache usage, 0-1.", s.get("kv_cache"), **lab)
            o.metric("aiops_model_generation_tokens_per_second", g, "Output tokens per second (Spark Monitor window).", s.get("tokens_per_s"), **lab)
            o.metric("aiops_model_requests_per_minute", g, "Requests per minute (Spark Monitor window).", s.get("req_per_min"), **lab)
            o.metric("aiops_model_ttft_p95_seconds", g, "Time to first token, p95 (Spark Monitor window).", s.get("ttft_p95"), **lab)

    for node, snap in sorted(services.items()):
        o.metric("aiops_services_readable", g, "Service inventory read from the node (1) or not (0).", 1 if snap.get("ok") else 0, node=node)
        if not snap.get("ok"):
            continue
        o.metric("aiops_docker_visible", g, "The monitoring account can read Docker on the node.", 1 if snap.get("docker_ok") else 0, node=node)
        cs = snap.get("containers") or []
        for state in ("running", "exited", "restarting", "created", "paused", "dead"):
            o.metric("aiops_containers", g, "Containers by state.", sum(c["state"] == state for c in cs), node=node, state=state)
        o.metric("aiops_containers_unhealthy", g, "Containers failing their health check.", sum(c["health"] == "unhealthy" for c in cs), node=node)
        pu = snap.get("problem_units") or []
        o.metric("aiops_systemd_units_failed", g, "Failed or restarting systemd units, excluding known-benign.", sum(not u["known"] for u in pu), node=node)
        o.metric("aiops_systemd_app_units", g, "Running application systemd services.", len(snap.get("app_units") or []), node=node)

    for (domain, cat, status), n in sorted(pipeline["incidents"].items()):
        o.metric("aiops_incidents", g, "Incidents by domain, category and current status.", n, domain=domain, category=cat, status=status)
    o.metric("aiops_approvals_pending", g, "Actions waiting for a human decision.", pipeline["approvals_pending"])
    for decision, n in sorted(pipeline["policy"].items()):
        o.metric("aiops_policy_decisions_total", "counter", "Policy decisions on proposed actions.", n, decision=decision)
    for ok, n in sorted(pipeline["executions"].items()):
        o.metric("aiops_executions_total", "counter", "Actions executed by the restricted executor.", n, result=ok)
    for outcome, n in sorted(pipeline["verifications"].items()):
        o.metric("aiops_verifications_total", "counter", "Independent post-action verification outcomes.", n, outcome=outcome)
    o.metric("aiops_workflows_running", g, "Incident workflows currently running.", detector.get("workflows_running"))
    o.metric("aiops_detector_last_scan_timestamp_seconds", g, "Unix time of the last completed detector scan.", detector.get("last"))
    o.metric("aiops_detector_scans_total", "counter", "Detector scans since the platform started.", detector.get("scans"))
    if detector.get("audit_ok") is not None:
        o.metric("aiops_audit_chain_ok", g, "Audit hash chain verified intact (1) or broken (0).", 1 if detector["audit_ok"] else 0)
        o.metric("aiops_audit_events", g, "Audit events in the verified chain.", detector.get("audit_events"))
    ix = detector.get("rag_index")
    if ix:
        o.metric("aiops_rag_sections", g, "Runbook sections the agent can retrieve, by state.", ix["embedded"], state="embedded")
        o.metric("aiops_rag_sections", g, "Runbook sections the agent can retrieve, by state.", ix["searchable"] - ix["embedded"], state="not_embedded")
        o.metric("aiops_rag_embedding_ok", g, "The embedding model answered the runbook index (1) or retrieval falls back to keywords (0).",
                 1 if ix["embedding_ok"] else 0)
    o.metric("aiops_detector_scan_error", g, "The last detector scan raised an error (1) or not (0).", 1 if detector.get("error") else 0)
    return "\n".join(o.lines) + "\n"


def _render_io(o: _Out, io: dict[str, dict]) -> None:
    """Disk, network and Wi-Fi from the detector's own collector: {node: {"ok", "sample", "rates"}}."""
    g, c = "gauge", "counter"
    for node, x in sorted(io.items()):
        o.metric("aiops_io_collector_ok", g, "Disk/network counters read from the node on the last scan (1) or not (0).",
                 1 if x.get("ok") else 0, node=node)
        sample, r = x.get("sample") or {}, x.get("rates") or {}
        for dev, d in sorted((sample.get("disks") or {}).items()):
            o.metric("aiops_disk_read_bytes_total", c, "Bytes read from the block device.", d["read_bytes"], node=node, device=dev)
            o.metric("aiops_disk_written_bytes_total", c, "Bytes written to the block device.", d["write_bytes"], node=node, device=dev)
            o.metric("aiops_disk_io_time_seconds_total", c, "Time the device had I/O in flight.", d["io_ms"] / 1000, node=node, device=dev)
            rd = (r.get("disks") or {}).get(dev) or {}
            o.metric("aiops_disk_busy_ratio", g, "Share of the last scan interval with I/O in flight, 0-1.", rd.get("busy_ratio"), node=node, device=dev)
        for nic, d in sorted((sample.get("nics") or {}).items()):
            for direction in ("rx", "tx"):
                o.metric("aiops_network_errors_total", c, "Interface receive/transmit errors.", d[f"{direction}_errors"],
                         node=node, interface=nic, direction=direction)
                o.metric("aiops_network_dropped_total", c, "Interface receive/transmit drops.", d[f"{direction}_dropped"],
                         node=node, interface=nic, direction=direction)
        for nic, state in sorted((sample.get("links") or {}).items()):
            o.metric("aiops_network_link_up", g, "Physical interface link up (1) or not (0).", 1 if state == "up" else 0,
                     node=node, interface=nic, state=state)
        for nic, w in sorted((sample.get("wifi") or {}).items()):
            lab = {"node": node, "interface": nic}
            o.metric("aiops_wifi_signal_dbm", g, "Wi-Fi signal, averaged by the driver.", w.get("signal_avg_dbm", w.get("signal_dbm")), **lab)
            o.metric("aiops_wifi_tx_bitrate_mbps", g, "Wi-Fi transmit bit rate.", w.get("tx_mbps"), **lab)
            o.metric("aiops_wifi_rx_bitrate_mbps", g, "Wi-Fi receive bit rate.", w.get("rx_mbps"), **lab)
            o.metric("aiops_wifi_tx_retries_total", c, "Wi-Fi frames retransmitted.", w.get("tx_retries"), **lab)
            o.metric("aiops_wifi_tx_failed_total", c, "Wi-Fi frames that failed after all retries.", w.get("tx_failed"), **lab)
            o.metric("aiops_wifi_beacon_loss_total", c, "Wi-Fi beacons missed from the access point.", w.get("beacon_loss"), **lab)
            o.metric("aiops_wifi_connected_seconds", g, "Seconds since the node (re)associated with the access point.", w.get("connected_s"), **lab)


def _open(addr: str) -> bool:
    host, port = addr.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port)), timeout=0.8):
            return True
    except OSError:
        return False


def vllm_targets(fleet: dict[str, dict | None], reachable=_open) -> list[dict]:
    """Prometheus http_sd: every model server the monitors see.
    Reachable over the LAN: scraped directly on its node. Not reachable (Node-1 firewalls some model ports;
    Node-3's embed/rerank listen on localhost only): scraped through the platform's relay, which reads
    127.0.0.1:<port>/metrics on that node over its existing SSH connection. Nothing on the nodes changes."""
    # Ollama has no Prometheus endpoint; SGLang has one only when started with --enable-metrics
    cand = [(n, s, f"{n.lan_ip}:{s['port']}") for n in config.NODES
            for s in (fleet.get(n.name) or {}).get("vllm") or [] if s.get("port")
            and s.get("engine") != "ollama" and not (s.get("engine") == "sglang" and not s.get("metrics"))]
    ok = list(_pool.map(reachable, [a for _, _, a in cand]))
    out = []
    for (n, s, addr), up in zip(cand, ok):
        lab = {"node": n.name, "model": str(s.get("name")), "kind": str(s.get("kind") or ""), "engine": str(s.get("engine") or "vllm")}
        if up:
            out.append({"targets": [addr], "labels": lab})
        else:
            out.append({"targets": [f"{RELAY_HOST}:{config.API_PORT}"],
                        "labels": lab | {"__metrics_path__": f"/metrics/relay/{n.name}/{int(s['port'])}", "instance": addr, "via": "relay"}})
    return out


RELAY_HOST = next((n.lan_ip for n in config.NODES if n.name == "Node-2"), "127.0.0.1")
_relay_cache: dict[tuple[str, int], tuple[float, str]] = {}


def relay(node: str, port: int, fleet: dict[str, dict | None]) -> str | None:
    """Model-server metrics read on the node itself (127.0.0.1) over SSH. Only (node, port) pairs that the
    fleet monitors currently report as model servers are allowed; anything else returns None."""
    import time as _t
    from backend.monitoring import kernel
    n = config.NODE_BY_NAME.get(node)
    if not n or not any(int(s.get("port") or 0) == port for s in (fleet.get(node) or {}).get("vllm") or []):
        return None
    hit = _relay_cache.get((node, port))
    if hit and _t.time() - hit[0] < 5:
        return hit[1]
    r = kernel.run(kernel.access_for(n), f"curl -s -m 5 http://127.0.0.1:{int(port)}/metrics")
    if r.returncode != 0 or not r.stdout:
        return None
    _relay_cache[(node, port)] = (_t.time(), r.stdout)
    return r.stdout
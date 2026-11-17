"""Read-only evidence tools.

Every tool takes identifiers that are validated against server-side config
(node names, registered resources) - never a free-form URL, path or command
from the model. All results carry their source and retrieval time.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any

import httpx

from backend.core.config import MONITOR_PORT, NODE_BY_NAME, NODES, TEST_RESOURCES

MAX_LOG_LINES = 60
MAX_LOG_CHARS = 6000


class ToolError(Exception):
    pass


def _monitor_get(node_name: str, path: str, timeout: float = 4.0) -> tuple[dict, str]:
    node = NODE_BY_NAME.get(node_name)
    if not node:
        raise ToolError(f"unknown node {node_name!r}")
    last = None
    for ip in (node.lan_ip, node.ts_ip):
        url = f"http://{ip}:{MONITOR_PORT}{path}"
        try:
            r = httpx.get(url, timeout=timeout)
            r.raise_for_status()
            return r.json(), url
        except Exception as e:  # try the next link
            last = e
    raise ToolError(f"{node_name} monitor unreachable on LAN and Tailscale: {last}")


def monitor_metrics(node_name: str) -> tuple[dict, str]:
    """Spark Monitor's /metrics for a node, plus the model servers it cannot see (SGLang, Ollama) and an engine
    label on every server row. Every reader of model-server data goes through here."""
    from backend.monitoring import engines
    d, src = _monitor_get(node_name, "/metrics")
    return engines.merge(node_name, d), src


def _result(tool: str, source: str, data: Any) -> dict:
    return {"tool": tool, "source": source, "retrieved_at": time.time(), "data": data}


# ---------------------------------------------------------------- telemetry
SERVER_FIELDS = ("name", "engine", "mem_gb", "bind", "kind", "port", "status", "started", "mem_fraction", "running", "waiting",
                 "kv_cache", "tokens_per_s", "ttft_p95", "e2e_p95", "preemptions", "req_per_min")


def summarize_metrics(d: dict) -> dict:
    """Pure: Spark Monitor /metrics payload -> compact node summary."""
    mem, disk = d.get("memory", {}), d.get("disk", {})
    cpu = d.get("cpu", {})
    return {
        "hostname": d.get("hostname"),
        "uptime_h": round((d.get("uptime_s") or 0) / 3600, 2),
        "cpu_percent": cpu.get("percent"),
        "cpu_temp_c": cpu.get("temp"),
        "mem_used_gb": round(mem.get("used", 0) / 2**30, 1),
        "mem_total_gb": round(mem.get("total", 0) / 2**30, 1),
        "disk_used_pct": round(100 * disk.get("used", 0) / disk["total"], 1) if disk.get("total") else None,
        "disk_used_gb": round(disk.get("used", 0) / 1e9) if disk.get("total") else None,
        "disk_total_gb": round(disk["total"] / 1e9) if disk.get("total") else None,
        "net_rx_bps": (d.get("net") or {}).get("rx_bps"),
        "net_tx_bps": (d.get("net") or {}).get("tx_bps"),
        "gpu": d.get("gpu"),
        "gpu_apps": d.get("gpu_apps"),
        "model_servers": len(d.get("vllm", [])),
    }


def full_name(s: dict) -> str:
    """The model's complete name, from the weights it loaded: a served alias like "qwen-embed" says less than
    "qwen3-embedding-0.6b". The last part of the model path or Hugging Face id, lower case like the names served on
    Node-2; the served name if there is none."""
    src = (s.get("model") or s.get("name") or "").rstrip("/")
    return (src.rsplit("/", 1)[-1] or s.get("name") or "").lower()


def summarize_servers(d: dict) -> list[dict]:
    """Pure: Spark Monitor /metrics payload -> model server list."""
    return [{k: s.get(k) for k in SERVER_FIELDS} | {"full_name": full_name(s)} for s in d.get("vllm", [])]


def get_node_metrics(node: str) -> dict:
    d, src = monitor_metrics(node)
    return _result("get_node_metrics", src, summarize_metrics(d))


def get_model_servers(node: str) -> dict:
    d, src = monitor_metrics(node)
    return _result("get_model_servers", src, summarize_servers(d))


def get_node_history(node: str, range_: str = "15m") -> dict:
    if range_ not in ("15m", "1h", "24h"):
        raise ToolError("range must be 15m, 1h or 24h")
    d, src = _monitor_get(node, f"/history?range={range_}")
    pts = d.get("points", [])
    # [t, gpu_util, cpu_pct, mem_pct] -> keep it small: first, last, min/max per series
    def series(i):
        xs = [p[i] for p in pts if p[i] is not None]
        return {"min": min(xs), "max": max(xs), "last": xs[-1]} if xs else None
    data = {"range": range_, "samples": len(pts),
            "gpu_util": series(1), "cpu_pct": series(2), "mem_pct": series(3),
            "models": {k: {"points": len(v["points"])} for k, v in d.get("models", {}).items()}}
    return _result("get_node_history", src, data)


def fleet_snapshot() -> dict:
    out = {}
    for n in NODES:
        try:
            out[n.name] = get_node_metrics(n.name)["data"]
        except ToolError as e:
            out[n.name] = {"error": str(e)}
    return _result("fleet_snapshot", "spark-monitor:/metrics x4", out)


# ---------------------------------------------------------------- registered test resources
def _resource(resource_id: str):
    r = TEST_RESOURCES.get(resource_id)
    if not r:
        raise ToolError(f"{resource_id!r} is not a registered resource")
    return r


def check_endpoint_health(resource_id: str) -> dict:
    r = _resource(resource_id)
    t0 = time.time()
    try:
        resp = httpx.get(r.health_url, timeout=3)
        data = {"ok": resp.status_code == 200, "status_code": resp.status_code, "body": resp.text[:300],
                "latency_ms": round(1000 * (time.time() - t0), 1)}
    except Exception as e:
        data = {"ok": False, "error": type(e).__name__, "latency_ms": round(1000 * (time.time() - t0), 1)}
    return _result("check_endpoint_health", r.health_url, data)


def inspect_container(resource_id: str) -> dict:
    r = _resource(resource_id)
    out = subprocess.run(["docker", "inspect", r.container], capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        return _result("inspect_container", f"docker:{r.container}", {"exists": False})
    info = json.loads(out.stdout)[0]
    st = info.get("State", {})
    data = {"exists": True, "status": st.get("Status"), "running": st.get("Running"),
            "exit_code": st.get("ExitCode"), "oom_killed": st.get("OOMKilled"),
            "started_at": st.get("StartedAt"), "finished_at": st.get("FinishedAt"),
            "restart_count": info.get("RestartCount"), "image": info.get("Config", {}).get("Image"),
            "restart_policy": (info.get("HostConfig", {}).get("RestartPolicy") or {}).get("Name") or "no"}
    return _result("inspect_container", f"docker:{r.container}", data)


def get_container_logs(resource_id: str) -> dict:
    r = _resource(resource_id)
    out = subprocess.run(["docker", "logs", "--tail", str(MAX_LOG_LINES), "--timestamps", r.container],
                         capture_output=True, text=True, timeout=10)
    text = (out.stdout + out.stderr)[-MAX_LOG_CHARS:]
    return _result("get_container_logs", f"docker:{r.container}", {"lines": text.splitlines()})


def get_container_events(resource_id: str, since_minutes: int = 60) -> dict:
    """Container lifecycle events (start/kill/stop/die/oom...) recorded by the
    platform's event recorder: shows whether an exit came from an operator, the
    daemon, or the OOM killer."""
    from backend.core import store
    r = _resource(resource_id)
    evs = store.container_events(r.container, time.time() - since_minutes * 60)
    for e in evs:
        e["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.pop("t")))
    return _result("get_container_events", f"aiops-event-recorder:{r.container}",
                   {"events": evs, "note": "recorded since the recorder started"})


def get_gpu_kernel_events(node: str, since_minutes: int = 120) -> dict:
    """GPU driver (NVRM Xid) events from the node's kernel journal, current boot."""
    from backend.monitoring import kernel
    n = NODE_BY_NAME[node]
    # In replay mode (AIOPS_KERNEL_BOOT set to an earlier boot) read that whole boot.
    since = 0 if kernel.replay_boot() else time.time() - since_minutes * 60
    r = kernel.read_gpu_events(n, since)
    if not r["ok"]:
        raise ToolError(f"kernel journal not readable on {node} ({r['access']}): {r['error']}")
    groups = kernel.summarize(r["xids"])
    for g in groups:
        g["first"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(g["first"]))
        g["last"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(g["last"]))
    return _result("get_gpu_kernel_events", f"kernel-journal:{node} ({r['access']})",
                   {"scope": "current boot: the GPU has NOT been reset since any event listed here",
                    "window_minutes": since_minutes, "xid_groups": groups,
                    "bad_register_reads": r["bad_register_reads"]})


# ---------------------------------------------------------------- security evidence (SOC)
def get_auth_events_by_source(ip: str) -> dict:
    from backend.security import tools as st
    return _result("get_auth_events_by_source", f"security-events:src={ip}", st.auth_events_by_source(ip))


def get_auth_events_by_user(user: str) -> dict:
    from backend.security import tools as st
    return _result("get_auth_events_by_user", f"security-events:user={user}", st.auth_events_by_user(user))


def get_source_context(ip: str) -> dict:
    from backend.security import tools as st
    return _result("get_source_context", f"security-events:context={ip}", st.source_context(ip))


def get_endpoint_events(target: str = "LAB") -> dict:
    from backend.security import tools as st
    return _result("get_endpoint_events", "soc-lab:edr+netflow", st.endpoint_events())


def get_vulnerability_status(target: str) -> dict:
    from backend.security import tools as st
    return _result("get_vulnerability_status", f"{target}:dpkg+changelog", st.vulnerability_status(target))


def get_lab_state(target: str = "LAB") -> dict:
    from backend.security import tools as st
    return _result("get_lab_state", "soc-lab:state", st.lab_state())


def get_services(node: str) -> dict:
    """Containers and systemd units on the node, from the service monitor (read-only)."""
    from backend.monitoring import services
    snap = services.collect(NODE_BY_NAME[node])
    if not snap["ok"]:
        raise ToolError(f"service status not readable on {node}: {snap['error']}")
    return _result("get_services", f"service-monitor:{node}",
                   {"docker_visible": snap["docker_ok"], "containers": snap["containers"],
                    "problem_units": snap["problem_units"], "application_units": snap["app_units"]})


# ---------------------------------------------------------------- logs (Loki)
def _log_result(tool: str, node: str, incident: dict | None, **kw) -> dict:
    from backend.monitoring import logs
    start, end = logs.window_for(incident.get("created") if incident else None)
    try:
        data = logs.search(node, start, end, **kw)
    except logs.LogsUnavailable as e:
        # Raised, so the evidence record says "unavailable" - never an empty result that reads as "no errors".
        raise ToolError(f"log search unavailable ({e}); absence of log lines here is not evidence of health")
    data["node"] = node
    return _result(tool, f"loki:{logs.LOKI_URL} {data['query']}", data)


def get_node_logs(node: str, incident: dict | None = None) -> dict:
    """Warnings and errors from every service and container on a node, around the incident time."""
    return _log_result("get_node_logs", node, incident, problems_only=True, limit=60)


def get_resource_logs(resource_id: str, incident: dict | None = None) -> dict:
    """All log lines of a registered resource's container, around the incident time (history, not just the tail)."""
    r = _resource(resource_id)
    return _log_result("get_resource_logs", r.node, incident, service=r.container, limit=80)


def get_service_logs(target: str, incident: dict | None = None) -> dict:
    """All log lines of one systemd service or container on a node. Target 'Node-n:service'."""
    from backend.monitoring import logs
    node, _, service = target.partition(":")
    if node not in NODE_BY_NAME or not service:
        raise ToolError(f"{target!r} is not 'Node-n:service'")
    try:
        known = logs.services(node)
    except logs.LogsUnavailable as e:
        raise ToolError(f"log search unavailable ({e}); absence of log lines here is not evidence of health")
    if service not in known:
        raise ToolError(f"{service!r} has no logs on {node}; services with logs: {', '.join(known[:60])}")
    return _log_result("get_service_logs", node, incident, service=service, limit=80)


def get_metric_window(node: str, incident: dict | None = None) -> dict:
    """Prometheus history for a node around the incident time: GPU/CPU/memory and each model server's
    load, queue, latency (p95) and KV cache, summarised per series."""
    from backend.monitoring import logs, promql
    created = incident.get("created") if incident else None
    if created is not None and time.time() < created + 120:
        promql.wait_for_scrape(node, created + 30)   # let Prometheus catch up with the live detector
    start, end = logs.window_for(created)
    try:
        data = promql.incident_window(node, start, end)
    except ValueError as e:
        raise ToolError(str(e))
    if len(data["errors"]) == len(data["queries"]):
        raise ToolError(f"Prometheus unavailable ({data['errors'][0]}); missing metrics are not evidence of health")
    from backend.core.config import PROMETHEUS_URL
    return _result("get_metric_window", f"prometheus:{PROMETHEUS_URL}", data)


def _service_on_port(node: str, port: int) -> str | None:
    """Which systemd unit or container serves a port, from the process list (read-only, no root needed)."""
    from backend.monitoring import kernel
    acc = kernel.access_for(NODE_BY_NAME[node])
    cmd = (f"ps -eo unit=,args= | grep -E -- '--port[= ]{port}( |$)' | grep -v grep | head -1 | awk '{{print $1}}'; "
           "echo ---; docker ps --no-trunc --format '{{.ID}} {{.Names}}' 2>/dev/null")
    r = kernel.run(acc, cmd)
    if r is None or r.returncode != 0:
        return None
    unit, _, containers = r.stdout.partition("---")
    unit = unit.strip()
    m = re.match(r"docker-([0-9a-f]{12,})\.scope$", unit)
    if m:
        for row in containers.split("\n"):
            cid, _, name = row.strip().partition(" ")
            if cid and (cid.startswith(m.group(1)) or m.group(1).startswith(cid)):
                return name or None
        return None
    if unit.endswith(".service") and unit not in ("docker.service", "containerd.service", "user@1000.service"):
        return unit[: -len(".service")]
    return None


def get_model_logs(target: str, incident: dict | None = None) -> dict:
    """Logs of the model server on a port (the systemd unit or container that serves it). Target 'Node-n:port'."""
    from backend.monitoring import logs
    node, _, port = target.partition(":")
    if node not in NODE_BY_NAME or not port.isdigit() or not 0 < int(port) < 65536:
        raise ToolError(f"{target!r} is not 'Node-n:port'")
    service = _service_on_port(node, int(port))
    if not service:
        raise ToolError(f"no service found listening on {node}:{port}")
    out = _log_result("get_model_logs", node, incident, service=service, limit=80)
    out["data"]["service"] = service
    return out


LOG_TOOLS = {"get_model_logs", "get_node_logs", "get_resource_logs", "get_service_logs", "get_metric_window"}


# The menu the diagnosis agent may pick from when it asks for more evidence.
def get_test_deployment(resource_id: str) -> dict:
    """Release, rollback target, replica count and recent deployment history of the test service."""
    _resource(resource_id)
    from backend.incidents import testsvc
    return _result("get_test_deployment", f"docker labels + {testsvc.HISTORY.name}", testsvc.status())


READONLY_TOOLS = {
    "get_services": ("node", get_services),
    "get_auth_events_by_source": ("ip", get_auth_events_by_source),
    "get_auth_events_by_user": ("user", get_auth_events_by_user),
    "get_source_context": ("ip", get_source_context),
    "get_lab_state": ("lab", get_lab_state),
    "get_endpoint_events": ("lab", get_endpoint_events),
    "get_vulnerability_status": ("node_or_lab", get_vulnerability_status),
    "get_gpu_kernel_events": ("node", get_gpu_kernel_events),
    "get_container_events": ("resource", get_container_events),
    "get_node_metrics": ("node", get_node_metrics),
    "get_model_servers": ("node", get_model_servers),
    "get_node_history": ("node", get_node_history),
    "check_endpoint_health": ("resource", check_endpoint_health),
    "inspect_container": ("resource", inspect_container),
    "get_container_logs": ("resource", get_container_logs),
    "get_test_deployment": ("resource", get_test_deployment),
    "get_node_logs": ("node", get_node_logs),
    "get_metric_window": ("node", get_metric_window),
    "get_resource_logs": ("resource", get_resource_logs),
    "get_service_logs": ("node_service", get_service_logs),
    "get_model_logs": ("node_port", get_model_logs),
}


def run_readonly(tool: str, target: str, incident: dict | None = None) -> dict:
    """Run one allow-listed read-only tool. `incident` only scopes log searches to its time window."""
    if tool not in READONLY_TOOLS:
        raise ToolError(f"{tool!r} is not an allowed read-only tool")
    kind, fn = READONLY_TOOLS[tool]
    if kind == "node" and target not in NODE_BY_NAME:
        raise ToolError(f"{target!r} is not a known node")
    if kind == "node_or_lab" and target not in NODE_BY_NAME and target != "LAB":
        raise ToolError(f"{target!r} is not a known node or LAB")
    if kind == "resource" and target not in TEST_RESOURCES:
        raise ToolError(f"{target!r} is not a registered resource")
    if kind in ("ip", "user", "lab"):
        from backend.security import tools as st
        ok = {"ip": st.valid_ip, "user": st.valid_user, "lab": lambda v: v == "LAB"}[kind](target)
        if not ok:
            raise ToolError(f"{target!r} is not a valid {kind} target")
    if kind in ("node_service", "node_port") and target.partition(":")[0] not in NODE_BY_NAME:
        raise ToolError(f"{target!r} is not 'Node-n:service'")
    return fn(target, incident) if tool in LOG_TOOLS else fn(target)

"""Generate the Grafana dashboards (run after editing; the JSON is provisioned read-only).

Colour rules match the platform UI:
- nodes are categorical and always keep the same hue (Node-1 blue, Node-2 orange, Node-3 violet, Node-4 pink), the
  app's dark-mode chart palette, validated for colour-vision deficiency on the dark surface; series are never green;
- green is reserved for healthy/fixed outcomes; orange = waiting on a person; red = failed;
- single-metric panels use the metric identity colour (GPU blue, memory amber, CPU grey, disk teal);
- history recorded before the nodes were renamed (DGX-1..4) is shown under the new names (Node-1..4);
- one y-axis per panel; units are explicit.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "grafana" / "dashboards"
DS = {"type": "prometheus", "uid": "prom"}
NODE_COLOURS = {"Node-1": "#3d7bf5", "Node-2": "#ee6420", "Node-3": "#9670f2", "Node-4": "#ec3f88"}
GPU, MEM, CPU, DISK = "#3d7bf5", "#f5a524", "#8f9bb3", "#14b8a6"
GOOD, WARN, CRIT, GREY = "#3dd68c", "#f0a030", "#ef4444", "#7d8796"
FINAL = "resolved|recovered|closed|action_rejected"
# Fixed colours for categorical series (validated palette, no green). Anything else: shades of one blue.
CATEGORY_COLOURS = {"sre": "#3d7bf5", "security": "#ec3f88",
                    "resolved": GOOD, "recovered": "#3d7bf5", "awaiting_approval": WARN,
                    "needs_human": WARN, "diagnosed": WARN, "not_resolved": CRIT, "action_denied": CRIT,
                    "action_rejected": GREY, "closed": GREY, "investigating": "#9670f2", "open": "#9670f2"}


def merged_nodes(expr: str) -> str:
    """One series per node across the rename: DGX-n history and Node-n data become a single Node-n line.
    (A direct label_replace would collide while both names exist, so merge on a temporary label first.)"""
    return (f'label_replace(max by (n) (label_replace(({expr}), "n", "Node-$1", "node", "(?:DGX|Node)-(.*)")), '
            f'"node", "$1", "n", "(.*)")')


def _colour_overrides(by_node: bool) -> list[dict]:
    """Fixed colour per series. Node series match the old DGX-n label too and are shown as Node-n."""
    if by_node:
        return [{"matcher": {"id": "byRegexp", "options": f"^(DGX|Node)-{n[-1]}$"},
                 "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}, {"id": "displayName", "value": n}]}
                for n, c in NODE_COLOURS.items()]
    return [{"matcher": {"id": "byName", "options": n}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
            for n, c in CATEGORY_COLOURS.items()]


class Board:
    def __init__(self, uid: str, title: str, description: str, refresh: str = "30s", time_from: str = "now-6h"):
        self.d = {"uid": uid, "title": title, "description": description, "tags": ["sovereign-ai-ops"],
                  "timezone": "browser", "schemaVersion": 41, "version": 1, "editable": False,
                  "graphTooltip": 1,                                  # shared crosshair across panels
                  "refresh": refresh, "time": {"from": time_from, "to": "now"}, "panels": [],
                  "links": [{"title": "Sovereign AI Ops", "type": "link", "url": "http://NODE2_ADDR:5170/", "targetBlank": True},
                            {"title": "Dashboards", "type": "dashboards", "tags": ["sovereign-ai-ops"], "asDropdown": True}]}
        self.y = 0
        self.id = 1

    def _add(self, p: dict, w: int, h: int, x: int) -> None:
        p |= {"id": self.id, "gridPos": {"x": x, "y": self.y, "w": w, "h": h}, "datasource": DS}
        self.id += 1
        self.d["panels"].append(p)

    def row(self, title: str) -> None:
        self.d["panels"].append({"id": self.id, "type": "row", "title": title, "collapsed": False,
                                 "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": []})
        self.id += 1
        self.y += 1

    def stats(self, items: list[tuple[str, str, str, str]], h: int = 4) -> None:
        """items: (title, expr, unit, description). Values in text colour; thresholds only where they mean something."""
        w = 24 // len(items)
        for i, (title, expr, unit, desc) in enumerate(items):
            self._add({"type": "stat", "title": title, "description": desc,
                       "targets": [{"refId": "A", "expr": expr, "instant": True}],
                       "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "none", "graphMode": "none",
                                   "textMode": "value", "justifyMode": "auto", "wideLayout": True},
                       "fieldConfig": {"defaults": {"unit": unit, "decimals": 0 if unit in ("short", "none") else None,
                                                    "noValue": "0"}, "overrides": []}}, w, h, i * w)
        self.y += h

    def series(self, title: str, targets: list[tuple[str, str]], unit: str, *, w: int = 12, h: int = 8, x: int = 0,
               colour: str | None = None, by_node: bool = False, desc: str = "", max_: float | None = None,
               stack: bool = False, min_: float | None = 0) -> None:
        defaults = {"unit": unit, "min": min_, "custom": {"lineWidth": 2, "fillOpacity": 8 if not stack else 60,
                                                      "showPoints": "never", "spanNulls": 60_000,
                                                      "axisSoftMin": min_, "stacking": {"mode": "normal" if stack else "none"},
                                                      "gradientMode": "none"},
                    "color": {"mode": "fixed", "fixedColor": colour} if colour else {"mode": "shades", "fixedColor": "#3d7bf5"}}
        if max_ is not None:
            defaults["max"] = max_
        overrides = _colour_overrides(by_node)
        self._add({"type": "timeseries", "title": title, "description": desc,
                   "targets": [{"refId": chr(65 + i), "expr": merged_nodes(e) if by_node else e, "legendFormat": lf}
                               for i, (e, lf) in enumerate(targets)],
                   "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                               "tooltip": {"mode": "multi", "sort": "desc"}},
                   "fieldConfig": {"defaults": defaults, "overrides": overrides}}, w, h, x)

    def donut(self, title: str, expr: str, legend: str, *, w: int = 12, h: int = 8, x: int = 0, desc: str = "") -> None:
        """Ring chart of the composition right now (same as the app's Metric history)."""
        self._add({"type": "piechart", "title": title, "description": desc,
                   "targets": [{"refId": "A", "expr": expr, "instant": True, "legendFormat": legend}],
                   "options": {"pieType": "donut", "reduceOptions": {"calcs": ["lastNotNull"], "values": False},
                               "legend": {"displayMode": "table", "placement": "right", "showLegend": True, "values": ["value", "percent"]},
                               "tooltip": {"mode": "single"}, "displayLabels": []},
                   "fieldConfig": {"defaults": {"unit": "short", "decimals": 0}, "overrides": _colour_overrides(False)}}, w, h, x)

    def table(self, title: str, expr: str, columns: dict[str, str], w: int = 24, h: int = 8, desc: str = "") -> None:
        self._add({"type": "table", "title": title, "description": desc,
                   "targets": [{"refId": "A", "expr": expr, "instant": True, "format": "table"}],
                   "transformations": [{"id": "organize", "options": {
                       "excludeByName": {"Time": True, "__name__": True, "job": True, "instance": True, "Value": False},
                       "renameByName": columns}}],
                   "fieldConfig": {"defaults": {}, "overrides": []}, "options": {"showHeader": True}}, w, h, 0)
        self.y += h

    def next_row(self, h: int = 8) -> None:
        self.y += h

    def save(self, name: str) -> None:
        (OUT / name).write_text(json.dumps(self.d, indent=1) + "\n")


def fleet() -> Board:
    b = Board("aiops-fleet", "Fleet", "All four DGX Sparks: compute, memory, GPU, storage, network. Source: Spark Monitor via the platform exporter.")
    b.stats([
        ("Nodes reporting", "sum(aiops_node_up)", "short", "Nodes whose Spark Monitor answered the last scrape."),
        ("Models ready", "sum(aiops_model_ready)", "short", "Model servers ready across the fleet."),
        ("Containers running", "sum(aiops_containers{state=\"running\"})", "short", "Docker containers running across the four nodes."),
        ("Failed services", "sum(aiops_systemd_units_failed) + sum(aiops_containers_unhealthy)", "short", "Failed systemd units plus unhealthy containers."),
    ])
    b.row("Utilisation")
    b.series("CPU", [("aiops_node_cpu_percent", "{{node}}")], "percent", by_node=True, max_=100, x=0)
    b.series("Memory used", [("100 * aiops_node_memory_used_bytes / aiops_node_memory_total_bytes", "{{node}}")], "percent",
             by_node=True, max_=100, x=12, desc="GB10 has unified memory: model weights and KV cache count here. Detector warns at 92%.")
    b.next_row()
    b.series("GPU utilisation", [("aiops_gpu_utilization_percent", "{{node}}")], "percent", by_node=True, max_=100, x=0)
    b.series("GPU temperature", [("aiops_gpu_temp_celsius", "{{node}}")], "celsius", by_node=True, x=12)
    b.next_row()
    b.series("GPU power", [("aiops_gpu_power_watts", "{{node}}")], "watt", by_node=True, x=0)
    b.series("Disk used", [("100 * aiops_node_disk_used_bytes / aiops_node_disk_total_bytes", "{{node}}")], "percent",
             by_node=True, max_=100, x=12)
    b.next_row()
    b.row("Network")
    b.series("Receive", [("aiops_node_network_receive_bytes_per_second", "{{node}}")], "Bps", by_node=True, x=0)
    b.series("Transmit", [("aiops_node_network_transmit_bytes_per_second", "{{node}}")], "Bps", by_node=True, x=12)
    b.next_row()
    b.row("Storage and network health")
    b.series("Disk busy", [("100 * aiops_disk_busy_ratio", "{{node}}")], "percent", by_node=True, max_=100, x=0,
             desc="Share of time the NVMe drive had I/O in flight. Model loads and downloads saturate it for minutes; "
                  "the detector alerts at 95% held for 15 minutes.")
    b.series("Disk throughput (read + write)", [("rate(aiops_disk_read_bytes_total[2m]) + on(node, device) "
                                                  "rate(aiops_disk_written_bytes_total[2m])", "{{node}}")], "Bps", by_node=True, x=12)
    b.next_row()
    b.series("Wi-Fi signal", [("aiops_wifi_signal_dbm", "{{node}}")], "dBm", by_node=True, x=0, min_=None,
             desc="Every node reaches the office network over Wi-Fi. Normal is -60 to -68 dBm; the detector alerts at -75 dBm held for 3 minutes.")
    b.series("Wi-Fi retransmissions", [("rate(aiops_wifi_tx_retries_total[5m])", "{{node}}")], "short", by_node=True, x=12,
             desc="Frames sent again per second. A rise with a steady signal points to interference or a busy channel.")
    b.next_row()
    b.series("Interface errors (5 min)", [("sum by (node) (increase(aiops_network_errors_total[5m]))", "{{node}}")], "short",
             by_node=True, x=0, desc="Receive and transmit errors on physical interfaces (Wi-Fi and ConnectX). Normally zero.")
    b.series("Wi-Fi transmit bit rate", [("aiops_wifi_tx_bitrate_mbps", "{{node}}")], "Mbits", by_node=True, x=12)
    b.next_row()
    b.row("Availability")
    b.series("Node reporting (1 = up)", [("aiops_node_up", "{{node}}")], "short", by_node=True, x=0, max_=1)
    b.series("Uptime", [("aiops_node_uptime_seconds", "{{node}}")], "s", by_node=True, x=12,
             desc="A drop to zero is a reboot; the detector opens a 'restarted' incident.")
    b.next_row()
    return b


def models() -> Board:
    b = Board("aiops-models", "Model serving", "vLLM servers scraped directly (native histograms) plus the model gateway.", time_from="now-3h")
    b.stats([
        ("Requests / min (all models)", "sum(rate(vllm:request_success_total[5m])) * 60", "short", "Completed requests per minute across scraped vLLM servers."),
        ("Requests running", "sum(vllm:num_requests_running)", "short", ""),
        ("Requests queued", "sum(vllm:num_requests_waiting)", "short", "Queued and not yet running. Sustained queueing means a server is saturated or stuck."),
        ("Output tokens / s", "sum(rate(vllm:generation_tokens_total[2m]))", "short", ""),
    ])
    b.row("Latency (p95, 5 min window)")
    q = lambda m: f"histogram_quantile(0.95, sum by (le, model) (rate(vllm:{m}_bucket[5m])))"
    b.series("Time to first token", [(q("time_to_first_token_seconds"), "{{model}}")], "s", x=0)
    b.series("Inter-token latency", [(q("inter_token_latency_seconds"), "{{model}}")], "s", x=12)
    b.next_row()
    b.series("End-to-end request latency", [(q("e2e_request_latency_seconds"), "{{model}}")], "s", x=0)
    b.series("Queue time", [(q("request_queue_time_seconds"), "{{model}}")], "s", x=12)
    b.next_row()
    b.row("Load")
    b.series("Requests running", [("sum by (model) (vllm:num_requests_running)", "{{model}}")], "short", x=0)
    b.series("Requests queued", [("sum by (model) (vllm:num_requests_waiting)", "{{model}}")], "short", x=12)
    b.next_row()
    b.series("KV cache usage", [("100 * max by (model) (vllm:kv_cache_usage_perc)", "{{model}}")], "percent", max_=100, x=0)
    b.series("Output tokens / s", [("sum by (model) (rate(vllm:generation_tokens_total[2m]))", "{{model}}")], "short", x=12)
    b.next_row()
    b.row("Model gateway")
    b.series("Gateway requests / s", [("sum by (model) (rate(aiops_gateway_requests_total[5m]))", "{{model}}")], "reqps", x=0)
    b.series("Gateway errors / s", [("sum by (model) (rate(aiops_gateway_errors_total[5m]))", "{{model}}")], "reqps", x=12,
             desc="Upstream errors before the first byte are retried on another replica; these are the ones counted.")
    b.next_row()
    b.series("Gateway mean latency", [("sum by (model) (rate(aiops_gateway_latency_seconds_sum[5m])) / sum by (model) (rate(aiops_gateway_latency_seconds_count[5m]))", "{{model}}")], "s", x=0)
    b.series("Gateway in-flight", [("sum by (model) (aiops_gateway_inflight)", "{{model}}")], "short", x=12)
    b.next_row()
    b.table("Model servers", "aiops_model_ready", {"node": "Node", "model": "Model", "kind": "Kind", "port": "Port", "Value": "Ready (1 = yes)"},
            desc="Every server Spark Monitor sees, including ports the node firewalls from direct scraping.")
    return b


def operations() -> Board:
    b = Board("aiops-operations", "AI operations", "Incident pipeline: detection, agent investigation, approvals, execution and verification.", time_from="now-24h")
    b.stats([
        ("Open incidents", f"sum(aiops_incidents{{status!~\"{FINAL}\"}})", "short", "Incidents not yet resolved, recovered, closed or rejected."),
        ("Waiting for approval", "aiops_approvals_pending", "short", "Actions proposed by the agent that need a human decision."),
        ("Actions executed", "sum(aiops_executions_total)", "short", "All by the restricted executor, all after approval."),
        ("Verified fixed", "sum(aiops_verifications_total{outcome=\"resolved\"})", "short", "Recovery confirmed by independent health checks."),
    ])
    b.stats([
        ("Detector last scan", "time() - aiops_detector_last_scan_timestamp_seconds", "s", "Seconds since the detector completed a scan (normally under 30 s)."),
        ("Workflows running", "aiops_workflows_running", "short", "Agent investigations in progress."),
        ("Policy decisions", "sum(aiops_policy_decisions_total)", "short", "Every proposed action passes the deterministic policy engine."),
        ("Recovered on their own", "sum(aiops_incidents{status=\"recovered\"})", "short", "Incidents closed because the detector saw the condition clear."),
    ])
    b.row("Incidents")
    b.donut("Open incidents by domain", f"sum by (domain) (aiops_incidents{{status!~\"{FINAL}\"}}) > 0", "{{domain}}", x=0)
    b.donut("Incidents by outcome", "sum by (status) (aiops_incidents) > 0", "{{status}}", x=12)
    b.next_row()
    b.row("Services")
    b.series("Unhealthy containers + failed units", [("aiops_containers_unhealthy + on(node) aiops_systemd_units_failed", "{{node}}")], "short", by_node=True, x=0)
    b.series("Containers running", [("aiops_containers{state=\"running\"}", "{{node}}")], "short", by_node=True, x=12)
    b.next_row()
    b.series("Models ready per node", [("sum by (node) (aiops_model_ready)", "{{node}}")], "short", by_node=True, x=0)
    b.series("Detector scan age", [("time() - aiops_detector_last_scan_timestamp_seconds", "scan age")], "s", colour=CPU, x=12,
             desc="Prometheus alerts (AIOpsDetectorStale) if this exceeds 2 minutes: the watcher of the watcher.")
    b.next_row()
    return b


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for f, b in (("fleet.json", fleet()), ("models.json", models()), ("operations.json", operations())):
        b.save(f)
        print("wrote", f, len(b.d["panels"]), "panels")

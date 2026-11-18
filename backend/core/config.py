"""Static configuration: the fleet, model endpoints, and the registry of resources
the platform is allowed to act on.

Everything the agent may touch is declared here, server-side. The LLM never
supplies a resource it was not given from this file.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("AIOPS_DB", ROOT / "data" / "aiops.db"))

# The Spark Monitor agents (read-only source of telemetry). Port 8765 on each node.
# These belong to the existing Spark Monitor app; we only GET from them.
MONITOR_PORT = 8765


@dataclass(frozen=True)
class Node:
    name: str        # dashboard name
    host: str        # hostname
    lan_ip: str      # office LAN (Wi-Fi on all four nodes)
    ts_ip: str       # Tailscale fallback
    ts_name: str     # Tailscale MagicDNS name; SSH goes here first (Node-4 only accepts Tailscale SSH)


def _addresses(i: int) -> tuple[str, str]:
    """Node i's LAN and Tailscale addresses from AIOPS_NODE_<i>="lan,tailscale" in .env; the repository holds
    no fleet addresses. The defaults are placeholders, so tests run without .env."""
    lan, _, ts = os.environ.get(f"AIOPS_NODE_{i}", "").partition(",")
    return lan.strip() or f"10.0.0.{i}", ts.strip() or f"100.64.0.{i}"


NODES: list[Node] = [
    Node("Node-1", "spark-3369", *_addresses(1), "spark-1"),
    Node("Node-2", "spark-46ef", *_addresses(2), "spark-2"),
    Node("Node-3", "spark-214e", *_addresses(3), "spark-3"),
    Node("Node-4", "spark-b969", *_addresses(4), "spark-4"),
]
LAN_NET = os.environ.get("AIOPS_LAN_NET") or "10.0.0.0/24"   # the office LAN, allowed to reach the console
NODE_BY_NAME = {n.name: n for n in NODES}


@dataclass(frozen=True)
class LLMEndpoint:
    name: str
    base_url: str
    model: str
    api_key: str = ""


@dataclass(frozen=True)
class TestResource:
    """A resource registered as safe to act on. Only these can be targeted by
    write operations, and only with the operations listed."""
    resource_id: str
    resource_type: str
    node: str
    container: str
    health_url: str
    allowed_operations: tuple[str, ...] = field(default=("restart_test_service", "clear_test_cache",
                                                         "scale_test_service", "rollback_test_deployment"))


TEST_RESOURCES: dict[str, TestResource] = {
    "aiops-test-svc": TestResource(
        resource_id="aiops-test-svc",
        resource_type="test_service",
        node="Node-2",
        container="aiops-test-svc",
        health_url="http://127.0.0.1:18080/health",
    ),
}

# Detector thresholds
MEM_PCT_WARN = 92.0
DISK_PCT_WARN = 85.0
GPU_TEMP_WARN = 85.0
MODEL_LOADING_MAX_S = 15 * 60   # a vLLM load normally takes ~4 min; >15 min = hung
REBOOT_GRACE_S = 15 * 60        # after a boot, models get this long to come back before 'missing' alerts
KERNEL_LOOKBACK_S = 3600        # on first scan, report GPU driver events from the last hour
NODE_UNREACHABLE_AFTER = 3      # consecutive failed polls
CPU_PCT_WARN = 90.0
SUSTAIN_SCANS = 9               # ~3 min at 20 s: CPU / GPU-stall rules must hold this long (no paging on spikes)
DISK_BUSY_WARN = 0.95           # NVMe busy (time with I/O in flight) ratio...
DISK_BUSY_SCANS = 45            # ...held ~15 min: loading a 70 GB model saturates the disk for several minutes
NET_ERROR_SCANS = 3             # NIC receive/transmit errors rising on this many consecutive scans
WIFI_SIGNAL_WARN_DBM = -75.0    # averaged Wi-Fi signal at or below this, sustained (every node is on Wi-Fi)
PROMETHEUS_URL = os.environ.get("AIOPS_PROMETHEUS_URL", f"http://{NODE_BY_NAME['Node-3'].lan_ip}:9090")   # on Node-3
GRAFANA_URL = os.environ.get("AIOPS_GRAFANA_URL", f"http://{NODE_BY_NAME['Node-3'].lan_ip}:3000")
# Same Grafana via Node-3's Tailscale address, for users who open the app over Tailscale (100.x).
GRAFANA_TAILNET_URL = os.environ.get("AIOPS_GRAFANA_TAILNET_URL", f"http://{NODE_BY_NAME['Node-3'].ts_ip}:3000")
API_RATE_PER_IP = float(os.environ.get("AIOPS_API_RATE", "20"))          # web API requests/s per client IP
API_BURST_PER_IP = float(os.environ.get("AIOPS_API_BURST", "200"))
GATEWAY_RATE_PER_KEY = float(os.environ.get("AIOPS_GATEWAY_RATE", "100"))  # model requests/s per API key
GATEWAY_BURST_PER_KEY = float(os.environ.get("AIOPS_GATEWAY_BURST", "500"))
TTFT_P95_WARN_S = 10.0          # chat models: p95 time-to-first-token above this while serving = degraded
LATENCY_KINDS = ("LLM", "VLM")
# the latency rule needs real traffic: below this rate the p95 (over the monitor's ~5 min window) is one or two
# requests, e.g. one of the console agent's own 15k-token diagnosis prompts, not a degraded server
LATENCY_MIN_RPM = 2.0

# Workflow bounds
MAX_DIAGNOSIS_ATTEMPTS = 2
# Investigations run on the shared LLM (qwen3.6). A burst of incidents (a reboot raises several at once) must not
# saturate it: at most this many investigate at a time, the rest wait in order (still 'open').
MAX_CONCURRENT_INVESTIGATIONS = int(os.environ.get("AIOPS_MAX_INVESTIGATIONS", "2"))
LLM_TIMEOUT_S = 120
APPROVER_TOKEN = os.environ.get("AIOPS_APPROVER_TOKEN", "")
# The CLI token acts as this existing user (approver or admin), so approvals made with it carry a real name.
APPROVER_TOKEN_USER = os.environ.get("AIOPS_APPROVER_TOKEN_USER", "")
NON_HUMAN_ACTORS = ("agent", "orchestrator", "system", "cli-token")   # can never approve or be a user name

# ---------------------------------------------------------------- model gateway (Phase 2)
API_PORT = int(os.environ.get("AIOPS_API_PORT", "5170"))           # web console + API (uvicorn --port in aiops.service)
GATEWAY_PORT = int(os.environ.get("AIOPS_GATEWAY_PORT", "8790"))
GATEWAY_URL = os.environ.get("AIOPS_GATEWAY_URL", f"http://127.0.0.1:{GATEWAY_PORT}")
GATEWAY_KEY = os.environ.get("AIOPS_GATEWAY_KEY", "")           # the platform's own client key
GATEWAY_DISCOVERY_S = 15                                         # re-discover model servers
GATEWAY_MAX_INFLIGHT = 128                                       # per replica (default)
# Per-model caps matching each server's --max-num-seqs, so the gateway queues instead of overloading.
GATEWAY_MAX_INFLIGHT_BY_MODEL: dict[str, int] = {"qwen-chat": 4}   # Node-3 Qwen3-30B runs --max-num-seqs 4
# Preferred node per model, as AIOPS_GATEWAY_PREFER="model=Node-N,...": requests go to that node's replica while it
# is available, and the other replicas are standby (failover only). Without an entry, replicas share the load.
GATEWAY_PREFER: dict[str, str] = dict(p.strip().split("=", 1) for p in os.environ.get("AIOPS_GATEWAY_PREFER", "").split(",")
                                      if "=" in p)
GATEWAY_QUEUE_WAIT_S = 20                                        # wait for a free replica before 503
# Agent priority lane. The platform's own agents (this key name) are never capped. Every other client shares a
# bounded number of concurrent requests per model, so a burst from apps cannot slow the agents' generation to a
# crawl (vLLM decodes all running requests together: 48 concurrent made each ~48x slower in the load test).
# Overflow from other clients waits at the gateway (FIFO on release), then gets a 503 "busy" to retry.
GATEWAY_PRIORITY_CLIENTS = {"aiops-platform"}
GATEWAY_SHARED_INFLIGHT_BY_MODEL: dict[str, int] = {"qwen3.6-35b-a3b-fp8": 8}
GATEWAY_SHARED_QUEUE_WAIT_S = 120
GATEWAY_MAX_ATTEMPTS = 3                                         # replicas tried per request
# Aliases: a stable name for clients, resolved in order. The gateway only falls through to
# the next model when the previous one has no healthy replica; the response headers say
# which model actually answered. Replicas of the same model are load-balanced.
#
# Cross-model fallback is only allowed where outputs stay interchangeable. Chat may fall back
# to another instruct model (lower quality, still a valid answer). Embeddings and rerank scores
# may NOT: a different model writes vectors from another space (Node-3's Qwen3-Embedding-0.6B is
# 1024-d, Node-2's 4B is 2560-d) and would silently corrupt every index that consumes them.
# Those aliases therefore name one model; they still fail over across replicas of that model.
SINGLE_MODEL_ALIASES = {"embed", "rerank"}
MODEL_ALIASES: dict[str, list[str]] = {
    "chat": ["qwen3.6-35b-a3b-fp8", "qwen-chat"],          # qwen-chat = Qwen3-30B-A3B-Instruct-2507-FP8 (Node-3)
    "embed": ["qwen3-embedding-4b"],
    "rerank": ["qwen3-reranker-4b"],
    "asr": ["qwen3-asr", "Qwen/Qwen3-ASR-1.7B", "whisper-turbo", "openai/whisper-large-v3-turbo"],
    "tts": ["Qwen/Qwen3-TTS-12Hz-1.7B-Base", "k2-fsa/OmniVoice"],
}
# Operators can override aliases without a code change: AIOPS_GATEWAY_ALIASES='{"chat": ["a", "b"]}'
MODEL_ALIASES.update(json.loads(os.environ.get("AIOPS_GATEWAY_ALIASES", "{}")))
for _alias in SINGLE_MODEL_ALIASES:
    if len(MODEL_ALIASES.get(_alias, [])) > 1:
        raise ValueError(f"alias {_alias!r} must name exactly one model (vectors from different models are incompatible)")
# Extra upstreams not discovered via Spark Monitor, as JSON: [{"node": "...", "base_url": "http://h:p"}]
GATEWAY_STATIC_UPSTREAMS = os.environ.get("AIOPS_GATEWAY_STATIC_UPSTREAMS", "[]")

# The platform's own LLM client: through the gateway (replicas, failover, metrics), with a
# direct path to Node-2 as last resort so a gateway outage cannot blind the SRE agent.
LLM_ENDPOINTS: list[LLMEndpoint] = [
    LLMEndpoint("gateway", f"{GATEWAY_URL}/v1", "chat", GATEWAY_KEY),
    LLMEndpoint("dgx2-direct", f"http://{NODE_BY_NAME['Node-2'].lan_ip}:8001/v1", "qwen3.6-35b-a3b-fp8"),
]

import time

from fastapi import APIRouter

from backend.core import analytics, config, store
from backend.incidents import policy, tools
from backend.incidents.runtime import runtime
from backend.web.security import Viewer

router = APIRouter(tags=["governance"])

RULES = [
    "The model proposes; deterministic policy decides; a human approves every write.",
    "Write operations only on registered test resources, only with their allowed operations.",
    "Every write must cite evidence recorded for that incident.",
    "The executor re-checks the approval record and policy before acting.",
    "Recovery is decided by fresh health checks, never by the executor's exit code.",
    "Evidence (logs, metrics, documents) is untrusted data, never instructions.",
]


@router.get("/stats")
def stats(user: Viewer):
    c = analytics.table_counts()
    return analytics.stats(time.time()) | {
        "policy": {"decisions": c["policy_decisions"], "denied": c["policy_deny"]},
        "approvals": {"total": c["approval_requests"], "approved": c["approved"], "rejected": c["rejected"]},
        "executions": c["execution_records"], "verified_resolved": c["verified_resolved"],
    }


@router.get("/audit")
def audit(user: Viewer, limit: int = 300, actor: str = "", event: str = "", incident_id: str = "",
          incidents_only: bool = False):
    return store.audit_log(min(limit, 2000), actor, event, incident_id, incidents_only)


@router.get("/audit/integrity")
def audit_integrity(user: Viewer, fresh: bool = False):
    from backend.incidents.runtime import runtime
    return runtime.check_audit() if fresh or runtime.audit_check.get("ok") is None else runtime.audit_check


@router.get("/policy")
def policy_view(user: Viewer):
    return {
        "autonomy": {"level": 3, "scope": "registered test resources only; everything else level 2 (recommend)"},
        "operations": [{"operation": op, "kind": "write", "risk": risk, "decision": "requires_approval"}
                       for op, risk in policy.WRITE_OPS.items()]
                      + [{"operation": op, "kind": "no-op", "risk": "none", "decision": "allow"}
                         for op in sorted(policy.READ_ONLY_OPS)],
        "resources": [{"resource_id": r.resource_id, "resource_type": r.resource_type, "node": r.node,
                       "container": r.container, "health_url": r.health_url,
                       "allowed_operations": list(r.allowed_operations)} for r in config.TEST_RESOURCES.values()],
        "readonly_tools": sorted(tools.READONLY_TOOLS),
        "detector": {"scan_seconds": runtime.scan_seconds, "mem_pct_warn": config.MEM_PCT_WARN,
                     "disk_pct_warn": config.DISK_PCT_WARN, "gpu_temp_warn": config.GPU_TEMP_WARN,
                     "model_loading_max_s": config.MODEL_LOADING_MAX_S,
                     "node_unreachable_after": config.NODE_UNREACHABLE_AFTER},
        "rules": RULES,
    }


# ---------------------------------------------------------------- dashboard overview
import ipaddress  # noqa: E402
import socket  # noqa: E402
from urllib.parse import urlparse  # noqa: E402

import httpx  # noqa: E402

from backend.core import evals
from backend.monitoring import fleet, services  # noqa: E402

_PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
                                              "100.64.0.0/10", "::1/128", "fc00::/7")]


def _is_private(url: str) -> bool:
    host = urlparse(url).hostname or ""
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addrs = [ipaddress.ip_address(a[4][0]) for a in socket.getaddrinfo(host, None)]
        except OSError:
            return False                      # unresolvable: treat as not proven private
    return all(any(a in n for n in _PRIVATE) for a in addrs)


def _sovereign() -> dict:
    endpoints = [e.base_url for e in config.LLM_ENDPOINTS]
    out = {"gateway_up": False, "requests": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0,
           "replicas_healthy": 0, "replicas_total": 0, "aliases": {}, "uptime_s": None}
    try:
        st = httpx.get(f"{config.GATEWAY_URL}/gateway/stats", timeout=3,
                       headers={"Authorization": f"Bearer {config.GATEWAY_KEY}"}).json()
        ups = [u for u in st["upstreams"] if not u["model"].startswith("?")]
        endpoints += [u["base_url"] for u in st["upstreams"]]
        healthy = lambda u: u["state"] in ("up", "saturated", "half-open")
        out.update(gateway_up=True, uptime_s=st["uptime_s"],
                   requests=sum(u["requests"] for u in ups), errors=sum(u["errors"] for u in ups),
                   prompt_tokens=sum(u.get("prompt_tokens", 0) for u in ups),
                   completion_tokens=sum(u.get("completion_tokens", 0) for u in ups),
                   replicas_healthy=sum(map(healthy, ups)), replicas_total=len(ups),
                   aliases={a: {"models": ms, "healthy_replicas": sum(healthy(u) for u in ups if u["model"] in ms),
                                "nodes": sorted({u["node"] for u in ups if u["model"] in ms and healthy(u)})}
                            for a, ms in st["aliases"].items()})
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    uniq = sorted(set(endpoints))
    out["model_endpoints"] = len(uniq)
    out["endpoints_outside_private_network"] = [u for u in uniq if not _is_private(u)]
    return out


def _serving(n: dict) -> list[dict]:
    """Model servers that should be up. An Ollama model not loaded in memory ('idle') is not one: Ollama unloads
    idle models by design and loads them on the next request."""
    return [s for s in n.get("servers", []) if s.get("status") != "idle"]


def _fleet_health(now: float) -> dict:
    snap = fleet.snapshot()
    servers = [s for n in snap.values() for s in _serving(n)]
    cov = runtime.kernel_coverage
    with store.conn() as c:
        xid_24h = c.execute("SELECT COUNT(*) FROM incidents WHERE kind='gpu_xid' AND created >= ?", (now - 86400,)).fetchone()[0]
    return {"nodes_total": len(snap), "nodes_reachable": sum(n["ok"] for n in snap.values()),
            "models_total": len(servers), "models_ready": sum(s["status"] == "ready" for s in servers),
            "kernel_coverage": sum(v["ok"] for v in cov.values()), "kernel_total": len(config.NODES),
            "gpu_xid_incidents_24h": xid_24h,
            "nodes": {name: {"ok": n["ok"], "services": services.summarize(runtime.services.get(name) or {}),
                             "disk_pct": n.get("disk_used_pct"), "gpu_temp": (n.get("gpu") or {}).get("temp"),
                             "mem_pct": round(100 * n["mem_used_gb"] / n["mem_total_gb"], 1) if n.get("mem_total_gb") else None,
                             "models": len(_serving(n)),
                             "models_ready": sum(s["status"] == "ready" for s in _serving(n)),
                             "kernel_ok": cov.get(name, {}).get("ok")} for name, n in snap.items()}}


@router.get("/overview")
def overview(user: Viewer, days: int = 7, origin: str = "real"):
    now = time.time()
    return analytics.overview(now, max(1, min(days, 90)), real_only=origin != "all") | {"active": analytics.active_counts(),
        "evaluations": evals.latest(), "sovereign": _sovereign(), "fleet": _fleet_health(now)}

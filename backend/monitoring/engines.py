"""Model servers Spark Monitor does not see: SGLang and Ollama, plus which vLLM servers are vLLM-Omni.

Spark Monitor (left unmodified) only recognises `vllm serve` / `vllm-omni serve`. This module finds the other
engines so a model a colleague starts under SGLang or Ollama shows up everywhere vLLM servers do.

- **Discovery.** Every SCAN_S, one read-only SSH call per node (or a local call on the platform's own node) runs
  PROBE: a stdlib Python script that reads /proc for SGLang and Ollama processes and asks each server over
  127.0.0.1 for its health, models and metrics. It also lists vLLM-Omni pids. Nothing is installed, nothing is
  written.
- **Shape.** Results come back as rows in Spark Monitor's `vllm` row shape, plus `engine`, so the detector, the
  fleet view, the Prometheus targets and the gateway handle them without special cases.
- **Ollama.** Installed models are rows with status `ready` when loaded in memory and `idle` when not. Ollama
  unloading an idle model is normal and never looks like a server disappearing.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from backend.core import config

log = logging.getLogger("backend.monitoring.engines")
SCAN_S = 30.0
STALE_S = 180.0          # a node whose scan keeps failing drops its engine rows after this long

# Runs on the node. Read-only: /proc and HTTP GETs/POSTs to 127.0.0.1 (Ollama's /api/show is a read despite POST).
PROBE = r'''
import json, os, re, time, urllib.request
def get(url, body=None, t=2.5):
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=t) as r:
            return r.status, r.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""
def arg(a, *names, default=None):
    for i, x in enumerate(a):
        for n in names:
            if x == n and i + 1 < len(a): return a[i + 1]
            if x.startswith(n + "="): return x.split("=", 1)[1]
    return default
def listen_addr(port):
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            for line in open(f).read().splitlines()[1:]:
                p = line.split()
                h, pt = p[1].rsplit(":", 1)
                if p[3] == "0A" and int(pt, 16) == port:
                    return "loopback" if h in ("0100007F", "00000000000000000000000001000000") else "any"
        except OSError:
            pass
    return None
boot = time.time() - float(open("/proc/uptime").read().split()[0])
hz = os.sysconf("SC_CLK_TCK")
out = {"sglang": [], "ollama": [], "omni_pids": []}
seen_ports = set()
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        a = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        a = [x.decode("utf-8", "replace") for x in a if x]
        started = boot + int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[19]) / hz
    except OSError:
        continue
    if not a: continue
    s = " ".join(a)
    if "AIOPS_PROBE" in s: continue                      # this probe's own shell carries its source text
    if ("vllm-omni" in s or "--omni" in a or "vllm_omni" in s) and "serve" in a:
        out["omni_pids"].append(int(pid))
    elif "sglang.launch_server" in s or "sglang/launch_server" in s or (os.path.basename(a[0]) == "sglang" and "serve" in a):
        port = int(arg(a, "--port", default="30000"))
        if port in seen_ports: continue                       # child processes repeat the parent's arguments
        seen_ports.add(port)
        name = arg(a, "--served-model-name") or arg(a, "--model-path", "--model", default="?")
        st, _ = get(f"http://127.0.0.1:{port}/health")
        _, info = get(f"http://127.0.0.1:{port}/get_model_info")
        ms, metrics = get(f"http://127.0.0.1:{port}/metrics")
        try: info = json.loads(info)
        except ValueError: info = {}
        m = {}
        if ms == 200:
            for line in metrics.splitlines():
                mm = re.match(r"^(sglang:[a-z_]+)(?:\{[^}]*\})? ([0-9.eE+-]+)$", line)
                if mm: m[mm.group(1)] = m.get(mm.group(1), 0.0) + float(mm.group(2))
        out["sglang"].append({"pid": int(pid), "port": port, "name": name, "started": started, "health": st,
                              "is_generation": info.get("is_generation", True), "metrics": ms == 200, "values": m,
                              "bind": listen_addr(port)})
    elif os.path.basename(a[0]) == "ollama" and "serve" in a:
        host = None
        try:
            env = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
            host = next((e.decode().split("=", 1)[1] for e in env if e.startswith(b"OLLAMA_HOST=")), None)
        except OSError:
            pass                                             # another user's process: environment not readable
        port = int(host.rsplit(":", 1)[1]) if host and ":" in host else 11434
        if port in seen_ports: continue
        seen_ports.add(port)
        base = f"http://127.0.0.1:{port}"
        vs, ver = get(base + "/api/version")
        _, tags = get(base + "/api/tags")
        _, ps = get(base + "/api/ps")
        try: tags = json.loads(tags).get("models", [])
        except ValueError: tags = []
        try: loaded = {m["name"]: m for m in json.loads(ps).get("models", [])}
        except ValueError: loaded = {}
        models = []
        for t in tags[:40]:
            _, show = get(base + "/api/show", {"model": t["name"]})
            try: caps = json.loads(show).get("capabilities") or []
            except ValueError: caps = []
            l = loaded.get(t["name"])
            models.append({"name": t["name"], "size": t.get("size"), "family": (t.get("details") or {}).get("family"),
                           "parameter_size": (t.get("details") or {}).get("parameter_size"), "capabilities": caps,
                           "loaded": l is not None, "size_vram": (l or {}).get("size_vram"), "expires_at": (l or {}).get("expires_at")})
        try: ver = json.loads(ver).get("version")
        except ValueError: ver = None
        out["ollama"].append({"pid": int(pid), "port": port, "started": started, "api": vs == 200, "version": ver,
                              "models": models, "bind": listen_addr(port)})
print(json.dumps(out))
'''

_lock = threading.Lock()
_state: dict[str, tuple[float, dict]] = {}        # node -> (time of last good scan, raw probe output)
_thread: threading.Thread | None = None


def _kind_ollama(m: dict) -> str:
    caps, name = m.get("capabilities") or [], m["name"].lower()
    if "rerank" in name:
        return "Rerank"
    if "embedding" in caps or "embed" in name or m.get("family") in ("bert", "nomic-bert"):
        return "Embed"
    return "VLM" if "vision" in caps else "LLM"


def rows(raw: dict) -> list[dict]:
    """Probe output -> rows in Spark Monitor's `vllm` row shape (plus engine fields)."""
    out = []
    for s in raw.get("sglang", []):
        v = s.get("values") or {}
        out.append({"engine": "sglang", "name": s["name"].rstrip("/").split("/")[-1] if s["name"].startswith("/") else s["name"],
                    "kind": "LLM" if s.get("is_generation", True) else "Embed", "port": s["port"], "pid": s["pid"],
                    "status": "ready" if s.get("health") == 200 else "loading", "started": s.get("started"),
                    "running": v.get("sglang:num_running_reqs"), "waiting": v.get("sglang:num_queue_reqs"),
                    "kv_cache": v.get("sglang:token_usage"), "tokens_per_s": v.get("sglang:gen_throughput"),
                    "prefix_hit": v.get("sglang:cache_hit_rate"), "metrics": bool(s.get("metrics")), "bind": s.get("bind")})
    for o in raw.get("ollama", []):
        if not o.get("api"):
            out.append({"engine": "ollama", "name": "ollama", "kind": "LLM", "port": o["port"], "pid": o["pid"],
                        "status": "error", "started": o.get("started"), "bind": o.get("bind")})
            continue
        for m in o.get("models", []):
            out.append({"engine": "ollama", "name": m["name"], "kind": _kind_ollama(m), "port": o["port"], "pid": o["pid"],
                        "status": "ready" if m.get("loaded") else "idle", "started": o.get("started"),
                        "running": None, "waiting": None, "mem_gb": round((m.get("size_vram") or 0) / 1e9, 1) if m.get("loaded") else None,
                        "engine_version": o.get("version"), "parameter_size": m.get("parameter_size"), "bind": o.get("bind")})
    return out


def scan_node(node: config.Node) -> dict:
    from backend.monitoring import kernel
    acc = kernel.access_for(node)
    r = kernel.run(acc, "python3 - <<'AIOPS_PROBE'\n" + PROBE + "\nAIOPS_PROBE")
    if r is None or r.returncode != 0:
        raise RuntimeError((r.stderr if r else "no result")[-300:])
    return json.loads(r.stdout.strip().splitlines()[-1])


def _loop() -> None:
    while True:
        for n in config.NODES:
            try:
                raw = scan_node(n)
                with _lock:
                    _state[n.name] = (time.time(), raw)
            except Exception as e:  # keep the last good scan until it goes stale
                log.debug("engine scan %s failed: %s", n.name, e)
        time.sleep(SCAN_S)


def start() -> None:
    global _thread
    if _thread is None:
        _thread = threading.Thread(target=_loop, name="engine-scan", daemon=True)
        _thread.start()


def servers(node: str) -> list[dict]:
    start()
    with _lock:
        t, raw = _state.get(node, (0.0, {}))
    return rows(raw) if time.time() - t < STALE_S else []


def omni_pids(node: str) -> set[int]:
    start()
    with _lock:
        t, raw = _state.get(node, (0.0, {}))
    return set(raw.get("omni_pids", [])) if time.time() - t < STALE_S else set()


def merge(node: str, d: dict) -> dict:
    """Spark Monitor payload + engines it cannot see. Every vLLM row is labelled vllm or vllm-omni."""
    omni = omni_pids(node)
    base = [dict(s, engine="vllm-omni" if s.get("pid") in omni else "vllm") for s in d.get("vllm") or []]
    known = {s.get("port") for s in base}
    extra = [s for s in servers(node) if s.get("port") not in known or s["engine"] == "ollama"]
    return {**d, "vllm": base + extra}

"""Fleet snapshot for the UI: one /metrics fetch per node, all nodes in parallel,
cached briefly so any number of viewers costs at most one fan-out per TTL."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from backend.core import config
from backend.incidents import tools

TTL_S = 4.0
_pool = ThreadPoolExecutor(max_workers=len(config.NODES), thread_name_prefix="fleet")
_lock = threading.Lock()
_cache: tuple[float, dict] = (0.0, {})


def _node(n: config.Node) -> tuple[str, dict]:
    base = {"host": n.host, "ip": n.lan_ip}
    try:
        d, src = tools.monitor_metrics(n.name)
    except tools.ToolError as e:
        return n.name, base | {"ok": False, "error": str(e)}
    return n.name, base | {"ok": True, "via": src, **tools.summarize_metrics(d), "servers": tools.summarize_servers(d)}


def snapshot() -> dict:
    global _cache
    t, data = _cache
    if time.monotonic() - t < TTL_S:
        return data
    with _lock:                      # single flight: concurrent callers wait for one refresh
        t, data = _cache
        if time.monotonic() - t < TTL_S:
            return data
        data = dict(_pool.map(_node, config.NODES))
        _cache = (time.monotonic(), data)
        return data

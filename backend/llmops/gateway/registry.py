"""Upstream registry: discovery, health, circuit breaking and replica selection.

Discovery: every model server the Spark Monitor agents report, addressed on the
node's LAN IP, plus optional static upstreams. Each candidate is probed with
GET /v1/models; only servers that answer *from here* become routable, and the
model ids they report are authoritative (not the monitor's labels).

Health is both active (periodic probe) and passive (a request that fails to
connect or gets 502/503/504 opens that replica's breaker with exponential
backoff). Selection is least-in-flight among available replicas of one model.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

from backend.core import config
from backend.monitoring import fleet

PROBE_TIMEOUT = httpx.Timeout(2.5)
BACKOFF_MAX_S = 60.0


def _quantiles(xs, qs=(0.5, 0.95)) -> dict:
    if not xs:
        return {f"p{int(q * 100)}": None for q in qs}
    s = sorted(xs)
    return {f"p{int(q * 100)}": round(s[min(len(s) - 1, int(q * len(s)))], 4) for q in qs}


RTF_BUCKETS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 5.0)


@dataclass
class Stats:
    requests: int = 0
    errors: int = 0             # transport failures and 5xx
    client_errors: int = 0      # 4xx from upstream
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency: deque = field(default_factory=lambda: deque(maxlen=512))    # full response, seconds
    ttft: deque = field(default_factory=lambda: deque(maxlen=512))       # first byte, seconds
    # speech recognition: seconds of input audio (from the response's usage.seconds) and real-time factor
    # (processing time / audio length) as a cumulative histogram, so Prometheus can take quantiles over any range
    audio_seconds: float = 0.0
    rtf_counts: list = field(default_factory=lambda: [0] * len(RTF_BUCKETS))
    rtf_sum: float = 0.0
    rtf_n: int = 0

    def observe_rtf(self, audio_s: float, elapsed_s: float) -> None:
        rtf = elapsed_s / audio_s
        self.audio_seconds += audio_s
        self.rtf_sum += rtf
        self.rtf_n += 1
        for i, le in enumerate(RTF_BUCKETS):
            if rtf <= le:
                self.rtf_counts[i] += 1

    def snapshot(self) -> dict:
        return {"requests": self.requests, "errors": self.errors, "client_errors": self.client_errors,
                "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "latency": _quantiles(self.latency), "ttft": _quantiles(self.ttft)}


@dataclass(eq=False)
class Upstream:
    node: str
    model: str
    base_url: str
    kind: str = "LLM"
    static: bool = False
    healthy: bool = False
    inflight: int = 0
    shared: int = 0            # in-flight requests from non-priority clients (the priority lane leaves room)
    fails: int = 0
    down_until: float = 0.0
    last_error: str | None = None
    last_probe: float = 0.0
    max_model_len: int | None = None
    stats: Stats = field(default_factory=Stats)

    @property
    def capacity(self) -> int:
        return config.GATEWAY_MAX_INFLIGHT_BY_MODEL.get(self.model, config.GATEWAY_MAX_INFLIGHT)

    @property
    def shared_capacity(self) -> int:
        return config.GATEWAY_SHARED_INFLIGHT_BY_MODEL.get(self.model, self.capacity)

    @property
    def key(self) -> str:
        return f"{self.model}@{self.base_url}"

    def available(self, now: float, priority: bool = True) -> bool:
        if not self.healthy or now < self.down_until:
            return False
        if not priority and self.shared >= self.shared_capacity:
            return False                                   # the rest of this replica is kept for the agents
        # half-open: a replica recovering from failures gets one trial request at a time
        return self.inflight < (1 if self.fails else self.capacity)

    def failed(self, err: str) -> None:
        self.fails += 1
        self.down_until = time.monotonic() + min(BACKOFF_MAX_S, 2.5 * 2 ** self.fails)
        self.last_error = err[:200]

    def succeeded(self) -> None:
        if self.fails:
            self.fails, self.down_until = 0, 0.0

    def view(self) -> dict:
        now = time.monotonic()
        if not self.healthy:
            state = "down"
        elif now < self.down_until:
            state = "backoff"
        elif self.fails:
            state = "half-open"
        else:
            state = "up" if self.available(now) else "saturated"
        return {"model": self.model, "node": self.node, "base_url": self.base_url, "kind": self.kind,
                "state": state, "inflight": self.inflight, "shared_inflight": self.shared, "fails": self.fails,
                "retry_in_s": round(max(0.0, self.down_until - now), 1), "last_error": self.last_error,
                "max_model_len": self.max_model_len, "capacity": self.capacity, "static": self.static,
                **self.stats.snapshot()}


_GATEWAY_HOST = next((n.lan_ip for n in config.NODES if n.name == "Node-2"), "127.0.0.1")   # where the gateway runs


class Registry:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.upstreams: dict[str, Upstream] = {}          # key -> upstream
        self.last_discovery: float | None = None
        self.discovery_error: str | None = None
        self._rr = itertools.count()
        self._changed = asyncio.Condition()
        self._kinds: dict[tuple[str, str], str] = {}     # (node, model) -> kind, for servers that host many models (Ollama)

    # ------------------------------------------------------------ discovery
    def _candidates(self) -> list[tuple[str, str, str, bool]]:
        out = []
        kinds: dict[tuple[str, str], str] = {}
        for name, n in fleet.snapshot().items():
            node = config.NODE_BY_NAME[name]
            # a server bound to localhost is reachable only from its own node; the gateway runs on RELAY_HOST's node
            local = node.lan_ip == _GATEWAY_HOST
            for s in n.get("servers", []):
                host = "127.0.0.1" if local and s.get("bind") == "loopback" else node.lan_ip
                base = f"http://{host}:{s['port']}"
                if s.get("engine") == "ollama":         # one server, many models: each model keeps its own kind
                    kinds[(name, s["name"])] = s.get("kind") or "LLM"
                    if s.get("status") != "error":
                        out.append((name, base, "auto", False))
                    continue
                out.append((name, base, s.get("kind") or "LLM", False))
        self._kinds = kinds                              # swap in whole: probes read it from another thread
        for s in json.loads(config.GATEWAY_STATIC_UPSTREAMS or "[]"):
            out.append((s["node"], s["base_url"].rstrip("/"), s.get("kind", "LLM"), True))
        return out

    async def _probe(self, node: str, base_url: str, kind: str, static: bool) -> list[Upstream]:
        try:
            r = await self.client.get(f"{base_url}/v1/models", timeout=PROBE_TIMEOUT)
            r.raise_for_status()
            models = r.json().get("data", [])
        except Exception as e:
            return [Upstream(node, f"?{base_url}", base_url, "LLM" if kind == "auto" else kind, static, healthy=False,
                             last_error=f"{type(e).__name__}", last_probe=time.time())]
        return [Upstream(node, m["id"], base_url, self._kinds.get((node, m["id"]), "LLM") if kind == "auto" else kind, static,
                         healthy=True, last_probe=time.time(), max_model_len=m.get("max_model_len")) for m in models if m.get("id")]

    async def discover(self) -> None:
        try:
            cands = await asyncio.to_thread(self._candidates)
        except Exception as e:  # monitor outage: keep the last known table, keep probing it
            self.discovery_error = repr(e)
            cands = [(u.node, u.base_url, u.kind, u.static) for u in self.upstreams.values()]
        found = [u for group in await asyncio.gather(*(self._probe(*c) for c in set(cands))) for u in group]
        fresh: dict[str, Upstream] = {}
        for u in found:
            old = self.upstreams.get(u.key)
            if old:                                  # keep live counters and breaker state
                old.healthy, old.last_probe, old.max_model_len, old.kind = u.healthy, u.last_probe, u.max_model_len, u.kind
                if u.healthy and old.last_error and old.down_until <= time.monotonic():
                    old.last_error = None
                fresh[u.key] = old
            else:
                fresh[u.key] = u
        self.upstreams = fresh
        self.last_discovery = time.time()
        async with self._changed:
            self._changed.notify_all()

    async def run(self) -> None:
        while True:
            try:
                await self.discover()
            except Exception as e:
                self.discovery_error = repr(e)
            await asyncio.sleep(config.GATEWAY_DISCOVERY_S)

    # ------------------------------------------------------------ routing
    def models(self) -> dict[str, list[Upstream]]:
        out: dict[str, list[Upstream]] = {}
        for u in self.upstreams.values():
            if not u.model.startswith("?"):
                out.setdefault(u.model, []).append(u)
        return out

    def resolve(self, name: str) -> list[list[Upstream]] | None:
        """Ordered replica groups for a model or alias; None if the name is unknown."""
        table = self.models()
        if name in config.MODEL_ALIASES:
            return [table.get(m, []) for m in config.MODEL_ALIASES[name]]
        return [table[name]] if name in table else None

    def pick(self, group: list[Upstream], exclude: set[str], priority: bool = True) -> Upstream | None:
        now = time.monotonic()
        ready = [u for u in group if u.key not in exclude and u.available(now, priority)]
        if not ready:
            return None
        low = min(u.inflight for u in ready)
        tied = [u for u in ready if u.inflight == low]
        return tied[next(self._rr) % len(tied)]

    def has_capacity_later(self, group: list[Upstream], exclude: set[str]) -> bool:
        """Healthy replicas that are only saturated (not failing) are worth waiting for."""
        now = time.monotonic()
        return any(u.key not in exclude and u.healthy and now >= u.down_until for u in group)

    async def released(self, timeout: float) -> None:
        async with self._changed:
            try:
                await asyncio.wait_for(self._changed.wait(), timeout)
            except asyncio.TimeoutError:
                pass

    async def release(self, u: Upstream, shared: bool = False) -> None:
        u.inflight -= 1
        if shared:
            u.shared -= 1
        async with self._changed:
            self._changed.notify_all()      # waiters queue per model: waking one could wake the wrong one

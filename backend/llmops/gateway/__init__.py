"""OpenAI-compatible model gateway (Phase 2).

Clients call one address with a model name or alias; the gateway picks a healthy
replica on any node, streams the response through, and fails over to another
replica if (and only if) the chosen one fails before sending anything.

  POST /v1/{chat/completions, completions, embeddings, rerank, score, audio/...}
  GET  /v1/models          routable models and aliases
  GET  /gateway/stats      per-replica health and metrics (JSON)
  GET  /metrics            Prometheus exposition
  GET  /healthz            liveness (no auth)

Security: same network allowlist as the console; Bearer API key on every /v1 and
/gateway call; upstream servers never see the client's key.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from backend.core.ratelimit import TokenBuckets
from backend.core import config
from backend.web import security
from backend.llmops.gateway import keys
from backend.llmops.requestlog import RequestLog, endpoint
from backend.llmops.gateway.registry import RTF_BUCKETS, Registry, Upstream

log = logging.getLogger("backend.llmops.gateway")
RETRYABLE_STATUS = {502, 503, 504}
HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "host", "authorization",
       "proxy-authorization", "te", "trailer", "upgrade"}
TIMEOUT = httpx.Timeout(connect=3.0, read=600.0, write=60.0, pool=5.0)
TAIL_BYTES = 4096
MAX_BODY_BYTES = 64 * 2**20     # largest request accepted (an audio file for transcription is the big case)
_AUDIO_SECONDS = re.compile(rb'"usage"\s*:\s*\{[^{}]*?"seconds"\s*:\s*([0-9]+(?:\.[0-9]+)?)')
_ASR_PATHS = ("audio/transcriptions", "audio/translations")
_USAGE = re.compile(rb'"prompt_tokens"\s*:\s*(\d+).{0,200}?"completion_tokens"\s*:\s*(\d+)', re.S)


# Who uses what: requests per API key (by its name, never the key) and requested model, and how often a request
# had to fail over to another replica. Process-lifetime counters, exported at /metrics.
CLIENT_REQUESTS: Counter = Counter()      # (client, model, outcome) -> n
FAILOVERS: Counter = Counter()            # requested model -> n


def _label(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')[:80]


def _count_usage(stats, tail: bytes) -> tuple[int, int]:
    """Read the OpenAI `usage` block from the end of a response (the final SSE chunk when
    streaming with include_usage), without buffering the whole body. Returns (prompt, completion) tokens."""
    m = None
    for m in _USAGE.finditer(tail):
        pass
    if not m:
        return 0, 0
    p, c = int(m.group(1)), int(m.group(2))
    stats.prompt_tokens += p
    stats.completion_tokens += c
    return p, c


def _error(status: int, message: str, rid: str, kind: str = "gateway_error") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind, "request_id": rid}}, status_code=status,
                        headers={"x-request-id": rid})


class Body:
    """The request body, with the model name read from JSON or multipart form data."""

    def __init__(self, raw: bytes, ctype: str, model: str | None, json_doc: dict | None = None, form=None):
        self.raw, self.ctype, self.model, self.json_doc, self.form = raw, ctype, model, json_doc, form

    async def for_model(self, model: str) -> tuple[bytes, str]:
        """(payload, content-type) naming `model`; unchanged if it already does."""
        if model == self.model:
            return self.raw, self.ctype
        if self.json_doc is not None:
            return json.dumps(self.json_doc | {"model": model}).encode(), self.ctype
        if self.form is not None:              # re-encode the multipart body with the new model field
            data, files = {}, {}
            for k, v in self.form.multi_items():
                if isinstance(v, str):
                    data[k] = model if k == "model" else v
                else:
                    await v.seek(0)
                    files[k] = (v.filename, await v.read(), v.content_type)
            req = httpx.Request("POST", "http://upstream", data=data, files=files)
            return req.read(), req.headers["content-type"]
        return self.raw, self.ctype


async def _read_body(request: Request) -> Body:
    raw = await request.body()
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        try:
            doc = json.loads(raw or b"{}")
        except ValueError:
            return Body(raw, ctype, None)
        return Body(raw, ctype, doc.get("model") if isinstance(doc, dict) else None, doc if isinstance(doc, dict) else None)
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        m = form.get("model")
        return Body(raw, ctype, m if isinstance(m, str) else None, form=form)
    return Body(raw, ctype, None)


def create_app(client: httpx.AsyncClient | None = None, *, background: bool = True,
               request_log: RequestLog | None = None) -> FastAPI:
    state: dict = {}
    requests_log = request_log or RequestLog()     # metadata only: see requestlog.py

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # idle connections are dropped after 4 s: the model servers (uvicorn) close theirs after 5 s, and reusing one
        # they just closed fails the request with "Server disconnected without sending a response"
        c = client or httpx.AsyncClient(timeout=TIMEOUT, limits=httpx.Limits(max_connections=1024, max_keepalive_connections=256,
                                                                              keepalive_expiry=4.0))
        reg = Registry(c)
        state.update(client=c, registry=reg, started=time.time(), requests=requests_log)
        task = None
        if background:
            await reg.discover()
            task = asyncio.create_task(reg.run())
            requests_log.start()
        yield
        if task:
            task.cancel()
        if background:
            requests_log.stop()
        if client is None:
            await c.aclose()

    app = FastAPI(title="Sovereign AI Ops model gateway", lifespan=lifespan, docs_url=None, redoc_url=None)
    key_buckets = TokenBuckets(config.GATEWAY_RATE_PER_KEY, config.GATEWAY_BURST_PER_KEY)
    app.state.gw = state

    @app.middleware("http")
    async def guard(request: Request, call_next):
        rid = request.headers.get("x-request-id") or f"gw-{uuid.uuid4().hex[:16]}"
        request.state.rid = rid
        if not security.client_allowed(request.client.host if request.client else None):
            return _error(403, "forbidden network", rid, "permission_error")
        if request.url.path.startswith(("/v1/", "/gateway/")):
            auth = request.headers.get("authorization", "")
            key = keys.verify(auth[7:] if auth.lower().startswith("bearer ") else None)
            if not key:
                return _error(401, "missing or invalid API key", rid, "authentication_error")
            wait = key_buckets.take(key.get("id") or key["digest"][:16])
            if wait:
                resp = _error(429, "rate limit exceeded for this API key", rid, "rate_limit_error")
                resp.headers["Retry-After"] = str(max(1, round(wait)))
                return resp
            request.state.key = key
        return await call_next(request)

    @app.get("/healthz")
    async def healthz():
        reg: Registry = state["registry"]
        return {"ok": True, "upstreams": len(reg.upstreams), "last_discovery": reg.last_discovery}

    @app.get("/v1/models")
    async def models():
        reg: Registry = state["registry"]
        table = reg.models()
        now = int(time.time())
        data = [{"id": m, "object": "model", "created": now, "owned_by": "aiops-gateway",
                 "replicas": len(ups), "healthy_replicas": sum(u.healthy for u in ups),
                 "max_model_len": max((u.max_model_len or 0 for u in ups), default=None) or None}
                for m, ups in sorted(table.items())]
        data += [{"id": a, "object": "model", "created": now, "owned_by": "aiops-gateway", "alias_for": targets,
                  "healthy_replicas": sum(u.healthy for t in targets for u in table.get(t, []))}
                 for a, targets in config.MODEL_ALIASES.items()]
        return {"object": "list", "data": data}

    @app.get("/gateway/stats")
    async def stats():
        reg: Registry = state["registry"]
        ups = sorted(reg.upstreams.values(), key=lambda u: (u.model, u.node))
        return {"uptime_s": round(time.time() - state["started"]), "last_discovery": reg.last_discovery,
                "discovery_error": reg.discovery_error, "aliases": config.MODEL_ALIASES,
                "upstreams": [u.view() for u in ups]}

    @app.get("/metrics")
    async def metrics():
        reg: Registry = state["registry"]
        lines = ["# TYPE aiops_gateway_requests_total counter", "# TYPE aiops_gateway_errors_total counter",
                 "# TYPE aiops_gateway_inflight gauge", "# TYPE aiops_gateway_up gauge",
                 "# TYPE aiops_gateway_latency_seconds summary", "# TYPE aiops_gateway_ttft_seconds summary",
                 "# TYPE aiops_gateway_tokens_total counter"]
        now = time.monotonic()
        for u in reg.upstreams.values():
            lab = f'model="{u.model}",node="{u.node}",upstream="{u.base_url}"'
            s = u.stats
            lines += [f"aiops_gateway_requests_total{{{lab}}} {s.requests}",
                      f"aiops_gateway_errors_total{{{lab}}} {s.errors}",
                      f"aiops_gateway_inflight{{{lab}}} {u.inflight}",
                      f"aiops_gateway_up{{{lab}}} {int(u.available(now))}",
                      f'aiops_gateway_tokens_total{{{lab},type="prompt"}} {s.prompt_tokens}',
                      f'aiops_gateway_tokens_total{{{lab},type="completion"}} {s.completion_tokens}']
            for name, xs in (("latency", s.latency), ("ttft", s.ttft)):
                if xs:
                    srt = sorted(xs)
                    for q in (0.5, 0.95, 0.99):
                        lines.append(f'aiops_gateway_{name}_seconds{{{lab},quantile="{q}"}} '
                                     f"{srt[min(len(srt) - 1, int(q * len(srt)))]:.4f}")
        lines += ["# TYPE aiops_gateway_audio_input_seconds_total counter", "# TYPE aiops_gateway_asr_rtf histogram"]
        for u in reg.upstreams.values():
            s = u.stats
            if not s.rtf_n:
                continue
            lab = f'model="{u.model}",node="{u.node}"'
            lines.append(f"aiops_gateway_audio_input_seconds_total{{{lab}}} {s.audio_seconds:.3f}")
            lines += [f'aiops_gateway_asr_rtf_bucket{{{lab},le="{le}"}} {n}' for le, n in zip(RTF_BUCKETS, s.rtf_counts)]
            lines += [f'aiops_gateway_asr_rtf_bucket{{{lab},le="+Inf"}} {s.rtf_n}',
                      f"aiops_gateway_asr_rtf_sum{{{lab}}} {s.rtf_sum:.4f}", f"aiops_gateway_asr_rtf_count{{{lab}}} {s.rtf_n}"]
        lines += ["# TYPE aiops_gateway_client_requests_total counter", "# TYPE aiops_gateway_failovers_total counter"]
        lines += [f'aiops_gateway_client_requests_total{{client="{_label(c)}",model="{_label(m)}",outcome="{o}"}} {n}'
                  for (c, m, o), n in CLIENT_REQUESTS.items()]
        lines += [f'aiops_gateway_failovers_total{{model="{_label(m)}"}} {n}' for m, n in FAILOVERS.items()]
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.post("/v1/{path:path}")
    async def proxy(path: str, request: Request):
        reg: Registry = state["registry"]
        c: httpx.AsyncClient = state["client"]
        rid = request.state.rid
        started = time.time()
        size = int(request.headers.get("content-length") or 0)
        if size > MAX_BODY_BYTES:                      # bodies are buffered (alias rewrite, multipart): cap them
            return _error(413, f"request body over {MAX_BODY_BYTES // 2**20} MiB", rid, "request_too_large")
        body = await _read_body(request)
        model = body.model
        client = (getattr(request.state, "key", None) or {}).get("name") or "unnamed"
        if not model:
            return _error(400, "request must name a model", rid, "invalid_request_error")
        groups = reg.resolve(model)
        if groups is None or not any(groups):
            CLIENT_REQUESTS[(client, model, "not_found")] += 1
            requests_log.add(id=rid, t=started, client=client, requested=model, endpoint=endpoint(path), status=404,
                             outcome="not_found", latency_s=round(time.time() - started, 4), attempts=0)
            return _error(404, f"model '{model}' is not served by any reachable replica", rid, "model_not_found")

        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        headers["x-request-id"] = rid
        tried: set[str] = set()
        attempts, last_err = 0, "no healthy replica"
        priority = client in config.GATEWAY_PRIORITY_CLIENTS
        deadline = time.monotonic() + (config.GATEWAY_QUEUE_WAIT_S if priority else config.GATEWAY_SHARED_QUEUE_WAIT_S)

        for group in groups:
            while attempts < config.GATEWAY_MAX_ATTEMPTS:
                up = reg.pick(group, tried, priority)
                if up is None:
                    # all replicas of this model busy (not failing): wait for a slot, bounded
                    if reg.has_capacity_later(group, tried) and time.monotonic() < deadline:
                        await reg.released(deadline - time.monotonic())
                        continue
                    break
                tried.add(up.key)
                attempts += 1
                # the upstream only knows its own model id (aliases are resolved here)
                payload, ctype = await body.for_model(up.model)
                resp, t0 = await _send(c, up, request.method, path, request.url.query,
                                       headers | {"content-type": ctype}, payload, shared=not priority)
                if isinstance(resp, str):
                    last_err = resp
                    continue
                CLIENT_REQUESTS[(client, model, "served")] += 1
                if attempts > 1:
                    FAILOVERS[model] += 1
                return _relay(reg, up, resp, t0, rid, model, shared=not priority, path=path,
                              meta={"t": started, "client": client, "attempts": attempts})
        CLIENT_REQUESTS[(client, model, "unavailable")] += 1
        requests_log.add(id=rid, t=started, client=client, requested=model, endpoint=endpoint(path), status=503,
                         outcome="unavailable", latency_s=round(time.time() - started, 4), attempts=attempts)
        return _error(503, f"no replica could serve '{model}' ({last_err})", rid, "service_unavailable")

    async def _send(c, up: Upstream, method, path, query, headers, payload, shared: bool = False):
        """Open the upstream response. Returns (response, t0) or (error string, t0) after
        recording the failure; a returned response holds an in-flight slot."""
        up.inflight += 1
        up.shared += shared
        up.stats.requests += 1
        t0 = time.monotonic()
        url = f"{up.base_url}/v1/{path}" + (f"?{query}" if query else "")
        try:
            try:
                resp = await c.send(c.build_request(method, url, headers=headers, content=payload, timeout=TIMEOUT),
                                    stream=True)
            except httpx.RemoteProtocolError:
                # a pooled connection the server had just closed: it never saw the request, so sending it again on a
                # fresh connection is safe, and the server is not at fault (no breaker, no failover)
                resp = await c.send(c.build_request(method, url, headers=headers, content=payload, timeout=TIMEOUT),
                                    stream=True)
        except httpx.HTTPError as e:
            up.stats.errors += 1
            up.failed(f"{type(e).__name__}: {e}")
            await state["registry"].release(up, shared)
            log.warning("upstream %s failed: %s", up.key, e)
            return f"{up.node}: {type(e).__name__}", t0
        if resp.status_code in RETRYABLE_STATUS:
            await resp.aclose()
            up.stats.errors += 1
            up.failed(f"HTTP {resp.status_code}")
            await state["registry"].release(up, shared)
            return f"{up.node}: HTTP {resp.status_code}", t0
        up.succeeded()
        return resp, t0

    def _relay(reg: Registry, up: Upstream, resp: httpx.Response, t0: float, rid: str, asked: str, shared: bool = False,
               path: str = "", meta: dict | None = None):
        async def body():
            first = True
            tail = b""
            ttft = None
            cut = False
            tokens = (0, 0)
            audio = None
            try:
                async for chunk in resp.aiter_bytes():    # decoded, so content-encoding is dropped below
                    if first:
                        ttft = time.monotonic() - t0
                        up.stats.ttft.append(ttft)
                        first = False
                    tail = (tail + chunk)[-TAIL_BYTES:]
                    yield chunk
            except httpx.HTTPError as e:      # mid-stream failure: cannot retry, client sees a cut stream
                cut = True
                up.stats.errors += 1
                up.failed(f"stream: {type(e).__name__}")
            finally:
                await resp.aclose()
                elapsed = time.monotonic() - t0
                up.stats.latency.append(elapsed)
                if resp.status_code < 400:
                    tokens = _count_usage(up.stats, tail)
                    if path.endswith(_ASR_PATHS):
                        m = _AUDIO_SECONDS.search(tail)
                        if m and float(m.group(1)) > 0:
                            audio = float(m.group(1))
                            up.stats.observe_rtf(audio, elapsed)
                if 400 <= resp.status_code < 500:
                    up.stats.client_errors += 1
                elif resp.status_code >= 500:
                    up.stats.errors += 1
                await reg.release(up, shared)
                m = meta or {}
                requests_log.add(
                    id=rid, t=m.get("t", time.time() - elapsed), client=m.get("client"), requested=asked, model=up.model,
                    node=up.node, endpoint=endpoint(path), status=resp.status_code,
                    outcome="cut" if cut else "ok" if resp.status_code < 400 else "client_error" if resp.status_code < 500 else "error",
                    latency_s=round(elapsed, 4), ttft_s=round(ttft, 4) if ttft is not None else None,
                    prompt_tokens=tokens[0], completion_tokens=tokens[1], audio_s=audio, attempts=m.get("attempts", 1))

        out = {k: v for k, v in resp.headers.items() if k.lower() not in HOP and k.lower() != "content-encoding"}
        out.update({"x-request-id": rid, "x-aiops-model": up.model, "x-aiops-node": up.node,
                    "x-aiops-upstream": up.base_url, "x-aiops-requested": asked})
        return StreamingResponse(body(), status_code=resp.status_code, headers=out)

    return app


app = create_app()

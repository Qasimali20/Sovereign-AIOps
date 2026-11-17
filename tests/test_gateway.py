"""Model gateway routing, failover and security, against simulated upstreams."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.core import config
from backend.llmops.gateway import create_app, keys
from backend.llmops.gateway.registry import Upstream
from backend.web import security


class Upstreams:
    """A fake fleet: base_url -> behaviour ('ok' | 'down' | 503 | 400 | 'stream')."""

    def __init__(self, **behaviour):
        self.behaviour = behaviour
        self.calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = f"{request.url.scheme}://{request.url.host}:{request.url.port}"
        b = self.behaviour[host]
        body = json.loads(request.content or b"{}")
        self.calls.append((host, body))
        if b == "down":
            raise httpx.ConnectError("refused", request=request)
        if isinstance(b, int):
            return httpx.Response(b, json={"error": {"message": f"status {b}"}})
        if b == "stream":
            sse = b"".join(f"data: {json.dumps({'choices': [{'delta': {'content': t}}]})}\n\n".encode() for t in ("Hel", "lo"))
            return httpx.Response(200, content=sse + b"data: [DONE]\n\n", headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"model": body.get("model"), "served_by": host,
                                         "choices": [{"message": {"content": "ok"}}]})


@pytest.fixture
def gw(tmp_path, monkeypatch):
    monkeypatch.setattr(keys, "PATH", tmp_path / "keys.json")
    monkeypatch.setattr(security, "client_allowed", lambda h: True)
    key, _ = keys.create("test", "pytest")

    def make(ups: Upstreams, table: list[tuple[str, str, str]]):
        app = create_app(httpx.AsyncClient(transport=httpx.MockTransport(ups.handler)), background=False)
        c = TestClient(app)
        c.__enter__()
        reg = app.state.gw["registry"]
        for node, model, url in table:
            u = Upstream(node, model, url, healthy=True)
            reg.upstreams[u.key] = u
        c.headers["authorization"] = f"Bearer {key}"
        return c, reg

    return make


def chat(c, model="m", stream=False):
    return c.post("/v1/chat/completions", json={"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": stream})


def test_requires_api_key(gw):
    c, _ = gw(Upstreams(**{"http://a:1": "ok"}), [("N1", "m", "http://a:1")])
    assert c.post("/v1/chat/completions", json={"model": "m"}, headers={"authorization": "Bearer nope"}).status_code == 401
    del c.headers["authorization"]
    assert c.get("/v1/models").status_code == 401
    assert c.get("/healthz").status_code == 200


def test_unknown_model_404(gw):
    c, _ = gw(Upstreams(**{"http://a:1": "ok"}), [("N1", "m", "http://a:1")])
    assert chat(c, "nope").status_code == 404


def test_routes_and_reports_upstream(gw):
    c, _ = gw(Upstreams(**{"http://a:1": "ok"}), [("N1", "m", "http://a:1")])
    r = chat(c)
    assert r.status_code == 200 and r.headers["x-aiops-node"] == "N1" and r.headers["x-request-id"]


def test_failover_on_connection_error_and_breaker_opens(gw):
    ups = Upstreams(**{"http://a:1": "down", "http://b:1": "ok"})
    c, reg = gw(ups, [("N1", "m", "http://a:1"), ("N2", "m", "http://b:1")])
    for _ in range(4):
        assert chat(c).json()["served_by"] == "http://b:1"
    down = reg.upstreams["m@http://a:1"]
    assert down.fails == 1 and down.view()["state"] == "backoff"
    assert [h for h, _ in ups.calls].count("http://a:1") == 1     # not hammered while in backoff


def test_stale_keepalive_connection_is_retried_without_tripping_the_breaker(gw):
    """A pooled connection the server closed while idle: resend once, same replica, no failure recorded."""
    ups = Upstreams(**{"http://a:1": "ok"})
    first = {"done": False}
    plain = ups.handler

    def handler(request):
        if not first["done"]:
            first["done"] = True
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
        return plain(request)
    ups.handler = handler
    c, reg = gw(ups, [("N1", "m", "http://a:1")])
    r = chat(c)
    up = reg.upstreams["m@http://a:1"]
    assert r.status_code == 200 and r.json()["served_by"] == "http://a:1"
    assert up.fails == 0 and up.stats.errors == 0 and up.view()["state"] != "backoff"


def test_503_is_retried_400_is_not(gw):
    c, _ = gw(Upstreams(**{"http://a:1": 503, "http://b:1": "ok"}), [("N1", "m", "http://a:1"), ("N2", "m", "http://b:1")])
    assert chat(c).status_code == 200
    ups = Upstreams(**{"http://a:1": 400, "http://b:1": "ok"})
    c2, _ = gw(ups, [("N1", "m", "http://a:1"), ("N2", "m", "http://b:1")])
    statuses = {chat(c2).status_code for _ in range(4)}
    assert 400 in statuses                                      # a bad request is the client's problem


def test_all_replicas_down_is_503(gw):
    c, _ = gw(Upstreams(**{"http://a:1": "down", "http://b:1": "down"}), [("N1", "m", "http://a:1"), ("N2", "m", "http://b:1")])
    r = chat(c)
    assert r.status_code == 503 and r.json()["error"]["type"] == "service_unavailable"


def test_alias_rewrites_model_and_falls_through_in_order(gw, monkeypatch):
    monkeypatch.setitem(config.MODEL_ALIASES, "chat", ["primary", "secondary"])
    ups = Upstreams(**{"http://a:1": "down", "http://b:1": "ok"})
    c, _ = gw(ups, [("N1", "primary", "http://a:1"), ("N2", "secondary", "http://b:1")])
    r = chat(c, "chat")
    assert r.status_code == 200 and r.headers["x-aiops-model"] == "secondary" and r.headers["x-aiops-requested"] == "chat"
    assert ups.calls[-1][1]["model"] == "secondary"


def test_alias_prefers_primary_when_healthy(gw, monkeypatch):
    monkeypatch.setitem(config.MODEL_ALIASES, "chat", ["primary", "secondary"])
    c, _ = gw(Upstreams(**{"http://a:1": "ok", "http://b:1": "ok"}), [("N1", "primary", "http://a:1"), ("N2", "secondary", "http://b:1")])
    assert {chat(c, "chat").headers["x-aiops-model"] for _ in range(5)} == {"primary"}


def test_streaming_passthrough_and_slots_released(gw):
    c, reg = gw(Upstreams(**{"http://a:1": "stream"}), [("N1", "m", "http://a:1")])
    r = chat(c, stream=True)
    assert r.status_code == 200 and r.text.count("data:") == 3 and r.text.endswith("[DONE]\n\n")
    u = reg.upstreams["m@http://a:1"]
    assert u.inflight == 0 and len(u.stats.ttft) == 1


def test_least_inflight_balancing(gw):
    c, reg = gw(Upstreams(**{"http://a:1": "ok", "http://b:1": "ok"}), [("N1", "m", "http://a:1"), ("N2", "m", "http://b:1")])
    reg.upstreams["m@http://a:1"].inflight = 5                 # a is busy
    assert {chat(c).json()["served_by"] for _ in range(3)} == {"http://b:1"}


def test_models_lists_models_and_aliases(gw):
    c, _ = gw(Upstreams(**{"http://a:1": "ok"}), [("N1", "qwen3.6-35b-a3b-fp8", "http://a:1")])
    ids = {m["id"]: m for m in c.get("/v1/models").json()["data"]}
    assert ids["qwen3.6-35b-a3b-fp8"]["healthy_replicas"] == 1 and ids["chat"]["healthy_replicas"] == 1


def test_key_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(keys, "PATH", tmp_path / "k.json")
    k, meta = keys.create("svc", "me")
    assert keys.verify(k)["id"] == meta["id"] and "digest" not in keys.list_keys()[0]
    assert keys.revoke(meta["id"]) and keys.verify(k) is None


def test_multipart_alias_is_rewritten(gw, monkeypatch):
    monkeypatch.setitem(config.MODEL_ALIASES, "asr", ["whisper"])
    seen = {}

    def handler(request: httpx.Request):
        seen["ctype"] = request.headers["content-type"]
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "ok"})

    from backend.llmops.gateway import create_app as mk
    app = mk(httpx.AsyncClient(transport=httpx.MockTransport(handler)), background=False)
    with TestClient(app) as c:
        reg = app.state.gw["registry"]
        u = Upstream("N1", "whisper", "http://a:1", healthy=True)
        reg.upstreams[u.key] = u
        c.headers["authorization"] = f"Bearer {keys.create('t', 't')[0]}"
        r = c.post("/v1/audio/transcriptions", data={"model": "asr", "language": "en"},
                   files={"file": ("a.wav", b"RIFF....WAVEdata", "audio/wav")})
    assert r.status_code == 200 and r.headers["x-aiops-model"] == "whisper"
    assert seen["ctype"].startswith("multipart/form-data; boundary=")
    assert b'name="model"\r\n\r\nwhisper' in seen["body"] and b"RIFF....WAVEdata" in seen["body"]
    assert b'name="language"\r\n\r\nen' in seen["body"]


def test_half_open_allows_one_trial_then_recovers():
    import time as _t
    u = Upstream("N1", "m", "http://a:1", healthy=True)
    u.failed("boom")
    assert not u.available(_t.monotonic()) and u.view()["state"] == "backoff"
    u.down_until = 0                                    # backoff elapsed
    assert u.available(_t.monotonic()) and u.view()["state"] == "half-open"
    u.inflight = 1                                      # the single trial request is in flight
    assert not u.available(_t.monotonic())
    u.inflight = 0
    u.succeeded()
    assert u.view()["state"] == "up" and u.fails == 0


def test_vector_aliases_never_mix_models():
    """Embedding/rerank fallback across different models would corrupt vector indexes."""
    for alias in config.SINGLE_MODEL_ALIASES:
        assert len(config.MODEL_ALIASES[alias]) == 1, alias


def test_per_model_capacity_cap(monkeypatch):
    import time as _t
    monkeypatch.setitem(config.GATEWAY_MAX_INFLIGHT_BY_MODEL, "small", 4)
    u = Upstream("N3", "small", "http://c:1", healthy=True)
    u.inflight = 3
    assert u.available(_t.monotonic())
    u.inflight = 4
    assert not u.available(_t.monotonic()) and u.view()["state"] == "saturated" and u.view()["capacity"] == 4


def test_token_usage_counted_for_json_and_stream():
    from backend.llmops.gateway import _count_usage
    from backend.llmops.gateway.registry import Stats
    s = Stats()
    _count_usage(s, b'{"choices":[],"usage":{"prompt_tokens":12,"total_tokens":20,"completion_tokens":8}}')
    sse = (b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
           b'data: {"choices":[],"usage":{"prompt_tokens":5,"total_tokens":9,"completion_tokens":4}}\n\ndata: [DONE]\n\n')
    _count_usage(s, sse)
    _count_usage(s, b'data: {"choices":[{"delta":{}}]}\n\n')          # no usage: nothing counted
    assert (s.prompt_tokens, s.completion_tokens) == (17, 12)


def test_usage_by_client_and_failovers_are_exported(gw):
    import backend.llmops.gateway as g
    g.CLIENT_REQUESTS.clear(); g.FAILOVERS.clear()
    ups = Upstreams(**{"http://a:1": "down", "http://b:1": "ok"})
    c, reg = gw(ups, [("N1", "m", "http://a:1"), ("N2", "m", "http://b:1")])
    chat(c); chat(c); chat(c, model="nope")
    body = c.get("/metrics").text
    assert 'outcome="served"} 2' in body and 'model="nope",outcome="not_found"} 1' in body
    assert 'aiops_gateway_failovers_total{model="m"} 1' in body          # only the first hit the dead replica


def test_priority_lane_caps_other_clients_but_not_the_agents(monkeypatch):
    """Other clients share at most N slots on a model; the platform's agents always get one."""
    import asyncio, time
    from backend.core import config
    from backend.llmops.gateway.registry import Registry
    monkeypatch.setattr(config, "GATEWAY_SHARED_INFLIGHT_BY_MODEL", {"m": 2})
    u = Upstream("N1", "m", "http://a:1", healthy=True)
    u.inflight = u.shared = 2                                   # two app requests already running
    now = time.monotonic()
    assert not u.available(now, priority=False)                 # a third app request waits at the gateway
    assert u.available(now, priority=True)                      # an agent request goes straight through
    reg = Registry.__new__(Registry); reg._rr = iter(range(10**6)); reg._changed = asyncio.Condition()
    assert reg.pick([u], set(), priority=False) is None and reg.pick([u], set(), priority=True) is u
    asyncio.run(reg.release(u, shared=True))
    assert (u.inflight, u.shared) == (1, 1) and u.available(time.monotonic(), priority=False)


def test_asr_real_time_factor_from_transcription_usage(gw):
    """The gateway derives ASR real-time factor (processing time / audio length) from usage.seconds."""
    class Asr(Upstreams):
        def handler(self, request):
            return httpx.Response(200, json={"text": "hello", "usage": {"type": "duration", "seconds": 8.0}})
    c, reg = gw(Asr(**{"http://a:1": "ok"}), [("N4", "whisper", "http://a:1")])
    r = c.post("/v1/audio/transcriptions", data={"model": "whisper"}, files={"file": ("a.wav", b"RIFF0000WAVE", "audio/wav")})
    assert r.status_code == 200
    s = next(iter(reg.upstreams.values())).stats
    assert s.rtf_n == 1 and s.audio_seconds == 8.0 and 0 < s.rtf_sum < 1
    body = c.get("/metrics").text
    assert 'aiops_gateway_audio_input_seconds_total{model="whisper",node="N4"} 8.000' in body
    assert 'aiops_gateway_asr_rtf_count{model="whisper",node="N4"} 1' in body
    assert 'aiops_gateway_asr_rtf_bucket{model="whisper",node="N4",le="+Inf"} 1' in body
    chat(c, model="whisper")                     # a non-transcription response never counts as audio
    assert s.rtf_n == 1


def test_oversized_body_is_refused_before_buffering(gw):
    import backend.llmops.gateway as g
    c, _ = gw(Upstreams(**{"http://a:1": "ok"}), [("N1", "m", "http://a:1")])
    r = c.post("/v1/chat/completions", content=b"{}", headers={"content-type": "application/json",
                                                              "content-length": str(g.MAX_BODY_BYTES + 1)})
    assert r.status_code == 413


def test_request_log_records_metadata_never_content(tmp_path, monkeypatch):
    """Every answered request leaves one row (served, not found, unavailable) and the prompt text is never stored."""
    from backend.llmops.requestlog import RequestLog, connect
    monkeypatch.setattr(keys, "PATH", tmp_path / "keys.json")
    monkeypatch.setattr(security, "client_allowed", lambda h: True)
    key, _ = keys.create("team-a", "pytest")
    log = RequestLog(tmp_path / "req.db")
    ups = Upstreams(**{"http://a:1": "ok", "http://b:1": "down"})
    app = create_app(httpx.AsyncClient(transport=httpx.MockTransport(ups.handler)), background=False, request_log=log)
    with TestClient(app) as c:
        reg = app.state.gw["registry"]
        for node, model, url in (("N1", "m", "http://a:1"), ("N2", "dead", "http://b:1")):
            reg.upstreams[f"{model}@{url}"] = Upstream(node, model, url, healthy=True)
        c.headers["authorization"] = f"Bearer {key}"
        secret = "SECRET-PROMPT-TEXT-1234"
        assert c.post("/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": secret}]}).status_code == 200
        assert c.post("/v1/chat/completions", json={"model": "nope", "messages": []}).status_code == 404
        assert c.post("/v1/chat/completions", json={"model": "dead", "messages": []}).status_code == 503
    assert log.flush() == 3
    rows = {r["requested"]: dict(r) for r in connect(tmp_path / "req.db").execute("SELECT * FROM requests")}
    ok = rows["m"]
    assert ok["outcome"] == "ok" and ok["status"] == 200 and ok["client"] == "team-a" and ok["model"] == "m"
    assert ok["node"] == "N1" and ok["endpoint"] == "chat" and ok["latency_s"] >= 0 and ok["ttft_s"] is not None
    assert rows["nope"]["outcome"] == "not_found" and rows["dead"]["outcome"] == "unavailable"
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("req.db*"))
    assert secret.encode() not in raw and key.encode() not in raw

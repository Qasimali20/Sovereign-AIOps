"""LLMOps read side: request summaries from the gateway's request log, and GPU energy attributed to models."""
import time

import pytest

from backend.llmops import summary as llmops
from backend.llmops import requestlog


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setattr(requestlog, "PATH", tmp_path / "req.db")
    rl = requestlog.RequestLog(tmp_path / "req.db")
    now = time.time()
    rows = [  # model, node, outcome, latency, tokens (p, c), client
        ("chat-a", "N1", "ok", 2.0, (100, 50), "team"), ("chat-a", "N1", "ok", 4.0, (100, 150), "team"),
        ("chat-a", "N1", "error", 0.5, (0, 0), "team"), ("asr-b", "N1", "ok", 2.0, (0, 0), "agent"),
        ("embed-c", "N2", "ok", 1.0, (40, 0), "agent"),
    ]
    for i, (m, n, o, lat, (p, c), cl) in enumerate(rows):
        rl.add(id=f"r{i}", t=now - 60 * i, client=cl, requested=m, model=m, node=n, endpoint="chat", status=200 if o == "ok" else 502,
               outcome=o, latency_s=lat, ttft_s=lat / 4, prompt_tokens=p, completion_tokens=c, attempts=1)
    rl.add(id="old", t=now - 3 * 86400, client="team", requested="chat-a", model="chat-a", node="N1", outcome="ok", latency_s=1)
    rl.flush()
    return now


def test_summary_groups_by_model_and_client_within_the_range(log):
    s = llmops.summary("24h", log)
    assert s["total"]["requests"] == 5 and s["total"]["errors"] == 1 and s["total"]["error_rate"] == 0.2
    a = next(m for m in s["by_model"] if m["model"] == "chat-a")
    assert a["requests"] == 3 and a["errors"] == 1 and a["completion_tokens"] == 200 and a["busy_s"] == 6.0
    assert {c["client"]: c["requests"] for c in s["by_client"]} == {"team": 3, "agent": 2}
    assert llmops.summary("7d", log)["total"]["requests"] == 6                 # the 3-day-old row only in 7 d


def test_request_filters(log):
    assert len(llmops.requests("24h", model="chat-a")) == 3
    assert [r["outcome"] for r in llmops.requests("24h", outcome="errors")] == ["error"]
    assert {r["client"] for r in llmops.requests("24h", client="agent")} == {"agent"}


def test_energy_is_shared_by_time_answering_and_divided_by_tokens(log, monkeypatch):
    # the log covers the last 4 minutes (rows at 0..240 s ago) -> a 240 s window, 1/15 h
    # N1: 100 W average, 20 W idle -> 80 W x 1/15 h = 5.333 Wh active; N2: 30 W avg, 10 W idle -> 1.333 Wh
    seen = []
    monkeypatch.setattr(llmops, "_prom", lambda e: seen.append(e) or ({"N1": 100.0, "N2": 30.0} if "avg_over_time" in e else {"N1": 20.0, "N2": 10.0}))
    e = llmops.energy("24h", log)
    assert all("[240s]" in q for q in seen) and e["hours"] == round(240 / 3600, 3)   # not the whole 24 h
    assert {n["node"]: n["active_wh"] for n in e["nodes"]} == {"N1": 5.3, "N2": 1.3}
    m = {x["model"]: x for x in e["models"]}
    assert m["chat-a"]["share"] == 0.75 and m["chat-a"]["wh"] == round(0.75 * 5.3, 2)   # 6 s of N1's 8 busy seconds
    assert m["chat-a"]["wh_per_1k_tokens"] == pytest.approx(1000 * 0.75 * 5.3 / 400, abs=1e-3)   # successful requests' tokens only
    assert m["embed-c"]["wh"] == 1.3 and m["asr-b"]["wh_per_1k_tokens"] is None


def test_no_log_file_yet_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(requestlog, "PATH", tmp_path / "missing.db")
    assert llmops.summary("1h")["total"]["requests"] == 0 and llmops.requests("1h") == []

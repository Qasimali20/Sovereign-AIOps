"""LLMOps model registry: versions by identity, restarts counted not versioned, eval scores before and after."""
import time

import pytest

from backend.llmops import model_registry as mr
from backend.core import store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init()
    mr._init()


S = {"name": "chat", "port": 8001, "kind": "LLM", "engine": "vllm", "started": 1000.0}
P = {"engine_version": "0.23.1", "root": "/models/chat-v1", "max_model_len": 8192}


def test_same_identity_is_one_version_and_a_restart_is_counted(db):
    assert mr.observe("N1", S, P, 1) == "new"
    assert mr.observe("N1", S | {"engine": "vllm-omni"}, P, 2) == "same"        # engine label flapping is not a change
    assert mr.observe("N1", S | {"started": 2000.0}, P, 3) == "restarted"
    [v] = mr.history("N1", 8001, "chat")
    assert v["restarts"] == 1 and v["last_seen"] == 3


def test_identity_change_is_a_new_version_with_the_differences_audited(db):
    mr.observe("N1", S, P, time.time() - 100)
    assert mr.observe("N1", S, P | {"root": "/models/chat-v2", "engine_version": "0.24.0"}, time.time()) == "changed"
    hist = mr.history("N1", 8001, "chat")
    assert len(hist) == 2 and hist[0]["root"] == "/models/chat-v2"
    [ch] = mr.changes()
    assert ch["data"]["changes"] == {"engine_version": ["0.23.1", "0.24.0"], "root": ["/models/chat-v1", "/models/chat-v2"]}


def test_evaluations_are_split_at_the_version_change(db):
    t0 = time.time() - 1000
    mr.observe("N1", S, P, t0)
    with store.conn() as c:
        for t, score in ((t0 + 10, 1.0), (t0 + 20, 0.8), (t0 + 500, 0.4)):
            c.execute("INSERT INTO model_evals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (store.new_id("EVAL"), "r", t, "chat", "chat", "N1", score, 1, 1, 1.0, None, "[]"))
    mr.observe("N1", S, P | {"root": "/models/chat-v2"}, t0 + 400)
    [m] = mr.models()
    assert m["versions"] == 2 and not m["first_known"]
    assert m["evals_before"] == {"runs": 2, "mean": 0.9} and m["evals_since"] == {"runs": 1, "mean": 0.4}


def test_first_known_version_counts_all_earlier_evaluations(db):
    with store.conn() as c:
        c.execute("INSERT INTO model_evals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (store.new_id("EVAL"), "r", time.time() - 999, "chat", "chat", "N1", 1.0, 1, 1, 1.0, None, "[]"))
    mr.observe("N1", S, P, time.time())
    [m] = mr.models()
    assert m["first_known"] and m["evals_since"]["runs"] == 1 and m["evals_before"] is None


def test_unknown_is_not_a_change_and_a_new_fact_fills_in(db):
    mr.observe("N1", S, P, 1)
    assert mr.observe("N1", S, {}, 2) == "same"                                   # probe timed out: nothing known, nothing changed
    assert mr.observe("N1", S, P | {"digest": "abc123"}, 3) == "same"             # first time the digest is visible
    [v] = mr.history("N1", 8001, "chat")
    assert v["digest"] == "abc123" and v["root"] == "/models/chat-v1"
    assert mr.observe("N1", S, P | {"digest": "def456"}, 4) == "changed"          # a known digest that differs is a change

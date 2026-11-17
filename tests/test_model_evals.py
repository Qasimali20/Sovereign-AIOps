"""LLMOps quality evaluations: scoring functions, run bookkeeping, regression alerts and their recovery."""
import pytest

from backend.llmops import model_evals as me
from backend.incidents import runtime
from backend.core import store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init()


def test_error_rates():
    assert me.wer("the backup finished on time", "The backup finished on time.") == 0
    assert me.wer("a b c d", "a x c") == 0.5                      # one substitution, one deletion over 4 words
    assert me.cer("Total 741.60", "Total 741.80") == pytest.approx(1 / 12)


@pytest.mark.parametrize("name,answer,ok", [
    ("arithmetic", "391", True), ("arithmetic", "The answer is 391", False),
    ("exact word", "READY.", True), ("JSON extraction", '```json\n{"node": "Node-3", "temp_c": 81}\n```', True),
    ("JSON extraction", '{"node": "Node-3", "temp_c": "eighty-one"}', False), ("sorting", "2, 4, 7, 9", True),
])
def test_chat_checks(name, answer, ok):
    check = next(c for n, _, c in me.CHAT_CASES if n == name)
    assert bool(check(me._strip_think(f"<think>reasoning</think>{answer}"))) is ok


def _fake_run(monkeypatch, scores: dict[str, float], fail: set[str] = frozenset()):
    def runner(c, model):
        if model in fail:
            raise RuntimeError("upstream down")
        return [{"case": "x", "ok": scores[model] >= 0.5, "score": scores[model]}], "Node-2"
    monkeypatch.setattr(me, "RUNNERS", {s: runner for s in me.SUITES})
    monkeypatch.setattr(me, "SUITES", {"chat": list(scores)})
    monkeypatch.setattr(me, "api_key", lambda: "k")


def test_run_records_every_model_and_a_failure_scores_zero(db, monkeypatch):
    _fake_run(monkeypatch, {"a": 1.0, "b": 0.9}, fail={"b"})
    s = me.run_all("test")
    assert s["models"] == 2 and s["failed"] == ["b"]
    rows = {r["model"]: r for r in me.latest()}
    assert rows["a"]["score"] == 1.0 and rows["b"]["score"] == 0 and rows["b"]["error"].startswith("RuntimeError")
    assert not rows["b"]["regressed"]                              # unreachable is the detector's job, not a quality drop


def test_a_clear_drop_against_the_models_own_baseline_alerts_then_recovers(db, monkeypatch):
    for score in (1.0, 1.0, 0.95):
        _fake_run(monkeypatch, {"a": score})
        me.run_all("test")
    assert me.regression_alerts() == []
    _fake_run(monkeypatch, {"a": 0.6})                           # 0.6 < 0.75 floor and 0.4 below the 1.0 median
    me.run_all("test")
    [alert] = me.regression_alerts()
    assert alert.kind == "quality_regression" and alert.resource == "a" and alert.detail["baseline"] == 1.0
    _fake_run(monkeypatch, {"a": 0.95})
    me.run_all("test")
    assert me.regression_alerts() == []


def test_a_small_dip_or_a_model_always_mediocre_does_not_alert(db, monkeypatch):
    for score in (0.83, 0.83, 0.67):                             # bge-m3-like: never great, small dip
        _fake_run(monkeypatch, {"a": score})
        me.run_all("test")
    assert me.regression_alerts() == []


def test_quality_regression_clears_only_when_no_longer_firing():
    class Det:
        reachable = {"Node-2": True}
    inc = {"fingerprint": "quality_regression:Node-2:a", "kind": "quality_regression", "node": "Node-2", "resource": "a"}
    assert runtime.condition_cleared(inc, Det(), {}, {inc["fingerprint"]}) is None
    assert "back to its usual level" in runtime.condition_cleared(inc, Det(), {}, set())


def test_only_one_run_at_a_time(db, monkeypatch):
    _fake_run(monkeypatch, {"a": 1.0})
    me._run_lock.acquire()
    try:
        assert me.run_all("test") is None and me.running()
    finally:
        me._run_lock.release()

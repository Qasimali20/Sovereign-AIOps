"""Reboot detection, post-boot grace, self-recovery and model-container de-duplication."""
import pytest

from backend.core import config, store
from backend.incidents import detector, runtime
from backend.monitoring.services import ServiceAlert

# fleet addresses come from .env (placeholders without it)
N3 = config.NODE_BY_NAME["Node-3"].lan_ip

N = config.NODE_BY_NAME["Node-3"]
NOW = 2_000_000_000.0


def srv(name, port, status="ready"):
    return {"name": name, "port": port, "status": status}


def test_reboot_alert_only_when_uptime_goes_backwards():
    assert detector.reboot_alert(N, {"uptime_s": 500}, None, NOW) == []          # first read: no baseline
    assert detector.reboot_alert(N, {"uptime_s": 5000}, 4980, NOW) == []         # normal tick
    (a,) = detector.reboot_alert(N, {"uptime_s": 120}, 24_000, NOW)
    assert a.kind == "node_rebooted" and a.node == "Node-3" and a.detail["previous_uptime_s"] == 24_000
    # the same boot seen a scan later keeps the same fingerprint (one incident per boot)
    assert a.fingerprint == detector.reboot_alert(N, {"uptime_s": 140}, 24_000, NOW + 21)[0].fingerprint


def test_missing_models_held_during_boot_grace_then_alert():
    seen = {"qwen-chat@8103"}
    booting = {"uptime_s": 300, "vllm": []}
    assert detector.missing_model_alerts(N, booting, seen, "t") == []
    later = {"uptime_s": config.REBOOT_GRACE_S + 60, "vllm": []}
    (a,) = detector.missing_model_alerts(N, later, seen, "t")
    assert a.kind == "model_server_missing" and a.resource == "qwen-chat@8103"


def test_missing_model_on_long_running_node_alerts_immediately():
    seen = set()
    detector.missing_model_alerts(N, {"uptime_s": 90_000, "vllm": [srv("qwen-chat", 8103)]}, seen, "t")
    (a,) = detector.missing_model_alerts(N, {"uptime_s": 90_020, "vllm": []}, seen, "t")
    assert a.resource == "qwen-chat@8103"


def _det(reachable=True, present=()):
    d = detector.DetectorState()
    d.reachable["Node-3"] = reachable
    d.present["Node-3"] = set(present)
    return d


def inc(kind, resource, fp=None):
    return {"kind": kind, "node": "Node-3", "resource": resource, "fingerprint": fp or f"{kind}:Node-3:{resource}"}


def test_cleared_needs_positive_evidence():
    svc = {"Node-3": {"ok": True, "containers": [{"name": "qwen-embed", "state": "running", "health": None}],
                     "problem_units": [{"unit": "foo.service"}]}}
    # node back
    assert runtime.condition_cleared(inc("node_unreachable", f"{N3}"), _det(), svc, set())
    assert not runtime.condition_cleared(inc("node_unreachable", f"{N3}"), _det(False), svc, set())
    # model: absence of the alert is not enough, the model must be serving
    assert not runtime.condition_cleared(inc("model_server_missing", "qwen-chat@8103"), _det(), svc, set())
    assert runtime.condition_cleared(inc("model_server_missing", "qwen-chat@8103"), _det(present=["qwen-chat@8103"]), svc, set())
    # still firing never clears
    i = inc("model_server_missing", "qwen-chat@8103")
    assert not runtime.condition_cleared(i, _det(present=["qwen-chat@8103"]), svc, {i["fingerprint"]})
    # containers and units
    assert runtime.condition_cleared(inc("svc_container_stopped", "qwen-embed"), _det(), svc, set())
    assert not runtime.condition_cleared(inc("svc_container_stopped", "gone"), _det(), svc, set())
    assert not runtime.condition_cleared(inc("svc_unit_failed", "foo.service"), _det(), svc, set())
    assert runtime.condition_cleared(inc("svc_unit_failed", "bar.service"), _det(), svc, set())
    # unreadable services snapshot: no verdict
    assert not runtime.condition_cleared(inc("svc_unit_failed", "bar.service"), _det(), {"Node-3": {"ok": False}}, set())


def test_model_container_detection():
    svc = {"Node-3": {"containers": [{"name": "qwen3-30b", "image": "vllm/vllm-openai", "state": "exited"},
                                    {"name": "postgres", "image": "postgres:16", "state": "exited"}]}}
    mk = lambda r: ServiceAlert("svc_container_stopped", "Node-3", r, "", {})
    assert runtime.is_model_container(mk("qwen3-30b"), svc)
    assert not runtime.is_model_container(mk("postgres"), svc)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init()


def test_a_quick_recurrence_is_a_new_incident_chained_to_the_last(db):
    """Flapping: each occurrence gets its own incident (own evidence/approval/execution), chained to the previous one,
    which is marked merged so lists show the problem once with its recurrence count."""
    iid, created = store.open_incident("node_unreachable", "Node-3", "h", "down", "fp1", {})
    assert created and [i["id"] for i in store.open_incidents_of(("node_unreachable",))] == [iid]
    assert store.mark_recovered(iid, NOW, "monitor answering again")
    assert store.get_incident(iid)["status"] == "recovered"
    assert not store.mark_recovered(iid, NOW, "again")                       # compare-and-set
    assert store.open_incidents_of(("node_unreachable",)) == []
    iid2, created2 = store.open_incident("node_unreachable", "Node-3", "h", "down", "fp1", {})
    new, old = store.get_incident(iid2), store.get_incident(iid)
    assert created2 and iid2 != iid
    assert (new["occurrences"], new["recurrence_of"], new["status"]) == (2, iid, "open")
    assert (old["origin"], old["status"]) == ("merged", "recovered")
    assert store.evidence_ids(iid2) == set()                                  # nothing carried over


def test_state_change_and_its_audit_commit_together(db):
    iid, _ = store.open_incident("node_unreachable", "Node-3", "h", "down", "fp-tx", {})
    assert store.set_status(iid, "closed", actor="human:alice")
    assert not store.set_status(iid, "investigating", keep_final=True)        # a late workflow write cannot reopen it
    assert store.get_incident(iid)["status"] == "closed"
    events = [e["event"] for e in store.audit_log(limit=20, incident_id=iid)]
    assert events.count("status") == 1 and "incident_opened" in events
    assert store.verify_audit_chain()["ok"]


def test_a_late_recurrence_opens_a_new_incident(db, monkeypatch):
    iid, _ = store.open_incident("node_unreachable", "Node-3", "h", "down", "fp1b", {})
    store.mark_recovered(iid, NOW, "ok")
    monkeypatch.setattr(store, "FLAP_WINDOW_S", -1)                          # the old one is outside the window
    iid2, created2 = store.open_incident("node_unreachable", "Node-3", "h", "down", "fp1b", {})
    assert created2 and iid2 != iid


def test_origin_drill_planned_real(db):
    lab, _ = store.open_incident("sec_bruteforce", "LAB", "203.0.113.5", "t", "fp-o1", {})
    svc, _ = store.open_incident("service_unhealthy", "Node-2", "aiops-test-svc", "t", "fp-o2", {})
    real, _ = store.open_incident("gpu_stalled", "Node-4", "h4", "t", "fp-o3", {})
    store.add_maintenance("Node-1", 30, "load test", "human:alice")
    planned, _ = store.open_incident("model_latency_high", "Node-1", "m@1", "t", "fp-o4", {})
    other, _ = store.open_incident("model_latency_high", "Node-3", "m@1", "t", "fp-o5", {})
    origins = {i: store.get_incident(i)["origin"] for i in (lab, svc, real, planned, other)}
    assert origins == {lab: "drill", svc: "drill", real: "real", planned: "planned", other: "real"}


def test_rejected_incident_does_not_swallow_a_recurrence(db):
    iid, _ = store.open_incident("service_unhealthy", "Node-2", "svc", "t", "fp2", {})
    store.set_status(iid, "action_rejected")
    iid2, created = store.open_incident("service_unhealthy", "Node-2", "svc", "t", "fp2", {})
    assert created and iid2 != iid


def test_sustained_rules_need_consecutive_scans(monkeypatch):
    monkeypatch.setattr(config, "SUSTAIN_SCANS", 3)
    streak = {}
    hot = {"cpu": {"percent": 97.0}, "gpu": {"util": 0.0}, "vllm": [{"name": "m", "port": 1, "running": 2}]}
    assert detector.sustained_alerts(N, hot, streak, "t") == []
    assert detector.sustained_alerts(N, hot, streak, "t") == []
    kinds = {a.kind for a in detector.sustained_alerts(N, hot, streak, "t")}
    assert kinds == {"high_cpu", "gpu_stalled"}
    # one normal reading resets the streak
    detector.sustained_alerts(N, {"cpu": {"percent": 10}, "gpu": {"util": 50}, "vllm": []}, streak, "t")
    assert detector.sustained_alerts(N, hot, streak, "t") == []


def test_gpu_idle_without_work_is_not_a_stall(monkeypatch):
    monkeypatch.setattr(config, "SUSTAIN_SCANS", 1)
    idle = {"gpu": {"util": 0.0}, "vllm": [{"name": "m", "port": 1, "running": 0}]}
    assert detector.sustained_alerts(N, idle, {}, "t") == []


def _tts(total, running, waiting):
    return {"gpu": {"util": 0.0}, "vllm": [{"name": "tts", "port": 8091, "requests_total": total,
                                            "running": running, "waiting": waiting}]}


def _kinds(reading, streak, progress):
    return {a.kind for a in detector.sustained_alerts(N, reading, streak, "t", progress)}


def test_leaked_queue_counts_after_traffic_are_not_a_stall(monkeypatch):
    """Node-4 TTS (vLLM-Omni): requests complete, but the engine leaves some counted as running / waiting."""
    monkeypatch.setattr(config, "SUSTAIN_SCANS", 3)
    streak, progress = {}, {}
    for total, run, wait in [(100, 1, 0), (120, 2, 1), (140, 0, 1), (150, 1, 1)]:   # busy: counter advances
        assert _kinds(_tts(total, run, wait), streak, progress) == set()
    for _ in range(10):                                                              # quiet, leftovers stay
        assert _kinds(_tts(150, 1, 1), streak, progress) == set()


def test_requests_arriving_at_a_hung_server_still_alert(monkeypatch):
    monkeypatch.setattr(config, "SUSTAIN_SCANS", 3)
    streak, progress = {}, {}
    _kinds(_tts(150, 1, 1), streak, progress)                     # resting level with leftovers: 1 running, 1 waiting
    _kinds(_tts(150, 1, 1), streak, progress)
    hung = [_kinds(_tts(150, 3, 1), streak, progress) for _ in range(3)]   # two new requests run, none completes
    assert hung[-1] == {"gpu_stalled"} and hung[0] == set()
    queued = [_kinds(_tts(150, 1, 4), streak, progress) for _ in range(3)]  # requests wait, nothing scheduled
    assert "model_queue_stuck" in queued[-1]
    assert _kinds(_tts(151, 1, 4), streak, progress) == set()    # one completes: progress, not stuck


def test_a_restart_resets_the_resting_level(monkeypatch):
    monkeypatch.setattr(config, "SUSTAIN_SCANS", 2)
    streak, progress = {}, {}
    _kinds(_tts(500, 2, 2), streak, progress)                     # leftovers before the restart
    _kinds(_tts(0, 0, 0), streak, progress)                        # restarted: counter back to 0
    assert [_kinds(_tts(0, 1, 0), streak, progress) for _ in range(2)][-1] == {"gpu_stalled"}


def test_latency_rule_only_for_chat_models_under_load(monkeypatch):
    monkeypatch.setattr(config, "SUSTAIN_SCANS", 1)
    s = {"name": "qwen-chat", "port": 8103, "kind": "LLM", "status": "ready", "req_per_min": 3, "ttft_p95": 14.0}
    (a,) = detector.sustained_alerts(N, {"vllm": [s]}, {}, "t")
    assert a.kind == "model_latency_high" and a.resource == "qwen-chat@8103"
    assert detector.sustained_alerts(N, {"vllm": [s | {"req_per_min": 0}]}, {}, "t") == []
    assert detector.sustained_alerts(N, {"vllm": [s | {"kind": "ASR"}]}, {}, "t") == []


def test_a_few_slow_requests_at_low_traffic_do_not_alert(monkeypatch):
    """Node-2 qwen3.6: one request every few minutes (the console's own 15k-token diagnosis prompts) set the p95."""
    monkeypatch.setattr(config, "SUSTAIN_SCANS", 3)
    s = {"name": "qwen3.6", "port": 8001, "kind": "LLM", "status": "ready", "req_per_min": 0.2, "ttft_p95": 19.5}
    streak: dict = {}
    assert all(detector.sustained_alerts(N, {"vllm": [s]}, streak, "t") == [] for _ in range(10))
    busy = s | {"req_per_min": 6}
    runs = [detector.sustained_alerts(N, {"vllm": [busy]}, streak, "t") for _ in range(3)]
    assert runs[0] == [] and [a.kind for a in runs[-1]] == ["model_latency_high"]     # slow under real load, held 3 min


def test_metric_kinds_clear_when_reachable_and_not_firing():
    i = inc("high_cpu", "spark-214e")
    assert runtime.condition_cleared(i, _det(), {}, set())
    assert not runtime.condition_cleared(i, _det(), {}, {i["fingerprint"]})
    assert not runtime.condition_cleared(i, _det(False), {}, set())


def test_pending_approval_expires_and_cannot_then_be_granted(db):
    iid, _ = store.open_incident("service_unhealthy", "Node-2", "aiops-test-svc", "t", "fp-exp", {})
    store.save_proposal(iid, {"incident_id": iid, "action_id": "ACT-x", "operation": "restart_test_service",
                              "target": {"resource_type": "test_service", "resource_id": "aiops-test-svc"}})
    aid = store.create_approval("ACT-x", iid)
    assert store.expire_pending(iid, "cleared") == 1
    assert store.decide_approval(aid, True, "alice", "late") is None          # too late: nothing to approve
    assert store.get_approval(aid)["status"] == "expired"

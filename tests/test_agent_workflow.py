"""End-to-end agent workflow on a scripted model.

The real LangGraph workflow, policy engine, executor, verifier, store and audit chain run on a scratch database and a
scratch lab. Only two things are fake: the model's HTTP endpoint (scripted replies, so each test pins one model
behaviour) and the read-only tool back-ends (the tool gateway's validation in front of them stays real).
"""
import json
import re
import threading
import time

import pytest
from langgraph.checkpoint.memory import MemorySaver

from backend.core import config, llm, store
from backend.incidents import graph, tools
from backend.incidents.runtime import Runtime
from backend.security import lab

EV = re.compile(r"EV-[0-9a-f]+")


class Model:
    """Scripted OpenAI-compatible endpoint. Replies are queued per schema; the last one repeats.
    A reply is a dict (sent as JSON), a str (sent verbatim) or a callable(prompt) -> dict | str."""

    def __init__(self):
        self.replies: dict[str, list] = {"diagnosis": [], "action_proposal": []}
        self.calls: dict[str, int] = {"diagnosis": 0, "action_proposal": 0}

    def post(self, url, timeout=None, headers=None, json=None):
        schema = "diagnosis" if "named diagnosis" in json["messages"][0]["content"] else "action_proposal"
        queue = self.replies[schema]
        reply = queue[min(self.calls[schema], len(queue) - 1)]
        self.calls[schema] += 1
        prompt = json["messages"][1]["content"]
        reply = reply(prompt) if callable(reply) else reply
        content = reply if isinstance(reply, str) else _json(reply)
        return _Resp({"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 100}})


class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


_json = json.dumps


def diagnosis(more=(), cite=True, security=False):
    def make(prompt):
        ids = EV.findall(prompt)[:2] if cite else []
        d = {"summary": "A known-malicious file was written in the lab.",
             "observations": [{"fact": "file written to /var/tmp/.cache", "evidence_ids": ids}],
             "hypotheses": [{"cause": "malware drop", "confidence": "high", "supporting_evidence_ids": ids, "missing_information": []}],
             "more_evidence": list(more)}
        if security:
            d |= {"assessment": "malicious", "attack_techniques": []}
        return d
    return make


def proposal(operation="quarantine_lab_file", target=lab.MALWARE_PATH, cite="real"):
    def make(prompt):
        ids = EV.findall(prompt)[:1] if cite == "real" else ["EV-0000000bad"]
        return {"operation": operation, "target_resource_id": target, "parameters": {},
                "reason": "contain the file", "evidence_ids": ids, "expected_outcome": "file quarantined",
                "rollback_plan": "restore from quarantine", "recommended_manual_steps": []}
    return make


@pytest.fixture
def wf(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(lab, "STATE", tmp_path / "lab.json")
    store.init()
    lab.run("malware_drop")
    monkeypatch.setattr(graph, "_graph", graph.build(MemorySaver()))
    monkeypatch.setattr(graph, "retrieve_runbooks", lambda inc: {"query": None, "method": "test", "chunks": []})
    reads = []

    def fake(name):
        def fn(target, incident=None):
            reads.append((name, target))
            return {"tool": name, "source": target, "retrieved_at": time.time(), "data": {"ok": True}}
        return fn
    monkeypatch.setattr(tools, "READONLY_TOOLS", {t: (k, fake(t)) for t, (k, _) in tools.READONLY_TOOLS.items()})
    m = Model()
    monkeypatch.setattr(llm, "LLM_ENDPOINTS", [config.LLMEndpoint("scripted", "http://scripted.invalid/v1", "m")])
    monkeypatch.setattr(llm, "_healthy", lambda ep: True)
    monkeypatch.setattr(llm.httpx, "post", m.post)
    m.reads = reads
    return m


def _malware_incident() -> str:
    iid, _ = store.open_incident("sec_malicious_file", "LAB", lab.MALWARE_PATH, "Known-malicious file written on LAB",
                                 f"fp-{time.time()}", {"path": lab.MALWARE_PATH, "host": lab.HOST})
    return iid


def _approval(iid: str) -> dict:
    return next(a for a in store.approvals_with_context("pending") if a["incident_id"] == iid)


# ---------------------------------------------------------------- the whole path
def test_detect_to_verified_fix_with_a_human_approval(wf):
    wf.replies["diagnosis"] = [diagnosis(security=True)]
    wf.replies["action_proposal"] = [proposal()]
    iid = _malware_incident()
    graph.run_incident(iid)
    assert store.get_incident(iid)["status"] == "awaiting_approval" and graph.parked_at_approval(iid)
    assert lab.MALWARE_PATH not in lab.state()["quarantined_files"]          # nothing changes before a person decides

    store.decide_approval(_approval(iid)["id"], True, "alice", "")
    out = graph.resume_incident(iid, _approval_id(iid))
    assert out["final_status"] == "resolved"
    assert lab.MALWARE_PATH in lab.state()["quarantined_files"]               # the lab really changed
    assert store.verify_audit_chain()["ok"]


def _approval_id(iid: str) -> str:
    with store.conn() as c:
        return c.execute("SELECT id FROM approval_requests WHERE incident_id = ?", (iid,)).fetchone()[0]


def test_rejection_executes_nothing(wf):
    wf.replies["diagnosis"] = [diagnosis(security=True)]
    wf.replies["action_proposal"] = [proposal()]
    iid = _malware_incident()
    graph.run_incident(iid)
    store.decide_approval(_approval(iid)["id"], False, "alice", "not now")
    assert graph.resume_incident(iid, _approval_id(iid))["final_status"] == "action_rejected"
    assert lab.MALWARE_PATH not in lab.state()["quarantined_files"]
    assert not store.has_execution(iid)


# ---------------------------------------------------------------- invalid model output
def test_one_malformed_reply_is_corrected(wf):
    wf.replies["diagnosis"] = ["Sure! Here is the diagnosis: ```not json```", diagnosis(security=True)]
    wf.replies["action_proposal"] = [proposal()]
    iid = _malware_incident()
    out = graph.run_incident(iid)
    assert wf.calls["diagnosis"] == 2 and out["diagnosis_meta"]["attempts"] == 2
    assert store.get_incident(iid)["status"] == "awaiting_approval"


@pytest.mark.parametrize("bad", ["not json at all", {"summary": "missing required fields"},
                                 {"summary": "s", "observations": [], "hypotheses": [], "more_evidence": [],
                                  "assessment": "certainly evil", "attack_techniques": []}])
def test_a_model_that_keeps_failing_degrades_to_a_person(wf, bad):
    wf.replies["diagnosis"] = [bad]
    iid = _malware_incident()
    out = graph.run_incident(iid)
    assert out["final_status"] == "needs_human" and out.get("proposal") is None
    assert wf.calls["diagnosis"] == 2 and wf.calls["action_proposal"] == 0    # retried once, never guessed onward
    assert not store.has_execution(iid)


def test_a_plan_outside_the_catalogue_is_never_proposed(wf):
    wf.replies["diagnosis"] = [diagnosis(security=True)]
    wf.replies["action_proposal"] = [lambda p: proposal()(p) | {"operation": "rm_rf_root"}]
    iid = _malware_incident()
    out = graph.run_incident(iid)
    assert out["final_status"] == "needs_human" and not store.approvals_with_context("pending")


# ---------------------------------------------------------------- step limits
def test_evidence_requests_stop_at_the_attempt_limit(wf):
    targets = iter(f"10.0.0.{i}" for i in range(1, 100))         # always a new, valid request: the loop must still end
    wf.replies["diagnosis"] = [lambda p: diagnosis(more=[{"tool": "get_auth_events_by_source", "target": next(targets)}], security=True)(p)]
    wf.replies["action_proposal"] = [proposal()]
    iid = _malware_incident()
    graph.run_incident(iid)
    assert wf.calls["diagnosis"] == config.MAX_DIAGNOSIS_ATTEMPTS
    assert store.get_incident(iid)["status"] == "awaiting_approval"


def test_invalid_evidence_requests_are_refused_and_audited(wf):
    wf.replies["diagnosis"] = [diagnosis(more=[{"tool": "get_node_metrics", "target": "Node-99"}], security=True)]
    wf.replies["action_proposal"] = [proposal()]
    iid = _malware_incident()
    graph.run_incident(iid)
    assert ("get_node_metrics", "Node-99") not in wf.reads
    with store.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM audit_events WHERE incident_id=? AND event='evidence_request_refused'", (iid,)).fetchone()[0] == 1
    assert wf.calls["diagnosis"] == 1                             # nothing new was added: no second diagnosis


def test_invented_evidence_ids_get_exactly_one_replan(wf):
    wf.replies["diagnosis"] = [diagnosis(security=True)]
    wf.replies["action_proposal"] = [proposal(cite="invented")]
    iid = _malware_incident()
    out = graph.run_incident(iid)
    assert wf.calls["action_proposal"] == 2 and out["final_status"] == "action_denied"
    assert not store.approvals_with_context("pending")


def test_invented_evidence_corrected_on_replan_reaches_approval(wf):
    wf.replies["diagnosis"] = [diagnosis(security=True)]
    wf.replies["action_proposal"] = [proposal(cite="invented"), proposal()]
    iid = _malware_incident()
    graph.run_incident(iid)
    assert wf.calls["action_proposal"] == 2 and store.get_incident(iid)["status"] == "awaiting_approval"


def test_containment_of_anything_but_the_incident_subject_is_denied(wf):
    wf.replies["diagnosis"] = [diagnosis(security=True)]
    wf.replies["action_proposal"] = [proposal(target="/etc/passwd")]
    iid = _malware_incident()
    assert graph.run_incident(iid)["final_status"] == "action_denied"
    assert wf.calls["action_proposal"] == 1                       # a scope denial is final: no replan


# ---------------------------------------------------------------- duplicates
def test_one_incident_is_never_investigated_twice_at_once(wf, monkeypatch):
    gate, runs = threading.Event(), []

    def slow(iid):
        runs.append(iid)
        gate.wait(2)
    monkeypatch.setattr(graph, "run_incident", slow)
    rt = Runtime(3600)
    assert rt.spawn(graph.run_incident, "INC-dup") is True
    time.sleep(0.05)
    assert rt.spawn(graph.run_incident, "INC-dup") is False
    gate.set()
    for _ in range(50):
        if not rt.is_running("INC-dup"):
            break
        time.sleep(0.02)
    assert runs == ["INC-dup"]


def test_a_repeat_alert_while_open_adds_an_occurrence_not_an_incident(wf):
    a, new_a = store.open_incident("sec_malicious_file", "LAB", lab.MALWARE_PATH, "t", "fp-same", {})
    b, new_b = store.open_incident("sec_malicious_file", "LAB", lab.MALWARE_PATH, "t", "fp-same", {})
    assert new_a and not new_b and a == b

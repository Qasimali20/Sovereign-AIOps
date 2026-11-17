"""Endpoint/network lab detections, new lab containment, and offline CVE relevance."""
import time

import pytest

from backend.incidents import policy
from backend.core import store
from backend.security import lab, rules, vulns


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(lab, "STATE", tmp_path / "lab.json")
    store.init()


def kinds():
    return {(a.kind, a.resource) for a in rules.evaluate(time.time())}


def test_malware_drop_and_beacon_detected():
    lab.run("malware_drop")
    lab.run("c2_beacon")
    k = kinds()
    assert ("sec_malicious_file", lab.MALWARE_PATH) in k
    assert ("sec_beaconing", lab.C2[0]) in k


def test_irregular_connections_are_not_beaconing():
    from backend.security import events
    now = time.time()
    events.save([{"t": now - 900 + off, "node": "LAB", "source": "netflow", "kind": "net_connection", "user": "www-data",
                  "src_ip": "203.0.113.9", "method": "tcp", "detail": "lab-web-01 -> 203.0.113.9:443 1kB"}
                 for off in (0, 10, 200, 230, 700, 705)])
    assert not any(kind == "sec_beaconing" for kind, _ in kinds())


def prop(op, target, rtype):
    return {"operation": op, "target": {"resource_type": rtype, "resource_id": target}, "evidence_refs": ["EV-1"]}


def test_lab_containment_bound_to_subject():
    lab.run("malware_drop")
    inc = {"kind": "sec_malicious_file", "resource": lab.MALWARE_PATH, "detail": {"path": lab.MALWARE_PATH, "host": lab.HOST}}
    assert policy.evaluate(prop("quarantine_lab_file", lab.MALWARE_PATH, "lab_file"), {"EV-1"}, inc)[0] == "requires_approval"
    assert policy.evaluate(prop("isolate_lab_endpoint", lab.HOST, "lab_endpoint"), {"EV-1"}, inc)[0] == "requires_approval"
    # a file never observed in the lab, a real node, or a real system path: denied
    assert policy.evaluate(prop("quarantine_lab_file", "/etc/passwd", "lab_file"), {"EV-1"}, inc)[0] == "deny"
    assert policy.evaluate(prop("isolate_lab_endpoint", "Node-2", "lab_endpoint"), {"EV-1"}, inc)[0] == "deny"


E = {"cve": "CVE-2021-3156", "name": "n", "package": "sudo", "severity": "high", "technique": "T1068", "references": [],
     "summary": "s", "affected_upstream": ["1.8.2", "1.9.5p2"]}


@pytest.mark.parametrize("installed,mentions,status", [
    (None, 0, "not_installed"),
    ("1.9.15p5-3ubuntu5.24.04.2", 13, "patched"),                 # fix recorded in the changelog
    ("1.9.15p5-3ubuntu5.24.04.2", 0, "not_affected"),             # above the affected range
    ("1.9.5p1-1ubuntu1", 0, "vulnerable"),
    ("1.9.5p1-1ubuntu1.1", 3, "patched"),                         # backported fix, same upstream version
])
def test_cve_verdicts(installed, mentions, status):
    assert vulns.assess(E, installed, mentions)["status"] == status


def test_upstream_resolves_epoch_and_really():
    assert vulns.upstream("1:9.6p1-3ubuntu13.18") == "9.6p1"
    assert vulns.upstream("5.6.1+really5.4.5-1ubuntu0.3") == "5.4.5"     # xz: not the backdoored 5.6.1


def test_lab_vulnerable_inventory():
    lab.run("vulnerable_sudo")
    s = lab.state()
    res = {r["cve"]: r["status"] for r in vulns.scan_lab(s["packages"], s["patched"])["results"]}
    assert res["CVE-2021-3156"] == "vulnerable" and res["CVE-2024-3094"] == "not_affected"

"""SOC module: parsing, detection rules, lab-only containment policy and executor."""
import time

import pytest

from backend.incidents import policy
from backend.core import store
from backend.security import events, lab, rules
from backend.core import config

# fleet addresses come from .env (placeholders without it)
N2 = config.NODE_BY_NAME["Node-2"].lan_ip
T2 = config.NODE_BY_NAME["Node-2"].ts_ip


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(lab, "STATE", tmp_path / "lab.json")
    store.init()


def test_parse_real_sshd_and_sudo_lines():
    p = lambda line: events.parse(line, "Node-3")
    assert p(f"1.0 spark-214e sshd[1]: Failed password for tmc from {N2} port 41446 ssh2")["kind"] == "auth_failure"
    e = p("1.0 spark-214e sshd[1]: Failed password for invalid user oracle from 203.0.113.9 port 22 ssh2")
    assert (e["kind"], e["user"], e["src_ip"]) == ("invalid_user", "oracle", "203.0.113.9")
    e = p(f"1.0 h sshd[1]: Accepted publickey for tmc from {T2} port 1 ssh2")
    assert (e["kind"], e["method"]) == ("auth_success", "publickey")
    e = p("1.0 h sudo[9]:      tmc : TTY=pts/1 ; PWD=/home/tmc ; USER=root ; COMMAND=/usr/bin/systemctl restart x --secret=abc")
    assert e["kind"] == "sudo" and e["detail"] == "systemctl"            # program only, never arguments
    assert p("1.0 h sudo[9]: pam_unix(sudo:auth): authentication failure; logname=tmc uid=1000 user=tmc")["kind"] == "sudo_failure"
    assert p("1.0 h systemd[1]: Started x") is None


def test_bruteforce_and_login_after_failures_same_node_only():
    now = time.time()
    events.save([{"t": now - 100 + i, "node": "Node-3", "source": "sshd", "kind": "auth_failure", "user": "tmc",
                  "src_ip": "10.0.0.9", "method": "password", "detail": ""} for i in range(6)])
    events.save([{"t": now - 10, "node": "Node-1", "source": "sshd", "kind": "auth_success", "user": "tmc",
                  "src_ip": "10.0.0.9", "method": "publickey", "detail": ""}])
    fps = {a.fingerprint for a in rules.evaluate(now)}
    assert "sec_bruteforce:Node-3:10.0.0.9" in fps
    assert not any(f.startswith("sec_login_after_failures:Node-1") for f in fps)   # failures were on another node


def test_lab_scenarios_trigger_expected_rules():
    lab.run("bruteforce_then_success")
    lab.run("password_spray")
    lab.run("sudo_abuse")
    fps = {a.fingerprint for a in rules.evaluate(time.time())}
    assert "sec_login_after_failures:LAB:203.0.113.50" in fps
    assert "sec_password_spray:LAB:198.51.100.23" in fps
    assert "sec_sudo_failures:LAB:j.doe" in fps


def prop(op, target, rtype, refs=("EV-1",)):
    return {"operation": op, "target": {"resource_type": rtype, "resource_id": target}, "evidence_refs": list(refs)}


def test_policy_contains_only_lab_targets():
    lab.run("bruteforce_then_success")
    events.save([{"t": time.time(), "node": "Node-1", "source": "sshd", "kind": "auth_success", "user": "tmc",
                  "src_ip": f"{N2}", "method": "publickey", "detail": ""}])
    assert policy.evaluate(prop("block_ip_lab", "203.0.113.50", "lab_ip"), {"EV-1"})[0] == "requires_approval"
    assert policy.evaluate(prop("block_ip_lab", f"{N2}", "lab_ip"), {"EV-1"})[0] == "deny"      # a real fleet node
    assert policy.evaluate(prop("block_ip_lab", "8.8.8.8", "lab_ip"), {"EV-1"})[0] == "deny"           # real internet
    assert policy.evaluate(prop("block_ip_lab", "203.0.113.99", "lab_ip"), {"EV-1"})[0] == "deny"      # never seen in lab
    assert policy.evaluate(prop("disable_lab_account", "ops-admin", "lab_account"), {"EV-1"})[0] == "requires_approval"
    assert policy.evaluate(prop("disable_lab_account", "tmc", "lab_account"), {"EV-1"})[0] == "deny"   # real account
    assert policy.evaluate(prop("disable_lab_account", "ops-admin", "test_service"), {"EV-1"})[0] == "deny"


def test_executor_applies_lab_containment_only_with_human_approval():
    from backend.incidents import executor
    lab.run("bruteforce_then_success")
    iid, _ = store.open_incident("sec_login_after_failures", "LAB", "203.0.113.50", "t", "fp", {})
    ev = store.add_evidence(iid, "s", "get_source_context", {})
    p = {"incident_id": iid, "action_id": "ACT-L", **prop("block_ip_lab", "203.0.113.50", "lab_ip", [ev])}
    store.save_proposal(iid, p)
    aid = store.create_approval("ACT-L", iid)
    with pytest.raises(executor.ExecutionRefused):
        executor.execute(p, aid)                                   # still pending
    store.decide_approval(aid, True, "alice", "")
    assert executor.execute(p, aid)["ok"] and "203.0.113.50" in lab.state()["blocked_ips"]


def test_ip_classification_order():
    from backend.security.events import classify_ip
    assert classify_ip("203.0.113.50") == "lab-attacker"      # ipaddress says is_private: must not win
    assert classify_ip("198.51.100.23").startswith("lab-attacker")
    assert classify_ip(f"{N2}") == "fleet-node"
    assert classify_ip(f"{T2}") == "tailnet"
    assert classify_ip("192.168.1.5") == "private-lan"
    assert classify_ip("8.8.8.8") == "public"


def test_containment_bound_to_incident_subject():
    lab.run("password_spray")
    lab.run("sudo_abuse")
    sudo_inc = {"kind": "sec_sudo_failures", "resource": "j.doe", "detail": {}}
    assert policy.evaluate(prop("block_ip_lab", "198.51.100.23", "lab_ip"), {"EV-1"}, sudo_inc)[0] == "deny"
    assert policy.evaluate(prop("disable_lab_account", "j.doe", "lab_account"), {"EV-1"}, sudo_inc)[0] == "requires_approval"
    spray = {"kind": "sec_password_spray", "resource": "198.51.100.23", "detail": {"users": ["ops-admin", "deploy"]}}
    assert policy.evaluate(prop("block_ip_lab", "198.51.100.23", "lab_ip"), {"EV-1"}, spray)[0] == "requires_approval"
    assert policy.evaluate(prop("disable_lab_account", "deploy", "lab_account"), {"EV-1"}, spray)[0] == "requires_approval"
    assert policy.evaluate(prop("disable_lab_account", "a.khan", "lab_account"), {"EV-1"}, spray)[0] == "deny"


def test_sudo_events_are_stored_once_across_rescans():
    """Regression: sudo lines have no source IP; NULL made the old unique key never match, so every scan re-saved them."""
    from backend.security import events
    events.init()                                              # scratch database from the autouse fixture
    ev = events.parse("1794700000.5 spark-b969 sudo[1]:      tmc : TTY=pts/0 ; PWD=/home/tmc ; USER=root ; COMMAND=/usr/sbin/reboot", "Node-4")
    assert events.save([ev]) == 1
    assert events.save([ev]) == 0 and events.save([ev]) == 0
    with store.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM security_events").fetchone()[0] == 1

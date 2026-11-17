"""Isolated SOC lab: synthetic attack scenarios and lab-only response actions.

Nothing here touches a real node. The lab host and accounts are fictional; attack sources come
from the IETF documentation ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24), which can
never be real hosts. Response actions change recorded lab state, which verification reads back."""
from __future__ import annotations

import copy
import ipaddress
import json
import threading
import time

from backend.core import store
from backend.core.config import ROOT
from backend.security import events

NODE = "LAB"
HOST = "lab-web-01"
ACCOUNTS = ("ops-admin", "svc-backup", "j.doe", "a.khan", "deploy")
STATE = ROOT / "data" / "soc_lab.json"
_lock = threading.Lock()
_DOC_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]

SCENARIOS = {
    "bruteforce_then_success": "8 failed passwords for ops-admin from 203.0.113.50, then a successful login",
    "password_spray": "One source (198.51.100.23) tries one password against 6 lab accounts",
    "sudo_abuse": "j.doe fails sudo authentication 4 times on the lab host",
    "malware_drop": "The web server user writes a known-malicious binary to /var/tmp/.cache/kworkerd and runs it",
    "c2_beacon": "lab-web-01 calls 203.0.113.66:4444 every 60 seconds (command-and-control beaconing)",
    "vulnerable_sudo": "The lab host runs sudo 1.9.5p1 without the Baron Samedit fix (CVE-2021-3156)",
}
# Local threat intelligence for the lab (sovereign: no external lookups). sha256 -> description.
IOCS = {"9f2b5c1d0e7a4b3c8d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c": "Linux cryptominer dropper (lab IOC set)"}
MALWARE_PATH = "/var/tmp/.cache/kworkerd"
C2 = ("203.0.113.66", "4444")
BASE_PACKAGES = {"openssh-server": "1:9.6p1-3ubuntu13.18", "openssh-client": "1:9.6p1-3ubuntu13.18",
                 "sudo": "1.9.15p5-3ubuntu5.24.04.2", "xz-utils": "5.6.1+really5.4.5-1ubuntu0.3"}
BASE_PATCHED = {"openssh-server": ["CVE-2024-6387"], "openssh-client": ["CVE-2023-38408"],
                "sudo": ["CVE-2021-3156", "CVE-2025-32463"]}
def _empty() -> dict:
    """A fresh lab state. Always a deep copy: callers mutate it, and the baselines must never change."""
    return copy.deepcopy({"blocked_ips": {}, "disabled_accounts": {}, "isolated_endpoints": {}, "quarantined_files": {},
                          "packages": BASE_PACKAGES, "patched": BASE_PATCHED})


def _ev(t, kind, user, ip=None, method="password", source="sshd", detail=""):
    return {"t": t, "node": NODE, "source": source, "kind": kind, "user": user, "src_ip": ip, "method": method, "detail": detail}


def run(scenario: str) -> int:
    now = time.time()
    if scenario == "bruteforce_then_success":
        evs = [_ev(now - 150 + i * 15, "auth_failure", "ops-admin", "203.0.113.50") for i in range(8)]
        evs.append(_ev(now - 20, "auth_success", "ops-admin", "203.0.113.50"))
    elif scenario == "password_spray":
        users = ["ops-admin", "svc-backup", "j.doe", "a.khan", "deploy", "oracle"]
        evs = [_ev(now - 120 + i * 18, "invalid_user" if u == "oracle" else "auth_failure", u, "198.51.100.23")
               for i, u in enumerate(users)]
    elif scenario == "sudo_abuse":
        evs = [_ev(now - 90 + i * 20, "sudo_failure", "j.doe", source="sudo") for i in range(4)]
    elif scenario == "malware_drop":
        sha = next(iter(IOCS))
        evs = [_ev(now - 60, "file_created", "www-data", source="edr", method="", detail=f"{MALWARE_PATH}|sha256={sha}"),
               _ev(now - 55, "process_exec", "www-data", source="edr", method="", detail=f"{MALWARE_PATH} --daemon")]
    elif scenario == "c2_beacon":
        evs = [_ev(now - 600 + i * 60, "net_connection", "www-data", C2[0], method="tcp", source="netflow",
                   detail=f"{HOST} -> {C2[0]}:{C2[1]} 312B") for i in range(10)]
    elif scenario == "vulnerable_sudo":
        with _lock:
            s = state()
            s["packages"] = s["packages"] | {"sudo": "1.9.5p1-1ubuntu1"}
            s["patched"] = s["patched"] | {"sudo": []}
            _write(s)
        return 0
    else:
        raise ValueError(f"unknown scenario {scenario!r}")
    return events.save(evs)


# ---------------------------------------------------------------- state + response actions
def state() -> dict:
    try:
        return _empty() | json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return _empty()


def _write(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(STATE)


def ip_blockable(ip: str) -> tuple[bool, str]:
    """Only a lab attacker may be blocked: a documentation-range address seen exclusively in lab events."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False, "not an IP address"
    if not any(a in n for n in _DOC_NETS):
        return False, "only lab (documentation-range) addresses can be blocked"
    with store.conn() as c:
        nodes = {r[0] for r in c.execute("SELECT DISTINCT node FROM security_events WHERE src_ip = ?", (ip,))}
    if nodes != {NODE}:
        return False, f"address seen on {sorted(nodes) or 'no node'}, not exclusively in the lab"
    return True, "lab attacker address"


def endpoint_isolatable(host: str) -> tuple[bool, str]:
    return (True, "lab endpoint") if host == HOST else (False, "only the lab endpoint can be isolated")


def file_quarantinable(path: str) -> tuple[bool, str]:
    with store.conn() as c:
        seen = c.execute("SELECT 1 FROM security_events WHERE node=? AND kind='file_created' AND detail LIKE ?",
                         (NODE, f"{path}|%")).fetchone()
    return (True, "file observed on the lab endpoint") if seen else (False, "file not observed on the lab endpoint")


def isolate_endpoint(host: str, actor: str, incident_id: str) -> dict:
    with _lock:
        s = state()
        s["isolated_endpoints"][host] = {"by": actor, "at": time.time(), "incident": incident_id}
        _write(s)
    return {"ok": True, "isolated": host}


def quarantine_file(path: str, actor: str, incident_id: str) -> dict:
    with _lock:
        s = state()
        s["quarantined_files"][path] = {"by": actor, "at": time.time(), "incident": incident_id}
        _write(s)
    return {"ok": True, "quarantined": path}


def account_disableable(user: str) -> tuple[bool, str]:
    return (True, "lab account") if user in ACCOUNTS else (False, "not a lab account")


def block_ip(ip: str, actor: str, incident_id: str) -> dict:
    with _lock:
        s = state()
        s["blocked_ips"][ip] = {"by": actor, "at": time.time(), "incident": incident_id}
        _write(s)
    return {"ok": True, "blocked": ip}


def disable_account(user: str, actor: str, incident_id: str) -> dict:
    with _lock:
        s = state()
        s["disabled_accounts"][user] = {"by": actor, "at": time.time(), "incident": incident_id}
        _write(s)
    return {"ok": True, "disabled": user}


def reset() -> None:
    with _lock:
        _write(_empty())
    with store.conn() as c:
        c.execute("DELETE FROM security_events WHERE node = ?", (NODE,))

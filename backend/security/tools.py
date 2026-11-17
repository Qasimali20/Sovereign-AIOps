"""Read-only security evidence tools. Targets are validated (an IP address or a POSIX user
name); data comes from the platform's own event store, never from the model."""
from __future__ import annotations

import ipaddress
import re
import time

from backend.core import config, store
from backend.security import lab
from backend.security.events import classify_ip, query

USER_RX = re.compile(r"^[a-z_][a-z0-9._-]{0,31}$")
WINDOW = 86400


def valid_ip(v: str) -> bool:
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False


def valid_user(v: str) -> bool:
    return bool(USER_RX.match(v or ""))


def _summ(evs: list[dict]) -> dict:
    by = {}
    for e in evs:
        k = f"{e['node']}:{e['kind']}"
        by[k] = by.get(k, 0) + 1
    return by


def _fmt(evs: list[dict], n: int = 40) -> list[dict]:
    return [{"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["t"])), **{k: e[k] for k in ("node", "kind", "user", "src_ip", "method", "detail") if e[k]}}
            for e in evs[:n]]


def auth_events_by_source(ip: str) -> dict:
    evs = query("src_ip = ? AND t >= ?", (ip, time.time() - WINDOW), 500)
    return {"source": ip, "window_hours": WINDOW // 3600, "counts": _summ(evs), "events": _fmt(evs)}


def auth_events_by_user(user: str) -> dict:
    evs = query("user = ? AND t >= ?", (user, time.time() - WINDOW), 500)
    return {"user": user, "window_hours": WINDOW // 3600, "counts": _summ(evs), "events": _fmt(evs)}


def source_context(ip: str) -> dict:
    with store.conn() as c:
        r = c.execute("SELECT MIN(t), MAX(t), COUNT(*), SUM(kind='auth_success'), SUM(kind IN ('auth_failure','invalid_user')), "
                      "GROUP_CONCAT(DISTINCT node), GROUP_CONCAT(DISTINCT user) FROM security_events WHERE src_ip = ?", (ip,)).fetchone()
    fmt = lambda t: t and time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    return {"source": ip, "classification": classify_ip(ip), "first_seen": fmt(r[0]), "last_seen": fmt(r[1]),
            "events_all_time": r[2], "successful_logins": r[3] or 0, "failures": r[4] or 0,
            "nodes": (r[5] or "").split(",") if r[5] else [], "users": (r[6] or "").split(",") if r[6] else [],
            "blocked_in_lab": ip in lab.state()["blocked_ips"],
            "note": "fleet-node = one of the four DGX Sparks (the platform itself runs on Node-2, " + config.NODE_BY_NAME["Node-2"].lan_ip + "); "
                    "lab-attacker = documentation-range address (192.0.2/24, 198.51.100/24, 203.0.113/24): "
                    "attacker-controlled, occurs only in the isolated lab"}


def lab_state() -> dict:
    s = lab.state()
    return {"lab_host": lab.HOST, "lab_accounts": list(lab.ACCOUNTS), "blocked_ips": sorted(s["blocked_ips"]),
            "disabled_accounts": sorted(s["disabled_accounts"]), "isolated_endpoints": sorted(s["isolated_endpoints"]),
            "quarantined_files": sorted(s["quarantined_files"])}


def endpoint_events() -> dict:
    """File, process and network events from the lab endpoint (EDR + netflow), newest first."""
    with store.conn() as c:
        rows = c.execute("SELECT t, kind, user, src_ip, detail, source FROM security_events WHERE node=? AND kind IN "
                         "('file_created','process_exec','net_connection') ORDER BY t DESC LIMIT 40", (lab.NODE,)).fetchall()
    return {"host": lab.HOST, "events": [dict(r) for r in rows], "known_iocs": lab.IOCS}


def vulnerability_status(target: str) -> dict:
    from backend.security import vulns
    from backend.core import config
    if target == "LAB":
        s = lab.state()
        r = vulns.scan_lab(s["packages"], s["patched"])
    else:
        r = vulns.scan_node(config.NODE_BY_NAME[target])
    cat = vulns.catalogue()
    return r | {"catalogue_reviewed": cat["reviewed"], "method": "dpkg version + CVE id in package changelog (offline)"}

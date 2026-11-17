"""Deterministic security detections over normalised auth events. Each rule names the
ATT&CK technique it indicates; the agent later confirms, refines or rejects the mapping."""
from __future__ import annotations

from dataclasses import dataclass

from backend.core import store
from backend.security.events import classify_ip

BRUTE_WINDOW_S, BRUTE_MIN = 600, 5          # >=5 failures from one source to one node in 10 min
SPRAY_WINDOW_S, SPRAY_USERS = 1800, 3       # failures for >=3 distinct users from one source in 30 min
SUCCESS_AFTER_FAIL_S, SUCCESS_MIN_FAIL = 1800, 3
SUDO_WINDOW_S, SUDO_MIN = 600, 3
BEACON_WINDOW_S, BEACON_MIN, BEACON_JITTER = 1800, 5, 0.2   # >=5 calls to one ip:port, intervals within 20%
FAIL_KINDS = ("auth_failure", "invalid_user")


@dataclass
class SecAlert:
    kind: str
    node: str
    resource: str          # the source IP or user the alert is about
    title: str
    detail: dict

    @property
    def fingerprint(self) -> str:
        return f"{self.kind}:{self.node}:{self.resource}"


def evaluate(now: float) -> list[SecAlert]:
    out: list[SecAlert] = []
    ph = ",".join("?" * len(FAIL_KINDS))
    with store.conn() as c:
        brute = c.execute(
            f"SELECT node, src_ip, COUNT(*) n, COUNT(DISTINCT user) users, MIN(t) first, MAX(t) last, "
            f"GROUP_CONCAT(DISTINCT user) ulist FROM security_events WHERE kind IN ({ph}) AND t >= ? "
            f"GROUP BY node, src_ip HAVING n >= ?", (*FAIL_KINDS, now - BRUTE_WINDOW_S, BRUTE_MIN)).fetchall()
        spray = c.execute(
            f"SELECT src_ip, COUNT(DISTINCT user) users, COUNT(*) n, GROUP_CONCAT(DISTINCT node) nodes, "
            f"GROUP_CONCAT(DISTINCT user) ulist, MIN(t) first, MAX(t) last FROM security_events "
            f"WHERE kind IN ({ph}) AND t >= ? GROUP BY src_ip HAVING users >= ?",
            (*FAIL_KINDS, now - SPRAY_WINDOW_S, SPRAY_USERS)).fetchall()
        # one row per (node, source): the latest success that followed >= N failures on the SAME node
        after = c.execute(
            f"SELECT s.node, s.src_ip, s.user, MAX(s.t) t, s.method, (SELECT COUNT(*) FROM security_events f "
            f"WHERE f.node = s.node AND f.src_ip = s.src_ip AND f.kind IN ({ph}) AND f.t BETWEEN s.t - ? AND s.t) fails "
            f"FROM security_events s WHERE s.kind = 'auth_success' AND s.t >= ? GROUP BY s.node, s.src_ip "
            f"HAVING fails >= ?", (*FAIL_KINDS, SUCCESS_AFTER_FAIL_S, now - SUCCESS_AFTER_FAIL_S, SUCCESS_MIN_FAIL)).fetchall()
        sudo = c.execute(
            "SELECT node, user, COUNT(*) n, MIN(t) first, MAX(t) last FROM security_events WHERE kind IN ('sudo_failure','sudo_denied') "
            "AND t >= ? GROUP BY node, user HAVING n >= ?", (now - SUDO_WINDOW_S, SUDO_MIN)).fetchall()

        files = c.execute("SELECT node, user, t, detail FROM security_events WHERE kind='file_created' AND t >= ?",
                          (now - 3600,)).fetchall()
        conns = c.execute("SELECT node, src_ip, detail, t FROM security_events WHERE kind='net_connection' AND t >= ? "
                          "ORDER BY t", (now - BEACON_WINDOW_S,)).fetchall()
    from backend.security.lab import IOCS
    for r in files:
        path, _, h = (r["detail"] or "").partition("|sha256=")
        if h in IOCS:
            out.append(SecAlert("sec_malicious_file", r["node"], path, f"Known-malicious file written on {r['node']}: {path}",
                                {"path": path, "sha256": h, "ioc": IOCS[h], "user": r["user"], "written_at": r["t"],
                                 "host": "lab-web-01" if r["node"] == "LAB" else r["node"],
                                 "suggested_techniques": ["T1105", "T1059.004"]}))
    groups: dict[tuple, list[float]] = {}
    for r in conns:
        port = (r["detail"] or "").rsplit(":", 1)[-1].split()[0] if ":" in (r["detail"] or "") else "?"
        groups.setdefault((r["node"], r["src_ip"], port), []).append(r["t"])
    for (node, ip, port), ts in groups.items():
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        if len(ts) >= BEACON_MIN and gaps and max(gaps) - min(gaps) <= BEACON_JITTER * (sum(gaps) / len(gaps)):
            out.append(SecAlert("sec_beaconing", node, ip, f"Regular outbound connections from {node} to {ip}:{port} (beaconing)",
                                {"remote_ip": ip, "port": port, "connections": len(ts), "interval_s": round(sum(gaps) / len(gaps)),
                                 "host": "lab-web-01" if node == "LAB" else node, "source_class": classify_ip(ip),
                                 "suggested_techniques": ["T1071.001", "T1571"]}))

    spray_ips = {r["src_ip"] for r in spray}
    for r in brute:
        if r["src_ip"] in spray_ips:
            continue                                   # reported once, as spraying
        out.append(SecAlert("sec_bruteforce", r["node"], r["src_ip"],
                            f"Repeated SSH login failures on {r['node']} from {r['src_ip']}",
                            {"failures": r["n"], "users": (r["ulist"] or "").split(","), "first": r["first"], "last": r["last"],
                             "source_class": classify_ip(r["src_ip"]), "suggested_techniques": ["T1110.001", "T1021.004"]}))
    for r in spray:
        out.append(SecAlert("sec_password_spray", (r["nodes"] or "").split(",")[0], r["src_ip"],
                            f"Password spraying from {r['src_ip']}: {r['users']} accounts tried",
                            {"failures": r["n"], "users": (r["ulist"] or "").split(","), "nodes": (r["nodes"] or "").split(","),
                             "first": r["first"], "last": r["last"], "source_class": classify_ip(r["src_ip"]),
                             "suggested_techniques": ["T1110.003", "T1021.004"]}))
    for r in after:
        out.append(SecAlert("sec_login_after_failures", r["node"], r["src_ip"],
                            f"Successful SSH login as {r['user']} on {r['node']} after {r['fails']} failures from {r['src_ip']}",
                            {"user": r["user"], "method": r["method"], "failures_before": r["fails"], "success_at": r["t"],
                             "source_class": classify_ip(r["src_ip"]), "suggested_techniques": ["T1110.001", "T1078", "T1021.004"]}))
    for r in sudo:
        out.append(SecAlert("sec_sudo_failures", r["node"], r["user"],
                            f"Repeated sudo authentication failures by {r['user']} on {r['node']}",
                            {"failures": r["n"], "first": r["first"], "last": r["last"], "suggested_techniques": ["T1548.003"]}))
    return out

"""Security telemetry: SSH and sudo authentication events from every node's journal.

Read-only (journalctl over the same SSH paths as the GPU detector). Events are normalised and
stored once (unique key), commands are reduced to the program name so no arguments or secrets
are retained. Lab events (synthetic attacks for response testing) live in the same table with
node 'LAB'."""
from __future__ import annotations

import ipaddress
import re
import time

from backend.core import config, store
from backend.monitoring import kernel

MAX_LINES = 2000
_SSH = [
    (re.compile(r"Failed (\w+) for invalid user (\S+) from (\S+) port"), "invalid_user", ("method", "user", "src_ip")),
    (re.compile(r"Failed (\w+) for (\S+) from (\S+) port"), "auth_failure", ("method", "user", "src_ip")),
    (re.compile(r"Invalid user (\S+) from (\S+)"), "invalid_user", ("user", "src_ip")),
    (re.compile(r"Accepted (\w+) for (\S+) from (\S+) port"), "auth_success", ("method", "user", "src_ip")),
]
_SUDO_CMD = re.compile(r"sudo\[\d+\]:\s+(\S+) : (.*?)USER=(\S+) ; COMMAND=(\S+)")
_SUDO_FAIL = re.compile(r"pam_unix\(sudo:auth\): authentication failure;.*\buser=(\S+)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS security_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, node TEXT, source TEXT, kind TEXT,
  user TEXT, src_ip TEXT, method TEXT, detail TEXT,
  UNIQUE(node, t, kind, user, src_ip)
);
CREATE INDEX IF NOT EXISTS sec_t ON security_events(t);
CREATE INDEX IF NOT EXISTS sec_ip ON security_events(src_ip, t);
CREATE INDEX IF NOT EXISTS sec_user ON security_events(user, t);
"""
# The table's UNIQUE(...) treats NULLs as distinct, so sudo lines (no src_ip) were re-inserted on every scan.
# This index is the real once-only key: NULL-safe, and includes the command so two sudo calls in one second both count.
ONCE_KEY = "node, t, kind, COALESCE(user, ''), COALESCE(src_ip, ''), COALESCE(detail, '')"


def init() -> None:
    with store.conn() as c:
        c.executescript(SCHEMA)
        # remove duplicates saved before the NULL-safe key existed (keep the first copy), then add the key
        c.execute(f"DELETE FROM security_events WHERE id NOT IN (SELECT MIN(id) FROM security_events GROUP BY {ONCE_KEY})")
        c.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS sec_once ON security_events({ONCE_KEY})")
        ips = sorted(platform_ips())                            # label logins stored before the platform tag existed
        c.execute(f"UPDATE security_events SET source='platform' WHERE source='sshd' AND kind='auth_success' AND "
                  f"method='publickey' AND user=? AND src_ip IN ({','.join('?' * len(ips))})", (kernel.SSH_USER, *ips))


def parse(line: str, node: str) -> dict | None:
    """Parse one `journalctl -o short-unix` line from sshd or sudo. Pure."""
    ts, _, rest = line.partition(" ")
    try:
        t = float(ts)
    except ValueError:
        return None
    if " sshd[" in rest:
        for rx, kind, fields in _SSH:
            m = rx.search(rest)
            if m:
                ev = dict(zip(fields, m.groups()))
                return {"t": t, "node": node, "source": "sshd", "kind": kind, "user": ev.get("user"),
                        "src_ip": ev.get("src_ip"), "method": ev.get("method"), "detail": ""}
        return None
    if " sudo[" in rest:
        m = _SUDO_FAIL.search(rest)
        if m:
            return {"t": t, "node": node, "source": "sudo", "kind": "sudo_failure", "user": m.group(1),
                    "src_ip": None, "method": "password", "detail": ""}
        m = _SUDO_CMD.search(rest)
        if m:
            user, mid, as_user, cmd = m.groups()
            kind = "sudo_denied" if "incorrect password" in mid or "NOT in sudoers" in mid else "sudo"
            prog = cmd.split("/")[-1][:40]                       # program name only, never arguments
            return {"t": t, "node": node, "source": "sudo", "kind": kind, "user": user, "src_ip": None,
                    "method": f"as {as_user}", "detail": prog}
    return None


def save(events: list[dict]) -> int:
    with store.conn() as c:
        before = c.total_changes
        c.executemany("INSERT OR IGNORE INTO security_events (t, node, source, kind, user, src_ip, method, detail) "
                      "VALUES (:t, :node, :source, :kind, :user, :src_ip, :method, :detail)", events)
        return c.total_changes - before


def collect(node: config.Node, since: float) -> dict:
    """Read sshd/sudo auth events since `since` on one node. Returns {ok, error, events}."""
    cmd = (f"journalctl _COMM=sshd _COMM=sudo --since=@{int(since)} --no-pager -o short-unix "
           f"| grep -E 'Failed |Invalid user|Accepted |COMMAND=|sudo:auth' | tail -n {MAX_LINES}; echo __END__")
    try:
        r = kernel.run(kernel.access_for(node), cmd)
    except Exception as e:  # timeouts, missing ssh
        return {"ok": False, "error": str(e)[:200], "events": []}
    if "__END__" not in r.stdout:
        return {"ok": False, "error": (r.stderr.strip().splitlines() or ["no output"])[-1][:200], "events": []}
    evs = [e for e in (parse(l, node.name) for l in r.stdout.splitlines()) if e]
    for e in evs:
        if is_platform_login(e):
            e["source"] = "platform"
    return {"ok": True, "error": None, "events": evs}


def platform_ips() -> set[str]:
    """Addresses the platform itself connects from (the node it runs on)."""
    host = next((n for n in config.NODES if n.host == kernel.LOCAL_HOST), None)
    return {"127.0.0.1", "::1"} | ({host.lan_ip, host.ts_ip} if host else set())


def is_platform_login(e: dict) -> bool:
    """The platform's own key-based SSH checks: kept as evidence, but labelled so people see real sign-ins first."""
    return (e.get("kind") == "auth_success" and e.get("method") == "publickey"
            and e.get("user") == kernel.SSH_USER and e.get("src_ip") in platform_ips())


def classify_ip(ip: str | None) -> str:
    if not ip:
        return "local"
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown"
    # order matters: Python's ipaddress counts the documentation ranges as is_private
    if any(a in ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")):
        return "lab-attacker"
    if a in ipaddress.ip_network("100.64.0.0/10"):
        return "tailnet"
    if a.is_loopback:
        return "loopback"
    if any(a == ipaddress.ip_address(n.lan_ip) for n in config.NODES):
        return "fleet-node"
    if a.is_private:
        return "private-lan"
    return "public"


def query(where: str = "", args: tuple = (), limit: int = 200) -> list[dict]:
    with store.conn() as c:
        rows = c.execute(f"SELECT t, node, source, kind, user, src_ip, method, detail FROM security_events "
                         f"{'WHERE ' + where if where else ''} ORDER BY t DESC LIMIT ?", (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def now() -> float:
    return time.time()

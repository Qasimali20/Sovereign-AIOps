"""GPU driver (NVRM) signals from each node's kernel journal.

The Spark Monitor agents only see nvidia-smi, which keeps answering (with N/A/ERR
fields) while the GPU firmware is hung. The kernel log is where the real signal
is: `NVRM: Xid (...): 119, ...` GSP timeouts, `Xid 154` reset-required, and a
flood of `bad register read` lines. This module reads those, locally or over
SSH, without installing anything on the nodes.

Access is read-only (journalctl). Nodes we cannot reach are reported as a
coverage gap, never silently treated as healthy.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
from pathlib import Path
import time
from dataclasses import dataclass

from backend.core.config import Node

SSH_USER = os.environ.get("AIOPS_SSH_USER", "tmc")
CMD_TIMEOUT_S = 12
MAX_XID_LINES = 200
LOCAL_HOST = socket.gethostname()

# Xid catalogue: what the code means and what an operator should do about it.
# severity: critical = GPU unusable until reset; serious = workload failed; warning = investigate.
XID_INFO: dict[int, dict] = {
    119: {"severity": "critical", "meaning": "GSP firmware RPC timeout: the GPU System Processor stopped responding",
          "guidance": "GPU is hung. A service restart or warm reboot has not cleared this on GB10; do a full power-off "
                      "(shutdown, remove power ~30 s, power on). Check what triggered it (desktop/Xorg, model load)."},
    120: {"severity": "critical", "meaning": "GSP firmware error",
          "guidance": "Treat as a GPU firmware failure: drain workloads and power-cycle the node."},
    154: {"severity": "critical", "meaning": "GPU recovery action changed (driver requests a reset)",
          "guidance": "The driver reports the recovery action required (e.g. 'GPU Reset Required'). Follow it; on GB10 "
                      "a reset means a full power-off."},
    79: {"severity": "critical", "meaning": "GPU has fallen off the bus",
         "guidance": "Hardware/link level failure. Power-cycle; if it recurs, open a hardware case."},
    48: {"severity": "critical", "meaning": "Double-bit ECC error", "guidance": "Drain and power-cycle; recurring = hardware."},
    95: {"severity": "critical", "meaning": "Uncontained ECC error", "guidance": "Drain workloads and reset the GPU."},
    94: {"severity": "serious", "meaning": "Contained ECC error", "guidance": "Affected application was killed; restart it."},
    109: {"severity": "serious", "meaning": "Context switch timeout", "guidance": "Workload hang; restart the workload, watch for recurrence."},
    13: {"severity": "serious", "meaning": "Graphics engine exception (often an application fault)",
         "guidance": "Usually the application; check the process named in the event."},
    31: {"severity": "serious", "meaning": "GPU memory page fault (often an application fault)",
         "guidance": "Usually the application; check the process named in the event."},
    43: {"severity": "warning", "meaning": "GPU stopped processing (application-reported reset)",
         "guidance": "Check the application named in the event."},
    45: {"severity": "warning", "meaning": "Preemptive cleanup after a previous error",
         "guidance": "Follow-on event; look for the earlier Xid."},
}
SEVERITY_RANK = {"warning": 0, "serious": 1, "critical": 2}

# NVRM: Xid (PCI:000f:01:00): 119, pid=7770, name=VLLM::EngineCor, Timeout after 45s ...
# NVRM: Xid (PCI:000f:01:00): 154, GPU recovery action changed from 0x0 (None) to 0x1 (GPU Reset Required)
_XID = re.compile(r"NVRM: Xid \((?P<pci>[^)]*)\): (?P<code>\d+),\s*(?:pid=(?P<pid>[^,]*),\s*name=(?P<name>[^,]*),\s*)?(?P<msg>.*)")
_BOOT = re.compile(r"^-?\d+$")
_SPLIT = "__AIOPS_SPLIT__"


def xid_info(code: int) -> dict:
    return XID_INFO.get(code, {"severity": "warning", "meaning": f"Xid {code}",
                               "guidance": "Look the code up in the NVIDIA Xid catalogue."})


def parse_xid(line: str) -> dict | None:
    """Parse one `journalctl -o short-unix` line. Pure."""
    ts, _, rest = line.partition(" ")
    m = _XID.search(rest)
    if not m:
        return None
    try:
        t = float(ts)
    except ValueError:
        return None
    code = int(m["code"])
    return {"t": t, "code": code, "pci": m["pci"], "pid": (m["pid"] or "").strip() or None,
            "process": (m["name"] or "").strip() or None, "message": m["msg"].strip()[:300],
            **{k: v for k, v in xid_info(code).items() if k in ("severity", "meaning")}}


@dataclass(frozen=True)
class Access:
    mode: str                       # "local" | "ssh"
    targets: tuple[str, ...] = ()   # ssh destinations, tried in order


def access_for(node: Node) -> Access:
    if node.host == LOCAL_HOST:
        return Access("local")
    # LAN first by IP: the MagicDNS name routes to Tailscale SSH, which can hold a session open waiting for a
    # browser re-authentication ("check" mode). Tailscale is the fallback (Node-4 only accepts Tailscale SSH).
    return Access("ssh", (f"{SSH_USER}@{node.lan_ip}", f"{SSH_USER}@{node.ts_name}"))


def _command(since: float, boot: str) -> str:
    if not _BOOT.match(boot):
        raise ValueError("boot must be an integer offset")
    j = f"journalctl -k -b {boot} --since=@{int(since)} --no-pager"
    return (f"{j} -o short-unix --grep 'NVRM: Xid' | tail -n {MAX_XID_LINES}; echo {_SPLIT}; "
            f"{j} -o cat --grep 'bad register read' | wc -l")


# One persistent SSH connection per node, reused for every check (detector, services, security events, CVE scan).
# Without it each check is a fresh login: ~2,400 "Accepted publickey" lines a day per node in the nodes' auth logs,
# which drowned real sign-ins on the Security page. A new login now happens at most once per 15 minutes per node.
SSH_CM_DIR = Path.home() / ".ssh" / "aiops-cm"
SSH_REUSE = ["-o", "ControlMaster=auto", "-o", f"ControlPath={SSH_CM_DIR}/%C", "-o", "ControlPersist=15m"]
SSH_CM_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)

_preferred: dict[tuple[str, ...], str] = {}   # remember the destination that last worked


def run(acc: Access, cmd: str) -> subprocess.CompletedProcess:
    """Run a read-only command on a node: locally, or over SSH (LAN first, Tailscale as fallback)."""
    if acc.mode == "local":
        return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=CMD_TIMEOUT_S)
    first = _preferred.get(acc.targets)
    order = ([first] if first else []) + [t for t in acc.targets if t != first]
    r = None
    for target in order:
        try:
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "-o", "StrictHostKeyChecking=accept-new",
                                *SSH_REUSE, target, cmd], capture_output=True, text=True, timeout=CMD_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:    # e.g. Tailscale SSH waiting for a browser check: try the next path
            r = subprocess.CompletedProcess(e.cmd, 255, "", f"ssh to {target} timed out after {CMD_TIMEOUT_S}s "
                                            "(Tailscale SSH may need re-authentication)")
            if _preferred.get(acc.targets) == target:
                _preferred.pop(acc.targets)
            continue
        if "Tailscale SSH requires an additional check" in (r.stderr or ""):
            r = subprocess.CompletedProcess(r.args, 255, r.stdout, "Tailscale SSH needs re-authentication in a browser")
            continue
        if r.returncode != 255:          # 255 = ssh itself failed (auth/connect); anything else reached the node
            _preferred[acc.targets] = target
            return r
    return r


def replay_boot() -> str | None:
    """An earlier boot selected for replaying a past failure (evaluation only), else None."""
    b = os.environ.get("AIOPS_KERNEL_BOOT", "0")
    return None if b == "0" else b


def read_gpu_events(node: Node, since: float, boot: str | None = None) -> dict:
    """GPU driver events on `node` since epoch `since` (current boot by default).

    Returns {"access", "ok", "error", "xids": [...], "bad_register_reads": int, "checked": t}.
    `boot` (or AIOPS_KERNEL_BOOT) selects an earlier boot for replaying past failures.
    """
    boot = boot or os.environ.get("AIOPS_KERNEL_BOOT", "0")
    acc = access_for(node)
    out = {"access": acc.mode, "ok": False, "error": None, "xids": [], "bad_register_reads": 0, "checked": time.time()}
    try:
        r = run(acc, _command(since, boot))
    except subprocess.TimeoutExpired:
        out["error"] = f"{acc.mode} timed out after {CMD_TIMEOUT_S}s"
        return out
    except (OSError, ValueError) as e:
        out["error"] = str(e)
        return out
    if _SPLIT not in r.stdout:
        err = (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1]
        out["error"] = err[:200]
        return out
    xid_part, _, count_part = r.stdout.partition(_SPLIT)
    out["xids"] = [x for x in map(parse_xid, xid_part.strip().splitlines()) if x]
    if replay_boot() and out["xids"]:
        # Replay an earlier boot as if it were happening now: keep the relative timing, end
        # ~90 s ago. The replay then reproduces a live incident instead of a historical one.
        shift = (time.time() - 90) - max(x["t"] for x in out["xids"])
        for x in out["xids"]:
            x["t"] += shift
    out["bad_register_reads"] = int(count_part.strip() or 0)
    out["ok"] = True
    return out


def summarize(xids: list[dict]) -> list[dict]:
    """Group events by Xid code: count, first/last time, processes. Pure."""
    groups: dict[int, dict] = {}
    for x in xids:
        g = groups.setdefault(x["code"], {"code": x["code"], "count": 0, "first": x["t"], "last": x["t"],
                                          "processes": set(), "example": x["message"], **xid_info(x["code"])})
        g["count"] += 1
        g["first"], g["last"] = min(g["first"], x["t"]), max(g["last"], x["t"])
        if x["process"]:
            g["processes"].add(f"{x['process']} (pid {x['pid']})")
    out = [g | {"processes": sorted(g["processes"])} for g in groups.values()]
    return sorted(out, key=lambda g: (-SEVERITY_RANK[g["severity"]], g["first"]))

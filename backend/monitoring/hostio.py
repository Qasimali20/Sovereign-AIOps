"""Disk I/O, network errors and Wi-Fi link quality on every node.

Spark Monitor reports disk space and network throughput only. Storage and network faults show up elsewhere: in the
kernel's block-device and interface counters (/proc/diskstats, /proc/net/dev), in the interface link state, in the
Wi-Fi station counters (every node reaches the office network over Wi-Fi) and in kernel messages (NVMe timeouts,
I/O errors, filesystems remounted read-only, NIC watchdog resets). This module reads all of that in one read-only
command per node over the existing SSH connection; nothing is installed on the nodes.

Counters are cumulative, so rates need two samples: `rates` is pure over (previous sample, current sample)."""
from __future__ import annotations

import re
import subprocess
import time

from backend.core.config import Node
from backend.monitoring import kernel

_SPLIT = "__AIOPS_IO__"
MAX_KERNEL_LINES = 50
# whole block devices only: partitions, loop and ram devices would double-count or add noise
_DISK = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+)$")
# physical interfaces: Docker bridges, veth pairs and the Tailscale tunnel are not hardware
PHYSICAL_NIC = re.compile(r"^(en|eth|wl|ib)")
# kernel messages that mean storage or network hardware trouble. Grouped by what an operator does about them.
KERNEL_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("disk", "filesystem remounted read-only", re.compile(r"Remounting filesystem read-only|remounted read-only", re.I)),
    ("disk", "filesystem error", re.compile(r"(EXT4|XFS|BTRFS)-fs.*error", re.I)),
    ("disk", "NVMe controller timeout or reset", re.compile(r"nvme\S*:.*(timeout|reset|not ready|failed)", re.I)),
    ("disk", "block device I/O error", re.compile(r"I/O error", re.I)),
    ("net", "NIC transmit watchdog timeout", re.compile(r"NETDEV WATCHDOG", re.I)),
    ("net", "network link down", re.compile(r"\bLink is Down\b|\blink down\b", re.I)),
]
_KERNEL_GREP = "Remounting filesystem read-only|remounted read-only|-fs.*error|nvme.*(timeout|reset|not ready|failed)|" \
               "I/O error|NETDEV WATCHDOG|Link is Down|link down"


def _command(since: float) -> str:
    return ("cat /proc/diskstats; echo " + _SPLIT + "; cat /proc/net/dev; echo " + _SPLIT + "; "
            "for i in /sys/class/net/*; do echo \"${i##*/} $(cat $i/operstate 2>/dev/null)\"; done; echo " + _SPLIT + "; "
            "for w in /sys/class/net/*/wireless; do d=${w%/wireless}; d=${d##*/}; [ -e \"$w\" ] && "
            "{ echo \"iface $d\"; iw dev \"$d\" station dump 2>/dev/null; }; done; echo " + _SPLIT + "; "
            f"journalctl -k -b --since=@{int(since)} --no-pager -o short-unix --grep '{_KERNEL_GREP}' 2>/dev/null "
            f"| grep -v '^-- ' | tail -n {MAX_KERNEL_LINES}")


# ---------------------------------------------------------------- parsing (pure)
def parse_diskstats(text: str) -> dict[str, dict]:
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) < 14 or not _DISK.match(f[2]):
            continue
        out[f[2]] = {"reads": int(f[3]), "read_bytes": int(f[5]) * 512, "writes": int(f[7]),
                     "write_bytes": int(f[9]) * 512, "io_ms": int(f[12])}
    return out


def parse_netdev(text: str) -> dict[str, dict]:
    out = {}
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        f = rest.split()
        if not sep or len(f) < 16 or not PHYSICAL_NIC.match(name.strip()):
            continue
        out[name.strip()] = {"rx_bytes": int(f[0]), "rx_errors": int(f[2]), "rx_dropped": int(f[3]),
                             "tx_bytes": int(f[8]), "tx_errors": int(f[10]), "tx_dropped": int(f[11])}
    return out


def parse_operstate(text: str) -> dict[str, str]:
    return {f[0]: f[1] for f in (l.split() for l in text.splitlines()) if len(f) == 2 and PHYSICAL_NIC.match(f[0])}


_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def parse_wifi(text: str) -> dict[str, dict]:
    """`iw dev <if> station dump`, one block per interface (prefixed "iface <name>")."""
    out: dict[str, dict] = {}
    cur = None
    for line in text.splitlines():
        if line.startswith("iface "):
            cur = out.setdefault(line.split()[1], {})
            continue
        key, sep, val = line.strip().partition(":")
        if cur is None or not sep:
            continue
        m = _NUM.search(val)
        if not m:
            continue
        k = {"signal": "signal_dbm", "signal avg": "signal_avg_dbm", "tx bitrate": "tx_mbps", "rx bitrate": "rx_mbps",
             "tx retries": "tx_retries", "tx failed": "tx_failed", "beacon loss": "beacon_loss",
             "connected time": "connected_s"}.get(key.strip())
        if k:
            cur[k] = float(m.group())
    return {k: v for k, v in out.items() if v}          # an interface with no station is not associated


def classify_kernel(line: str) -> dict | None:
    ts, _, msg = line.partition(" ")
    try:
        t = float(ts)
    except ValueError:
        return None
    msg = msg.split(": ", 1)[-1] if "kernel: " in msg else msg
    for area, what, rx in KERNEL_PATTERNS:
        if rx.search(msg):
            dev = re.search(r"\b(nvme\d+(?:n\d+)?(?:p\d+)?|sd[a-z]+\d*|en\w+|eth\d+|wl\w+)\b", msg)
            return {"t": t, "area": area, "what": what, "device": dev.group(1) if dev else None, "message": msg.strip()[:300]}
    return None


def parse(out: str) -> dict:
    parts = out.split(_SPLIT)
    if len(parts) < 5:
        raise ValueError("unexpected collector output")
    return {"disks": parse_diskstats(parts[0]), "nics": parse_netdev(parts[1]), "links": parse_operstate(parts[2]),
            "wifi": parse_wifi(parts[3]), "kernel": [k for k in map(classify_kernel, parts[4].strip().splitlines()) if k]}


# ---------------------------------------------------------------- rates (pure)
def rates(prev: dict | None, cur: dict) -> dict:
    """Per-second rates between two samples ({"t", "disks", "nics", "wifi"}). Counter resets (reboot) read as no data."""
    out = {"disks": {}, "nics": {}, "wifi": {}}
    if not prev or cur["t"] <= prev["t"]:
        return out
    dt = cur["t"] - prev["t"]

    def delta(a, b, k):
        return b[k] - a[k] if b[k] >= a[k] else None

    for dev, b in cur["disks"].items():
        a = prev["disks"].get(dev)
        if not a:
            continue
        busy = delta(a, b, "io_ms")
        rb, wb = delta(a, b, "read_bytes"), delta(a, b, "write_bytes")
        out["disks"][dev] = {"busy_ratio": None if busy is None else min(1.0, busy / (dt * 1000)),
                             "read_bytes_per_s": None if rb is None else rb / dt,
                             "write_bytes_per_s": None if wb is None else wb / dt}
    for nic, b in cur["nics"].items():
        a = prev["nics"].get(nic)
        if a:
            out["nics"][nic] = {k: delta(a, b, k) for k in ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped")}
    for nic, b in cur["wifi"].items():
        a = prev["wifi"].get(nic)
        if a and "tx_retries" in a and "tx_retries" in b:
            out["wifi"][nic] = {"tx_retries": delta(a, b, "tx_retries"), "tx_failed": delta(a, b, "tx_failed")
                                if "tx_failed" in a and "tx_failed" in b else None}
    return out


# ---------------------------------------------------------------- collection
def collect(node: Node, since: float) -> dict:
    """One sample: counters, link state, Wi-Fi station and kernel storage/network messages since `since`."""
    now = time.time()
    try:
        r = kernel.run(kernel.access_for(node), _command(since))
        if r.returncode == 255:
            raise RuntimeError((r.stderr or "ssh failed").strip().splitlines()[-1])
        data = parse(r.stdout)
    except (subprocess.TimeoutExpired, OSError, RuntimeError, ValueError) as e:
        return {"ok": False, "error": str(e)[:200], "t": now}
    data["kernel"] = [k for k in data["kernel"] if k["t"] > since]
    return {"ok": True, "error": None, "t": now, **data}

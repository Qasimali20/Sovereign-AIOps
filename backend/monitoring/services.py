"""Application service monitoring: Docker containers and systemd units on every node.

Read-only (`docker ps`, `systemctl list-units`) over the same SSH paths as the GPU and security
collectors. Detection is transition-based so a fleet's long-stopped containers never page anyone:
a container alerts when it turns unhealthy, restarts in a loop, or stops after having been seen
running. Known-benign failed units are listed with a reason and shown, never alerted on."""
from __future__ import annotations

from pathlib import Path

import os

import json
import re
import time
from dataclasses import dataclass, field

from backend.core import config
from backend.monitoring import kernel

# Units that fail by design on this hardware: shown as "known", never alerted on.
KNOWN_FAILED_UNITS = {
    "iwpmd.service": "iWARP port mapper; there is no iWARP NIC on DGX Spark, so it always fails at boot",
}
# Containers owned by the platform's own tests: covered by their own rules or disposable.
OWN_CONTAINERS = re.compile(r"^(aiops-test-svc(-[0-9]+)?|aiops-inject-svc)$")
_HEALTH = re.compile(r"\((healthy|unhealthy|health: starting)\)")
_SPLIT = "__AIOPS_SVC__"
# systemd units that are part of the OS rather than an application
_OS_UNITS = re.compile(
    r"^(systemd|dbus|cron|ssh|snap|accounts|avahi|bluetooth|colord|cups|gdm|kerneloops|ModemManager|networkd|"
    r"NetworkManager|polkit|power|rsyslog|rtkit|switcheroo|udisks|upower|user@|wpa|getty|irqbalance|nvidia|containerd|"
    r"docker|fwupd|gnome|kmod|lvm|multipathd|packagekit|smartmontools|thermald|unattended|whoopsie|rpc|tailscaled|"
    r"chrony|fail2ban|apport|serial|setvtrgb|sssd|keyboard|console|alsa|blk|plymouth|nfs|wireplumber|pipewire|"
    r"lldpd|rasdaemon|rdma|iperf3|fsidd|xrdp|dgx|spark-monitor)")


def _cmd() -> str:
    return ("docker ps -a --format '{{json .}}' 2>/dev/null || echo DOCKER_DENIED; echo " + _SPLIT + "; "
            "systemctl list-units --type=service --all --no-legend --plain --state=failed,auto-restart 2>/dev/null; echo " + _SPLIT + "; "
            "systemctl list-units --type=service --state=running --no-legend --plain 2>/dev/null")


def parse(out: str) -> dict:
    """Pure: the collector's combined output -> containers, problem units, application units."""
    parts = out.split(_SPLIT)
    if len(parts) < 3:
        raise ValueError("unexpected collector output")
    dock, problems, running = parts[0], parts[1], parts[2]
    containers, docker_ok = [], "DOCKER_DENIED" not in dock
    for line in dock.strip().splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        m = _HEALTH.search(c.get("Status", ""))
        containers.append({"name": c.get("Names", ""), "state": c.get("State", ""), "status": c.get("Status", ""),
                           "health": m.group(1).replace("health: ", "") if m else None, "image": c.get("Image", "")})
    unit_rows = [l.split() for l in problems.strip().splitlines() if l.strip()]
    failed = [{"unit": r[0], "sub": r[3] if len(r) > 3 else "", "known": KNOWN_FAILED_UNITS.get(r[0])} for r in unit_rows]
    apps = [l.split()[0] for l in running.strip().splitlines() if l.strip() and not _OS_UNITS.match(l.split()[0])]
    return {"docker_ok": docker_ok, "containers": sorted(containers, key=lambda c: c["name"]),
            "problem_units": failed, "app_units": sorted(apps)}


def collect(node: config.Node) -> dict:
    try:
        r = kernel.run(kernel.access_for(node), _cmd())
        if r.returncode == 255:                 # ssh never reached the node
            raise RuntimeError((r.stderr or "ssh failed").strip().splitlines()[-1])
        data = parse(r.stdout)
    except Exception as e:  # timeouts, ssh failure, malformed output
        return {"ok": False, "error": str(e)[:200], "checked": time.time()}
    return {"ok": True, "error": None, "checked": time.time(), **data}


def summarize(snap: dict) -> dict:
    if not snap.get("ok"):
        return {"ok": False}
    cs = [c for c in snap["containers"] if not OWN_CONTAINERS.match(c["name"])]
    running = [c for c in cs if c["state"] == "running"]
    return {"ok": True, "docker_ok": snap["docker_ok"], "containers_running": len(running),
            "containers_unhealthy": sum(c["health"] == "unhealthy" for c in cs),
            "containers_restarting": sum(c["state"] == "restarting" for c in cs),
            "units_failed": sum(not u["known"] for u in snap["problem_units"]),
            "units_known_failed": sum(bool(u["known"]) for u in snap["problem_units"]),
            "app_units": len(snap["app_units"])}


@dataclass
class ServiceAlert:
    kind: str
    node: str
    resource: str
    title: str
    detail: dict

    @property
    def fingerprint(self) -> str:
        return f"{self.kind}:{self.node}:{self.resource}"


@dataclass
class Baseline:
    running: dict[str, set[str]] = field(default_factory=dict)     # node -> containers seen running


DECOMMISSIONED_PATH = Path(os.environ.get("AIOPS_DECOMMISSIONED", Path(__file__).resolve().parents[2] / "data" / "decommissioned.json"))


def decommissioned() -> dict[str, dict]:
    """Containers stopped on purpose: {"Node-1/agpb-postgres-1": {"by": ..., "at": ..., "reason": ...}}. A stop of one of
    these is planned work, not an incident. Edited only through decommission() so every entry is audited."""
    try:
        return json.loads(DECOMMISSIONED_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def decommission(node: str, container: str, by: str, reason: str) -> None:
    from backend.core import store
    reg = decommissioned()
    reg[f"{node}/{container}"] = {"by": by, "at": time.time(), "reason": reason}
    DECOMMISSIONED_PATH.write_text(json.dumps(reg, indent=1, sort_keys=True))
    store.audit(None, by, "container_decommissioned", {"node": node, "container": container, "reason": reason})


def alerts(node: str, snap: dict, base: Baseline) -> list[ServiceAlert]:
    """Pure over (snapshot, baseline); updates the baseline in place."""
    if not snap.get("ok"):
        return []
    out: list[ServiceAlert] = []
    seen = base.running.setdefault(node, set())
    planned = decommissioned()
    now_running = set()
    for c in snap["containers"]:
        if OWN_CONTAINERS.match(c["name"]):
            continue
        d = {"container": c["name"], "state": c["state"], "status": c["status"], "health": c["health"], "image": c["image"]}
        if c["state"] == "running":
            now_running.add(c["name"])
        if c["health"] == "unhealthy":
            out.append(ServiceAlert("svc_container_unhealthy", node, c["name"], f"Container {c['name']} on {node} is unhealthy", d))
        elif c["state"] == "restarting":
            out.append(ServiceAlert("svc_container_restarting", node, c["name"], f"Container {c['name']} on {node} is restarting repeatedly", d))
        elif c["name"] in seen and c["state"] in ("exited", "dead") and f"{node}/{c['name']}" not in planned:
            out.append(ServiceAlert("svc_container_stopped", node, c["name"], f"Container {c['name']} on {node} stopped ({c['status']})", d))
            seen.discard(c["name"])          # one alert per running -> stopped transition
    seen |= now_running
    for u in snap["problem_units"]:
        if u["known"]:
            continue
        kind = "svc_unit_restarting" if u["sub"] == "auto-restart" else "svc_unit_failed"
        out.append(ServiceAlert(kind, node, u["unit"], f"Service {u['unit']} on {node} {'keeps restarting' if kind.endswith('restarting') else 'has failed'}",
                                {"unit": u["unit"], "sub": u["sub"]}))
    return out

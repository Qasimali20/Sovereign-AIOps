"""Configuration drift: what is configured on each node now, against an accepted baseline.

Read-only, over the same SSH connection as the other collectors; nothing is installed on the nodes. Recorded per
node:
- host: kernel, OS release, NVIDIA driver, Docker and CUDA versions;
- unit: every application systemd unit (system units and the tmc user's units) as `systemctl cat` prints it,
  drop-ins included, plus whether it is enabled. This is where model servers are configured (memory share,
  context length, flags);
- container: every long-lived container (restart policy other than "no"): image and image id, command, restart
  policy, ports, mounts, and its environment variable names with a hash of each value (values never leave the node);
- security: SSH daemon configuration, the tmc account's authorized keys (fingerprints) and the sudoers.d file names.

The first reading of a node becomes its baseline. A change against the baseline is drift: an incident per
changed item, with a diff. A person either puts the item back (the incident then clears) or accepts the current
configuration as the new baseline; acceptance is audited. Changes made inside a maintenance window are recorded
as planned work. Secrets in unit files (Environment= values that look like keys, tokens or passwords) are
masked before anything is stored."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from backend.core import config
from backend.monitoring import kernel

BASELINE_PATH = Path(os.environ.get("AIOPS_DRIFT_BASELINE", config.ROOT / "data" / "drift_baseline.json"))
CHECK_INTERVAL_S = 300
_SPLIT = "__AIOPS_DRIFT__"
_UNIT_SPLIT = "__AIOPS_UNIT__"
MAX_DIFF_LINES = 80
_lock = threading.Lock()

# Units of the operating system itself (same list as the service monitor) and NVIDIA's system management (nvsm).
# Aliases (display-manager, syslog) are symlinks and skipped by the collector.
from backend.monitoring.services import OWN_CONTAINERS, _OS_UNITS   # noqa: E402
_VENDOR_UNITS = re.compile(r"^nvsm")

_SECRET_ENV = re.compile(r"(?i)^(\s*Environment=\"?[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|KEY)[A-Z0-9_]*=)([^\s\"]+)")


def _command() -> str:
    units = ("for f in /etc/systemd/system/*.service; do [ -L \"$f\" ] && continue; u=${f##*/}; echo \"" + _UNIT_SPLIT + " system $u $(systemctl is-enabled $u 2>/dev/null)\"; "
             "systemctl cat \"$u\" 2>/dev/null; done; "
             "for f in $HOME/.config/systemd/user/*.service; do [ -e \"$f\" ] || continue; u=${f##*/}; "
             "echo \"" + _UNIT_SPLIT + " user $u $(systemctl --user is-enabled $u 2>/dev/null)\"; systemctl --user cat \"$u\" 2>/dev/null; done")
    containers = ("docker ps -aq 2>/dev/null | xargs -r docker inspect --format "
                  "'{{json .Name}}\t{{json .Config.Image}}\t{{json .Image}}\t{{json .HostConfig.RestartPolicy.Name}}\t"
                  "{{json .Config.Cmd}}\t{{json .HostConfig.PortBindings}}\t{{json .Mounts}}\t{{json .Config.Env}}' 2>/dev/null")
    host = ("echo kernel=$(uname -r); . /etc/os-release; echo os=$VERSION_ID; "
            "echo nvidia_driver=$(grep -oE '[0-9]+\\.[0-9]+(\\.[0-9]+)?' /proc/driver/nvidia/version 2>/dev/null | head -1); "
            "echo docker=$(docker version --format '{{.Server.Version}}' 2>/dev/null); "
            "echo cuda=$(readlink -f /usr/local/cuda 2>/dev/null | sed 's|.*/cuda-||')")
    security = ("echo sshd_config=$(cat /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null | grep -vE '^\\s*(#|$)' | sha256sum | cut -c1-16); "
                "echo authorized_keys=$(ssh-keygen -lf $HOME/.ssh/authorized_keys 2>/dev/null | awk '{print $2}' | sort | tr '\\n' ' '); "
                "echo sudoers_d=$(ls /etc/sudoers.d 2>/dev/null | sort | tr '\\n' ' ')")
    return f"{host}; echo {_SPLIT}; {security}; echo {_SPLIT}; {units}; echo {_SPLIT}; {containers}"


# ---------------------------------------------------------------- parsing (pure)
def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _kv(text: str, prefix: str) -> dict[str, dict]:
    out = {}
    for line in text.strip().splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip():
            out[f"{prefix}:{k.strip()}"] = {"value": v.strip(), "hash": _h(v.strip())}
    return out


def mask_secrets(unit_text: str) -> str:
    return "\n".join(_SECRET_ENV.sub(r"\1<masked>", line) for line in unit_text.splitlines())


def parse_units(text: str) -> dict[str, dict]:
    out = {}
    for block in text.split(_UNIT_SPLIT)[1:]:
        head, _, body = block.partition("\n")
        f = head.split()
        if len(f) < 2 or _OS_UNITS.match(f[1]) or _VENDOR_UNITS.match(f[1]):
            continue
        scope, name, enabled = f[0], f[1], (f[2] if len(f) > 2 else "unknown")
        body = mask_secrets(body.rstrip())
        content = f"# enabled: {enabled}\n{body}"
        key = f"unit:{name}" if scope == "system" else f"unit:user/{name}"
        out[key] = {"value": f"{enabled}, {len(body.splitlines())} lines", "hash": _h(content), "content": content}
    return out


def parse_containers(text: str) -> dict[str, dict]:
    out = {}
    for line in text.strip().splitlines():
        try:
            name, image, image_id, restart, cmd, ports, mounts, env = (json.loads(x) for x in line.split("\t"))
        except ValueError:
            continue
        name = str(name).lstrip("/")
        if restart in ("", "no") or OWN_CONTAINERS.match(name):
            continue                              # one-off containers come and go; long-lived services are configuration
        envs = sorted(f"{e.partition('=')[0]}=#{_h(e.partition('=')[2])[:8]}" for e in env or [])
        spec = {"image": image, "image_id": str(image_id)[:19], "restart": restart, "command": cmd,
                "ports": sorted(f"{k}->{','.join(b.get('HostPort', '') for b in v or [])}" for k, v in (ports or {}).items()),
                "mounts": sorted(f"{m.get('Source') or m.get('Name')}:{m.get('Destination')}" for m in mounts or []),
                "env": envs}
        content = json.dumps(spec, indent=1)
        out[f"container:{name}"] = {"value": f"{image} ({spec['image_id']})", "hash": _h(content), "content": content}
    return out


def parse(out: str) -> dict[str, dict]:
    parts = out.split(_SPLIT)
    if len(parts) < 4:
        raise ValueError("unexpected collector output")
    return _kv(parts[0], "host") | _kv(parts[1], "security") | parse_units(parts[2]) | parse_containers(parts[3])


def collect(node: config.Node) -> dict:
    try:
        r = kernel.run(kernel.access_for(node), _command())
        if r.returncode == 255:
            raise RuntimeError((r.stderr or "ssh failed").strip().splitlines()[-1])
        items = parse(r.stdout)
    except (subprocess.TimeoutExpired, OSError, RuntimeError, ValueError) as e:
        return {"ok": False, "error": str(e)[:200], "checked": time.time()}
    return {"ok": True, "error": None, "checked": time.time(), "items": items}


# ---------------------------------------------------------------- comparison (pure)
def _diff(a: dict | None, b: dict | None) -> str:
    before = (a or {}).get("content", (a or {}).get("value", ""))
    after = (b or {}).get("content", (b or {}).get("value", ""))
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), "baseline", "now", n=1, lineterm=""))
    return "\n".join(lines[:MAX_DIFF_LINES] + ([f"... {len(lines) - MAX_DIFF_LINES} more lines"] if len(lines) > MAX_DIFF_LINES else []))


def compare(baseline: dict[str, dict], current: dict[str, dict]) -> list[dict]:
    out = []
    for key in sorted(set(baseline) | set(current)):
        a, b = baseline.get(key), current.get(key)
        if a and b and a["hash"] == b["hash"]:
            continue
        change = "added" if a is None else "removed" if b is None else "changed"
        out.append({"key": key, "change": change, "before": (a or {}).get("value"), "after": (b or {}).get("value"),
                    "diff": _diff(a, b)})
    return out


def fleet_consistency(snaps: dict[str, dict]) -> dict[str, dict[str, str]]:
    """Host versions side by side; only the items where the nodes disagree."""
    keys = sorted({k for s in snaps.values() for k in (s.get("items") or {}) if k.startswith("host:")})
    table = {k: {n: (s.get("items") or {}).get(k, {}).get("value") for n, s in sorted(snaps.items()) if s.get("ok")} for k in keys}
    return {k: v for k, v in table.items() if len(set(v.values())) > 1}


# ---------------------------------------------------------------- baseline
def load_baseline() -> dict[str, dict]:
    try:
        return json.loads(BASELINE_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save(b: dict) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = BASELINE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(b, indent=1, sort_keys=True))
    os.chmod(tmp, 0o600)
    tmp.replace(BASELINE_PATH)


def ensure_baseline(node: str, items: dict[str, dict]) -> bool:
    """The first reading of a node becomes its baseline. Returns True if one was created."""
    from backend.core import store
    with _lock:
        b = load_baseline()
        if node in b:
            return False
        b[node] = {"at": time.time(), "by": "platform: first reading", "items": items}
        _save(b)
    store.audit(None, "drift", "baseline_created", {"node": node, "items": len(items)})
    return True


def accept(node: str, current: dict[str, dict], keys: list[str] | None, by: str, reason: str) -> list[str]:
    """Make the current configuration of `keys` (all differing items when None) the baseline. Audited per item."""
    from backend.core import store
    with _lock:
        b = load_baseline()
        base = b.setdefault(node, {"at": time.time(), "by": by, "items": {}})
        changes = {c["key"]: c for c in compare(base["items"], current)}
        todo = sorted(changes) if keys is None else [k for k in keys if k in changes]
        for k in todo:
            if k in current:
                base["items"][k] = current[k]
            else:
                base["items"].pop(k, None)
        base["updated"], base["updated_by"] = time.time(), by
        _save(b)
    for k in todo:
        store.audit(None, by, "drift_accepted", {"node": node, "item": k, "change": changes[k]["change"],
                                                 "before": changes[k]["before"], "after": changes[k]["after"], "reason": reason})
    return todo

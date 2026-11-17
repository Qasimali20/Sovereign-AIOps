"""The designated test service's deployment: the only thing write operations can change.

Every Docker command here is fixed. The only values that reach a command line are a release from
RELEASES and a replica count from REPLICAS, both validated here as well as by the policy engine.
Deployment history (which release ran when, deployed by whom) is kept so rollback has a defined
target: the previous release in the history.

Layout on Node-2:
  aiops-test-svc     primary, 127.0.0.1:18080 (the registered health URL)
  aiops-test-svc-2   replica, 127.0.0.1:18083
  aiops-test-svc-3   replica, 127.0.0.1:18084
  volumes aiops-test-state (shared heartbeat + demand), aiops-test-cache (primary's cache)"""
from __future__ import annotations

import json
import subprocess
import time

from backend.core import config

PRIMARY = "aiops-test-svc"
IMAGE = "aiops-test-svc"
RELEASES = ("v1", "v2")
REPLICAS = range(1, 4)
PORTS = {1: 18080, 2: 18083, 3: 18084}
HISTORY = config.ROOT / "data" / "testsvc_deployments.json"


def _name(i: int) -> str:
    return PRIMARY if i == 1 else f"{PRIMARY}-{i}"


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def history() -> list[dict]:
    try:
        return json.loads(HISTORY.read_text())
    except (OSError, ValueError):
        return []


def _record(release: str, by: str, reason: str, bad: str | None = None) -> None:
    h = history()
    if bad:                                    # a release we rolled back from is never a rollback target again
        for e in h:
            if e["release"] == bad:
                e["rolled_back"] = True
    h += [{"release": release, "at": time.time(), "by": by, "reason": reason}]
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(h[-50:], indent=1))


def current_release() -> str | None:
    r = _docker("inspect", PRIMARY, "--format", "{{index .Config.Labels \"aiops.release\"}}")
    return r.stdout.strip() or None if r.returncode == 0 else None


def previous_release() -> str | None:
    """Rollback target: the most recent other release that was never itself rolled back."""
    cur = current_release()
    bad = {h["release"] for h in history() if h.get("rolled_back")}
    for h in reversed(history()):
        if h["release"] != cur and h["release"] not in bad:
            return h["release"]
    return None


def running_replicas() -> int:
    r = _docker("ps", "--filter", "label=aiops.test=true", "--filter", "label=aiops.role", "--format", "{{.Names}}")
    return len([n for n in r.stdout.split() if n == PRIMARY or n.startswith(PRIMARY + "-")])


def _run(i: int, release: str) -> subprocess.CompletedProcess:
    _docker("rm", "-f", _name(i))
    args = ["run", "-d", "--name", _name(i), "--restart", "no",
            "--label", "aiops.test=true", "--label", f"aiops.release={release}",
            "--label", f"aiops.role={'primary' if i == 1 else 'replica'}",
            "-e", f"NAME={_name(i)}", "-p", f"127.0.0.1:{PORTS[i]}:8080",
            "-v", "aiops-test-state:/state", "--memory", "128m", "--cpus", "0.5"]
    if i == 1:
        args += ["-v", "aiops-test-cache:/cache"]
    return _docker(*args, f"{IMAGE}:{release}")


def _result(steps: list[subprocess.CompletedProcess], **extra) -> dict:
    return {"ok": all(s.returncode == 0 for s in steps),
            "steps": [{"cmd": " ".join(s.args[:3]) + " ...", "rc": s.returncode, "err": s.stderr[-300:]} for s in steps],
            **extra}


def deploy(release: str, by: str, reason: str, _bad: str | None = None) -> dict:
    """(Re)deploy every running instance on `release`, keeping the replica count."""
    if release not in RELEASES:
        raise ValueError(f"unknown release {release!r}")
    n = max(1, running_replicas())
    steps = [_run(i, release) for i in range(1, n + 1)]
    _record(release, by, reason, _bad)
    return _result(steps, release=release, replicas=n)


def rollback(by: str) -> dict:
    cur, target = current_release(), previous_release()
    if target is None:
        return {"ok": False, "error": "no earlier good release in the deployment history"}
    return deploy(target, by, f"rollback from {cur}", _bad=cur) | {"rolled_back_from": cur, "rolled_back_to": target}


def scale(replicas: int, by: str) -> dict:
    if replicas not in REPLICAS:
        raise ValueError(f"replicas must be within {REPLICAS.start}..{REPLICAS.stop - 1}")
    release = current_release() or "v1"
    steps = [_run(i, release) for i in range(2, replicas + 1) if _docker("inspect", _name(i)).returncode != 0
             or "running" not in _docker("inspect", _name(i), "--format", "{{.State.Status}}").stdout]
    steps += [_docker("rm", "-f", _name(i)) for i in range(replicas + 1, REPLICAS.stop)
              if _docker("inspect", _name(i)).returncode == 0]
    return _result(steps, replicas=replicas, by=by)


def clear_cache(by: str) -> dict:
    before = _docker("exec", PRIMARY, "du", "-sm", "/cache").stdout.split()[:1]
    step = _docker("exec", PRIMARY, "sh", "-c", "rm -rf /cache/* /cache/.[!.]* 2>/dev/null; true")
    after = _docker("exec", PRIMARY, "du", "-sm", "/cache").stdout.split()[:1]
    return _result([step], cache_mb_before=before[0] if before else None, cache_mb_after=after[0] if after else None, by=by)


def restart(by: str) -> dict:
    return _result([_docker("restart", "--timeout", "10", PRIMARY)], by=by)


def status() -> dict:
    return {"release": current_release(), "previous": previous_release(), "replicas": running_replicas(),
            "history": history()[-5:]}

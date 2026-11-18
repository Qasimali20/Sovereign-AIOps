"""Controlled deployment: preview, confirm, record, apply, verify.

    uv run --group ops python ops/scripts/deploy.py <playbook> --reason "why" [--limit Node-4] [-e k=v ...]
        [--minutes 30] [--become] [--yes] [--accept-drift]

1. Preview: the playbook runs with --check --diff and nothing changes. If no task would change anything, stop.
2. Confirm: the preview is shown and a person types yes (or passes --yes).
3. Record: the platform records the deployment (who, playbook, nodes, commit, reason, preview) in the audit log
   and opens a maintenance window on those nodes, so incidents the change causes are recorded as planned work.
4. Apply: the playbook runs for real.
5. Verify: the deployment is closed as succeeded or failed with the per-node recap; the platform re-reads the
   configuration of the nodes and shows the drift the change caused. --accept-drift accepts that drift as the new
   baseline (audited, with the deployment id as the reason); otherwise an admin accepts it on the Drift page.

The platform API is called with the CLI token from .env (AIOPS_APPROVER_TOKEN), so the deployment is recorded
under the user that token is bound to."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "ops" / "ansible"
API = os.environ.get("AIOPS_API", "http://127.0.0.1:5170/api")
RECAP = re.compile(r"^(\S+)\s+:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", re.M)


def env(key: str) -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and k == key:
            return v.strip().strip("\"'")
    return ""


def recap(output: str) -> dict[str, dict]:
    return {m[1]: {"ok": int(m[2]), "changed": int(m[3]), "unreachable": int(m[4]), "failed": int(m[5])}
            for m in RECAP.finditer(output) if m[1] != "localhost"}


def playbook(name: str, args: argparse.Namespace, check: bool) -> tuple[int, str]:
    cmd = [str(Path(sys.executable).with_name("ansible-playbook")), f"playbooks/{name}.yml"]
    cmd += ["--limit", args.limit] if args.limit else []
    for e in args.extra or []:
        cmd += ["-e", e]
    cmd += ["--ask-become-pass"] if args.become else []
    cmd += ["--check", "--diff"] if check else ["--diff"]
    print(f"\n$ {' '.join(cmd[1:])}\n", flush=True)
    p = subprocess.Popen(cmd, cwd=ANSIBLE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         stdin=None if args.become else subprocess.DEVNULL)
    out = []
    for line in p.stdout:
        print(line, end="", flush=True)
        out.append(line)
    return p.wait(), "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("playbook", choices=sorted(p.stem for p in (ANSIBLE / "playbooks").glob("*.yml")))
    ap.add_argument("--reason", required=True)
    ap.add_argument("--limit")
    ap.add_argument("-e", dest="extra", action="append")
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--become", action="store_true", help="the playbook needs sudo: ask for the password")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--accept-drift", action="store_true")
    args = ap.parse_args()

    token = env("AIOPS_APPROVER_TOKEN")
    if not token:
        print("AIOPS_APPROVER_TOKEN is not set in .env: deployments must be recorded", file=sys.stderr)
        return 2
    headers = {"X-Approver-Token": token, "X-Requested-With": "aiops"}
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()

    rc, out = playbook(args.playbook, args, check=True)
    preview = recap(out)
    if rc != 0 or any(r["failed"] or r["unreachable"] for r in preview.values()):
        print("\npreview failed: nothing was changed", file=sys.stderr)
        return 1
    nodes = sorted(n for n, r in preview.items() if r["changed"])
    if not nodes:
        print("\nnothing to deploy: every node already matches")
        return 0
    print(f"\nwould change: {', '.join(f'{n} ({preview[n]['changed']} tasks)' for n in nodes)}; commit {commit}")
    if not args.yes and input("apply? type yes: ").strip() != "yes":
        print("not applied")
        return 1

    r = httpx.post(f"{API}/deployments", headers=headers, timeout=15,
                   json={"playbook": args.playbook, "nodes": nodes, "commit": commit, "reason": args.reason,
                         "minutes": args.minutes, "preview": preview})
    r.raise_for_status()
    did = r.json()["id"]
    print(f"\nrecorded as {did}; maintenance window open on {', '.join(nodes)} for {args.minutes:.0f} min")

    limit = args.limit
    args.limit = ",".join(nodes)                 # apply only where the preview showed a change
    rc, out = playbook(args.playbook, args, check=False)
    args.limit = limit
    result = recap(out)
    ok = rc == 0 and not any(r["failed"] or r["unreachable"] for r in result.values())

    r = httpx.post(f"{API}/deployments/{did}/finish", headers=headers, timeout=120, json={"ok": ok, "recap": result})
    r.raise_for_status()
    drift = r.json()["drift"]
    print(f"\n{did} {'succeeded' if ok else 'FAILED'}")
    for n, changes in drift.items():
        keys = [c["key"] for c in changes or []]
        print(f"  {n}: {'no drift' if not keys else 'drift: ' + ', '.join(keys)}")
        if keys and args.accept_drift and ok:
            a = httpx.post(f"{API}/drift/accept", headers=headers, timeout=30,
                           json={"node": n, "keys": keys, "reason": f"deployment {did} ({args.playbook}): {args.reason}"})
            a.raise_for_status()
            print(f"    accepted as the new baseline: {', '.join(a.json()['accepted'])}")
    if any(drift.values()) and not args.accept_drift:
        print("  review and accept the drift on the Drift page, or re-run with --accept-drift")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

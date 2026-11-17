#!/usr/bin/env python3
"""Operator CLI for the platform API.

  aiops.py incidents                 list incidents
  aiops.py show INC-...              full incident report / audit trail
  aiops.py approvals                 pending approvals with the proposal
  aiops.py approve APR-... [comment]
  aiops.py reject  APR-... [comment]
Approve/reject read AIOPS_APPROVER_TOKEN from the environment. The server records the user the token is bound to
(AIOPS_APPROVER_TOKEN_USER), never a name supplied by the client.
"""
import json
import os
import sys
import time
import urllib.request

API = os.environ.get("AIOPS_API", "http://127.0.0.1:5170")


def call(path, body=None, token=None):
    token = token or os.environ.get("AIOPS_APPROVER_TOKEN")
    req = urllib.request.Request(API + "/api" + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-Requested-With": "aiops",
                                          **({"X-Approver-Token": token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()}")


def ts(t):
    return time.strftime("%H:%M:%S", time.localtime(t)) if t else "-"


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "incidents"
    if cmd == "incidents":
        for i in call("/incidents"):
            print(f"{i['id']}  {ts(i['created'])}  {i['status']:18} {i['kind']:20} {i['title']}")
    elif cmd == "show":
        b = call(f"/incidents/{argv[2]}")
        rep = next((s["data"] for s in reversed(b["agent_steps"]) if s["node"] == "report"), None)
        print(json.dumps(rep or b["incident"], indent=2))
        print("\nAudit trail:")
        for a in b["audit_events"]:
            print(f"  {ts(a['created'])}  {a['actor']:22} {a['event']}")
    elif cmd == "approvals":
        for a in call("/approvals"):
            p = a["proposal"] or {}
            print(f"{a['id']}  incident {a['incident_id']}\n  operation: {p.get('operation')} -> "
                  f"{p.get('target')}\n  reason: {p.get('reason')}\n  expected: {p.get('expected_outcome')}\n"
                  f"  rollback: {p.get('rollback_plan')}\n  evidence: {p.get('evidence_refs')}\n")
    elif cmd in ("approve", "reject"):
        token = os.environ.get("AIOPS_APPROVER_TOKEN") or sys.exit("set AIOPS_APPROVER_TOKEN")
        # the server records the token's bound user (AIOPS_APPROVER_TOKEN_USER) as the approver
        print(call(f"/approvals/{argv[2]}", {"approve": cmd == "approve", "comment": " ".join(argv[3:])}, token))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)

#!/usr/bin/env python3
"""Manage platform users (run on Node-2 from the project folder).

  uv run ops/scripts/users.py list
  uv run ops/scripts/users.py add NAME approver|viewer      # prompts for the password
  uv run ops/scripts/users.py add NAME approver --generate  # prints a generated password once
  uv run ops/scripts/users.py delete NAME
"""
import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.security import auth  # noqa: E402


def main(a):
    if len(a) < 2 or a[1] == "list":
        for u in auth.list_users():
            print(f"{u['username']:20} {u['role']}")
    elif a[1] == "add" and len(a) >= 4:
        if "--generate" in a:
            pw = secrets.token_urlsafe(12)
        else:
            pw = getpass.getpass("Password (min 8 chars): ")
            if pw != getpass.getpass("Repeat: "):
                sys.exit("passwords do not match")
        auth.set_user(a[2], pw, a[3])
        print(f"saved {a[2]} ({a[3]})" + (f"\npassword: {pw}" if "--generate" in a else ""))
    elif a[1] == "delete" and len(a) == 3:
        print("deleted" if auth.delete_user(a[2]) else "no such user")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)

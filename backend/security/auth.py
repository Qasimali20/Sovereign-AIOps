"""Users, password hashing, signed session cookies, and roles.

Roles:
  viewer    read incidents, evidence, fleet
  approver  viewer + approve / reject proposed actions, close incidents
  admin     approver + manage users
The approver recorded on an approval is the logged-in user, never free text.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from backend.core.config import DB_PATH

USERS_PATH = DB_PATH.with_name("users.json")
SECRET_PATH = DB_PATH.with_name("session.key")
SESSION_TTL_S = 12 * 3600
ROLES = ("viewer", "approver", "admin")


def _secret() -> bytes:
    if not SECRET_PATH.exists():
        SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        SECRET_PATH.write_bytes(secrets.token_bytes(32))
        os.chmod(SECRET_PATH, 0o600)
    return SECRET_PATH.read_bytes()


_cache: tuple[str, float, dict] = ("", 0.0, {})


def _load() -> dict:
    """Users, re-read only when the file changes (every request authenticates through here)."""
    global _cache
    try:
        mtime = USERS_PATH.stat().st_mtime
    except FileNotFoundError:
        return {}
    if _cache[0] != str(USERS_PATH) or _cache[1] != mtime:
        _cache = (str(USERS_PATH), mtime, json.loads(USERS_PATH.read_text()))
    return json.loads(json.dumps(_cache[2]))           # callers may mutate: hand out a copy


def _save(users: dict) -> None:
    USERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = USERS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(USERS_PATH)


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def set_user(username: str, password: str, role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    users = _load()
    salt = secrets.token_bytes(16)
    old = users.get(username, {})
    # a new password or role ends every existing session of this user (gen is stamped into each token)
    users[username] = {"salt": salt.hex(), "hash": _hash(password, salt), "role": role,
                       "created": old.get("created", time.time()), "gen": old.get("gen", 0) + 1}
    _save(users)


def delete_user(username: str) -> bool:
    users = _load()
    if users.pop(username, None) is None:
        return False
    _save(users)
    return True


def list_users() -> list[dict]:
    return [{"username": u, "role": d["role"], "created": d.get("created")} for u, d in sorted(_load().items())]


def get_user(username: str) -> dict | None:
    u = _load().get(username)
    return {"username": username, "role": u["role"]} if u else None


def check_password(username: str, password: str) -> dict | None:
    u = _load().get(username)
    if not u:
        _hash(password, b"0" * 16)          # same cost for unknown users
        return None
    if not hmac.compare_digest(_hash(password, bytes.fromhex(u["salt"])), u["hash"]):
        return None
    return {"username": username, "role": u["role"]}


def make_session(user: dict) -> str:
    gen = _load().get(user["username"], {}).get("gen", 0)
    payload = json.dumps({"u": user["username"], "exp": int(time.time()) + SESSION_TTL_S, "g": gen,
                          "sid": secrets.token_hex(8)}).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_session(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return None
    if data.get("exp", 0) < time.time() or data.get("sid") in _revoked():
        return None
    u = _load().get(data.get("u"))        # role is read fresh; deleted users lose access
    if not u or data.get("g", 0) != u.get("gen", 0):      # password/role changed since this token was issued
        return None
    return {"username": data["u"], "role": u["role"]}


def _revoked_path():
    return USERS_PATH.with_name("revoked_sessions.json")


def _revoked() -> dict:
    try:
        return json.loads(_revoked_path().read_text())
    except (FileNotFoundError, ValueError):
        return {}


def revoke_session(token: str | None) -> None:
    """Logout: this token stops working now, not when it would have expired."""
    if not read_session(token):            # only a genuine, still-valid token can be revoked
        return
    try:
        data = json.loads(base64.urlsafe_b64decode(token.rsplit(".", 1)[0] + "=" * (-len(token.rsplit(".", 1)[0]) % 4)))
    except Exception:
        return
    if not data.get("sid"):
        return
    now = time.time()
    keep = {sid: exp for sid, exp in _revoked().items() if exp > now} | {data["sid"]: data.get("exp", now + SESSION_TTL_S)}
    tmp = _revoked_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(keep))
    os.chmod(tmp, 0o600)
    tmp.replace(_revoked_path())

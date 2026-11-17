"""Gateway API keys. Stored as SHA-256 digests (keys are 256-bit random, so a fast
hash is appropriate); the plaintext is shown once at creation. The file is shared
by the console (which manages keys) and the gateway (which re-reads it on change)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from backend.core.config import DB_PATH

PATH = DB_PATH.with_name("gateway_keys.json")
PREFIX = "sk-aiops-"
_lock = threading.Lock()
_cache: tuple[float, dict] = (-1.0, {})


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _load() -> dict:
    global _cache
    try:
        mtime = PATH.stat().st_mtime
    except FileNotFoundError:
        return {}
    if mtime != _cache[0]:
        _cache = (mtime, json.loads(PATH.read_text()))
    return _cache[1]


def _save(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(PATH)


def create(name: str, created_by: str) -> tuple[str, dict]:
    key = PREFIX + secrets.token_urlsafe(32)
    kid = "key_" + secrets.token_hex(6)
    meta = {"id": kid, "name": name, "prefix": key[:len(PREFIX) + 6], "digest": _digest(key),
            "created": time.time(), "created_by": created_by}
    with _lock:
        data = dict(_load())
        data[kid] = meta
        _save(data)
    return key, {k: v for k, v in meta.items() if k != "digest"}


def revoke(kid: str) -> bool:
    with _lock:
        data = dict(_load())
        if data.pop(kid, None) is None:
            return False
        _save(data)
    return True


def list_keys() -> list[dict]:
    return sorted(({k: v for k, v in m.items() if k != "digest"} for m in _load().values()), key=lambda m: m["created"])


def verify(key: str | None) -> dict | None:
    if not key or not key.startswith(PREFIX):
        return None
    d = _digest(key)
    for m in _load().values():
        if hmac.compare_digest(m["digest"], d):
            return m
    return None

"""Request security: network allowlist + CSRF guard (middleware), session and
CLI-token authentication, role checks, login throttling.

Order, outermost first:
  1. client IP must be in AIOPS_ALLOWED_NETS (localhost, office LAN, Tailscale)
  2. state-changing /api requests need X-Requested-With (the session cookie is
     also SameSite=Strict, so a cross-site form cannot ride on it)
  3. per-route: a valid session cookie or the CLI approver token, then a role
The agent workflow holds neither, so it cannot approve its own proposals.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
import threading
import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.core.ratelimit import TokenBuckets
from backend.security import auth
from backend.core import config

COOKIE = "aiops_session"
ALLOWED_NETS = tuple(ipaddress.ip_network(n.strip()) for n in os.environ.get(
    "AIOPS_ALLOWED_NETS", f"127.0.0.0/8,::1/128,{config.LAN_NET},100.64.0.0/10").split(","))
RANK = {"viewer": 0, "approver": 1, "admin": 2}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; font-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


def client_allowed(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return any(ip in n for n in ALLOWED_NETS)


_api_buckets = TokenBuckets(config.API_RATE_PER_IP, config.API_BURST_PER_IP)


async def guard(request: Request, call_next):
    ip = request.client.host if request.client else None
    if not client_allowed(ip):
        return JSONResponse({"detail": "forbidden network"}, status_code=403)
    if request.url.path.startswith("/api/"):
        wait = _api_buckets.take(ip or "?")
        if wait:
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429,
                                headers={"Retry-After": str(max(1, round(wait)))})
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/") \
            and request.headers.get("x-requested-with") != "aiops":
        return JSONResponse({"detail": "missing X-Requested-With"}, status_code=400)
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response


def current_user(request: Request) -> dict:
    tok = request.headers.get("x-approver-token")
    if tok and config.APPROVER_TOKEN and hmac.compare_digest(tok, config.APPROVER_TOKEN):
        u = auth.get_user(config.APPROVER_TOKEN_USER) if config.APPROVER_TOKEN_USER else None
        if not u:
            raise HTTPException(403, "the CLI token is not bound to a user (set AIOPS_APPROVER_TOKEN_USER)")
        return {"username": u["username"], "role": u["role"], "via": "token"}
    user = auth.read_session(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(401, "login required")
    return user


def require(role: str):
    def dep(user: Annotated[dict, Depends(current_user)]) -> dict:
        if RANK.get(user["role"], -1) < RANK[role]:
            raise HTTPException(403, f"{role} role required")
        return user
    return dep


Viewer = Annotated[dict, Depends(current_user)]
Approver = Annotated[dict, Depends(require("approver"))]
Admin = Annotated[dict, Depends(require("admin"))]


class LoginThrottle:
    """At most `limit` failed logins per client IP per `window` seconds."""

    def __init__(self, limit: int = 5, window: float = 300):
        self.limit, self.window = limit, window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            recent = [t for t in self._fails.get(ip, []) if now - t < self.window]
            self._fails[ip] = recent
            if len(recent) >= self.limit:
                raise HTTPException(429, "too many failed logins, wait 5 minutes")

    def fail(self, ip: str) -> None:
        with self._lock:
            self._fails.setdefault(ip, []).append(time.time())

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


throttle = LoginThrottle()

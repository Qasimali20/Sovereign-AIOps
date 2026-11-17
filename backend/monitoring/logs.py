"""Bounded, read-only log search against Loki (Node-3).

The agents never write LogQL. They name a node, a registered resource or a node service; this module
builds the query from validated parts, clamps the time window and the number of lines, applies a
timeout, masks secrets a second time (collectors already mask them), and returns every line with
its source labels and timestamp. Log lines are untrusted data: they go into evidence, never into
instructions.
"""
from __future__ import annotations

import os
import re
import time

import httpx

from backend.core.config import NODE_BY_NAME

LOKI_URL = os.environ.get("AIOPS_LOKI_URL", f"http://{NODE_BY_NAME['Node-3'].lan_ip}:3100")
LOKI_USER = os.environ.get("AIOPS_LOKI_USER", "aiops-query")
LOKI_PASSWORD = os.environ.get("AIOPS_LOKI_PASSWORD", "")

MAX_WINDOW_S = 6 * 3600          # one query never spans more than 6 h
DEFAULT_BEFORE_S = 30 * 60       # incident window: 30 min before it opened ...
DEFAULT_AFTER_S = 15 * 60        # ... to 15 min after (or now, if sooner)
MAX_LINES = 200
DEFAULT_LINES = 60
MAX_LINE_CHARS = 400
TIMEOUT_S = 10.0

SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,79}$")
# Journal priority keywords (emerg..warning) and the levels the collector extracts from container lines.
PROBLEM_SEVERITIES = "emerg|alert|crit|err|warning|critical|fatal|error"
# Lines that matter even when the source logs them at info level.
PROBLEM_WORDS = r"(?i)(error|fail|fatal|panic|exception|traceback|oom|out of memory|killed|xid|timeout|refused|denied|unhealthy)"

_SECRET = re.compile(r"(?i)((?:password|passwd|pwd|secret|token|api[_-]?key|authorization)[\"'\s]*[:=]\s*[\"']?(?:bearer\s+)?)"
                     r"([^\s\"',;&]+)")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}")


class LogsUnavailable(Exception):
    """Loki could not answer. Callers record this as missing evidence, never as 'no errors found'."""


def redact(line: str) -> str:
    line = _SECRET.sub(lambda m: m.group(1) + "<redacted>", line)
    return _BEARER.sub(lambda m: m.group(1) + "<redacted>", line)


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_query(node: str, service: str | None = None, problems_only: bool = False,
                contains: str | None = None) -> str:
    if node not in NODE_BY_NAME:
        raise ValueError(f"{node!r} is not a known node")
    sel = [f"node={_quote(node)}"]
    if service is not None:
        if not SERVICE_RE.match(service):
            raise ValueError(f"{service!r} is not a valid service name")
        sel.append(f"service={_quote(service)}")
    q = "{" + ", ".join(sel) + "}"
    if contains:
        if len(contains) > 120:
            raise ValueError("search text is limited to 120 characters")
        q += f" |= {_quote(contains)}"
    if problems_only:
        # either the source marked it as a problem, or the text says so
        q += f' | severity=~"{PROBLEM_SEVERITIES}" or __line__ =~ {_quote(PROBLEM_WORDS)}'
    return q


def window_for(incident_created: float | None, now: float | None = None) -> tuple[float, float]:
    now = now or time.time()
    if incident_created is None:
        return now - 3600, now
    start = incident_created - DEFAULT_BEFORE_S
    end = min(now, incident_created + DEFAULT_AFTER_S)
    if end <= start:
        end = start + 60
    return start, end


def _clamp(start: float, end: float) -> tuple[float, float]:
    if end - start > MAX_WINDOW_S:
        start = end - MAX_WINDOW_S
    return start, end


def _get(path: str, params: dict) -> dict:
    if not LOKI_PASSWORD:
        raise LogsUnavailable("log search is not configured (AIOPS_LOKI_PASSWORD unset)")
    try:
        r = httpx.get(LOKI_URL + path, params=params, auth=(LOKI_USER, LOKI_PASSWORD), timeout=TIMEOUT_S)
    except httpx.HTTPError as e:
        raise LogsUnavailable(f"Loki unreachable: {type(e).__name__}") from e
    if r.status_code != 200:
        raise LogsUnavailable(f"Loki returned HTTP {r.status_code}: {r.text[:200]}")
    body = r.json()
    if body.get("status") != "success":
        raise LogsUnavailable(f"Loki query failed: {str(body)[:200]}")
    return body["data"]


def search(node: str, start: float, end: float, service: str | None = None, problems_only: bool = False,
           contains: str | None = None, limit: int = DEFAULT_LINES) -> dict:
    """Newest-first log lines for one node, bounded in time, size and duration."""
    q = build_query(node, service, problems_only, contains)
    start, end = _clamp(start, end)
    limit = max(1, min(int(limit), MAX_LINES))
    data = _get("/loki/api/v1/query_range", {"query": q, "start": f"{int(start * 1e9)}", "end": f"{int(end * 1e9)}",
                                             "limit": limit, "direction": "backward"})
    rows = []
    for stream in data.get("result", []):
        lab = stream.get("stream", {})
        for ts, line in stream.get("values", []):
            rows.append({"ts": int(ts) / 1e9, "service": lab.get("service", "?"), "severity": lab.get("severity", ""),
                         "component": lab.get("component", ""), "line": redact(line)[:MAX_LINE_CHARS]})
    rows.sort(key=lambda r: r["ts"], reverse=True)
    rows = rows[:limit]
    by_service: dict[str, int] = {}
    for r in rows:
        by_service[r["service"]] = by_service.get(r["service"], 0) + 1
    return {"query": q, "window": {"start": start, "end": end}, "limit": limit, "returned": len(rows),
            "truncated": len(rows) >= limit, "lines_by_service": by_service,
            "entries": [r | {"time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(r["ts"]))} for r in rows]}


def services(node: str, hours: float = 24) -> list[str]:
    if node not in NODE_BY_NAME:
        raise ValueError(f"{node!r} is not a known node")
    now = time.time()
    data = _get("/loki/api/v1/label/service/values",
                {"query": "{node=" + _quote(node) + "}", "start": f"{int((now - hours * 3600) * 1e9)}",
                 "end": f"{int(now * 1e9)}"})
    return sorted(data or [])


def ready() -> bool:
    try:
        return httpx.get(LOKI_URL + "/ready", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False

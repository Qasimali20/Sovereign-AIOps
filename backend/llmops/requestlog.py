"""Per-request records of the model gateway: metadata only, never prompt or response text.

One row per request the gateway answered: when, which client (API key name, never the key), which model was asked for
and which served it, on which node, the endpoint, status and outcome, latency, time to first byte, token counts, and
audio seconds for transcriptions. Rows are buffered in memory and written in batches from a worker thread, so the
request path never waits on disk. Kept for RETENTION_DAYS, capped at MAX_ROWS.

The console reads the same file (separate process, WAL) for the LLMOps Requests page.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

from backend.core import config

PATH = Path(os.environ.get("AIOPS_GATEWAY_REQUESTS_DB", config.DB_PATH.parent / "gateway_requests.db"))
RETENTION_DAYS = 7
MAX_ROWS = 500_000
FLUSH_S = 2.0
FIELDS = ("id", "t", "client", "requested", "model", "node", "endpoint", "status", "outcome", "latency_s", "ttft_s",
          "prompt_tokens", "completion_tokens", "audio_s", "attempts")
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS requests (
  id TEXT, t REAL, client TEXT, requested TEXT, model TEXT, node TEXT, endpoint TEXT, status INTEGER, outcome TEXT,
  latency_s REAL, ttft_s REAL, prompt_tokens INTEGER, completion_tokens INTEGER, audio_s REAL, attempts INTEGER
);
CREATE INDEX IF NOT EXISTS requests_t ON requests(t);
CREATE INDEX IF NOT EXISTS requests_model_t ON requests(model, t);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    c = sqlite3.connect(path or PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


def endpoint(path: str) -> str:
    """The OpenAI endpoint family, for grouping: chat, completions, embeddings, rerank, transcription, speech, other."""
    for key, name in (("chat/completions", "chat"), ("completions", "completions"), ("embeddings", "embeddings"),
                      ("rerank", "rerank"), ("score", "rerank"), ("audio/transcriptions", "transcription"),
                      ("audio/translations", "transcription"), ("audio/speech", "speech")):
        if path.startswith(key):
            return name
    return "other"


class RequestLog:
    def __init__(self, path: Path | None = None):
        self.path = path or PATH
        self._buf: list[tuple] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.dropped = 0
        self.last_prune = 0.0

    def add(self, **row) -> None:
        """Queue one record (called on the event loop; never blocks on disk)."""
        with self._lock:
            if len(self._buf) >= 50_000:      # the writer is stuck: drop rather than grow without bound
                self.dropped += 1
                return
            self._buf.append(tuple(row.get(f) for f in FIELDS))

    def flush(self) -> int:
        with self._lock:
            rows, self._buf = self._buf, []
        if not rows:
            return 0
        with connect(self.path) as c:
            c.executemany(f"INSERT INTO requests VALUES ({','.join('?' * len(FIELDS))})", rows)
            if time.time() - self.last_prune > 600:
                self.last_prune = time.time()
                c.execute("DELETE FROM requests WHERE t < ?", (time.time() - RETENTION_DAYS * 86400,))
                c.execute("DELETE FROM requests WHERE rowid <= (SELECT MAX(rowid) FROM requests) - ?", (MAX_ROWS,))
        return len(rows)

    def start(self) -> None:
        def loop():
            while not self._stop.wait(FLUSH_S):
                try:
                    self.flush()
                except sqlite3.Error:
                    pass                      # disk trouble must never take the gateway down; retried next tick
            self.flush()
        self._thread = threading.Thread(target=loop, name="gateway-requestlog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

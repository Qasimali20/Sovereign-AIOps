"""Token-bucket rate limiting, keyed per client (IP for the web API, API key for the model gateway).

A bucket holds up to `burst` tokens and refills at `rate` per second; each request takes one. Bursts
such as a dashboard loading all its panels pass; a client looping on the API is held to `rate`."""
from __future__ import annotations

import threading
import time


class TokenBuckets:
    def __init__(self, rate: float, burst: float, max_keys: int = 10_000):
        self.rate, self.burst, self.max_keys = rate, burst, max_keys
        self._b: dict[str, tuple[float, float]] = {}            # key -> (tokens, last refill)
        self._lock = threading.Lock()

    def take(self, key: str, now: float | None = None) -> float:
        """0.0 if allowed, else seconds until a token is available."""
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._b.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens >= 1:
                self._b[key] = (tokens - 1, now)
                wait = 0.0
            else:
                self._b[key] = (tokens, now)
                wait = (1 - tokens) / self.rate
            if len(self._b) > self.max_keys:                  # bound memory: drop the stalest buckets
                for k, _ in sorted(self._b.items(), key=lambda kv: kv[1][1])[: len(self._b) // 10]:
                    del self._b[k]
            return wait

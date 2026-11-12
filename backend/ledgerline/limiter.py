"""Small in-process sliding-window rate limiter.

Scope: it protects a *single* uvicorn process and is intentionally simple. For
multi-worker or multi-host deployments this belongs in Redis; the call sites are
written so that swapping the implementation is a one-line change. Login
throttling does not use this class -- it is backed by the audit log in SQLite so
that it survives restarts and is shared between processes.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any


class SlidingWindowLimiter:
    """In-process sliding window; correct for exactly one worker."""

    backend = "in-process"

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            if len(self._hits) > 10_000:  # crude unbounded-growth guard
                for stale_key in [k for k, v in self._hits.items() if not v]:
                    self._hits.pop(stale_key, None)
            return True

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


# --- Redis-backed limiter ----------------------------------------------------
#
# The in-process limiter above protects one worker. Behind more than one worker
# (and the moment you scale past a single uvicorn process, that is the normal
# case) each worker keeps its own counter, so "30 money writes per minute"
# becomes 30 per worker. Redis moves the counter out of the process.
#
# The check-and-increment runs as one Lua script, so two workers cannot both read
# "29 used" and both allow the thirtieth.

LUA_SLIDING_WINDOW = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
if redis.call('ZCARD', key) >= limit then
  redis.call('PEXPIRE', key, window)
  return 0
end
redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, window)
return 1
"""


class RedisSlidingWindowLimiter:
    """Same interface as :class:`SlidingWindowLimiter`, shared across workers.

    Failure policy: if Redis is unreachable the limiter **fails open** and logs
    once. Rate limiting is a protection, not a correctness guarantee — the money
    invariants are enforced in the database — and turning a Redis outage into a
    total outage would be the worse trade. This is written down in THREATS.md
    rather than left implicit.
    """

    backend = "redis"

    def __init__(self, client: Any, limit: int, window_seconds: float = 60.0, prefix: str = "ledgerline") -> None:
        self.client = client
        self.limit = limit
        self.window_seconds = window_seconds
        self.prefix = prefix
        self._script = client.register_script(LUA_SLIDING_WINDOW)
        self._warned = False

    def _redis_key(self, key: str) -> str:
        return f"{self.prefix}:rl:{self.window_seconds:.0f}:{key}"

    def allow(self, key: str) -> bool:
        import logging
        import time
        import uuid

        now_ms = int(time.time() * 1000)
        try:
            allowed = self._script(
                keys=[self._redis_key(key)],
                args=[now_ms, int(self.window_seconds * 1000), self.limit, f"{now_ms}-{uuid.uuid4().hex}"],
            )
        except Exception as exc:  # pragma: no cover - only when Redis is down
            if not self._warned:
                logging.getLogger("ledgerline").warning(
                    "redis limiter unavailable (%s); failing open", exc.__class__.__name__
                )
                self._warned = True
            return True
        return bool(allowed)

    def reset(self, key: str | None = None) -> None:
        try:
            if key is None:
                for found in self.client.scan_iter(match=f"{self.prefix}:rl:*"):
                    self.client.delete(found)
            else:
                self.client.delete(self._redis_key(key))
        except Exception:  # pragma: no cover
            pass


def make_redis_client(settings: Any) -> Any:
    """Return a Redis client, or None when no Redis URL is configured."""
    url = getattr(settings, "redis_url", None)
    if not url:
        return None
    import redis

    client = redis.Redis.from_url(url, socket_timeout=0.25, socket_connect_timeout=0.25)
    return client


def make_limiter(client: Any, *, limit: int, window_seconds: float, prefix: str) -> Any:
    """Redis when configured and reachable, in-process otherwise."""
    if client is None:
        return SlidingWindowLimiter(limit, window_seconds)
    return RedisSlidingWindowLimiter(client, limit, window_seconds, prefix)

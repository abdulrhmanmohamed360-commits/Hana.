"""Fixed-window rate limiter with Redis (distributed) and in-memory (tests/dev) backends."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol

from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    remaining: int
    retry_after: int


class RateLimiter(Protocol):
    def hit(self, key: str) -> RateDecision: ...


class MemoryRateLimiter:
    def __init__(self, limit: int, window_seconds: int, clock=time.time):
        self.limit, self.window, self._clock = limit, window_seconds, clock
        self._buckets: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> RateDecision:
        now = int(self._clock())
        window_id = now // self.window
        with self._lock:
            stored_window, count = self._buckets.get(key, (window_id, 0))
            if stored_window != window_id:
                count = 0
            count += 1
            self._buckets[key] = (window_id, count)
        retry_after = (window_id + 1) * self.window - now
        return RateDecision(count <= self.limit, max(self.limit - count, 0), retry_after)


class RedisRateLimiter:
    """Uses INCR + EXPIRE on a per-window key. Fails open (logs) if Redis is unreachable."""

    def __init__(self, client, limit: int, window_seconds: int, clock=time.time):
        self.client, self.limit, self.window, self._clock = client, limit, window_seconds, clock

    def hit(self, key: str) -> RateDecision:
        now = int(self._clock())
        window_id = now // self.window
        redis_key = f"ratelimit:{key}:{window_id}"
        try:
            pipe = self.client.pipeline()
            pipe.incr(redis_key)
            pipe.expire(redis_key, self.window + 1)
            count, _ = pipe.execute()
        except Exception:  # noqa: BLE001 - availability over strictness
            log.warning("rate_limiter_redis_unavailable_failing_open")
            return RateDecision(True, self.limit, 0)
        retry_after = (window_id + 1) * self.window - now
        return RateDecision(int(count) <= self.limit, max(self.limit - int(count), 0), retry_after)

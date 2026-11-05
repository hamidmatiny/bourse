"""Token-bucket rate limits for register/refresh (ADR-0043)."""

import time
from collections.abc import Callable


class TokenBucket:
    """`rate_per_min` tokens refill each minute, burst equal to the rate."""

    def __init__(self, rate_per_min: int, clock: Callable[[], float] = time.monotonic) -> None:
        if rate_per_min < 1:
            raise ValueError("rate_per_min must be >= 1")
        self.rate_per_sec = rate_per_min / 60.0
        self.capacity = float(rate_per_min)
        self.clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = self.clock()
        tokens, last = self._buckets.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate_per_sec)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True

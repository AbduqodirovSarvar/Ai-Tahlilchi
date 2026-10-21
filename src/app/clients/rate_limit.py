"""Token-bucket rate limiter: spaces requests to one provider below its per-minute limit."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Final

type MonotonicClock = Callable[[], float]
type Sleep = Callable[[float], Awaitable[None]]

SECONDS_PER_MINUTE: Final = 60.0


class TokenBucket:
    """Allows ``capacity`` requests at once, refilled at ``requests_per_minute``.

    ``acquire`` waits (via the injected ``sleep``) until a token is available, so callers
    never hit the provider's per-minute limit (which would cost a request and a 429).
    Concurrent callers are served in arrival order.
    """

    def __init__(
        self,
        requests_per_minute: float,
        *,
        capacity: int = 1,
        clock: MonotonicClock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Start full, so the first ``capacity`` requests go out immediately."""
        if requests_per_minute <= 0:
            msg = f"requests_per_minute must be positive (got {requests_per_minute})"
            raise ValueError(msg)
        if capacity < 1:
            msg = f"capacity must be >= 1 (got {capacity})"
            raise ValueError(msg)
        self._rate_per_second = requests_per_minute / SECONDS_PER_MINUTE
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """Take one token, waiting if necessary; return the time waited in seconds."""
        async with self._lock:
            self._refill()
            waited = 0.0
            if self._tokens < 1:
                waited = (1 - self._tokens) / self._rate_per_second
                await self._sleep(waited)
                self._refill()
                # A fake clock in tests may not advance during sleep: never go below zero.
                self._tokens = max(self._tokens, 1.0)
            self._tokens -= 1
            return waited

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(now - self._updated, 0.0)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_second)
        self._updated = now

"""Client-side rate limiting that cooperates with Freshdesk's server-side limits.

Freshdesk enforces a per-account, per-minute budget (trial 50/min up to Enterprise
700/min, with stricter sub-limits on some endpoints) and reports it on every response:

    X-RateLimit-Total       the account's budget for the window
    X-RateLimit-Remaining   calls left in the current window
    429 + Retry-After       budget exhausted; seconds to wait

The connector uses three layers:

1. **Token bucket** (here): smooths bursts from an agent that fires many tool calls
   at once, so we rarely hit 429 in the first place.
2. **Header sync** (here): the bucket's rate shrinks to the account's real limit, and
   its tokens never exceed what the server says remains. This matters because other
   integrations (the merchant's helpdesk apps, other agents) share the same budget.
3. **Retry policy** (client.py): honours Retry-After for 429 and backs off with jitter
   on 5xx, but gives up quickly when the wait would be longer than an agent should block.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class TokenBucket:
    def __init__(
        self,
        rate_per_minute: float,
        *,
        burst: int | None = None,
        reserve: int = 2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self._configured_rate = rate_per_minute
        self.rate_per_minute = rate_per_minute
        self.capacity = float(burst if burst is not None else max(1, min(10, int(rate_per_minute))))
        self.tokens = self.capacity
        self.reserve = reserve
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    @property
    def _per_second(self) -> float:
        return self.rate_per_minute / 60.0

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self.tokens = min(self.capacity, self.tokens + elapsed * self._per_second)
        self._updated = now

    async def acquire(self) -> float:
        """Wait for a token. Returns the seconds spent waiting."""
        waited = 0.0
        async with self._lock:
            while True:
                now = self._clock()
                if now < self._blocked_until:
                    await self._sleep(self._blocked_until - now)
                    waited += self._blocked_until - now
                    continue
                self._refill()
                if self.tokens >= 1:
                    self.tokens -= 1
                    return waited
                delay = (1 - self.tokens) / self._per_second
                await self._sleep(delay)
                waited += delay
                # We have now waited exactly long enough for one token. Grant it explicitly:
                # recomputing from the clock can fall short by float rounding (and, with a
                # sub-resolution delay, never converge).
                self._refill()
                self.tokens = max(self.tokens, 1.0)

    def observe(self, total: int | None, remaining: int | None) -> None:
        """Sync the bucket with the server's view of the budget."""
        if total and total > 0:
            self.rate_per_minute = min(self._configured_rate, float(total))
        if remaining is not None:
            self._refill()
            self.tokens = min(self.tokens, float(max(remaining - self.reserve, 0)))

    def block_for(self, seconds: float) -> None:
        """After a 429, stop *all* callers until Retry-After elapses, not just the one that failed."""
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)
        self.tokens = 0.0

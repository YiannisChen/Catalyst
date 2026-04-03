from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from catalyst_data.config import RatePolicy


class DailyBudgetExhausted(Exception):
    """Raised when the daily API call budget for a provider is exhausted."""


class TokenBucketLimiter:
    """Per-provider rate limiter using semaphore + interval + daily budget.

    Each provider gets its own instance, configured via RatePolicy.
    Callers await `acquire()` before making an API request.
    """

    def __init__(self, policy: RatePolicy) -> None:
        self._policy = policy
        self._semaphore = asyncio.Semaphore(policy.max_concurrent)
        self._lock = asyncio.Lock()
        self._last_request: float = 0.0
        self._daily_count: int = 0
        self._budget_day = datetime.now(timezone.utc).date()

    @asynccontextmanager
    async def acquire(self):
        """Acquire permission to make one API request and release on exit.

        Order of checks:
        1. Semaphore wait (concurrency cap).
        2. Semaphore wait (concurrency cap).
        3. Daily budget guard and reset under lock.
        4. Interval enforcement (min time between requests).
        """
        await self._semaphore.acquire()
        try:
            async with self._lock:
                today = datetime.now(timezone.utc).date()
                if today != self._budget_day:
                    self._budget_day = today
                    self._daily_count = 0

                if (
                    self._policy.daily_budget is not None
                    and self._daily_count >= self._policy.daily_budget
                ):
                    raise DailyBudgetExhausted(
                        f"Daily budget of {self._policy.daily_budget} requests exhausted"
                    )

                now = time.monotonic()
                elapsed = now - self._last_request
                wait = self._policy.min_interval_sec - elapsed
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_request = time.monotonic()
                self._daily_count += 1
            yield
        finally:
            self._semaphore.release()

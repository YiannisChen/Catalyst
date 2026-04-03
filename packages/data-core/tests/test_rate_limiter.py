import asyncio
import time
from datetime import timedelta

import pytest
from catalyst_data.config import RatePolicy
from catalyst_data.rate_limiter import TokenBucketLimiter, DailyBudgetExhausted


@pytest.mark.asyncio
async def test_rate_limiter_enforces_interval():
    policy = RatePolicy(min_interval_sec=0.1, max_concurrent=1, daily_budget=None)
    limiter = TokenBucketLimiter(policy)
    start = time.monotonic()
    async with limiter.acquire():
        pass
    async with limiter.acquire():
        pass
    elapsed = time.monotonic() - start
    assert elapsed >= 0.1


@pytest.mark.asyncio
async def test_rate_limiter_daily_budget():
    policy = RatePolicy(min_interval_sec=0.0, max_concurrent=5, daily_budget=2)
    limiter = TokenBucketLimiter(policy)
    async with limiter.acquire():
        pass
    async with limiter.acquire():
        pass
    with pytest.raises(DailyBudgetExhausted):
        async with limiter.acquire():
            pass


@pytest.mark.asyncio
async def test_rate_limiter_concurrency():
    policy = RatePolicy(min_interval_sec=0.0, max_concurrent=1, daily_budget=None)
    limiter = TokenBucketLimiter(policy)
    in_flight = 0
    max_in_flight = 0

    async def task(n):
        nonlocal in_flight, max_in_flight
        async with limiter.acquire():
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1

    await asyncio.gather(task(1), task(2), task(3))
    assert max_in_flight == 1


@pytest.mark.asyncio
async def test_rate_limiter_daily_budget_resets_on_new_day():
    policy = RatePolicy(min_interval_sec=0.0, max_concurrent=1, daily_budget=1)
    limiter = TokenBucketLimiter(policy)
    async with limiter.acquire():
        pass
    limiter._budget_day = limiter._budget_day - timedelta(days=1)
    async with limiter.acquire():
        pass

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RatePolicy:
    min_interval_sec: float  # minimum time between requests
    max_concurrent: int  # asyncio.Semaphore limit
    daily_budget: int | None  # None = unlimited


RATE_POLICIES = {
    "dev": {
        "polygon": RatePolicy(12.0, 1, None),  # 5/min
        "finnhub": RatePolicy(1.0, 3, None),  # 60/min
        "fred": RatePolicy(0.5, 3, None),  # 120/min
        "gdelt": RatePolicy(5.5, 1, None),  # sequential only
        "fmp": RatePolicy(1.0, 2, 250),  # 250/day
        "sec": RatePolicy(0.2, 3, None),  # 5/sec conservative
        "yfinance": RatePolicy(2.0, 1, None),  # treat gently
    },
    "test": {},  # all mocked, no network
}

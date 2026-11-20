"""Per-provider request budgets shared by every thread of the process.

A token bucket per data provider, sized to the free tier's per-minute limit: up to ``capacity`` requests may go at
once, then one more each ``60 / per_minute`` seconds. Requests that would exceed the budget wait; a 429 from the
provider is answered by ``backoff`` (the caller retries). Politeness towards web hosts is separate (fetch.Fetcher: one
request per second per host).

``SharedBucket`` is the same bucket kept in a file under CATALYST_RATELIMIT_DIR (default /opt/catalyst/ratelimit) and
taken under an exclusive lock, so every collector process on the server draws from one budget per provider. The
daily job (its own checkout) keeps in-process buckets; the collectors' timers stay out of its window (11:00-13:30 UTC)
and their shared budgets leave headroom below the free tier.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable


class TokenBucket:
    def __init__(self, per_minute: float, capacity: int, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.interval = 60.0 / per_minute
        self.capacity = capacity
        self._tokens = float(capacity)
        self._clock, self._sleep = clock, sleep
        self._stamp = clock()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Take one request slot, waiting if needed; returns the seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + (now - self._stamp) / self.interval)
                self._stamp = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return waited
                delay = (1 - self._tokens) * self.interval
            self._sleep(delay)
            waited += delay

    def drain(self) -> None:
        """After a 429: empty the bucket (the waiting is resilient.request's)."""
        with self._lock:
            self._tokens = 0.0
            self._stamp = self._clock()

    def backoff(self, attempt: int) -> None:
        """After a 429: empty the bucket and wait (attempt 0, 1, 2 → 1, 2, 4 refill intervals, at least 2 s)."""
        self.drain()
        self._sleep(max(2.0, self.interval * (2 ** attempt)))


class SharedBucket:
    """A TokenBucket whose state is a small JSON file, updated under flock: one budget across processes. Falls back
    to an in-process bucket when the directory cannot be written (tests, a developer machine)."""

    def __init__(self, name: str, per_minute: float, capacity: int, *, directory: str | Path | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.name, self.interval, self.capacity = name, 60.0 / per_minute, capacity
        self._clock, self._sleep = clock, sleep
        root = Path(directory or os.environ.get("CATALYST_RATELIMIT_DIR", "/opt/catalyst/ratelimit"))
        self.path = root / f"{name}.bucket"
        self._per_minute = per_minute
        self._local = None
        self._initialized = False
        self._init_lock = threading.Lock()

    def _ensure(self) -> None:
        # Provider imports happen before manual-job preflight. Allocate state only
        # when an authorized request first uses the bucket.
        with self._init_lock:
            if self._initialized:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.touch(exist_ok=True)
            except OSError:
                self._local = TokenBucket(self._per_minute, self.capacity, clock=self._clock, sleep=self._sleep)
            self._initialized = True

    def _update(self, take: bool, empty: bool = False) -> float:
        """Refill, then take one token (returns 0) or report the wait needed; ``empty`` zeroes the bucket."""
        with open(self.path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                raw = f.read()
                state = json.loads(raw) if raw.strip() else {"tokens": float(self.capacity), "stamp": self._clock()}
                now = self._clock()
                tokens = min(self.capacity, state["tokens"] + (now - state["stamp"]) / self.interval)
                wait = 0.0
                if empty:
                    tokens = 0.0
                elif tokens >= 1:
                    tokens -= 1 if take else 0
                else:
                    wait = (1 - tokens) * self.interval
                f.seek(0)
                f.truncate()
                f.write(json.dumps({"tokens": tokens, "stamp": now}))
                return wait
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def acquire(self) -> float:
        self._ensure()
        if self._local is not None:
            return self._local.acquire()
        waited = 0.0
        while True:
            delay = self._update(take=True)
            if delay == 0:
                return waited
            self._sleep(delay)
            waited += delay

    def drain(self) -> None:
        self._ensure()
        if self._local is not None:
            return self._local.drain()
        self._update(take=False, empty=True)

    def backoff(self, attempt: int) -> None:
        self._ensure()
        if self._local is not None:
            return self._local.backoff(attempt)
        self._update(take=False, empty=True)
        self._sleep(max(2.0, self.interval * (2 ** attempt)))


# Free tiers: Finnhub 60 calls per minute; Polygon 5; Alpaca 200. Shared across the collector processes, with headroom.
FINNHUB = SharedBucket("finnhub", per_minute=50, capacity=10)
POLYGON = SharedBucket("polygon", per_minute=4, capacity=2)
ALPACA = SharedBucket("alpaca", per_minute=150, capacity=20)

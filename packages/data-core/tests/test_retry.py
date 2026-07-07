import unittest

import pytest

from catalyst_data.connectors.base import FetchResult
from catalyst_data.retry import (
    ErrorClass,
    compute_backoff,
    should_retry,
    with_retry,
    MAX_RETRIES,
)
from catalyst_data.error_taxonomy import classify_fetch_error


class TestClassifyError(unittest.TestCase):
    def test_401_immediate_fallback(self):
        self.assertEqual(classify_fetch_error(401), ErrorClass.AUTH)

    def test_403_immediate_fallback(self):
        self.assertEqual(classify_fetch_error(403), ErrorClass.AUTH)

    def test_429_retryable(self):
        self.assertEqual(classify_fetch_error(429), ErrorClass.RATE_LIMIT)

    def test_500_retryable(self):
        self.assertEqual(classify_fetch_error(500), ErrorClass.PROVIDER_5XX)

    def test_502_retryable(self):
        self.assertEqual(classify_fetch_error(502), ErrorClass.PROVIDER_5XX)

    def test_503_retryable(self):
        self.assertEqual(classify_fetch_error(503), ErrorClass.PROVIDER_5XX)

    def test_504_retryable(self):
        self.assertEqual(classify_fetch_error(504), ErrorClass.PROVIDER_5XX)

    def test_timeout_retryable(self):
        self.assertEqual(
            classify_fetch_error(None, is_timeout=True), ErrorClass.TIMEOUT
        )

    def test_200_no_fallback(self):
        self.assertEqual(classify_fetch_error(200), ErrorClass.EMPTY_VALID)

    def test_404_no_fallback(self):
        self.assertEqual(classify_fetch_error(404), ErrorClass.AUTH)

    def test_none_status_no_timeout_no_fallback(self):
        self.assertEqual(classify_fetch_error(None), ErrorClass.UNKNOWN)


class TestComputeBackoff(unittest.TestCase):
    def test_attempt_1_is_2s(self):
        self.assertAlmostEqual(compute_backoff(1), 2.0)

    def test_attempt_2_is_4s(self):
        self.assertAlmostEqual(compute_backoff(2), 4.0)

    def test_attempt_3_is_8s(self):
        self.assertAlmostEqual(compute_backoff(3), 8.0)

    def test_zero_attempt_raises(self):
        with self.assertRaises(ValueError):
            compute_backoff(0)

    def test_retry_after_overrides_exponential(self):
        self.assertAlmostEqual(compute_backoff(1, retry_after=10.0), 10.0)


class TestShouldRetry(unittest.TestCase):
    def test_below_max_true(self):
        for i in range(1, MAX_RETRIES):
            self.assertTrue(should_retry(i))

    def test_at_max_false(self):
        self.assertFalse(should_retry(MAX_RETRIES))

    def test_above_max_false(self):
        self.assertFalse(should_retry(MAX_RETRIES + 5))


# ---------------------------------------------------------------------------
# with_retry integration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_with_retry_429_then_success(monkeypatch):
    """First call returns 429, second returns 200 — retry succeeds."""
    async def noop_sleep(_): pass
    monkeypatch.setattr("catalyst_data.retry.asyncio.sleep", noop_sleep)

    call_count = 0

    async def flaky_fetch(ticker, endpoint, date):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return FetchResult(status=429, error="Rate limited", source_label="test")
        return FetchResult(status=200, data={"ok": True}, source_label="test")

    wrapped = with_retry(flaky_fetch)
    result = await wrapped("AAPL", "news", "2026-01-15")
    assert result.status == 200
    assert call_count == 2


@pytest.mark.asyncio
async def test_with_retry_5xx_then_success(monkeypatch):
    """First call returns 503, second returns 200."""
    async def noop_sleep(_): pass
    monkeypatch.setattr("catalyst_data.retry.asyncio.sleep", noop_sleep)

    call_count = 0

    async def flaky_fetch(ticker, endpoint, date):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return FetchResult(status=503, error="Service unavailable", source_label="test")
        return FetchResult(status=200, data={"ok": True}, source_label="test")

    wrapped = with_retry(flaky_fetch)
    result = await wrapped("AAPL", "news", "2026-01-15")
    assert result.status == 200
    assert call_count == 2


@pytest.mark.asyncio
async def test_with_retry_exhausted_returns_last_error(monkeypatch):
    """All 3 attempts fail — returns last result with exhaustion message."""
    async def noop_sleep(_): pass
    monkeypatch.setattr("catalyst_data.retry.asyncio.sleep", noop_sleep)

    call_count = 0

    async def always_fail(ticker, endpoint, date):
        nonlocal call_count
        call_count += 1
        return FetchResult(status=500, error="Server error", source_label="test")

    wrapped = with_retry(always_fail)
    result = await wrapped("AAPL", "news", "2026-01-15")
    assert result.status == 500
    assert "exhausted" in result.error.lower()
    assert call_count == MAX_RETRIES


@pytest.mark.asyncio
async def test_with_retry_non_retryable_returns_immediately(monkeypatch):
    """404 is not retryable — should return after first call."""
    async def noop_sleep(_): pass
    monkeypatch.setattr("catalyst_data.retry.asyncio.sleep", noop_sleep)

    call_count = 0

    async def not_found(ticker, endpoint, date):
        nonlocal call_count
        call_count += 1
        return FetchResult(status=404, error="Not found", source_label="test")

    wrapped = with_retry(not_found)
    result = await wrapped("AAPL", "news", "2026-01-15")
    assert result.status == 404
    assert call_count == 1


@pytest.mark.asyncio
async def test_with_retry_429_structured_retry_after_used(monkeypatch):
    """429 with retry_after_seconds field should sleep that value, not exponential."""
    async def noop_sleep(_): pass
    sleeps: list[float] = []

    async def spy_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("catalyst_data.retry.asyncio.sleep", spy_sleep)

    call_count = 0

    async def rate_limited_then_ok(ticker, endpoint, date):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return FetchResult(
                status=429, error="Rate limited",
                source_label="test", retry_after_seconds=30.0,
            )
        return FetchResult(status=200, data={"ok": True}, source_label="test")

    wrapped = with_retry(rate_limited_then_ok)
    result = await wrapped("AAPL", "news", "2026-01-15")
    assert result.status == 200
    assert call_count == 2
    # Should have used the structured 30s, not the exponential 2s
    assert sleeps == [30.0]


@pytest.mark.asyncio
async def test_with_retry_429_no_header_uses_exponential(monkeypatch):
    """429 without retry_after_seconds should use exponential backoff."""
    sleeps: list[float] = []

    async def spy_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("catalyst_data.retry.asyncio.sleep", spy_sleep)

    call_count = 0

    async def rate_limited_then_ok(ticker, endpoint, date):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return FetchResult(
                status=429, error="Rate limited",
                source_label="test",
                # retry_after_seconds is None (default)
            )
        return FetchResult(status=200, data={"ok": True}, source_label="test")

    wrapped = with_retry(rate_limited_then_ok)
    result = await wrapped("AAPL", "news", "2026-01-15")
    assert result.status == 200
    # Exponential backoff attempt=1: 2.0
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_with_retry_5xx_uses_exponential(monkeypatch):
    """5xx should always use exponential backoff (no retry-after)."""
    sleeps: list[float] = []

    async def spy_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("catalyst_data.retry.asyncio.sleep", spy_sleep)

    call_count = 0

    async def server_error_then_ok(ticker, endpoint, date):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return FetchResult(status=503, error="Service unavailable", source_label="test")
        return FetchResult(status=200, data={"ok": True}, source_label="test")

    wrapped = with_retry(server_error_then_ok)
    result = await wrapped("AAPL", "news", "2026-01-15")
    assert result.status == 200
    assert sleeps == [2.0]


if __name__ == "__main__":
    unittest.main()

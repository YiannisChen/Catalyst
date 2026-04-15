import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from catalyst_data.connectors.fmp import create_fmp_fetcher, FMP_BASE_URL, ENDPOINT_PATH_MAP


class TestFmpFetcher(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_200_with_data(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"revenue": 35082000000}]

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        fetch = create_fmp_fetcher(
            api_key="test-key",
            client=mock_client,
        )
        result = await fetch("NVDA", "income_statement", "2025-01-01")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.data, [{"revenue": 35082000000}])
        mock_client.get.assert_called_once()
        call_args, call_kw = mock_client.get.call_args
        self.assertIn("/stable/income-statement", call_args[0])
        self.assertEqual(
            call_kw["params"],
            {"symbol": "NVDA", "apikey": "test-key", "period": "annual"},
        )

    async def test_401_returns_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        fetch = create_fmp_fetcher(
            api_key="bad-key",
            client=mock_client,
        )
        result = await fetch("NVDA", "income_statement", "2025-01-01")
        self.assertEqual(result.status, 401)
        self.assertIsNotNone(result.error)

    async def test_429_preserves_response_text(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.text = "rate limited"
        mock_resp.headers = {}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        # Patch sleep to avoid real delays during retry exhaustion
        import catalyst_data.retry as retry_mod
        orig_sleep = retry_mod.asyncio.sleep
        retry_mod.asyncio.sleep = AsyncMock()
        try:
            fetch = create_fmp_fetcher(
                api_key="test-key",
                client=mock_client,
            )
            result = await fetch("NVDA", "income_statement", "2025-01-01")
            self.assertEqual(result.status, 429)
            self.assertEqual(result.source_label, "fmp:income_statement")
            self.assertIn("429", result.error)
            self.assertIn("rate limited", result.error)
        finally:
            retry_mod.asyncio.sleep = orig_sleep

    async def test_timeout_returns_error_with_zero_status(self):
        import httpx
        import catalyst_data.retry as retry_mod

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        orig_sleep = retry_mod.asyncio.sleep
        retry_mod.asyncio.sleep = AsyncMock()
        try:
            fetch = create_fmp_fetcher(
                api_key="test-key",
                client=mock_client,
            )
            result = await fetch("NVDA", "income_statement", "2025-01-01")
            self.assertEqual(result.status, 0)
            self.assertIn("timeout", result.error.lower())
        finally:
            retry_mod.asyncio.sleep = orig_sleep

    async def test_unknown_endpoint_returns_error_without_request(self):
        mock_client = AsyncMock()

        fetch = create_fmp_fetcher(
            api_key="test-key",
            client=mock_client,
        )
        result = await fetch("NVDA", "unsupported_endpoint", "2025-01-01")

        self.assertEqual(result.status, 0)
        self.assertEqual(result.source_label, "fmp:unsupported_endpoint")
        self.assertIn("unknown", result.error.lower())
        mock_client.get.assert_not_called()

    async def test_endpoint_path_mapping(self):
        self.assertEqual(ENDPOINT_PATH_MAP["income_statement"], "income-statement")
        self.assertEqual(ENDPOINT_PATH_MAP["balance_sheet"], "balance-sheet-statement")
        self.assertEqual(ENDPOINT_PATH_MAP["cash_flow"], "cash-flow-statement")

    async def test_limiter_acquire_called_before_request(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"revenue": 100}]

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        class DummyLimiter:
            def __init__(self):
                self.entered = 0

            def acquire(self):
                return self

            async def __aenter__(self):
                self.entered += 1

            async def __aexit__(self, exc_type, exc, tb):
                return False

        mock_limiter = DummyLimiter()

        fetch = create_fmp_fetcher(
            api_key="test-key",
            limiter=mock_limiter,
            client=mock_client,
        )
        result = await fetch("NVDA", "income_statement", "2025-01-01")
        self.assertEqual(result.status, 200)
        self.assertEqual(mock_limiter.entered, 1)

    async def test_no_limiter_still_works(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"revenue": 100}]

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        fetch = create_fmp_fetcher(api_key="test-key", client=mock_client)
        result = await fetch("NVDA", "income_statement", "2025-01-01")
        self.assertEqual(result.status, 200)


import pytest


@pytest.mark.asyncio
async def test_fmp_429_retry_after_header_extracted(monkeypatch):
    """FMP connector must read Retry-After header and propagate to retry logic."""
    sleeps: list[float] = []

    async def spy_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("catalyst_data.retry.asyncio.sleep", spy_sleep)

    call_count = 0

    async def fake_get(url, params=None):
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        if call_count == 1:
            resp.status_code = 429
            resp.text = "rate limited"
            resp.headers = {"retry-after": "20"}
        else:
            resp.status_code = 200
            resp.json.return_value = [{"revenue": 100}]
            resp.headers = {}
        return resp

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=fake_get)

    fetch = create_fmp_fetcher(api_key="test-key", client=mock_client)
    result = await fetch("NVDA", "income_statement", "2025-01-01")

    assert result.status == 200
    assert call_count == 2
    # Must have slept 20s (from header), not 2s (exponential)
    assert sleeps == [20.0]


if __name__ == "__main__":
    unittest.main()

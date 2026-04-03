import unittest
from unittest.mock import patch, MagicMock

from catalyst_data.connectors.yfinance_fallback import create_yfinance_fetcher


class TestYfinanceFetcher(unittest.IsolatedAsyncioTestCase):
    @patch("catalyst_data.connectors.yfinance_fallback._fetch_yf_sync")
    async def test_success_returns_200(self, mock_sync):
        mock_sync.return_value = {"revenue": [26044000000]}
        fetch = create_yfinance_fetcher()
        result = await fetch("NVDA", "income_statement", "2025-01-01")
        self.assertEqual(result.status, 200)
        self.assertIsNotNone(result.data)

    @patch("catalyst_data.connectors.yfinance_fallback._fetch_yf_sync")
    async def test_no_data_returns_404(self, mock_sync):
        mock_sync.return_value = None
        fetch = create_yfinance_fetcher()
        result = await fetch("INVALID", "income_statement", "2025-01-01")
        self.assertEqual(result.status, 404)

    @patch("catalyst_data.connectors.yfinance_fallback._fetch_yf_sync")
    async def test_exception_returns_error(self, mock_sync):
        mock_sync.side_effect = Exception("rate limit reached")
        fetch = create_yfinance_fetcher()
        result = await fetch("NVDA", "income_statement", "2025-01-01")
        self.assertEqual(result.status, 0)
        self.assertIn("rate limit", result.error)

    async def test_unsupported_endpoint_returns_error(self):
        fetch = create_yfinance_fetcher()
        result = await fetch("NVDA", "unknown_endpoint", "2025-01-01")
        self.assertEqual(result.status, 0)
        self.assertIn("Unsupported", result.error)

    @patch("catalyst_data.connectors.yfinance_fallback._fetch_yf_sync")
    async def test_limiter_acquire_called_before_request(self, mock_sync):
        mock_sync.return_value = {"revenue": [100]}
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

        fetch = create_yfinance_fetcher(limiter=mock_limiter)
        result = await fetch("NVDA", "income_statement", "2025-01-01")
        self.assertEqual(result.status, 200)
        self.assertEqual(mock_limiter.entered, 1)

    @patch("catalyst_data.connectors.yfinance_fallback._fetch_yf_sync")
    async def test_no_limiter_still_works(self, mock_sync):
        mock_sync.return_value = {"revenue": [100]}
        fetch = create_yfinance_fetcher()
        result = await fetch("NVDA", "income_statement", "2025-01-01")
        self.assertEqual(result.status, 200)


if __name__ == "__main__":
    unittest.main()

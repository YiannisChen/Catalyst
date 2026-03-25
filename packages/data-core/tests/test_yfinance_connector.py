import unittest
from unittest.mock import patch, MagicMock

from data_core.connectors.yfinance_fallback import create_yfinance_fetcher


class TestYfinanceFetcher(unittest.IsolatedAsyncioTestCase):
    @patch("data_core.connectors.yfinance_fallback._fetch_yf_sync")
    async def test_success_returns_200(self, mock_sync):
        mock_sync.return_value = {"revenue": [26044000000]}
        fetch = create_yfinance_fetcher()
        result = await fetch("NVDA", "income_statement", "2025-01-01")
        self.assertEqual(result.status, 200)
        self.assertIsNotNone(result.data)

    @patch("data_core.connectors.yfinance_fallback._fetch_yf_sync")
    async def test_no_data_returns_404(self, mock_sync):
        mock_sync.return_value = None
        fetch = create_yfinance_fetcher()
        result = await fetch("INVALID", "income_statement", "2025-01-01")
        self.assertEqual(result.status, 404)

    @patch("data_core.connectors.yfinance_fallback._fetch_yf_sync")
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


if __name__ == "__main__":
    unittest.main()

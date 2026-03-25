import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from data_core.connectors.fmp import create_fmp_fetcher, FMP_BASE_URL, ENDPOINT_PATH_MAP


class TestFmpFetcher(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_200_with_data(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"revenue": 35082000000}]

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        fetch = create_fmp_fetcher(
            api_key="test-key",
            semaphore=asyncio.Semaphore(5),
            client=mock_client,
        )
        result = await fetch("NVDA", "income_statement", "2025-01-01")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.data, [{"revenue": 35082000000}])

    async def test_401_returns_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        fetch = create_fmp_fetcher(
            api_key="bad-key",
            semaphore=asyncio.Semaphore(5),
            client=mock_client,
        )
        result = await fetch("NVDA", "income_statement", "2025-01-01")
        self.assertEqual(result.status, 401)
        self.assertIsNotNone(result.error)

    async def test_timeout_returns_error_with_zero_status(self):
        import httpx

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        fetch = create_fmp_fetcher(
            api_key="test-key",
            semaphore=asyncio.Semaphore(5),
            client=mock_client,
        )
        result = await fetch("NVDA", "income_statement", "2025-01-01")
        self.assertEqual(result.status, 0)
        self.assertIn("timeout", result.error.lower())

    async def test_endpoint_path_mapping(self):
        self.assertEqual(ENDPOINT_PATH_MAP["income_statement"], "income-statement")
        self.assertEqual(ENDPOINT_PATH_MAP["balance_sheet"], "balance-sheet-statement")
        self.assertEqual(ENDPOINT_PATH_MAP["cash_flow"], "cash-flow-statement")

    async def test_semaphore_limits_concurrency(self):
        sem = asyncio.Semaphore(1)
        concurrent_count = 0
        max_concurrent = 0

        async def counting_get(*args, **kwargs):
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.05)
            concurrent_count -= 1
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {}
            return mock_resp

        mock_client = AsyncMock()
        mock_client.get = counting_get

        fetch = create_fmp_fetcher(api_key="k", semaphore=sem, client=mock_client)
        await asyncio.gather(
            fetch("A", "income_statement", "2025-01-01"),
            fetch("B", "income_statement", "2025-01-01"),
            fetch("C", "income_statement", "2025-01-01"),
        )
        self.assertEqual(max_concurrent, 1)


if __name__ == "__main__":
    unittest.main()

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from catalyst_data.connectors.polygon import create_polygon_fetcher

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_polygon_ohlcv_parses_response():
    with open(FIXTURES / "polygon_ohlcv.json") as f:
        mock_data = json.load(f)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_data

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_polygon_fetcher(api_key="test_key", client=mock_client)
    result = await fetcher("AAPL", "ohlcv", "2026-01-15")

    assert result.status == 200
    assert result.data is not None
    assert "results" in result.data


@pytest.mark.asyncio
async def test_polygon_news_parses_response():
    with open(FIXTURES / "polygon_news.json") as f:
        mock_data = json.load(f)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_data

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_polygon_fetcher(api_key="test_key", client=mock_client)
    result = await fetcher("AAPL", "news", "2026-01-15")

    assert result.status == 200
    assert result.data is not None
    assert len(result.data["results"]) == 2


@pytest.mark.asyncio
async def test_polygon_error_returns_status():
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.text = "Rate limit"

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_polygon_fetcher(api_key="test_key", client=mock_client)
    result = await fetcher("AAPL", "ohlcv", "2026-01-15")

    assert result.status == 429
    assert result.error is not None


@pytest.mark.asyncio
async def test_polygon_timeout_returns_error():
    import httpx

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

    fetcher = create_polygon_fetcher(api_key="test_key", client=mock_client)
    result = await fetcher("AAPL", "ohlcv", "2026-01-15")

    assert result.status == 0
    assert "timeout" in result.error.lower()

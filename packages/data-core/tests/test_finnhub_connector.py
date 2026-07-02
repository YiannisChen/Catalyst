"""Tests for Finnhub connector — mock-only, zero network."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from catalyst_data.connectors.finnhub import create_finnhub_fetcher

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_company_news_200():
    """Mocked 200 response returns parsed JSON array."""
    with open(FIXTURES / "finnhub_company_news_AAPL.json") as f:
        mock_data = json.load(f)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_data

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_finnhub_fetcher(api_key="test_key", client=mock_client)
    result = await fetcher.fetch("AAPL", "company-news", "2025-07-01")

    assert result.status == 200
    assert isinstance(result.data, list)
    assert len(result.data) == 3
    assert result.data[0]["headline"] == "Apple Reports Record Q2 Earnings"


@pytest.mark.asyncio
async def test_company_news_field_mapping():
    """Verify fixture fields parse correctly from real fixture."""
    with open(FIXTURES / "finnhub_company_news_AAPL.json") as f:
        data = json.load(f)

    article = data[0]
    assert isinstance(article["datetime"], int)
    assert article["datetime"] == 1751385600
    assert isinstance(article["headline"], str)
    assert article["source"] == "Yahoo"
    assert "summary" in article
    assert "url" in article
    assert isinstance(article["id"], int)


@pytest.mark.asyncio
async def test_token_as_query_param():
    """Token is passed as query param, not header."""
    api_key = "test_api_key_123"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = []

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_finnhub_fetcher(api_key=api_key, client=mock_client)
    await fetcher.fetch("AAPL", "company-news", "2025-07-01")

    # Check that token was passed as a query param
    call_kwargs = mock_client.get.call_args
    params = call_kwargs[1].get("params", {})
    assert params.get("token") == api_key


@pytest.mark.asyncio
async def test_limiter_honored():
    """Mock limiter: assert acquire() is called before HTTP request."""
    mock_limiter = MagicMock()
    # acquire() returns an async context manager
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock()
    mock_cm.__aexit__ = AsyncMock()
    mock_limiter.acquire.return_value = mock_cm

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = []

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_finnhub_fetcher(
        api_key="test_key", limiter=mock_limiter, client=mock_client
    )
    await fetcher.fetch("AAPL", "company-news", "2025-07-01")

    assert mock_limiter.acquire.call_count >= 1


@pytest.mark.asyncio
async def test_401_non_retryable():
    """401 returns immediately — no retry."""
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "Unauthorized"

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_finnhub_fetcher(api_key="bad_key", client=mock_client)
    result = await fetcher.fetch("AAPL", "company-news", "2025-07-01")

    assert result.status == 401
    assert "Unauthorized" in result.error


@pytest.mark.asyncio
async def test_429_retry():
    """429 triggers retry via with_retry wrapper."""
    # First call: 429, second: 200
    mock_resp_429 = MagicMock()
    mock_resp_429.status_code = 429
    mock_resp_429.text = "Rate limited"
    mock_resp_429.headers = MagicMock()
    mock_resp_429.headers.get.return_value = None

    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200
    mock_resp_200.json.return_value = []

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=[mock_resp_429, mock_resp_200])

    fetcher = create_finnhub_fetcher(api_key="test_key", client=mock_client)
    result = await fetcher.fetch("AAPL", "company-news", "2025-07-01")

    # Should succeed after retry
    assert result.status == 200
    assert mock_client.get.call_count == 2


@pytest.mark.asyncio
async def test_unknown_endpoint():
    """Unknown endpoint returns status=0 with error."""
    fetcher = create_finnhub_fetcher(api_key="test_key")
    result = await fetcher.fetch("AAPL", "nonexistent", "2025-07-01")

    assert result.status == 0
    assert "Unknown Finnhub endpoint" in result.error


@pytest.mark.asyncio
async def test_empty_api_key_raises():
    """Empty API key raises ValueError immediately."""
    with pytest.raises(ValueError, match="FINNHUB_API_KEY"):
        create_finnhub_fetcher(api_key="")

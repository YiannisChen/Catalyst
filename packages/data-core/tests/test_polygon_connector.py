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
async def test_polygon_error_returns_status(monkeypatch):
    async def noop_sleep(_): pass
    monkeypatch.setattr("catalyst_data.retry.asyncio.sleep", noop_sleep)

    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.text = "Rate limit"
    mock_resp.headers = {}

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_polygon_fetcher(api_key="test_key", client=mock_client)
    result = await fetcher("AAPL", "ohlcv", "2026-01-15")

    # All retries exhausted — final result is 429
    assert result.status == 429
    assert result.error is not None


@pytest.mark.asyncio
async def test_polygon_429_retry_after_header_extracted(monkeypatch):
    """Connector must extract Retry-After header and pass it through to retry logic."""
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
            resp.text = "Rate limit"
            resp.headers = {"retry-after": "15"}
        else:
            resp.status_code = 200
            resp.json.return_value = {"results": []}
            resp.headers = {}
        return resp

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=fake_get)

    fetcher = create_polygon_fetcher(api_key="test_key", client=mock_client)
    result = await fetcher("AAPL", "ohlcv", "2026-01-15")

    assert result.status == 200
    assert call_count == 2
    # Must have slept 15s (from header), not 2s (exponential)
    assert sleeps == [15.0]


@pytest.mark.asyncio
async def test_polygon_server_error_preserves_response_text(monkeypatch):
    async def noop_sleep(_): pass
    monkeypatch.setattr("catalyst_data.retry.asyncio.sleep", noop_sleep)

    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_resp.text = "upstream unavailable"
    mock_resp.headers = {}

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_polygon_fetcher(api_key="test_key", client=mock_client)
    result = await fetcher("AAPL", "news", "2026-01-15")

    assert result.status == 503
    assert result.source_label == "polygon:news"
    assert "503" in result.error
    assert "upstream unavailable" in result.error


@pytest.mark.asyncio
async def test_polygon_timeout_returns_error(monkeypatch):
    import httpx

    async def noop_sleep(_): pass
    monkeypatch.setattr("catalyst_data.retry.asyncio.sleep", noop_sleep)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

    fetcher = create_polygon_fetcher(api_key="test_key", client=mock_client)
    result = await fetcher("AAPL", "ohlcv", "2026-01-15")

    assert result.status == 0
    assert "timeout" in result.error.lower()


@pytest.mark.asyncio
async def test_polygon_unknown_endpoint_returns_error_without_request():
    mock_client = AsyncMock()

    fetcher = create_polygon_fetcher(api_key="test_key", client=mock_client)
    result = await fetcher("AAPL", "unsupported_endpoint", "2026-01-15")

    assert result.status == 0
    assert result.source_label == "polygon:unsupported_endpoint"
    assert "unknown polygon endpoint" in result.error.lower()
    mock_client.get.assert_not_called()

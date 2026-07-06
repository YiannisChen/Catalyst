import pytest
from unittest.mock import AsyncMock, MagicMock
from catalyst_data.connectors.fred import create_fred_fetcher


@pytest.mark.asyncio
async def test_fred_fetches_series():
    mock_response = {
        "observations": [
            {"date": "2026-01-15", "value": "5.50"},
            {"date": "2026-01-14", "value": "5.50"},
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_response
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_fred_fetcher(api_key="test_key", client=mock_client)
    result = await fetcher("", "DFF", "2026-01-15")  # FRED doesn't use ticker

    assert result.status == 200
    assert result.data is not None


@pytest.mark.asyncio
async def test_fred_preserves_dot_values():
    mock_response = {
        "observations": [
            {"date": "2026-01-15", "value": "."},
            {"date": "2026-01-14", "value": "5.50"},
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_response
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_fred_fetcher(api_key="test_key", client=mock_client)
    result = await fetcher("", "DFF", "2026-01-15")

    assert result.status == 200
    obs = result.data.get("observations", [])
    assert any(o["value"] == "." for o in obs)


@pytest.mark.asyncio
async def test_fred_error_returns_status():
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Bad request"
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_fred_fetcher(api_key="test_key", client=mock_client)
    result = await fetcher("", "DFF", "2026-01-15")

    assert result.status == 400
    assert result.error is not None


@pytest.mark.asyncio
async def test_fred_timeout_returns_structured_error():
    import httpx

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

    fetcher = create_fred_fetcher(api_key="test_key", client=mock_client)
    result = await fetcher("", "DFF", "2026-01-15")

    assert result.status == 0
    assert result.source_label == "fred:DFF"
    assert "timeout" in result.error.lower()

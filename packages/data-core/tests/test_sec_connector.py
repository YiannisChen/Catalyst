import json, pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import httpx

from catalyst_data.connectors.sec import create_sec_fetcher

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_sec_submissions_parses_response():
    with open(FIXTURES / "sec_submissions_AAPL.json") as f:
        mock_data = json.load(f)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_data

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_sec_fetcher(user_agent="Test/1.0", client=mock_client)
    result = await fetcher.fetch("AAPL", "sec_submissions", "2025-05-01")

    assert result.status == 200
    assert result.data is not None
    assert "filings" in result.data
    assert "recent" in result.data["filings"]


@pytest.mark.asyncio
async def test_sec_submissions_404_error():
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = "Not Found"

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_sec_fetcher(user_agent="Test/1.0", client=mock_client)
    result = await fetcher.fetch("AAPL", "sec_submissions", "2025-01-01")

    assert result.status == 404
    assert result.error is not None


@pytest.mark.asyncio
async def test_fetch_document_html_extracts_text():
    with open(FIXTURES / "sec_8k_exhibit_99_1.htm", "rb") as f:
        html_bytes = f.read()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "text/html"}
    mock_resp.content = html_bytes

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_sec_fetcher(user_agent="Test/1.0", client=mock_client)
    result = await fetcher.fetch_document("https://example.com/doc.htm")

    assert result.status == 200
    assert result.data is not None
    assert result.data["text"] is not None
    assert len(result.data["text"]) > 100
    assert result.data["extraction_status"] == "success"


@pytest.mark.asyncio
async def test_fetch_document_pdf_skipped():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/pdf"}
    mock_resp.content = b"%PDF-1.4 fake pdf content"

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_sec_fetcher(user_agent="Test/1.0", client=mock_client)
    result = await fetcher.fetch_document("https://example.com/doc.pdf")

    assert result.status == 200
    assert result.data["extraction_status"] == "pdf_skipped"


@pytest.mark.asyncio
async def test_user_agent_passed_to_both():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"filings": {"recent": {}}}
    mock_resp.headers = {"content-type": "application/json"}

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_sec_fetcher(user_agent="MyApp/2.0 (test@test.com)", client=mock_client)
    await fetcher.fetch("AAPL", "sec_submissions", "2025-01-01")
    await fetcher.fetch_document("https://example.com/doc.htm")

    # Both calls should include User-Agent header
    for call in mock_client.get.call_args_list:
        kwargs = call[1] if call[1] else {}
        headers = kwargs.get("headers", {})
        assert "User-Agent" in headers
        assert headers["User-Agent"] == "MyApp/2.0 (test@test.com)"


@pytest.mark.asyncio
async def test_unknown_endpoint_returns_error():
    mock_client = AsyncMock()
    fetcher = create_sec_fetcher(user_agent="Test/1.0", client=mock_client)
    result = await fetcher.fetch("AAPL", "nonexistent", "2025-01-01")

    assert result.status == 0
    assert "Unknown SEC endpoint" in result.error
    mock_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_document_raw_bytes_preserved():
    """data["raw_bytes"] == resp.content on a mocked 200 HTML response."""
    html_bytes = b"<html><body><p>SEC Filing Content</p></body></html>"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "text/html"}
    mock_resp.content = html_bytes

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    fetcher = create_sec_fetcher(user_agent="Test/1.0", client=mock_client)
    result = await fetcher.fetch_document("https://example.com/doc.htm")

    assert result.status == 200
    assert result.data is not None
    assert result.data["raw_bytes"] == html_bytes
    assert "<html>" in result.data["raw_bytes"].decode("latin-1")

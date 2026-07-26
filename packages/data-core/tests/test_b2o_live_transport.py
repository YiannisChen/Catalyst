"""B2-O-X: Transport-level Polygon continuation URL tests.
Uses httpx.MockTransport to capture real httpx.Request objects."""
from __future__ import annotations

import asyncio
import httpx
import pytest
from urllib.parse import parse_qs, urlparse


def _make_handler(requests_captured: list):
    """Return an async handler for httpx.MockTransport that records requests."""

    async def handler(request: httpx.Request) -> httpx.Response:
        requests_captured.append(request)
        return httpx.Response(200, json={"results": [], "status": "OK"})

    return handler


def _make_client(requests_captured: list) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_make_handler(requests_captured)))


def _make_transport(polygon_key="test-key", client=None):
    from catalyst_data.b2o import B2OLiveTransport, ProviderCredentials
    from catalyst_data.config import RatePolicy

    clients = {"polygon": client} if client else None
    return B2OLiveTransport(
        credentials=ProviderCredentials(polygon=polygon_key),
        rate_policies={"polygon": RatePolicy(0, 1, None)},
        request_caps={},
        clients=clients,
    )


def _call(transport, page_url):
    return asyncio.run(
        transport._request_polygon(
            "polygon_news", "news", "AAPL", "2025-01-01", "2025-01-07", page_url
        )
    )


class TestPageUrlContinuation:
    """Page URL continuation with real httpx.MockTransport."""

    def test_cursor_preserved(self):
        """cursor from next_url must survive httpx request building."""
        requests = []
        client = _make_client(requests)
        transport = _make_transport(client=client)
        _call(transport, "https://api.polygon.io/v2/reference/news?cursor=cursor-2&limit=50")
        qs = parse_qs(urlparse(str(requests[0].url)).query)
        assert qs.get("cursor") == ["cursor-2"], f"cursor missing: {qs}"

    def test_limit_preserved(self):
        requests = []
        client = _make_client(requests)
        transport = _make_transport(client=client)
        _call(transport, "https://api.polygon.io/v2/reference/news?limit=50&cursor=abc")
        qs = parse_qs(urlparse(str(requests[0].url)).query)
        assert qs.get("limit") == ["50"], f"limit missing: {qs}"

    def test_all_query_pairs_preserved(self):
        requests = []
        client = _make_client(requests)
        transport = _make_transport(client=client)
        _call(transport, "https://api.polygon.io/v2/reference/news?cursor=abc&ticker=AAPL&sort=published_utc")
        qs = parse_qs(urlparse(str(requests[0].url)).query)
        assert "cursor" in qs
        assert "ticker" in qs
        assert "sort" in qs
        assert "apiKey" in qs

    def test_api_key_replaced_once(self):
        """Old apiKey removed, new apiKey appears exactly once."""
        requests = []
        client = _make_client(requests)
        transport = _make_transport(polygon_key="final-key", client=client)
        _call(transport, "https://api.polygon.io/v2/reference/news?cursor=abc&apiKey=old-key")
        qs = parse_qs(urlparse(str(requests[0].url)).query)
        api_keys = qs.get("apiKey", [])
        assert len(api_keys) == 1, f"apiKey count={len(api_keys)}: {requests[0].url}"
        assert api_keys[0] == "final-key", f"wrong apiKey: {api_keys[0]}"

    def test_api_key_added_when_missing(self):
        requests = []
        client = _make_client(requests)
        transport = _make_transport(polygon_key="added-key", client=client)
        _call(transport, "https://api.polygon.io/v2/reference/news?cursor=abc")
        qs = parse_qs(urlparse(str(requests[0].url)).query)
        assert qs.get("apiKey") == ["added-key"]

    def test_duplicate_query_pairs_preserved(self):
        """tag=a&tag=b must both survive."""
        requests = []
        client = _make_client(requests)
        transport = _make_transport(client=client)
        _call(transport, "https://api.polygon.io/v2/reference/news?tag=a&tag=b&cursor=x")
        url_str = str(requests[0].url)
        # Both 'tag' values must appear
        assert "tag=a" in url_str, f"tag=a missing: {url_str}"
        assert "tag=b" in url_str, f"tag=b missing: {url_str}"

    def test_blank_query_value_preserved(self):
        requests = []
        client = _make_client(requests)
        transport = _make_transport(client=client)
        _call(transport, "https://api.polygon.io/v2/reference/news?cursor=abc&empty=")
        url_str = str(requests[0].url)
        assert "empty=" in url_str or "empty" in url_str, f"blank value lost: {url_str}"

    def test_rejects_http(self):
        transport = _make_transport()
        with pytest.raises(ValueError, match="untrusted Polygon pagination URL"):
            _call(transport, "http://api.polygon.io/v2/reference/news?cursor=abc")

    def test_rejects_wrong_hostname(self):
        transport = _make_transport()
        with pytest.raises(ValueError, match="untrusted Polygon pagination URL"):
            _call(transport, "https://evil.com/v2/reference/news?cursor=abc")

    def test_no_network_calls(self, monkeypatch):
        """Verify httpx MockTransport never makes real network calls."""
        requests = []
        client = _make_client(requests)
        transport = _make_transport(client=client)
        # Should complete without network
        result = _call(transport, "https://api.polygon.io/v2/reference/news?cursor=abc")
        assert result.status == 200
        assert len(requests) == 1


class TestSecretLeakage:
    """Secret must not appear in transport repr or request params."""

    def test_secret_not_in_repr(self):
        from catalyst_data.b2o import ProviderCredentials, create_b2o_live_transport
        from catalyst_data.config import RatePolicy

        transport = create_b2o_live_transport(
            credentials=ProviderCredentials(
                polygon="super-secret-key-12345",
                finnhub="fh-12345",
                fred="fred-12345",
                sec_user_agent="test@example.com",
            ),
            rate_policies={"polygon": RatePolicy(0, 1, None)},
            request_caps={},
        )
        r = repr(transport)
        assert "super-secret-key-12345" not in r, f"secret leaked: {r[:200]}"


class TestInitialRequests:
    """Initial (non-continuation) requests must include params via httpx params kwarg."""

    def test_initial_news_includes_params(self):
        requests = []
        client = _make_client(requests)
        transport = _make_transport(client=client)
        asyncio.run(
            transport._request_polygon("polygon_news", "news", "AAPL", "2025-01-01", "2025-01-07", None)
        )
        qs = parse_qs(urlparse(str(requests[0].url)).query)
        assert qs.get("ticker") == ["AAPL"]
        assert "published_utc.gte" in qs
        assert "published_utc.lt" in qs
        assert "limit" in qs
        assert "apiKey" in qs

    def test_initial_ohlcv_includes_params(self):
        requests = []
        client = _make_client(requests)
        transport = _make_transport(client=client)
        asyncio.run(
            transport._request_polygon("polygon_ohlcv", "ohlcv", "AAPL", "2025-01-01", "2025-01-07", None)
        )
        qs = parse_qs(urlparse(str(requests[0].url)).query)
        assert qs.get("adjusted") == ["true"]
        assert "apiKey" in qs

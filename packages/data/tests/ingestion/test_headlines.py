from __future__ import annotations

from catalyst_data.ingestion.collectors import headlines


class Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or []

    def json(self):
        return self._payload


def test_missing_keys_report_disabled(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    assert headlines.finnhub_headlines_with_status("ABC", "a", "b") == ([], "DISABLED")
    assert headlines.polygon_headlines_with_status("ABC", "a", "b") == ([], "DISABLED")
    assert headlines.select_with_status("ABC", "a", "b", 5) == ([], "DISABLED")


def test_non_200_reports_error_without_exposing_key(monkeypatch):
    secret = "test-secret-token"
    monkeypatch.setenv("FINNHUB_API_KEY", secret)
    monkeypatch.setattr(headlines, "http_get", lambda *a, **k: Response(503))
    items, status = headlines.finnhub_headlines_with_status("ABC", "a", "b")
    assert items == [] and status == "ERROR"
    assert secret not in status


def test_select_filters_window_and_company_name(monkeypatch):
    rows = [
        {"url": "u1", "title": "Acme wins", "published": "2026-01-10T12:00:00Z", "provider": "finnhub"},
        {"url": "u2", "title": "Acme late", "published": "2026-01-11T12:00:00Z", "provider": "finnhub"},
        {"url": "u3", "title": "Acme old", "published": "2026-01-08T12:00:00Z", "provider": "finnhub"},
        {"url": "u4", "title": "Other firm wins", "published": "2026-01-10T12:00:00Z", "provider": "finnhub"},
    ]
    monkeypatch.setattr(headlines, "_names", lambda ticker, title: "Acme" in title)
    monkeypatch.setattr(headlines, "finnhub_headlines_with_status", lambda *a: (rows, "OK"))
    monkeypatch.setattr(headlines, "polygon_headlines_with_status", lambda *a: ([], "DISABLED"))
    picked, status = headlines.select_with_status(
        "ABC", "2026-01-09T00:00:00Z", "2026-01-10T23:59:59Z", 10
    )
    assert [row["url"] for row in picked] == ["u1"]
    assert status == "OK"


def test_gold_proxy_uses_finnhub_and_commodity_terms(monkeypatch):
    calls = []
    row = {"url": "u", "title": "Gold rises on demand", "published": "2026-01-10T12:00:00Z", "provider": "finnhub"}
    monkeypatch.setattr(headlines, "finnhub_headlines_with_status", lambda *a: (calls.append("finnhub") or [row], "OK"))
    monkeypatch.setattr(headlines, "polygon_headlines_with_status", lambda *a: (calls.append("polygon") or [], "OK"))
    picked, status = headlines.select_with_status(
        "GLD", "2026-01-09T00:00:00Z", "2026-01-10T23:59:59Z", 10
    )
    assert [row["url"] for row in picked] == ["u"]
    assert calls == ["finnhub"] and status == "OK"


def test_request_error_is_redacted(monkeypatch):
    import requests

    secret = "private-key-value"
    monkeypatch.setenv("FINNHUB_API_KEY", secret)

    def fail(*args, **kwargs):
        raise requests.RequestException(secret)

    monkeypatch.setattr(headlines, "http_get", fail)
    result = headlines.finnhub_headlines_with_status("ABC", "a", "b")
    assert result == ([], "ERROR")
    assert secret not in repr(result)

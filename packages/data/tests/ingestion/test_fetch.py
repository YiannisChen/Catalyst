from __future__ import annotations

import pytest
from catalyst_data.ingestion.collectors import fetch


class FakeResponse:
    def __init__(self, *, status_code=200, text="", url="", content_type="text/html", location=None):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = {"content-type": content_type}
        if location:
            self.headers["location"] = location

    @property
    def is_redirect(self):
        return "location" in self.headers and self.status_code in (301, 302, 303, 307, 308)


class FakeSession:
    def __init__(self, responses):
        self.headers = {}
        self.responses = responses

    def get(self, url, timeout=15, allow_redirects=False):
        return self.responses[url]


@pytest.fixture(autouse=True)
def no_host_wait(monkeypatch):
    monkeypatch.setattr(fetch, "PER_HOST_INTERVAL", 0)
    monkeypatch.setattr(fetch.time, "sleep", lambda _seconds: None)


def test_extract_strips_zero_width_characters_and_keeps_title_term():
    paras = "".join(
        f"<p>Acme reported {i} " + ("market activity ​‌‍⁠﻿" * 12) + "</p>"
        for i in range(5)
    )
    result = fetch.extract(f"<html><article><h1>Acme results</h1>{paras}</article></html>", "Acme")

    assert result is not None
    assert "Acme" in result
    assert not any(char in result for char in "\u200b\u200c\u200d\u2060\ufeff")


def test_extract_rejects_body_shorter_than_minimum():
    assert fetch.extract("<article><p>" + ("Acme short " * 4) + "</p></article>", "Acme") is None


def test_fetch_page_rejects_disallowed_redirect_and_returns_allowed_page():
    original = "https://source.example/story"
    redirected = "https://redirect.example/story"
    html = "<html><body>story</body></html>"
    session = FakeSession(
        {
            "https://source.example/robots.txt": FakeResponse(text="User-agent: *\nAllow: /"),
            "https://redirect.example/robots.txt": FakeResponse(text="User-agent: *\nDisallow: /"),
            # Each hop is requested separately, after its own robots check.
            original: FakeResponse(status_code=301, url=original, location=redirected),
            redirected: FakeResponse(text=html, url=redirected),
        }
    )
    fetcher = fetch.Fetcher(session=session)

    assert fetcher.fetch_page(original) is None

    session.responses["https://redirect.example/robots.txt"] = FakeResponse(
        text="User-agent: *\nAllow: /"
    )
    fetcher = fetch.Fetcher(session=session)
    assert fetcher.fetch_page(original) == (redirected, html)


def test_fetch_page_rejects_non_html_content_type():
    url = "https://source.example/story"
    session = FakeSession(
        {
            "https://source.example/robots.txt": FakeResponse(text="User-agent: *\nAllow: /"),
            url: FakeResponse(text="binary", url=url, content_type="application/pdf"),
        }
    )

    assert fetch.Fetcher(session=session).fetch_page(url) is None


@pytest.mark.parametrize(
    ("status", "error", "reason"),
    [
        (404, fetch.PermanentPageError, "http_404"),
        (503, fetch.TransientPageError, "http_503"),
    ],
)
def test_detailed_fetch_classifies_http_status_for_resumable_media(status, error, reason):
    url = "https://source.example/story"
    session = FakeSession(
        {
            "https://source.example/robots.txt": FakeResponse(text="User-agent: *\nAllow: /"),
            url: FakeResponse(status_code=status, url=url),
        }
    )
    with pytest.raises(error, match=reason):
        fetch.Fetcher(session=session).fetch_page_detailed(url)


def test_detailed_fetch_marks_robots_refusal_as_permanent():
    url = "https://source.example/story"
    session = FakeSession({
        "https://source.example/robots.txt": FakeResponse(text="User-agent: *\nDisallow: /"),
    })
    with pytest.raises(fetch.PermanentPageError, match="robots_disallowed"):
        fetch.Fetcher(session=session).fetch_page_detailed(url)


def test_relay_status_tells_a_refusing_relay_from_a_missing_one():
    """A live relay answers a url-less request with 403, so any answer is 'up'; a refused connection is 'down'."""
    import requests
    from catalyst_data.ingestion.collectors import fetch as fetch_module

    env = {"CATALYST_RELAY_URL": "http://127.0.0.1:8765/"}
    seen = []
    up = fetch_module.relay_status(env, get=lambda url, timeout: seen.append((url, timeout)) or type("R", (), {"status_code": 403})())
    assert up == {"configured": True, "up": True} and seen == [("http://127.0.0.1:8765/allowed", 5)]

    def refused(url, timeout):
        raise requests.ConnectionError("tunnel gone")

    assert fetch_module.relay_status(env, get=refused) == {"configured": True, "up": False, "reason": "ConnectionError"}
    assert fetch_module.relay_status({}, get=refused) == {"configured": False, "up": None}
    bad_gateway = fetch_module.relay_status(env, get=lambda url, timeout: type("R", (), {"status_code": 502})())
    assert bad_gateway == {"configured": True, "up": False, "reason": "HTTP 502"}

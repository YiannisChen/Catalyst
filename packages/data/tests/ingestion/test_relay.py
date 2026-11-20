from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest
import requests
from catalyst_data.ingestion.collectors import fetch, relay


class FakeFetcher:
    def allowed(self, url):
        return "/private/" not in url

    def get(self, url):
        class R:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            text = "<html>body</html>"
        R.url = url
        return R()


@pytest.fixture()
def relay_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), relay.make_handler(FakeFetcher(), log=lambda _: None))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_relayed_host_goes_through_the_relay(relay_url, monkeypatch):
    monkeypatch.setenv("CATALYST_RELAY_URL", relay_url)
    monkeypatch.setenv("CATALYST_RELAY_HOSTS", "www.benzinga.com")
    f = fetch.Fetcher()
    assert f.allowed("https://www.benzinga.com/news/1")
    assert not f.allowed("https://www.benzinga.com/private/1")
    assert f.fetch_page("https://www.benzinga.com/news/1") == ("https://www.benzinga.com/news/1", "<html>body</html>")


def test_relay_refuses_hosts_it_does_not_relay(relay_url):
    resp = requests.get(f"{relay_url}/get", params={"url": "https://example.com/"}, timeout=5)
    assert resp.status_code == 403


def test_a_dead_relay_fails_the_step_instead_of_returning_no_page(monkeypatch):
    monkeypatch.setenv("CATALYST_RELAY_URL", "http://127.0.0.1:9")  # nothing listens
    monkeypatch.setenv("CATALYST_RELAY_HOSTS", "www.benzinga.com")
    f = fetch.Fetcher()
    with pytest.raises(fetch.RelayUnavailable):
        f.fetch_page("https://www.benzinga.com/news/1")


def test_without_relay_env_hosts_are_fetched_directly(monkeypatch):
    monkeypatch.delenv("CATALYST_RELAY_URL", raising=False)
    assert fetch.Fetcher().relay_hosts == set()


def test_relay_slims_pages_without_changing_what_the_server_extracts():
    from catalyst_data.ingestion.collectors import benzinga

    ld = ('<script type="application/ld+json">{"@type": "NewsArticle", "headline": "Acme shares surge", '
          '"datePublished": "2026-03-02T15:00:00Z", "dateModified": "2026-03-02T15:00:00Z"}</script>')
    body = "".join(f"<p>Acme shares surged after paragraph {i} of the report was published today.</p>" for i in range(12))
    page = (f"<html><head>{ld}<script>var big = '{'x' * 5000}';</script><style>p{{}}</style></head>"
            f"<body><!-- ad --><svg><path d='M0'/></svg><article>{body}</article><noscript>n</noscript></body></html>")
    slim = relay.slim_html(page)
    assert len(slim) < len(page) // 3 and "application/ld+json" in slim and "var big" not in slim
    assert benzinga.article_metadata(slim) == benzinga.article_metadata(page)
    assert benzinga.extract(slim, "Acme shares surge") == benzinga.extract(page, "Acme shares surge")


def test_relay_responses_are_gzipped(relay_url):
    resp = requests.get(f"{relay_url}/get", params={"url": "https://www.benzinga.com/news/1"}, timeout=5)
    assert resp.headers["Content-Encoding"] == "gzip" and resp.json()["text"] == "<html>body</html>"


def test_a_host_marked_down_is_answered_503_and_retried_after_the_ttl():
    now = [0.0]

    class DownFetcher(FakeFetcher):
        def __init__(self):
            self.down = {}

        def allowed(self, url):
            if "www.benzinga.com" in self.down:
                raise fetch.HostUnavailable("www.benzinga.com", self.down["www.benzinga.com"])
            return True

    f = DownFetcher()
    server = ThreadingHTTPServer(("127.0.0.1", 0), relay.make_handler(f, log=lambda _: None, clock=lambda: now[0]))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/allowed"
    try:
        f.down["www.benzinga.com"] = "page ReadTimeout"
        assert requests.get(url, params={"url": "https://www.benzinga.com/x"}, timeout=5).status_code == 503
        now[0] = relay.DOWN_TTL_S + 1
        assert requests.get(url, params={"url": "https://www.benzinga.com/x"}, timeout=5).json() == {"allowed": True}
    finally:
        server.shutdown()

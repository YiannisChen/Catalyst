import threading
import time

import pytest
from catalyst_data.ingestion.collectors import fetch


class _Stub:
    def __init__(self):
        self.calls, self.active, self.peak = [], 0, 0
        self.lock = threading.Lock()

    def fetch_page(self, url):
        with self.lock:
            self.calls.append(url)
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.05)
        with self.lock:
            self.active -= 1
        if "down" in url:
            raise fetch.HostUnavailable("down.test", "robots.txt ConnectionError")
        return None if "missing" in url else (url + "#final", "<html>" + url + "</html>")

    def allowed(self, url):
        return True


def test_prefetch_overlaps_and_answers_like_the_sequential_fetcher():
    urls = [f"https://h{i}.test/a" for i in range(6)] + ["https://h0.test/missing"]
    stub = _Stub()
    pf = fetch.PrefetchedFetcher(stub, urls + urls[:2], workers=4)  # duplicates fetched once
    assert stub.peak > 1 and sorted(stub.calls) == sorted(urls)
    expected = [_Stub().fetch_page(u) for u in urls]
    assert [pf.fetch_page(u) for u in urls] == expected
    assert pf.allowed("x") is True  # other attributes pass through


def test_prefetched_exception_is_raised_on_use_and_unknown_urls_go_through():
    stub = _Stub()
    pf = fetch.PrefetchedFetcher(stub, ["https://down.test/x", "https://ok.test/y"], workers=2)
    with pytest.raises(fetch.HostUnavailable):
        pf.fetch_page("https://down.test/x")
    assert pf.fetch_page("https://late.test/z") == ("https://late.test/z#final", "<html>https://late.test/z</html>")


def test_one_worker_keeps_the_lazy_sequential_path():
    stub = _Stub()
    pf = fetch.PrefetchedFetcher(stub, ["https://a.test/1", "https://b.test/2"], workers=1)
    assert stub.calls == []
    pf.fetch_page("https://a.test/1")
    assert stub.calls == ["https://a.test/1"]


def test_real_fetcher_still_spaces_requests_to_one_host(monkeypatch):
    stamps = []

    class _Resp:
        status_code, url, headers, text, is_redirect = 200, "", {"content-type": "text/html"}, "<html></html>", False

    f = fetch.Fetcher()
    monkeypatch.setattr(f, "allowed", lambda url: True)
    monkeypatch.setattr(fetch, "PER_HOST_INTERVAL", 0.2)

    def get(url, **k):
        stamps.append(time.monotonic())
        r = _Resp(); r.url = url
        return r
    monkeypatch.setattr(f.session, "get", get)
    urls = [f"https://same-host-spacing.test/{i}" for i in range(4)]
    fetch.PrefetchedFetcher(f, urls, workers=4)
    gaps = [b - a for a, b in zip(sorted(stamps), sorted(stamps)[1:])]
    assert len(stamps) == 4 and min(gaps) >= 0.15

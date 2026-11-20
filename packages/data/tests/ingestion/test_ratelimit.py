from catalyst_data.ingestion import ratelimit
from catalyst_data.ingestion.collectors import headlines


class _Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_bucket_allows_the_burst_then_paces_at_the_per_minute_rate():
    c = _Clock()
    b = ratelimit.TokenBucket(per_minute=5, capacity=5, clock=c.now, sleep=c.sleep)
    for _ in range(5):
        assert b.acquire() == 0  # the free tier's five per minute go at once
    b.acquire()
    assert abs(c.t - 12.0) < 1e-9  # the sixth waits one refill interval
    b.backoff(1)
    assert c.slept[-1] == 24.0  # after a 429: two intervals


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._payload = status, payload or {}

    def json(self):
        return self._payload


def test_polygon_429_backs_off_and_retries(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "x")
    c = _Clock()
    monkeypatch.setattr(ratelimit, "POLYGON", ratelimit.TokenBucket(5, 5, clock=c.now, sleep=c.sleep))
    answers = [_Resp(429), _Resp(200, {"results": [{"article_url": "https://a.test/1", "title": "t",
                                                     "published_utc": "2026-10-15T12:00:00Z"}]})]
    monkeypatch.setattr(headlines, "http_get", lambda *a, **k: answers.pop(0))
    rows, status = headlines.polygon_headlines_with_status("AAPL", "2026-10-14T20:00:00Z", "2026-10-15T20:00:00Z")
    assert status == "OK" and len(rows) == 1 and c.slept  # backed off once, then succeeded


def test_finnhub_first_call_does_not_wait(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "x")
    c = _Clock()
    monkeypatch.setattr(ratelimit, "FINNHUB", ratelimit.TokenBucket(60, 10, clock=c.now, sleep=c.sleep))
    monkeypatch.setattr(headlines, "http_get", lambda *a, **k: _Resp(200, []))
    headlines.finnhub_headlines_with_status("AAPL", "2026-10-14", "2026-10-15")
    assert c.slept == []

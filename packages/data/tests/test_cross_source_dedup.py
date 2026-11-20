from __future__ import annotations

from datetime import datetime, timedelta, timezone

from catalyst_data.dedup.cross_source import (
    AssetCandidate,
    canonical_url,
    duplicate_across_source,
    select_canonical,
)


def _candidate(
    *,
    source_type: str = "polygon_news",
    title: str = "Apple earnings beat",
    published_utc: datetime | None = None,
    url: str = "https://example.com/news?a=1&b=2",
    ticker_primary: str = "AAPL",
    body_md: str = "A" * 80,
) -> AssetCandidate:
    return AssetCandidate(
        source_type=source_type,
        title=title,
        published_utc=published_utc or datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc),
        url=url,
        ticker_primary=ticker_primary,
        body_md=body_md,
    )


def test_canonical_url_drops_tracking_params_and_normalizes_host():
    url = "https://Foo.com/path/?utm_source=x&b=2&a=1&ref_src=y&mc_cid=z#frag"

    assert canonical_url(url) == "https://foo.com/path?a=1&b=2"


def test_canonical_url_is_idempotent():
    url = "https://foo.com/path?a=1&b=2"

    assert canonical_url(canonical_url(url)) == canonical_url(url)


def test_duplicate_across_source_matches_same_canonical_url():
    a = _candidate(url="https://Foo.com/path/?utm_source=x&a=1#frag")
    b = _candidate(
        source_type="fmp_news",
        url="https://foo.com/path?a=1",
        title="Completely different title",
    )

    assert duplicate_across_source(a, b) is True


def test_duplicate_across_source_uses_title_ticker_and_four_hour_window():
    ts = datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc)
    a = _candidate(title="Apple earnings beat", published_utc=ts)
    b = _candidate(
        source_type="fmp_news",
        title="Apple earnings beat",
        url="https://example.com/other",
        published_utc=ts + timedelta(hours=3, minutes=59),
    )
    c = _candidate(
        source_type="finnhub_company_news",
        title="Apple earnings beat",
        url="https://example.com/later",
        published_utc=ts + timedelta(hours=4, minutes=1),
    )

    assert duplicate_across_source(a, b) is True
    assert duplicate_across_source(a, c) is False


def test_select_canonical_prefers_primary_source_priority():
    polygon = _candidate(source_type="polygon_news")
    fmp = _candidate(source_type="fmp_news")

    assert select_canonical([fmp, polygon]) is polygon


def test_select_canonical_prefers_earliest_within_same_source():
    later = _candidate(published_utc=datetime(2026, 4, 3, 13, 0, tzinfo=timezone.utc))
    earlier = _candidate(published_utc=datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc))

    assert select_canonical([later, earlier]) is earlier


def test_select_canonical_prefers_longest_body_with_same_source_and_time():
    short = _candidate(body_md="short")
    long = _candidate(body_md="long body" * 20, url="https://example.com/2")

    assert select_canonical([short, long]) is long

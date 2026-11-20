from catalyst_data.ingestion.collectors import kitco
from catalyst_data.ingestion.time import times


def test_reconcile_keeps_the_later_time_and_flags_whole_hours():
    # Finnhub's CNBC time: the New York clock labelled UTC (4 h early in EDT)
    assert times.reconcile("2026-10-16T12:11:09Z", "2026-10-16T16:11:09+0000", provider="finnhub",
                           publisher="CNBC") == ("2026-10-16T16:11:09Z", "suspected_tz")
    assert times.reconcile("2026-01-05T10:14:23Z", "2026-01-05T15:14:23+00:00", provider="finnhub",
                           publisher="CNBC")[1] == "suspected_tz"  # 5 h in EST
    assert times.reconcile("2026-10-16T12:00:00Z", "2026-10-16T12:00:30Z", provider="polygon",
                           publisher="Yahoo Finance") == ("2026-10-16T12:00:30Z", "agree")
    assert times.reconcile("2026-10-16T13:00:00Z", "2026-10-16T12:00:00Z", provider="polygon",
                           publisher="Yahoo Finance") == ("2026-10-16T13:00:00Z", "api_later")


def test_no_page_time_and_naive_times():
    assert times.reconcile("2026-10-16T12:11:09Z", None, provider="finnhub", publisher="CNBC") == (None, "imprecise")
    assert times.reconcile("2026-10-16T12:11:09Z", None, provider="polygon", publisher="CNBC") == (
        "2026-10-16T12:11:09Z", "api_only")
    # a page time without a time zone is not used
    assert times.reconcile("2026-10-16T12:11:09Z", "2026-10-16T12:11:09", provider="finnhub",
                           publisher="CNBC") == (None, "imprecise")
    assert times.utc_or_none("2026-10-16T12:11:09") is None


def test_kitco_sitemap_skips_dates_without_a_time_zone():
    xml = ("<url><loc>https://www.kitco.com/a</loc><news:publication_date>2026-10-19T16:10:52-04:00"
           "</news:publication_date><news:title><![CDATA[A]]></news:title></url>"
           "<url><loc>https://www.kitco.com/b</loc><news:publication_date>2026-10-19T16:10:52"
           "</news:publication_date><news:title><![CDATA[B]]></news:title></url>")
    assert kitco.parse_sitemap(xml) == [("https://www.kitco.com/a", "2026-10-19T20:10:52Z", "A")]


def test_company_rows_keep_both_times_and_drop_imprecise(tmp_path, monkeypatch):
    import sqlite3

    from catalyst_data.ingestion.ops import refresh
    monkeypatch.setenv("CATALYST_PAGE_VERSIONS_DB", str(tmp_path / "pv.sqlite3"))
    body = "<p>ABC quarterly update and operating results. " + "Results increased across all operations. " * 30 + "</p>"

    class Fetcher:
        def fetch_page(self, url):
            ld = ('<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-10-16T16:11:09+0000",'
                  '"dateModified":"2026-10-16T16:46:18+0000"}</script>') if "with-time" in url else ""
            return url, f"<html><head>{ld}</head><article>{body}</article></html>"

    monkeypatch.setattr(refresh.fetch, "PrefetchedFetcher", lambda f, urls: f)
    items = [{"url": "https://www.cnbc.com/2026/10/16/with-time.html", "title": "ABC quarterly update",
              "published": "2026-10-16T12:11:09Z", "provider": "finnhub", "ticker": "ABC"},
             {"url": "https://www.cnbc.com/2026/10/16/no-time.html", "title": "ABC quarterly update",
              "published": "2026-10-16T12:11:09Z", "provider": "finnhub", "ticker": "ABC"}]
    rows = refresh._company_rows(Fetcher(), items)
    assert [(r["url"].rsplit("/", 1)[-1], r["eligible_at"]) for r in rows] == [("with-time.html", "2026-10-16T16:11:09Z")]
    kept = sqlite3.connect(tmp_path / "pv.sqlite3").execute(
        "SELECT url, api_published, page_published, eligible_at, flag FROM body_times ORDER BY url").fetchall()
    assert kept == [("https://www.cnbc.com/2026/10/16/no-time.html", "2026-10-16T12:11:09Z", None, None, "imprecise"),
                    ("https://www.cnbc.com/2026/10/16/with-time.html", "2026-10-16T12:11:09Z", "2026-10-16T16:11:09Z",
                     "2026-10-16T16:11:09Z", "suspected_tz")]


def test_bars_with_a_missing_close_are_not_stored(tmp_path):
    import sqlite3
    from datetime import datetime, timezone

    import pandas as pd
    from catalyst_data.ingestion.collectors import market
    conn = sqlite3.connect(tmp_path / "m.sqlite3")
    conn.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, "
                 "source TEXT, PRIMARY KEY (symbol, date))")
    frame = pd.DataFrame({"Open": [1.0, 2.0], "High": [1.5, 2.5], "Low": [0.5, 1.5], "Close": [1.2, float("nan")],
                          "Volume": [10.0, 20.0]}, index=pd.to_datetime(["2026-10-20", "2026-10-21"]))
    out = market.load_bars(conn, ["PFE"], "2026-10-20", "2026-10-21", now=datetime(2026, 10, 22, 11, tzinfo=timezone.utc),
                           download=lambda *a, **k: frame)
    assert out == {"PFE": 1}
    assert conn.execute("SELECT date FROM ohlcv").fetchall() == [("2026-10-20",)]
    metadata = conn.execute(
        "SELECT symbol,date,first_seen_at,is_backfill FROM ohlcv_metadata"
    ).fetchall()
    assert len(metadata) == 1
    assert metadata[0][:2] == ("PFE", "2026-10-20")
    assert metadata[0][2] is not None
    assert metadata[0][3] == 0

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from catalyst_data.ingestion.collectors import cnbc_roundups

FIXTURES = Path(__file__).parent / "fixtures" / "cnbc"


def test_sitemap_parser_decodes_urls_and_classifies_roundup_kinds():
    items = cnbc_roundups.parse_sitemap((FIXTURES / "sitemap_2026_April_14.html").read_text())
    assert len(items) == 3
    assert items[0]["url"].endswith("biggest-moves-premarket.html")
    assert cnbc_roundups.classify_title(items[0]["title"]) == "premarket"
    assert cnbc_roundups.classify_title(items[1]["title"]) == "midday"
    assert cnbc_roundups.classify_title(items[2]["title"]) == "analyst_calls"
    assert cnbc_roundups.classify_title("Stocks making the biggest moves after hours") == "after_hours"
    assert all(cnbc_roundups.classify_title(item["title"]) for item in items)
    assert all("market-news.html" not in item["url"] for item in items)


def test_fixture_company_misses_map_issuer_leads_and_analyst_targets():
    cases = json.loads((FIXTURES / "mapping_cases.json").read_text())
    segments = cnbc_roundups.segment_body(cases["multi_company_head"], "premarket")
    assert segments[0]["tickers"] == ["NEM", "FCX"]
    assert cnbc_roundups.segment_body(cases["blackrock"], "premarket")[0]["tickers"] == ["BLK"]
    assert cnbc_roundups.segment_body(cases["air_products"], "premarket")[0]["tickers"] == ["APD"]
    analyst = cnbc_roundups.segment_body(cases["analyst_calls"], "analyst_calls")
    assert analyst[0]["tickers"] == ["MDT"]
    assert cnbc_roundups.map_company("BlackRock, Inc.") == ["BLK"]
    assert cnbc_roundups.map_company("Air Products & Chemicals, Inc.") == ["APD"]


def test_inline_company_price_moves_are_stored_as_distinct_inline_segments():
    cases = json.loads((FIXTURES / "mapping_cases.json").read_text())
    body = "AT&T — The carrier declined 1% after an update. " + cases["inline_sentence"]
    segments = cnbc_roundups.segment_body(body, "premarket")
    inline = [row for row in segments if row["segment_type"] == "inline"]
    assert {ticker for row in inline for ticker in row["tickers"]} == {"FCX"}
    assert all(row["text"] == cases["inline_sentence"] for row in inline)
    assert all(row["segment_type"] == "block" for row in segments if row["company"] == "AT&T")


def test_discover_day_persists_inline_segment_type_and_unique_index(tmp_path):
    cases = json.loads((FIXTURES / "mapping_cases.json").read_text())
    sitemap_url = cnbc_roundups.sitemap_url("2026-04-14")
    story_url = "https://www.cnbc.com/2026/04/14/stocks-making-biggest-moves-premarket.html"
    sitemap = ('<script type="application/json">{"items":[{"__typename":"cnbcnewsstory",'
               '"title":"Stocks making the biggest moves premarket",'
               f'"url":"{story_url}"}}]}}</script>')
    page = ('<html><head><meta property="og:title" content="Stocks making the biggest moves premarket">'
            '<script type="application/ld+json">{"datePublished":"2026-04-14T08:00:00Z"}'
            '</script></head><body><div class="ArticleBody-articleBody">'
            'AT&T — The carrier declined 1% after an update. ' + cases["inline_sentence"]
            + '</div></body></html>')

    class Fetcher:
        def fetch_page(self, url):
            if url == sitemap_url:
                return url, sitemap
            if url == story_url:
                return url, page
            return url, "<html></html>"

    db = tmp_path / "web.sqlite3"
    cnbc_roundups.discover_day("2026-04-14", Fetcher(), db)
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT ticker,segment_index,segment_text,segment_type FROM roundup_segments ORDER BY segment_index,ticker"
    ).fetchall()
    conn.close()
    by_ticker = {row[0]: row for row in rows}
    assert by_ticker["T"][3] == "block"
    assert by_ticker["FCX"][2] == cases["inline_sentence"]
    assert by_ticker["FCX"][3] == "inline"
    assert by_ticker["FCX"][1] != by_ticker["T"][1]


def test_cached_segments_preserves_rows_order_and_inclusive_window(tmp_path):
    db = tmp_path / "web.sqlite3"
    conn = cnbc_roundups._connect(db)
    rows = [
        ("2026-04-14", "premarket", "https://cnbc.com/a", "https://cnbc.com/a",
         "A", "2026-04-14T09:30:00Z", "jsonld.datePublished", "cnbc_article_body",
         "TSLA", 1, "later", "seg-a", "page-a", "fetched", 0, "block"),
        ("2026-04-14", "midday", "https://cnbc.com/b", "https://cnbc.com/b",
         "B", "2026-04-14T09:30:00Z", "jsonld.datePublished", "cnbc_article_body",
         "TSLA", 2, "same-time midday", "seg-b", "page-b", "fetched", 0, "inline"),
        ("2026-04-14", "premarket", "https://cnbc.com/c", "https://cnbc.com/c",
         "C", None, None, "cnbc_article_body", "TSLA", 1, "undated", "seg-c",
         "page-c", "fetched", 0, "block"),
        ("2026-04-14", "premarket", "https://cnbc.com/d", "https://cnbc.com/d",
         "D", "2026-04-14T10:00:00Z", "jsonld.datePublished", "cnbc_article_body",
         "AAPL", 1, "other ticker", "seg-d", "page-d", "fetched", 0, "block"),
    ]
    conn.executemany("INSERT INTO roundup_segments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    selected = cnbc_roundups.cached_segments(
        "tsla", "2026-04-14", db_path=db,
        start_at="2026-04-14T09:00:00Z", end_at="2026-04-14T09:30:00Z")
    assert [(row["canonical_url"], row["segment_text"], row["segment_type"])
            for row in selected] == [
        ("https://cnbc.com/b", "same-time midday", "inline"),
        ("https://cnbc.com/a", "later", "block"),
    ]


def test_web_evidence_roundup_facade_uses_cnbc_reader(monkeypatch):
    from catalyst_data import web_evidence

    expected = [{"ticker": "TSLA", "segment_text": "roundup"}]
    calls = []

    def cached_segments(ticker, session_date, **kwargs):
        calls.append((ticker, session_date, kwargs))
        return expected

    monkeypatch.setattr(cnbc_roundups, "cached_segments", cached_segments)
    rows = web_evidence.cached_roundup_segments(
        "TSLA", "2026-04-14", start_at="start", end_at="end")
    assert rows is expected
    assert calls == [("TSLA", "2026-04-14", {"start_at": "start", "end_at": "end"})]


def test_company_motion_does_not_match_similar_issuer_or_bare_ticker():
    cases = json.loads((FIXTURES / "mapping_cases.json").read_text())
    assert cnbc_roundups.segment_body(cases["united_airlines_negative"], "premarket") == []
    assert cnbc_roundups.map_company(cases["bare_ticker_negative"]) == []


def test_article_body_and_jsonld_page_owned_date_are_extracted():
    html = (FIXTURES / "movers_article.html").read_text()
    assert "AT&T" in cnbc_roundups.extract_body(html)
    assert cnbc_roundups.extract_published_at(html) == "2026-04-14T16:12:00Z"
    fallback = '<span class="xyz-data">Fallback <b>CNBC</b> body.</span>'
    assert cnbc_roundups.extract_body(fallback) == "Fallback CNBC body."
    assert cnbc_roundups.extract_published_at("<html><body>No metadata</body></html>") is None


def test_movers_segments_map_required_names_and_ignore_bare_single_letter_ticker():
    html = (FIXTURES / "movers_article.html").read_text()
    segments = cnbc_roundups.segment_body(cnbc_roundups.extract_body(html), "premarket")
    by_company = {row["company"]: row["tickers"] for row in segments}
    assert by_company["Netflix"] == ["NFLX"]
    assert by_company["Lockheed Martin"] == ["LMT"]
    assert by_company["Amazon.com"] == ["AMZN"]
    assert by_company["Intel"] == ["INTC"]
    assert by_company["Adobe"] == ["ADBE"]
    assert by_company["AT&T"] == ["T"]
    assert not cnbc_roundups.map_company("T")


def test_analyst_calls_segment_by_heading_target_company():
    html = (FIXTURES / "analyst_calls_article.html").read_text()
    segments = cnbc_roundups.segment_article(html, "analyst_calls")
    assert [row["tickers"] for row in segments] == [["NOW"], ["AMZN"]]
    assert "growth expectations" in segments[0]["text"]


def test_discover_day_persists_page_even_when_no_segments_and_reprocesses(tmp_path):
    sitemap = (FIXTURES / "sitemap_2026_April_14.html").read_text()
    empty_html = '''<html><head><title>Stocks making the biggest moves premarket</title>
    <script type="application/ld+json">{"datePublished":"2026-04-14T08:00:00+0000"}</script>
    </head><body><div class="ArticleBody-articleBody">Market overview only, no company blocks.</div></body></html>'''
    pages = {
        "https://www.cnbc.com/site-map/articles/2026/April/14/": ("https://www.cnbc.com/site-map/articles/2026/April/14/", sitemap),
        "https://www.cnbc.com/site-map/articles/2026/April/13/": ("https://www.cnbc.com/site-map/articles/2026/April/13/", "<html></html>"),
        "https://www.cnbc.com/2026/04/14/stocks-making-biggest-moves-premarket.html": ("https://www.cnbc.com/2026/04/14/stocks-making-biggest-moves-premarket.html", empty_html),
    }

    class Fetcher:
        def fetch_page(self, url):
            if url not in pages:
                raise RuntimeError("fixture fetch failure")
            return pages[url]

    db = tmp_path / "web.sqlite3"
    result = cnbc_roundups.discover_day("2026-04-14", Fetcher(), db)
    assert result["status"] == "PARTIAL"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM roundup_pages").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM roundup_segments").fetchone()[0] == 0
    conn.close()
    assert cnbc_roundups.reprocess(db)["pages"] == 1


def test_discover_day_records_page_failure_and_continues_to_other_pages(tmp_path):
    sitemap = (FIXTURES / "sitemap_2026_April_14.html").read_text()
    article = (FIXTURES / "analyst_calls_article.html").read_text()

    class Fetcher:
        def fetch_page(self, url):
            if "/site-map/" in url:
                return url, sitemap
            if url.endswith("biggest-analyst-calls.html"):
                return url, article
            if "/2026/04/14/" in url:
                raise RuntimeError("fixture article failure")
            return url, "<html></html>"

    db = tmp_path / "web.sqlite3"
    result = cnbc_roundups.discover_day("2026-04-14", Fetcher(), db)
    assert result["status"] == "PARTIAL"
    conn = sqlite3.connect(db)
    errors = conn.execute("SELECT count(*) FROM roundup_day_errors").fetchone()[0]
    pages = conn.execute("SELECT count(*) FROM roundup_pages").fetchone()[0]
    conn.close()
    assert errors >= 1
    assert pages >= 1

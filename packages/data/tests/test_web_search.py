from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from catalyst_data.ingestion.collectors import web_search
from catalyst_data.ingestion.collectors.web_search import (
    classify_page_time,
    validate_hints,
)


def test_hints_are_bounded_plain_text():
    assert validate_hints(["earnings", "contract award"]) == ("earnings", "contract award")
    with pytest.raises(ValueError, match="hints_limit"):
        validate_hints(["one", "two", "three"])
    with pytest.raises(ValueError, match="hint_length"):
        validate_hints(["x" * 121])
    with pytest.raises(ValueError, match="hint_plain_text"):
        validate_hints(["<script>alert(1)</script>"])
    with pytest.raises(ValueError, match="hint_disallowed_query"):
        validate_hints(["why shares moved"])


def test_page_time_requires_unique_article_jsonld_and_modified_before_cutoff():
    start = datetime(2026, 4, 22, tzinfo=timezone.utc)
    cutoff = datetime(2026, 4, 23, 20, tzinfo=timezone.utc)

    def page(*items):
        return "".join(
            '<script type="application/ld+json">' + json.dumps(item) + "</script>"
            for item in items
        )

    article = {"@type": "NewsArticle", "datePublished": "2026-04-23T14:00:00-04:00"}
    assert classify_page_time(page(article), start, cutoff)[0] == "PRE"
    assert classify_page_time(
        page({**article, "dateModified": "2026-04-23T17:00:00-04:00"}), start, cutoff
    )[0] == "POST"
    assert classify_page_time(page(article, article), start, cutoff)[0] == "UNDATED"
    assert classify_page_time(
        page({**article, "datePublished": "2026-04-21T14:00:00-04:00"}), start, cutoff
    )[0] == "UNDATED"


def test_page_time_fallback_requires_explicit_timezone():
    start = datetime(2026, 4, 22, tzinfo=timezone.utc)
    cutoff = datetime(2026, 4, 23, 20, tzinfo=timezone.utc)

    assert classify_page_time(
        '<meta property="article:published_time" content="2026-04-23T14:00:00-04:00">',
        start,
        cutoff,
    )[0] == "PRE"
    assert classify_page_time(
        '<meta property="article:published_time" content="2026-04-23 14:00:00">',
        start,
        cutoff,
    )[0] == "UNDATED"


def test_search_uses_header_key_and_caches_response_without_key(tmp_path, monkeypatch):
    monkeypatch.setattr(web_search, "_company_name", lambda _ticker: "Tesla, Inc.")
    monkeypatch.setenv("TAVILY_API_KEY", "server-secret-value")
    html = (
        '<script type="application/ld+json">'
        '{"@type":"NewsArticle","headline":"Tesla factory article title",'
        '"datePublished":"2026-04-23T14:00:00-04:00"}'
        '</script><meta property="og:image" content="https://example.com/photo.jpg">'
        '<article><p>' + ("Tesla announced a long detailed project today. " * 40) + '</p></article>'
    )

    class FakeFetcher:
        def fetch_page(self, _url):
            return "https://example.com/story", html

    seen = []

    def search(query, key, *, start, end):
        seen.append((query, key, start, end))
        return {"results": [{"url": "https://example.com/story", "title": "Tesla announces project",
                             "source": "Example", "content": "A" * 500, "published_date": "2026-04-23"}]}

    response = web_search.search_leads(
        "TSLA", "2026-04-23", api_key="header-secret-value", search_fn=search,
        fetcher=FakeFetcher(), db_path=tmp_path / "web.sqlite3",
    )
    assert len(seen) == 3
    assert all(entry[1] == "header-secret-value" for entry in seen)
    assert response["credits_used"] == 3 and response["leads"][0]["status"] == "PRE"
    assert response["leads"][0]["title"] == "Tesla factory article title"
    assert response["leads"][0]["snippet"] == "A" * 400
    assert "header-secret-value" not in json.dumps(response)
    cached = web_search.search_leads(
        "TSLA", "2026-04-23", api_key="header-secret-value", search_fn=search,
        fetcher=FakeFetcher(), db_path=tmp_path / "web.sqlite3",
    )
    assert cached["cached"] is True and cached["credits_used"] == 0
    assert len(seen) == 3
    stored = web_search.get_pre_lead(response["leads"][0]["lead_id"], db_path=tmp_path / "web.sqlite3")
    assert stored["extracted_text"] and len(stored["html_sha256"]) == 64
    assert json.loads(stored["pit_metadata_json"])["datePublished"] == "2026-04-23T14:00:00-04:00"


def test_server_daily_budget_rejects_before_search(tmp_path, monkeypatch):
    monkeypatch.setattr(web_search, "_company_name", lambda _ticker: "Tesla, Inc.")
    monkeypatch.setenv("CATALYST_TAVILY_DAILY_CREDITS", "2")
    calls = []
    with pytest.raises(web_search.WebSearchError, match="daily_credit_limit"):
        web_search.search_leads(
            "TSLA", "2026-04-23", server_key="server-key", search_fn=lambda *a, **k: calls.append(a),
            fetcher=object(), db_path=tmp_path / "budget.sqlite3",
        )
    assert calls == []


def test_search_filters_off_subject_dedupes_titles_and_prioritizes_title_mentions(tmp_path, monkeypatch):
    monkeypatch.setattr(web_search, "_company_name", lambda _ticker: "Alphabet Inc.")
    html_by_url = {
        "https://example.com/off": '<script type="application/ld+json">{"@type":"NewsArticle","headline":"Prediction: Nvidia Stock Will Soar","datePublished":"2026-05-13T10:00:00-04:00"}</script><article><p>' + ("Nvidia outlook. " * 80) + '</p></article>',
        "https://example.com/original": '<script type="application/ld+json">{"@type":"NewsArticle","headline":"Alphabet Announces Cloud Results!","datePublished":"2026-05-13T09:00:00-04:00"}</script><article><p>Alphabet announced its results. ' + ("Details follow. " * 80) + '</p></article>',
        "https://example.com/syndicated": '<script type="application/ld+json">{"@type":"NewsArticle","headline":"ALPHABET announces cloud results","datePublished":"2026-05-13T10:00:00-04:00"}</script><article><p>Alphabet announced its results. ' + ("Details follow. " * 80) + '</p></article>',
        "https://example.com/body-match": '<script type="application/ld+json">{"@type":"NewsArticle","headline":"Cloud company shares rise","datePublished":"2026-05-13T11:00:00-04:00"}</script><article><p>' + ("Alphabet says cloud revenue rose. " * 50) + '</p></article>',
    }

    class FakeFetcher:
        def fetch_page(self, url):
            return url, html_by_url[url]

    rows = [
        {"url": url, "title": url, "source": publisher, "content": "search snippet", "published_date": "2026-05-13"}
        for url, publisher in (
            ("https://example.com/off", "Nvidia Source"),
            ("https://example.com/original", "Original Publisher"),
            ("https://example.com/syndicated", "Syndicator"),
            ("https://example.com/body-match", "Body Match Publisher"),
        )
    ]
    result = web_search.search_leads(
        "GOOGL", "2026-05-13", server_key="server-key", search_fn=lambda *a, **k: {"results": rows},
        fetcher=FakeFetcher(), db_path=tmp_path / "filtered.sqlite3",
    )

    assert result["excluded"]["off_subject"] == 1
    assert [lead["publisher"] for lead in result["leads"]] == ["Original Publisher", "Body Match Publisher"]
    assert result["leads"][0]["also_published_by"] == ["Syndicator"]
    assert result["leads"][0]["mentions_in_title"] is True
    assert result["leads"][1]["mentions_in_title"] is False


def test_issuer_matcher_uses_full_company_name_and_keeps_shared_matcher(tmp_path):
    from catalyst_data.ingestion.collectors.web_search import issuer_matcher

    matcher = issuer_matcher("AMD", "Advanced Micro Devices, Inc.")

    assert matcher("Advanced Micro Devices reports new products")
    assert not matcher("Advanced battery maker reports new products")


def test_shared_subject_matcher_unchanged_for_pipeline():
    """The evaluated pipeline's matcher stays case-sensitive on the ticker."""
    from catalyst_data.retrieval.web_bodies import subject_matcher

    assert not subject_matcher("NOW")("Stocks rally now as yields fall")

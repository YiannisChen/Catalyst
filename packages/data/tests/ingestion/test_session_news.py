from __future__ import annotations

import json
import sqlite3

import pytest
from catalyst_data.ingestion.session_news import session_news


def _stores(tmp_path):
    bodies = tmp_path / "bodies.sqlite3"
    archive = tmp_path / "archive.sqlite3"
    (tmp_path / "sec_company_tickers.json").write_text(
        json.dumps({"0": {"ticker": "ACME", "title": "Acme Corporation"}})
    )
    with sqlite3.connect(bodies) as conn:
        conn.executescript(
            """
            CREATE TABLE web_body_chunks (
                asset_id TEXT, ordinal INTEGER, chunk_id TEXT, text TEXT,
                canonical_url TEXT, title TEXT, publisher TEXT, eligible_at TEXT
            );
            CREATE TABLE web_headlines (asset_id TEXT, ticker TEXT, eligible_at TEXT);
            CREATE TABLE web_asset_media (
                asset_id TEXT, canonical_url TEXT, image_url TEXT, image_source TEXT, checked_at TEXT
            );
            INSERT INTO web_body_chunks VALUES
              ('web:body1',1,'web:body1:web_body:0001','Acme reports first-quarter results and beats estimates.',
               'https://news.example.com/earnings/','Acme beats earnings estimates','Daily News','2026-01-06T13:00:00Z'),
              ('web:body2',1,'web:body2:web_body:0001','Acme launches a new chip.',
               'https://news.example.com/product','Acme launches new chip','Tech Daily','2026-01-06T15:00:00Z');
            INSERT INTO web_headlines VALUES
              ('web:body1','ACME','2026-01-06T13:00:00Z'),
              ('web:body2','ACME','2026-01-06T15:00:00Z');
            INSERT INTO web_asset_media VALUES
              ('web:body1','https://news.example.com/earnings/','https://img.example.com/og.jpg','og','now'),
              ('web:body2','https://news.example.com/product','https://img.example.com/placeholder.jpg','og','now');
            """
        )
    with sqlite3.connect(archive) as conn:
        conn.executescript(
            """
            CREATE TABLE finnhub_news (
                symbol TEXT, news_id INTEGER, published_utc TEXT, headline TEXT,
                url TEXT, source TEXT, summary TEXT, raw_json TEXT
            );
            CREATE TABLE polygon_feed (
                session_date TEXT, news_id TEXT, published_utc TEXT, title TEXT,
                url TEXT, tickers_json TEXT, raw_json TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO finnhub_news VALUES (?,?,?,?,?,?,?,?)",
            (
                "ACME", 10, "2026-01-06T13:00:00Z", "ACME beats earnings estimates",
                "https://NEWS.example.com/earnings/?source=finnhub", "Finnhub",
                "Acme posts a quarterly earnings beat", json.dumps({"image": "https://img.example.com/finn.jpg"}),
            ),
        )
        conn.execute(
            "INSERT INTO polygon_feed VALUES (?,?,?,?,?,?,?)",
            (
                "2026-01-06", "p1", "2026-01-06T13:00:00Z", "ACME earnings beat",
                "https://news.example.com/earnings#top", '["ACME"]',
                json.dumps({"image_url": "https://img.example.com/polygon.jpg", "publisher": {"name": "Polygon Press"}}),
            ),
        )
        shared_image_rows = [
            (
                "OTHER", 1000 + index, "2026-01-06T18:00:00Z",
                f"Other company story {index}", f"https://other.example/{index}",
                "Other", None, json.dumps({"image": "https://img.example.com/shared.jpg"}),
            )
            for index in range(21)
        ]
        conn.executemany("INSERT INTO finnhub_news VALUES (?,?,?,?,?,?,?,?)", shared_image_rows)
        conn.execute(
            "INSERT INTO finnhub_news VALUES (?,?,?,?,?,?,?,?)",
            (
                "ACME", 15, "2026-01-06T17:00:00Z", "ACME shares rise in afternoon trade",
                "https://x.example/shared-image", "Finnhub", None,
                json.dumps({"image": "https://img.example.com/shared.jpg"}),
            ),
        )
        conn.executemany(
            "INSERT INTO finnhub_news VALUES (?,?,?,?,?,?,?,?)",
            [
                ("ACME", 11, "2026-01-05T21:00:00Z", "ACME boundary item", "https://x.example/boundary", "Finnhub", None, "{}"),
                ("ACME", 12, "2026-01-06T21:00:01Z", "ACME late item", "https://x.example/late", "Finnhub", None, "{}"),
                ("ACME", 13, "2026-01-06T14:30:00Z", "ACME receives FDA approval", "https://x.example/fda", "Finnhub", "ACME FDA approval", "{}"),
                ("ACME", 14, "2026-01-06T16:00:00Z", "ACME awarded a contract", "https://x.example/contract?from=finnhub", "Finnhub", None, json.dumps({"image": "https://img.example.com/finn-contract.jpg"})),
            ],
        )
        conn.execute(
            "INSERT INTO polygon_feed VALUES (?,?,?,?,?,?,?)",
            (
                "2026-01-06", "p2", "2026-01-06T16:00:00Z", "ACME awarded contract",
                "https://x.example/contract#details", '["ACME"]',
                json.dumps({"image_url": "https://img.example.com/polygon-contract.jpg"}),
            ),
        )
    return bodies, archive


def test_session_news_filters_window_deduplicates_and_prefers_body_media(tmp_path):
    bodies, archive = _stores(tmp_path)
    result = session_news("ACME", "2026-01-06", bodies_db=bodies, archive_db=archive, limit=30)

    assert result["window"] == {
        "information_window_start_at": "2026-01-05T21:00:00Z",
        "cutoff_at": "2026-01-06T21:00:00Z",
        "session_open_at": "2026-01-06T14:30:00Z",
    }
    assert [item["url"] for item in result["items"]] == [
        "https://news.example.com/earnings/",
        "https://x.example/fda",
        "https://news.example.com/product",
        "https://x.example/contract#details",
        "https://x.example/shared-image",
    ]
    earnings = result["items"][0]
    assert earnings["has_body"] is True
    assert earnings["image_url"] == "https://img.example.com/og.jpg"
    assert earnings["image_source"] == "og"
    assert earnings["phase"] == "PRE_OPEN"
    assert earnings["category"] is None  # nothing classified yet in this fixture
    assert result["items"][1]["phase"] == "SESSION"
    assert result["items"][2]["image_url"] is None
    contract = result["items"][3]
    assert contract["url"] == "https://x.example/contract#details"
    assert contract["image_url"] == "https://img.example.com/polygon-contract.jpg"
    assert contract["image_source"] == "polygon"
    assert contract["has_body"] is False
    assert result["items"][4]["image_url"] is None
    assert result["counts_by_category"]["UNCATEGORIZED"] == len(result["items"])


def _add_title_dedup_pair(
    bodies,
    archive,
    *,
    feed_title,
    feed_time="2026-01-06T13:00:00Z",
    title="ACME announces a strategic partnership with Northstar Holdings",
):
    with sqlite3.connect(bodies) as conn:
        conn.execute(
            "INSERT INTO web_body_chunks VALUES (?,?,?,?,?,?,?,?)",
            ("web:dedup", 1, "web:dedup:chunk", "ACME announces the partnership.",
             "https://canonical.example/article", title, "Real Publisher", "2026-01-06T13:00:00Z"),
        )
        conn.execute("INSERT INTO web_headlines VALUES (?,?,?)", ("web:dedup", "ACME", "2026-01-06T13:00:00Z"))
    with sqlite3.connect(archive) as conn:
        conn.execute(
            "INSERT INTO finnhub_news VALUES (?,?,?,?,?,?,?,?)",
            ("ACME", 999, feed_time, feed_title,
             "https://finnhub.io/api/news?id=999", "Yahoo", None, "{}"),
        )
    return title


def test_session_news_merges_same_title_and_time_across_body_and_finnhub(tmp_path):
    bodies, archive = _stores(tmp_path)
    title = _add_title_dedup_pair(
        bodies, archive,
        feed_title="ACME announces a strategic partnership with Northstar Holdings",
    )

    result = session_news(
        "ACME", "2026-01-06", bodies_db=bodies, archive_db=archive,
        evidence_ids={"finnhub:ACME:999"}, limit=30,
    )
    matches = [item for item in result["items"] if item["title"] == title]
    assert len(matches) == 1
    assert matches[0]["has_body"] is True
    assert matches[0]["url"] == "https://canonical.example/article"
    assert matches[0]["publisher"] == "Real Publisher"
    assert matches[0]["used_as_evidence"] is True


@pytest.mark.parametrize(
    "feed_title",
    [
        "ACME announces a strategic partnership with Northstar Holdings - Acme (NASDAQ:ACME)",
        "ACME announces a strategic partnership with Northstar Holdings - Acme\u2026",
    ],
)
def test_session_news_merges_a_syndicated_copy_with_a_listing_suffix(tmp_path, feed_title):
    bodies, archive = _stores(tmp_path)
    title = _add_title_dedup_pair(bodies, archive, feed_title=feed_title)

    result = session_news("ACME", "2026-01-06", bodies_db=bodies, archive_db=archive, limit=30)
    titles = [item["title"] for item in result["items"]]
    assert titles.count(title) + titles.count(feed_title) == 1


def test_session_news_merges_a_long_headline_reposted_later_at_its_first_time(tmp_path):
    bodies, archive = _stores(tmp_path)
    title = _add_title_dedup_pair(
        bodies, archive,
        feed_title="ACME announces a strategic partnership with Northstar Holdings",
        feed_time="2026-01-06T13:20:00Z",
    )

    result = session_news("ACME", "2026-01-06", bodies_db=bodies, archive_db=archive, limit=30)
    matches = [item for item in result["items"] if item["title"] == title]
    assert len(matches) == 1
    assert matches[0]["published_at"].startswith("2026-01-06T13:00")


def test_session_news_does_not_merge_a_short_headline_ten_minutes_apart(tmp_path):
    bodies, archive = _stores(tmp_path)
    title = _add_title_dedup_pair(
        bodies, archive,
        title="ACME stock moves on the open today",
        feed_title="ACME stock moves on the open today",
        feed_time="2026-01-06T13:10:00Z",
    )

    result = session_news("ACME", "2026-01-06", bodies_db=bodies, archive_db=archive, limit=30)
    assert len([item for item in result["items"] if item["title"] == title]) == 2


def test_session_news_does_not_merge_different_titles_at_same_time(tmp_path):
    bodies, archive = _stores(tmp_path)
    title = _add_title_dedup_pair(
        bodies, archive,
        feed_title="ACME signs a separate and unrelated distribution agreement",
    )

    result = session_news("ACME", "2026-01-06", bodies_db=bodies, archive_db=archive, limit=30)
    assert len([item for item in result["items"] if item["title"] in {
        title, "ACME signs a separate and unrelated distribution agreement",
    }]) == 2


def test_session_news_merges_cached_headline_media_and_counts_uncached_images(tmp_path):
    bodies, archive = _stores(tmp_path)
    cache = tmp_path / "media.sqlite3"
    with sqlite3.connect(cache) as conn:
        conn.execute("""
            CREATE TABLE headline_media (
                article_key TEXT PRIMARY KEY, resolved_url TEXT, image_url TEXT,
                image_source TEXT, status TEXT, reason TEXT, attempts INTEGER,
                checked_at TEXT, next_attempt_at TEXT
            )
        """)
        conn.execute(
            "INSERT INTO headline_media VALUES (?,?,?,?,?,?,?,?,?)",
            ("https://x.example/fda", "https://x.example/fda",
             "https://img.example.com/fda-story.jpg", "og", "ok", None, 1, "now", None),
        )
    result = session_news("ACME", "2026-01-06", bodies_db=bodies, archive_db=archive,
                          media_db=cache, limit=30)
    fda = next(item for item in result["items"] if item["url"] == "https://x.example/fda")
    assert (fda["image_url"], fda["image_source"]) == ("https://img.example.com/fda-story.jpg", "og")
    assert result["images_pending"] == 1


def test_news_prefilters_polygon_and_reads_media_by_candidate_key(tmp_path, monkeypatch):
    from catalyst_data.ingestion import session_news as news

    bodies, archive = _stores(tmp_path)
    cache = tmp_path / "media.sqlite3"
    with sqlite3.connect(cache) as conn:
        conn.execute("CREATE TABLE headline_media (article_key TEXT PRIMARY KEY, image_url TEXT, image_source TEXT, status TEXT, attempts INTEGER)")
        conn.execute("INSERT INTO headline_media VALUES (?,?,?,?,?)", ("https://unrelated.example/", "x", "og", "ok", 1))

    statements = []
    original = news._read_only

    def traced(path):
        conn = original(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(news, "_read_only", traced)
    monkeypatch.setattr(news, "_shared_for", lambda *_: frozenset())
    result = news.session_news(
        "ACME", "2026-01-06", bodies_db=bodies, archive_db=archive,
        media_db=cache, shared_images_path=tmp_path / "missing.json",
    )

    assert len(result["items"]) == 5
    assert any("instr(tickers_json" in sql for sql in statements)
    media_queries = [sql for sql in statements if "FROM headline_media WHERE article_key IN" in sql]
    assert len(media_queries) == 1
    assert "unrelated.example" not in media_queries[0]


def test_session_news_paginates_and_marks_evidence(tmp_path):
    bodies, archive = _stores(tmp_path)
    first = session_news(
        "ACME", "2026-01-06", bodies_db=bodies, archive_db=archive, limit=1,
        evidence_ids={"web:body1:web_body:0001"},
    )
    second = session_news(
        "ACME", "2026-01-06", bodies_db=bodies, archive_db=archive, limit=1,
        cursor=first["next_cursor"],
    )

    assert first["items"][0]["used_as_evidence"] is True
    assert first["next_cursor"]
    assert second["items"][0]["id"] != first["items"][0]["id"]
    assert second["next_cursor"]


def test_session_news_rejects_non_trading_day(tmp_path):
    bodies, archive = _stores(tmp_path)
    try:
        session_news("ACME", "2026-01-10", bodies_db=bodies, archive_db=archive)
    except ValueError as exc:
        assert "not_a_trading_session" in str(exc)
    else:
        raise AssertionError("expected a non-trading day to be rejected")


def test_session_news_uses_early_close_cutoff(tmp_path):
    bodies, archive = _stores(tmp_path)
    result = session_news("ACME", "2026-11-27", bodies_db=bodies, archive_db=archive)
    assert result["window"]["cutoff_at"] == "2026-11-27T18:00:00Z"


def test_session_news_reads_cached_model_categories(tmp_path):
    from catalyst_data.ingestion import categorize_llm

    bodies, archive = _stores(tmp_path)

    class Fake:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    import json as _j
                    from types import SimpleNamespace
                    n = kw["messages"][0]["content"].split("ITEMS:")[1].count(" || ")
                    content = _j.dumps({"labels": {str(i): "EARNINGS" for i in range(n)}})
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    cats = tmp_path / "cats.sqlite3"
    first = session_news("ACME", "2026-01-06", bodies_db=bodies, archive_db=archive, categories_db=cats)
    keys = [{"key": __import__("catalyst_data.ingestion.session_news", fromlist=["canonical_url"]).canonical_url(i["url"]),
             "title": i["title"], "summary": i["summary"]} for i in first["items"]]
    labels = categorize_llm.classify(keys, "k", db_path=cats, client=Fake())
    assert set(labels.values()) == {"EARNINGS"}
    again = session_news("ACME", "2026-01-06", bodies_db=bodies, archive_db=archive, categories_db=cats)
    assert all(i["category"] == "EARNINGS" and i["category_source"] == "llm" for i in again["items"])
    assert again["counts_by_category"]["EARNINGS"] == len(again["items"])
    # cached: a second classify makes no model call
    assert categorize_llm.classify(keys, "k", db_path=cats, client=None) == labels


def test_canonical_url_keeps_identifying_query_and_drops_tracking():
    from catalyst_data.ingestion.session_news import canonical_url

    a = canonical_url("https://finnhub.io/api/news?id=111")
    b = canonical_url("https://finnhub.io/api/news?id=222")
    assert a != b
    assert canonical_url("https://X.com/a/?utm_source=rss&utm_medium=x#top") == "https://x.com/a"
    assert canonical_url("https://x.com/a?b=2&a=1") == canonical_url("https://x.com/a?a=1&b=2")


def test_session_news_reads_skewed_finnhub_publishers_as_new_york_clock(tmp_path):
    bodies, archive = _stores(tmp_path)
    with sqlite3.connect(archive) as conn:
        conn.executemany(
            "INSERT INTO finnhub_news VALUES (?,?,?,?,?,?,?,?)",
            [
                # Benzinga's 10:00 New York (EST) is stored as 10:00Z; the real instant is 15:00Z, in session.
                ("ACME", 20, "2026-01-06T10:00:00Z", "ACME upgraded at Example Bank", "https://bz.example/upgrade",
                 "Benzinga", None, "{}"),
                # Stored before the window start, but really 17:30 New York on the previous day: inside the window.
                ("ACME", 21, "2026-01-05T17:30:00Z", "ACME after-hours note", "https://cnbc.example/note",
                 "CNBC", None, "{}"),
            ],
        )
    result = session_news("ACME", "2026-01-06", bodies_db=bodies, archive_db=archive,
                          media_db=tmp_path / "missing-media.sqlite3")
    by_title = {item["title"]: item for item in result["items"]}
    assert by_title["ACME upgraded at Example Bank"]["published_at"] == "2026-01-06T15:00:00Z"
    assert by_title["ACME upgraded at Example Bank"]["phase"] == "SESSION"
    assert by_title["ACME after-hours note"]["published_at"] == "2026-01-05T22:30:00Z"
    assert by_title["ACME after-hours note"]["phase"] == "PRE_OPEN"


def test_session_news_normalizes_display_title_and_summary_only(tmp_path):
    from catalyst_data.ingestion.session_news import category_inputs

    bodies, archive = _stores(tmp_path)
    with sqlite3.connect(archive) as conn:
        conn.execute(
            "INSERT INTO finnhub_news VALUES (?,?,?,?,?,?,?,?)",
            ("ACME", 30, "2026-01-06T14:00:00Z", "ACME M&amp;A update",
             "https://news.example.com/acme-ma", "Benzinga", "Wall Street&#39;s ACME update", "{}"),
        )

    result = session_news("ACME", "2026-01-06", bodies_db=bodies, archive_db=archive,
                          media_db=tmp_path / "missing-media.sqlite3")
    item = next(item for item in result["items"] if item["url"].endswith("/acme-ma"))
    assert item["title"] == "ACME M&A update"
    assert item["summary"] == "Wall Street's ACME update"
    inputs = category_inputs("ACME", "2026-01-06", bodies_db=bodies, archive_db=archive)
    raw_input = next(item for item in inputs if item["key"].endswith("/acme-ma"))
    assert raw_input["title"] == "ACME M&amp;A update"
    assert raw_input["summary"] == "Wall Street&#39;s ACME update"

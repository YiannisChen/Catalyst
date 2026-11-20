from __future__ import annotations

import json
from datetime import datetime, timezone

from catalyst_data.ingestion.collectors import web_discovery_helpers
from catalyst_data.ingestion.collectors import web_discovery_v2 as v2


class _Fetcher:
    def __init__(self, pages=None):
        self.pages = pages or {}
        self.calls = []

    def fetch_page(self, url):
        self.calls.append(url)
        return self.pages.get(url)


def _html(title, published="2026-03-06T12:00:00Z", body=None):
    body = body or ("The company shares moved after a report described the event. " * 12)
    date_json = f',"datePublished":"{published}"' if published else ""
    return ("<html><head><title>" + title + "</title><script type='application/ld+json'>"
            + '{"@type":"NewsArticle","headline":' + json.dumps(title) + date_json
            + "}</script></head><body><article><p>" + body + "</p></article></body></html>")


def _case(ticker="TSLA", tier="T3", case_key=None, **extra):
    return {"case_key": case_key or ticker, "ticker": ticker, "session_date": "2026-03-06",
            "return_pct": 4.2, "spy_pct": 0.8, "sector_etf": "XLK", "tier": tier, **extra}


def _company(monkeypatch):
    monkeypatch.setattr(web_discovery_helpers, "_company", lambda ticker: {
        "TSLA": "Tesla, Inc.", "NVDA": "NVIDIA Corporation", "AAPL": "Apple Inc."
    }.get(ticker, ticker))


def _daily_paths(tmp_path, monkeypatch):

    from catalyst_data.ingestion.ops import daily

    peer_map = tmp_path / "peer-map.json"
    peer_map.write_text("{}")
    paths = daily.Paths(
        market_db=tmp_path / "market.sqlite3", bodies_db=tmp_path / "bodies.sqlite3",
        peer_map={}, snapshot_db=None, jobs_db=tmp_path / "jobs.sqlite3",
        archive_db=tmp_path / "archive.sqlite3", peer_map_path=peer_map,
    )
    monkeypatch.setattr(daily.Paths, "from_env", classmethod(lambda cls: paths))
    for name in ("CATALYST_MARKET_CONTEXT_DB", "CATALYST_NEWS_BODIES_DB", "CATALYST_PEER_MAP",
                 "CATALYST_UPDATE_JOBS_DB", "CATALYST_HEADLINE_ARCHIVE_DB", "CATALYST_SNAPSHOT_DB"):
        # Record every key so monkeypatch restores it after the adapter publishes paths.
        monkeypatch.setenv(name, "")
    return paths


def test_all_tiers_publishes_resolved_paths_for_legacy_helpers(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    paths = _daily_paths(tmp_path, monkeypatch)

    def fake_run(_command, **_kwargs):
        assert os.environ["CATALYST_MARKET_CONTEXT_DB"] == str(paths.market_db)
        assert os.environ["CATALYST_NEWS_BODIES_DB"] == str(paths.bodies_db)
        assert os.environ["CATALYST_PEER_MAP"] == str(paths.peer_map_path)
        return SimpleNamespace(stdout='[{"ticker":"AAA","tier":"T2"}]')

    monkeypatch.setattr(v2.subprocess, "run", fake_run)
    assert v2._all_tiers([{"ticker": "AAA", "session_date": "2026-03-06"}]) == {
        ("2026-03-06", "AAA"): "T2"
    }


def test_query_families_by_tier_use_natural_date(monkeypatch):
    _company(monkeypatch)
    t3 = v2.queries_for_case(_case())
    assert [q for _, _, q in t3] == [
        "Why did Tesla (TSLA) stock rise on March 6, 2026?",
        "Tesla shares jump March 6, 2026",
    ]
    t2 = v2.queries_for_case(_case(tier="T2"))
    assert {family for _, family, _ in t2} == {"issuer_primary", "sector", "market"}
    copper = v2.queries_for_case(_case("FCX", "T2"))
    assert any(family == "commodity" and "copper prices March 6, 2026" in query
               for _, family, query in copper)
    assert not v2.queries_for_case(_case(tier="T1"))


def test_tier_candidate_formatter_supports_all_tiers_without_subprocess():
    from catalyst_agents.runtime.web_tier_candidates import _candidate_row

    class Decision:
        tier = "T2"
        governing_ref = {"residual_pct": 2.5}

    class Profile:
        target_return = 4.0

    mover = {"ticker": "ABC"}
    assert _candidate_row(mover, Decision(), Profile(), all_tiers=True) == {
        "ticker": "ABC", "tier": "T2"
    }
    assert _candidate_row(mover, Decision(), Profile(), all_tiers=False) is None
    Decision.tier = "T3"
    assert _candidate_row(mover, Decision(), Profile(), all_tiers=False) == {
        "ticker": "ABC", "priority": 2.5
    }


def test_candidates_for_session_returns_every_tier_in_all_tiers_mode(monkeypatch, tmp_path):
    from catalyst_agents.attribution import observation_builder, tiers
    from catalyst_agents.runtime import context, web_tier_candidates
    from catalyst_data.canonical import temporal
    from catalyst_data.read import daily_universe

    class Profile:
        def __init__(self, ticker):
            self.ticker = ticker
            self.target_return = 1.0

    class Builder:
        def __init__(self, **kwargs):
            pass

        def build(self, *, ticker, **kwargs):
            return Profile(ticker)

    class Provider:
        def __init__(self, *_args):
            pass

        def issuer_filings_in_window(self, **kwargs):
            return 0

    class Decision:
        def __init__(self, tier):
            self.tier = tier
            self.governing_ref = None

    class Identity:
        session_date = "2026-03-06"
        cutoff_at = object()
        information_window_start_at = object()

    monkeypatch.setattr(observation_builder, "ObservationBuilder", Builder)
    monkeypatch.setattr(context, "SQLiteContextProvider", Provider)
    monkeypatch.setattr(tiers, "default_observation_policy", lambda: object())
    monkeypatch.setattr(tiers, "decide_tier", lambda profile, **kwargs:
                        Decision({"AAA": "T1", "BBB": "T2", "CCC": "T3"}[profile.ticker]))
    monkeypatch.setattr(temporal, "build_temporal_identity", lambda _session: Identity())
    monkeypatch.setattr(temporal, "utc_iso_z", lambda _value: "2026-03-06T00:00:00Z")
    monkeypatch.setattr(daily_universe, "movers_for", lambda *_args: [
        {"ticker": "AAA"}, {"ticker": "BBB"}, {"ticker": "CCC"}
    ])
    monkeypatch.setattr(daily_universe, "universe", lambda: [])
    monkeypatch.setenv("CATALYST_MARKET_CONTEXT_DB", str(tmp_path / "market.db"))
    monkeypatch.setenv("CATALYST_PEER_MAP", str(tmp_path / "peers.json"))

    kwargs = {"market_db": tmp_path / "market.db", "snapshot_db": None,
              "peer_map": tmp_path / "peers.json", "session_date": "2026-03-06"}
    assert web_tier_candidates.candidates_for_session(**kwargs, all_tiers=True) == [
        {"ticker": "AAA", "tier": "T1"}, {"ticker": "BBB", "tier": "T2"},
        {"ticker": "CCC", "tier": "T3"},
    ]
    assert web_tier_candidates.candidates_for_session(**kwargs) == [
        {"ticker": "CCC", "priority": 1.0}
    ]


def test_market_and_sector_queries_are_shared_once(tmp_path, monkeypatch):
    _company(monkeypatch)
    calls = []

    def search(query, key, **kwargs):
        calls.append(query)
        return {"results": []}

    a = _case("TSLA", "T2", "a")
    b = _case("NVDA", "T2", "b")
    first = v2.discover_v2(a, search_fn=search, fetcher=_Fetcher(), db_path=tmp_path / "x.db")
    second = v2.discover_v2(b, search_fn=search, fetcher=_Fetcher(), db_path=tmp_path / "x.db")
    assert first["credits_used"] == 4
    assert second["credits_used"] == 2
    assert sum(q.startswith("Stock market today") for q in calls) == 1
    assert sum(q.startswith("Why did technology stocks") for q in calls) == 1


def test_pass_two_runs_when_issuer_scope_has_no_relevant_result(tmp_path, monkeypatch):
    _company(monkeypatch)
    calls = []

    def search(query, key, **kwargs):
        calls.append((query, kwargs.get("include_domains")))
        return {"results": []}

    result = v2.discover_v2(_case(), search_fn=search, fetcher=_Fetcher(), db_path=tmp_path / "x.db")
    assert result["credits_used"] == 3
    assert calls[2][1] is None


def test_spam_group_and_issuer_classification(tmp_path, monkeypatch):
    _company(monkeypatch)
    spam = "https://example.com/spam"
    group = "https://example.com/market"
    unrelated = "https://example.com/unrelated"
    issuer = "https://example.com/issuer"
    urls = [spam, group, unrelated, issuer]

    def search(query, key, **kwargs):
        return {"results": [{"url": url, "title": title} for url, title in zip(urls, [
            "INVESTOR ALERT: Tesla class action deadline", "Technology stocks rally as yields fall",
            "Local weather update", "Tesla shares rise after delivery report",
        ])]}

    fetcher = _Fetcher({url: (url, _html(title)) for url, title in zip(urls, [
        "INVESTOR ALERT: Tesla class action deadline", "Technology stocks rally as yields fall",
        "Local weather update", "Tesla shares rise after delivery report",
    ])})
    v2.discover_v2(_case(), search_fn=search, fetcher=fetcher, db_path=tmp_path / "x.db")
    rows = v2.rows_for_case("TSLA", db_path=tmp_path / "x.db")
    statuses = {row["url"]: row["status"] for row in rows}
    assert statuses[spam] == "SPAM"
    assert statuses[group] == "IRRELEVANT"  # group wraps do not qualify for issuer scope
    assert statuses[unrelated] == "IRRELEVANT"
    assert statuses[issuer] == "FULL_TEXT"


def test_group_wrap_is_kept_only_for_t2_shared_scope(tmp_path, monkeypatch):
    _company(monkeypatch)
    url = "https://example.com/tech"
    title = "Technology stocks rally as yields fall"
    body = "Technology shares gained as lower yields lifted growth stocks. " * 12

    def search(query, key, **kwargs):
        return {"results": [{"url": url, "title": title}]}

    pages = {url: (url, _html(title, body=body))}
    v2.discover_v2(_case(tier="T2", case_key="t2"), search_fn=search, fetcher=_Fetcher(pages),
                   db_path=tmp_path / "x.db")
    v2.discover_v2(_case(tier="T3", case_key="t3"), search_fn=search, fetcher=_Fetcher(pages),
                   db_path=tmp_path / "x.db")
    t2 = v2.rows_for_case("t2", db_path=tmp_path / "x.db")
    t3 = v2.rows_for_case("t3", db_path=tmp_path / "x.db")
    assert any(row["status"] == "FULL_TEXT" and row["scope"] == "group" for row in t2)
    assert all(row["status"] == "IRRELEVANT" for row in t3)


def test_date_fallback_order_and_out_of_window(tmp_path, monkeypatch):
    _company(monkeypatch)
    urls = ["https://example.com/2026/03/06/page", "https://example.com/2026-03-06/story",
            "https://example.com/provider-only", "https://example.com/2026/04/01/old",
            "https://example.com/2026/03/06/page-owned"]
    titles = ["Tesla shares rise"] * 5
    results = [{"url": urls[0], "title": titles[0], "published_date": "2026-03-10"},
               {"url": urls[1], "title": titles[1], "published_date": "2026-03-10"},
               {"url": urls[2], "title": titles[2], "published_date": "2026-03-06"},
               {"url": urls[3], "title": titles[3], "published_date": "2026-03-06"},
               {"url": urls[4], "title": titles[4], "published_date": "2026-03-06"}]
    pages = {url: (url, _html(title, published="2026-03-10T12:00:00Z" if url == urls[4] else None))
             for url, title in zip(urls, titles)}
    v2.discover_v2(_case(), search_fn=lambda *a, **k: {"results": results}, fetcher=_Fetcher(pages),
                   db_path=tmp_path / "x.db", now=datetime(2026, 3, 20, tzinfo=timezone.utc))
    rows = {row["url"]: row for row in v2.rows_for_case("TSLA", db_path=tmp_path / "x.db")}
    assert rows[urls[0]]["date_source"] == "url"
    assert rows[urls[1]]["date_source"] == "url"
    assert rows[urls[2]]["date_source"] == "provider"
    assert rows[urls[3]]["status"] == "OUT_OF_WINDOW"
    assert rows[urls[4]]["date_source"] == "page"


def test_undated_and_raw_only_without_dateline(tmp_path, monkeypatch):
    _company(monkeypatch)
    url = "https://example.com/undated"
    v2.discover_v2(_case(), search_fn=lambda *a, **k: {"results": [
        {"url": url, "title": "Tesla shares rise", "raw_content": "Tesla shares rose. " * 80}
    ]}, fetcher=_Fetcher(), db_path=tmp_path / "x.db")
    row = v2.rows_for_case("TSLA", db_path=tmp_path / "x.db")[0]
    assert row["status"] == "UNDATED"


def test_rows_for_case_unions_shared_rows_and_sorts_relevance(tmp_path, monkeypatch):
    _company(monkeypatch)
    v2.discover_v2(_case(tier="T2", case_key="case"), search_fn=lambda q, k, **kw: {"results": []},
                   fetcher=_Fetcher(), db_path=tmp_path / "x.db")
    conn = v2._connect(tmp_path / "x.db")
    conn.executemany("INSERT INTO web_discovery_v2_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("MARKET", "2026-03-06", "market", "u1", "u1", "p", "p", None, None,
         "t", "b", "fetch", None, "group", .9, "IRRELEVANT", "now"),
        ("TSLA", "2026-03-06", "issuer_primary", "u2", "u2", "p", "p", None, None,
         "t", "b", "fetch", None, "issuer", .2, "IRRELEVANT", "now"),
    ])
    conn.commit()
    conn.close()
    rows = v2.rows_for_case("case", db_path=tmp_path / "x.db")
    assert [row["scope_key"] for row in rows] == ["MARKET", "TSLA"]


def test_credit_cap_limits_queries_per_case(tmp_path, monkeypatch):
    _company(monkeypatch)
    calls = []
    result = v2.discover_v2(_case(tier="T2"), search_fn=lambda q, k, **kw: calls.append(q) or {"results": []},
                            fetcher=_Fetcher(), db_path=tmp_path / "x.db", max_credits=2)
    assert result["credits_used"] == 2
    assert len(calls) == 2


def test_sector_wrap_shared_row_reused_across_case_links(tmp_path, monkeypatch):
    _company(monkeypatch)
    # API lookup ordering is stable even when there are no fetched articles.
    v2.discover_v2(_case(tier="T2", case_key="a"), search_fn=lambda *a, **k: {"results": []},
                   fetcher=_Fetcher(), db_path=tmp_path / "x.db")
    v2.discover_v2(_case("NVDA", "T2", "b"), search_fn=lambda *a, **k: {"results": []},
                   fetcher=_Fetcher(), db_path=tmp_path / "x.db")
    conn = v2._connect(tmp_path / "x.db")
    links = conn.execute("SELECT COUNT(*) FROM web_discovery_v2_case_links WHERE scope_key='SECTOR:XLK'").fetchone()[0]
    conn.close()
    assert links == 2


def test_cached_rows_turn_date_only_publication_into_end_of_day(tmp_path):
    from catalyst_data.ingestion.collectors import web_discovery_v2 as v2

    db = tmp_path / "web.sqlite3"
    conn = v2._connect(db)
    conn.execute("INSERT INTO web_discovery_v2_case_links VALUES ('c1','ISRG','2026-05-11','ISRG','issuer_primary')")
    row = ("ISRG", "2026-05-11", "issuer_primary", "https://x.test/a", "https://x.test/a", "X", "x.test",
           "2026-05-11", "url", "ISRG falls", "Intuitive Surgical fell.", "fetch", "h", "issuer", 0.9,
           "FULL_TEXT", "2026-05-12T00:00:00Z")
    conn.execute("INSERT INTO web_discovery_v2_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    conn.commit()
    conn.close()
    rows = v2.cached_rows_for_ticker_session("ISRG", "2026-05-11", db_path=db)
    assert rows[0]["published_at"] == "2026-05-11T23:59:59Z"


def test_provider_stamp_keeps_time_of_day_only_when_present():
    from catalyst_data.ingestion.collectors import web_discovery_v2 as v2

    assert v2._provider_stamp("Mon, 11 May 2026 13:05:00 GMT") == "2026-05-11T13:05:00Z"
    assert v2._provider_stamp("2026-05-11") is None


def test_group_wraps_only_from_the_session_day_or_next_pre_open(tmp_path):
    from catalyst_data.ingestion.collectors import web_discovery_v2 as v2

    db = tmp_path / "web.sqlite3"
    conn = v2._connect(db)
    conn.execute("INSERT INTO web_discovery_v2_case_links VALUES ('c1','XOM','2026-04-17','COMMODITY:oil','commodity')")
    conn.execute("INSERT INTO web_discovery_v2_case_links VALUES ('c1','XOM','2026-04-17','XOM','issuer_primary')")
    def row(scope_key, family, url, published, scope):
        return (scope_key, "2026-04-17", family, url, url, "X", "x.test", published, "page", "t", "b", "fetch",
                "h", scope, 0.5, "FULL_TEXT", "2026-04-23T00:00:00Z")
    for r in (row("COMMODITY:oil", "commodity", "https://x.test/same", "2026-04-17T20:00:00Z", "group"),
              row("COMMODITY:oil", "commodity", "https://x.test/preopen", "2026-04-20T12:00:00Z", "group"),
              row("COMMODITY:oil", "commodity", "https://x.test/later", "2026-04-22T13:34:00Z", "group"),
              row("XOM", "issuer_primary", "https://x.test/recap", "2026-04-21T10:00:00Z", "issuer")):
        conn.execute("INSERT INTO web_discovery_v2_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
    conn.commit()
    conn.close()
    urls = {r["canonical_url"] for r in v2.cached_rows_for_ticker_session("XOM", "2026-04-17", db_path=db)}
    assert urls == {"https://x.test/same", "https://x.test/preopen", "https://x.test/recap"}


def test_company_names_are_clean_display_names():
    from catalyst_data.ingestion.collectors.web_discovery_v2 import _clean_name

    assert _clean_name("BOEING CO") == "Boeing"
    assert _clean_name("AMERICAN EXPRESS CO") == "American Express"
    assert _clean_name("JPMORGAN CHASE & CO") == "Jpmorgan Chase"
    assert _clean_name("AT&T INC.") == "AT&T"
    assert _clean_name("3M CO") == "3M"
    assert _clean_name("Tesla, Inc.") == "Tesla"
    assert _clean_name("The Goldman Sachs Group, Inc.") == "Goldman Sachs Group"

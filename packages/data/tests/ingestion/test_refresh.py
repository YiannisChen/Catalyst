from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from catalyst_data.ingestion.collectors import market
from catalyst_data.ingestion.ops.refresh import Deps, refresh_latest, refresh_session
from catalyst_data.ingestion.storage import store

SESSION = "2026-09-14"
NOW = datetime(2026, 9, 14, 21, tzinfo=timezone.utc)
PEER_MAP = {
    "benchmark": "SPY",
    "targets": {"ABC": {"sector_etf": "XLK", "direct_peers": ["PEER"]}},
}


class FakeResponse:
    status_code = 200
    url = "https://finance.yahoo.com/news/example"
    headers = {"content-type": "text/html"}
    text = "<html><article>" + "ABC quarterly update and operating results. " * 30 + "</article></html>"


class FakeFetcher:
    def __init__(self):
        self.pages = []
        self.gets = []

    def allowed(self, url):
        return True

    def get(self, url):
        self.gets.append(url)
        return FakeResponse()

    def fetch_page(self, url):
        self.pages.append(url)
        subject = url.rsplit("/", 1)[-1].split("-", 1)[0].upper()
        text = f"<html><article><p>{subject} quarterly update and operating results. " + "Results increased across all business operations. " * 30 + "</p></article></html>"
        return url, text


XML = """<urlset xmlns:news="news"><url><loc>https://www.kitco.com/news/example</loc>
<news:publication_date>2026-09-14T15:00:00Z</news:publication_date>
<news:title><![CDATA[Gold prices rise]]></news:title></url></urlset>"""


def _stores(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    snapshot = tmp_path / "snapshot.sqlite3"
    conn = sqlite3.connect(snapshot)
    conn.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, close REAL)")
    conn.commit()
    conn.close()
    market_db = tmp_path / "market.sqlite3"
    conn = market.open_market_writer(market_db)
    conn.close()
    bodies = tmp_path / "bodies.sqlite3"
    conn = store.open_writer(bodies)
    conn.close()
    return snapshot, market_db, bodies


def _deps(*, symbols=None, headline_mode="all", now=NOW, sic_error=None):
    fetcher = FakeFetcher()
    calls = {"bars": [], "sic": [], "select": [], "sec": [], "fetcher": fetcher}

    def load_bars(conn, requested, start, through, *, now):
        calls["bars"].append((list(requested), start, through))
        days = []
        current = date.fromisoformat(through) - timedelta(days=180)
        while current <= date.fromisoformat(through):
            if current.weekday() < 5:
                days.append(current.isoformat())
            current += timedelta(days=1)
        for symbol in requested:
            for i, day in enumerate(days):
                close = 100 + i * (0.02 if symbol == "SPY" else 0.03)
                conn.execute(
                    "INSERT OR REPLACE INTO ohlcv VALUES (?,?,?,?,?,?,?,?)",
                    (symbol, day, close, close, close, close, 1000, "fake"),
                )
        conn.commit()
        return {symbol: len(days) for symbol in requested}

    def load_sic(conn, tickers):
        calls["sic"].append(list(tickers))
        if sic_error:
            raise sic_error
        return {}

    def select(subject, start, cutoff, limit):
        calls["select"].append(subject)
        if headline_mode == "none":
            return [], "EMPTY"
        if headline_mode == "disabled":
            return [], "DISABLED"
        return ([{"url": f"https://finance.yahoo.com/news/{subject.lower()}",
                  "title": f"{subject} quarterly update", "published": "2026-09-14T15:00:00Z",
                  "ticker": subject}][:limit], "OK")

    def sec_intake(ticker, session, *, now):
        calls["sec"].append((ticker, session))
        return [], "EMPTY"

    return Deps(
        fetcher_factory=lambda: fetcher,
        select_with_status=select,
        load_bars=load_bars,
        load_sic=load_sic,
        sec_intake=sec_intake,
        kitco_sitemap=lambda _fetcher: XML,
        benzinga_discover=lambda *_args: ([], 0),
        now=now,
    ), calls


def _refresh(snapshot, market_db, bodies, deps, *, ticker="ABC", peer_map=PEER_MAP):
    return refresh_session(
        ticker, SESSION, snapshot_db=snapshot, market_db=market_db,
        bodies_db=bodies, peer_map=peer_map, deps=deps,
    )


def test_open_session_returns_without_calling_dependencies(tmp_path):
    snapshot, market_db, bodies = _stores(tmp_path)
    deps, calls = _deps(now=datetime(2026, 9, 14, 15, tzinfo=timezone.utc))
    report = _refresh(snapshot, market_db, bodies, deps)
    assert report.errors == ["SESSION_NOT_CLOSED"]
    assert report.after == report.before
    assert calls["bars"] == calls["select"] == []


def test_refresh_fills_mapped_target_bars_news_and_ledger(tmp_path):
    snapshot, market_db, bodies = _stores(tmp_path)
    deps, calls = _deps()
    report = _refresh(snapshot, market_db, bodies, deps)
    assert calls["bars"] == [(["ABC", "SPY", "XLK", "PEER"], "2026-05-17", SESSION)]
    assert report.after == []
    assert calls["sec"] == [("ABC", SESSION)]
    conn = sqlite3.connect(bodies)
    assert conn.execute(
        "SELECT status FROM intake_log WHERE subject='ABC' AND source='finnhub+polygon'"
    ).fetchone() == ("OK",)
    assert conn.execute("SELECT COUNT(*) FROM web_body_chunks").fetchone()[0] > 0
    conn.close()


def test_refresh_runs_sec_intake_and_sec_error_stays_missing(tmp_path):
    snapshot, market_db, bodies = _stores(tmp_path)
    deps, calls = _deps()
    row = {
        "asset_id": "web:secfixture", "text": "Filing body " * 40,
        "url": "https://www.sec.gov/Archives/edgar/data/1234/filing.htm",
        "title": "ABC Form 8-K EX-99.1", "publisher": "SEC filing",
        "source_class": "issuer_disclosure", "eligible_at": "2026-09-14T19:00:00Z",
        "fetched_at": "2026-09-14T21:00:00Z",
        "body_sha256": hashlib.sha256(("Filing body " * 40).encode()).hexdigest(),
        "tickers": ["ABC"],
    }

    def sec_ok(ticker, session, *, now):
        calls["sec"].append((ticker, session))
        return [row], "OK"

    deps.sec_intake = sec_ok
    report = _refresh(snapshot, market_db, bodies, deps)
    assert calls["sec"] == [("ABC", SESSION)]
    assert not any(item == "filings:target" for item in report.after)
    assert any(action["step"] == "sec" and action["status"] == "OK" for action in report.actions)

    snapshot, market_db, bodies = _stores(tmp_path / "error")
    deps, calls = _deps()

    def sec_error(ticker, session, *, now):
        calls["sec"].append((ticker, session))
        return [], "ERROR"

    deps.sec_intake = sec_error
    report = _refresh(snapshot, market_db, bodies, deps)
    assert calls["sec"] == [("ABC", SESSION)]
    assert "filings:target" in report.after
    conn = sqlite3.connect(bodies)
    assert conn.execute(
        "SELECT status FROM intake_log WHERE subject='ABC' AND source='sec'"
    ).fetchone() == ("ERROR",)
    conn.close()


@pytest.mark.parametrize(("mode", "expected"), [("none", "EMPTY"), ("disabled", "DISABLED")])
def test_empty_and_disabled_headline_results_are_recorded(tmp_path, mode, expected):
    snapshot, market_db, bodies = _stores(tmp_path)
    deps, _ = _deps(headline_mode=mode)
    report = _refresh(snapshot, market_db, bodies, deps)
    conn = sqlite3.connect(bodies)
    assert conn.execute(
        "SELECT status FROM intake_log WHERE subject='ABC' AND source='finnhub+polygon'"
    ).fetchone() == (expected,)
    assert "news:target" in report.after
    conn.close()


def test_market_aligned_spy_news_records_both_proxy_sources(tmp_path):
    snapshot, market_db, bodies = _stores(tmp_path)
    conn = sqlite3.connect(snapshot)
    _put_session_bars(conn, "ABC", SESSION, 2.0)
    conn.commit()
    conn.close()
    conn = sqlite3.connect(market_db)
    _put_session_bars(conn, "SPY", SESSION, 2.0)
    conn.commit()
    conn.close()
    deps, _ = _deps()
    report = _refresh(snapshot, market_db, bodies, deps)
    spy_actions = [a for a in report.actions if a["subject"] == "SPY"]
    assert [a["step"] for a in spy_actions] == ["finnhub", "kitco"]
    conn = sqlite3.connect(bodies)
    assert conn.execute(
        "SELECT source FROM intake_log WHERE subject='SPY' ORDER BY source"
    ).fetchall() == [("finnhub",), ("kitco",)]
    conn.close()


def _put_session_bars(conn, symbol, session, move):
    start = date.fromisoformat(session) - timedelta(days=35)
    rows = []
    close = 100.0
    current = start
    while current <= date.fromisoformat(session):
        if current.weekday() < 5:
            rows.append((symbol, current.isoformat(), close))
            close *= 1 + move / 100
        current += timedelta(days=1)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ohlcv)")}
    if "source" in columns:
        conn.executemany(
            "INSERT INTO ohlcv (symbol,date,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,?,?)",
            [(sym, day, close, close, close, close, 1000, "fake") for sym, day, close in rows],
        )
    else:
        conn.executemany("INSERT INTO ohlcv (symbol,date,close) VALUES (?,?,?)", rows)


def test_busy_bodies_lock_returns_refresh_busy(tmp_path):
    snapshot, market_db, bodies = _stores(tmp_path)
    deps, calls = _deps()
    with store.refresh_lock(bodies):
        report = _refresh(snapshot, market_db, bodies, deps)
    assert report.errors == ["REFRESH_BUSY"]
    assert report.after == report.before
    assert calls["select"] == []


def test_latest_skips_complete_sessions_and_only_uses_closed_days(monkeypatch, tmp_path):
    now = datetime(2026, 10, 6, 19, tzinfo=timezone.utc)  # Tuesday before close
    snapshot, market_db, bodies = _stores(tmp_path)
    deps, _ = _deps(now=now)
    checked, refreshed = [], []

    class Status:
        def __init__(self, missing):
            self.missing = missing

    def status_fn(ticker, session, **kwargs):
        checked.append(session)
        return Status(["news:target"] if session == "2026-10-05" else [])

    def refresh_fn(ticker, session, **kwargs):
        refreshed.append(session)
        return object()

    monkeypatch.setattr("catalyst_data.ingestion.ops.refresh.status.session_status", status_fn)
    monkeypatch.setattr("catalyst_data.ingestion.ops.refresh.refresh_session", refresh_fn)
    reports = refresh_latest(
        ["ABC"], since="2026-10-02", max_sessions=20, snapshot_db=snapshot,
        market_db=market_db, bodies_db=bodies, peer_map=PEER_MAP, deps=deps,
    )
    assert checked == ["2026-10-05", "2026-10-02"]
    assert refreshed == ["2026-10-05"]
    assert reports == [reports[0]]


def test_cli_status_prints_json_and_requires_store_env(tmp_path, monkeypatch, capsys):
    from catalyst_data.ingestion.ops import refresh

    snapshot, market_db, bodies = _stores(tmp_path)
    peer_path = tmp_path / "peer-map.json"
    peer_path.write_text(json.dumps(PEER_MAP))
    monkeypatch.setenv("CATALYST_MARKET_CONTEXT_DB", str(market_db))
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(bodies))
    monkeypatch.setenv("CATALYST_PEER_MAP", str(peer_path))
    monkeypatch.setattr(refresh.market, "last_closed_session", lambda now=None: SESSION)
    assert refresh.main(["status", "--ticker", "ABC", "--date", SESSION,
                         "--snapshot", str(snapshot)]) == 0
    assert '"missing"' in capsys.readouterr().out


def test_parallel_network_work_writes_the_same_ledger_and_actions_as_one_worker(tmp_path, monkeypatch):
    from catalyst_data.ingestion.ops import refresh as R

    def run(workers, sub):
        monkeypatch.setattr(R, "SUBJECT_WORKERS", workers)
        d = tmp_path / sub
        d.mkdir()
        snapshot, market_db, bodies = _stores(d)
        deps, _ = _deps()
        report = _refresh(snapshot, market_db, bodies, deps)
        conn = sqlite3.connect(bodies)
        ledger_rows = conn.execute(
            "SELECT subject, source, status, headlines, bodies FROM intake_log ORDER BY rowid").fetchall()
        assets = conn.execute("SELECT asset_id, body_sha256 FROM web_body_chunks ORDER BY asset_id, ordinal").fetchall()
        conn.close()
        return [(a["step"], a["subject"], a["status"], a["headlines"], a["bodies"]) for a in report.actions], ledger_rows, assets, report.after

    assert run(1, "seq") == run(5, "par")


def test_a_selector_bound_to_its_thread_runs_on_the_calling_thread(tmp_path):
    import threading
    from dataclasses import replace
    snapshot, market_db, bodies = _stores(tmp_path)
    deps, _ = _deps()
    owner = threading.get_ident()
    inner = deps.select_with_status

    def select(*a):
        assert threading.get_ident() == owner, "selector ran on a worker thread"
        return inner(*a)
    report = _refresh(snapshot, market_db, bodies, replace(deps, select_with_status=select, select_in_workers=False))
    assert report.after == [] and not any("worker thread" in e for e in report.errors)


def test_the_daily_archived_selector_works_through_refresh(tmp_path, monkeypatch):
    """Regression for 2026-10-21: the daily job's selector reads a SQLite connection opened on this thread."""
    from dataclasses import replace

    from catalyst_data.ingestion.collectors import headline_archive
    from catalyst_data.ingestion.ops import daily
    snapshot, market_db, bodies = _stores(tmp_path)
    deps, _ = _deps()
    archive = headline_archive.open_store(tmp_path / "archive.sqlite3")  # opened on this (the main) thread
    feed = daily.PolygonFeed(archive)
    monkeypatch.setattr(daily, "archive_covers", lambda *a: True)
    monkeypatch.setattr(feed, "items", lambda ticker, session: ([], "EMPTY"))
    selector = daily.archived_selector(archive, feed, SESSION)
    for in_workers, expect_thread_error in ((True, True), (False, False)):
        d = tmp_path / str(in_workers)
        d.mkdir()
        snapshot, market_db, bodies = _stores(d)
        report = _refresh(snapshot, market_db, bodies,
                          replace(deps, select_with_status=selector, select_in_workers=in_workers))
        assert any("same thread" in e for e in report.errors) is expect_thread_error
    rt = daily.Runtime(paths=None, job_sessions=[SESSION])
    rt.archive_conn, rt.feed = archive, feed
    assert daily._deps_for(rt, SESSION).select_in_workers is False

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from catalyst_data.ingestion.ledger import ensure_ledger, record_attempt
from catalyst_data.ingestion.ops.status import _has_sec_filing, session_status
from catalyst_data.ingestion.storage.store import open_writer, write_assets

SESSION = "2026-09-14"
NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
PEER_MAP = {
    "benchmark": "SPY",
    "targets": {
        "ABC": {
            "sector_etf": "XLK",
            "direct_peers": ["PEER"],
            "commodity_proxy": "GLD",
        }
    },
}


def _add_bars(conn: sqlite3.Connection, symbol: str, *, prior: int = 21, move: float = 1.0) -> None:
    start = date.fromisoformat(SESSION) - timedelta(days=prior)
    rows = []
    close = 100.0
    for i in range(prior + 1):
        day = (start + timedelta(days=i)).isoformat()
        rows.append((symbol, day, close))
        close *= 1.0 + move / 100.0
    conn.executemany("INSERT INTO ohlcv (symbol, date, close) VALUES (?, ?, ?)", rows)


def _stores(tmp_path, *, target_prior: int = 21, spy_move: float = 1.5, include_target: bool = True):
    snapshot_path = tmp_path / "snapshot.sqlite3"
    snapshot = sqlite3.connect(snapshot_path)
    snapshot.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, close REAL)")
    if include_target:
        _add_bars(snapshot, "ABC", prior=target_prior, move=2.0)
    snapshot.commit()
    snapshot.close()

    market_path = tmp_path / "market.sqlite3"
    market = sqlite3.connect(market_path)
    market.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, close REAL)")
    market.execute("CREATE TABLE issuer_sic (ticker TEXT, sic TEXT)")
    for symbol, move in (("SPY", spy_move), ("XLK", 1.0), ("GLD", 1.1), ("PEER", 0.8)):
        _add_bars(market, symbol, move=move)
    market.execute("INSERT INTO issuer_sic VALUES ('ABC', '3571')")
    market.commit()
    market.close()
    return snapshot_path, market_path


def _body_store(tmp_path, monkeypatch, bodies=()):
    path = tmp_path / "bodies.sqlite3"
    conn = open_writer(path)
    write_assets(
        conn,
        [
            {
                "asset_id": f"asset-{i}",
                "text": f"Body for {ticker}.",
                "url": "https://finance.yahoo.com/x",
                "title": title,
                "source_class": "NEWS",
                "eligible_at": eligible_at,
                "fetched_at": "2026-09-14T20:00:00Z",
                "body_sha256": hashlib.sha256(f"Body for {ticker}.".encode()).hexdigest(),
                "tickers": [ticker],
            }
            for i, (ticker, title, eligible_at) in enumerate(bodies)
        ],
    )
    ensure_ledger(conn)
    conn.close()
    (tmp_path / "sec_company_tickers.json").write_text(
        json.dumps({"0": {"ticker": "ABC", "title": "Example Corp."}})
    )
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(path))
    return path


def _status(snapshot, market, bodies, *, peer_map=PEER_MAP, ticker="ABC", now=NOW):
    return session_status(
        ticker,
        SESSION,
        snapshot_db=snapshot,
        market_db=market,
        bodies_db=bodies,
        peer_map=peer_map,
        now=now,
    )


def _asset(ticker: str, title: str, eligible_at: str = "2026-09-14T15:00:00Z"):
    return ticker, title, eligible_at


def test_open_session_returns_only_session_missing(tmp_path, monkeypatch):
    snapshot, market = _stores(tmp_path)
    bodies = _body_store(tmp_path, monkeypatch)
    status = _status(
        snapshot,
        market,
        bodies,
        now=datetime(2026, 9, 14, 15, tzinfo=timezone.utc),
    )
    assert [(item.name, item.detail) for item in status.items] == [
        ("session", "SESSION_NOT_CLOSED")
    ]


def test_mapped_target_bodies_and_aligned_market_body_are_present(tmp_path, monkeypatch):
    snapshot, market = _stores(tmp_path)
    bodies = _body_store(
        tmp_path,
        monkeypatch,
        [
            _asset("ABC", "ABC quarterly update"),
            _asset("PEER", "PEER quarterly update"),
            _asset("GLD", "Gold prices rise"),
            _asset("SPY", "US stocks rally"),
        ],
    )
    status = _status(snapshot, market, bodies)
    # Session discovery has not run for this session yet.
    assert status.missing == ["filings:target", "news:discovery"]
    assert status.scope == {
        "benchmark": "SPY",
        "sector": "XLK",
        "peers": ["PEER"],
        "proxy": "GLD",
        "target_return": pytest.approx(2.0),
        "market_return": pytest.approx(1.5),
        "proxy_return": pytest.approx(1.1),
        "news_peers": ["PEER"],
    }


def test_opposite_market_sign_does_not_add_market_news_item(tmp_path, monkeypatch):
    snapshot, market = _stores(tmp_path, spy_move=-1.5)
    bodies = _body_store(tmp_path, monkeypatch, [_asset("ABC", "ABC quarterly update")])
    status = _status(snapshot, market, bodies)
    assert "news:market" not in [item.name for item in status.items]


def test_target_body_after_cutoff_is_missing(tmp_path, monkeypatch):
    snapshot, market = _stores(tmp_path)
    bodies = _body_store(
        tmp_path, monkeypatch, [_asset("ABC", "ABC quarterly update", "2026-09-14T21:00:00Z")]
    )
    status = _status(snapshot, market, bodies)
    assert "news:target" in status.missing


def test_old_empty_target_ledger_counts_but_proxy_requires_both_sources(tmp_path, monkeypatch):
    snapshot, market = _stores(tmp_path)
    bodies = _body_store(tmp_path, monkeypatch)
    conn = open_writer(bodies)
    for subject, source in (("ABC", "finnhub+polygon"), ("GLD", "kitco")):
        record_attempt(
            conn,
            subject=subject,
            session_date=SESSION,
            source=source,
            status="EMPTY",
            headlines=0,
            bodies=0,
            attempted_at="2026-09-17T12:00:00Z",
        )
    conn.close()
    status = _status(snapshot, market, bodies)
    assert "news:target" not in status.missing
    assert "news:proxy:GLD" in status.missing


def test_sec_error_attempt_keeps_filings_missing(tmp_path, monkeypatch):
    snapshot, market = _stores(tmp_path)
    bodies = _body_store(tmp_path, monkeypatch)
    conn = open_writer(bodies)
    record_attempt(
        conn, subject="ABC", session_date=SESSION, source="sec", status="ERROR",
        headlines=0, bodies=0, attempted_at="2026-09-14T21:00:00Z",
    )
    conn.close()
    status = _status(snapshot, market, bodies)
    assert "filings:target" in status.missing


def test_sec_empty_attempt_after_cutoff_completes_filings_item(tmp_path, monkeypatch):
    snapshot, market = _stores(tmp_path)
    bodies = _body_store(tmp_path, monkeypatch)
    conn = open_writer(bodies)
    record_attempt(
        conn, subject="ABC", session_date=SESSION, source="sec", status="EMPTY",
        headlines=0, bodies=0, attempted_at="2026-09-14T21:00:00Z",
    )
    conn.close()
    status = _status(snapshot, market, bodies)
    assert "filings:target" not in status.missing


def test_sec_latest_error_reopens_status_after_prior_empty(tmp_path, monkeypatch):
    bodies = _body_store(tmp_path, monkeypatch)
    conn = open_writer(bodies)
    record_attempt(
        conn, subject="ABC", session_date=SESSION, source="sec", status="EMPTY",
        headlines=0, bodies=0, attempted_at="2026-09-14T21:00:00Z",
    )
    record_attempt(
        conn, subject="ABC", session_date=SESSION, source="sec", status="ERROR",
        headlines=0, bodies=0, attempted_at="2026-09-15T12:00:00Z",
    )
    start = "2026-09-11T20:00:00Z"
    cutoff = "2026-09-14T20:00:00Z"

    assert not _has_sec_filing(
        conn, ticker="ABC", session_date=SESSION, start=start, cutoff=cutoff
    )
    conn.close()


def test_sec_body_in_window_completes_filings_item(tmp_path, monkeypatch):
    snapshot, market = _stores(tmp_path)
    bodies = _body_store(tmp_path, monkeypatch)
    conn = open_writer(bodies)
    text = "SEC filing body with substantive issuer disclosure information. " * 8
    write_assets(conn, [{
        "asset_id": "web:sec-body", "text": text,
        "url": "https://www.sec.gov/Archives/edgar/data/1234/filing.htm",
        "title": "Example Corp Form 8-K EX-99.1", "publisher": "SEC filing",
        "source_class": "issuer_disclosure", "eligible_at": "2026-09-14T19:00:00Z",
        "fetched_at": "2026-09-14T21:00:00Z",
        "body_sha256": hashlib.sha256(text.encode()).hexdigest(), "tickers": ["ABC"],
    }])
    conn.close()
    status = _status(snapshot, market, bodies)
    assert "filings:target" not in status.missing


def test_target_with_only_ten_prior_bars_is_missing_target_bars(tmp_path, monkeypatch):
    snapshot, market = _stores(tmp_path, target_prior=10)
    bodies = _body_store(tmp_path, monkeypatch)
    status = _status(snapshot, market, bodies)
    assert "bars:target" in status.missing


def test_no_body_store_marks_news_missing_with_detail(tmp_path):
    snapshot, market = _stores(tmp_path)
    status = session_status(
        "ABC", SESSION, snapshot_db=snapshot, market_db=market, bodies_db=None,
        peer_map=PEER_MAP, now=NOW,
    )
    news = [item for item in status.items if item.kind == "news"]
    assert news
    assert all(not item.present and item.detail == "NO_BODY_STORE" for item in news)

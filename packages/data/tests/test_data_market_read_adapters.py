"""Offline contract tests for manifest-pinned market-bar adapters.

Legacy bar schemas have session/event time and fetch bookkeeping, but no
content-bound response observation. These fixtures intentionally exercise
that boundary without a live provider or a real database.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

import pytest
from catalyst_data.ingestion.collectors import market
from catalyst_data.storage.read_adapters import read_store_facts
from catalyst_data.storage.snapshots import load_verified_stores

INVOCATION = ["python", "-m", "catalyst_data.query_service"]
WINDOW_START = "2026-10-20T13:30:00Z"
WINDOW_END = "2026-10-20T14:30:00Z"
CUTOFF = WINDOW_END
DAILY_START = "2026-10-20T00:00:00Z"
DAILY_END = "2026-10-21T00:00:00Z"
DAILY_CUTOFF = DAILY_END


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _registry(path, *, sources=None, stores=None):
    path.write_text(json.dumps({
        "version": 1,
        "stores": stores or {
            "market_archive": "/opt/catalyst/archive/market_archive.sqlite3",
            "daily": "/opt/catalyst/repo/data/m9/market.sqlite3",
        },
        "sources": sources or [
            {"id": "market_archive:minute", "store": "market_archive", "category": "market"},
            {"id": "market_archive:research", "store": "market_archive", "category": "research"},
            {"id": "market_archive:events", "store": "market_archive", "category": "calendar"},
        ],
    }, sort_keys=True))
    return path


def _verified(tmp_path, stores, *, sources=None, registry_stores=None):
    registry = _registry(tmp_path / "sources.json", sources=sources, stores=registry_stores)
    files = [*stores.values(), registry]
    manifest = tmp_path / "inputs.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "invocation": INVOCATION,
        "files": [{"path": str(path), "sha256": _sha(path)} for path in files],
        "environment": {},
    }, sort_keys=True))
    verified = load_verified_stores(
        manifest, invocation=INVOCATION, required_stores=stores, environ={},
    )
    return verified, registry


def _daily_store(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(market.SCHEMA)
        conn.executemany(
            "INSERT INTO ohlcv VALUES (?,?,?,?,?,?,?,?)",
            [
                ("ABC", "2026-10-20", 10, 12, 9, 11, 100, "yfinance"),
                ("XYZ", "2026-10-20", 20, 22, 19, 21, 200, "yfinance"),
            ],
        )


def _minute_store(path):
    with sqlite3.connect(path) as conn:
        conn.executescript("""CREATE TABLE minute_bars (
            symbol TEXT NOT NULL, ts TEXT NOT NULL, session_date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            source TEXT NOT NULL, fetched_at TEXT NOT NULL,
            PRIMARY KEY (symbol, ts, source));""")
        conn.executemany(
            "INSERT INTO minute_bars "
            "(symbol, ts, session_date, open, high, low, close, volume, source, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                # Lower bound is exclusive; upper bound is inclusive.
                ("ABC", WINDOW_START, "2026-10-20", 1, 1, 1, 1, 10, "alpaca", "2026-10-20T13:00:00Z"),
                ("ABC", WINDOW_END, "2026-10-20", 2, 2, 2, 2, 20, "alpaca", "2026-10-20T13:00:00Z"),
                ("ABC", "2026-10-20T14:31:00Z", "2026-10-20", 3, 3, 3, 3, 30,
                 "alpaca", "2026-10-20T13:00:00Z"),
                ("XYZ", WINDOW_END, "2026-10-20", 4, 4, 4, 4, 40, "alpaca", "2026-10-20T13:00:00Z"),
            ],
        )


def _read(verified, registry, store_id, adapter, ticker, *, mode, sessions,
          market_source_id=None, start=WINDOW_START, end=WINDOW_END, cutoff=CUTOFF):
    return read_store_facts(
        verified, store_id, adapter, ticker, start, end, cutoff,
        mode=mode, session_dates=sessions, registry_path=registry,
        market_source_id=market_source_id,
    )


def test_daily_bars_fail_closed_without_a_registered_source_binding(tmp_path):
    daily = tmp_path / "daily.sqlite3"
    _daily_store(daily)
    verified, registry = _verified(tmp_path, {"daily": daily})

    with pytest.raises(ValueError, match="source|registered|binding"):
        _read(
            verified, registry, "daily", "daily_bars", "ABC",
            mode="published", sessions=("2026-10-20",),
            start=DAILY_START, end=DAILY_END, cutoff=DAILY_CUTOFF,
        )
    with pytest.raises(ValueError, match="daily|market|source"):
        _read(
            verified, registry, "daily", "daily_bars", "ABC",
            mode="published", sessions=("2026-10-20",),
            market_source_id="market_archive:minute",
            start=DAILY_START, end=DAILY_END, cutoff=DAILY_CUTOFF,
        )


def test_daily_price_bars_exception_is_accepted_only_for_market_store(tmp_path):
    daily = tmp_path / "daily.sqlite3"
    _daily_store(daily)
    sources = [
        {"id": "daily:price_bars", "store": "market", "category": "market"},
    ]
    verified, registry = _verified(
        tmp_path, {"daily": daily}, sources=sources,
        registry_stores={"market": "/opt/catalyst/repo/data/m9/market.sqlite3"},
    )
    result = read_store_facts(
        verified, "daily", "daily_bars", "ABC", DAILY_START, DAILY_END,
        DAILY_CUTOFF, mode="published", session_dates=("2026-10-20",),
        registry_path=registry, market_source_id="daily:price_bars",
    )
    assert result["audit"]["market_source_id"] == "daily:price_bars"

    invalid_sources = [
        {"id": "daily:price_bars", "store": "fundamentals", "category": "market"},
    ]
    invalid, invalid_registry = _verified(
        tmp_path, {"daily": daily}, sources=invalid_sources,
        registry_stores={"fundamentals": "/opt/catalyst/fundamentals.sqlite3"},
    )
    with pytest.raises(ValueError, match="registry"):
        read_store_facts(
            invalid, "daily", "daily_bars", "ABC", DAILY_START, DAILY_END,
            DAILY_CUTOFF, mode="published", session_dates=("2026-10-20",),
            registry_path=invalid_registry, market_source_id="daily:price_bars",
        )


def test_market_archive_source_cannot_be_rebound_to_fundamentals(tmp_path):
    minute = tmp_path / "minute_2026-10.sqlite3"
    _minute_store(minute)
    verified, registry = _verified(
        tmp_path, {"minute-oct": minute},
        sources=[{"id": "market_archive:minute", "store": "fundamentals", "category": "market"}],
        registry_stores={"fundamentals": "/opt/catalyst/fundamentals.sqlite3"},
    )
    with pytest.raises(ValueError, match="registry"):
        _read(verified, registry, "minute-oct", "minute_bars", "ABC",
              mode="published", sessions=("2026-10-20",),
              market_source_id="market_archive:minute")


def test_daily_legacy_unknown_observation_is_excluded_and_counted_but_published(tmp_path):
    daily = tmp_path / "daily.sqlite3"
    _daily_store(daily)
    with sqlite3.connect(daily) as conn:
        conn.execute(
            "INSERT INTO ohlcv_metadata (symbol,date,first_seen_at,is_backfill,provenance) "
            "VALUES ('ABC','2026-10-20',NULL,0,'legacy_unknown')"
        )
    verified, registry = _verified(
        tmp_path, {"daily": daily},
        sources=[{"id": "daily:price_bars", "store": "market", "category": "market"}],
        registry_stores={"market": "/opt/catalyst/repo/data/m9/market.sqlite3"},
    )
    observed = read_store_facts(
        verified, "daily", "daily_bars", "ABC", DAILY_START, DAILY_END,
        DAILY_CUTOFF, mode="observed", session_dates=("2026-10-20",),
        registry_path=registry, market_source_id="daily:price_bars",
    )
    published = read_store_facts(
        verified, "daily", "daily_bars", "ABC", DAILY_START, DAILY_END,
        DAILY_CUTOFF, mode="published", session_dates=("2026-10-20",),
        registry_path=registry, market_source_id="daily:price_bars",
    )
    assert observed["market_facts"] == []
    assert observed["audit"]["excluded_unobserved_count"] == 1
    assert len(published["market_facts"]) == 1
    assert published["market_facts"][0]["observation_provenance"] == "legacy_unknown"


def test_daily_bars_without_metadata_table_are_excluded_and_counted_but_published(tmp_path):
    daily = tmp_path / "legacy-daily.sqlite3"
    with sqlite3.connect(daily) as conn:
        conn.execute(
            "CREATE TABLE ohlcv (symbol TEXT NOT NULL,date TEXT NOT NULL,open REAL,high REAL,"
            "low REAL,close REAL,volume REAL,source TEXT NOT NULL,PRIMARY KEY(symbol,date))"
        )
        conn.execute(
            "INSERT INTO ohlcv VALUES ('ABC','2026-10-20',10,12,9,11,100,'yfinance')"
        )
    verified, registry = _verified(
        tmp_path, {"daily": daily},
        sources=[{"id": "daily:price_bars", "store": "market", "category": "market"}],
        registry_stores={"market": "/opt/catalyst/repo/data/m9/market.sqlite3"},
    )
    observed = _read(
        verified, registry, "daily", "daily_bars", "ABC", mode="observed",
        sessions=("2026-10-20",), market_source_id="daily:price_bars",
        start=DAILY_START, end=DAILY_END, cutoff=DAILY_CUTOFF,
    )
    published = _read(
        verified, registry, "daily", "daily_bars", "ABC", mode="published",
        sessions=("2026-10-20",), market_source_id="daily:price_bars",
        start=DAILY_START, end=DAILY_END, cutoff=DAILY_CUTOFF,
    )
    assert observed["market_facts"] == []
    assert observed["audit"]["excluded_unobserved_count"] == 1
    assert len(published["market_facts"]) == 1
    assert published["market_facts"][0]["observation_provenance"] == "legacy_unknown"


def test_daily_observation_cutoff_includes_boundary_and_excludes_later_metadata(tmp_path):
    daily = tmp_path / "daily.sqlite3"
    with sqlite3.connect(daily) as conn:
        conn.executescript(market.SCHEMA)
        conn.executemany(
            "INSERT INTO ohlcv VALUES (?,?,?,?,?,?,?,?)",
            [
                ("ABC", "2026-10-20", 10, 12, 9, 11, 100, "yfinance"),
                ("ABC", "2026-10-21", 11, 13, 10, 12, 110, "yfinance"),
            ],
        )
        conn.executemany(
            "INSERT INTO ohlcv_metadata "
            "(symbol,date,first_seen_at,is_backfill,provenance) VALUES (?,?,?,?,?)",
            [
                ("ABC", "2026-10-20", "2026-10-21T22:00:00Z", 0, "known"),
                ("ABC", "2026-10-21", "2026-10-21T22:00:00.000001Z", 0, "known"),
            ],
        )
    verified, registry = _verified(
        tmp_path, {"daily": daily},
        sources=[{"id": "daily:price_bars", "store": "market", "category": "market"}],
        registry_stores={"market": "/opt/catalyst/repo/data/m9/market.sqlite3"},
    )
    observed = _read(
        verified, registry, "daily", "daily_bars", "ABC", mode="observed",
        sessions=("2026-10-20", "2026-10-21"), market_source_id="daily:price_bars",
        start=DAILY_START, end="2026-10-22T00:00:00Z", cutoff="2026-10-21T22:00:00Z",
    )
    assert [row["session_date"] for row in observed["market_facts"]] == ["2026-10-20"]
    assert observed["audit"]["excluded_unobserved_count"] == 1


def test_minute_bars_need_explicit_session_and_never_treat_legacy_fetched_at_as_observed(tmp_path):
    minute = tmp_path / "minute_2026-10.sqlite3"
    _minute_store(minute)
    verified, registry = _verified(tmp_path, {"minute-oct": minute})

    published = _read(
        verified, registry, "minute-oct", "minute_bars", "ABC",
        mode="published", sessions=("2026-10-20",), market_source_id="market_archive:minute",
    )
    observed = _read(
        verified, registry, "minute-oct", "minute_bars", "ABC",
        mode="observed", sessions=("2026-10-20",), market_source_id="market_archive:minute",
    )

    assert published["metadata_leads"] == []
    assert len(published["market_facts"]) == 1
    fact = published["market_facts"][0]
    assert fact["subject_kind"] == "instrument" and fact["subject_id"] == "ABC"
    assert fact["provider"] == "alpaca"
    assert datetime.fromisoformat(fact["timestamp"]) == datetime.fromisoformat(WINDOW_END.replace("Z", "+00:00"))
    assert fact["session_date"] == "2026-10-20"
    assert fact["temporal_status"] == "unverified_published"
    assert fact.get("observed_at") is None
    assert fact.get("published_at") is None  # ts is market event time, not availability time
    assert observed["market_facts"] == []


def test_minute_bars_are_isolated_by_ticker_and_declared_session(tmp_path):
    minute = tmp_path / "minute_2026-10.sqlite3"
    _minute_store(minute)
    verified, registry = _verified(tmp_path, {"minute-oct": minute})

    abc = _read(
        verified, registry, "minute-oct", "minute_bars", "ABC",
        mode="published", sessions=("2026-10-20",), market_source_id="market_archive:minute",
    )
    other_session = _read(
        verified, registry, "minute-oct", "minute_bars", "ABC",
        mode="published", sessions=("2026-10-21",), market_source_id="market_archive:minute",
    )

    assert [row["subject_id"] for row in abc["market_facts"]] == ["ABC"]
    assert [datetime.fromisoformat(row["timestamp"]) for row in abc["market_facts"]] == [datetime.fromisoformat(WINDOW_END.replace("Z", "+00:00"))]
    assert other_session["market_facts"] == []


def test_minute_bar_session_must_match_utc_timestamp_in_new_york(tmp_path):
    minute = tmp_path / "minute_2026-10.sqlite3"
    _minute_store(minute)
    with sqlite3.connect(minute) as conn:
        conn.execute(
            "INSERT INTO minute_bars "
            "(symbol, ts, session_date, open, high, low, close, volume, source, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("ABC", "2026-10-21T04:30:00Z", "2026-10-20", 5, 5, 5, 5, 50,
             "alpaca", "2026-10-20T13:00:00Z"),
        )
    verified, registry = _verified(tmp_path, {"minute-oct": minute})

    with pytest.raises(ValueError, match="session|timestamp|date"):
        _read(
            verified, registry, "minute-oct", "minute_bars", "ABC",
            mode="published", sessions=("2026-10-20",), market_source_id="market_archive:minute",
            start="2026-10-21T04:00:00Z", end="2026-10-21T05:00:00Z",
            cutoff="2026-10-21T00:00:00Z",
        )


def test_minute_bar_adapter_requires_explicit_sessions(tmp_path):
    minute = tmp_path / "minute_2026-10.sqlite3"
    _minute_store(minute)
    verified, registry = _verified(tmp_path, {"minute-oct": minute})

    with pytest.raises(ValueError, match="session"):
        _read(
            verified, registry, "minute-oct", "minute_bars", "ABC",
            mode="published", sessions=(),
            start=DAILY_START, end=DAILY_END, cutoff=DAILY_CUTOFF,
        )


def test_minute_bars_require_an_explicit_registered_source_binding(tmp_path):
    minute = tmp_path / "minute_2026-10.sqlite3"
    _minute_store(minute)
    verified, registry = _verified(tmp_path, {"minute-oct": minute})

    with pytest.raises(ValueError, match="source|registered|binding"):
        _read(
            verified, registry, "minute-oct", "minute_bars", "ABC",
            mode="published", sessions=("2026-10-20",),
        )


def test_minute_bars_reject_registered_source_with_non_market_category(tmp_path):
    minute = tmp_path / "minute_2026-10.sqlite3"
    _minute_store(minute)
    verified, registry = _verified(tmp_path, {"minute-oct": minute})

    with pytest.raises(ValueError, match="category|market|source"):
        _read(
            verified, registry, "minute-oct", "minute_bars", "ABC",
            mode="published", sessions=("2026-10-20",),
            market_source_id="market_archive:events",
        )


def test_minute_bars_accept_explicit_registered_research_source_binding(tmp_path):
    minute = tmp_path / "minute_2026-10.sqlite3"
    _minute_store(minute)
    verified, registry = _verified(tmp_path, {"minute-oct": minute})

    result = _read(
        verified, registry, "minute-oct", "minute_bars", "ABC",
        mode="published", sessions=("2026-10-20",),
        market_source_id="market_archive:research",
    )

    assert {row["source_id"] for row in result["market_facts"]} == {
        "market_archive:research",
    }

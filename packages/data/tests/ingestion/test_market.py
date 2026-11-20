from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import pytest
from catalyst_data.ingestion.collectors import market
from catalyst_data.ingestion.collectors.market import (
    SCHEMA,
    last_closed_session,
    load_bars,
    load_sic,
    session_closed,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    return conn


def test_session_closed_uses_close_and_trading_calendar() -> None:
    assert not session_closed("2026-10-05", datetime.fromisoformat("2026-10-05T15:00:00-04:00"))
    assert session_closed("2026-10-05", datetime(2026, 10, 5, 21, tzinfo=timezone.utc))
    assert not session_closed("2026-10-03", datetime(2026, 10, 5, 21, tzinfo=timezone.utc))


def test_last_closed_session_during_tuesday_session_returns_monday() -> None:
    now = datetime.fromisoformat("2026-10-06T15:00:00-04:00")
    assert last_closed_session(now) == "2026-10-05"


def test_load_bars_refuses_open_session() -> None:
    with pytest.raises(ValueError, match="SESSION_NOT_CLOSED"):
        load_bars(
            _conn(), ["SPY"], "2026-10-05", "2026-10-05",
            now=datetime.fromisoformat("2026-10-05T15:00:00-04:00"), download=lambda *a, **k: None,
        )


def test_load_bars_uses_exclusive_end_and_drops_later_rows() -> None:
    conn = _conn()
    calls = []
    frame = pd.DataFrame(
        {
            "Open": [1, 2, 3], "High": [2, 3, 4], "Low": [0.5, 1.5, 2.5],
            "Close": [1.5, 2.5, 3.5], "Volume": [10, 20, 30],
        },
        index=pd.to_datetime(["2026-10-02", "2026-10-05", "2026-10-06"]),
    )

    def download(*args, **kwargs):
        calls.append((args, kwargs))
        return frame

    counts = load_bars(
        conn, ["SPY"], "2026-10-02", "2026-10-05",
        now=datetime(2026, 10, 5, 21, tzinfo=timezone.utc), download=download,
    )
    assert calls == [(("SPY",), {"start": "2026-10-02", "end": "2026-10-06",
                                   "progress": False, "auto_adjust": False})]
    assert counts == {"SPY": 2}
    assert conn.execute("SELECT date FROM ohlcv ORDER BY date").fetchall() == [
        ("2026-10-02",), ("2026-10-05",)
    ]


def test_load_sic_skips_existing_and_fetches_missing_ticker(tmp_path) -> None:
    conn = _conn()
    conn.execute("INSERT INTO issuer_sic VALUES (?,?,?,?,?)", ("OLD", "0000000001", "1111", "old", "now"))
    mapping = tmp_path / "tickers.json"
    mapping.write_text(json.dumps({"0": {"ticker": "NEW", "cik_str": 2}}))
    requests = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"sic": "2222", "sicDescription": "new"}

    def http_get(url, **kwargs):
        requests.append((url, kwargs))
        return Response()

    assert load_sic(
        conn, ["OLD", "NEW"], user_agent="test-agent", http_get=http_get,
        sleep=lambda _: None, tickers_json=mapping,
    ) == {"OLD": "1111", "NEW": "2222"}
    assert len(requests) == 1
    assert requests[0][0] == "https://data.sec.gov/submissions/CIK0000000002.json"


def test_load_sic_requires_sec_user_agent(monkeypatch) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(ValueError, match="SEC_USER_AGENT not set"):
        load_sic(_conn(), [], tickers_json="unused.json")


def test_ohlcv_metadata_migration_keeps_legacy_observation_unknown_and_is_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, "
        "close REAL, volume REAL, source TEXT, PRIMARY KEY(symbol,date))"
    )
    conn.execute(
        "CREATE TABLE ohlcv_metadata (symbol TEXT NOT NULL,date TEXT NOT NULL,first_seen_at TEXT,"
        "is_backfill INTEGER NOT NULL CHECK (is_backfill IN (0,1)),PRIMARY KEY(symbol,date))"
    )
    conn.execute(
        "INSERT INTO ohlcv VALUES ('OLD','2026-10-20',1,2,0.5,1.5,10,'yfinance')"
    )
    market._migrate_ohlcv_metadata(conn)
    row = conn.execute(
        "SELECT first_seen_at,is_backfill,provenance FROM ohlcv_metadata"
    ).fetchone()
    assert row == (None, 0, "legacy_unknown")

    market._migrate_ohlcv_metadata(conn)
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_metadata").fetchone() == (1,)
    market._record_ohlcv_metadata(
        conn, "OLD", "2026-10-20", first_seen_at="2026-10-27T10:00:00+00:00",
        is_backfill=False,
    )
    assert conn.execute(
        "SELECT first_seen_at,is_backfill,provenance FROM ohlcv_metadata"
    ).fetchone() == ("2026-10-27T10:00:00+00:00", 0, "known")

    fresh = sqlite3.connect(":memory:")
    fresh.executescript(SCHEMA)
    fresh.execute(
        "INSERT INTO ohlcv VALUES ('OLD','2026-10-20',1,2,0.5,1.5,10,'yfinance')"
    )
    market._migrate_ohlcv_metadata(fresh)
    assert fresh.execute(
        "SELECT first_seen_at,is_backfill,provenance FROM ohlcv_metadata"
    ).fetchone() == (None, None, "legacy_unknown")


@pytest.mark.parametrize(
    ("existing", "incoming", "expected"),
    [
        (("2026-10-27T10:00:00Z", 0, "known"),
         ("2026-10-28T10:00:00Z", 1), ("2026-10-27T10:00:00Z", 0, "known")),
        (("2026-10-27T10:00:00Z", 1, "known"),
         ("2026-10-28T10:00:00Z", 0), ("2026-10-27T10:00:00Z", 1, "known")),
        ((None, None, "legacy_unknown"),
         ("2026-10-28T10:00:00Z", 0), ("2026-10-28T10:00:00Z", 0, "known")),
        ((None, None, "legacy_unknown"),
         ("2026-10-28T10:00:00Z", 1), ("2026-10-28T10:00:00Z", 1, "known")),
        ((None, None, "legacy_unknown"),
         (None, 1), (None, None, "legacy_unknown")),
    ],
    ids=["realtime-then-backfill", "backfill-then-realtime", "null-then-realtime",
         "null-then-backfill", "both-null"],
)
def test_record_ohlcv_metadata_preserves_first_known_observation(existing, incoming, expected):
    conn = _conn()
    conn.execute(
        "INSERT INTO ohlcv_metadata (symbol,date,first_seen_at,is_backfill,provenance) "
        "VALUES ('ABC','2026-10-20',?,?,?)", existing,
    )
    market._record_ohlcv_metadata(
        conn, "ABC", "2026-10-20", first_seen_at=incoming[0], is_backfill=bool(incoming[1]),
    )
    assert conn.execute(
        "SELECT first_seen_at,is_backfill,provenance FROM ohlcv_metadata "
        "WHERE symbol='ABC' AND date='2026-10-20'"
    ).fetchone() == expected


def test_load_sic_retries_a_dropped_connection_and_continues(tmp_path, monkeypatch):
    import json
    import sqlite3

    from catalyst_data.ingestion.collectors import market

    mapping = tmp_path / "tickers.json"
    mapping.write_text(json.dumps({
        "0": {"ticker": "AAA", "cik_str": 1}, "1": {"ticker": "BBB", "cik_str": 2},
    }))
    conn = sqlite3.connect(tmp_path / "m.sqlite3")
    conn.executescript(market.SCHEMA)
    calls = {"AAA": 0, "BBB": 0}

    class Resp:
        status_code = 200

        def __init__(self, sic):
            self._sic = sic

        def json(self):
            return {"sic": self._sic, "sicDescription": "x"}

    def http_get(url, **_):
        ticker = "AAA" if url.endswith("0000000001.json") else "BBB"
        calls[ticker] += 1
        if ticker == "AAA":
            raise OSError("SSL EOF")
        return Resp("1234")

    result = market.load_sic(conn, ["AAA", "BBB"], user_agent="ua", http_get=http_get,
                             sleep=lambda s: None, tickers_json=mapping)
    assert calls["AAA"] == 3 and result["AAA"] is None
    assert result["BBB"] == "1234"


def test_the_provenance_migration_runs_once_per_process_and_file_not_for_every_symbol(tmp_path, monkeypatch):
    """The daily opens a writer per symbol; scanning every stored bar each time made the price stage 5x slower."""
    calls = []
    real = market._migrate_ohlcv_metadata
    monkeypatch.setattr(market, "_migrate_ohlcv_metadata", lambda conn: calls.append(1) or real(conn))
    monkeypatch.setattr(market, "_MIGRATED", set())
    path = tmp_path / "market.sqlite3"
    for _ in range(3):
        conn = market.open_market_writer(path)
        market.load_bars(conn, [], "2026-10-01", "2026-10-02", now=datetime(2026, 10, 3, tzinfo=timezone.utc))
        conn.close()
    assert calls == [1]
    other = tmp_path / "other.sqlite3"
    market.open_market_writer(other).close()
    assert calls == [1, 1]


def test_a_migration_that_was_never_committed_is_not_remembered_and_legacy_bars_get_their_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(market, "_MIGRATED", set())
    path = tmp_path / "market.sqlite3"
    conn = market.open_market_writer(path)
    conn.execute("INSERT INTO ohlcv VALUES ('OLD','2026-09-01',1,1,1,1,1,'x')")  # a bar from before provenance existed
    conn.commit()
    conn.close()
    reopened = market.open_market_writer(path)  # same process: remembered, so not migrated again here ...
    assert reopened.execute("SELECT count(*) FROM ohlcv_metadata WHERE symbol='OLD'").fetchone() == (0,)
    reopened.close()
    monkeypatch.setattr(market, "_MIGRATED", set())  # ... and a new process migrates and keeps the result
    market.open_market_writer(path).close()
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT provenance FROM ohlcv_metadata WHERE symbol='OLD'").fetchone() == ("legacy_unknown",)


def test_a_migration_inside_the_callers_transaction_is_not_remembered(tmp_path, monkeypatch):
    monkeypatch.setattr(market, "_MIGRATED", set())
    path = tmp_path / "market.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(market.SCHEMA)
    conn.execute("BEGIN")
    market._ensure_ohlcv_metadata(conn)
    assert market._MIGRATED == set()
    conn.rollback()
    conn.close()

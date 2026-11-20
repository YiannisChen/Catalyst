import sqlite3

from catalyst_agents.runtime.validation import validate_live_run_request


def _db_with_ohlcv(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE ohlcv (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            source TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO ohlcv (symbol, date, open, high, low, close, volume, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


def test_valid_request_accepts_supported_ticker_trading_day_and_empty_query():
    conn = _db_with_ohlcv([
        ("AAPL", "2026-01-15", 1.0, 2.0, 0.5, 1.5, 1000, "polygon"),
        ("MSFT", "2026-01-15", 1.0, 2.0, 0.5, 1.5, 1000, "polygon"),
    ])

    result = validate_live_run_request(conn, ticker="aapl", trade_date="2026-01-15", query=None)

    assert result == {
        "ok": True,
        "ticker": "AAPL",
        "trade_date": "2026-01-15",
        "query": None,
        "failure": None,
    }


def test_valid_request_uses_real_ohlcv_symbol_date_schema():
    conn = _db_with_ohlcv([("AAPL", "2026-01-15", 1.0, 2.0, 0.5, 1.5, 1000, "polygon")])

    result = validate_live_run_request(conn, ticker="AAPL", trade_date="2026-01-15", query=None)

    assert result["ok"] is True
    assert result["ticker"] == "AAPL"
    assert result["trade_date"] == "2026-01-15"


def test_valid_request_strips_non_empty_query():
    conn = _db_with_ohlcv([("AAPL", "2026-01-15", 1.0, 2.0, 0.5, 1.5, 1000, "polygon")])

    result = validate_live_run_request(conn, ticker="AAPL", trade_date="2026-01-15", query="  explain move  ")

    assert result["ok"] is True
    assert result["query"] == "explain move"


def test_validation_rejects_unsupported_ticker():
    conn = _db_with_ohlcv([("AAPL", "2026-01-15", 1.0, 2.0, 0.5, 1.5, 1000, "polygon")])

    result = validate_live_run_request(conn, ticker="TSLA", trade_date="2026-01-15", query=None)

    assert result["ok"] is False
    assert result["failure"] == {
        "status": "FAILED_REQUEST",
        "sub_reason": "unsupported_ticker",
        "message": "Ticker is not available in the runtime dataset.",
        "field": "ticker",
        "retryable": False,
    }


def test_validation_rejects_date_out_of_range():
    conn = _db_with_ohlcv([
        ("AAPL", "2026-01-10", 1.0, 2.0, 0.5, 1.5, 1000, "polygon"),
        ("AAPL", "2026-01-20", 1.0, 2.0, 0.5, 1.5, 1000, "polygon"),
    ])

    result = validate_live_run_request(conn, ticker="AAPL", trade_date="2026-01-25", query=None)

    assert result["ok"] is False
    assert result["failure"]["sub_reason"] == "date_out_of_range"
    assert result["failure"]["field"] == "trade_date"


def test_validation_rejects_missing_trading_day_context_inside_range():
    conn = _db_with_ohlcv([
        ("AAPL", "2026-01-10", 1.0, 2.0, 0.5, 1.5, 1000, "polygon"),
        ("AAPL", "2026-01-20", 1.0, 2.0, 0.5, 1.5, 1000, "polygon"),
    ])

    result = validate_live_run_request(conn, ticker="AAPL", trade_date="2026-01-15", query=None)

    assert result["ok"] is False
    assert result["failure"] == {
        "status": "FAILED_REQUEST",
        "sub_reason": "missing_trading_day_context",
        "message": "No OHLCV row exists for the requested ticker and trade date.",
        "field": "trade_date",
        "retryable": False,
    }


def test_validation_rejects_invalid_query_type_and_blank_query():
    conn = _db_with_ohlcv([("AAPL", "2026-01-15", 1.0, 2.0, 0.5, 1.5, 1000, "polygon")])

    invalid_type = validate_live_run_request(conn, ticker="AAPL", trade_date="2026-01-15", query=123)
    blank = validate_live_run_request(conn, ticker="AAPL", trade_date="2026-01-15", query="   ")

    assert invalid_type["failure"]["sub_reason"] == "invalid_query_type"
    assert invalid_type["failure"]["field"] == "query"
    assert blank["failure"]["sub_reason"] == "empty_query"
    assert blank["failure"]["field"] == "query"


def test_validation_rejects_too_long_query():
    conn = _db_with_ohlcv([("AAPL", "2026-01-15", 1.0, 2.0, 0.5, 1.5, 1000, "polygon")])

    result = validate_live_run_request(conn, ticker="AAPL", trade_date="2026-01-15", query="x" * 501, max_query_chars=500)

    assert result["ok"] is False
    assert result["failure"]["sub_reason"] == "query_too_long"
    assert result["failure"]["field"] == "query"

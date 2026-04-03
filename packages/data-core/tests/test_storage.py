import sqlite3
from catalyst_data.storage.sqlite import (
    init_db,
    upsert_raw_asset,
    upsert_clean_asset,
    get_raw_asset,
    get_clean_asset,
    upsert_ohlcv,
    get_ohlcv,
)


def test_init_db_creates_all_tables():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "raw_assets" in tables
    assert "clean_assets" in tables
    assert "ohlcv" in tables
    assert "news_alignment" in tables
    assert "attributions" in tables
    assert "golden_events" in tables
    conn.close()


def test_init_db_sets_wal_mode():
    import tempfile, os

    path = os.path.join(tempfile.mkdtemp(), "test.db")
    conn = sqlite3.connect(path)
    init_db(conn)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    conn.close()


def test_upsert_and_get_raw_asset():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    upsert_raw_asset(
        conn,
        asset_id="abc123",
        ticker="AAPL",
        source_type="polygon_news",
        reference_date="2026-01-15",
        data_version="v1",
        content_raw=b'{"test": true}',
        http_status=200,
        metadata={},
    )
    asset = get_raw_asset(conn, "abc123")
    assert asset is not None
    assert asset["ticker"] == "AAPL"
    assert asset["source_type"] == "polygon_news"
    conn.close()


def test_upsert_and_get_clean_asset():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    upsert_raw_asset(
        conn,
        asset_id="abc123",
        ticker="AAPL",
        source_type="polygon_news",
        reference_date="2026-01-15",
        data_version="v1",
        content_raw=b'{"test": true}',
        http_status=200,
        metadata={},
    )
    upsert_clean_asset(
        conn,
        asset_id="abc123",
        ticker="AAPL",
        source_type="polygon_news",
        reference_date="2026-01-15",
        content_md="## Test",
        title_hash="aabb",
    )
    asset = get_clean_asset(conn, "abc123")
    assert asset is not None
    assert asset["content_md"] == "## Test"
    conn.close()


def test_upsert_and_get_ohlcv():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    upsert_ohlcv(
        conn,
        symbol="AAPL",
        date="2026-01-15",
        open=150.0,
        high=155.0,
        low=149.0,
        close=152.0,
        volume=1000000.0,
    )
    bar = get_ohlcv(conn, "AAPL", "2026-01-15")
    assert bar is not None
    assert bar["close"] == 152.0
    conn.close()

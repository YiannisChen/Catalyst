"""Bronze/Silver medallion storage layer backed by SQLite.

Tables
------
- raw_assets     (Bronze) – immutable fetched payloads, zlib-compressed.
- clean_assets   (Silver) – cleaned Markdown, deduplicated.
- ohlcv          – daily price bars.
- news_alignment – maps articles to trading days with forward returns.
- attributions   – agent attribution results.
- golden_events  – evaluation golden set.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_PRAGMAS = [
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA busy_timeout=5000;",
    "PRAGMA foreign_keys=ON;",
]

_DDL = """
-- BRONZE: raw fetched data, never modified after insert
CREATE TABLE IF NOT EXISTS raw_assets (
    asset_id        TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    reference_date  TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    data_version    TEXT NOT NULL DEFAULT 'v1',
    content_raw     BLOB NOT NULL,
    http_status     INTEGER,
    metadata_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_raw_ticker_date ON raw_assets(ticker, reference_date);

-- SILVER: cleaned Markdown, deduplicated
CREATE TABLE IF NOT EXISTS clean_assets (
    asset_id        TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    reference_date  TEXT NOT NULL,
    cleaned_at      TEXT NOT NULL,
    content_md      TEXT NOT NULL,
    title_hash      TEXT,
    is_duplicate    INTEGER DEFAULT 0,
    FOREIGN KEY (asset_id) REFERENCES raw_assets(asset_id)
);
CREATE INDEX IF NOT EXISTS idx_clean_ticker_date ON clean_assets(ticker, reference_date);
CREATE INDEX IF NOT EXISTS idx_clean_title_hash ON clean_assets(title_hash);

-- OHLCV price data
CREATE TABLE IF NOT EXISTS ohlcv (
    symbol          TEXT NOT NULL,
    date            TEXT NOT NULL,
    open            REAL,
    high            REAL,
    low             REAL,
    close           REAL,
    volume          REAL,
    source          TEXT DEFAULT 'polygon',
    PRIMARY KEY (symbol, date)
);

-- News-to-trading-day alignment
CREATE TABLE IF NOT EXISTS news_alignment (
    asset_id        TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    published_utc   TEXT,
    ret_t0          REAL,
    ret_t1          REAL,
    ret_t3          REAL,
    ret_t5          REAL,
    PRIMARY KEY (asset_id, ticker),
    FOREIGN KEY (asset_id) REFERENCES clean_assets(asset_id)
);
CREATE INDEX IF NOT EXISTS idx_align_ticker_date ON news_alignment(ticker, trade_date);

-- Agent attribution results
CREATE TABLE IF NOT EXISTS attributions (
    id              TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    causes_json     TEXT NOT NULL,
    summary_md      TEXT NOT NULL,
    grounding_rate  REAL,
    model_id        TEXT,
    agent_config    TEXT,
    token_count     INTEGER,
    cost_usd        REAL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attr_ticker_date ON attributions(ticker, trade_date);

-- Eval golden set
CREATE TABLE IF NOT EXISTS golden_events (
    id              TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    price_move_pct  REAL NOT NULL,
    causes_json     TEXT NOT NULL,
    source_ids      TEXT,
    annotator       TEXT DEFAULT 'manual',
    created_at      TEXT NOT NULL
);
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_asset_id(
    ticker: str, date: str, source_type: str, data_version: str = "v1"
) -> str:
    """Deterministic SHA-256 content-address for a data asset."""
    raw = f"{ticker}|{date}|{source_type}|{data_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def init_db(conn: sqlite3.Connection) -> None:
    """Set pragmas, create all tables and indexes."""
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    conn.executescript(_DDL)
    conn.commit()


# ---------------------------------------------------------------------------
# raw_assets (Bronze)
# ---------------------------------------------------------------------------


def upsert_raw_asset(
    conn: sqlite3.Connection,
    *,
    asset_id: str,
    ticker: str,
    source_type: str,
    reference_date: str,
    data_version: str = "v1",
    content_raw: bytes,
    http_status: int | None = None,
    metadata: dict | None = None,
) -> None:
    """Insert or replace a raw (Bronze) asset. Compresses content_raw with zlib."""
    compressed = zlib.compress(content_raw)
    metadata_json = json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True)

    conn.execute(
        """
        INSERT OR REPLACE INTO raw_assets
            (asset_id, ticker, source_type, reference_date, fetched_at,
             data_version, content_raw, http_status, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            asset_id,
            ticker,
            source_type,
            reference_date,
            _now_iso(),
            data_version,
            compressed,
            http_status,
            metadata_json,
        ),
    )
    conn.commit()


def get_raw_asset(conn: sqlite3.Connection, asset_id: str) -> dict | None:
    """Fetch a raw asset by id; decompresses content_raw."""
    row = conn.execute(
        """
        SELECT asset_id, ticker, source_type, reference_date, fetched_at,
               data_version, content_raw, http_status, metadata_json
        FROM raw_assets WHERE asset_id = ?
        """,
        (asset_id,),
    ).fetchone()

    if row is None:
        return None

    content_raw = row[6]
    try:
        content_raw = zlib.decompress(content_raw)
    except zlib.error:
        content_raw = None

    return {
        "asset_id": row[0],
        "ticker": row[1],
        "source_type": row[2],
        "reference_date": row[3],
        "fetched_at": row[4],
        "data_version": row[5],
        "content_raw": content_raw,
        "http_status": row[7],
        "metadata": json.loads(row[8]) if row[8] else {},
    }


# ---------------------------------------------------------------------------
# clean_assets (Silver)
# ---------------------------------------------------------------------------


def upsert_clean_asset(
    conn: sqlite3.Connection,
    *,
    asset_id: str,
    ticker: str,
    source_type: str,
    reference_date: str,
    content_md: str,
    title_hash: str | None = None,
    is_duplicate: int = 0,
) -> None:
    """Insert or replace a cleaned (Silver) asset."""
    conn.execute(
        """
        INSERT OR REPLACE INTO clean_assets
            (asset_id, ticker, source_type, reference_date, cleaned_at,
             content_md, title_hash, is_duplicate)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            asset_id,
            ticker,
            source_type,
            reference_date,
            _now_iso(),
            content_md,
            title_hash,
            is_duplicate,
        ),
    )
    conn.commit()


def get_clean_asset(conn: sqlite3.Connection, asset_id: str) -> dict | None:
    """Fetch a cleaned asset by id."""
    row = conn.execute(
        """
        SELECT asset_id, ticker, source_type, reference_date, cleaned_at,
               content_md, title_hash, is_duplicate
        FROM clean_assets WHERE asset_id = ?
        """,
        (asset_id,),
    ).fetchone()

    if row is None:
        return None

    return {
        "asset_id": row[0],
        "ticker": row[1],
        "source_type": row[2],
        "reference_date": row[3],
        "cleaned_at": row[4],
        "content_md": row[5],
        "title_hash": row[6],
        "is_duplicate": row[7],
    }


# ---------------------------------------------------------------------------
# ohlcv
# ---------------------------------------------------------------------------


def upsert_ohlcv(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    date: str,
    open: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    source: str = "polygon",
) -> None:
    """Insert or replace a daily OHLCV bar."""
    conn.execute(
        """
        INSERT OR REPLACE INTO ohlcv
            (symbol, date, open, high, low, close, volume, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (symbol, date, open, high, low, close, volume, source),
    )
    conn.commit()


def get_ohlcv(conn: sqlite3.Connection, symbol: str, date: str) -> dict | None:
    """Fetch a single OHLCV bar by symbol and date."""
    row = conn.execute(
        """
        SELECT symbol, date, open, high, low, close, volume, source
        FROM ohlcv WHERE symbol = ? AND date = ?
        """,
        (symbol, date),
    ).fetchone()

    if row is None:
        return None

    return {
        "symbol": row[0],
        "date": row[1],
        "open": row[2],
        "high": row[3],
        "low": row[4],
        "close": row[5],
        "volume": row[6],
        "source": row[7],
    }

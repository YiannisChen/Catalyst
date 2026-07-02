"""Bronze/Silver medallion storage layer backed by SQLite.

Tables
------
- raw_assets     (Bronze) - immutable fetched payloads, zlib-compressed.
- clean_assets   (Silver) - cleaned Markdown, deduplicated.
- ohlcv          - daily price bars.
- news_alignment - maps articles to trading days with forward returns.
- attributions   - agent attribution results.
- golden_events  - evaluation golden set.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from datetime import datetime, timezone

from catalyst_data.articles import ensure_articles_table

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
-- Additive: provenance column (applied via ensure_clean_provenance)
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

-- Index build manifest: one row per real embedding build
CREATE TABLE IF NOT EXISTS index_manifests (
    build_id              TEXT PRIMARY KEY,
    created_at            TEXT NOT NULL,
    model                 TEXT NOT NULL,
    model_hash            TEXT NOT NULL,
    lancedb_path          TEXT NOT NULL,
    l1_count              INTEGER NOT NULL,
    l2_count              INTEGER NOT NULL,
    article_count         INTEGER NOT NULL,
    indexed_through_date  TEXT NOT NULL,
    corpus_hash           TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending'
);

-- Per-item index state: polymorphic across article/filing sources
-- corpus_item_id = article_id for articles; "sec:{cik}:{accession}" for filings
-- source_kind = 'article' | 'filing'
-- NO foreign key -- the column is polymorphic
CREATE TABLE IF NOT EXISTS index_state (
    corpus_item_id  TEXT NOT NULL,
    source_kind     TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    source_tier     INTEGER,
    dedup_group_id  TEXT,
    indexed_build_id TEXT,
    indexed_at      TEXT,
    PRIMARY KEY (corpus_item_id, source_kind)
);
CREATE INDEX IF NOT EXISTS idx_index_state_build ON index_state(indexed_build_id);

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



def ensure_clean_provenance(conn: sqlite3.Connection) -> None:
    """Add raw_asset_id provenance column and index to clean_assets (idempotent)."""
    try:
        conn.execute("ALTER TABLE clean_assets ADD COLUMN raw_asset_id TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_clean_raw_asset ON clean_assets(raw_asset_id)"
    )
    conn.commit()


def ensure_filings_tables(conn):
    """Create filings and filing_documents tables (idempotent)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS filings (
            filing_id         TEXT PRIMARY KEY,
            cik               TEXT NOT NULL,
            ticker            TEXT NOT NULL,
            form_type         TEXT NOT NULL,
            filed_at          TEXT NOT NULL,
            period            TEXT,
            accession_number  TEXT NOT NULL,
            primary_document  TEXT,
            url               TEXT NOT NULL,
            items_json        TEXT,
            source_tier       INTEGER NOT NULL DEFAULT 1,
            dedup_group_id    TEXT,
            is_canonical      INTEGER DEFAULT 1,
            is_rag_eligible   INTEGER DEFAULT 1,
            quality_score     REAL DEFAULT 1.0,
            raw_asset_id      TEXT,
            created_at        TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_filings_cik ON filings(cik);
        CREATE INDEX IF NOT EXISTS idx_filings_ticker_date ON filings(ticker, filed_at);
        CREATE INDEX IF NOT EXISTS idx_filings_form ON filings(form_type);
        CREATE INDEX IF NOT EXISTS idx_filings_accession ON filings(accession_number);

        CREATE TABLE IF NOT EXISTS filing_documents (
            filing_id         TEXT NOT NULL,
            document_url      TEXT NOT NULL,
            document_type     TEXT NOT NULL DEFAULT 'primary_doc',
            text              TEXT,
            char_len          INTEGER,
            content_type      TEXT,
            byte_size         INTEGER,
            extraction_status TEXT NOT NULL
                CHECK (extraction_status IN (
                    'success', 'empty', 'pdf_skipped', 'fetch_failed', 'timeout'
                )),
            extracted_at      TEXT,
            PRIMARY KEY (filing_id, document_url),
            FOREIGN KEY (filing_id) REFERENCES filings(filing_id)
        );
        CREATE INDEX IF NOT EXISTS idx_filing_docs_filing ON filing_documents(filing_id);
    """)
    conn.commit()


def upsert_filing(conn, *, filing_id, cik, ticker, form_type, filed_at,
                  accession_number, url, period=None, primary_document=None,
                  items_json=None, source_tier=1, dedup_group_id=None,
                  is_canonical=1, is_rag_eligible=1, quality_score=1.0,
                  raw_asset_id=None):
    """Insert or replace a filing row."""
    from catalyst_data.storage.sqlite import _now_iso
    conn.execute("""
        INSERT OR REPLACE INTO filings
            (filing_id, cik, ticker, form_type, filed_at, period,
             accession_number, primary_document, url, items_json,
             source_tier, dedup_group_id, is_canonical, is_rag_eligible,
             quality_score, raw_asset_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (filing_id, cik, ticker, form_type, filed_at, period,
          accession_number, primary_document, url, items_json,
          source_tier, dedup_group_id, is_canonical, is_rag_eligible,
          quality_score, raw_asset_id, _now_iso()))
    conn.commit()


def upsert_filing_document(conn, *, filing_id, document_url, document_type='primary_doc',
                           text=None, char_len=None, content_type=None,
                           byte_size=None, extraction_status='success'):
    """Insert or replace a filing_documents row."""
    from datetime import datetime, timezone
    extracted_at = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT OR REPLACE INTO filing_documents
            (filing_id, document_url, document_type, text, char_len,
             content_type, byte_size, extraction_status, extracted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (filing_id, document_url, document_type, text, char_len,
          content_type, byte_size, extraction_status, extracted_at))
    conn.commit()

def init_db(conn: sqlite3.Connection) -> None:
    """Set pragmas, create all tables and indexes."""
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    conn.executescript(_DDL)
    ensure_clean_provenance(conn)
    ensure_articles_table(conn)
    ensure_filings_tables(conn)
    ensure_macro_tables(conn)
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
    raw_asset_id: str | None = None,
) -> None:
    """Insert or replace a cleaned (Silver) asset."""
    conn.execute(
        """
        INSERT OR REPLACE INTO clean_assets
            (asset_id, ticker, source_type, reference_date, cleaned_at,
             content_md, title_hash, is_duplicate, raw_asset_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            raw_asset_id,
        ),
    )
    conn.commit()


def get_clean_asset(conn: sqlite3.Connection, asset_id: str) -> dict | None:
    """Fetch a cleaned asset by id."""
    row = conn.execute(
        """
        SELECT asset_id, ticker, source_type, reference_date, cleaned_at,
               content_md, title_hash, is_duplicate, raw_asset_id
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
        "raw_asset_id": row[8],
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


def _migrate_article_tickers_dedup(conn: sqlite3.Connection) -> None:
    """Add dedup_group_id column to article_tickers if not present (additive DDL)."""
    try:
        conn.execute("SELECT dedup_group_id FROM article_tickers LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE article_tickers ADD COLUMN dedup_group_id TEXT")
        conn.commit()


# ---------------------------------------------------------------------------
# macro_observations (Plane-2 — structured signals, NEVER embedded)
# ---------------------------------------------------------------------------

_MACRO_DDL = """
CREATE TABLE IF NOT EXISTS macro_observations (
    series_id        TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    value            REAL,
    released_at      TEXT,
    fetched_at       TEXT NOT NULL DEFAULT (datetime('now')),
    raw_asset_id     TEXT,
    PRIMARY KEY (series_id, observation_date),
    FOREIGN KEY (raw_asset_id) REFERENCES raw_assets(asset_id)
);
CREATE INDEX IF NOT EXISTS idx_macro_obs_date
    ON macro_observations(observation_date);
CREATE INDEX IF NOT EXISTS idx_macro_obs_series_date
    ON macro_observations(series_id, observation_date);
"""


def ensure_macro_tables(conn: sqlite3.Connection) -> None:
    """Create macro_observations table and indexes (additive DDL)."""
    conn.executescript(_MACRO_DDL)
    conn.commit()


def upsert_macro_observation(
    conn: sqlite3.Connection,
    *,
    series_id: str,
    observation_date: str,
    value: float | None,
    released_at: str | None,
    raw_asset_id: str | None,
) -> None:
    """INSERT OR REPLACE into macro_observations."""
    conn.execute(
        """
        INSERT OR REPLACE INTO macro_observations
            (series_id, observation_date, value, released_at, fetched_at, raw_asset_id)
        VALUES (?, ?, ?, ?, datetime('now'), ?)
        """,
        (series_id, observation_date, value, released_at, raw_asset_id),
    )
    conn.commit()

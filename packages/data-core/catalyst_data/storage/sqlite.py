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

import enum
import hashlib
import json
import sqlite3
import zlib
from datetime import datetime, timezone

from catalyst_data.articles import ensure_articles_table

import os as _os

# ── Frozen DB guard (S3 Data Belt §0.1, D1) ──
from pathlib import Path as _Path
_REPO_ROOT = str(_Path(__file__).resolve().parents[4])
FROZEN_PATHS = frozenset({
    _os.path.realpath(_os.path.join(_REPO_ROOT, "data", "catalyst_eval_frozen_v2.db")),
})


class FrozenDBWriteError(RuntimeError):
    """Raised when a write operation targets a frozen (immutable) database."""
    pass


def _assert_not_frozen(db_path: str) -> None:
    """Raise FrozenDBWriteError if db_path resolves to a frozen database.

    Uses os.path.realpath to catch symlinks, copies, and renamed files.
    NEVER uses endswith() -- that misses renamed copies and future versions.
    """
    real = _os.path.realpath(db_path)
    if real in FROZEN_PATHS:
        raise FrozenDBWriteError(
            f"Refusing to write to frozen database: {real}. "
            f"Use a separate dev database for writes."
        )

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
    rowid            INTEGER PRIMARY KEY AUTOINCREMENT,
            chunk_id         TEXT NOT NULL,
    chunk_level      TEXT NOT NULL DEFAULT 'l1',
    corpus_item_id   TEXT NOT NULL,
    source_kind      TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    content_text     TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'pending',
    provider         TEXT,
    source_type      TEXT,
    source_tier      INTEGER,
    tickers_json     TEXT DEFAULT '[]',
    reference_date   TEXT,
    published_utc    TEXT,
    publisher_name   TEXT,
    publisher_logo_url TEXT,
    article_url      TEXT,
    image_url        TEXT,
    author           TEXT,
    dedup_group_id   TEXT,
    indexed_build_id TEXT,
    indexed_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_index_state_corpus ON index_state(corpus_item_id, source_kind);

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



# ---- W1-C: OHLCV source precedence ----
SOURCE_PRECEDENCE: dict[str, int] = {
    "polygon": 100,
    "yfinance": 50,
}


class UnknownOHLCVSource(Exception):
    """Source has no entry in SOURCE_PRECEDENCE — fail closed."""


class UpsertOutcome(enum.Enum):
    BLOCKED = "blocked"       # lower rank — row unchanged
    UPDATED = "updated"       # equal or higher rank — row inserted/updated

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
                  raw_asset_id=None, commit: bool = True):
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
    if commit:
        conn.commit()


def upsert_filing_document(conn, *, filing_id, document_url, document_type='primary_doc',
                           text=None, char_len=None, content_type=None,
                           byte_size=None, extraction_status='success',
                           commit: bool = True):
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
    if commit:
        conn.commit()


# ── corpus_items VIEW (S3 Data Belt §0.1, D1) ──
_CORPUS_ITEMS_VIEW = """
CREATE VIEW corpus_items AS
-- Articles branch: one row per article_id x ticker (INNER JOIN article_tickers)
-- content_md byte-identical to index_builder L1: title || char(10) || COALESCE(description, '')
SELECT
    a.article_id AS corpus_item_id,
    'article'     AS source_kind,
    at.ticker,
    a.provider,
    a.source_type,
    at.reference_date,
    a.published_utc,
    a.title || char(10) || COALESCE(a.description, '') AS content_md,
    a.title,
    a.article_url,
    a.publisher_name,
    a.source_tier,
    at.dedup_group_id,
    at.is_canonical,
    a.is_rag_eligible,
    'l1'          AS chunk_level
FROM articles a
INNER JOIN article_tickers at ON a.article_id = at.article_id
UNION ALL
-- Filings branch: one row per is_rag_eligible filing with best-document selection
-- content_md matches index_builder filing L1
-- ROW_NUMBER() window function reproduces document preference (exhibit_99_1 over primary_doc)
SELECT
    ranked.filing_id AS corpus_item_id,
    'filing'      AS source_kind,
    ranked.ticker,
    'sec'         AS provider,
    'sec_filing'  AS source_type,
    ranked.filed_at AS reference_date,
    ranked.filed_at AS published_utc,
    CASE
        WHEN ranked.doc_text IS NOT NULL AND ranked.doc_text != ''
        THEN (ranked.form_type || ' filed ' || ranked.filed_at) || char(10) || ranked.doc_text
        ELSE (ranked.form_type || ' filed ' || ranked.filed_at)
    END AS content_md,
    ranked.form_type AS title,
    ranked.url AS article_url,
    'SEC'         AS publisher_name,
    ranked.source_tier,
    ranked.dedup_group_id,
    ranked.is_canonical,
    ranked.is_rag_eligible,
    'l1'          AS chunk_level
FROM (
    SELECT
        f.filing_id,
        f.ticker,
        f.form_type,
        f.filed_at,
        f.url,
        f.source_tier,
        f.dedup_group_id,
        f.is_canonical,
        f.is_rag_eligible,
        fd.text AS doc_text,
        fd.extraction_status,
        ROW_NUMBER() OVER (
            PARTITION BY f.filing_id
            ORDER BY CASE fd.document_type WHEN 'exhibit_99_1' THEN 0 ELSE 1 END,
                     fd.document_type
        ) AS doc_rank
    FROM filings f
    LEFT JOIN filing_documents fd
        ON f.filing_id = fd.filing_id
        AND fd.extraction_status = 'success'
    WHERE f.is_rag_eligible = 1
) ranked
WHERE ranked.doc_rank = 1
"""


def _get_conn_path(conn):
    """Extract the database file path from a sqlite3 connection.

    Returns None for in-memory databases (':memory:' or '').
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row:
            db_name = row[2]
            if db_name and db_name not in ("", "main", ":memory:"):
                return db_name
        return None
    except Exception:
        return None


def _ensure_corpus_items_view(conn):
    """Create or refresh the corpus_items VIEW.

    Drops any existing corpus_items VIEW and re-creates it with the
    current definition.  This ensures dev DBs always use the corrected
    SQL even if a stale VIEW was created by an older code version.

    Raises FrozenDBWriteError if conn points to a frozen database.
    """
    db_path = _get_conn_path(conn)
    if db_path:
        _assert_not_frozen(db_path)
    conn.execute("DROP VIEW IF EXISTS corpus_items")
    conn.executescript(_CORPUS_ITEMS_VIEW)
    conn.commit()

def init_db(conn: sqlite3.Connection) -> None:
    """Set pragmas, create all tables and indexes."""
    # S3 Data Belt: frozen DB guard — must be FIRST, before any PRAGMA/DDL
    db_path = _get_conn_path(conn)
    if db_path:
        _assert_not_frozen(db_path)
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    conn.executescript(_DDL)
    ensure_clean_provenance(conn)
    ensure_articles_table(conn)
    ensure_filings_tables(conn)
    ensure_macro_tables(conn)
    _ensure_corpus_items_view(conn)
    # Fold quality tables into init_db so migrations can see them (H4-F5)
    from catalyst_data.quality import _QUALITY_TABLES_SQL
    conn.executescript(_QUALITY_TABLES_SQL)
    from catalyst_data.migrations import run_migrations
    run_migrations(conn)
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
    commit: bool = True,
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
    if commit:
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
    commit: bool = True,
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
    if commit:
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
    commit: bool = True,
):
    """Precedence-aware OHLCV upsert (W1-C).

    Runs inside the caller's existing transaction.  When commit=False, the
    caller controls commit/rollback.  Never issues BEGIN IMMEDIATE.
    """
    rank = SOURCE_PRECEDENCE.get(source)
    if rank is None:
        raise UnknownOHLCVSource(f"Unknown OHLCV source: {source}")

    existing = conn.execute(
        "SELECT source FROM ohlcv WHERE symbol=? AND date=?",
        (symbol, date),
    ).fetchone()

    if existing:
        existing_rank = SOURCE_PRECEDENCE.get(existing[0])
        if existing_rank is None:
            raise UnknownOHLCVSource(
                f"Unknown persisted OHLCV source: {existing[0]}"
            )
        if rank < existing_rank:
            return UpsertOutcome.BLOCKED

    conn.execute(
        """
        INSERT INTO ohlcv (symbol, date, open, high, low, close, volume, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, date) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume, source=excluded.source
        """,
        (symbol, date, open, high, low, close, volume, source),
    )
    if commit:
        conn.commit()
    return UpsertOutcome.UPDATED


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



def _migrate_index_state_step4a(conn):
    """Migrate index_state from old (corpus_item_id PK) to new (chunk_id PK) schema.
    
    Safe when index_state is empty. If rows exist, raises to prevent data loss.
    Callers: persist_index_state in index_builder.py.
    """
    # Check if chunk_id column already exists — if so, already migrated
    try:
        conn.execute("SELECT chunk_id FROM index_state LIMIT 0")
        return  # Already migrated
    except sqlite3.OperationalError:
        pass
    
    # Migration needed. Refuse if rows exist (data loss risk).
    row_count = conn.execute("SELECT COUNT(*) FROM index_state").fetchone()[0]
    if row_count > 0:
        raise RuntimeError(
            f"index_state has {row_count} rows but old schema. Manual migration required."
        )
    
    # Drop and recreate with new schema — safe because row_count == 0
    conn.execute("DROP TABLE IF EXISTS index_state")
    conn.execute("DROP INDEX IF EXISTS idx_index_state_build")
    conn.execute("DROP INDEX IF EXISTS idx_index_state_corpus")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS index_state (
            rowid            INTEGER PRIMARY KEY AUTOINCREMENT,
            chunk_id         TEXT NOT NULL,
            chunk_level      TEXT NOT NULL DEFAULT 'l1',
            corpus_item_id   TEXT NOT NULL,
            source_kind      TEXT NOT NULL,
            content_hash     TEXT NOT NULL,
            content_text     TEXT NOT NULL DEFAULT '',
            status           TEXT NOT NULL DEFAULT 'pending',
            provider         TEXT,
            source_type      TEXT,
            source_tier      INTEGER,
            tickers_json     TEXT DEFAULT '[]',
            reference_date   TEXT,
            published_utc    TEXT,
            publisher_name   TEXT,
            publisher_logo_url TEXT,
            article_url      TEXT,
            image_url        TEXT,
            author           TEXT,
            dedup_group_id   TEXT,
            indexed_build_id TEXT,
            indexed_at       TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_index_state_corpus ON index_state(corpus_item_id, source_kind)"
    )
    conn.commit()

def _migrate_article_tickers_dedup(conn: sqlite3.Connection) -> None:
    """Add dedup_group_id and is_canonical columns to article_tickers if missing (additive)."""
    for col_spec in [
        ("dedup_group_id", "TEXT"),
        ("is_canonical", "INTEGER NOT NULL DEFAULT 1"),
    ]:
        col_name = col_spec[0]
        try:
            conn.execute(f"SELECT {col_name} FROM article_tickers LIMIT 0")
        except sqlite3.OperationalError:
            conn.execute(f"ALTER TABLE article_tickers ADD COLUMN {col_name} {col_spec[1]}")
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

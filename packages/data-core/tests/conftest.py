"""Shared B2 test fixtures — isolated DB helpers, table inspection, seed helpers."""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from pathlib import Path

import pytest

from catalyst_data.migrations import MIGRATIONS, run_migrations as _run_migrations


def _fresh_db_at_version(target_version: int, *, foreign_keys: bool = True) -> sqlite3.Connection:
    """Create a fresh in-memory DB, create base tables, apply migrations up to target_version.

    Sets user_version to target_version so run_migrations skips migrations <= target_version.
    Target 0 means no migrations (base schema only).
    For migrations to actually apply, call with target_version = the max desired version,
    which sets user_version just below that point.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys = ON")

    # Base tables required by migrations v1-v7
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS source_checkpoints (
            run_id TEXT, source_type TEXT, ticker TEXT, date TEXT,
            status TEXT, error_class TEXT, retries INTEGER DEFAULT 0,
            PRIMARY KEY (run_id, source_type, ticker, date)
        );
        CREATE TABLE IF NOT EXISTS ingestion_runs (
            run_id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'PLANNED',
            started_at TEXT,
            ended_at TEXT
        );
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
        CREATE TABLE IF NOT EXISTS ohlcv (
            symbol TEXT, date TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            source TEXT,
            PRIMARY KEY (symbol, date, source)
        );
        CREATE TABLE IF NOT EXISTS articles (
            article_id TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            published_utc TEXT,
            source TEXT,
            publisher TEXT,
            article_url TEXT,
            image_url TEXT
        );
        CREATE TABLE IF NOT EXISTS article_tickers (
            article_id TEXT,
            ticker TEXT,
            reference_date TEXT,
            PRIMARY KEY (article_id, ticker)
        );
        CREATE TABLE IF NOT EXISTS clean_assets (
            asset_id TEXT PRIMARY KEY,
            ticker TEXT,
            reference_date TEXT,
            source_type TEXT,
            data TEXT,
            fetched_at TEXT
        );
        CREATE TABLE IF NOT EXISTS index_state (
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
    """)
    conn.commit()

    if target_version > 0:
        # Set user_version = target_version - 1 so run_migrations applies
        # migrations with version > user_version, i.e., migration v=target_version
        # through the latest. But run_migrations applies ALL pending, so to
        # apply ONLY up to target_version, we temporarily cap the list.
        import copy
        from catalyst_data.migrations import MIGRATIONS as _MIGRATIONS

        # Apply migrations version by version
        for mig in sorted(_MIGRATIONS, key=lambda m: m.version):
            if mig.version > target_version:
                break
            current_v = conn.execute("PRAGMA user_version").fetchone()[0]
            if mig.version <= current_v:
                continue
            # v2 special case
            if mig.version == 2:
                from catalyst_data.quality import _reconcile_source_checkpoints_check
                _reconcile_source_checkpoints_check(conn)
                conn.execute(f"PRAGMA user_version = {mig.version}")
                continue
            import sqlite3 as _sqlite3
            for stmt in mig.statements:
                if stmt.strip().startswith("--"):
                    continue
                try:
                    conn.execute(stmt)
                except _sqlite3.OperationalError as exc:
                    err = str(exc).lower()
                    if "duplicate column name" in err or "already exists" in err:
                        continue
                    raise
            conn.execute(f"PRAGMA user_version = {mig.version}")

    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current < target_version:
        raise RuntimeError(
            f"Could not reach target version {target_version}; current={current}"
        )
    return conn

def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return set of column names for a table."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    """Return set of table names in the database."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r["name"] for r in rows}


def _seed_v2_raw_row(
    conn: sqlite3.Connection,
    *,
    raw_asset_id: str = "raw:req-test-001",
    request_id: str = "req-test-001",
    response_sha256: str | None = None,
    content_encoding: str = "identity",
    page_no: int = 1,
    ticker: str = "AAPL",
    reference_date: str = "2026-01-01",
) -> str:
    """Seed a v2-format raw row for FK satisfaction in provenance tests."""
    if response_sha256 is None:
        response_sha256 = hashlib.sha256(b"test-payload").hexdigest()
    import json as _json
    metadata = _json.dumps({"content_encoding": content_encoding, "page_no": page_no})
    conn.execute(
        """INSERT INTO raw_assets
           (asset_id, request_id, response_sha256, content_encoding, page_no, ticker, reference_date, source_type, data_version, content_raw, metadata_json, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'news', 'v2', ?, ?, '2026-01-01T00:00:00Z')""",
        (raw_asset_id, request_id, response_sha256, content_encoding, page_no, ticker, reference_date, b"test-payload", metadata),
    )
    conn.commit()
    return raw_asset_id


def _seed_ingestion_run(
    conn: sqlite3.Connection,
    *,
    run_id: str = "run-test-001",
    status: str = "PLANNED",
) -> str:
    """Seed an ingestion_runs row (parent for FK references)."""
    conn.execute(
        "INSERT OR REPLACE INTO ingestion_runs (run_id, status, started_at) VALUES (?, ?, '2026-01-01T00:00:00Z')",
        (run_id, status),
    )
    conn.commit()
    return run_id


# Shared base attempt dict for contract-valid test data
BASE_ATTEMPT = {
    "request_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "run_id": "run-test-001",
    "logical_fetch_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "source_type": "news",
    "provider": "polygon",
    "endpoint_name": "polygon_news",
    "ticker_or_series": "AAPL",
    "window_start": "2026-01-01",
    "window_end": "2026-01-01",
    "attempt_no": 1,
    "page_no": 1,
    "parent_request_id": None,
    "request_fingerprint": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "request_params_redacted": '{"ticker":"AAPL","limit":50}',
    "cursor_fingerprint": None,
    "started_at": "2026-01-01T09:30:00Z",
    "completed_at": None,
    "status": "STARTED",
}

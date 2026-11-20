from __future__ import annotations

import re
import sqlite3

_QUALITY_REASONS = (
    "short_text",
    "missing_fields",
    "non_target_language",
    "template_spam",
)

_VALID_CHECKPOINT_STATUSES = frozenset({"pending", "success", "success_empty", "failed", "skipped"})

_QUALITY_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id               TEXT PRIMARY KEY,
    started_at           TEXT NOT NULL,
    ended_at             TEXT,
    ticker_list_json     TEXT NOT NULL,
    source_list_json     TEXT NOT NULL,
    status               TEXT NOT NULL,
    success_count        INTEGER NOT NULL DEFAULT 0,
    fail_count           INTEGER NOT NULL DEFAULT 0,
    cost_usd             REAL,
    notes                TEXT,
    current_source       TEXT,
    current_ticker       TEXT,
    current_date         TEXT,
    cells_total          INTEGER DEFAULT 0,
    cells_done           INTEGER DEFAULT 0,
    canceled_at          TEXT,
    report_path          TEXT,
    run_config_json      TEXT
);

CREATE TABLE IF NOT EXISTS source_checkpoints (
    run_id               TEXT NOT NULL,
    source_type          TEXT NOT NULL,
    ticker               TEXT NOT NULL,
    date                 TEXT NOT NULL,
    status               TEXT NOT NULL,
    error_class          TEXT,
    retries              INTEGER NOT NULL DEFAULT 0,
    error_message_redacted TEXT,
    http_status          INTEGER,
    retry_after_seconds  REAL,
    provider_latency_ms  REAL,
    raw_asset_id         TEXT,
    items_count          INTEGER,
    fallback_provider    TEXT,
    fallback_triggered   INTEGER DEFAULT 0,
    empty_reason         TEXT,
    PRIMARY KEY (run_id, source_type, ticker, date)
);

CREATE TABLE IF NOT EXISTS asset_quality_flags (
    asset_id             TEXT PRIMARY KEY,
    is_rag_eligible      INTEGER NOT NULL,
    quality_reason       TEXT CHECK (
        quality_reason IS NULL OR quality_reason IN {_quality_reasons}
    ),
    quality_score        REAL NOT NULL,
    evaluated_at         TEXT NOT NULL,
    FOREIGN KEY (asset_id) REFERENCES clean_assets(asset_id)
);
""".format(_quality_reasons=_QUALITY_REASONS)


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

def normalize_title(title: str | None) -> str | None:
    if not title:
        return None
    normalized = _NON_ALNUM_RE.sub(" ", title.lower()).strip()
    return normalized or None


def _reconcile_source_checkpoints_check(conn: sqlite3.Connection) -> None:
    """Remove legacy CHECK constraint from source_checkpoints if present.

    Idempotent — running twice no-ops on the second run.  Preserves all
    existing rows and the PRIMARY KEY.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='source_checkpoints'"
    ).fetchone()
    if row is None:
        return

    sql_text = row[0] or ""
    if "CHECK (" not in sql_text.upper():
        return

    # Discover which columns exist in the current table
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(source_checkpoints)").fetchall()}

    FULL_COLUMNS = [
        "run_id", "source_type", "ticker", "date", "status",
        "error_class", "retries", "error_message_redacted", "http_status",
        "retry_after_seconds", "provider_latency_ms", "raw_asset_id", "items_count",
        "fallback_provider", "fallback_triggered", "empty_reason",
    ]

    select_parts = []
    insert_cols = []
    for col in FULL_COLUMNS:
        insert_cols.append(col)
        if col in existing_cols:
            select_parts.append(col)
        else:
            select_parts.append(f"NULL AS {col}")

    insert_cols_str = ", ".join(insert_cols)
    select_str = ", ".join(select_parts)

    conn.executescript(f"""
        CREATE TABLE source_checkpoints_new (
            run_id               TEXT NOT NULL,
            source_type          TEXT NOT NULL,
            ticker               TEXT NOT NULL,
            date                 TEXT NOT NULL,
            status               TEXT NOT NULL,
            error_class          TEXT,
            retries              INTEGER NOT NULL DEFAULT 0,
            error_message_redacted TEXT,
            http_status          INTEGER,
            retry_after_seconds  REAL,
            provider_latency_ms  REAL,
            raw_asset_id         TEXT,
            items_count          INTEGER,
            fallback_provider    TEXT,
            fallback_triggered   INTEGER DEFAULT 0,
            empty_reason         TEXT,
            PRIMARY KEY (run_id, source_type, ticker, date)
        );

        INSERT INTO source_checkpoints_new
            ({insert_cols_str})
        SELECT {select_str}
        FROM source_checkpoints;

        DROP TABLE source_checkpoints;

        ALTER TABLE source_checkpoints_new RENAME TO source_checkpoints;

        CREATE INDEX IF NOT EXISTS idx_source_checkpoints_run
            ON source_checkpoints(run_id);
    """)

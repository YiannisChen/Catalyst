"""Ordered migration registry keyed by PRAGMA user_version (v6 = S3 timestamp canonicalization).

H4 owns this module.  All schema changes after initial init_db DDL are
registered here and applied sequentially by run_migrations().

Per-statement idempotent: catches OperationalError per ALTER statement
and skips "duplicate column name" errors.  Non-duplicate errors re-raise.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: list[str]
    reversible: bool = False


MIGRATIONS: list[Migration] = [
    Migration(version=1, name="h2_checkpoint_columns", statements=[
        "ALTER TABLE source_checkpoints ADD COLUMN error_message_redacted TEXT",
        "ALTER TABLE source_checkpoints ADD COLUMN http_status INTEGER",
        "ALTER TABLE source_checkpoints ADD COLUMN retry_after_seconds REAL",
        "ALTER TABLE source_checkpoints ADD COLUMN provider_latency_ms REAL",
        "ALTER TABLE source_checkpoints ADD COLUMN raw_asset_id TEXT",
        "ALTER TABLE source_checkpoints ADD COLUMN items_count INTEGER",
    ]),
    Migration(version=2, name="h2_drop_legacy_status_check", statements=[
        "-- h2_drop_legacy_status_check: calls _reconcile_source_checkpoints_check (table rebuild)",
    ]),
    Migration(version=3, name="h2_fetch_result_error_class", statements=[
        "-- No DDL change. FetchResult.error_class is Python-only.",
    ]),
    Migration(version=4, name="h3_ingestion_run_progress", statements=[
        "ALTER TABLE ingestion_runs ADD COLUMN current_source TEXT",
        "ALTER TABLE ingestion_runs ADD COLUMN current_ticker TEXT",
        "ALTER TABLE ingestion_runs ADD COLUMN current_date TEXT",
        "ALTER TABLE ingestion_runs ADD COLUMN cells_total INTEGER DEFAULT 0",
        "ALTER TABLE ingestion_runs ADD COLUMN cells_done INTEGER DEFAULT 0",
        "ALTER TABLE ingestion_runs ADD COLUMN canceled_at TEXT",
        "ALTER TABLE ingestion_runs ADD COLUMN report_path TEXT",
        "ALTER TABLE ingestion_runs ADD COLUMN run_config_json TEXT",
    ]),
    Migration(version=5, name="h3_fallback_checkpoint_columns", statements=[
        "ALTER TABLE source_checkpoints ADD COLUMN fallback_provider TEXT",
        "ALTER TABLE source_checkpoints ADD COLUMN fallback_triggered INTEGER DEFAULT 0",
    ]),
    Migration(version=6, name="s3_timestamp_canonical", statements=[
        "UPDATE article_tickers SET reference_date = REPLACE(reference_date, '+00:00', 'Z') WHERE reference_date LIKE '%+00:00'",
        "UPDATE articles SET published_utc = REPLACE(published_utc, '+00:00', 'Z') WHERE published_utc LIKE '%+00:00'",
        "UPDATE clean_assets SET reference_date = REPLACE(reference_date, '+00:00', 'Z') WHERE reference_date LIKE '%+00:00'",
        "UPDATE raw_assets SET reference_date = REPLACE(reference_date, '+00:00', 'Z') WHERE reference_date LIKE '%+00:00'",
    ], reversible=False),
]


def run_migrations(conn: sqlite3.Connection) -> int:
    """Apply all pending migrations in version order.  Idempotent.

    Catches OperationalError per ALTER statement and skips "duplicate column"
    errors.  Non-duplicate errors propagate.  Always bumps user_version to
    the max applied version.

    Returns the final PRAGMA user_version value.
    """
    # S3 Data Belt: frozen DB guard — refuse writes before any PRAGMA/DDL
    from catalyst_data.storage.sqlite import _get_conn_path, _assert_not_frozen
    db_path = _get_conn_path(conn)
    if db_path:
        _assert_not_frozen(db_path)

    current = conn.execute("PRAGMA user_version").fetchone()[0]

    for migration in sorted(MIGRATIONS, key=lambda m: m.version):
        if migration.version <= current:
            continue

        # Special case: v2 calls the reconcile function to rebuild source_checkpoints
        if migration.version == 2:
            from catalyst_data.quality import _reconcile_source_checkpoints_check
            _reconcile_source_checkpoints_check(conn)
            conn.execute(f"PRAGMA user_version = {migration.version}")
            logger.info("Applied migration v%d (%s)", migration.version, migration.name)
            continue

        for stmt in migration.statements:
            if stmt.strip().startswith("--"):
                continue
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                err = str(exc).lower()
                if "duplicate column name" in err or "already exists" in err or "no such table" in err:
                    logger.debug(
                        "Migration v%d: column already exists, skipping: %s",
                        migration.version, stmt[:80],
                    )
                    continue
                raise

        conn.execute(f"PRAGMA user_version = {migration.version}")
        logger.info("Applied migration v%d (%s)", migration.version, migration.name)

    return conn.execute("PRAGMA user_version").fetchone()[0]

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
    Migration(version=7, name='source_checkpoints_add_empty_reason', statements=[
        'ALTER TABLE source_checkpoints ADD COLUMN empty_reason TEXT',
    ]),
    Migration(version=8, name="b2_request_provenance_and_run_control", statements=[
        # =================================================================
        # provider_request_attempts — full contract DDL §4.6
        # =================================================================
        """CREATE TABLE IF NOT EXISTS provider_request_attempts (
            request_id                  TEXT PRIMARY KEY,
            run_id                      TEXT NOT NULL,
            logical_fetch_id            TEXT NOT NULL,
            source_type                 TEXT NOT NULL,
            provider                    TEXT NOT NULL,
            endpoint_name               TEXT NOT NULL,
            ticker_or_series            TEXT NOT NULL,
            window_start                TEXT NOT NULL,
            window_end                  TEXT NOT NULL,
            attempt_no                  INTEGER NOT NULL CHECK (attempt_no >= 1),
            page_no                     INTEGER NOT NULL CHECK (page_no >= 1),
            parent_request_id           TEXT,
            request_fingerprint         TEXT NOT NULL
                CHECK (length(request_fingerprint) = 64
                       AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
            request_params_redacted     TEXT NOT NULL DEFAULT '{}'
                CHECK (json_valid(request_params_redacted)),
            cursor_fingerprint          TEXT
                CHECK (cursor_fingerprint IS NULL OR
                       (length(cursor_fingerprint) = 64
                        AND cursor_fingerprint NOT GLOB '*[^0-9a-f]*')),
            started_at                  TEXT NOT NULL,
            completed_at                TEXT,
            status                      TEXT NOT NULL CHECK (status IN (
                'STARTED', 'SUCCEEDED', 'HTTP_ERROR', 'TRANSPORT_ERROR',
                'TIMEOUT', 'RATE_LIMITED', 'AUTH_ERROR', 'PARSE_ERROR', 'CANCELLED'
            )),
            http_status                 INTEGER CHECK (
                http_status IS NULL OR http_status BETWEEN 100 AND 599
            ),
            latency_ms                  REAL CHECK (latency_ms IS NULL OR latency_ms >= 0),
            items_count                INTEGER CHECK (items_count IS NULL OR items_count >= 0),
            retry_after_seconds         REAL CHECK (
                retry_after_seconds IS NULL OR retry_after_seconds >= 0
            ),
            rate_limit_remaining        INTEGER CHECK (
                rate_limit_remaining IS NULL OR rate_limit_remaining >= 0
            ),
            provider_request_id         TEXT,
            error_class                 TEXT,
            error_message_redacted      TEXT,
            raw_asset_id                TEXT,
            response_sha256             TEXT CHECK (
                response_sha256 IS NULL OR
                (length(response_sha256) = 64
                 AND response_sha256 NOT GLOB '*[^0-9a-f]*')
            ),
            response_bytes              INTEGER CHECK (
                response_bytes IS NULL OR response_bytes >= 0
            ),
            UNIQUE (logical_fetch_id, attempt_no, page_no),
            FOREIGN KEY (run_id) REFERENCES ingestion_runs(run_id) ON DELETE RESTRICT,
            FOREIGN KEY (parent_request_id) REFERENCES provider_request_attempts(request_id)
                ON DELETE RESTRICT,
            FOREIGN KEY (raw_asset_id) REFERENCES raw_assets(asset_id) ON DELETE RESTRICT
        )""",

        "CREATE INDEX IF NOT EXISTS idx_request_attempts_run ON provider_request_attempts(run_id, started_at, request_id)",
        "CREATE INDEX IF NOT EXISTS idx_request_attempts_fetch ON provider_request_attempts(logical_fetch_id, page_no, attempt_no)",
        "CREATE INDEX IF NOT EXISTS idx_request_attempts_status ON provider_request_attempts(status, provider, started_at)",

        # =================================================================
        # normalized_provenance — contract DDL §4.6
        # =================================================================
        """CREATE TABLE IF NOT EXISTS normalized_provenance (
            entity_type        TEXT NOT NULL CHECK (entity_type IN (
                'article', 'filing', 'macro_observation', 'ohlcv',
                'fundamental_snapshot'
            )),
            entity_id          TEXT NOT NULL,
            entity_version     TEXT NOT NULL
                CHECK (length(entity_version) = 64
                       AND entity_version NOT GLOB '*[^0-9a-f]*'),
            raw_asset_id       TEXT NOT NULL,
            normalizer_version TEXT NOT NULL,
            created_at         TEXT NOT NULL,
            PRIMARY KEY (entity_type, entity_id, entity_version, raw_asset_id),
            FOREIGN KEY (raw_asset_id) REFERENCES raw_assets(asset_id) ON DELETE RESTRICT
        )""",

        "CREATE INDEX IF NOT EXISTS idx_normalized_provenance_raw ON normalized_provenance(raw_asset_id, entity_type, entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_normalized_provenance_entity ON normalized_provenance(entity_type, entity_id, entity_version)",

        # =================================================================
        # Additive columns and v2 raw-row rules
        # =================================================================
        "ALTER TABLE raw_assets ADD COLUMN response_sha256 TEXT",
        "ALTER TABLE raw_assets ADD COLUMN request_id TEXT",
        "ALTER TABLE raw_assets ADD COLUMN page_no INTEGER",
        "ALTER TABLE raw_assets ADD COLUMN content_encoding TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_assets_request_id ON raw_assets(request_id) WHERE request_id IS NOT NULL",

        "ALTER TABLE source_checkpoints ADD COLUMN logical_fetch_id TEXT",
        "ALTER TABLE source_checkpoints ADD COLUMN request_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE source_checkpoints ADD COLUMN pages_received INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE source_checkpoints ADD COLUMN items_received INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE source_checkpoints ADD COLUMN is_complete INTEGER NOT NULL DEFAULT 0",

        "ALTER TABLE ingestion_runs ADD COLUMN plan_hash TEXT",
        "ALTER TABLE ingestion_runs ADD COLUMN expected_plan_hash TEXT",
        "ALTER TABLE ingestion_runs ADD COLUMN allow_stale_ohlcv INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ingestion_runs ADD COLUMN allow_stale_ohlcv_overridden INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ingestion_runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ingestion_runs ADD COLUMN lease_holder TEXT",
        "ALTER TABLE ingestion_runs ADD COLUMN lease_expires_at TEXT",
        "ALTER TABLE ingestion_runs ADD COLUMN parent_run_id TEXT",

        # =================================================================
        # Triggers — exact names and abort codes per contract table
        # =================================================================

        # --- raw_assets v2 guards ---
        """CREATE TRIGGER IF NOT EXISTS trg_raw_assets_v2_insert_guard
           BEFORE INSERT ON raw_assets
           WHEN NEW.request_id IS NOT NULL
              AND (NEW.asset_id != 'raw:' || NEW.request_id
                   OR NEW.data_version != 'v2'
                   OR NEW.response_sha256 IS NULL
                   OR length(NEW.response_sha256) != 64
                   OR NEW.response_sha256 GLOB '*[^0-9a-f]*'
                   OR NEW.page_no IS NULL OR NEW.page_no < 1
                   OR NEW.content_encoding IS NULL)
        BEGIN
            SELECT RAISE(ABORT, 'raw_asset_v2_contract');
        END""",

        """CREATE TRIGGER IF NOT EXISTS trg_raw_assets_v2_update_guard
           BEFORE UPDATE ON raw_assets
           WHEN OLD.request_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'raw_asset_v2_immutable');
        END""",

        """CREATE TRIGGER IF NOT EXISTS trg_raw_assets_v2_delete_guard
           BEFORE DELETE ON raw_assets
           WHEN OLD.request_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'raw_asset_v2_immutable');
        END""",

        # --- request_attempt guards ---
        """CREATE TRIGGER IF NOT EXISTS trg_request_attempt_insert_guard
           BEFORE INSERT ON provider_request_attempts
           WHEN NEW.status != 'STARTED'
        BEGIN
            SELECT RAISE(ABORT, 'request_attempt_initial_status');
        END""",

        """CREATE TRIGGER IF NOT EXISTS trg_request_attempt_identity_guard
           BEFORE UPDATE ON provider_request_attempts
           WHEN OLD.request_id != NEW.request_id
              OR OLD.run_id != NEW.run_id
              OR OLD.logical_fetch_id != NEW.logical_fetch_id
              OR OLD.source_type != NEW.source_type
              OR OLD.provider != NEW.provider
              OR OLD.endpoint_name != NEW.endpoint_name
              OR OLD.ticker_or_series != NEW.ticker_or_series
              OR OLD.window_start != NEW.window_start
              OR OLD.window_end != NEW.window_end
              OR OLD.attempt_no != NEW.attempt_no
              OR OLD.page_no != NEW.page_no
              OR OLD.parent_request_id IS NOT NEW.parent_request_id
              OR OLD.request_fingerprint != NEW.request_fingerprint
              OR OLD.request_params_redacted != NEW.request_params_redacted
              OR OLD.started_at != NEW.started_at
        BEGIN
            SELECT RAISE(ABORT, 'request_attempt_identity_immutable');
        END""",

        """CREATE TRIGGER IF NOT EXISTS trg_request_attempt_transition_guard
           BEFORE UPDATE ON provider_request_attempts
           WHEN OLD.status != 'STARTED'
              OR NEW.completed_at IS NULL
              OR (OLD.status = 'STARTED' AND NEW.status NOT IN (
                  'SUCCEEDED', 'HTTP_ERROR', 'TRANSPORT_ERROR', 'TIMEOUT',
                  'RATE_LIMITED', 'AUTH_ERROR', 'PARSE_ERROR', 'CANCELLED'))
        BEGIN
            SELECT RAISE(ABORT, 'request_attempt_illegal_transition');
        END""",

        """CREATE TRIGGER IF NOT EXISTS trg_request_attempt_delete_guard
           BEFORE DELETE ON provider_request_attempts
        BEGIN
            SELECT RAISE(ABORT, 'request_attempt_append_only');
        END""",

        # --- checkpoint v2 guards ---
        """CREATE TRIGGER IF NOT EXISTS trg_checkpoint_v2_insert_guard
           BEFORE INSERT ON source_checkpoints
           WHEN NEW.logical_fetch_id IS NOT NULL
              AND (NEW.is_complete NOT IN (0, 1)
                   OR NEW.request_count < 0
                   OR NEW.pages_received < 0
                   OR NEW.items_received < 0)
        BEGIN
            SELECT RAISE(ABORT, 'checkpoint_v2_contract');
        END""",

        """CREATE TRIGGER IF NOT EXISTS trg_checkpoint_v2_update_guard
           BEFORE UPDATE ON source_checkpoints
           WHEN (NEW.request_count < 0
                 OR NEW.pages_received < 0
                 OR NEW.items_received < 0
                 OR NEW.is_complete NOT IN (0, 1))
        BEGIN
            SELECT RAISE(ABORT, 'checkpoint_v2_contract');
        END""",

        # --- ingestion_run v2 guards ---
        """CREATE TRIGGER IF NOT EXISTS trg_ingestion_run_v2_insert_guard
           BEFORE INSERT ON ingestion_runs
           WHEN NEW.plan_hash IS NOT NULL
              AND (NEW.status != 'PLANNED'
                   OR NEW.allow_stale_ohlcv NOT IN (0, 1)
                   OR NEW.allow_stale_ohlcv_overridden NOT IN (0, 1)
                   OR NEW.cancel_requested NOT IN (0, 1)
                   OR (NEW.parent_run_id = NEW.run_id))
        BEGIN
            SELECT RAISE(ABORT, 'ingestion_run_v2_contract');
        END""",

        """CREATE TRIGGER IF NOT EXISTS trg_ingestion_run_v2_transition_guard
           BEFORE UPDATE ON ingestion_runs
           WHEN OLD.plan_hash IS NOT NULL AND NEW.plan_hash IS NOT NULL
              AND (OLD.plan_hash != NEW.plan_hash
                   OR OLD.expected_plan_hash != NEW.expected_plan_hash
                   OR OLD.run_id != NEW.run_id
                   OR OLD.parent_run_id IS NOT NEW.parent_run_id)
        BEGIN
            SELECT RAISE(ABORT, 'ingestion_run_illegal_transition');
        END""",
    ]),

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
                if "duplicate column name" in err or "already exists" in err:
                    logger.debug(
                        "Migration v%d: column already exists, skipping: %s",
                        migration.version, stmt[:80],
                    )
                    continue
                raise

        conn.execute(f"PRAGMA user_version = {migration.version}")
        logger.info("Applied migration v%d (%s)", migration.version, migration.name)

    return conn.execute("PRAGMA user_version").fetchone()[0]

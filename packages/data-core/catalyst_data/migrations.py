"""Ordered migration registry keyed by PRAGMA user_version (v6 = S3 timestamp canonicalization).

H4 owns this module.  All schema changes after initial init_db DDL are
registered here and applied sequentially by run_migrations().

Per-statement idempotent: catches OperationalError per ALTER statement
and skips "duplicate column name" errors.  Non-duplicate errors re-raise.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass

logger = logging.getLogger(__name__)

CORPUS_CHUNKS_FTS_DDL = """CREATE VIRTUAL TABLE IF NOT EXISTS corpus_chunks_fts USING fts5(
    manifest_id UNINDEXED,
    chunk_id UNINDEXED,
    content_text,
    tokenize = 'unicode61 remove_diacritics 2'
)"""


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _reconcile_clean_assets_foreign_key(conn: sqlite3.Connection) -> None:
    """Replace the legacy clean-id foreign key with the raw provenance key."""
    foreign_keys = conn.execute("PRAGMA foreign_key_list(clean_assets)").fetchall()
    has_legacy_fk = any(
        row[2] == "raw_assets" and row[3] == "asset_id" and row[4] == "asset_id"
        for row in foreign_keys
    )
    if not has_legacy_fk:
        return

    columns = conn.execute("PRAGMA table_info(clean_assets)").fetchall()
    column_names = [row[1] for row in columns]
    if "raw_asset_id" not in column_names:
        raise sqlite3.OperationalError(
            "legacy clean_assets table is missing raw_asset_id"
        )

    declarations: list[str] = []
    for _, name, column_type, not_null, default_value, primary_key in columns:
        declaration = _quote_identifier(name)
        if column_type:
            declaration += f" {column_type}"
        if not_null:
            declaration += " NOT NULL"
        if default_value is not None:
            declaration += f" DEFAULT {default_value}"
        if primary_key:
            declaration += " PRIMARY KEY"
        declarations.append(declaration)
    declarations.append(
        "FOREIGN KEY (raw_asset_id) REFERENCES raw_assets(asset_id)"
    )

    conn.execute(
        f"CREATE TABLE clean_assets_v11 ({', '.join(declarations)})"
    )
    quoted_columns = ", ".join(_quote_identifier(name) for name in column_names)
    conn.execute(
        f"INSERT INTO clean_assets_v11 ({quoted_columns}) "
        f"SELECT {quoted_columns} FROM clean_assets"
    )
    conn.execute("DROP TABLE clean_assets")
    conn.execute("ALTER TABLE clean_assets_v11 RENAME TO clean_assets")

    if {"ticker", "reference_date"}.issubset(column_names):
        conn.execute(
            "CREATE INDEX idx_clean_ticker_date "
            "ON clean_assets(ticker, reference_date)"
        )
    if "title_hash" in column_names:
        conn.execute(
            "CREATE INDEX idx_clean_title_hash ON clean_assets(title_hash)"
        )
    conn.execute(
        "CREATE INDEX idx_clean_raw_asset ON clean_assets(raw_asset_id)"
    )
@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: list[str]
    reversible: bool = False


def _apply_migration_v12(conn: sqlite3.Connection) -> int:
    """Rebuild source_checkpoints with surrogate checkpoint_id PK.

    Old: PRIMARY KEY (run_id, source_type, ticker, date)
    New: checkpoint_id TEXT PRIMARY KEY (surrogate)
         + UNIQUE(run_id, cell_id)  (B2-O identity; NULLs distinct per SQLite)
         + UNIQUE(run_id, source_type, ticker, date) WHERE cell_id IS NULL  (legacy)

    Preserves ALL DEFAULT values, indexes, and trigger contracts.
    Executed within SAVEPOINT by run_migrations; on failure, v11 is restored.
    """
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_version >= 12:
        return current_version

    backup_count = conn.execute(
        "SELECT COUNT(*) FROM source_checkpoints"
    ).fetchone()[0]

    # Create the v12 table with explicit canonical DDL.
    conn.execute("""
        CREATE TABLE source_checkpoints_v12 (
            checkpoint_id        TEXT PRIMARY KEY,
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
            logical_fetch_id     TEXT,
            request_count        INTEGER NOT NULL DEFAULT 0,
            pages_received       INTEGER NOT NULL DEFAULT 0,
            items_received       INTEGER NOT NULL DEFAULT 0,
            is_complete          INTEGER NOT NULL DEFAULT 0,
            cell_id              TEXT,
            window_start         TEXT,
            window_end           TEXT,
            endpoint_name        TEXT,
            provider_profile_version TEXT,
            UNIQUE(run_id, cell_id)
        )
    """)

    # Copy data with deterministic checkpoint IDs.
    col_names = [
        "run_id", "source_type", "ticker", "date", "status", "error_class",
        "retries", "error_message_redacted", "http_status", "retry_after_seconds",
        "provider_latency_ms", "raw_asset_id", "items_count", "fallback_provider",
        "fallback_triggered", "empty_reason", "logical_fetch_id", "request_count",
        "pages_received", "items_received", "is_complete", "cell_id",
        "window_start", "window_end", "endpoint_name", "provider_profile_version",
    ]
    quoted_cols = ", ".join(f'"{c}"' for c in col_names)
    placeholders = ", ".join("?" for _ in col_names)

    all_rows = conn.execute(
        f"SELECT {quoted_cols} FROM source_checkpoints ORDER BY rowid"
    ).fetchall()

    insert_sql = (
        f"INSERT INTO source_checkpoints_v12 (checkpoint_id, {quoted_cols}) "
        f"VALUES (?, {placeholders})"
    )

    for row in all_rows:
        row_dict = dict(zip(col_names, row))
        id_parts = [
            str(row_dict.get("run_id", "") or ""),
            str(row_dict.get("source_type", "") or ""),
            str(row_dict.get("ticker", "") or ""),
            str(row_dict.get("date", "") or ""),
            str(row_dict.get("cell_id", "") or ""),
        ]
        identity = "|".join(id_parts)
        checkpoint_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
        conn.execute(insert_sql, (checkpoint_id, *row))

    new_count = conn.execute(
        "SELECT COUNT(*) FROM source_checkpoints_v12"
    ).fetchone()[0]
    if new_count != backup_count:
        raise RuntimeError(
            f"Migration v12: row count mismatch: {backup_count} -> {new_count}"
        )

    # Swap tables.
    conn.execute("DROP TABLE source_checkpoints")
    conn.execute(
        "ALTER TABLE source_checkpoints_v12 RENAME TO source_checkpoints"
    )

    # Recreate indexes.
    conn.execute(
        "CREATE INDEX idx_source_checkpoints_run "
        "ON source_checkpoints(run_id)"
    )
    conn.execute("""
        CREATE UNIQUE INDEX idx_source_checkpoints_legacy_identity
        ON source_checkpoints(run_id, source_type, ticker, date)
        WHERE cell_id IS NULL
    """)

    # Recreate the v11 trigger contracts.
    triggers = [
        """CREATE TRIGGER trg_checkpoint_v2_insert_guard
           BEFORE INSERT ON source_checkpoints
           WHEN NEW.logical_fetch_id IS NOT NULL
              AND (NEW.is_complete NOT IN (0, 1)
                   OR NEW.request_count < 0
                   OR NEW.pages_received < 0
                   OR NEW.items_received < 0)
        BEGIN
            SELECT RAISE(ABORT, \'checkpoint_v2_contract\');
        END""",
        """CREATE TRIGGER trg_checkpoint_v2_update_guard
           BEFORE UPDATE ON source_checkpoints
           WHEN (NEW.request_count < 0
                 OR NEW.pages_received < 0
                 OR NEW.items_received < 0
                 OR NEW.is_complete NOT IN (0, 1))
        BEGIN
            SELECT RAISE(ABORT, \'checkpoint_v2_contract\');
        END""",
        """CREATE TRIGGER trg_b2o_checkpoint_cell_identity_insert
           BEFORE INSERT ON source_checkpoints
           WHEN NEW.cell_id IS NOT NULL
              AND (NEW.window_start IS NULL
                   OR NEW.window_end IS NULL
                   OR NEW.endpoint_name IS NULL
                   OR NEW.provider_profile_version IS NULL
                   OR NEW.date != NEW.window_start
                   OR length(NEW.cell_id) != 64
                   OR NEW.cell_id != lower(NEW.cell_id)
                   OR NEW.cell_id GLOB \'*[^0-9a-f]*\')
        BEGIN
            SELECT RAISE(ABORT, \'b2o_checkpoint_cell_identity_contract\');
        END""",
        """CREATE TRIGGER trg_b2o_checkpoint_cell_identity_update
           BEFORE UPDATE ON source_checkpoints
           WHEN OLD.cell_id IS NOT NULL
              AND (OLD.cell_id IS NOT NEW.cell_id
                   OR OLD.window_start IS NOT NEW.window_start
                   OR OLD.window_end IS NOT NEW.window_end
                   OR OLD.endpoint_name IS NOT NEW.endpoint_name
                   OR OLD.provider_profile_version IS NOT NEW.provider_profile_version
                   OR NEW.date IS NOT NEW.window_start)
        BEGIN
            SELECT RAISE(ABORT, \'b2o_checkpoint_cell_identity_immutable\');
        END""",
        """CREATE TRIGGER trg_b2o_checkpoint_cell_identity_update_contract
           BEFORE UPDATE ON source_checkpoints
           WHEN OLD.cell_id IS NULL
              AND NEW.cell_id IS NOT NULL
              AND (NEW.window_start IS NULL
                   OR NEW.window_end IS NULL
                   OR NEW.endpoint_name IS NULL
                   OR NEW.provider_profile_version IS NULL
                   OR NEW.date IS NOT NEW.window_start
                   OR length(NEW.cell_id) != 64
                   OR NEW.cell_id != lower(NEW.cell_id)
                   OR NEW.cell_id GLOB \'*[^0-9a-f]*\')
        BEGIN
            SELECT RAISE(ABORT, \'b2o_checkpoint_cell_identity_contract\');
        END""",
    ]
    for trigger_sql in triggers:
        conn.execute(trigger_sql)

    return new_count


def _apply_migration_v13(conn: sqlite3.Connection) -> None:
    """Rebuild corpus_chunks for filing_v3; add filing_documents.document_id."""
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_version >= 13:
        return

    # Ensure corpus tables exist (v9) before rebuild.
    has_chunks = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='corpus_chunks'"
    ).fetchone()
    if has_chunks:
        before_count = conn.execute("SELECT COUNT(*) FROM corpus_chunks").fetchone()[0]
        col_defs = [
            (r[1], (r[2] or "").upper())
            for r in conn.execute("PRAGMA table_info(corpus_chunks)")
        ]
        cols = [c for c, _ in col_defs]
        col_types = dict(col_defs)
        # Typed full-row hash reusing snapshot identity encoding
        import hashlib as _hl
        from catalyst_data.manifests.snapshot import _typed_value
        from catalyst_data.manifests.universe import canonical_json_bytes

        def _full_row_hashes(table: str, columns: list[str]) -> list[str]:
            select = ", ".join(f'"{c}"' for c in columns)
            out: list[str] = []
            for row in conn.execute(
                f"SELECT {select} FROM {table} ORDER BY chunk_id"
            ):
                row_obj = {
                    col: _typed_value(row[idx], col_types.get(col, ""))
                    for idx, col in enumerate(columns)
                }
                # include declared types in row identity
                row_obj["_column_types"] = {c: col_types.get(c, "") for c in columns}
                out.append(
                    _hl.sha256(canonical_json_bytes(row_obj)).hexdigest()
                )
            return out

        before_hashes = _full_row_hashes("corpus_chunks", cols)
        before_pk = [
            r[0]
            for r in conn.execute(
                "SELECT chunk_id FROM corpus_chunks ORDER BY chunk_id"
            )
        ]
        before_agg = _hl.sha256("".join(before_hashes).encode("utf-8")).hexdigest()
        col_list = ", ".join(f'"{c}"' for c in cols)

        conn.execute("DROP TRIGGER IF EXISTS trg_corpus_chunks_insert_guard")
        conn.execute("DROP TRIGGER IF EXISTS trg_corpus_chunks_update_guard")

        conn.execute(
            """
            CREATE TABLE corpus_chunks_v13 (
                chunk_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                chunk_profile_version TEXT NOT NULL CHECK (
                    chunk_profile_version IN ('news_v2','filing_v2','filing_v3')
                ),
                section_key TEXT NOT NULL,
                ordinal TEXT NOT NULL CHECK (
                    length(ordinal) = 4 AND ordinal GLOB '[0-9][0-9][0-9][0-9]'
                ),
                content_text TEXT NOT NULL CHECK (length(content_text) > 0),
                content_hash TEXT NOT NULL CHECK (
                    length(content_hash) = 64 AND lower(content_hash) = content_hash
                ),
                metadata_hash TEXT NOT NULL CHECK (
                    length(metadata_hash) = 64 AND lower(metadata_hash) = metadata_hash
                ),
                source_class TEXT NOT NULL CHECK (source_class IN (
                    'structured_market_data','official_government','issuer_disclosure',
                    'corporate_press_release','reported_news','analysis_opinion',
                    'aggregated_unknown'
                )),
                dedup_cluster_id TEXT,
                cluster_first_available_at TEXT,
                representative_document_id TEXT,
                available_at TEXT NOT NULL,
                ticker_associations TEXT NOT NULL CHECK (json_valid(ticker_associations)),
                eligibility TEXT NOT NULL CHECK (eligibility IN ('eligible','ineligible')),
                manifest_id TEXT,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'active','pending_embedding','embedded','metadata_only','tombstoned'
                    )
                ),
                boundary_kind TEXT NOT NULL CHECK (
                    boundary_kind IN (
                        'document_end','paragraph','sentence','token_fallback'
                    )
                ),
                body_token_start INTEGER NOT NULL CHECK (body_token_start >= 0),
                body_token_end INTEGER NOT NULL CHECK (body_token_end > body_token_start),
                body_overlap_tokens INTEGER NOT NULL CHECK (
                    body_overlap_tokens BETWEEN 0 AND 48
                ),
                prefix_token_count INTEGER NOT NULL CHECK (
                    prefix_token_count BETWEEN 0 AND 64
                ),
                prefix_truncated INTEGER NOT NULL CHECK (prefix_truncated IN (0,1)),
                section_parse_degraded INTEGER NOT NULL DEFAULT 0 CHECK (
                    section_parse_degraded IN (0,1)
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(document_id, chunk_profile_version, section_key, ordinal),
                FOREIGN KEY (manifest_id) REFERENCES corpus_manifest(manifest_id)
            )
            """
        )
        conn.execute(
            f"INSERT INTO corpus_chunks_v13 ({col_list}) "
            f"SELECT {col_list} FROM corpus_chunks"
        )
        after_count = conn.execute(
            "SELECT COUNT(*) FROM corpus_chunks_v13"
        ).fetchone()[0]
        if after_count != before_count:
            raise RuntimeError(
                f"Migration v13 row count mismatch: {before_count} -> {after_count}"
            )
        after_cols = [r[1] for r in conn.execute("PRAGMA table_info(corpus_chunks_v13)")]
        # Columns must match for full-row compare (same logical columns)
        if after_cols != cols:
            # allow only if same set/order of logical fields
            if set(after_cols) != set(cols):
                raise RuntimeError(
                    f"Migration v13 column set changed: {cols} -> {after_cols}"
                )
        after_hashes = _full_row_hashes("corpus_chunks_v13", cols)
        after_pk = [
            r[0]
            for r in conn.execute(
                "SELECT chunk_id FROM corpus_chunks_v13 ORDER BY chunk_id"
            )
        ]
        if after_pk != before_pk:
            raise RuntimeError("Migration v13 ordered primary keys mismatch")
        after_agg = _hl.sha256("".join(after_hashes).encode("utf-8")).hexdigest()
        if after_agg != before_agg:
            raise RuntimeError("Migration v13 aggregate full-row hash mismatch")
        if after_hashes != before_hashes:
            raise RuntimeError("Migration v13 full-row logical hash mismatch after rebuild")

        conn.execute("DROP TABLE corpus_chunks")
        conn.execute("ALTER TABLE corpus_chunks_v13 RENAME TO corpus_chunks")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_corpus_chunks_document "
            "ON corpus_chunks(document_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_corpus_chunks_available "
            "ON corpus_chunks(available_at, chunk_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_corpus_chunks_source_class "
            "ON corpus_chunks(source_class, available_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_corpus_chunks_manifest "
            "ON corpus_chunks(manifest_id, status)"
        )

        conn.execute(
            """
            CREATE TRIGGER trg_corpus_chunks_insert_guard
               BEFORE INSERT ON corpus_chunks
               WHEN (
                   length(NEW.content_hash) != 64
                   OR NEW.content_hash != lower(NEW.content_hash)
                   OR NEW.content_hash GLOB '*[^0-9a-f]*'
                   OR length(NEW.metadata_hash) != 64
                   OR NEW.metadata_hash != lower(NEW.metadata_hash)
                   OR NEW.metadata_hash GLOB '*[^0-9a-f]*'
                   OR length(NEW.ordinal) != 4
                   OR NEW.ordinal NOT GLOB '[0-9][0-9][0-9][0-9]'
                   OR NOT json_valid(NEW.ticker_associations)
                   OR NEW.chunk_profile_version NOT IN (
                       'news_v2','filing_v2','filing_v3'
                   )
                   OR NEW.source_class NOT IN (
                       'structured_market_data','official_government',
                       'issuer_disclosure','corporate_press_release',
                       'reported_news','analysis_opinion','aggregated_unknown'
                   )
                   OR NEW.eligibility NOT IN ('eligible','ineligible')
                   OR NEW.status NOT IN (
                       'active','pending_embedding','embedded',
                       'metadata_only','tombstoned'
                   )
                   OR NEW.boundary_kind NOT IN (
                       'document_end','paragraph','sentence','token_fallback'
                   )
                   OR NEW.body_token_start < 0
                   OR NEW.body_token_end <= NEW.body_token_start
                   OR NEW.body_overlap_tokens < 0 OR NEW.body_overlap_tokens > 48
                   OR NEW.prefix_token_count < 0 OR NEW.prefix_token_count > 64
                   OR NEW.prefix_truncated NOT IN (0,1)
                   OR NEW.section_parse_degraded NOT IN (0,1)
               )
            BEGIN
                SELECT RAISE(ABORT, 'corpus_chunk_contract');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER trg_corpus_chunks_update_guard
               BEFORE UPDATE ON corpus_chunks
               WHEN OLD.status = 'embedded'
                  AND (
                      OLD.chunk_id != NEW.chunk_id
                      OR OLD.document_id != NEW.document_id
                      OR OLD.chunk_profile_version != NEW.chunk_profile_version
                      OR OLD.section_key != NEW.section_key
                      OR OLD.ordinal != NEW.ordinal
                      OR OLD.content_text != NEW.content_text
                  )
            BEGIN
                SELECT RAISE(ABORT, 'corpus_chunk_identity_immutable');
            END
            """
        )

    # filing_documents.document_id additive
    has_fd = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='filing_documents'"
    ).fetchone()
    if has_fd:
        fd_cols = {r[1] for r in conn.execute("PRAGMA table_info(filing_documents)")}
        if "document_id" not in fd_cols:
            conn.execute("ALTER TABLE filing_documents ADD COLUMN document_id TEXT")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_filing_documents_document_id
            ON filing_documents(document_id)
            WHERE document_id IS NOT NULL
            """
        )
        conn.execute("DROP TRIGGER IF EXISTS trg_filing_documents_document_id_insert_guard")
        conn.execute("DROP TRIGGER IF EXISTS trg_filing_documents_document_id_update_guard")
        conn.execute(
            """
            CREATE TRIGGER trg_filing_documents_document_id_insert_guard
               BEFORE INSERT ON filing_documents
               WHEN NEW.document_id IS NOT NULL
                  AND (
                      length(NEW.document_id) != 64
                      OR NEW.document_id != lower(NEW.document_id)
                      OR NEW.document_id GLOB '*[^0-9a-f]*'
                  )
            BEGIN
                SELECT RAISE(ABORT, 'filing_document_id_contract');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER trg_filing_documents_document_id_update_guard
               BEFORE UPDATE ON filing_documents
               WHEN OLD.document_id IS NOT NULL
                  AND OLD.document_id IS NOT NEW.document_id
            BEGIN
                SELECT RAISE(ABORT, 'filing_document_id_immutable');
            END
            """
        )

    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        raise RuntimeError(f"Migration v13 foreign_key_check failed: {fk}")


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

    Migration(version=9, name="b3_corpus_tables", statements=[
        """CREATE TABLE IF NOT EXISTS corpus_chunks (
            chunk_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            chunk_profile_version TEXT NOT NULL CHECK (chunk_profile_version IN ('news_v2','filing_v2')),
            section_key TEXT NOT NULL,
            ordinal TEXT NOT NULL CHECK (length(ordinal) = 4 AND ordinal GLOB '[0-9][0-9][0-9][0-9]'),
            content_text TEXT NOT NULL CHECK (length(content_text) > 0),
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64 AND lower(content_hash) = content_hash),
            metadata_hash TEXT NOT NULL CHECK (length(metadata_hash) = 64 AND lower(metadata_hash) = metadata_hash),
            source_class TEXT NOT NULL CHECK (source_class IN (
                'structured_market_data','official_government','issuer_disclosure',
                'corporate_press_release','reported_news','analysis_opinion','aggregated_unknown'
            )),
            dedup_cluster_id TEXT,
            cluster_first_available_at TEXT,
            representative_document_id TEXT,
            available_at TEXT NOT NULL,
            ticker_associations TEXT NOT NULL CHECK (json_valid(ticker_associations)),
            eligibility TEXT NOT NULL CHECK (eligibility IN ('eligible','ineligible')),
            manifest_id TEXT,
            status TEXT NOT NULL CHECK (status IN ('active','pending_embedding','embedded','metadata_only','tombstoned')),
            boundary_kind TEXT NOT NULL CHECK (boundary_kind IN ('document_end','paragraph','sentence','token_fallback')),
            body_token_start INTEGER NOT NULL CHECK (body_token_start >= 0),
            body_token_end INTEGER NOT NULL CHECK (body_token_end > body_token_start),
            body_overlap_tokens INTEGER NOT NULL CHECK (body_overlap_tokens BETWEEN 0 AND 48),
            prefix_token_count INTEGER NOT NULL CHECK (prefix_token_count BETWEEN 0 AND 64),
            prefix_truncated INTEGER NOT NULL CHECK (prefix_truncated IN (0,1)),
            section_parse_degraded INTEGER NOT NULL DEFAULT 0 CHECK (section_parse_degraded IN (0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(document_id, chunk_profile_version, section_key, ordinal),
            FOREIGN KEY (manifest_id) REFERENCES corpus_manifest(manifest_id)
        )""",

        """CREATE TABLE IF NOT EXISTS corpus_tombstones (
            chunk_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            reason TEXT NOT NULL CHECK (reason IN (
                'document_removed','eligibility_lost','profile_version_replaced',
                'disappeared_child','dedup_cluster_reassigned'
            )),
            previous_content_hash TEXT,
            previous_metadata_hash TEXT,
            replacement_chunk_id TEXT,
            manifest_id TEXT NOT NULL,
            tombstoned_at TEXT NOT NULL,
            FOREIGN KEY (manifest_id) REFERENCES corpus_manifest(manifest_id)
        )""",

        """CREATE TABLE IF NOT EXISTS corpus_manifest (
            manifest_id TEXT PRIMARY KEY CHECK (length(manifest_id) = 64 AND lower(manifest_id) = manifest_id),
            manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
            is_current INTEGER NOT NULL CHECK (is_current IN (0,1)),
            created_at TEXT NOT NULL
        )""",

        "CREATE UNIQUE INDEX IF NOT EXISTS idx_corpus_manifest_current ON corpus_manifest(is_current) WHERE is_current = 1",

        "ALTER TABLE articles ADD COLUMN source_class TEXT",
        "ALTER TABLE articles ADD COLUMN dedup_cluster_id TEXT",
        "ALTER TABLE articles ADD COLUMN cluster_first_available_at TEXT",
        "ALTER TABLE articles ADD COLUMN representative_document_id TEXT",

        "ALTER TABLE index_state ADD COLUMN metadata_hash TEXT",
        "ALTER TABLE index_state ADD COLUMN is_tombstone INTEGER NOT NULL DEFAULT 0 CHECK (is_tombstone IN (0,1))",

        "CREATE INDEX IF NOT EXISTS idx_corpus_chunks_document ON corpus_chunks(document_id)",
        "CREATE INDEX IF NOT EXISTS idx_corpus_chunks_available ON corpus_chunks(available_at, chunk_id)",
        "CREATE INDEX IF NOT EXISTS idx_corpus_chunks_source_class ON corpus_chunks(source_class, available_at)",
        "CREATE INDEX IF NOT EXISTS idx_corpus_chunks_manifest ON corpus_chunks(manifest_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_corpus_tombstones_manifest ON corpus_tombstones(manifest_id)",

        """CREATE TRIGGER IF NOT EXISTS trg_corpus_chunks_insert_guard
           BEFORE INSERT ON corpus_chunks
           WHEN (
               length(NEW.content_hash) != 64
               OR NEW.content_hash != lower(NEW.content_hash)
               OR NEW.content_hash GLOB '*[^0-9a-f]*'
               OR length(NEW.metadata_hash) != 64
               OR NEW.metadata_hash != lower(NEW.metadata_hash)
               OR NEW.metadata_hash GLOB '*[^0-9a-f]*'
               OR length(NEW.ordinal) != 4
               OR NEW.ordinal NOT GLOB '[0-9][0-9][0-9][0-9]'
               OR NOT json_valid(NEW.ticker_associations)
               OR NEW.chunk_profile_version NOT IN ('news_v2','filing_v2')
               OR NEW.source_class NOT IN (
                   'structured_market_data','official_government','issuer_disclosure',
                   'corporate_press_release','reported_news','analysis_opinion','aggregated_unknown'
               )
               OR NEW.eligibility NOT IN ('eligible','ineligible')
               OR NEW.status NOT IN ('active','pending_embedding','embedded','metadata_only','tombstoned')
               OR NEW.boundary_kind NOT IN ('document_end','paragraph','sentence','token_fallback')
               OR NEW.body_token_start < 0
               OR NEW.body_token_end <= NEW.body_token_start
               OR NEW.body_overlap_tokens < 0 OR NEW.body_overlap_tokens > 48
               OR NEW.prefix_token_count < 0 OR NEW.prefix_token_count > 64
               OR NEW.prefix_truncated NOT IN (0,1)
               OR NEW.section_parse_degraded NOT IN (0,1)
           )
        BEGIN
            SELECT RAISE(ABORT, 'corpus_chunk_contract');
        END""",

        """CREATE TRIGGER IF NOT EXISTS trg_corpus_chunks_update_guard
           BEFORE UPDATE ON corpus_chunks
           WHEN OLD.status = 'embedded'
              AND (
                  OLD.chunk_id != NEW.chunk_id
                  OR OLD.document_id != NEW.document_id
                  OR OLD.chunk_profile_version != NEW.chunk_profile_version
                  OR OLD.section_key != NEW.section_key
                  OR OLD.ordinal != NEW.ordinal
                  OR OLD.content_text != NEW.content_text
              )
        BEGIN
            SELECT RAISE(ABORT, 'corpus_chunk_identity_immutable');
        END""",

        """CREATE TRIGGER IF NOT EXISTS trg_corpus_manifest_current_guard
           BEFORE INSERT ON corpus_manifest
           WHEN NEW.is_current = 1
              AND (SELECT COUNT(*) FROM corpus_manifest WHERE is_current = 1) >= 1
        BEGIN
            SELECT RAISE(ABORT, 'corpus_manifest_current_unique');
        END""",

        """CREATE TRIGGER IF NOT EXISTS trg_corpus_manifest_current_guard_update
           BEFORE UPDATE ON corpus_manifest
           WHEN NEW.is_current = 1
              AND (SELECT COUNT(*) FROM corpus_manifest WHERE is_current = 1 AND manifest_id != NEW.manifest_id) >= 1
        BEGIN
            SELECT RAISE(ABORT, 'corpus_manifest_current_unique');
        END""",

        """CREATE TRIGGER IF NOT EXISTS trg_corpus_tombstone_insert_guard
           BEFORE INSERT ON corpus_tombstones
           WHEN (
               NEW.manifest_id IS NULL
               OR NEW.reason NOT IN (
                   'document_removed','eligibility_lost','profile_version_replaced',
                   'disappeared_child','dedup_cluster_reassigned'
               )
               OR length(NEW.manifest_id) != 64
               OR NEW.manifest_id != lower(NEW.manifest_id)
               OR NEW.manifest_id GLOB '*[^0-9a-f]*'
               OR NOT EXISTS (
                   SELECT 1 FROM corpus_manifest
                   WHERE manifest_id = NEW.manifest_id
               )
               OR (NEW.previous_content_hash IS NOT NULL
                   AND (length(NEW.previous_content_hash) != 64
                        OR NEW.previous_content_hash != lower(NEW.previous_content_hash)
                        OR NEW.previous_content_hash GLOB '*[^0-9a-f]*'))
               OR (NEW.previous_metadata_hash IS NOT NULL
                   AND (length(NEW.previous_metadata_hash) != 64
                        OR NEW.previous_metadata_hash != lower(NEW.previous_metadata_hash)
                        OR NEW.previous_metadata_hash GLOB '*[^0-9a-f]*'))
           )
        BEGIN
            SELECT RAISE(ABORT, 'corpus_tombstone_contract');
        END""",
    ], reversible=False),

    Migration(version=10, name="b4_lexical_index", statements=[
        """CREATE TABLE IF NOT EXISTS lexical_index_state (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            schema_version TEXT NOT NULL CHECK (schema_version = '1.0.0'),
            corpus_manifest_id TEXT NOT NULL,
            mode_served TEXT NOT NULL CHECK (mode_served IN ('fts5', 'sql_like')),
            fallback_reason TEXT CHECK (fallback_reason IN ('fts5_unavailable', 'fts5_missing', 'fts5_stale')),
            row_count INTEGER NOT NULL CHECK (row_count >= 0),
            built_at TEXT NOT NULL,
            FOREIGN KEY (corpus_manifest_id) REFERENCES corpus_manifest(manifest_id)
        )""",

        CORPUS_CHUNKS_FTS_DDL,
    ], reversible=False),

    Migration(version=11, name="b2o_full_cell_identity_and_fundamentals", statements=[
        "ALTER TABLE source_checkpoints ADD COLUMN cell_id TEXT",
        "ALTER TABLE source_checkpoints ADD COLUMN window_start TEXT",
        "ALTER TABLE source_checkpoints ADD COLUMN window_end TEXT",
        "ALTER TABLE source_checkpoints ADD COLUMN endpoint_name TEXT",
        "ALTER TABLE source_checkpoints ADD COLUMN provider_profile_version TEXT",
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_source_checkpoints_run_cell_id
           ON source_checkpoints(run_id, cell_id)
           WHERE cell_id IS NOT NULL""",
        """CREATE TRIGGER IF NOT EXISTS trg_b2o_checkpoint_cell_identity_insert
           BEFORE INSERT ON source_checkpoints
           WHEN NEW.cell_id IS NOT NULL
              AND (NEW.window_start IS NULL
                   OR NEW.window_end IS NULL
                   OR NEW.endpoint_name IS NULL
                   OR NEW.provider_profile_version IS NULL
                   OR NEW.date != NEW.window_start
                   OR length(NEW.cell_id) != 64
                   OR NEW.cell_id != lower(NEW.cell_id)
                   OR NEW.cell_id GLOB '*[^0-9a-f]*')
        BEGIN
            SELECT RAISE(ABORT, 'b2o_checkpoint_cell_identity_contract');
        END""",
        """CREATE TRIGGER IF NOT EXISTS trg_b2o_checkpoint_cell_identity_update
           BEFORE UPDATE ON source_checkpoints
           WHEN OLD.cell_id IS NOT NULL
              AND (OLD.cell_id IS NOT NEW.cell_id
                   OR OLD.window_start IS NOT NEW.window_start
                   OR OLD.window_end IS NOT NEW.window_end
                   OR OLD.endpoint_name IS NOT NEW.endpoint_name
                   OR OLD.provider_profile_version IS NOT NEW.provider_profile_version
                   OR NEW.date IS NOT NEW.window_start)
        BEGIN
            SELECT RAISE(ABORT, 'b2o_checkpoint_cell_identity_immutable');
        END""",
        """CREATE TRIGGER IF NOT EXISTS trg_b2o_checkpoint_cell_identity_update_contract
           BEFORE UPDATE ON source_checkpoints
           WHEN OLD.cell_id IS NULL
              AND NEW.cell_id IS NOT NULL
              AND (NEW.window_start IS NULL
                   OR NEW.window_end IS NULL
                   OR NEW.endpoint_name IS NULL
                   OR NEW.provider_profile_version IS NULL
                   OR NEW.date IS NOT NEW.window_start
                   OR length(NEW.cell_id) != 64
                   OR NEW.cell_id != lower(NEW.cell_id)
                   OR NEW.cell_id GLOB '*[^0-9a-f]*')
        BEGIN
            SELECT RAISE(ABORT, 'b2o_checkpoint_cell_identity_contract');
        END""",
        """CREATE TABLE IF NOT EXISTS fundamental_statements (
            statement_id TEXT PRIMARY KEY,
            raw_asset_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            ticker TEXT NOT NULL,
            statement_type TEXT NOT NULL,
            fiscal_date TEXT NOT NULL,
            fiscal_period TEXT,
            reported_currency TEXT,
            available_at TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(raw_asset_id) REFERENCES raw_assets(asset_id),
            CHECK(provider = 'fmp'),
            CHECK(statement_type IN (
                'income_statement',
                'balance_sheet',
                'cash_flow'
            ))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_fundamental_statements_ticker_date ON fundamental_statements(ticker, fiscal_date)",
        "CREATE INDEX IF NOT EXISTS idx_fundamental_statements_ticker_type_date ON fundamental_statements(ticker, statement_type, fiscal_date)",
    ], reversible=False),
    Migration(version=12, name="b2o_surrogate_checkpoint_pk", statements=[
        "-- v12: surrogate checkpoint_id PK with explicit canonical DDL.",
        "-- Applied via _apply_migration_v12() within SAVEPOINT.",
        "-- Replaces PRIMARY KEY (run_id, source_type, ticker, date).",
        "-- Adds UNIQUE(run_id, cell_id) for B2-O endpoint coexistence.",
        "-- Preserves ALL DEFAULT values, indexes, and trigger contracts.",
    ], reversible=False),
    Migration(version=13, name="filing_v3_profile", statements=[
        "-- v13: rebuild corpus_chunks for filing_v3; add filing_documents.document_id.",
        "-- Applied via _apply_migration_v13() within SAVEPOINT.",
    ], reversible=False),

]

# Bootstrap, snapshot, and tests derive the current schema from the registry.
CURRENT_SCHEMA_VERSION: int = max(migration.version for migration in MIGRATIONS)


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

        if migration.version == 12:
            conn.execute(f"SAVEPOINT migration_v{migration.version}")
            try:
                _apply_migration_v12(conn)
                conn.execute(f"PRAGMA user_version = {migration.version}")
                conn.execute(f"RELEASE SAVEPOINT migration_v{migration.version}")
            except Exception:
                conn.execute(f"ROLLBACK TO SAVEPOINT migration_v{migration.version}")
                conn.execute(f"RELEASE SAVEPOINT migration_v{migration.version}")
                raise
            logger.info("Applied migration v%d (%s)", migration.version, migration.name)
            continue

        if migration.version == 13:
            conn.execute(f"SAVEPOINT migration_v{migration.version}")
            try:
                _apply_migration_v13(conn)
                conn.execute(f"PRAGMA user_version = {migration.version}")
                conn.execute(f"RELEASE SAVEPOINT migration_v{migration.version}")
            except Exception:
                conn.execute(f"ROLLBACK TO SAVEPOINT migration_v{migration.version}")
                conn.execute(f"RELEASE SAVEPOINT migration_v{migration.version}")
                raise
            logger.info("Applied migration v%d (%s)", migration.version, migration.name)
            continue

        savepoint = f"migration_v{migration.version}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            if migration.version == 11:
                _reconcile_clean_assets_foreign_key(conn)
            for stmt in migration.statements:
                normalized_stmt = " ".join(stmt.split()).lower()
                if stmt.strip().startswith("--"):
                    continue
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as exc:
                    err = str(exc).lower()
                    is_v10_fts_statement = (
                        migration.version == 10
                        and normalized_stmt.startswith(
                            "create virtual table if not exists corpus_chunks_fts using fts5("
                        )
                    )
                    if is_v10_fts_statement and err == "no such module: fts5":
                        logger.warning(
                            "Migration v10: FTS5 unavailable, creating metadata-only index"
                        )
                        continue
                    if "duplicate column name" in err or "already exists" in err:
                        logger.debug(
                            "Migration v%d: column already exists, skipping: %s",
                            migration.version, stmt[:80],
                        )
                        continue
                    raise

            conn.execute(f"PRAGMA user_version = {migration.version}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except sqlite3.Error:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        logger.info("Applied migration v%d (%s)", migration.version, migration.name)

    return conn.execute("PRAGMA user_version").fetchone()[0]

"""V1.1 canonical registry schema (M3-1).

Execution-lock §B owns the exact v14 ``canonical_registry`` DDL. The canonical
tables are created only by migration v14 (additive; never added to
``storage/sqlite.py`` initial DDL). ``create_canonical_registry`` applies the
same statement list idempotently for tests/derivatives.
"""
from __future__ import annotations

import sqlite3

CANONICAL_REGISTRY_DDL: list[str] = [
    """CREATE TABLE IF NOT EXISTS canonical_assets (
        asset_id              TEXT PRIMARY KEY,
        asset_type            TEXT NOT NULL CHECK (asset_type IN ('NEWS','FILING','OFFICIAL_RELEASE','STRUCTURED_CONTEXT')),
        issuer_id             TEXT NOT NULL,
        tickers_json          TEXT NOT NULL CHECK (json_valid(tickers_json)),
        provider              TEXT NOT NULL,
        publisher             TEXT,
        canonical_url         TEXT,
        source_class          TEXT NOT NULL CHECK (source_class IN ('structured_market_data','official_government','issuer_disclosure','corporate_press_release','reported_news','analysis_opinion','aggregated_unknown')),
        source_published_at   TEXT,
        eligible_at           TEXT,
        eligible_at_reason    TEXT NOT NULL,
        temporal_precision    TEXT NOT NULL CHECK (temporal_precision IN ('accepted_time','publication_time','ingestion_time','date_only_latest_plausible','unknown_time_of_day','unknown')),
        accepted_time_recovered INTEGER NOT NULL DEFAULT 0 CHECK (accepted_time_recovered IN (0,1)),
        fail_closed           INTEGER NOT NULL DEFAULT 0 CHECK (fail_closed IN (0,1)),
        ingested_at           TEXT NOT NULL,
        content_state         TEXT NOT NULL CHECK (content_state IN ('FULL_TEXT','TITLE_ONLY','METADATA_ONLY','EMPTY','FAILED')),
        serving_status        TEXT NOT NULL CHECK (serving_status IN ('body_candidate','lead_candidate','excluded')),
        title                 TEXT,
        content_ref           TEXT,
        dedup_cluster_id      TEXT,
        independence_group_id TEXT,
        parse_quality         TEXT NOT NULL CHECK (parse_quality IN ('full','degraded','not_applicable','failed')),
        subtype_metadata      TEXT NOT NULL CHECK (json_valid(subtype_metadata)),
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_canonical_assets_asset_type ON canonical_assets(asset_type)",
    "CREATE INDEX IF NOT EXISTS idx_canonical_assets_source_class ON canonical_assets(source_class)",
    "CREATE INDEX IF NOT EXISTS idx_canonical_assets_content_state ON canonical_assets(content_state)",
    "CREATE INDEX IF NOT EXISTS idx_canonical_assets_eligible_at ON canonical_assets(eligible_at)",
    "CREATE INDEX IF NOT EXISTS idx_canonical_assets_dedup ON canonical_assets(dedup_cluster_id)",
    "CREATE INDEX IF NOT EXISTS idx_canonical_assets_independence ON canonical_assets(independence_group_id)",
    """CREATE TABLE IF NOT EXISTS canonical_content_versions (
        canonical_content_version_id TEXT PRIMARY KEY,
        asset_id              TEXT NOT NULL REFERENCES canonical_assets(asset_id),
        content_hash          TEXT NOT NULL,
        normalizer_version    TEXT NOT NULL,
        materiality_version   TEXT NOT NULL,
        version_ordinal       INTEGER NOT NULL CHECK (version_ordinal >= 1),
        created_at            TEXT NOT NULL,
        payload_ref           TEXT,
        UNIQUE (asset_id, canonical_content_version_id),
        UNIQUE (asset_id, version_ordinal)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_canonical_content_versions_asset ON canonical_content_versions(asset_id)",
    """CREATE TABLE IF NOT EXISTS canonical_asset_tickers (
        asset_id   TEXT NOT NULL REFERENCES canonical_assets(asset_id),
        ticker     TEXT NOT NULL,
        issuer_id  TEXT,
        PRIMARY KEY (asset_id, ticker)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_canonical_tickers_ticker ON canonical_asset_tickers(ticker)",
    """CREATE TABLE IF NOT EXISTS canonical_asset_links (
        asset_id        TEXT NOT NULL REFERENCES canonical_assets(asset_id),
        linked_asset_id TEXT NOT NULL REFERENCES canonical_assets(asset_id),
        link_type       TEXT NOT NULL CHECK (link_type IN ('known_syndication','same_content_hash','provider_document_id','normalized_url')),
        created_at      TEXT NOT NULL,
        PRIMARY KEY (asset_id, linked_asset_id, link_type),
        CHECK (asset_id <> linked_asset_id)
    )""",
    """CREATE TABLE IF NOT EXISTS canonical_subtype_assoc (
        asset_id                   TEXT NOT NULL REFERENCES canonical_assets(asset_id),
        subtype_table              TEXT NOT NULL CHECK (subtype_table IN ('articles','filings','filing_documents')),
        subtype_pk                 TEXT NOT NULL,
        subtype_pk_value           TEXT NOT NULL,
        canonical_content_version_id TEXT,
        created_at                 TEXT NOT NULL,
        PRIMARY KEY (subtype_table, subtype_pk, subtype_pk_value),
        FOREIGN KEY (asset_id, canonical_content_version_id)
            REFERENCES canonical_content_versions(asset_id, canonical_content_version_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_canonical_subtype_assoc_asset ON canonical_subtype_assoc(asset_id)",
    """CREATE INDEX IF NOT EXISTS idx_canonical_subtype_assoc_content_version
        ON canonical_subtype_assoc(canonical_content_version_id)
        WHERE canonical_content_version_id IS NOT NULL""",
    """CREATE TABLE IF NOT EXISTS canonical_structured_fact_refs (
        fact_id              TEXT PRIMARY KEY,
        fact_type            TEXT NOT NULL CHECK (
            fact_type IN ('ohlcv','macro_observation','fundamental_statement')
        ),
        source_table         TEXT NOT NULL CHECK (
            source_table IN ('ohlcv','macro_observations','fundamental_statements')
        ),
        source_pk_json       TEXT NOT NULL CHECK(json_valid(source_pk_json)),
        eligible_at          TEXT,
        temporal_precision   TEXT NOT NULL,
        source_row_sha256    TEXT NOT NULL
            CHECK(length(source_row_sha256)=64
                  AND source_row_sha256 NOT GLOB '*[^0-9a-f]*'),
        certification_status TEXT NOT NULL CHECK (
            certification_status IN ('certified','excluded_missing_time','excluded_invalid')
        ),
        created_at           TEXT NOT NULL,
        UNIQUE(source_table, source_pk_json)
    )""",
    # v14 normalized_provenance extension (additive columns; table DDL lives in
    # migrations.py v3 §4.6; composite PK and entity_type CHECK are retained)
    "ALTER TABLE normalized_provenance ADD COLUMN canonical_asset_id TEXT",
    "ALTER TABLE normalized_provenance ADD COLUMN canonical_content_version_id TEXT",
    # Repair results are persisted on subtype rows before canonical projection.
    "ALTER TABLE filings ADD COLUMN accepted_time_utc TEXT",
    "ALTER TABLE filings ADD COLUMN eligible_at TEXT",
    "ALTER TABLE filings ADD COLUMN eligible_at_reason TEXT",
    "ALTER TABLE filings ADD COLUMN temporal_precision TEXT",
    "ALTER TABLE filings ADD COLUMN accepted_time_recovered INTEGER CHECK (accepted_time_recovered IS NULL OR accepted_time_recovered IN (0,1))",
    "ALTER TABLE filings ADD COLUMN eligibility_fail_closed INTEGER CHECK (eligibility_fail_closed IS NULL OR eligibility_fail_closed IN (0,1))",
    "ALTER TABLE articles ADD COLUMN normalized_url TEXT",
    "ALTER TABLE articles ADD COLUMN recovered_body_text TEXT",
    "ALTER TABLE articles ADD COLUMN recovered_content_state TEXT CHECK (recovered_content_state IS NULL OR recovered_content_state IN ('FULL_TEXT','TITLE_ONLY','METADATA_ONLY','EMPTY','FAILED'))",
    "ALTER TABLE articles ADD COLUMN recovered_content_hash TEXT",
    "ALTER TABLE articles ADD COLUMN body_normalizer_version TEXT",
    # Batch B: filing_documents reparse-persist columns (nullable; historical
    # rows remain readable). section_parse_degraded is a quality flag and never
    # folds into DATA-01.
    "ALTER TABLE filing_documents ADD COLUMN parser_version TEXT",
    "ALTER TABLE filing_documents ADD COLUMN document_hash TEXT",
    "ALTER TABLE filing_documents ADD COLUMN parse_quality TEXT CHECK (parse_quality IS NULL OR parse_quality IN ('full','degraded','not_applicable','failed'))",
    "ALTER TABLE filing_documents ADD COLUMN section_parse_degraded INTEGER CHECK (section_parse_degraded IS NULL OR section_parse_degraded IN (0,1))",
    "CREATE INDEX IF NOT EXISTS idx_normalized_provenance_canonical_asset ON normalized_provenance(canonical_asset_id) WHERE canonical_asset_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_normalized_provenance_canonical_content_version ON normalized_provenance(canonical_content_version_id) WHERE canonical_content_version_id IS NOT NULL",
]


def create_canonical_registry(conn: sqlite3.Connection) -> None:
    """Create the v14 canonical registry tables and columns (idempotent).

    Mirrors ``run_migrations`` per-statement duplicate-column/already-exists
    skipping so repeated calls are safe.
    """
    for stmt in CANONICAL_REGISTRY_DDL:
        if stmt.strip().startswith("--"):
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            err = str(exc).lower()
            if "duplicate column name" in err or "already exists" in err:
                continue
            raise


__all__ = ["CANONICAL_REGISTRY_DDL", "create_canonical_registry"]

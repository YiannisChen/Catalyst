"""M3-1: canonical registry migration v14 tests.

Covers execution-lock §B: user_version bump, canonical table creation, repair
persistence columns on subtype tables, idempotent rerun, FK enforcement,
composite-FK content-version ownership on canonical_subtype_assoc, and the
normalized_provenance v14 canonical reference columns.
"""
from __future__ import annotations

import sqlite3

import pytest

from catalyst_data.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS, run_migrations


def _db_at_v13() -> sqlite3.Connection:
    from conftest import _fresh_db_at_version
    from catalyst_data.storage.sqlite import ensure_filings_tables

    conn = _fresh_db_at_version(13)
    ensure_filings_tables(conn)
    return conn


def _canonical_tables() -> set[str]:
    return {
        "canonical_assets",
        "canonical_content_versions",
        "canonical_asset_tickers",
        "canonical_asset_links",
        "canonical_subtype_assoc",
        "canonical_structured_fact_refs",
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _seed_asset(conn: sqlite3.Connection, *, asset_id: str = "v1:asset:A") -> None:
    conn.execute(
        """
        INSERT INTO canonical_assets (
            asset_id, asset_type, issuer_id, tickers_json, provider, publisher,
            canonical_url, source_class, source_published_at, eligible_at,
            eligible_at_reason, temporal_precision, accepted_time_recovered,
            fail_closed, ingested_at, content_state, serving_status, title,
            content_ref, dedup_cluster_id, independence_group_id, parse_quality,
            subtype_metadata, created_at, updated_at
        ) VALUES (?, 'NEWS', 'issuer:AAPL', '["AAPL"]', 'finnhub', 'Example News',
                  'https://example.com/a', 'reported_news', '2026-01-05T10:00:00Z',
                  '2026-01-05T10:00:00Z', 'publication_time_provider',
                  'publication_time', 0, 0, '2026-01-05T10:00:00Z', 'FULL_TEXT',
                  'body_candidate', 'Title A', 'ref-a', NULL, NULL,
                  'not_applicable', '{"publisher":"Example News"}',
                  '2026-01-05T10:00:00Z', '2026-01-05T10:00:00Z')
        """,
        (asset_id,),
    )
    conn.commit()


def _seed_content_version(
    conn: sqlite3.Connection,
    *,
    version_id: str,
    asset_id: str,
    content_hash: str = "00ebb9b7daf865ce736aec5cf250d5f109d3d40c401265f13b9d36d72c178ee5",
    version_ordinal: int = 1,
) -> None:
    conn.execute(
        """
        INSERT INTO canonical_content_versions (
            canonical_content_version_id, asset_id, content_hash,
            normalizer_version, materiality_version, version_ordinal, created_at,
            payload_ref
        ) VALUES (?, ?, ?, 'news_body_v1', 'materiality_v1', ?, '2026-01-05T10:00:00Z', ?)
        """,
        (version_id, asset_id, content_hash, version_ordinal, f"ref-{version_id}"),
    )
    conn.commit()


def test_v14_registered_as_canonical_registry():
    v14 = [m for m in MIGRATIONS if m.version == 14]
    assert len(v14) == 1
    assert v14[0].name == "canonical_registry"
    assert CURRENT_SCHEMA_VERSION == 14


def test_run_migrations_bumps_to_14_and_creates_tables():
    conn = _db_at_v13()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
    assert run_migrations(conn) == 14
    from conftest import _table_names

    tables = _table_names(conn)
    assert _canonical_tables() <= tables
    conn.close()


def test_canonical_assets_has_no_content_hash_column():
    conn = _db_at_v13()
    run_migrations(conn)
    cols = _columns(conn, "canonical_assets")
    assert "content_hash" not in cols
    assert "asset_id" in cols
    assert "dedup_cluster_id" in cols
    assert "independence_group_id" in cols
    assert "parse_quality" in cols
    assert "eligible_at_reason" in cols
    conn.close()


def test_repair_persistence_columns_added():
    conn = _db_at_v13()
    run_migrations(conn)
    filings_cols = _columns(conn, "filings")
    assert {
        "accepted_time_utc",
        "eligible_at",
        "eligible_at_reason",
        "temporal_precision",
        "accepted_time_recovered",
        "eligibility_fail_closed",
    } <= filings_cols
    articles_cols = _columns(conn, "articles")
    assert {
        "normalized_url",
        "recovered_body_text",
        "recovered_content_state",
        "recovered_content_hash",
        "body_normalizer_version",
    } <= articles_cols
    provenance_cols = _columns(conn, "normalized_provenance")
    assert {
        "canonical_asset_id",
        "canonical_content_version_id",
    } <= provenance_cols
    conn.close()


def test_run_migrations_twice_idempotent():
    conn = _db_at_v13()
    assert run_migrations(conn) == 14
    assert run_migrations(conn) == 14
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
    assert _canonical_tables() <= {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()


def test_fk_every_content_version_asset_resolves():
    conn = _db_at_v13()
    run_migrations(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        _seed_content_version(conn, version_id="v1:content:missing", asset_id="v1:asset:missing")
    conn.close()


def test_content_change_new_version_not_new_asset():
    conn = _db_at_v13()
    run_migrations(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_asset(conn)
    _seed_content_version(
        conn,
        version_id="v1:content:v1",
        asset_id="v1:asset:A",
        content_hash="00ebb9b7daf865ce736aec5cf250d5f109d3d40c401265f13b9d36d72c178ee5",
        version_ordinal=1,
    )
    _seed_content_version(
        conn,
        version_id="v1:content:v2",
        asset_id="v1:asset:A",
        content_hash="11ebb9b7daf865ce736aec5cf250d5f109d3d40c401265f13b9d36d72c178ee5",
        version_ordinal=2,
    )
    assets = conn.execute("SELECT COUNT(*) FROM canonical_assets").fetchone()[0]
    versions = conn.execute("SELECT COUNT(*) FROM canonical_content_versions").fetchone()[0]
    assert assets == 1
    assert versions == 2
    conn.close()


def test_subtype_assoc_rejects_content_version_of_different_asset():
    conn = _db_at_v13()
    run_migrations(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_asset(conn, asset_id="v1:asset:A")
    _seed_asset(conn, asset_id="v1:asset:B")
    _seed_content_version(conn, version_id="v1:content:v1", asset_id="v1:asset:A")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO canonical_subtype_assoc (
                asset_id, subtype_table, subtype_pk, subtype_pk_value,
                canonical_content_version_id, created_at
            ) VALUES ('v1:asset:B', 'articles', 'article_id', 'finnhub:a',
                      'v1:content:v1', '2026-01-05T10:00:00Z')
            """
        )
        conn.commit()
    # Same-asset binding is accepted.
    conn.execute(
        """
        INSERT INTO canonical_subtype_assoc (
            asset_id, subtype_table, subtype_pk, subtype_pk_value,
            canonical_content_version_id, created_at
        ) VALUES ('v1:asset:A', 'articles', 'article_id', 'finnhub:a',
                  'v1:content:v1', '2026-01-05T10:00:00Z')
        """
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM canonical_subtype_assoc").fetchone()[0] == 1
    conn.close()


def test_normalized_provenance_v14_columns_resolve():
    from conftest import _seed_v2_raw_row

    conn = _db_at_v13()
    run_migrations(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_v2_raw_row(conn, raw_asset_id="raw:req-test-001")
    _seed_asset(conn, asset_id="v1:asset:A")
    _seed_content_version(conn, version_id="v1:content:v1", asset_id="v1:asset:A")
    conn.execute(
        """
        INSERT INTO normalized_provenance (
            entity_type, entity_id, entity_version, raw_asset_id,
            normalizer_version, created_at, canonical_asset_id,
            canonical_content_version_id
        ) VALUES ('article', 'finnhub:a',
                  '0000000000000000000000000000000000000000000000000000000000000000',
                  'raw:req-test-001', 'news_body_v1', '2026-01-05T10:00:00Z',
                  'v1:asset:A', 'v1:content:v1')
        """
    )
    conn.commit()
    # Non-null canonical_asset_id resolves to canonical_assets.
    bad_asset = conn.execute(
        "SELECT COUNT(*) FROM normalized_provenance p "
        "LEFT JOIN canonical_assets a ON a.asset_id = p.canonical_asset_id "
        "WHERE p.canonical_asset_id IS NOT NULL AND a.asset_id IS NULL"
    ).fetchone()[0]
    assert bad_asset == 0
    # Non-null canonical_content_version_id resolves to canonical_content_versions.
    bad_version = conn.execute(
        "SELECT COUNT(*) FROM normalized_provenance p "
        "LEFT JOIN canonical_content_versions v "
        "ON v.canonical_content_version_id = p.canonical_content_version_id "
        "WHERE p.canonical_content_version_id IS NOT NULL AND v.canonical_content_version_id IS NULL"
    ).fetchone()[0]
    assert bad_version == 0
    # When both are non-null the content version belongs to that asset.
    ownership_violation = conn.execute(
        "SELECT COUNT(*) FROM normalized_provenance p "
        "JOIN canonical_content_versions v "
        "ON v.canonical_content_version_id = p.canonical_content_version_id "
        "WHERE p.canonical_asset_id IS NOT NULL AND v.asset_id != p.canonical_asset_id"
    ).fetchone()[0]
    assert ownership_violation == 0
    conn.close()


# ---------------------------------------------------------------------------
# Batch B B9: v14 stamp guard — reparse-persist columns must exist before 14.
# ---------------------------------------------------------------------------


def test_filing_documents_reparse_columns_added():
    conn = _db_at_v13()
    run_migrations(conn)
    docs_cols = _columns(conn, "filing_documents")
    assert {
        "parser_version",
        "document_hash",
        "parse_quality",
        "section_parse_degraded",
    } <= docs_cols
    conn.close()


def test_v14_not_stamped_without_filing_documents():
    """A DB without filings/filing_documents never reports version 14."""
    from conftest import _fresh_db_at_version

    # Build a v13 DB without filing tables: _fresh_db_at_version(12) does not
    # create them, then stamp 13 manually so run_migrations only has v14 left.
    conn = _fresh_db_at_version(12)
    conn.execute("PRAGMA user_version = 13")
    conn.commit()
    with pytest.raises(RuntimeError, match="refusing to stamp"):
        run_migrations(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
    conn.close()


def test_v14_not_stamped_without_reparse_columns():
    """A DB whose filing_documents reparse columns cannot exist fails closed.

    The reparse-persist ALTERs are skipped when the table is missing; the B9
    guard must then refuse the version-14 stamp.
    """
    from conftest import _fresh_db_at_version
    from catalyst_data.storage.sqlite import ensure_filings_tables

    conn = _fresh_db_at_version(13)
    ensure_filings_tables(conn)
    # Simulate a DB where filing_documents is absent (reparse columns cannot
    # be added): drop the table so the v14 ALTERs are skipped.
    conn.execute("DROP TABLE filing_documents")
    conn.commit()
    with pytest.raises(RuntimeError, match="filing_documents"):
        run_migrations(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
    conn.close()

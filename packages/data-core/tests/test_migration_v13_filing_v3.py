"""Migration v13: filing_v3 profile + filing_documents.document_id."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from catalyst_data.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS, run_migrations


def _hash_row(row: tuple, col_types: list[str] | None = None) -> str:
    """Typed row hash aligned with migration/snapshot encoding."""
    from catalyst_data.manifests.snapshot import _typed_value
    from catalyst_data.manifests.universe import canonical_json_bytes

    types = col_types or [""] * len(row)
    obj = {
        f"c{i}": _typed_value(v, types[i] if i < len(types) else "")
        for i, v in enumerate(row)
    }
    return hashlib.sha256(canonical_json_bytes(obj)).hexdigest()


def test_typed_hash_null_vs_text_null_differ():
    assert _hash_row((None,)) != _hash_row(("null",))


def test_typed_hash_int_vs_text_differ():
    # when declared INT vs TEXT, types force distinction
    assert _hash_row((1,), ["INTEGER"]) != _hash_row(("1",), ["TEXT"])


def _bootstrap_minimal_v12(conn: sqlite3.Connection) -> None:
    """Create tables needed to reach v12 via run_migrations from empty-ish DB."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_checkpoints (
            run_id TEXT, source_type TEXT, ticker TEXT, date TEXT,
            status TEXT, error_class TEXT, retries INTEGER DEFAULT 0,
            PRIMARY KEY (run_id, source_type, ticker, date)
        );
        CREATE TABLE IF NOT EXISTS ingestion_runs (
            run_id TEXT PRIMARY KEY, status TEXT, plan_hash TEXT,
            expected_plan_hash TEXT, allow_stale_ohlcv INTEGER DEFAULT 0,
            allow_stale_ohlcv_overridden INTEGER DEFAULT 0,
            cancel_requested INTEGER DEFAULT 0, parent_run_id TEXT
        );
        CREATE TABLE IF NOT EXISTS raw_assets (
            asset_id TEXT PRIMARY KEY, ticker TEXT NOT NULL,
            source_type TEXT NOT NULL, reference_date TEXT NOT NULL,
            fetched_at TEXT NOT NULL, data_version TEXT NOT NULL DEFAULT 'v1',
            content_raw BLOB NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS articles (article_id TEXT PRIMARY KEY, published_utc TEXT);
        CREATE TABLE IF NOT EXISTS article_tickers (
            article_id TEXT, ticker TEXT, reference_date TEXT,
            PRIMARY KEY (article_id, ticker)
        );
        CREATE TABLE IF NOT EXISTS clean_assets (
            asset_id TEXT PRIMARY KEY, raw_asset_id TEXT, reference_date TEXT
        );
        CREATE TABLE IF NOT EXISTS ohlcv (
            symbol TEXT, date TEXT, source TEXT, PRIMARY KEY (symbol, date, source)
        );
        CREATE TABLE IF NOT EXISTS index_state (
            chunk_id TEXT NOT NULL, chunk_level TEXT NOT NULL DEFAULT 'l1',
            corpus_item_id TEXT NOT NULL, source_kind TEXT NOT NULL,
            content_hash TEXT NOT NULL, content_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE TABLE IF NOT EXISTS filings (
            filing_id TEXT PRIMARY KEY, cik TEXT NOT NULL, ticker TEXT NOT NULL,
            form_type TEXT NOT NULL, filed_at TEXT NOT NULL,
            accession_number TEXT NOT NULL, url TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS filing_documents (
            filing_id TEXT NOT NULL, document_url TEXT NOT NULL,
            document_type TEXT NOT NULL DEFAULT 'primary_doc',
            text TEXT, char_len INTEGER, content_type TEXT, byte_size INTEGER,
            extraction_status TEXT NOT NULL DEFAULT 'empty'
                CHECK (extraction_status IN (
                    'success','empty','pdf_skipped','fetch_failed','timeout'
                )),
            extracted_at TEXT,
            PRIMARY KEY (filing_id, document_url)
        );
        """
    )
    conn.commit()


def _seed_filing_v2_chunk(conn: sqlite3.Connection, chunk_id: str = "chunk_a") -> None:
    conn.execute(
        """INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, '{}', 1, '2026-01-01T00:00:00Z')""",
        ("a" * 64,),
    )
    content_hash = "b" * 64
    metadata_hash = "c" * 64
    conn.execute(
        """INSERT INTO corpus_chunks (
            chunk_id, document_id, chunk_profile_version, section_key, ordinal,
            content_text, content_hash, metadata_hash, source_class,
            available_at, ticker_associations, eligibility, manifest_id, status,
            boundary_kind, body_token_start, body_token_end, body_overlap_tokens,
            prefix_token_count, prefix_truncated, section_parse_degraded,
            created_at, updated_at
        ) VALUES (?, 'doc1', 'filing_v2', 'item_1.01', '0001',
            'hello world', ?, ?, 'official_government',
            '2025-08-01T00:00:00Z', '["AAPL"]', 'eligible', ?, 'active',
            'document_end', 0, 2, 0, 0, 0, 0,
            '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
        (chunk_id, content_hash, metadata_hash, "a" * 64),
    )
    conn.commit()


def test_current_schema_version_is_13():
    assert CURRENT_SCHEMA_VERSION == 13
    versions = [m.version for m in MIGRATIONS]
    assert 13 in versions


def test_v13_always_table_rebuild_not_alter_check(tmp_path: Path):
    """v13 must rebuild corpus_chunks (no ALTER CHECK)."""
    conn = sqlite3.connect(tmp_path / "t.db")
    _bootstrap_minimal_v12(conn)
    run_migrations(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='corpus_chunks'"
    ).fetchone()[0]
    assert "filing_v3" in sql
    assert "news_v2" in sql and "filing_v2" in sql
    conn.close()


def test_fresh_db_accepts_filing_v3_insert(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "t.db")
    _bootstrap_minimal_v12(conn)
    run_migrations(conn)
    conn.execute(
        """INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, '{}', 1, '2026-01-01T00:00:00Z')""",
        ("d" * 64,),
    )
    conn.execute(
        """INSERT INTO corpus_chunks (
            chunk_id, document_id, chunk_profile_version, section_key, ordinal,
            content_text, content_hash, metadata_hash, source_class,
            available_at, ticker_associations, eligibility, manifest_id, status,
            boundary_kind, body_token_start, body_token_end, body_overlap_tokens,
            prefix_token_count, prefix_truncated, section_parse_degraded,
            created_at, updated_at
        ) VALUES ('c1', 'doc', 'filing_v3', 'item_1', '0001',
            'body', ?, ?, 'official_government',
            '2025-08-01T00:00:00Z', '["AAPL"]', 'eligible', ?, 'pending_embedding',
            'document_end', 0, 1, 0, 0, 0, 0,
            '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
        ("e" * 64, "f" * 64, "d" * 64),
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM corpus_chunks WHERE chunk_profile_version='filing_v3'").fetchone()[0] == 1
    conn.close()


def test_upgrade_v12_to_v13_preserves_filing_v2_rows(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "t.db")
    _bootstrap_minimal_v12(conn)
    # Stop at v12 by applying through registry then... actually run_migrations goes to max.
    # Seed after reaching v12 by temporarily monkeypatching is hard; seed at v9-v12 path:
    # Apply migrations, but insert filing_v2 after v9 exists mid-way:
    # Simpler: run to 13 after inserting at v12 via two-step.
    from catalyst_data import migrations as mig

    # Force apply only up to 12 by setting CURRENT temporarily - instead insert after v12 manually:
    # Run migrations with version 13 removed? Cleaner approach:
    versions = sorted(m.version for m in MIGRATIONS)
    assert max(versions) == 13
    # Apply migrations to 12 by executing without v13:
    saved = list(mig.MIGRATIONS)
    mig.MIGRATIONS[:] = [m for m in saved if m.version <= 12]
    try:
        v = run_migrations(conn)
        assert v == 12
        _seed_filing_v2_chunk(conn)
        before = conn.execute(
            "SELECT chunk_id, document_id, content_hash, content_text FROM corpus_chunks"
        ).fetchall()
    finally:
        mig.MIGRATIONS[:] = saved
    # Now apply v13
    v = run_migrations(conn)
    assert v == 13
    after = conn.execute(
        "SELECT chunk_id, document_id, content_hash, content_text FROM corpus_chunks"
    ).fetchall()
    assert before == after
    assert after[0][2]  # content_hash preserved
    conn.close()


def test_v13_recreates_indexes_idx_corpus_chunks_document_available_source_class_manifest(
    tmp_path: Path,
):
    conn = sqlite3.connect(tmp_path / "t.db")
    _bootstrap_minimal_v12(conn)
    run_migrations(conn)
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='corpus_chunks'"
        )
    }
    for req in (
        "idx_corpus_chunks_document",
        "idx_corpus_chunks_available",
        "idx_corpus_chunks_source_class",
        "idx_corpus_chunks_manifest",
    ):
        assert req in names
    conn.close()


def test_v13_recreates_triggers_insert_and_update_guard(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "t.db")
    _bootstrap_minimal_v12(conn)
    run_migrations(conn)
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='corpus_chunks'"
        )
    }
    assert "trg_corpus_chunks_insert_guard" in names
    assert "trg_corpus_chunks_update_guard" in names
    conn.close()


def test_v13_adds_nullable_filing_document_id_and_unique_partial_index(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "t.db")
    _bootstrap_minimal_v12(conn)
    run_migrations(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(filing_documents)")}
    assert "document_id" in cols
    idx = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_filing_documents_document_id'"
    ).fetchone()
    assert idx is not None
    assert "WHERE document_id IS NOT NULL" in (idx[0] or "")
    conn.close()


def test_v13_document_id_guards_shape_and_immutability(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "t.db")
    _bootstrap_minimal_v12(conn)
    run_migrations(conn)
    conn.execute(
        "INSERT INTO filings (filing_id, cik, ticker, form_type, filed_at, accession_number, url) "
        "VALUES ('f1','0001','AAPL','8-K','2025-08-01','0001','http://x')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO filing_documents
               (filing_id, document_url, document_type, extraction_status, document_id)
               VALUES ('f1','http://a','primary_doc','success','NOTHEX')"""
        )
    conn.execute(
        """INSERT INTO filing_documents
           (filing_id, document_url, document_type, extraction_status, document_id)
           VALUES ('f1','http://a','primary_doc','success',?)""",
        ("a" * 64,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE filing_documents SET document_id=? WHERE filing_id='f1'",
            ("b" * 64,),
        )
    conn.close()


def test_v13_legacy_filing_document_null_id_preserved(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "t.db")
    _bootstrap_minimal_v12(conn)
    from catalyst_data import migrations as mig

    saved = list(mig.MIGRATIONS)
    mig.MIGRATIONS[:] = [m for m in saved if m.version <= 12]
    try:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO filings (filing_id, cik, ticker, form_type, filed_at, accession_number, url) "
            "VALUES ('f1','0001','AAPL','8-K','2025-08-01','0001','http://x')"
        )
        conn.execute(
            """INSERT INTO filing_documents
               (filing_id, document_url, document_type, extraction_status)
               VALUES ('f1','http://legacy','primary_doc','empty')"""
        )
        conn.commit()
    finally:
        mig.MIGRATIONS[:] = saved
    run_migrations(conn)
    row = conn.execute(
        "SELECT document_id FROM filing_documents WHERE document_url='http://legacy'"
    ).fetchone()
    assert row is not None and row[0] is None
    conn.close()


def test_v13_savepoint_rollback_restores_v12(tmp_path: Path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "t.db")
    _bootstrap_minimal_v12(conn)
    from catalyst_data import migrations as mig

    saved = list(mig.MIGRATIONS)
    mig.MIGRATIONS[:] = [m for m in saved if m.version <= 12]
    try:
        assert run_migrations(conn) == 12
    finally:
        mig.MIGRATIONS[:] = saved

    original = mig._apply_migration_v13

    def boom(c):
        original(c)
        raise RuntimeError("injected v13 failure")

    monkeypatch.setattr(mig, "_apply_migration_v13", boom)
    with pytest.raises(RuntimeError, match="injected v13"):
        run_migrations(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='corpus_chunks'"
    ).fetchone()
    # At v12, corpus_chunks may exist from v9 with filing_v2 only
    if sql and sql[0]:
        assert "filing_v3" not in sql[0]
    conn.close()


def test_trigger_rejects_unknown_profile(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "t.db")
    _bootstrap_minimal_v12(conn)
    run_migrations(conn)
    conn.execute(
        """INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, '{}', 1, '2026-01-01T00:00:00Z')""",
        ("d" * 64,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO corpus_chunks (
                chunk_id, document_id, chunk_profile_version, section_key, ordinal,
                content_text, content_hash, metadata_hash, source_class,
                available_at, ticker_associations, eligibility, manifest_id, status,
                boundary_kind, body_token_start, body_token_end, body_overlap_tokens,
                prefix_token_count, prefix_truncated, section_parse_degraded,
                created_at, updated_at
            ) VALUES ('c1', 'doc', 'filing_v9', 'item_1', '0001',
                'body', ?, ?, 'official_government',
                '2025-08-01T00:00:00Z', '["AAPL"]', 'eligible', ?, 'active',
                'document_end', 0, 1, 0, 0, 0, 0,
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
            ("e" * 64, "f" * 64, "d" * 64),
        )
    conn.close()


def test_trigger_allows_news_v2_filing_v2_filing_v3(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "t.db")
    _bootstrap_minimal_v12(conn)
    run_migrations(conn)
    conn.execute(
        """INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, '{}', 1, '2026-01-01T00:00:00Z')""",
        ("d" * 64,),
    )
    for i, profile in enumerate(("news_v2", "filing_v2", "filing_v3")):
        conn.execute(
            """INSERT INTO corpus_chunks (
                chunk_id, document_id, chunk_profile_version, section_key, ordinal,
                content_text, content_hash, metadata_hash, source_class,
                available_at, ticker_associations, eligibility, manifest_id, status,
                boundary_kind, body_token_start, body_token_end, body_overlap_tokens,
                prefix_token_count, prefix_truncated, section_parse_degraded,
                created_at, updated_at
            ) VALUES (?, 'doc', ?, 's', ?,
                'body', ?, ?, 'official_government',
                '2025-08-01T00:00:00Z', '["AAPL"]', 'eligible', ?, 'active',
                'document_end', 0, 1, 0, 0, 0, 0,
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
            (
                f"c{i}",
                profile,
                f"{i:04d}",
                f"{i:064x}"[:64],
                f"{(i+1):064x}"[:64],
                "d" * 64,
            ),
        )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM corpus_chunks").fetchone()[0] == 3
    conn.close()


def test_foreign_key_check_empty_after_v13(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("PRAGMA foreign_keys=ON")
    _bootstrap_minimal_v12(conn)
    run_migrations(conn)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_v13_full_row_logical_hashes_match_before_after_rebuild(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "t.db")
    _bootstrap_minimal_v12(conn)
    from catalyst_data import migrations as mig

    saved = list(mig.MIGRATIONS)
    mig.MIGRATIONS[:] = [m for m in saved if m.version <= 12]
    try:
        run_migrations(conn)
        _seed_filing_v2_chunk(conn, "chunk_x")
        conn.execute(
            """INSERT INTO corpus_chunks (
                chunk_id, document_id, chunk_profile_version, section_key, ordinal,
                content_text, content_hash, metadata_hash, source_class,
                available_at, ticker_associations, eligibility, manifest_id, status,
                boundary_kind, body_token_start, body_token_end, body_overlap_tokens,
                prefix_token_count, prefix_truncated, section_parse_degraded,
                created_at, updated_at
            ) VALUES ('chunk_y', 'doc2', 'filing_v2', 'item_2.01', '0001',
                'second', ?, ?, 'official_government',
                '2025-08-02T00:00:00Z', '["MSFT"]', 'eligible', ?, 'active',
                'document_end', 0, 1, 0, 0, 0, 0,
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
            ("1" * 64, "2" * 64, "a" * 64),
        )
        conn.commit()
        before_rows = conn.execute(
            "SELECT * FROM corpus_chunks ORDER BY chunk_id"
        ).fetchall()
        before_hashes = [_hash_row(r) for r in before_rows]
    finally:
        mig.MIGRATIONS[:] = saved
    run_migrations(conn)
    after_rows = conn.execute("SELECT * FROM corpus_chunks ORDER BY chunk_id").fetchall()
    after_hashes = [_hash_row(r) for r in after_rows]
    assert before_hashes == after_hashes
    assert len(after_hashes) == 2
    conn.close()


def test_v13_full_row_hash_detects_non_hash_column_mutation(tmp_path: Path):
    """Changing available_at/ticker/status must change full-row logical hash."""
    conn = sqlite3.connect(tmp_path / "t.db")
    _bootstrap_minimal_v12(conn)
    from catalyst_data import migrations as mig

    saved = list(mig.MIGRATIONS)
    mig.MIGRATIONS[:] = [m for m in saved if m.version <= 12]
    try:
        run_migrations(conn)
        _seed_filing_v2_chunk(conn, "chunk_z")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(corpus_chunks)")]
        row = conn.execute(
            f"SELECT {', '.join(cols)} FROM corpus_chunks WHERE chunk_id='chunk_z'"
        ).fetchone()
        base = _hash_row(row)
        # mutate non content_hash/metadata_hash fields in a copy via UPDATE
        conn.execute(
            "UPDATE corpus_chunks SET available_at='2099-01-01T00:00:00Z' WHERE chunk_id='chunk_z'"
        )
        mut = conn.execute(
            f"SELECT {', '.join(cols)} FROM corpus_chunks WHERE chunk_id='chunk_z'"
        ).fetchone()
        assert _hash_row(mut) != base
        conn.execute(
            "UPDATE corpus_chunks SET available_at='2025-08-01T00:00:00Z', "
            "ticker_associations='[\"ZZZZ\"]' WHERE chunk_id='chunk_z'"
        )
        mut2 = conn.execute(
            f"SELECT {', '.join(cols)} FROM corpus_chunks WHERE chunk_id='chunk_z'"
        ).fetchone()
        assert _hash_row(mut2) != base
        conn.execute(
            "UPDATE corpus_chunks SET status='embedded' WHERE chunk_id='chunk_z'"
        )
        mut3 = conn.execute(
            f"SELECT {', '.join(cols)} FROM corpus_chunks WHERE chunk_id='chunk_z'"
        ).fetchone()
        assert _hash_row(mut3) != base
    finally:
        mig.MIGRATIONS[:] = saved
    conn.close()

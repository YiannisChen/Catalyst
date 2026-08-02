from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from catalyst_data.candidate_recovery import (
    CandidateRecoveryError,
    fingerprint_source_tables,
    reset_pre_b6_candidate,
)


SOURCE_TABLES = (
    "raw_assets",
    "provider_request_attempts",
    "normalized_provenance",
    "articles",
    "article_tickers",
    "filings",
    "filing_documents",
)

FTS_SHADOW_TABLES = (
    "corpus_chunks_fts_data",
    "corpus_chunks_fts_idx",
    "corpus_chunks_fts_content",
    "corpus_chunks_fts_docsize",
    "corpus_chunks_fts_config",
)

EMPTY_FTS_STATE = {
    "corpus_chunks_fts": 0,
    "corpus_chunks_fts_data": 2,
    "corpus_chunks_fts_idx": 0,
    "corpus_chunks_fts_content": 0,
    "corpus_chunks_fts_docsize": 0,
    "corpus_chunks_fts_config": 1,
}


def _candidate(tmp_path: Path) -> Path:
    candidate_dir = tmp_path / "data" / "candidates"
    candidate_dir.mkdir(parents=True)
    path = candidate_dir / "catalyst_b2e_test_v13.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        PRAGMA user_version = 13;

        CREATE TABLE raw_assets (
            asset_id TEXT PRIMARY KEY,
            content_raw BLOB NOT NULL,
            response_sha256 TEXT,
            metadata_json TEXT NOT NULL
        );
        CREATE TABLE provider_request_attempts (
            request_id TEXT PRIMARY KEY,
            raw_asset_id TEXT REFERENCES raw_assets(asset_id),
            status TEXT NOT NULL
        );
        CREATE TABLE normalized_provenance (
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            entity_version TEXT NOT NULL,
            raw_asset_id TEXT NOT NULL REFERENCES raw_assets(asset_id),
            PRIMARY KEY (entity_type, entity_id, entity_version, raw_asset_id)
        );
        CREATE TABLE articles (
            article_id TEXT PRIMARY KEY,
            raw_asset_id TEXT NOT NULL REFERENCES raw_assets(asset_id),
            title TEXT NOT NULL
        );
        CREATE TABLE article_tickers (
            article_id TEXT NOT NULL REFERENCES articles(article_id),
            ticker TEXT NOT NULL,
            PRIMARY KEY (article_id, ticker)
        );
        CREATE TABLE filings (
            filing_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL
        );
        CREATE TABLE filing_documents (
            filing_id TEXT NOT NULL REFERENCES filings(filing_id),
            document_url TEXT NOT NULL,
            text TEXT,
            document_id TEXT,
            PRIMARY KEY (filing_id, document_url)
        );

        CREATE TABLE corpus_manifest (
            manifest_id TEXT PRIMARY KEY,
            manifest_json TEXT NOT NULL
        );
        CREATE TABLE corpus_chunks (
            chunk_id TEXT PRIMARY KEY,
            manifest_id TEXT REFERENCES corpus_manifest(manifest_id)
        );
        CREATE TABLE corpus_tombstones (
            chunk_id TEXT PRIMARY KEY,
            manifest_id TEXT REFERENCES corpus_manifest(manifest_id)
        );
        CREATE TABLE index_state (
            rowid INTEGER PRIMARY KEY,
            chunk_id TEXT NOT NULL
        );
        CREATE TABLE lexical_index_state (
            singleton_id INTEGER PRIMARY KEY,
            corpus_manifest_id TEXT REFERENCES corpus_manifest(manifest_id)
        );
        CREATE TABLE index_manifests (
            build_id TEXT PRIMARY KEY
        );
        CREATE VIRTUAL TABLE corpus_chunks_fts USING fts5(
            manifest_id UNINDEXED,
            chunk_id UNINDEXED,
            content_text,
            tokenize = 'unicode61 remove_diacritics 2'
        );

        INSERT INTO raw_assets VALUES ('raw:1', X'000102', 'abc123', '{"k":1}');
        INSERT INTO provider_request_attempts VALUES ('req:1', 'raw:1', 'SUCCEEDED');
        INSERT INTO normalized_provenance
            VALUES ('article', 'article:1', 'version:1', 'raw:1');
        INSERT INTO articles VALUES ('article:1', 'raw:1', 'Title');
        INSERT INTO article_tickers VALUES ('article:1', 'AAPL');
        INSERT INTO filings VALUES ('filing:1', 'AAPL');
        INSERT INTO filing_documents
            VALUES ('filing:1', 'https://example.test/a.htm', 'body', 'doc:1');

        INSERT INTO corpus_manifest VALUES ('manifest:1', '{}');
        INSERT INTO corpus_chunks VALUES ('chunk:1', 'manifest:1');
        INSERT INTO corpus_tombstones VALUES ('old:1', 'manifest:1');
        INSERT INTO index_state VALUES (1, 'chunk:1');
        INSERT INTO lexical_index_state VALUES (1, 'manifest:1');
        INSERT INTO index_manifests VALUES ('index:1');
        INSERT INTO corpus_chunks_fts VALUES ('manifest:1', 'chunk:1', 'body');
        """
    )
    conn.commit()
    conn.close()
    return path


def _derived_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        tables = (
            "corpus_chunks_fts",
            "lexical_index_state",
            "index_state",
            "corpus_tombstones",
            "corpus_chunks",
            "corpus_manifest",
            "index_manifests",
        )
        return {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables
        }
    finally:
        conn.close()


def _fts_state(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("corpus_chunks_fts", *FTS_SHADOW_TABLES)
        }
    finally:
        conn.close()


def test_reset_removes_only_rebuildable_state_and_writes_audit(tmp_path: Path):
    db_path = _candidate(tmp_path)
    audit_path = tmp_path / "reports" / "candidate-reset.json"
    before = fingerprint_source_tables(sqlite3.connect(db_path))

    result = reset_pre_b6_candidate(
        db_path,
        audit_path=audit_path,
        clock=lambda: "2026-08-01T00:00:00Z",
    )

    conn = sqlite3.connect(db_path)
    try:
        after = fingerprint_source_tables(conn)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
    finally:
        conn.close()

    assert before == after == result.source_tables_after
    assert all(count == 0 for count in _derived_counts(db_path).values())
    assert result.derived_rows_before["corpus_chunks"] == 1
    assert result.derived_rows_after == _derived_counts(db_path)
    audit = json.loads(audit_path.read_text())
    assert audit == result.to_dict()
    assert "fts_shadow_rows_before" in audit
    assert "fts_shadow_rows_after" in audit
    assert audit["fts_shadow_rows_after"] == EMPTY_FTS_STATE
    assert _fts_state(db_path) == EMPTY_FTS_STATE


def test_reset_is_idempotent(tmp_path: Path):
    db_path = _candidate(tmp_path)
    first = reset_pre_b6_candidate(
        db_path,
        audit_path=tmp_path / "reports" / "first.json",
        clock=lambda: "2026-08-01T00:00:00Z",
    )
    second = reset_pre_b6_candidate(
        db_path,
        audit_path=tmp_path / "reports" / "second.json",
        clock=lambda: "2026-08-01T00:01:00Z",
    )

    assert first.source_tables_after == second.source_tables_before
    assert all(count == 0 for count in second.derived_rows_before.values())
    assert second.derived_rows_before == second.derived_rows_after
    assert _fts_state(db_path) == EMPTY_FTS_STATE
    assert first.fts_shadow_rows_after == second.fts_shadow_rows_before
    assert second.fts_shadow_rows_after == EMPTY_FTS_STATE


def test_reset_removes_optional_streaming_generation_state(tmp_path: Path):
    from catalyst_data.corpus.streaming_publication import (
        ensure_streaming_publication_schema,
    )

    db_path = _candidate(tmp_path)
    conn = sqlite3.connect(db_path)
    ensure_streaming_publication_schema(conn)
    conn.execute(
        """INSERT INTO corpus_publication_builds
           (build_id, certified_snapshot_identity, header_json, status,
            created_at, updated_at)
           VALUES (?, ?, '{}', 'staging', ?, ?)""",
        ("b" * 64, "s" * 64, "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z"),
    )
    conn.execute(
        """INSERT INTO corpus_build_chunks_fts
           (build_id, chunk_id, content_text) VALUES (?, 'chunk:new', 'new text')""",
        ("b" * 64,),
    )
    conn.execute(
        """INSERT INTO corpus_build_fts_batches
           (build_id, batch_no, row_count, text_utf8_bytes, first_chunk_id,
            last_chunk_id, batch_digest, checkpoint_chunk_id, committed_at)
           VALUES (?, 1, 1, 8, 'chunk:new', 'chunk:new', ?, 'chunk:new', ?)""",
        ("b" * 64, "d" * 64, "2026-08-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    reset_pre_b6_candidate(
        db_path,
        audit_path=tmp_path / "reports" / "streaming-reset.json",
    )

    conn = sqlite3.connect(db_path)
    try:
        remaining = {
            row[0]
            for row in conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE name LIKE 'corpus_build_%'
                      OR name='corpus_publication_builds'
                      OR name='corpus_served_chunks'"""
            )
        }
    finally:
        conn.close()
    assert remaining == set()


def test_reset_rolls_back_every_delete_when_late_delete_fails(tmp_path: Path):
    db_path = _candidate(tmp_path)
    before_derived = _derived_counts(db_path)
    before_fts = _fts_state(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TRIGGER reject_manifest_delete
           BEFORE DELETE ON corpus_manifest
           BEGIN SELECT RAISE(ABORT, 'manifest_delete_rejected'); END"""
    )
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.IntegrityError, match="manifest_delete_rejected"):
        reset_pre_b6_candidate(
            db_path,
            audit_path=tmp_path / "reports" / "failed.json",
        )

    assert _derived_counts(db_path) == before_derived
    assert _fts_state(db_path) == before_fts
    assert not (tmp_path / "reports" / "failed.json").exists()


def test_reset_rejects_wrong_schema_version(tmp_path: Path):
    db_path = _candidate(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 12")
    conn.close()

    with pytest.raises(CandidateRecoveryError, match="user_version"):
        reset_pre_b6_candidate(
            db_path,
            audit_path=tmp_path / "reports" / "wrong-version.json",
        )

    assert _derived_counts(db_path)["corpus_chunks"] == 1


def test_source_fingerprint_detects_non_payload_changes(tmp_path: Path):
    db_path = _candidate(tmp_path)
    conn = sqlite3.connect(db_path)
    before = fingerprint_source_tables(conn)
    conn.execute("UPDATE articles SET title = 'Changed' WHERE article_id = 'article:1'")
    conn.commit()
    after = fingerprint_source_tables(conn)
    conn.close()

    assert set(before) == set(SOURCE_TABLES)
    assert before["articles"]["row_count"] == after["articles"]["row_count"] == 1
    assert before["articles"]["logical_sha256"] != after["articles"]["logical_sha256"]


def test_source_fingerprint_detects_same_length_payload_changes(tmp_path: Path):
    db_path = _candidate(tmp_path)
    conn = sqlite3.connect(db_path)
    before = fingerprint_source_tables(conn)
    conn.execute(
        "UPDATE raw_assets SET content_raw = ? WHERE asset_id = 'raw:1'",
        (b"xyz",),
    )
    conn.commit()
    after = fingerprint_source_tables(conn)
    conn.close()

    assert before["raw_assets"]["row_count"] == after["raw_assets"]["row_count"] == 1
    assert before["raw_assets"]["logical_sha256"] != after["raw_assets"]["logical_sha256"]


def test_source_fingerprint_distinguishes_blob_from_its_text_rendering(tmp_path: Path):
    db_path = _candidate(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE raw_assets SET content_raw = ? WHERE asset_id = 'raw:1'",
        (b"abc",),
    )
    conn.commit()
    blob_fingerprint = fingerprint_source_tables(conn)
    conn.execute(
        "UPDATE raw_assets SET content_raw = ? WHERE asset_id = 'raw:1'",
        ("b'abc'",),
    )
    conn.commit()
    text_fingerprint = fingerprint_source_tables(conn)
    conn.close()

    assert blob_fingerprint["raw_assets"]["logical_sha256"] != (
        text_fingerprint["raw_assets"]["logical_sha256"]
    )

"""Tests for index_manifests and index_state schema (polymorphic, additive DDL)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from catalyst_data.storage.sqlite import init_db


class TestIndexManifestsTable:
    def test_table_exists_after_init(self, tmp_path: Path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        init_db(conn)

        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r[0] for r in tables}
        assert "index_manifests" in table_names
        conn.close()

    def test_can_insert_row(self, tmp_path: Path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        init_db(conn)

        conn.execute(
            """INSERT INTO index_manifests
               (build_id, created_at, model, model_hash, lancedb_path,
                l1_count, l2_count, article_count, indexed_through_date,
                corpus_hash, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("build-001", "2025-01-01T00:00:00Z", "bge-m3",
             "abc123def456", "/tmp/lancedb/test",
             11772, 0, 11772, "2025-01-01", "corpushash...", "completed"),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM index_manifests WHERE build_id = ?", ("build-001",)
        ).fetchone()
        assert row is not None
        assert row[1] == "2025-01-01T00:00:00Z"  # created_at
        conn.close()

    def test_additive_ddl_idempotent(self, tmp_path: Path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        init_db(conn)
        init_db(conn)  # second call — no error
        conn.close()


class TestIndexStateTable:
    def test_table_exists_after_init(self, tmp_path: Path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        init_db(conn)

        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r[0] for r in tables}
        assert "index_state" in table_names
        conn.close()

    def test_polymorphic_pk(self, tmp_path: Path):
        """Primary key is chunk_id (auto-increment rowid). Polymorphic via (corpus_item_id, source_kind)."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        init_db(conn)

        pks = conn.execute("PRAGMA table_info(index_state)").fetchall()
        pk_cols = [c[1] for c in pks if c[5] > 0]  # c[5] = pk order
        # Step 4a: PK is rowid (auto-increment), chunk_id is indexed
        assert "rowid" in pk_cols or any("rowid" in str(c) for c in pks)
        conn.close()

    def test_no_fk_to_articles(self, tmp_path: Path):
        """index_state must NOT have FK to articles — it's polymorphic."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        init_db(conn)

        fks = conn.execute(
            "PRAGMA foreign_key_list(index_state)"
        ).fetchall()
        assert len(fks) == 0, f"index_state has unexpected FKs: {fks}"
        conn.close()

    def test_can_insert_article_row(self, tmp_path: Path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        init_db(conn)

        conn.execute(
            """INSERT INTO index_state
               (chunk_id, chunk_level, corpus_item_id, source_kind,
                content_hash, content_text, status, source_tier,
                tickers_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("poly:abc123::l1", "l1", "poly:abc123", "article",
             "a" * 64, "test content", "pending", 5, "[]"),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM index_state WHERE chunk_id = ?",
            ("poly:abc123::l1",),
        ).fetchone()
        assert row is not None
        assert row[1] == "poly:abc123::l1"  # chunk_id
        assert row[2] == "l1"  # chunk_level
        conn.close()

    def test_can_insert_filing_row(self, tmp_path: Path):
        """Future-proof: filing rows with source_kind='filing'."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        init_db(conn)

        conn.execute(
            """INSERT INTO index_state
               (chunk_id, chunk_level, corpus_item_id, source_kind,
                content_hash, content_text, status, source_tier,
                tickers_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("sec:filing::l1", "l1", "sec:0000320193:0000320193-25-000012", "filing",
             "b" * 64, "filing content", "pending", 1, "[]"),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM index_state WHERE corpus_item_id = ? AND source_kind = ?",
            ("sec:0000320193:0000320193-25-000012", "filing"),
        ).fetchone()
        assert row is not None
        assert row[4] == "filing"  # source_kind
        conn.close()

    def test_step4a_migrates_empty_legacy_index_state_schema(self, tmp_path: Path):
        """One-off Step 4a migration must match authoritative index_state DDL."""
        from catalyst_data.storage.sqlite import _migrate_index_state_step4a

        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            """CREATE TABLE index_state (
               corpus_item_id TEXT NOT NULL,
               source_kind TEXT NOT NULL,
               content_hash TEXT NOT NULL,
               PRIMARY KEY (corpus_item_id, source_kind)
            )"""
        )
        conn.commit()

        _migrate_index_state_step4a(conn)

        cols = [row[1] for row in conn.execute("PRAGMA table_info(index_state)")]
        assert cols.count("article_url") == 1
        assert {"chunk_id", "chunk_level", "content_text", "status"} <= set(cols)
        conn.close()

    def test_same_id_different_kind_coexist(self, tmp_path: Path):
        """An article and a filing can share corpus_item_id if source_kind differs."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        init_db(conn)

        conn.execute(
            "INSERT INTO index_state (chunk_id, chunk_level, corpus_item_id, source_kind, content_hash, content_text) VALUES (?, ?, ?, ?, ?, ?)",
            ("same::article::l1", "l1", "same_id", "article", "a" * 64, "article text"),
        )
        conn.execute(
            "INSERT INTO index_state (chunk_id, chunk_level, corpus_item_id, source_kind, content_hash, content_text) VALUES (?, ?, ?, ?, ?, ?)",
            ("same::filing::l1", "l1", "same_id", "filing", "b" * 64, "filing text"),
        )
        conn.commit()

        count = conn.execute(
            "SELECT COUNT(*) FROM index_state WHERE corpus_item_id = ?", ("same_id",)
        ).fetchone()[0]
        assert count == 2
        conn.close()

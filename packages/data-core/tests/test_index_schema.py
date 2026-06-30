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
        """Primary key is (corpus_item_id, source_kind) — not article_id only."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        init_db(conn)

        pks = conn.execute("PRAGMA table_info(index_state)").fetchall()
        pk_cols = [c[1] for c in pks if c[5] > 0]  # c[5] = pk order
        assert "corpus_item_id" in pk_cols
        assert "source_kind" in pk_cols
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
            """INSERT OR REPLACE INTO index_state
               (corpus_item_id, source_kind, content_hash, source_tier,
                indexed_build_id, indexed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("poly:abc123", "article",
             "a" * 64, 5,
             "build-001", "2025-01-01T00:00:00Z"),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM index_state WHERE corpus_item_id = ? AND source_kind = ?",
            ("poly:abc123", "article"),
        ).fetchone()
        assert row is not None
        assert row[0] == "poly:abc123"
        assert row[1] == "article"
        conn.close()

    def test_can_insert_filing_row(self, tmp_path: Path):
        """Future-proof: filing rows (sec:{cik}:{accession}, source_kind='filing')."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        init_db(conn)

        conn.execute(
            """INSERT OR REPLACE INTO index_state
               (corpus_item_id, source_kind, content_hash, source_tier,
                indexed_build_id, indexed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("sec:0000320193:0000320193-25-000012", "filing",
             "b" * 64, 1,
             "build-002", "2025-01-02T00:00:00Z"),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM index_state WHERE corpus_item_id = ? AND source_kind = ?",
            ("sec:0000320193:0000320193-25-000012", "filing"),
        ).fetchone()
        assert row is not None
        assert row[1] == "filing"
        conn.close()

    def test_same_id_different_kind_coexist(self, tmp_path: Path):
        """An article and a filing can share corpus_item_id if source_kind differs."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        init_db(conn)

        conn.execute(
            "INSERT OR REPLACE INTO index_state VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("same_id", "article", "a" * 64, 5, None, "build-001", "2025-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO index_state VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("same_id", "filing", "b" * 64, 1, None, "build-001", "2025-01-01T00:00:00Z"),
        )
        conn.commit()

        count = conn.execute(
            "SELECT COUNT(*) FROM index_state WHERE corpus_item_id = ?", ("same_id",)
        ).fetchone()[0]
        assert count == 2
        conn.close()

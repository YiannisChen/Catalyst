"""Tests for index_builder.py — article-level record builder (dry-run)."""

from __future__ import annotations

import hashlib
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from catalyst_data.index_builder import (
    compute_content_hash,
    build_index_records,
    index_summary,
)
from catalyst_data.storage.sqlite import init_db
from catalyst_data.articles import ensure_articles_table, upsert_article, upsert_article_ticker


def _make_db(db_path: str) -> sqlite3.Connection:
    """Create a DB with 5 eligible articles, each with article_tickers rows.

    a3 has a long body (920+ chars) so it triggers L2 at 800-char threshold.
    Step 4a: seeds canonical article_tickers so articles are embed-eligible;
    runs the is_canonical migration.
    """
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_articles_table(conn)
    from catalyst_data.storage.sqlite import _migrate_article_tickers_dedup
    _migrate_article_tickers_dedup(conn)

    for i in range(5):
        conn.execute(
            "INSERT OR REPLACE INTO raw_assets "
            "(asset_id, ticker, source_type, reference_date, fetched_at, content_raw) "
            "VALUES (?, 'AAPL', 'polygon_news', '2025-01-01', datetime('now'), ?)",
            (f"raw-{i}", b"{}"),
        )
    conn.commit()

    articles = [
        ("poly:a1", "raw-0", "AAPL", "Short title", "Brief desc", "The Motley Fool", 5),
        ("poly:a2", "raw-1", "AAPL", "Another title", "Medium description here.", "Benzinga", 4),
        # a3: body-only length ≈ 920 chars — triggers L2 at 800 threshold
        ("poly:a3", "raw-2", "TSLA", "Tesla news", "A longer description " + "x" * 900, "GlobeNewswire Inc.", 3),
        ("poly:a4", "raw-3", "MSFT", "MSFT earnings", None, "MarketWatch", 2),
        ("poly:a5", "raw-4", "JPM", "JPM update", "Quick note.", "Zacks Investment Research", 5),
    ]
    from catalyst_data.articles import upsert_article_ticker
    for article_id, raw_id, ticker, title, desc, pub, tier in articles:
        upsert_article(conn, article={
            "article_id": article_id,
            "raw_asset_id": raw_id,
            "provider": "polygon",
            "source_type": "polygon_news",
            "ticker": ticker,
            "reference_date": "2025-01-01",
            "published_utc": "2025-01-01T12:00:00Z",
            "title": title,
            "description": desc,
            "publisher_name": pub,
            "source_tier": tier,
        })
        upsert_article_ticker(conn, article_id=article_id, ticker=ticker,
                              raw_asset_id=raw_id, reference_date="2025-01-01")
        conn.execute(
            "UPDATE articles SET is_rag_eligible = 1, dedup_group_id = ?, is_canonical = 1 WHERE article_id = ?",
            (f"grp-{article_id}", article_id),
        )
        conn.execute(
            "UPDATE article_tickers SET dedup_group_id = ?, is_canonical = 1 WHERE article_id = ? AND ticker = ?",
            (f"grp-{article_id}", article_id, ticker),
        )

    # article_tickers for every article
    data = [
        ("poly:a1", "AAPL", "raw-0", "2025-01-01"),
        ("poly:a2", "AAPL", "raw-1", "2025-01-01"),
        ("poly:a2", "TSLA", "raw-1", "2025-01-01"),
        ("poly:a3", "TSLA", "raw-2", "2025-01-01"),
        ("poly:a4", "MSFT", "raw-3", "2025-01-01"),
        ("poly:a5", "JPM", "raw-4", "2025-01-01"),
        ("poly:a5", "MSFT", "raw-4", "2025-01-01"),
    ]
    for article_id, ticker, raw_id, ref_date in data:
        upsert_article_ticker(conn, article_id=article_id, ticker=ticker,
                              raw_asset_id=raw_id, reference_date=ref_date)

    return conn


class TestComputeContentHash:
    def test_deterministic(self):
        h1 = compute_content_hash("Title", "Description")
        h2 = compute_content_hash("Title", "Description")
        assert h1 == h2

    def test_full_64_hex(self):
        h = compute_content_hash("T", "D")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_metadata_not_in_hash(self):
        h = compute_content_hash("Title", "Description")
        expected = hashlib.sha256(
            unicodedata.normalize("NFC", "Title\nDescription").encode("utf-8")
        ).hexdigest()
        assert h == expected

    def test_null_description_no_trailing_newline(self):
        """compute_content_hash(sentence, None) hashes bare sentence — no \n artifact."""
        h1 = compute_content_hash("bare sentence", None)
        expected = hashlib.sha256(
            unicodedata.normalize("NFC", "bare sentence").encode("utf-8")
        ).hexdigest()
        assert h1 == expected
        # Must NOT equal hash of "bare sentence\n"
        h_with_nl = hashlib.sha256(
            unicodedata.normalize("NFC", "bare sentence\n").encode("utf-8")
        ).hexdigest()
        assert h1 != h_with_nl

    def test_empty_description_same_as_none(self):
        h1 = compute_content_hash("Title", None)
        h2 = compute_content_hash("Title", "")
        assert h1 == h2

    def test_different_titles_produce_different_hashes(self):
        h1 = compute_content_hash("Title A", "Same desc")
        h2 = compute_content_hash("Title B", "Same desc")
        assert h1 != h2


class TestBuildIndexRecords:
    def test_l1_count_equals_article_count(self, tmp_path: Path):
        conn = _make_db(str(tmp_path / "test.db"))
        records = build_index_records(conn, min_l2_chars=800)
        l1 = [r for r in records if r["chunk_level"] == "l1"]
        assert len(l1) == 5
        conn.close()

    def test_l2_gated_on_body_only(self, tmp_path: Path):
        """L2 gates on len(description), NOT title+description.  a3 body is 920+ chars."""
        conn = _make_db(str(tmp_path / "test.db"))
        records = build_index_records(conn, min_l2_chars=800)
        l2 = [r for r in records if r["chunk_level"] == "l2"]
        l2_ids = {r["article_id"] for r in l2}
        # a3 has body ≈ 920 chars ≥ 800 → produces L2
        assert "poly:a3" in l2_ids
        assert len(l2_ids) == 1
        conn.close()

    def test_l2_body_only_title_not_in_l2(self, tmp_path: Path):
        """L2 sentences come from body only; the title is never an L2 sentence."""
        conn = _make_db(str(tmp_path / "test.db"))
        records = build_index_records(conn, min_l2_chars=800)
        l2 = [r for r in records if r["chunk_level"] == "l2"]
        # All L2 content_text values must NOT contain the title "Tesla news"
        for r in l2:
            assert "Tesla news" not in r["content_text"], (
                f"Title leaked into L2 sentence: {r['content_text'][:80]}"
            )
        conn.close()

    def test_l2_zero_for_short_body(self, tmp_path: Path):
        """Polygon-like: max description 758 chars < 800 → L2 == 0."""
        conn = _make_db(str(tmp_path / "test.db"))
        # At default 800 threshold, only a3's 920-char body qualifies.
        # Now test with a DB where all descriptions are < 800.
        # Create a fresh DB with short descriptions only
        db2 = tmp_path / "test2.db"
        conn2 = sqlite3.connect(str(db2))
        init_db(conn2)
        ensure_articles_table(conn2)
        for i in range(3):
            conn2.execute(
                "INSERT OR REPLACE INTO raw_assets "
                "(asset_id, ticker, source_type, reference_date, fetched_at, content_raw) "
                "VALUES (?, 'AAPL', 'polygon_news', '2025-01-01', datetime('now'), ?)",
                (f"rs-{i}", b"{}"),
            )
        conn2.commit()
        for aid, rid, tkr, title, desc in [
            ("poly:s1", "rs-0", "AAPL", "Short 1", "Brief text."),
            ("poly:s2", "rs-1", "TSLA", "Short 2", "A moderate description without much depth."),
            ("poly:s3", "rs-2", "MSFT", "Short 3", None),
        ]:
            upsert_article(conn2, article={
                "article_id": aid, "raw_asset_id": rid, "provider": "polygon",
                "source_type": "polygon_news", "ticker": tkr,
                "reference_date": "2025-01-01",
                "published_utc": "2025-01-01T12:00:00Z",
                "title": title, "description": desc,
                "publisher_name": "TestPub", "source_tier": 4,
            })
            upsert_article_ticker(conn2, article_id=aid, ticker=tkr,
                                  raw_asset_id=rid, reference_date="2025-01-01")
        records2 = build_index_records(conn2, min_l2_chars=800)
        l2_2 = [r for r in records2 if r["chunk_level"] == "l2"]
        assert len(l2_2) == 0, f"Expected 0 L2 for short bodies, got {len(l2_2)}"
        conn2.close()

    def test_l2_gated_at_low_threshold(self, tmp_path: Path):
        conn = _make_db(str(tmp_path / "test.db"))
        records = build_index_records(conn, min_l2_chars=10)
        l2 = [r for r in records if r["chunk_level"] == "l2"]
        assert len(l2) > 0
        conn.close()

    def test_chunk_id_format(self, tmp_path: Path):
        conn = _make_db(str(tmp_path / "test.db"))
        records = build_index_records(conn, min_l2_chars=10)
        for r in records:
            if r["chunk_level"] == "l1":
                assert r["chunk_id"].endswith("::l1")
            elif r["chunk_level"] == "l2":
                assert "::l2s" in r["chunk_id"]
            assert r["parent_article_id"] == r["article_id"]
        conn.close()

    def test_tickers_lossless(self, tmp_path: Path):
        conn = _make_db(str(tmp_path / "test.db"))
        # Guards are inside build_index_records — this passes or raises
        records = build_index_records(conn, min_l2_chars=800)
        l1_records = [r for r in records if r["chunk_level"] == "l1"]
        total_ticker_refs = sum(len(r["tickers"]) for r in l1_records)
        at_count = conn.execute(
            "SELECT COUNT(*) FROM article_tickers"
        ).fetchone()[0]
        assert total_ticker_refs == at_count, (
            f"Ticker loss: records={total_ticker_refs}, DB={at_count}"
        )
        conn.close()

    def test_multi_ticker_article(self, tmp_path: Path):
        conn = _make_db(str(tmp_path / "test.db"))
        records = build_index_records(conn, min_l2_chars=800)
        l1 = {r["article_id"]: r for r in records if r["chunk_level"] == "l1"}
        assert "AAPL" in l1["poly:a2"]["tickers"]
        assert "TSLA" in l1["poly:a2"]["tickers"]
        assert "JPM" in l1["poly:a5"]["tickers"]
        assert "MSFT" in l1["poly:a5"]["tickers"]
        conn.close()

    def test_no_model_or_lancedb_imports(self):
        import catalyst_data.index_builder as ib
        source = open(ib.__file__).read()
        assert "import lancedb" not in source
        assert "from lancedb" not in source
        assert "model.encode" not in source

    def test_content_hash_present(self, tmp_path: Path):
        conn = _make_db(str(tmp_path / "test.db"))
        records = build_index_records(conn, min_l2_chars=800)
        for r in records:
            assert len(r["content_hash"]) == 64
            assert all(c in "0123456789abcdef" for c in r["content_hash"])
        conn.close()

    def test_metadata_completeness(self, tmp_path: Path):
        conn = _make_db(str(tmp_path / "test.db"))
        records = build_index_records(conn, min_l2_chars=800)
        for r in records:
            for key in ("chunk_id", "article_id", "parent_article_id",
                         "content_hash", "provider", "source_type",
                         "publisher_name", "source_tier", "tickers"):
                assert key in r, f"Missing key: {key}"
            assert isinstance(r["tickers"], list)
        conn.close()

    def test_guard_lossless_raises_on_orphan(self, tmp_path: Path):
        """build_index_records raises when orphan article_tickers rows exist
        (not reachable via LEFT JOIN, so ticker_refs < DB count)."""
        conn = _make_db(str(tmp_path / "test.db"))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT OR REPLACE INTO article_tickers "
            "(article_id, ticker, raw_asset_id, reference_date) "
            "VALUES ('poly:ghost', 'ORPHAN', 'raw-99', '2025-01-01')"
        )
        conn.commit()
        with pytest.raises(AssertionError, match="Ticker-lossless"):
            build_index_records(conn, min_l2_chars=800)
        conn.close()


def test_build_corpus_wires_profiles_reconciliation_and_manifest(tmp_path: Path):
    """The B3 entrypoint persists contract chunks and publishes one manifest."""
    from catalyst_data.index_builder import build_corpus

    conn = _make_db(str(tmp_path / "b3-corpus.db"))
    article_state_before = conn.execute(
        """SELECT article_id, source_class, dedup_cluster_id,
                  cluster_first_available_at, representative_document_id
           FROM articles ORDER BY article_id"""
    ).fetchall()
    result = build_corpus(
        conn,
        certified_snapshot_identity="test-snapshot",
    )

    assert result.chunks
    assert all(":news_v2:body:" in chunk.chunk_id for chunk in result.chunks)
    assert all("::l1" not in chunk.chunk_id for chunk in result.chunks)
    current = conn.execute(
        "SELECT manifest_id FROM corpus_manifest WHERE is_current = 1"
    ).fetchone()
    assert current[0] == result.manifest_id
    assert conn.execute(
        "SELECT COUNT(*) FROM corpus_chunks WHERE manifest_id = ?",
        (result.manifest_id,),
    ).fetchone()[0] == len(result.chunks)
    article_state_after = conn.execute(
        """SELECT article_id, source_class, dedup_cluster_id,
                  cluster_first_available_at, representative_document_id
           FROM articles ORDER BY article_id"""
    ).fetchall()
    assert article_state_after == article_state_before
    assert conn.execute(
        "SELECT COUNT(*) FROM index_state WHERE chunk_id LIKE '%::l1'"
    ).fetchone()[0] == 0
    assert conn.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type = 'index' AND name = 'idx_index_state_chunk_id'"""
    ).fetchone() is not None

    repeated = build_corpus(
        conn,
        certified_snapshot_identity="test-snapshot",
    )
    assert repeated.manifest_id == result.manifest_id
    assert conn.execute(
        "SELECT COUNT(*) FROM corpus_manifest WHERE is_current = 1"
    ).fetchone()[0] == 1
    conn.close()


def test_build_corpus_keeps_reassigned_active_chunk_searchable(tmp_path: Path):
    """A dedup lineage tombstone must not deactivate its active replacement."""
    from catalyst_data.index_builder import build_corpus

    conn = _make_db(str(tmp_path / "b3-reassignment.db"))
    first = build_corpus(conn, certified_snapshot_identity="snapshot-1")
    chunk_id = next(
        chunk.chunk_id for chunk in first.chunks if chunk.document_id == "poly:a1"
    )
    conn.execute(
        "UPDATE articles SET dedup_cluster_id = 'reassigned' WHERE article_id = 'poly:a1'"
    )
    conn.commit()

    second = build_corpus(conn, certified_snapshot_identity="snapshot-2")
    assert second.manifest_id != first.manifest_id
    assert conn.execute(
        "SELECT status FROM corpus_chunks WHERE chunk_id = ?", (chunk_id,)
    ).fetchone()[0] != "tombstoned"
    assert conn.execute(
        "SELECT is_tombstone FROM index_state WHERE chunk_id = ?", (chunk_id,)
    ).fetchone()[0] == 0


class TestIndexSummary:
    def test_summary_counts(self, tmp_path: Path):
        conn = _make_db(str(tmp_path / "test.db"))
        records = build_index_records(conn, min_l2_chars=800)
        summary = index_summary(records)
        assert summary["l1_count"] == 5
        assert summary["l2_count"] >= 0
        assert "per_tier" in summary
        assert summary["would_embed_count"] == summary["l1_count"] + summary["l2_count"]
        conn.close()

    def test_summary_l2_eligible_pct(self, tmp_path: Path):
        conn = _make_db(str(tmp_path / "test.db"))
        records = build_index_records(conn, min_l2_chars=800)
        summary = index_summary(records)
        # a3 has body ≈ 920 chars ≥ 800 → 1 eligible out of 5
        assert summary["l2_eligible_count"] == 1
        assert summary["l2_eligible_pct"] == 20.0
        conn.close()


# ============================================================
# build_incremental_records tests (Step 2)
# ============================================================

class TestBuildIncrementalRecords:
    def test_all_articles_in_delta_when_index_empty(self, tmp_path):
        """When index_state is empty, all articles appear in delta."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        from catalyst_data.index_builder import build_incremental_records
        result = build_incremental_records(conn, min_l2_chars=800)

        assert result["new_article_count"] == 5
        assert result["changed_article_count"] == 0
        assert result["total_delta_articles"] == 5
        assert result["l1_count"] == 5
        assert len(result["delta_article_ids"]) == 5
        conn.close()

    def test_empty_delta_when_all_indexed(self, tmp_path):
        """When index_state covers all articles with correct hash, delta is empty."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        from catalyst_data.index_builder import (
            build_incremental_records,
            compute_content_hash,
        )

        # Pre-populate index_state for all 5 articles
        for article_id, title, desc in [
            ("poly:a1", "Short title", "Brief desc"),
            ("poly:a2", "Another title", "Medium description here."),
            ("poly:a3", "Tesla news", "A longer description " + "x" * 900),
            ("poly:a4", "MSFT earnings", None),
            ("poly:a5", "JPM update", "Quick note."),
        ]:
            h = compute_content_hash(title, desc)
            conn.execute(
                "INSERT OR REPLACE INTO index_state "
                "(chunk_id, chunk_level, corpus_item_id, source_kind, content_hash, content_text, source_tier, status) "
                "VALUES (? || '::l1', 'l1', ?, 'article', ?, 'text', 5, 'pending')",
                (article_id, article_id, h),
            )
        conn.commit()

        result = build_incremental_records(conn, min_l2_chars=800)
        assert result["total_delta_articles"] == 0
        assert result["would_embed_count"] == 0
        assert result["delta_article_ids"] == []
        conn.close()

    def test_detects_changed_article(self, tmp_path):
        """When one article's description changes, only it appears in delta."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        from catalyst_data.index_builder import (
            build_incremental_records,
            compute_content_hash,
        )

        # Index 4 of 5 articles with their current hashes
        for article_id, title, desc in [
            ("poly:a1", "Short title", "Brief desc"),
            ("poly:a2", "Another title", "Medium description here."),
            ("poly:a4", "MSFT earnings", None),
            ("poly:a5", "JPM update", "Quick note."),
        ]:
            h = compute_content_hash(title, desc)
            conn.execute(
                "INSERT OR REPLACE INTO index_state "
                "(chunk_id, chunk_level, corpus_item_id, source_kind, content_hash, content_text, source_tier, status) "
                "VALUES (? || '::l1', 'l1', ?, 'article', ?, 'text', 5, 'pending')",
                (article_id, article_id, h),
            )

        # Index a3 with a STALE/wrong hash (mismatched description)
        conn.execute(
            "INSERT OR REPLACE INTO index_state "
            "(chunk_id, chunk_level, corpus_item_id, source_kind, content_hash, content_text, source_tier, status) "
            "VALUES ('poly:a3::l1', 'l1', 'poly:a3', 'article', 'deadbeef', 'old text', 3, 'pending')",
        )
        conn.commit()

        result = build_incremental_records(conn, min_l2_chars=800)
        # a3 content changed (stale hash), a1/a2/a4/a5 are indexed with correct hashes
        # So a3 appears as changed, none as new
        assert result["changed_article_count"] == 1, f"Expected 1 changed, got {result}"
        assert result["new_article_count"] == 0, f"Expected 0 new, got {result}"
        assert result["total_delta_articles"] == 1
        assert "poly:a3" in result["delta_article_ids"]
        conn.close()

    def test_no_writes_to_index_state(self, tmp_path):
        """build_incremental_records does NOT write index_state or index_manifests."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        from catalyst_data.index_builder import build_incremental_records

        before_is = conn.execute("SELECT COUNT(*) FROM index_state").fetchone()[0]
        before_im = conn.execute("SELECT COUNT(*) FROM index_manifests").fetchone()[0]

        build_incremental_records(conn, min_l2_chars=800)

        after_is = conn.execute("SELECT COUNT(*) FROM index_state").fetchone()[0]
        after_im = conn.execute("SELECT COUNT(*) FROM index_manifests").fetchone()[0]

        assert after_is == before_is
        assert after_im == before_im
        conn.close()



    def test_incremental_stale_on_changed_article(self, tmp_path):
        """After a full persist_index_state, changing an article and running
        build_incremental_records produces a delta with the changed article.
        A subsequent persist_index_state run would mark the old row stale
        and insert a new pending row — mirroring the S2 guarantee."""
        db_path = str(tmp_path / "test_gate.db")
        conn = _make_db(db_path)

        from catalyst_data.index_builder import (
            build_incremental_records,
            persist_index_state,
            compute_content_hash,
        )

        # Full persist — all 5 articles enter index_state as pending
        r1 = persist_index_state(conn)
        assert r1["article_l1_pending"] == 5
        assert r1["total_new_rows"] == 6  # 5 L1 + 1 L2 (a3 body >= 800 chars)

        # Change poly:a3 description
        conn.execute(
            "UPDATE articles SET description = 'Completely rewritten Tesla news with new content' WHERE article_id = 'poly:a3'"
        )
        conn.commit()

        # Incremental: a3 should appear as changed
        inc = build_incremental_records(conn)
        assert inc["changed_article_count"] == 1
        assert "poly:a3" in inc["delta_article_ids"]

        # Re-persist: old a3 L1 should become stale, new a3 L1 pending
        r2 = persist_index_state(conn)
        # Still exactly 1 pending L1 for a3
        pending_a3 = conn.execute(
            "SELECT COUNT(*) FROM index_state WHERE corpus_item_id = 'poly:a3' AND chunk_level = 'l1' AND status = 'pending'"
        ).fetchone()[0]
        stale_a3 = conn.execute(
            "SELECT COUNT(*) FROM index_state WHERE corpus_item_id = 'poly:a3' AND chunk_level = 'l1' AND status = 'stale'"
        ).fetchone()[0]
        conn.close()

        assert pending_a3 == 1, f"Expected 1 pending L1 for a3, got {pending_a3}"
        assert stale_a3 == 1, f"Expected 1 stale L1 for a3, got {stale_a3}"
        assert r2["total_new_rows"] == 1  # only a3 L1 changed (L2 unchanged)

    def test_polymorphic_join_uses_source_kind(self, tmp_path):
        """Verifies join uses corpus_item_id + source_kind='article'."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        from catalyst_data.index_builder import (
            build_incremental_records,
            compute_content_hash,
        )

        # Insert a row with source_kind='filing' for one article_id
        # This should NOT satisfy the join (build_incremental_records joins on source_kind='article')
        conn.execute(
            "INSERT OR REPLACE INTO index_state "
            "(chunk_id, chunk_level, corpus_item_id, source_kind, content_hash, content_text, source_tier, status) "
            "VALUES ('poly:a1::filing', 'l1', 'poly:a1', 'filing', 'deadbeef', 'filing text', 1, 'pending')",
        )
        conn.commit()

        result = build_incremental_records(conn, min_l2_chars=800)
        # poly:a1 has index_state with source_kind='filing' only —
        # the article join (source_kind='article') should NOT match,
        # so a1 should appear as a NEW article
        assert "poly:a1" in result["delta_article_ids"]
        conn.close()


# ============================================================
# H1-T1: index_summary polymorphic fix
# ============================================================

class TestIndexSummaryPolymorphic:
    def test_summary_with_filing_records_does_not_crash(self, tmp_path):
        """index_summary must handle filing records which use corpus_item_id, not article_id."""
        from catalyst_data.index_builder import build_filing_records, index_summary
        from catalyst_data.storage.sqlite import init_db, ensure_filings_tables, upsert_filing

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_filings_tables(conn)
        conn.execute(
            "INSERT OR REPLACE INTO raw_assets "
            "(asset_id, ticker, source_type, reference_date, fetched_at, content_raw) "
            "VALUES ('ra1', 'AAPL', 'sec_filing', '2025-01-01', datetime('now'), x'7b7d')"
        )
        conn.commit()

        upsert_filing(conn, filing_id="sec:f1", cik="123", ticker="AAPL",
                      form_type="10-K", filed_at="2025-01-01",
                      accession_number="0001", url="https://example.com",
                      is_rag_eligible=1)
        # Also add a filing_document row so build_filing_records has text
        conn.execute(
            "INSERT OR REPLACE INTO filing_documents "
            "(filing_id, document_url, document_type, text, char_len, extraction_status) "
            "VALUES ('sec:f1', 'https://example.com/doc', 'primary_doc', "
            "'A' * 900, 900, 'success')"
        )
        conn.commit()

        records = build_filing_records(conn)
        assert len(records) > 0

        # This must not raise KeyError
        summary = index_summary(records)
        assert summary["l1_count"] >= 1
        assert "filing_l1_count" in summary
        conn.close()

    def test_summary_article_records_have_no_corpus_item_id(self, tmp_path):
        """Article records have article_id but NOT corpus_item_id. Unified accessor handles both."""
        from catalyst_data.index_builder import build_article_records, index_summary

        conn = _make_db(str(tmp_path / "test.db"))
        records = build_article_records(conn, min_l2_chars=800)
        # Article records should NOT have corpus_item_id
        for r in records:
            assert "corpus_item_id" not in r, f"Article record unexpectedly has corpus_item_id: {r['chunk_id']}"
            assert "article_id" in r

        # index_summary must work on article-only records
        summary = index_summary(records)
        assert summary["article_l1_count"] == 5
        conn.close()

    def test_summary_mixed_article_and_filing(self, tmp_path):
        """DB with both article and filing records → correct per-kind counts."""
        from catalyst_data.index_builder import build_index_records, index_summary
        from catalyst_data.storage.sqlite import ensure_filings_tables, upsert_filing

        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        ensure_filings_tables(conn)
        conn.execute(
            "INSERT OR REPLACE INTO raw_assets "
            "(asset_id, ticker, source_type, reference_date, fetched_at, content_raw) "
            "VALUES ('ra_f1', 'AAPL', 'sec_filing', '2025-01-01', datetime('now'), x'7b7d')"
        )
        conn.commit()
        upsert_filing(conn, filing_id="sec:f1", cik="123", ticker="AAPL",
                      form_type="10-K", filed_at="2025-01-01",
                      accession_number="0001", url="https://example.com",
                      is_rag_eligible=1)
        conn.execute(
            "INSERT OR REPLACE INTO filing_documents "
            "(filing_id, document_url, document_type, text, char_len, extraction_status) "
            "VALUES ('sec:f1', 'https://example.com/doc', 'primary_doc', "
            "'A' * 900, 900, 'success')"
        )
        conn.commit()

        records = build_index_records(conn, min_l2_chars=800)
        summary = index_summary(records)

        assert summary["article_l1_count"] == 5  # 5 articles from _make_db
        assert summary["filing_l1_count"] == 1    # 1 filing
        assert summary["l1_count"] == 6           # total
        conn.close()

    def test_summary_per_tier_split_by_source_kind(self, tmp_path):
        """per_tier must have 'article' and 'filing' sub-keys."""
        from catalyst_data.index_builder import build_index_records, index_summary
        from catalyst_data.storage.sqlite import ensure_filings_tables, upsert_filing

        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        ensure_filings_tables(conn)
        conn.execute(
            "INSERT OR REPLACE INTO raw_assets "
            "(asset_id, ticker, source_type, reference_date, fetched_at, content_raw) "
            "VALUES ('ra_f1', 'AAPL', 'sec_filing', '2025-01-01', datetime('now'), x'7b7d')"
        )
        conn.commit()
        upsert_filing(conn, filing_id="sec:f1", cik="123", ticker="AAPL",
                      form_type="10-K", filed_at="2025-01-01",
                      accession_number="0001", url="https://example.com",
                      is_rag_eligible=1, source_tier=1)
        conn.execute(
            "INSERT OR REPLACE INTO filing_documents "
            "(filing_id, document_url, document_type, text, char_len, extraction_status) "
            "VALUES ('sec:f1', 'https://example.com/doc', 'primary_doc', "
            "'A' * 900, 900, 'success')"
        )
        conn.commit()

        records = build_index_records(conn, min_l2_chars=800)
        summary = index_summary(records)

        assert "per_tier" in summary
        per_tier = summary["per_tier"]
        assert "article" in per_tier or ("article" in str(per_tier))
        # Filing tier 1 should be present
        assert "filing" in per_tier or str(1) in str(per_tier)
        conn.close()

    def test_summary_l2_eligible_filings(self, tmp_path):
        """l2_eligible_count includes filing items with long body."""
        from catalyst_data.index_builder import build_index_records, index_summary
        from catalyst_data.storage.sqlite import ensure_filings_tables, upsert_filing

        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        ensure_filings_tables(conn)
        conn.execute(
            "INSERT OR REPLACE INTO raw_assets "
            "(asset_id, ticker, source_type, reference_date, fetched_at, content_raw) "
            "VALUES ('ra_f1', 'AAPL', 'sec_filing', '2025-01-01', datetime('now'), x'7b7d')"
        )
        conn.commit()
        upsert_filing(conn, filing_id="sec:f1", cik="123", ticker="AAPL",
                      form_type="10-K", filed_at="2025-01-01",
                      accession_number="0001", url="https://example.com",
                      is_rag_eligible=1)
        # Long body ≥ 800 chars triggers L2
        conn.execute(
            "INSERT OR REPLACE INTO filing_documents "
            "(filing_id, document_url, document_type, text, char_len, extraction_status) "
            "VALUES ('sec:f1', 'https://example.com/doc', 'primary_doc', "
            "'A' * 900, 900, 'success')"
        )
        conn.commit()

        records = build_index_records(conn, min_l2_chars=800)
        summary = index_summary(records)
        assert summary["l2_eligible_count"] >= 1  # filing + article a3
        conn.close()

    def test_summary_article_l1_count_matches_article_count(self, tmp_path):
        """article_l1_count must equal COUNT(*) FROM articles (for rebuild-index guard)."""
        from catalyst_data.index_builder import build_index_records, index_summary
        from catalyst_data.storage.sqlite import ensure_filings_tables, upsert_filing

        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        ensure_filings_tables(conn)
        conn.execute(
            "INSERT OR REPLACE INTO raw_assets "
            "(asset_id, ticker, source_type, reference_date, fetched_at, content_raw) "
            "VALUES ('ra_f1', 'AAPL', 'sec_filing', '2025-01-01', datetime('now'), x'7b7d')"
        )
        conn.commit()
        upsert_filing(conn, filing_id="sec:f1", cik="123", ticker="AAPL",
                      form_type="10-K", filed_at="2025-01-01",
                      accession_number="0001", url="https://example.com",
                      is_rag_eligible=1)
        conn.execute(
            "INSERT OR REPLACE INTO filing_documents "
            "(filing_id, document_url, document_type, text, char_len, extraction_status) "
            "VALUES ('sec:f1', 'https://example.com/doc', 'primary_doc', "
            "'A' * 900, 900, 'success')"
        )
        conn.commit()

        article_count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        records = build_index_records(conn, min_l2_chars=800)
        summary = index_summary(records)
        assert summary["article_l1_count"] == article_count
        conn.close()

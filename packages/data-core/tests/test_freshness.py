"""Tests for freshness.py — news + index staleness (read-only)."""

from __future__ import annotations

import sqlite3

import pytest

from catalyst_data.freshness import (
    latest_local_ohlcv_date,
    news_freshness,
    index_freshness,
    freshness_report,
)
from catalyst_data.storage.sqlite import init_db
from catalyst_data.articles import ensure_articles_table, upsert_article, upsert_article_ticker


def _make_db(db_path: str, *, with_articles: bool = True) -> sqlite3.Connection:
    """Create a minimal DB with ohlcv and optional articles."""
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_articles_table(conn)

    # OHLCV — 3 trading days
    for dt in ("2026-05-01", "2026-04-30", "2026-04-29"):
        conn.execute(
            "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES (?, ?, 100.0)",
            ("AAPL", dt),
        )
        conn.execute(
            "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES (?, ?, 200.0)",
            ("TSLA", dt),
        )

    if with_articles:
        # Raw assets
        for i, (ticker, ref_date) in enumerate([
            ("AAPL", "2026-05-01"),  # FRESH
            ("TSLA", "2026-04-30"),  # STALE (1 day behind)
        ]):
            conn.execute(
                "INSERT OR REPLACE INTO raw_assets "
                "(asset_id, ticker, source_type, reference_date, fetched_at, content_raw) "
                "VALUES (?, ?, 'polygon_news', ?, datetime('now'), ?)",
                (f"raw-f{i}", ticker, ref_date, b"{}"),
            )

        articles_data = [
            ("poly:f1", "raw-f0", "AAPL", "2026-05-01", "Fresh article", "Body."),
            ("poly:f2", "raw-f1", "TSLA", "2026-04-30", "Stale article", "Body."),
        ]
        for art_id, raw_id, ticker, ref_date, title, desc in articles_data:
            upsert_article(conn, article={
                "article_id": art_id,
                "raw_asset_id": raw_id,
                "provider": "polygon",
                "source_type": "polygon_news",
                "ticker": ticker,
                "reference_date": ref_date,
                "published_utc": f"{ref_date}T12:00:00Z",
                "title": title,
                "description": desc,
                "publisher_name": "Test Publisher",
                "source_tier": 5,
            })
            upsert_article_ticker(conn, article_id=art_id, ticker=ticker,
                                  raw_asset_id=raw_id, reference_date=ref_date)

    conn.commit()
    return conn


class TestLatestOHLCVDate:
    def test_returns_max_date(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        assert latest_local_ohlcv_date(conn) == "2026-05-01"
        conn.close()

    def test_raises_on_empty(self, tmp_path):
        db_path = str(tmp_path / "empty.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.commit()
        with pytest.raises(RuntimeError, match="ohlcv.*empty"):
            latest_local_ohlcv_date(conn)
        conn.close()


class TestNewsFreshness:
    def test_fresh_and_stale(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        result = news_freshness(conn)
        conn.close()

        pn = result["polygon_news"]
        assert pn["AAPL"]["status"] == "FRESH"
        assert pn["AAPL"]["latest_date"] == "2026-05-01"
        assert pn["TSLA"]["status"] == "STALE"
        assert pn["TSLA"]["days_behind"] == 1

    def test_no_data_ticker(self, tmp_path):
        """Tickers in ohlcv but not in articles get NO_DATA."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path, with_articles=False)
        result = news_freshness(conn)
        conn.close()

        # polygon_news may not exist as a key if no articles at all
        pn = result.get("polygon_news", {})
        assert pn.get("AAPL", {}).get("status") == "NO_DATA"
        assert pn.get("TSLA", {}).get("status") == "NO_DATA"

    def test_accepts_watermark_kwarg(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        result = news_freshness(conn, watermark="2026-04-30")
        conn.close()

        pn = result["polygon_news"]
        # AAPL ref=2026-05-01 > watermark=2026-04-30 → AHEAD
        assert pn["AAPL"]["status"] == "AHEAD"
        # TSLA ref=2026-04-30 == watermark → FRESH
        assert pn["TSLA"]["status"] == "FRESH"

    def test_blank_reference_date_is_no_data_not_parsed(self, tmp_path):
        db_path = str(tmp_path / "blank_ref.db")
        conn = _make_db(db_path, with_articles=False)
        conn.execute(
            "INSERT OR REPLACE INTO raw_assets "
            "(asset_id, ticker, source_type, reference_date, fetched_at, content_raw) "
            "VALUES ('raw-blank', 'AAPL', 'polygon_news', '2026-05-01', datetime('now'), ?)",
            (b"{}",),
        )
        upsert_article(conn, article={
            "article_id": "poly:blank",
            "raw_asset_id": "raw-blank",
            "provider": "polygon",
            "source_type": "polygon_news",
            "ticker": "AAPL",
            "reference_date": "   ",
            "published_utc": "2026-05-01T12:00:00Z",
            "title": "Blank reference article",
            "description": "Body.",
            "publisher_name": "Test Publisher",
            "source_tier": 5,
        })
        conn.execute(
            "INSERT OR REPLACE INTO article_tickers "
            "(article_id, ticker, raw_asset_id, reference_date) "
            "VALUES ('poly:blank', 'AAPL', 'raw-blank', '   ')"
        )
        conn.commit()

        result = news_freshness(conn, watermark="2026-05-01")
        conn.close()

        assert result["polygon_news"]["AAPL"] == {
            "latest_date": None,
            "status": "NO_DATA",
            "days_behind": -1,
        }

    def test_null_reference_date_group_is_no_data(self, tmp_path):
        db_path = str(tmp_path / "null_ref.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, close REAL)")
        conn.execute(
            "CREATE TABLE articles (article_id TEXT PRIMARY KEY, source_type TEXT)"
        )
        conn.execute(
            "CREATE TABLE article_tickers "
            "(article_id TEXT, ticker TEXT, reference_date TEXT)"
        )
        conn.execute(
            "INSERT INTO ohlcv (symbol, date, close) VALUES ('AAPL', '2026-05-01', 100.0)"
        )
        conn.execute(
            "INSERT INTO articles (article_id, source_type) VALUES ('poly:null', 'polygon_news')"
        )
        conn.execute(
            "INSERT INTO article_tickers (article_id, ticker, reference_date) "
            "VALUES ('poly:null', 'AAPL', NULL)"
        )
        conn.commit()

        result = news_freshness(conn, watermark="2026-05-01")
        conn.close()

        assert result["polygon_news"]["AAPL"]["status"] == "NO_DATA"
        assert result["polygon_news"]["AAPL"]["latest_date"] is None
        assert result["polygon_news"]["AAPL"]["days_behind"] == -1


class TestIndexFreshness:
    def test_no_index_when_no_live_manifest(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        result = index_freshness(conn)
        conn.close()

        assert result["status"] == "NO_INDEX"
        assert result["stale_count"] > 0

    def test_ignores_dry_run_manifest(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        # Insert a dry_run manifest — should be ignored
        conn.execute("""
            INSERT INTO index_manifests
            (build_id, created_at, model, model_hash, lancedb_path,
             l1_count, l2_count, article_count, indexed_through_date,
             corpus_hash, status)
            VALUES (?, datetime('now'), 'bge-m3', 'abc123', '/tmp/test',
                    11772, 0, 11772, '2026-05-01', 'x', 'dry_run')
        """, ("build-dry-1",))
        conn.commit()

        result = index_freshness(conn)
        conn.close()

        assert result["status"] == "NO_INDEX"

    def test_stale_when_articles_not_in_index_state(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        # Insert a live manifest
        conn.execute("""
            INSERT INTO index_manifests
            (build_id, created_at, model, model_hash, lancedb_path,
             l1_count, l2_count, article_count, indexed_through_date,
             corpus_hash, status)
            VALUES (?, datetime('now'), 'bge-m3', 'abc123', '/tmp/test',
                    2, 0, 2, '2026-05-01', 'x', 'live')
        """, ("build-live-1",))
        conn.commit()

        result = index_freshness(conn)
        conn.close()

        assert result["status"] == "STALE"
        assert result["stale_count"] > 0


    def test_stale_when_articles_have_old_build_rows(self, tmp_path):
        """Articles with index_state rows from an OLD build are STALE.
        
        Bug regression: the old LEFT JOIN didn't filter on indexed_build_id,
        so rows from a prior build masked as 'present' and articles appeared
        falsely FRESH when only the old build rows existed.
        """
        import json
        from catalyst_data.index_builder import compute_content_hash
        
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        # Get actual article data for hash computation
        articles = conn.execute(
            "SELECT article_id, title, description FROM articles"
        ).fetchall()

        # Insert an OLD live manifest and index_state for all articles
        old_build_id = "build-old"
        conn.execute("""
            INSERT INTO index_manifests
            (build_id, created_at, model, model_hash, lancedb_path,
             l1_count, l2_count, article_count, indexed_through_date,
             corpus_hash, status)
            VALUES (?, datetime('now', '-2 days'), 'bge-m3', 'oldhash', '/tmp/test',
                    2, 0, 2, '2026-04-30', 'x', 'live')
        """, (old_build_id,))

        for art_id, title, desc in articles:
            h = compute_content_hash(title, desc)
            conn.execute("""
                INSERT INTO index_state
                (chunk_id, chunk_level, corpus_item_id, source_kind, content_hash, content_text, source_tier,
                 dedup_group_id, indexed_build_id, indexed_at)
                VALUES (? || '::l1', 'l1', ?, 'article', ?, 'text', 5, NULL, ?, datetime('now'))
            """, (art_id, art_id, h, old_build_id))

        # Now insert a NEW live manifest (build-live-new) — but do NOT
        # re-index the articles. They still only have old-build rows.
        new_build_id = "build-live-new"
        conn.execute("""
            INSERT INTO index_manifests
            (build_id, created_at, model, model_hash, lancedb_path,
             l1_count, l2_count, article_count, indexed_through_date,
             corpus_hash, status)
            VALUES (?, datetime('now'), 'bge-m3', 'newhash', '/tmp/test',
                    2, 0, 2, '2026-05-01', 'y', 'live')
        """, (new_build_id,))
        conn.commit()

        result = index_freshness(conn)
        conn.close()

        # All articles should be STALE — they have no rows with build-live-new
        assert result["status"] == "STALE"
        assert result["stale_count"] == len(articles)


class TestFreshnessReport:
    def test_is_read_only(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        # Capture row counts before
        before_articles = conn.execute(
            "SELECT COUNT(*) FROM articles"
        ).fetchone()[0]
        before_at = conn.execute(
            "SELECT COUNT(*) FROM article_tickers"
        ).fetchone()[0]

        report = freshness_report(conn)

        # Verify counts unchanged
        after_articles = conn.execute(
            "SELECT COUNT(*) FROM articles"
        ).fetchone()[0]
        after_at = conn.execute(
            "SELECT COUNT(*) FROM article_tickers"
        ).fetchone()[0]
        conn.close()

        assert after_articles == before_articles
        assert after_at == before_at
        assert "local_ohlcv_date" in report
        assert "news" in report
        assert "index" in report
        assert "generated_at" in report


# ============================================================================
# SEC filings freshness tests (Step 3C2)
# ============================================================================

import pytest
from catalyst_data.freshness import filings_freshness, freshness_report
from catalyst_data.storage.sqlite import init_db, ensure_filings_tables
from catalyst_data.quality import ensure_ingestion_quality_tables


def _make_filings_db(db_path: str) -> sqlite3.Connection:
    """Create a minimal DB with ohlcv + filings tables."""
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)
    ensure_filings_tables(conn)

    for dt in ("2026-06-30", "2026-06-29"):
        for sym in ("AAPL", "TSLA"):
            conn.execute(
                "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES (?, ?, 100.0)",
                (sym, dt),
            )

    conn.commit()
    return conn


class TestFilingsFreshness:
    """filings_freshness() tests."""

    def test_empty_filings_table(self, tmp_path):
        """Empty filings → all tickers never_checked, 0 counts."""
        db_path = str(tmp_path / "test.db")
        conn = _make_filings_db(db_path)

        result = filings_freshness(conn, watermark="2026-06-30")
        pt = result["per_ticker"]
        ov = result["overall"]

        assert ov["total_filings"] == 0
        assert ov["checked_tickers"] == 0
        assert ov["never_checked_tickers"] == 2
        for ticker in ("AAPL", "TSLA"):
            assert pt[ticker]["latest_filing_date"] is None
            assert pt[ticker]["filings_30d_count"] == 0
            assert pt[ticker]["status"] == "never_checked"
        conn.close()

    def test_with_filing_and_checkpoint(self, tmp_path):
        """One filing + checkpoint → status=current, data correct."""
        db_path = str(tmp_path / "test.db")
        conn = _make_filings_db(db_path)

        # Write a filing for AAPL
        conn.execute("""
            INSERT OR REPLACE INTO filings
                (filing_id, cik, ticker, form_type, filed_at,
                 accession_number, url, is_rag_eligible)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("sec:0000320193:test-001", "0000320193", "AAPL",
              "8-K", "2026-06-30", "test-001",
              "https://www.sec.gov/test", 1))

        # Write a success checkpoint
        conn.execute("""
            INSERT OR REPLACE INTO source_checkpoints
                (run_id, source_type, ticker, date, status)
            VALUES ('run_test', 'sec_filings', 'AAPL', '2026-06-30', 'success')
        """)
        conn.commit()

        result = filings_freshness(conn, watermark="2026-06-30")
        pt = result["per_ticker"]

        aapl = pt["AAPL"]
        assert aapl["latest_filing_date"] == "2026-06-30"
        assert aapl["filings_30d_count"] == 1
        assert aapl["status"] == "current"
        assert aapl["latest_checked_date"] == "2026-06-30"

        # TSLA: never checked
        tsla = pt["TSLA"]
        assert tsla["latest_filing_date"] is None
        assert tsla["status"] == "never_checked"
        conn.close()

    def test_stale_check_when_checked_before_watermark(self, tmp_path):
        """Checkpoint older than watermark → status=stale_check."""
        db_path = str(tmp_path / "test.db")
        conn = _make_filings_db(db_path)

        # Filing dated 2026-06-29 but checkpoint is from an older date we checked
        conn.execute("""
            INSERT OR REPLACE INTO filings
                (filing_id, cik, ticker, form_type, filed_at,
                 accession_number, url, is_rag_eligible)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("sec:0000320193:test-002", "0000320193", "AAPL",
              "10-Q", "2026-06-29", "test-002",
              "https://www.sec.gov/test", 1))

        # Checkpoint is 3 days before watermark
        conn.execute("""
            INSERT OR REPLACE INTO source_checkpoints
                (run_id, source_type, ticker, date, status)
            VALUES ('run_test', 'sec_filings', 'AAPL', '2026-06-27', 'success')
        """)
        conn.commit()

        result = filings_freshness(conn, watermark="2026-06-30")
        pt = result["per_ticker"]

        aapl = pt["AAPL"]
        assert aapl["latest_filing_date"] == "2026-06-29"
        assert aapl["latest_checked_date"] == "2026-06-27"
        assert aapl["status"] == "stale_check"
        conn.close()

    def test_no_filings_table_returns_empty(self, tmp_path):
        """DB without filings table → empty per_ticker (no ohlcv data needed)."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        # Do NOT call init_db — it auto-creates filings table
        # No ohlcv needed — the early return path short-circuits before ohlcv access

        result = filings_freshness(conn, watermark="2026-06-30")
        assert result["per_ticker"] == {}
        assert result["overall"]["total_filings"] == 0
        assert result["overall"]["checked_tickers"] == 0
        conn.close()

    def test_no_source_checkpoints_table(self, tmp_path):
        """DB without source_checkpoints → still returns filing data, checked_map empty."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_filings_tables(conn)

        conn.execute(
            "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES ('AAPL', '2026-06-30', 100.0)"
        )
        conn.execute("""
            INSERT OR REPLACE INTO filings
                (filing_id, cik, ticker, form_type, filed_at,
                 accession_number, url, is_rag_eligible)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("sec:test:003", "0000320193", "AAPL",
              "8-K", "2026-06-30", "test-003",
              "https://www.sec.gov/test", 1))
        conn.commit()

        # No source_checkpoints table exists
        result = filings_freshness(conn, watermark="2026-06-30")
        pt = result["per_ticker"]

        aapl = pt["AAPL"]
        assert aapl["latest_filing_date"] == "2026-06-30"
        assert aapl["latest_checked_date"] is None
        assert aapl["status"] == "never_checked"
        conn.close()


class TestFreshnessReportSEC:
    """freshness_report includes sec_filings section."""

    def test_report_includes_sec_section(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_filings_db(db_path)
        conn.close()

        report = freshness_report(sqlite3.connect(db_path))
        assert "sec_filings" in report
        sec = report["sec_filings"]
        assert "per_ticker" in sec
        assert "overall" in sec

    def test_report_sec_read_only(self, tmp_path):
        """freshness_report with sec_filings is read-only — no writes."""
        db_path = str(tmp_path / "test.db")
        conn = _make_filings_db(db_path)
        conn.close()

        # Snapshot table counts
        conn = sqlite3.connect(db_path)
        before = {}
        for t in ("filings", "filing_documents", "raw_assets",
                  "source_checkpoints", "index_state", "article_tickers"):
            try:
                before[t] = conn.execute(
                    f"SELECT COUNT(*) FROM {t}"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                before[t] = 0
        conn.close()

        report = freshness_report(sqlite3.connect(db_path))
        assert "sec_filings" in report

        conn = sqlite3.connect(db_path)
        after = {}
        for t in before:
            try:
                after[t] = conn.execute(
                    f"SELECT COUNT(*) FROM {t}"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                after[t] = 0
        conn.close()

        for t in before:
            assert after[t] == before[t], f"{t} changed during freshness_report"

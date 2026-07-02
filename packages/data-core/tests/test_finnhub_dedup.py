"""Tests for cross-source dedup (Polygon + Finnhub) — mock-only, zero network."""
from __future__ import annotations

import sqlite3

import pytest

from catalyst_data.articles import ensure_articles_table, upsert_article, upsert_article_ticker, compute_article_id
from catalyst_data.dedup.cross_source import compute_cross_source_dedup
from catalyst_data.storage.sqlite import init_db


def _make_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_articles_table(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def _insert_article(conn, article_id, provider, source_type, ticker, title,
                    published_utc, reference_date, description="", article_url=""):
    upsert_article(conn, article={
        "article_id": article_id,
        "raw_asset_id": f"raw_{article_id}",
        "provider": provider,
        "source_type": source_type,
        "ticker": ticker,
        "reference_date": reference_date,
        "published_utc": published_utc,
        "title": title,
        "description": description,
        "article_url": article_url,
        "image_url": None,
        "author": None,
        "publisher_name": "TestPublisher",
        "publisher_homepage_url": None,
        "publisher_logo_url": None,
        "publisher_favicon_url": None,
        "keywords_json": "[]",
        "insights_json": "[]",
        "tickers_json": f'["{ticker}"]',
        "source_tier": 4,
        "dedup_group_id": None,
        "is_canonical": 1,
        "is_rag_eligible": 1,
        "quality_score": 1.0,
    })
    upsert_article_ticker(
        conn, article_id=article_id, ticker=ticker,
        raw_asset_id=f"raw_{article_id}", reference_date=reference_date,
    )


class TestSameEventSharedDedupGroup:
    def test_same_event_shared_dedup_group(self, tmp_path):
        """Polygon + Finnhub article with same title+ticker+date → same dedup_group_id."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        _insert_article(conn, "poly:1", "polygon", "polygon_news", "AAPL",
                        "Apple Reports Q2 Earnings", "2025-07-01T14:00:00+00:00", "2025-07-01",
                        description="Apple Q2 earnings report.")
        _insert_article(conn, "finnhub:2", "finnhub", "finnhub_company_news", "AAPL",
                        "Apple Reports Q2 Earnings", "2025-07-01T15:00:00+00:00", "2025-07-01",
                        description="Apple earnings summary.")

        groups = compute_cross_source_dedup(conn)
        assert groups >= 1

        # Both articles should now have dedup_group_id set
        poly_row = conn.execute(
            "SELECT dedup_group_id, is_canonical FROM articles WHERE article_id='poly:1'"
        ).fetchone()
        finn_row = conn.execute(
            "SELECT dedup_group_id, is_canonical FROM articles WHERE article_id='finnhub:2'"
        ).fetchone()

        assert poly_row[0] is not None
        assert finn_row[0] is not None

        conn.close()

    def test_distinct_stories_not_merged(self, tmp_path):
        """Different titles → different dedup_group_ids."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        _insert_article(conn, "poly:3", "polygon", "polygon_news", "AAPL",
                        "Apple Earnings Beat", "2025-07-01T14:00:00+00:00", "2025-07-01")
        _insert_article(conn, "finnhub:4", "finnhub", "finnhub_company_news", "AAPL",
                        "Apple Announces Dividend", "2025-07-01T15:00:00+00:00", "2025-07-01")

        compute_cross_source_dedup(conn)

        g1 = conn.execute(
            "SELECT dedup_group_id FROM article_tickers WHERE article_id='poly:3'"
        ).fetchone()
        g2 = conn.execute(
            "SELECT dedup_group_id FROM article_tickers WHERE article_id='finnhub:4'"
        ).fetchone()

        # Different accessions → different groups
        assert g1[0] != g2[0]
        conn.close()

    def test_conservative_no_false_merge(self, tmp_path):
        """Subtly different titles → NOT merged (conservative)."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        _insert_article(conn, "poly:5", "polygon", "polygon_news", "AAPL",
                        "Apple Reports Q2 Earnings", "2025-07-01T14:00:00+00:00", "2025-07-01")
        _insert_article(conn, "finnhub:6", "finnhub", "finnhub_company_news", "AAPL",
                        "Apple Q2 Earnings Report", "2025-07-01T15:00:00+00:00", "2025-07-01")

        compute_cross_source_dedup(conn)

        g1 = conn.execute(
            "SELECT dedup_group_id FROM article_tickers WHERE article_id='poly:5'"
        ).fetchone()
        g2 = conn.execute(
            "SELECT dedup_group_id FROM article_tickers WHERE article_id='finnhub:6'"
        ).fetchone()

        assert g1[0] != g2[0], "Conservative dedup should NOT merge subtly different titles"
        conn.close()


class TestCanonicalSelection:
    def test_canonical_selection_polygon_wins(self, tmp_path):
        """Polygon + Finnhub in same group → Polygon is canonical."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        _insert_article(conn, "poly:7", "polygon", "polygon_news", "AAPL",
                        "Earnings Report", "2025-07-01T14:00:00+00:00", "2025-07-01",
                        description="Detailed earnings analysis with lots of data and metrics.")
        _insert_article(conn, "finnhub:8", "finnhub", "finnhub_company_news", "AAPL",
                        "Earnings Report", "2025-07-01T15:00:00+00:00", "2025-07-01",
                        description="Short summary.")

        compute_cross_source_dedup(conn)

        poly_canon = conn.execute(
            "SELECT is_canonical FROM articles WHERE article_id='poly:7'"
        ).fetchone()[0]
        finn_canon = conn.execute(
            "SELECT is_canonical FROM articles WHERE article_id='finnhub:8'"
        ).fetchone()[0]

        assert poly_canon == 1, "Polygon should be canonical (higher CROSS_SOURCE_PRIORITY)"
        assert finn_canon == 0, "Finnhub should NOT be canonical"
        conn.close()


class TestIdempotentRerun:
    def test_idempotent_rerun_one_canonical(self, tmp_path):
        """D1: After first dedup, adding a Finnhub article to existing group → one canonical."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        # First run: two articles
        _insert_article(conn, "poly:9", "polygon", "polygon_news", "AAPL",
                        "Apple Q3 Update", "2025-07-01T14:00:00+00:00", "2025-07-01",
                        description="Polygon article about Apple Q3.")
        _insert_article(conn, "finnhub:10", "finnhub", "finnhub_company_news", "AAPL",
                        "Apple Q3 Update", "2025-07-01T15:00:00+00:00", "2025-07-01",
                        description="Finnhub article about Apple Q3.")

        groups1 = compute_cross_source_dedup(conn)
        assert groups1 >= 1

        # Verify exactly one canonical after first run
        canon_count = conn.execute(
            "SELECT COUNT(*) FROM articles a JOIN article_tickers at ON at.article_id = a.article_id "
            "WHERE at.dedup_group_id = (SELECT dedup_group_id FROM article_tickers WHERE article_id='poly:9' LIMIT 1) "
            "AND a.is_canonical = 1"
        ).fetchone()[0]
        assert canon_count == 1

        # Second run: add a new Finnhub article to the SAME group
        _insert_article(conn, "finnhub:11", "finnhub", "finnhub_company_news", "AAPL",
                        "Apple Q3 Update", "2025-07-01T16:00:00+00:00", "2025-07-01",
                        description="Another Finnhub article about Apple Q3.")

        groups2 = compute_cross_source_dedup(conn)
        # Group now has 3 members → should be resolved
        canon_count2 = conn.execute(
            "SELECT COUNT(*) FROM articles a JOIN article_tickers at ON at.article_id = a.article_id "
            "WHERE at.dedup_group_id = (SELECT dedup_group_id FROM article_tickers WHERE article_id='poly:9' LIMIT 1) "
            "AND a.is_canonical = 1"
        ).fetchone()[0]
        assert canon_count2 == 1, "Still exactly ONE canonical after adding a third member"

        # Verify group has 3 members
        member_count = conn.execute(
            "SELECT COUNT(DISTINCT at.article_id) FROM article_tickers at "
            "WHERE at.dedup_group_id = (SELECT dedup_group_id FROM article_tickers WHERE article_id='poly:9' LIMIT 1)"
        ).fetchone()[0]
        assert member_count == 3
        conn.close()


class TestMultiTickerPolygon:
    def test_multi_ticker_polygon_matches_finnhub(self, tmp_path):
        """D2/M4: Multi-ticker Polygon [MSFT,AAPL] vs Finnhub AAPL → shared group via AAPL."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        # Multi-ticker Polygon article (MSFT, AAPL)
        poly_id = "poly:multi"
        upsert_article(conn, article={
            "article_id": poly_id,
            "raw_asset_id": "raw_multi",
            "provider": "polygon",
            "source_type": "polygon_news",
            "ticker": "AAPL",  # first ticker
            "reference_date": "2025-07-01",
            "published_utc": "2025-07-01T14:00:00+00:00",
            "title": "Earnings",
            "description": "Tech earnings roundup.",
            "article_url": "",
            "image_url": None,
            "author": None,
            "publisher_name": "TestPublisher",
            "publisher_homepage_url": None,
            "publisher_logo_url": None,
            "publisher_favicon_url": None,
            "keywords_json": "[]",
            "insights_json": "[]",
            "tickers_json": '["MSFT","AAPL"]',
            "source_tier": 4,
            "dedup_group_id": None,
            "is_canonical": 1,
            "is_rag_eligible": 1,
            "quality_score": 1.0,
        })
        upsert_article_ticker(conn, article_id=poly_id, ticker="MSFT",
                              raw_asset_id="raw_multi", reference_date="2025-07-01")
        upsert_article_ticker(conn, article_id=poly_id, ticker="AAPL",
                              raw_asset_id="raw_multi", reference_date="2025-07-01")

        # Single-ticker Finnhub article (AAPL only)
        _insert_article(conn, "finnhub:aapl", "finnhub", "finnhub_company_news", "AAPL",
                        "Earnings", "2025-07-01T15:00:00+00:00", "2025-07-01",
                        description="Earnings summary from Finnhub.")

        groups = compute_cross_source_dedup(conn)
        assert groups >= 1

        # The AAPL association should share the same dedup_group_id
        aapl_groups = conn.execute("""
            SELECT DISTINCT dedup_group_id FROM article_tickers
            WHERE article_id IN ('poly:multi', 'finnhub:aapl') AND ticker = 'AAPL'
        """).fetchall()

        # Both should share the same dedup_group_id via the AAPL association
        # DISTINCT collapses same values → expect exactly 1 unique group_id
        assert len(aapl_groups) == 1, (
            f"Expected 1 shared dedup_group_id for AAPL association, got {len(aapl_groups)}"
        )
        assert aapl_groups[0][0] is not None, "dedup_group_id should be non-NULL"
        
        # Also verify both articles exist in article_tickers with that group
        at_count = conn.execute("""
            SELECT COUNT(*) FROM article_tickers
            WHERE dedup_group_id = ? AND article_id IN ('poly:multi', 'finnhub:aapl')
        """, (aapl_groups[0][0],)).fetchone()[0]
        assert at_count == 2, (
            f"Both articles should be in the shared dedup group, got {at_count} rows"
        )

        # Verify exactly one canonical in the group
        group_id = aapl_groups[0][0]
        canon_count = conn.execute("""
            SELECT COUNT(*) FROM articles a
            JOIN article_tickers at ON at.article_id = a.article_id
            WHERE at.dedup_group_id = ? AND a.is_canonical = 1
        """, (group_id,)).fetchone()[0]
        assert canon_count == 1
        conn.close()

    def test_cross_group_no_zero_canonical(self, tmp_path):
        """OR semantics: multi-group article winning one group stays canonical.

        Setup:
          P1 has tickers [AAPL, MSFT], title "X", date 2025-07-01
          F1 is Finnhub AAPL, title "X", date 2025-07-01  -> group-AAPL: {P1, F1}
          P2 is Polygon MSFT, title "X", date 2025-07-01  -> group-MSFT: {P1, P2}

        After OR semantics: P1 (won group-AAPL) AND P2 (won group-MSFT) are BOTH
        canonical=1. Every group has >=1 canonical. No group with zero canonical.
        """
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        # P1: multi-ticker Polygon article [AAPL, MSFT]
        p1_id = "poly:p1_multi"
        upsert_article(conn, article={
            "article_id": p1_id,
            "raw_asset_id": "raw_p1",
            "provider": "polygon",
            "source_type": "polygon_news",
            "ticker": "AAPL",
            "reference_date": "2025-07-01",
            "published_utc": "2025-07-01T14:00:00+00:00",
            "title": "X",
            "description": "Polygon multi-ticker article body text.",
            "article_url": "",
            "image_url": None,
            "author": None,
            "publisher_name": "TestPublisher",
            "publisher_homepage_url": None,
            "publisher_logo_url": None,
            "publisher_favicon_url": None,
            "keywords_json": "[]",
            "insights_json": "[]",
            "tickers_json": '["AAPL","MSFT"]',
            "source_tier": 4,
            "dedup_group_id": None,
            "is_canonical": 1,
            "is_rag_eligible": 1,
            "quality_score": 1.0,
        })
        upsert_article_ticker(conn, article_id=p1_id, ticker="AAPL",
                              raw_asset_id="raw_p1", reference_date="2025-07-01")
        upsert_article_ticker(conn, article_id=p1_id, ticker="MSFT",
                              raw_asset_id="raw_p1", reference_date="2025-07-01")

        # F1: Finnhub AAPL
        _insert_article(conn, "finnhub:f1", "finnhub", "finnhub_company_news",
                        "AAPL", "X", "2025-07-01T15:00:00+00:00", "2025-07-01",
                        description="Finnhub short summary.")

        # P2: single-ticker Polygon MSFT
        _insert_article(conn, "poly:p2", "polygon", "polygon_news",
                        "MSFT", "X", "2025-07-01T14:30:00+00:00", "2025-07-01",
                        description="Polygon single-ticker article.")

        groups = compute_cross_source_dedup(conn)

        # Verify dedup group assignments
        aapl_fp = conn.execute(
            "SELECT dedup_group_id FROM article_tickers "
            "WHERE article_id=? AND ticker='AAPL'",
            (p1_id,)
        ).fetchone()
        msft_fp = conn.execute(
            "SELECT dedup_group_id FROM article_tickers "
            "WHERE article_id=? AND ticker='MSFT'",
            (p1_id,)
        ).fetchone()
        assert aapl_fp[0] is not None
        assert msft_fp[0] is not None
        assert aapl_fp[0] != msft_fp[0]

        # F1 should share group-AAPL
        f1_fp = conn.execute(
            "SELECT dedup_group_id FROM article_tickers WHERE article_id='finnhub:f1'"
        ).fetchone()
        assert f1_fp[0] == aapl_fp[0]

        # P2 should share group-MSFT
        p2_fp = conn.execute(
            "SELECT dedup_group_id FROM article_tickers WHERE article_id='poly:p2'"
        ).fetchone()
        assert p2_fp[0] == msft_fp[0]

        # Group-AAPL: exactly one canonical
        aapl_canon = conn.execute(
            "SELECT a.article_id FROM articles a "
            "JOIN article_tickers at ON at.article_id = a.article_id "
            "WHERE at.dedup_group_id = ? AND a.is_canonical = 1",
            (aapl_fp[0],)
        ).fetchall()
        assert len(aapl_canon) == 1

        # Group-MSFT: exactly one canonical
        msft_canon = conn.execute(
            "SELECT a.article_id FROM articles a "
            "JOIN article_tickers at ON at.article_id = a.article_id "
            "WHERE at.dedup_group_id = ? AND a.is_canonical = 1",
            (msft_fp[0],)
        ).fetchall()
        assert len(msft_canon) == 1

        # P1 (won group-AAPL) must be canonical
        p1_canon = conn.execute(
            "SELECT is_canonical FROM articles WHERE article_id=?", (p1_id,)
        ).fetchone()[0]
        assert p1_canon == 1

        # P2: loses group-MSFT to P1 (earlier pub time + longer body)
        # but BOTH groups still have exactly one canonical (P1)
        p2_canon = conn.execute(
            "SELECT is_canonical FROM articles WHERE article_id='poly:p2'"
        ).fetchone()[0]
        assert p2_canon == 0, (
            "P2 lost group-MSFT to P1 (earlier pub_utc, longer body) — "
            "no group has zero canonical"
        )

        # P1 is canonical for BOTH groups
        assert p1_canon == 1
        # F1 lost group-AAPL
        f1_canon = conn.execute(
            "SELECT is_canonical FROM articles WHERE article_id='finnhub:f1'"
        ).fetchone()[0]
        assert f1_canon == 0

        conn.close()

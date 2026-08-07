"""S3 Phase 1: corpus_items VIEW — byte-identity, grain, and fallback tests.

Design refs: §0.1, D1.
"""
from __future__ import annotations

import sqlite3
import hashlib
from pathlib import Path

import pytest

DEV_DB = Path(__file__).resolve().parents[3] / "data" / "catalyst_dev_ws4b.db"

# ── helpers ──────────────────────────────────────────────────────────────

def _db_module():
    from catalyst_data.storage import sqlite as mod
    return mod

def _open_dev():
    """Open dev DB read-only."""
    conn = sqlite3.connect(f"file:{DEV_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn

def _corpus_items_row_count(conn):
    return conn.execute("SELECT COUNT(*) FROM corpus_items").fetchone()[0]

# ── tests ────────────────────────────────────────────────────────────────

class TestCorpusItemsViewExists:
    """The VIEW must exist in a dev DB after init_db."""

    def test_view_exists_in_memory_after_init(self):
        conn = sqlite3.connect(":memory:")
        _db_module().init_db(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        )}
        assert "corpus_items" in tables, "corpus_items VIEW not created by init_db"

    def test_view_queryable(self):
        conn = sqlite3.connect(":memory:")
        _db_module().init_db(conn)
        # Query the view — should return zero rows on empty DB (not error)
        rows = conn.execute("SELECT * FROM corpus_items LIMIT 1").fetchall()
        assert rows == []


@pytest.mark.protected_artifact("data/catalyst_dev_ws4b.db")
class TestCorpusItemsGrain:
    """§0.1 grain: one row per (article_id × ticker) via INNER JOIN article_tickers."""

    def test_source_kind_enumeration(self):
        conn = _open_dev()
        kinds = {r["source_kind"] for r in conn.execute(
            "SELECT DISTINCT source_kind FROM corpus_items"
        )}
        assert kinds <= {"article", "filing"}, f"Unexpected source_kind: {kinds}"
        conn.close()

    def test_no_null_ticker_rows(self):
        conn = _open_dev()
        null_count = conn.execute(
            "SELECT COUNT(*) FROM corpus_items WHERE ticker IS NULL"
        ).fetchone()[0]
        assert null_count == 0, "corpus_items must have zero NULL-ticker rows (INNER JOIN)"
        conn.close()

    def test_multi_ticker_article(self):
        """Finnhub article 139126668 has 2 tickers."""
        conn = _open_dev()
        rows = conn.execute(
            "SELECT ticker FROM corpus_items WHERE corpus_item_id = 'finnhub:139126668'"
        ).fetchall()
        tickers = {r["ticker"] for r in rows}
        assert len(tickers) >= 2, f"Expected ≥2 tickers for finnhub:139126668, got {tickers}"
        conn.close()

    def test_is_rag_eligible_present(self):
        conn = _open_dev()
        row = conn.execute(
            "SELECT is_rag_eligible FROM corpus_items LIMIT 1"
        ).fetchone()
        assert row is not None
        assert "is_rag_eligible" in row.keys()
        conn.close()


@pytest.mark.protected_artifact("data/catalyst_dev_ws4b.db")
class TestArticleContentMDByteIdentity:
    """Article content_md must match title || char(10) || COALESCE(description, '').

    Compares corpus_items VIEW output directly against the computed formula
    from the articles table, NOT against stale index_state data.
    """

    def test_article_content_md_matches_formula(self):
        """Every article row in corpus_items must equal computed formula."""
        conn = _open_dev()
        mismatches = conn.execute(
            """SELECT ci.corpus_item_id,
                      ci.content_md,
                      a.title || char(10) || COALESCE(a.description, '') AS expected_md
               FROM corpus_items ci
               JOIN articles a ON ci.corpus_item_id = a.article_id
               WHERE ci.source_kind = 'article'
                 AND ci.chunk_level = 'l1'
                 AND ci.content_md IS NOT a.title || char(10) || COALESCE(a.description, '')
               LIMIT 20"""
        ).fetchall()
        conn.close()
        assert not mismatches, (
            f"content_md formula mismatch: {len(mismatches)} rows. "
            f"First: {mismatches[0] if mismatches else 'N/A'}"
        )


@pytest.mark.protected_artifact("data/catalyst_dev_ws4b.db")
class TestFilingContentMDByteIdentity:
    """Filing content_md must be byte-identical to index_state.content_text for L1."""

    def test_filing_l1_byte_identity(self):
        conn = _open_dev()
        is_rows = conn.execute(
            "SELECT corpus_item_id, content_text FROM index_state "
            "WHERE source_kind='filing' AND chunk_level='l1' LIMIT 20"
        ).fetchall()

        if not is_rows:
            pytest.skip("No filing L1 rows in index_state")

        mismatches = []
        for is_row in is_rows:
            ci_rows = conn.execute(
                "SELECT content_md FROM corpus_items WHERE corpus_item_id = ? "
                "AND source_kind = 'filing' AND chunk_level = 'l1'",
                (is_row["corpus_item_id"],),
            ).fetchall()
            if not ci_rows:
                mismatches.append((is_row["corpus_item_id"], "MISSING_FROM_CORPUS_ITEMS"))
                continue
            for ci_row in ci_rows:
                if ci_row["content_md"] != is_row["content_text"]:
                    mismatches.append((
                        is_row["corpus_item_id"],
                        f"EXPECTED={is_row['content_text'][:80]!r}\nGOT={ci_row['content_md'][:80]!r}"
                    ))
        conn.close()
        assert not mismatches, f"Filing byte-identity failures: {mismatches}"


@pytest.mark.protected_artifact("data/catalyst_dev_ws4b.db")
class TestCorpusItemsChunkLevel:
    """corpus_items includes l1 and l2 records."""

    def test_both_levels_present(self):
        conn = _open_dev()
        levels = {r["chunk_level"] for r in conn.execute(
            "SELECT DISTINCT chunk_level FROM corpus_items"
        )}
        assert "l1" in levels, "corpus_items missing l1 rows"
        conn.close()


class TestArticleContentMDEmptyDescription:
    """Article content_md must include trailing newline even for empty/NULL descriptions."""

    def test_empty_description_has_trailing_newline(self, tmp_path):
        """Prove content_md = title || char(10) || COALESCE(description, '')."""
        import sqlite3 as _sqlite3
        from catalyst_data.storage.sqlite import init_db, _CORPUS_ITEMS_VIEW

        db_path = str(tmp_path / "test_content_md.db")
        conn = _sqlite3.connect(db_path)
        init_db(conn)
        conn.row_factory = _sqlite3.Row
        conn.execute("PRAGMA foreign_keys=OFF")

        # Insert a fixture article with NULL description
        conn.execute("""
            INSERT INTO articles (article_id, provider, source_type, ticker,
                reference_date, published_utc, title, description,
                article_url, source_tier, is_rag_eligible, raw_asset_id)
            VALUES ('test:null_desc', 'finnhub', 'finnhub_company_news', 'AAPL',
                '2025-01-15', '2025-01-15T10:00:00Z', 'Test Title With Null Desc', NULL,
                'https://example.com/1', 2, 1, 'raw:null_desc')
        """)
        conn.execute("""
            INSERT INTO article_tickers (article_id, ticker, raw_asset_id, reference_date)
            VALUES ('test:null_desc', 'AAPL', 'raw:null_desc', '2025-01-15')
        """)

        # Insert another fixture article with empty description
        conn.execute("""
            INSERT INTO articles (article_id, provider, source_type, ticker,
                reference_date, published_utc, title, description,
                article_url, source_tier, is_rag_eligible, raw_asset_id)
            VALUES ('test:empty_desc', 'finnhub', 'finnhub_company_news', 'AAPL',
                '2025-01-16', '2025-01-16T10:00:00Z', 'Test Title With Empty Desc', '',
                'https://example.com/2', 2, 1, 'raw:empty_desc')
        """)
        conn.execute("""
            INSERT INTO article_tickers (article_id, ticker, raw_asset_id, reference_date)
            VALUES ('test:empty_desc', 'AAPL', 'raw:empty_desc', '2025-01-16')
        """)

        # Insert third article with non-empty description
        conn.execute("""
            INSERT INTO articles (article_id, provider, source_type, ticker,
                reference_date, published_utc, title, description,
                article_url, source_tier, is_rag_eligible, raw_asset_id)
            VALUES ('test:has_desc', 'finnhub', 'finnhub_company_news', 'AAPL',
                '2025-01-17', '2025-01-17T10:00:00Z', 'Test Title With Desc', 'Some description',
                'https://example.com/3', 2, 1, 'raw:has_desc')
        """)
        conn.execute("""
            INSERT INTO article_tickers (article_id, ticker, raw_asset_id, reference_date)
            VALUES ('test:has_desc', 'AAPL', 'raw:has_desc', '2025-01-17')
        """)
        conn.commit()

        # Query corpus_items VIEW
        rows = conn.execute(
            "SELECT corpus_item_id, content_md FROM corpus_items WHERE source_kind='article' ORDER BY corpus_item_id"
        ).fetchall()
        conn.close()

        results = {r["corpus_item_id"]: r["content_md"] for r in rows}

        # NULL description → title + \n + '' = title\n
        null_content = results.get("test:null_desc", "")
        assert null_content == "Test Title With Null Desc\n", \
            f"NULL desc: expected 'Test Title With Null Desc\\n', got {null_content!r}"

        # Empty description → title + \n + '' = title\n
        empty_content = results.get("test:empty_desc", "")
        assert empty_content == "Test Title With Empty Desc\n", \
            f"Empty desc: expected 'Test Title With Empty Desc\\n', got {empty_content!r}"

        # Non-empty description → title + \n + description
        has_content = results.get("test:has_desc", "")
        assert has_content == "Test Title With Desc\nSome description", \
            f"Has desc: expected 'Test Title With Desc\\nSome description', got {has_content!r}"


class TestCorpusItemsViewRefresh:
    """_ensure_corpus_items_view must replace a stale VIEW, not skip with IF NOT EXISTS."""

    OLD_VIEW_SQL = """CREATE VIEW IF NOT EXISTS corpus_items AS
SELECT
    a.article_id AS corpus_item_id,
    'article'     AS source_kind,
    at.ticker,
    a.provider,
    a.source_type,
    at.reference_date,
    a.published_utc,
    CASE
        WHEN a.description IS NULL OR a.description = ''
        THEN a.title
        ELSE a.title || char(10) || a.description
    END AS content_md,
    a.title,
    a.article_url,
    a.publisher_name,
    a.source_tier,
    at.dedup_group_id,
    at.is_canonical,
    a.is_rag_eligible,
    'l1'          AS chunk_level
FROM articles a
INNER JOIN article_tickers at ON a.article_id = at.article_id
UNION ALL
SELECT
    ranked.filing_id AS corpus_item_id,
    'filing'      AS source_kind,
    ranked.ticker,
    'sec'         AS provider,
    'sec_filing'  AS source_type,
    ranked.filed_at AS reference_date,
    ranked.filed_at AS published_utc,
    CASE
        WHEN ranked.doc_text IS NOT NULL AND ranked.doc_text != ''
        THEN (ranked.form_type || ' filed ' || ranked.filed_at) || char(10) || ranked.doc_text
        ELSE (ranked.form_type || ' filed ' || ranked.filed_at)
    END AS content_md,
    ranked.form_type AS title,
    ranked.url AS article_url,
    'SEC'         AS publisher_name,
    ranked.source_tier,
    ranked.dedup_group_id,
    ranked.is_canonical,
    ranked.is_rag_eligible,
    'l1'          AS chunk_level
FROM (
    SELECT
        f.filing_id,
        f.ticker,
        f.form_type,
        f.filed_at,
        f.url,
        f.source_tier,
        f.dedup_group_id,
        f.is_canonical,
        f.is_rag_eligible,
        fd.text AS doc_text,
        fd.extraction_status,
        ROW_NUMBER() OVER (
            PARTITION BY f.filing_id
            ORDER BY CASE fd.document_type WHEN 'exhibit_99_1' THEN 0 ELSE 1 END,
                     fd.document_type
        ) AS doc_rank
    FROM filings f
    LEFT JOIN filing_documents fd
        ON f.filing_id = fd.filing_id
        AND fd.extraction_status = 'success'
    WHERE f.is_rag_eligible = 1
) ranked
WHERE ranked.doc_rank = 1"""

    def test_refresh_replaces_stale_view(self, tmp_path):
        """Create DB with old VIEW, call refresh, verify new SQL is in sqlite_master."""
        import sqlite3 as _sqlite3
        from catalyst_data.storage.sqlite import init_db, _ensure_corpus_items_view

        db_path = str(tmp_path / "test_stale_view.db")
        conn = _sqlite3.connect(db_path)

        # Init DB (creates tables, then view)
        init_db(conn)

        # Manually replace the view with the OLD (stale) definition
        conn.execute("DROP VIEW IF EXISTS corpus_items")
        conn.executescript(self.OLD_VIEW_SQL)
        conn.commit()

        # Verify it's the old VIEW
        old_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name='corpus_items'"
        ).fetchone()[0]
        assert "CASE" in old_sql and "WHEN a.description IS NULL" in old_sql, \
            f"Stale VIEW not seeded correctly: {old_sql[:200]}"

        # Now call _ensure_corpus_items_view — must replace the stale VIEW
        _ensure_corpus_items_view(conn)

        # Verify new VIEW has corrected SQL
        new_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name='corpus_items'"
        ).fetchone()[0]
        assert "COALESCE(a.description, '')" in new_sql, \
            f"Refreshed VIEW missing COALESCE: {new_sql[:200]}"
        assert "CASE" not in new_sql or "WHEN a.description IS NULL" not in new_sql, \
            f"Refreshed VIEW still has old CASE WHEN: {new_sql[:200]}"

        conn.close()

    def test_refresh_is_idempotent(self, tmp_path):
        """Calling refresh on already-correct VIEW does not break it."""
        import sqlite3 as _sqlite3
        from catalyst_data.storage.sqlite import init_db, _ensure_corpus_items_view

        db_path = str(tmp_path / "test_idempotent_refresh.db")
        conn = _sqlite3.connect(db_path)
        init_db(conn)

        # First refresh (view is already correct from init_db)
        _ensure_corpus_items_view(conn)
        sql1 = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name='corpus_items'"
        ).fetchone()[0]

        # Second refresh
        _ensure_corpus_items_view(conn)
        sql2 = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name='corpus_items'"
        ).fetchone()[0]

        assert sql1 == sql2, "View SQL changed across idempotent refresh"
        assert "COALESCE(a.description, '')" in sql2
        conn.close()

    def test_sqlite_master_has_corrected_expression(self, tmp_path):
        """After init_db, sqlite_master.sql must contain corrected content_md expression."""
        import sqlite3 as _sqlite3
        from catalyst_data.storage.sqlite import init_db

        db_path = str(tmp_path / "test_expression.db")
        conn = _sqlite3.connect(db_path)
        init_db(conn)

        view_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name='corpus_items'"
        ).fetchone()[0]

        # Must have corrected article content_md
        assert "a.title || char(10) || COALESCE(a.description, '')" in view_sql, \
            f"Missing corrected content_md expression in VIEW SQL"

        # Must NOT have old CASE WHEN for article description
        assert "WHEN a.description IS NULL" not in view_sql, \
            f"Stale CASE WHEN still present in VIEW SQL"

        conn.close()

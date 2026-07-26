"""Tests for update_pipeline.py — full pipeline with mocked fetch_fn."""

from __future__ import annotations

import sqlite3

import pytest

from catalyst_data.update_pipeline import compute_missing_cells, run_update_batch
from catalyst_data.storage.sqlite import init_db, upsert_raw_asset
from catalyst_data.quality import ensure_ingestion_quality_tables
from catalyst_data.articles import ensure_articles_table, upsert_article, upsert_article_ticker


def _make_db(db_path: str, *, with_checkpoints: bool = False) -> sqlite3.Connection:
    """Create a minimal DB with ohlcv + articles."""
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)
    ensure_articles_table(conn)

    # 3 trading days, 2 tickers
    for dt in ("2026-06-30", "2026-06-29", "2026-06-28"):
        for sym in ("AAPL", "TSLA"):
            conn.execute(
                "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES (?, ?, 100.0)",
                (sym, dt),
            )

    if with_checkpoints:
        # Pre-populate: AAPL 2026-06-30 is already successful
        conn.execute(
            "INSERT OR REPLACE INTO source_checkpoints "
            "(run_id, source_type, ticker, date, status) "
            "VALUES ('run_prev', 'polygon_news', 'AAPL', '2026-06-30', 'success')"
        )
        # One failed checkpoint that should be retried
        conn.execute(
            "INSERT OR REPLACE INTO source_checkpoints "
            "(run_id, source_type, ticker, date, status, error_class, retries) "
            "VALUES ('run_prev', 'polygon_news', 'TSLA', '2026-06-29', 'failed', "
            "'TimeoutError', 1)"
        )

    conn.commit()
    return conn


class TestComputeMissingCells:
    def test_all_covered_returns_empty(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path, with_checkpoints=True)

        missing = compute_missing_cells(
            conn, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-30", to_date="2026-06-30",
        )
        assert missing == []
        conn.close()

    def test_failed_cells_included(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path, with_checkpoints=True)

        missing = compute_missing_cells(
            conn, tickers=["TSLA"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-29",
        )
        assert ("TSLA", "2026-06-29", "polygon_news") in missing
        conn.close()

    def test_uncovered_cells_returned(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        missing = compute_missing_cells(
            conn, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-30",
        )
        assert len(missing) == 2
        conn.close()

    def test_ticker_filter_respected(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        missing = compute_missing_cells(
            conn, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-30",
        )
        for cell in missing:
            assert cell[0] == "AAPL"
        conn.close()

    def test_source_filter_respected(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        missing = compute_missing_cells(
            conn, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-30",
        )
        for cell in missing:
            assert cell[2] == "polygon_news"
        conn.close()

    def test_empty_window_returns_empty(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        missing = compute_missing_cells(
            conn, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-30", to_date="2026-06-29",
        )
        assert missing == []
        conn.close()

    def test_partial_ohlcv_window_includes_later_calendar_trading_days(self, tmp_path):
        """OHLCV coverage must not truncate a requested future/backfill window."""
        db_path = str(tmp_path / "partial_calendar.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        ensure_articles_table(conn)
        for dt in ("2026-06-29", "2026-06-30"):
            conn.execute(
                "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES (?, ?, 100.0)",
                ("AAPL", dt),
            )
        conn.commit()

        missing = compute_missing_cells(
            conn,
            tickers=["AAPL"],
            sources=["polygon_news"],
            from_date="2026-06-29",
            to_date="2026-07-07",
        )
        dates = [date for _, date, _ in missing]

        assert "2026-07-06" in dates
        assert "2026-07-07" in dates
        assert "2026-07-03" not in dates
        assert "2026-07-04" not in dates
        assert "2026-07-05" not in dates
        conn.close()

    def test_empty_ohlcv_window_uses_calendar_trading_days(self, tmp_path):
        db_path = str(tmp_path / "empty_calendar.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        ensure_articles_table(conn)
        conn.commit()

        missing = compute_missing_cells(
            conn,
            tickers=["AAPL"],
            sources=["polygon_news"],
            from_date="2026-07-02",
            to_date="2026-07-06",
        )
        dates = [date for _, date, _ in missing]

        assert dates == ["2026-07-02", "2026-07-06"]
        conn.close()

    def test_full_ohlcv_window_unchanged_but_holiday_and_weekend_excluded(self, tmp_path):
        db_path = str(tmp_path / "full_calendar.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        ensure_articles_table(conn)
        for dt in ("2026-07-02", "2026-07-06"):
            conn.execute(
                "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES (?, ?, 100.0)",
                ("AAPL", dt),
            )
        conn.commit()

        missing = compute_missing_cells(
            conn,
            tickers=["AAPL"],
            sources=["polygon_news"],
            from_date="2026-07-02",
            to_date="2026-07-06",
        )
        dates = [date for _, date, _ in missing]

        assert dates == ["2026-07-02", "2026-07-06"]
        conn.close()


class TestRunUpdateBatchDryRun:
    def test_dry_run_no_network(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        import asyncio

        async def _run():
            return await run_update_batch(
                db_path,
                tickers=["AAPL"],
                sources=["polygon_news"],
                from_date="2026-06-29",
                to_date="2026-06-30",
                dry_run=True,
            )

        report = asyncio.run(_run())

        assert report["mode"] == "dry-run"

        assert report["cells_success"] == 0
        assert report["cells_failed"] == 0
        assert report["run_id"] is None
        assert report["cells_total"] > 0

    def test_dry_run_zero_db_writes(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        before_art = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        before_at = conn.execute(
            "SELECT COUNT(*) FROM article_tickers"
        ).fetchone()[0]
        before_cp = conn.execute(
            "SELECT COUNT(*) FROM source_checkpoints"
        ).fetchone()[0]
        before_runs = conn.execute(
            "SELECT COUNT(*) FROM ingestion_runs"
        ).fetchone()[0]
        conn.close()

        import asyncio

        async def _run():
            return await run_update_batch(
                db_path,
                tickers=["AAPL"],
                sources=["polygon_news"],
                from_date="2026-06-29",
                to_date="2026-06-30",
                dry_run=True,
            )

        asyncio.run(_run())

        conn = sqlite3.connect(db_path)
        after_art = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        after_at = conn.execute(
            "SELECT COUNT(*) FROM article_tickers"
        ).fetchone()[0]
        after_cp = conn.execute(
            "SELECT COUNT(*) FROM source_checkpoints"
        ).fetchone()[0]
        after_runs = conn.execute(
            "SELECT COUNT(*) FROM ingestion_runs"
        ).fetchone()[0]
        conn.close()

        assert after_art == before_art
        assert after_at == before_at
        assert after_cp == before_cp
        assert after_runs == before_runs

    def test_dry_run_requires_no_fetch_fn(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        import asyncio

        with pytest.raises(ValueError, match="fetch_fn"):
            asyncio.run(
                run_update_batch(
                    db_path,
                    tickers=["AAPL"],
                    from_date="2026-06-29",
                    to_date="2026-06-30",
                    dry_run=False,
                )
            )


class TestRunUpdateBatchReal:
    """Mocked real-path tests.  Uses await directly (pytest-asyncio auto mode)."""

    async def test_mocked_fetch(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        async def mock_fetch(ticker, endpoint, date):
            from catalyst_data.connectors.base import FetchResult
            return FetchResult(
                status=200,
                data={"results": []},
                source_label=endpoint,
            )

        report = await run_update_batch(
            db_path,
            tickers=["AAPL"],
            sources=["polygon_news"],
            from_date="2026-06-29",
            to_date="2026-06-30",
            fetch_fn=mock_fetch,
            limit=1,
            dry_run=False,
        )

        assert report["mode"] == "update"
        assert report["run_id"] is not None

    async def test_idempotent_rerun(self, tmp_path):
        """Dry-run re-run: all cells already checkpointed → zero missing."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path, with_checkpoints=True)
        conn.close()

        # All AAPL cells in window already have success checkpoints
        report = await run_update_batch(
            db_path, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-30", to_date="2026-06-30",
            dry_run=True,
        )
        assert report["cells_total"] == 0
        assert report["mode"] == "dry-run"

    async def test_checkpoint_skip(self, tmp_path):
        """Cells with a success checkpoint are skipped on re-run."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path, with_checkpoints=True)
        conn.close()

        fetch_calls = []

        async def mock_fetch(ticker, endpoint, date):
            fetch_calls.append((ticker, endpoint, date))
            from catalyst_data.connectors.base import FetchResult
            return FetchResult(
                status=200, data={"results": []},
                source_label=endpoint,
            )

        report = await run_update_batch(
            db_path, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-28", to_date="2026-06-30",
            fetch_fn=mock_fetch, dry_run=False,
        )

        # AAPL 2026-06-30 has success checkpoint → skipped
        fetched_tickers = {f[0] for f in fetch_calls}
        assert all(t == "AAPL" for t in fetched_tickers)
        fetched_dates = {f[1] for f in fetch_calls}
        assert "2026-06-30" not in fetched_dates

    async def test_polygon_only_batch_dedups_existing_finnhub_corpus(self, tmp_path):
        """Root cause: dedup must not be gated on finnhub being in current sources."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        upsert_raw_asset(
            conn,
            asset_id="raw-finnhub-existing",
            ticker="AAPL",
            source_type="finnhub_company_news",
            reference_date="2026-06-29",
            content_raw=b"[]",
            http_status=200,
        )
        upsert_article(conn, article={
            "article_id": "finnhub:existing",
            "raw_asset_id": "raw-finnhub-existing",
            "provider": "finnhub",
            "source_type": "finnhub_company_news",
            "ticker": "AAPL",
            "reference_date": "2026-06-29",
            "published_utc": "2026-06-29T10:30:00+00:00",
            "title": "Shared infrastructure update",
            "description": "Finnhub body",
            "article_url": "https://example.com/finnhub-existing",
            "publisher_name": "Finnhub Publisher",
            "keywords_json": "[]",
            "insights_json": "[]",
            "tickers_json": '["AAPL"]',
        })
        upsert_article_ticker(
            conn,
            article_id="finnhub:existing",
            ticker="AAPL",
            raw_asset_id="raw-finnhub-existing",
            reference_date="2026-06-29",
        )
        conn.close()

        async def mock_fetch(ticker, endpoint, date):
            from catalyst_data.connectors.base import FetchResult
            return FetchResult(
                status=200,
                data={"results": [{
                    "id": "poly-existing",
                    "title": "Shared infrastructure update",
                    "description": "Polygon body",
                    "published_utc": "2026-06-29T10:00:00Z",
                    "article_url": "https://example.com/poly-existing",
                    "publisher": {"name": "Polygon Publisher"},
                }]},
                source_label=endpoint,
            )

        await run_update_batch(
            db_path, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-29",
            fetch_fn=mock_fetch, limit=1, dry_run=False,
        )

        conn = sqlite3.connect(db_path)
        groups = conn.execute(
            """SELECT at.dedup_group_id, COUNT(DISTINCT at.article_id), SUM(a.is_canonical)
               FROM article_tickers at
               JOIN articles a ON a.article_id = at.article_id
               WHERE at.article_id IN ('finnhub:existing', 'poly:poly-existing')
               GROUP BY at.dedup_group_id"""
        ).fetchall()
        conn.close()

        assert len(groups) == 1
        assert groups[0][0] is not None
        assert groups[0][1:] == (2, 1)

    async def test_no_index_state_writes_from_dry_run(self, tmp_path):
        """Verify pipeline does NOT write index_state/manifests."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        async def mock_fetch(ticker, endpoint, date):
            from catalyst_data.connectors.base import FetchResult
            return FetchResult(
                status=200, data={"results": []},
                source_label=endpoint,
            )

        conn = sqlite3.connect(db_path)
        before_is = conn.execute("SELECT COUNT(*) FROM index_state").fetchone()[0]
        before_im = conn.execute("SELECT COUNT(*) FROM index_manifests").fetchone()[0]
        conn.close()

        await run_update_batch(
            db_path, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-29",
            fetch_fn=mock_fetch, limit=1, dry_run=False,
        )

        conn = sqlite3.connect(db_path)
        after_is = conn.execute("SELECT COUNT(*) FROM index_state").fetchone()[0]
        after_im = conn.execute("SELECT COUNT(*) FROM index_manifests").fetchone()[0]
        conn.close()

        assert after_is == before_is
        assert after_im == before_im


# ============================================================================
# SEC filings pipeline tests (Step 3C2)
# ============================================================================

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from catalyst_data.update_pipeline import _fetch_cell
from catalyst_data.storage.sqlite import ensure_filings_tables


def _make_sec_db(db_path: str) -> sqlite3.Connection:
    """Create minimal DB with ohlcv + filings tables."""
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)
    ensure_filings_tables(conn)

    # 2 trading days, 2 tickers
    for dt in ("2026-06-30", "2026-06-29"):
        for sym in ("AAPL", "TSLA"):
            conn.execute(
                "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES (?, ?, 100.0)",
                (sym, dt),
            )

    conn.commit()
    return conn


def _mock_sec_submissions(ticker: str = "AAPL", cik: str = "0000320193",
                          filed_at: str = "2026-06-30",
                          form: str = "8-K",
                          items: str = "2.02",
                          accession: str = "0000320193-26-000011",
                          include_filing: bool = True) -> dict:
    """Build a mock SEC submissions response matching real SEC shape."""
    data = {
        "filings": {
            "recent": {
                "form": [form] if include_filing else [],
                "filingDate": [filed_at] if include_filing else [],
                "accessionNumber": [accession] if include_filing else [],
                "reportDate": [filed_at] if include_filing else [],
                "primaryDocument": [
                    "a8-kq2202503292025.htm"
                ] if include_filing else [],
                "items": [items] if include_filing else [],
            }
        }
    }
    return data


class TestComputeMissingCellsSEC:
    """compute_missing_cells with sec_filings source."""

    def test_sec_filings_in_source_list(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_sec_db(db_path)

        missing = compute_missing_cells(
            conn,
            tickers=["AAPL", "TSLA"],
            sources=["sec_filings"],
            from_date="2026-06-29",
            to_date="2026-06-30",
        )
        # 2 tickers x 2 days = 4 cells, none covered yet
        assert len(missing) == 4
        conn.close()

    def test_sec_filings_covered_when_checkpoint_exists(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_sec_db(db_path)

        # Write a success checkpoint for AAPL on 2026-06-30
        conn.execute(
            "INSERT OR REPLACE INTO source_checkpoints "
            "(run_id, source_type, ticker, date, status) "
            "VALUES ('run_test', 'sec_filings', 'AAPL', '2026-06-30', 'success')"
        )
        conn.commit()

        missing = compute_missing_cells(
            conn,
            tickers=["AAPL", "TSLA"],
            sources=["sec_filings"],
            from_date="2026-06-29",
            to_date="2026-06-30",
        )
        # AAPL 2026-06-30 is covered; remaining 3 cells
        assert len(missing) == 3
        # AAPL 2026-06-30 should NOT be in the list
        covered = ("AAPL", "2026-06-30", "sec_filings")
        assert covered not in missing
        conn.close()


class TestFetchCellSEC:
    """_fetch_cell SEC path tests with mocked fetcher namespace."""

    def test_zero_filings_in_range(self, tmp_path):
        """Submissions 200 but no filings matching date → success, counts=0."""
        db_path = str(tmp_path / "test.db")
        conn = _make_sec_db(db_path)
        conn.close()

        # Mock fetcher: submissions returns empty filing list for this date
        fetcher = SimpleNamespace(
            fetch=AsyncMock(return_value=SimpleNamespace(
                status=200,
                data=_mock_sec_submissions(include_filing=False),
                error=None,
            )),
            fetch_document=AsyncMock(),
        )

        result = asyncio.run(_fetch_cell(
            db_path=db_path,
            ticker="AAPL",
            date="2026-06-29",
            source="sec_filings",
            run_id="run_test_zero",
            fetch_fn=fetcher.fetch,
            fetcher_ns=fetcher,
        ))

        assert result["status"] == "success"
        assert result["filings_count"] == 0
        assert result["documents_count"] == 0
        assert result["ticker"] == "AAPL"

        # Checkpoint written
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        cp = conn.execute(
            "SELECT status FROM source_checkpoints "
            "WHERE source_type='sec_filings' AND ticker='AAPL' AND date='2026-06-29'"
        ).fetchone()
        assert cp is not None
        assert cp[0] == "success"
        conn.close()

    def test_one_filing_with_documents(self, tmp_path):
        """One 8-K with EX-99.1 → filing + 2 documents stored, counts correct."""
        db_path = str(tmp_path / "test.db")
        conn = _make_sec_db(db_path)
        conn.close()

        # Mock submissions with a single 8-K
        submissions_data = _mock_sec_submissions(
            ticker="AAPL", cik="0000320193",
            filed_at="2026-06-30", form="8-K",
            items="2.02", accession="0000320193-26-000011",
        )

        # Mock fetch_document: returns raw_bytes for Bronze, text for Silver
        raw_primary = b"<html><body><p>SECURITIES AND EXCHANGE COMMISSION cover page text.</p></body></html>"
        raw_exhibit = b"<DOCUMENT><TYPE>EX-99.1<TEXT>Apple reports Q2 earnings. Net sales increased 12%.</TEXT></DOCUMENT>"

        async def mock_fetch_doc(url: str):
            if "primary" in url or "a8-k" in url.lower():
                return SimpleNamespace(
                    status=200,
                    data={
                        "url": url,
                        "text": "SECURITIES AND EXCHANGE COMMISSION cover page text.",
                        "raw_bytes": raw_primary,
                        "content_type": "text/html",
                        "byte_size": len(raw_primary),
                        "extraction_status": "success",
                    },
                )
            elif "ex99" in url.lower():
                return SimpleNamespace(
                    status=200,
                    data={
                        "url": url,
                        "text": "Apple reports Q2 earnings. Net sales increased 12%.",
                        "raw_bytes": raw_exhibit,
                        "content_type": "text/html",
                        "byte_size": len(raw_exhibit),
                        "extraction_status": "success",
                    },
                )
            return SimpleNamespace(status=404, data=None)

        fetcher = SimpleNamespace(
            fetch=AsyncMock(return_value=SimpleNamespace(
                status=200,
                data=submissions_data,
                error=None,
            )),
            fetch_document=mock_fetch_doc,
        )

        result = asyncio.run(_fetch_cell(
            db_path=db_path,
            ticker="AAPL",
            date="2026-06-30",
            source="sec_filings",
            run_id="run_test_one",
            fetch_fn=fetcher.fetch,
            fetcher_ns=fetcher,
        ))

        assert result["status"] == "success"
        assert result["filings_count"] == 1
        assert result["documents_count"] >= 1  # at least primary_doc

        # Verify filing stored
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        filing_row = conn.execute(
            "SELECT filing_id, form_type, filed_at, is_rag_eligible "
            "FROM filings WHERE ticker='AAPL'"
        ).fetchone()
        assert filing_row is not None
        assert filing_row[1] == "8-K"
        assert filing_row[2] == "2026-06-30"
        assert filing_row[3] == 1  # is_rag_eligible

        # Verify filing_documents stored
        doc_rows = conn.execute(
            "SELECT filing_id, document_type, extraction_status "
            "FROM filing_documents WHERE filing_id=?", (filing_row[0],)
        ).fetchall()
        assert len(doc_rows) >= 1
        doc_types = [r[1] for r in doc_rows]
        assert any(t in doc_types for t in ("primary_doc", "exhibit_99_1"))

        # Verify raw_assets stored for submissions
        raw_count = conn.execute(
            "SELECT COUNT(*) FROM raw_assets WHERE source_type='sec_submissions'"
        ).fetchone()[0]
        assert raw_count >= 1

        # Verify Bronze (raw_assets) for sec_primary_doc stores RAW HTML, not extracted text
        from catalyst_data.storage.sqlite import get_raw_asset
        raw_doc_rows = conn.execute(
            "SELECT asset_id FROM raw_assets WHERE source_type='sec_primary_doc'"
        ).fetchall()
        assert len(raw_doc_rows) >= 1, "No sec_primary_doc raw_assets stored"

        raw_asset = get_raw_asset(conn, raw_doc_rows[0][0])
        assert raw_asset is not None
        decompressed = raw_asset["content_raw"]
        assert decompressed is not None, "Decompressed Bronze content is None"
        # Must contain raw HTML markers, NOT just stripped text
        assert b"<" in decompressed, "Bronze content should contain HTML tags"
        assert b"</html>" in decompressed or b"<DOCUMENT>" in decompressed,             "Bronze content missing expected HTML/DOCUMENT markers"

        # Prove Bronze is re-derivable into Silver:
        # extract_text_from_html(decompressed_raw) reproduces filing_documents.text
        from catalyst_data.pipeline.sec_normalize import extract_text_from_html
        decompressed_text = decompressed.decode("latin-1", errors="replace")
        rederived = extract_text_from_html(decompressed_text)
        doc_text_rows = conn.execute(
            "SELECT text FROM filing_documents WHERE filing_id=?",
            (filing_row[0],)
        ).fetchall()
        assert len(doc_text_rows) >= 1
        stored_texts = {r[0] for r in doc_text_rows if r[0]}
        # At least one stored text should be contained within the re-derived text,
        # or the re-derived text should match one of the stored texts
        text_match = any(
            stored in rederived or rederived in stored
            for stored in stored_texts
        )
        assert text_match, (
            f"Re-derived text does not match any stored filing_documents.text. "
            f"Rederved: {rederived[:200]!r}... Stored: {stored_texts}"
        )

        # Checkpoint written
        cp = conn.execute(
            "SELECT status FROM source_checkpoints "
            "WHERE source_type='sec_filings' AND ticker='AAPL' AND date='2026-06-30'"
        ).fetchone()
        assert cp is not None
        assert cp[0] == "success"
        conn.close()

    def test_fetch_document_invoked(self, tmp_path):
        """Carry-forward E: assert fetch_document is actually called on SEC path."""
        db_path = str(tmp_path / "test.db")
        conn = _make_sec_db(db_path)
        conn.close()

        submissions_data = _mock_sec_submissions(
            ticker="AAPL", cik="0000320193",
            filed_at="2026-06-30", form="8-K",
            items="2.02",
        )

        fetch_doc_mock = AsyncMock(return_value=SimpleNamespace(
            status=200,
            data={
                "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019325000055/a8-k.htm",
                "text": "Test earnings text.",
                "content_type": "text/html",
                "byte_size": 100,
                "extraction_status": "success",
            },
        ))

        fetcher = SimpleNamespace(
            fetch=AsyncMock(return_value=SimpleNamespace(
                status=200, data=submissions_data, error=None,
            )),
            fetch_document=fetch_doc_mock,
        )

        _ = asyncio.run(_fetch_cell(
            db_path=db_path,
            ticker="AAPL",
            date="2026-06-30",
            source="sec_filings",
            run_id="run_test_e",
            fetch_fn=fetcher.fetch,
            fetcher_ns=fetcher,
        ))

        # fetch_document MUST have been called at least once
        assert fetch_doc_mock.call_count >= 1, (
            "fetch_document was NOT invoked on the SEC path"
        )

    def test_fetcher_ns_required_for_sec(self, tmp_path):
        """sec_filings without fetcher_ns raises ValueError."""
        db_path = str(tmp_path / "test.db")
        conn = _make_sec_db(db_path)
        conn.close()

        with pytest.raises(ValueError, match="fetcher_ns"):
            asyncio.run(_fetch_cell(
                db_path=db_path,
                ticker="AAPL",
                date="2026-06-30",
                source="sec_filings",
                run_id="run_test_ns",
                fetch_fn=AsyncMock(),
                fetcher_ns=None,
            ))

    def test_no_cik_for_ticker(self, tmp_path):
        """sec_filings for ticker without CIK → failed with NoCIK."""
        db_path = str(tmp_path / "test.db")
        conn = _make_sec_db(db_path)
        conn.close()

        fetcher = SimpleNamespace(
            fetch=AsyncMock(),
            fetch_document=AsyncMock(),
        )

        result = asyncio.run(_fetch_cell(
            db_path=db_path,
            ticker="ZZZZZ",
            date="2026-06-30",
            source="sec_filings",
            run_id="run_test_nocik",
            fetch_fn=fetcher.fetch,
            fetcher_ns=fetcher,
        ))

        assert result["status"] == "failed"
        assert "No CIK" in (result.get("error") or "")

    def test_submissions_http_error(self, tmp_path):
        """Submissions fetch fails → checkpoint written as failed."""
        db_path = str(tmp_path / "test.db")
        conn = _make_sec_db(db_path)
        conn.close()

        fetcher = SimpleNamespace(
            fetch=AsyncMock(return_value=SimpleNamespace(
                status=500, data=None, error="Server error",
            )),
            fetch_document=AsyncMock(),
        )

        result = asyncio.run(_fetch_cell(
            db_path=db_path,
            ticker="AAPL",
            date="2026-06-30",
            source="sec_filings",
            run_id="run_test_err",
            fetch_fn=fetcher.fetch,
            fetcher_ns=fetcher,
        ))

        assert result["status"] == "failed"

        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        cp = conn.execute(
            "SELECT status FROM source_checkpoints "
            "WHERE source_type='sec_filings' AND ticker='AAPL' AND date='2026-06-30'"
        ).fetchone()
        assert cp is not None
        assert cp[0] == "failed"
        conn.close()


class TestDryRunSECNoWrites:
    """run_update_batch dry-run with sec_filings: ZERO DB writes."""

    def test_dry_run_sec_zero_db_writes(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_sec_db(db_path)
        conn.close()

        # Snapshot counts
        def _counts():
            c = sqlite3.connect(db_path)
            c.row_factory = lambda cur, row: row
            tables = ["filings", "filing_documents", "raw_assets",
                      "source_checkpoints", "index_state"]
            counts = {}
            for t in tables:
                try:
                    n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except sqlite3.OperationalError:
                    n = 0
                counts[t] = n
            c.close()
            return counts

        before = _counts()

        report = asyncio.run(run_update_batch(
            db_path,
            tickers=["AAPL"],
            sources=["sec_filings"],
            from_date="2026-06-29",
            to_date="2026-06-30",
            fetch_fn=None,
            dry_run=True,
        ))

        assert report["mode"] == "dry-run"
        assert report["cells_total"] >= 0

        after = _counts()
        for tbl in before:
            assert after[tbl] == before[tbl], (
                f"Table {tbl} changed: {before[tbl]} → {after[tbl]} "
                f"(dry-run must have ZERO DB writes)"
            )

    def test_dry_run_sec_no_network(self, tmp_path):
        """Dry-run with sec_filings must not require fetch_fn."""
        db_path = str(tmp_path / "test.db")
        conn = _make_sec_db(db_path)
        conn.close()

        report = asyncio.run(run_update_batch(
            db_path,
            tickers=["AAPL"],
            sources=["sec_filings"],
            from_date="2026-06-29",
            to_date="2026-06-30",
            fetch_fn=None,
            dry_run=True,
        ))

        assert report["mode"] == "dry-run"
        # No fetch_fn needed, no network


# ============================================================================
# Finnhub company-news pipeline tests (Step 3D)
# ============================================================================

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from catalyst_data.update_pipeline import _fetch_cell
from catalyst_data.storage.sqlite import ensure_filings_tables


class TestFetchCellFinnhub:
    """_fetch_cell Finnhub path tests with mocked fetcher namespace."""

    def _make_finnhub_db(self, db_path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        ensure_filings_tables(conn)
        for dt in ("2025-07-01", "2025-06-30"):
            conn.execute(
                "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES (?, ?, 100.0)",
                ("AAPL", dt),
            )
        conn.commit()
        return conn

    def test_finnhub_zero_articles(self, tmp_path):
        """Mocked 200 with empty array → success, articles_count=0, checkpoint written."""
        db_path = str(tmp_path / "test.db")
        conn = self._make_finnhub_db(db_path)
        conn.close()

        fetcher = SimpleNamespace(
            fetch=AsyncMock(return_value=SimpleNamespace(
                status=200,
                data=[],
                error=None,
            )),
        )

        result = asyncio.run(_fetch_cell(
            db_path=db_path,
            ticker="AAPL",
            date="2025-07-01",
            source="finnhub_company_news",
            run_id="run_fh_zero",
            fetch_fn=fetcher.fetch,
            fetcher_ns=fetcher,
        ))

        assert result["status"] == "success"
        assert result["articles_count"] == 0
        assert result["ticker"] == "AAPL"

        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        cp = conn.execute(
            "SELECT status FROM source_checkpoints "
            "WHERE source_type='finnhub_company_news' AND ticker='AAPL' AND date='2025-07-01'"
        ).fetchone()
        assert cp is not None
        assert cp[0] == "success"

        # Raw asset stored even for 0 articles
        ra = conn.execute(
            "SELECT COUNT(*) FROM raw_assets WHERE source_type='finnhub_company_news'"
        ).fetchone()[0]
        assert ra >= 1
        conn.close()

    def test_finnhub_with_articles(self, tmp_path):
        """Mocked 200 with 3 articles → success, articles_count=3, raw_asset stored."""
        db_path = str(tmp_path / "test.db")
        conn = self._make_finnhub_db(db_path)
        conn.close()

        mock_articles = [
            {"id": 1, "headline": "News 1", "summary": "Body 1",
             "datetime": 1751385600, "source": "Yahoo", "url": "https://x.com/1"},
            {"id": 2, "headline": "News 2", "summary": "Body 2",
             "datetime": 1751385600, "source": "Reuters", "url": "https://x.com/2"},
            {"id": 3, "headline": "News 3", "summary": "Body 3",
             "datetime": 1751385600, "source": "Bloomberg", "url": "https://x.com/3"},
        ]

        fetcher = SimpleNamespace(
            fetch=AsyncMock(return_value=SimpleNamespace(
                status=200,
                data=mock_articles,
                error=None,
            )),
        )

        result = asyncio.run(_fetch_cell(
            db_path=db_path,
            ticker="AAPL",
            date="2025-07-01",
            source="finnhub_company_news",
            run_id="run_fh_one",
            fetch_fn=fetcher.fetch,
            fetcher_ns=fetcher,
        ))

        assert result["status"] == "success"
        assert result["articles_count"] == 3

        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)

        # Raw asset stored with correct metadata
        ra = conn.execute(
            "SELECT content_raw, metadata_json FROM raw_assets "
            "WHERE source_type='finnhub_company_news' AND ticker='AAPL'"
        ).fetchone()
        assert ra is not None
        import zlib
        decompressed = zlib.decompress(ra[0])
        parsed = json.loads(decompressed)
        assert len(parsed) == 3
        assert parsed[0]["headline"] == "News 1"

        meta = json.loads(ra[1])
        assert meta["article_count"] == 3

        conn.close()

    def test_finnhub_http_error(self, tmp_path):
        """Mocked 500 → failed checkpoint."""
        db_path = str(tmp_path / "test.db")
        conn = self._make_finnhub_db(db_path)
        conn.close()

        fetcher = SimpleNamespace(
            fetch=AsyncMock(return_value=SimpleNamespace(
                status=500,
                data=None,
                error="Server error",
            )),
        )

        result = asyncio.run(_fetch_cell(
            db_path=db_path,
            ticker="AAPL",
            date="2025-07-01",
            source="finnhub_company_news",
            run_id="run_fh_err",
            fetch_fn=fetcher.fetch,
            fetcher_ns=fetcher,
        ))

        assert result["status"] == "failed"
        assert "Server error" in (result.get("error") or "")

        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)
        cp = conn.execute(
            "SELECT status FROM source_checkpoints "
            "WHERE source_type='finnhub_company_news' AND ticker='AAPL' AND date='2025-07-01'"
        ).fetchone()
        assert cp is not None
        assert cp[0] == "failed"
        conn.close()

    def test_finnhub_dry_run_zero_writes(self, tmp_path):
        """Dry-run: zero new rows in raw_assets."""
        db_path = str(tmp_path / "test.db")
        conn = self._make_finnhub_db(db_path)

        ra_before = conn.execute(
            "SELECT COUNT(*) FROM raw_assets"
        ).fetchone()[0]
        conn.close()

        # Dry-run: fetch_fn=None, _fetch_cell never called
        from catalyst_data.update_pipeline import run_update_batch

        async def _run():
            return await run_update_batch(
                db_path,
                tickers=["AAPL"],
                sources=["finnhub_company_news"],
                from_date="2025-07-01",
                to_date="2025-07-01",
                fetch_fn=None,
                dry_run=True,
            )

        report = asyncio.run(_run())

        assert report["mode"] == "dry-run"
        # No DB writes happened
        conn = sqlite3.connect(db_path)
        ra_after = conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0]
        assert ra_after == ra_before, "Dry-run must not write to raw_assets"
        conn.close()


class TestExecuteUpdateDrift:
    """B2 Task 4: PlanDriftError wired into execute_update."""

    @pytest.mark.asyncio
    async def test_execute_update_raises_plan_drift_before_transport(self):
        """execute_update raises PlanDriftError before any transport call."""
        from catalyst_data.update_planner import UpdatePlan, compute_plan_hash, PlanDriftError
        from catalyst_data.update_pipeline import execute_update
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(8)
        plan = UpdatePlan(
            universe={"tickers": ["AAPL"]},
            reference_today="2026-01-15",
            latest_closed_session="2026-01-14",
            config={"sources": ["polygon_news"]},
            stages={
                "market": {"cells": [], "count": 0},
                "evidence": {"cells": [("AAPL", "2026-01-14", "polygon_news")], "count": 1},
            },
        )
        plan.plan_hash = compute_plan_hash(plan)
        plan.expected_plan_hash = "0000000000000000000000000000000000000000000000000000000000000000"
        before_counts = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("ingestion_runs", "provider_request_attempts", "raw_assets", "normalized_provenance")
        }

        transport_calls = []
        async def fake_transport(*args, **kwargs):
            transport_calls.append(1)
            return {}

        with pytest.raises(PlanDriftError):
            await execute_update(
                db=db,
                plan=plan,
                transport=fake_transport,
            )

        assert len(transport_calls) == 0, "transport must not be called on drift"
        after_counts = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before_counts
        }
        assert after_counts == before_counts
        db.close()

    @pytest.mark.asyncio
    async def test_execute_update_no_drift_calls_transport(self):
        """When expected_plan_hash matches, execution calls transport and persists B2 state."""
        from catalyst_data.update_planner import UpdatePlan, compute_plan_hash
        from catalyst_data.update_pipeline import execute_update
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(8)
        plan = UpdatePlan(
            universe={"tickers": ["AAPL"]},
            reference_today="2026-01-15",
            latest_closed_session="2026-01-14",
            config={"sources": ["polygon_news"]},
            stages={
                "market": {"cells": [], "count": 0},
                "evidence": {"cells": [("AAPL", "2026-01-14", "polygon_news")], "count": 1},
            },
        )
        plan.plan_hash = compute_plan_hash(plan)
        plan.expected_plan_hash = plan.plan_hash  # match

        transport_calls = []
        async def fake_transport(provider, method, url, **kwargs):
            transport_calls.append((provider, method, url, kwargs))
            class FakeResponse:
                status_code = 200
                def json(self):
                    return {
                        "results": [
                            {
                                "id": "poly-1",
                                "title": "AAPL update",
                                "description": "body",
                                "published_utc": "2026-01-14T15:00:00Z",
                                "article_url": "https://example.com/aapl",
                                "tickers": ["AAPL"],
                                "publisher": {"name": "Example"},
                            }
                        ],
                        "next_url": None,
                    }
            return FakeResponse()

        report = await execute_update(
            db=db,
            plan=plan,
            transport=fake_transport,
        )
        assert len(transport_calls) == 1
        assert report["status"] == "SUCCEEDED"
        assert report["stage_sequence"] == ["ohlcv", "evidence"]
        assert report["cells_success"] == 1
        assert db.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM provider_request_attempts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM raw_assets WHERE data_version='v2'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM normalized_provenance").fetchone()[0] == 1
        db.close()

    @pytest.mark.asyncio
    async def test_execute_update_two_stage_replans_evidence_after_ohlcv(self):
        """OHLCV commits before evidence and evidence re-plan sees refreshed watermark."""
        from catalyst_data.update_planner import UpdatePlan, compute_plan_hash
        from catalyst_data.update_pipeline import execute_update
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(8)
        db.execute(
            "INSERT INTO ohlcv (symbol, date, open, high, low, close, volume, source) "
            "VALUES ('AAPL', '2026-01-13', 1, 1, 1, 1, 1, 'polygon')"
        )
        db.commit()
        plan = UpdatePlan(
            universe={"tickers": ["AAPL"]},
            reference_today="2026-01-15",
            latest_closed_session="2026-01-14",
            config={"sources": ["polygon_ohlcv", "polygon_news"], "allow_stale_ohlcv": True},
            stages={
                "market": {"cells": [("AAPL", "2026-01-14", "polygon_ohlcv")], "count": 1},
                "evidence": {"cells": [("AAPL", "2026-01-14", "polygon_news")], "count": 1, "provisional": True},
            },
        )
        plan.plan_hash = compute_plan_hash(plan)
        plan.expected_plan_hash = plan.plan_hash

        seen = []
        async def fake_transport(provider, method, url, **kwargs):
            seen.append((kwargs["source"], db.execute(
                "SELECT MAX(date) FROM ohlcv WHERE symbol='AAPL'"
            ).fetchone()[0]))
            class FakeResponse:
                status_code = 200
                def json(self):
                    if kwargs["source"] == "polygon_ohlcv":
                        return {"results": [{"o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000}]}
                    return {"results": [], "next_url": None}
            return FakeResponse()

        report = await execute_update(db=db, plan=plan, transport=fake_transport)

        assert seen == [
            ("polygon_ohlcv", "2026-01-13"),
            ("polygon_news", "2026-01-14"),
        ]
        assert report["stage_sequence"] == ["ohlcv", "evidence"]
        assert report["evidence_replan"]["latest_ohlcv_watermark"] == "2026-01-14"
        assert report["allow_stale_ohlcv_overridden"] is True
        db.close()

    @pytest.mark.asyncio
    async def test_run_update_batch_delegates_v8_non_dry_run_to_b2_executor(self, tmp_path, monkeypatch):
        """v8 non-dry-run uses execute_update so the B2 executor is not dead code."""
        from catalyst_data.storage.sqlite import init_db
        from catalyst_data.migrations import run_migrations
        from catalyst_data.update_pipeline import run_update_batch
        import catalyst_data.update_pipeline as update_pipeline

        db_path = tmp_path / "b2_delegate.db"
        conn = sqlite3.connect(db_path)
        init_db(conn)
        run_migrations(conn)
        conn.execute(
            "INSERT INTO ohlcv (symbol, date, open, high, low, close, volume, source) "
            "VALUES ('AAPL', '2026-01-14', 1, 1, 1, 1, 1, 'polygon')"
        )
        conn.commit()
        conn.close()

        delegated = {}
        async def fake_execute_update(*, db, plan, transport):
            delegated["sources"] = plan.config["sources"]
            delegated["transport"] = transport
            return {
                "run_id": "run-delegated",
                "mode": "update",
                "status": "SUCCEEDED",
                "cells_total": 0,
                "cells_success": 0,
                "cells_failed": 0,
                "cells_skipped": 0,
                "per_cell_report": [],
            }
        monkeypatch.setattr(update_pipeline, "execute_update", fake_execute_update)

        report = await run_update_batch(
            db_path,
            tickers=["AAPL"],
            sources=["polygon_news"],
            from_date="2026-01-14",
            to_date="2026-01-14",
            fetch_fn=lambda *a, **k: None,
        )

        assert delegated["sources"] == ["polygon_news"]
        assert delegated["transport"] is not None
        assert report["run_id"] == "run-delegated"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("source", "stage", "payload", "expected_provider"),
        [
            ("polygon_news", "evidence", {"results": [{"id": "p1", "title": "P", "published_utc": "2026-01-14T00:00:00Z", "article_url": "https://x/p", "tickers": ["AAPL"], "publisher": {"name": "P"}}], "next_url": None}, "polygon"),
            ("finnhub_company_news", "evidence", {"data": [{"id": "fh1", "headline": "H"}]}, "finnhub"),
            ("fmp_fundamentals", "evidence", {"revenue": 100}, "fmp"),
            ("fred_macro", "evidence", {"observations": [{"date": "2026-01-14", "value": "1"}]}, "fred"),
            ("sec_filings", "evidence", {"filings": {"recent": {"accessionNumber": []}}}, "sec"),
            ("yfinance_ohlcv", "market", {"open": 10, "high": 11, "low": 9, "close": 10, "volume": 100}, "yfinance"),
        ],
    )
    async def test_execute_update_instruments_all_b2_provider_paths(
        self, source, stage, payload, expected_provider
    ):
        """B2 executor records request ledger/raw/provenance for each fake provider path."""
        from catalyst_data.update_planner import UpdatePlan, compute_plan_hash
        from catalyst_data.update_pipeline import execute_update
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(8)
        stages = {
            "market": {"cells": [], "count": 0},
            "evidence": {"cells": [], "count": 0, "provisional": True},
        }
        stages[stage]["cells"] = [("AAPL", "2026-01-14", source)]
        stages[stage]["count"] = 1
        plan = UpdatePlan(
            universe={"tickers": ["AAPL"]},
            reference_today="2026-01-15",
            latest_closed_session="2026-01-14",
            config={"sources": [source]},
            stages=stages,
        )
        plan.plan_hash = compute_plan_hash(plan)
        plan.expected_plan_hash = plan.plan_hash

        calls = []
        async def fake_transport(provider, method, url, **kwargs):
            calls.append((provider, kwargs["source"]))
            class FakeResponse:
                status_code = 200
                def json(self):
                    return payload
            return FakeResponse()

        await execute_update(db=db, plan=plan, transport=fake_transport)

        assert calls == [(expected_provider, source)]
        assert db.execute(
            "SELECT COUNT(*) FROM provider_request_attempts WHERE provider = ?",
            (expected_provider,),
        ).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM raw_assets WHERE data_version = 'v2'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM normalized_provenance").fetchone()[0] == 1
        db.close()

    @pytest.mark.asyncio
    async def test_execute_update_success_empty_persists_complete_checkpoint(self):
        """A valid empty Polygon response is success_empty, not failed."""
        from catalyst_data.update_planner import UpdatePlan, compute_plan_hash
        from catalyst_data.update_pipeline import execute_update
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(8)
        plan = UpdatePlan(
            universe={"tickers": ["AAPL"]},
            reference_today="2026-01-15",
            latest_closed_session="2026-01-14",
            config={"sources": ["polygon_news"]},
            stages={
                "market": {"cells": [], "count": 0},
                "evidence": {"cells": [("AAPL", "2026-01-14", "polygon_news")], "count": 1},
            },
        )
        plan.plan_hash = compute_plan_hash(plan)
        plan.expected_plan_hash = plan.plan_hash

        async def fake_transport(*args, **kwargs):
            class FakeResponse:
                status_code = 200
                def json(self):
                    return {"results": [], "next_url": None}
            return FakeResponse()

        report = await execute_update(db=db, plan=plan, transport=fake_transport)

        cp = db.execute(
            "SELECT status, is_complete, items_received FROM source_checkpoints"
        ).fetchone()
        assert report["status"] == "SUCCEEDED"
        assert cp["status"] == "success_empty"
        assert cp["is_complete"] == 1
        assert cp["items_received"] == 0
        db.close()




class TestFmpListNormalization:
    """FMP normalization: strict RED→GREEN contract for list-shaped responses.

    Seven scenarios covering every payload shape, ledger transition,
    checkpoint assertion, and idempotency guarantee.
    """

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _make_plan(db_version=11, ticker="AAPL", endpoint="income_statement",
                   date="2026-01-14", cell_suffix="1"):
        from catalyst_data.update_planner import UpdatePlan, compute_plan_hash
        plan = UpdatePlan(
            universe={"tickers": [ticker]},
            reference_today="2026-01-15",
            latest_closed_session="2026-01-14",
            config={"sources": ["fmp_fundamentals"]},
            stages={
                "market": {"cells": [], "count": 0},
                "evidence": {"cells": [{
                    "stage": "evidence",
                    "source_type": "fmp_fundamentals",
                    "endpoint_name": endpoint,
                    "subject": ticker,
                    "window_start": date,
                    "window_end": date,
                    "date_domain": "as_of",
                    "provider_profile_version": "v1",
                    "page_cap": None,
                    "item_cap": None,
                    "cell_id": "a" * 63 + cell_suffix,
                }], "count": 1},
            },
        )
        plan.plan_hash = compute_plan_hash(plan)
        plan.expected_plan_hash = plan.plan_hash
        return plan

    @staticmethod
    def _fake_transport(payload):
        async def _transport(provider, method, url, **kwargs):
            class FakeResponse:
                status_code = 200
                def json(self):
                    return payload
            return FakeResponse()
        return _transport

    # ── scenario 1: valid list → SUCCEEDED ──────────────────────────────

    @pytest.mark.asyncio
    async def test_scenario_1_valid_list_succeeded(self):
        """Valid top-level list: SUCCEEDED, is_complete=1, statements + provenance."""
        from catalyst_data.update_pipeline import execute_update
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(11)
        plan = self._make_plan()

        payload = [
            {"date": "2025-09-30", "period": "FY", "reportedCurrency": "USD",
             "revenue": 383290000000, "netIncome": 96995000000},
            {"date": "2024-09-30", "period": "FY", "reportedCurrency": "USD",
             "revenue": 391030000000, "netIncome": 93736000000},
        ]

        report = await execute_update(db=db, plan=plan,
                                       transport=self._fake_transport(payload))

        # Ledger: terminal state
        attempts = db.execute(
            "SELECT status FROM provider_request_attempts").fetchall()
        for a in attempts:
            assert a["status"] == "SUCCEEDED",                 f"Expected SUCCEEDED, got {a['status']}"

        # Checkpoint: precise assertion
        cp = db.execute(
            "SELECT status, is_complete, items_received FROM source_checkpoints"
        ).fetchone()
        assert cp["status"] == "success"
        assert cp["is_complete"] == 1
        assert cp["items_received"] == 2

        # Statements
        stmts = db.execute(
            "SELECT ticker, statement_type, fiscal_date, fiscal_period, "
            "reported_currency FROM fundamental_statements "
            "ORDER BY fiscal_date"
        ).fetchall()
        assert len(stmts) == 2
        assert stmts[0]["ticker"] == "AAPL"
        assert stmts[0]["statement_type"] == "income_statement"
        assert stmts[0]["fiscal_date"] == "2024-09-30"
        assert stmts[0]["fiscal_period"] == "FY"
        assert stmts[0]["reported_currency"] == "USD"
        assert stmts[1]["fiscal_date"] == "2025-09-30"

        # Provenance
        prov = db.execute(
            "SELECT COUNT(*) as n FROM normalized_provenance "
            "WHERE entity_type='fundamental_snapshot'"
        ).fetchone()
        assert prov["n"] == 2

        # Raw preserved
        raw = db.execute(
            "SELECT COUNT(*) as n FROM raw_assets WHERE data_version='v2'"
        ).fetchone()
        assert raw["n"] == 1

        db.close()

    # ── scenario 2: legally empty list → success_empty ───────────────────

    @pytest.mark.asyncio
    async def test_scenario_2_empty_list_success_empty(self):
        """Empty list from FMP: success_empty, is_complete=1, raw preserved,
        zero normalized rows, attempt SUCCEEDED."""
        from catalyst_data.update_pipeline import execute_update
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(11)
        plan = self._make_plan(endpoint="balance_sheet", cell_suffix="2")

        report = await execute_update(db=db, plan=plan,
                                       transport=self._fake_transport([]))

        # Attempt must be terminal
        attempts = db.execute(
            "SELECT status FROM provider_request_attempts").fetchall()
        for a in attempts:
            assert a["status"] != "STARTED"

        # Checkpoint: success_empty, is_complete=1
        cp = db.execute(
            "SELECT status, is_complete, items_received FROM source_checkpoints"
        ).fetchone()
        assert cp["status"] == "success_empty"
        assert cp["is_complete"] == 1
        assert cp["items_received"] == 0

        # Zero statement rows
        stmt_count = db.execute(
            "SELECT COUNT(*) as n FROM fundamental_statements").fetchone()["n"]
        assert stmt_count == 0

        # Raw preserved
        raw = db.execute(
            "SELECT COUNT(*) as n FROM raw_assets WHERE data_version='v2'"
        ).fetchone()
        assert raw["n"] == 1

        db.close()

    # ── scenario 3: non-list/non-dict → PARSE_ERROR ─────────────────────

    @pytest.mark.asyncio
    async def test_scenario_3_non_list_non_dict_parse_error(self):
        """String payload: PARSE_ERROR, is_complete=0, raw preserved, zero projection."""
        from catalyst_data.update_pipeline import execute_update
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(11)
        plan = self._make_plan(cell_suffix="3")

        report = await execute_update(db=db, plan=plan,
                                       transport=self._fake_transport("invalid"))

        # Attempt must be PARSE_ERROR
        attempts = db.execute(
            "SELECT status FROM provider_request_attempts").fetchall()
        for a in attempts:
            assert a["status"] == "PARSE_ERROR",                 f"Expected PARSE_ERROR, got {a['status']}"

        # Checkpoint: failed, is_complete=0
        cp = db.execute(
            "SELECT status, is_complete, items_received FROM source_checkpoints"
        ).fetchone()
        assert cp["status"] == "failed"
        assert cp["is_complete"] == 0
        assert cp["items_received"] == 0

        # Zero statement rows
        stmt_count = db.execute(
            "SELECT COUNT(*) as n FROM fundamental_statements").fetchone()["n"]
        assert stmt_count == 0

        # Zero provenance
        prov_count = db.execute(
            "SELECT COUNT(*) as n FROM normalized_provenance").fetchone()["n"]
        assert prov_count == 0

        # Raw preserved
        raw = db.execute(
            "SELECT COUNT(*) as n FROM raw_assets WHERE data_version='v2'"
        ).fetchone()
        assert raw["n"] == 1

        db.close()

    # ── scenario 4: non-empty list, all rows invalid → PARSE_ERROR ───────

    @pytest.mark.asyncio
    async def test_scenario_4_all_rows_invalid_parse_error(self):
        """Every row missing fiscal_date: PARSE_ERROR, is_complete=0,
        zero projection, raw preserved. Must NOT be success_empty."""
        from catalyst_data.update_pipeline import execute_update
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(11)
        plan = self._make_plan(endpoint="cash_flow", cell_suffix="4")

        # All rows lack date/fiscal_date — every row skipped
        payload = [
            {"period": "FY", "reportedCurrency": "USD", "value": 100},
            {"period": "Q1", "reportedCurrency": "USD", "value": 200},
        ]

        report = await execute_update(db=db, plan=plan,
                                       transport=self._fake_transport(payload))

        # Attempt must be PARSE_ERROR (not SUCCEEDED, not success_empty)
        attempts = db.execute(
            "SELECT status FROM provider_request_attempts").fetchall()
        for a in attempts:
            assert a["status"] == "PARSE_ERROR",                 f"Expected PARSE_ERROR, got {a['status']}"

        # Checkpoint: failed, is_complete=0
        cp = db.execute(
            "SELECT status, is_complete, items_received FROM source_checkpoints"
        ).fetchone()
        assert cp["status"] == "failed"
        assert cp["is_complete"] == 0

        # Zero statement rows (no partial projection)
        stmt_count = db.execute(
            "SELECT COUNT(*) as n FROM fundamental_statements").fetchone()["n"]
        assert stmt_count == 0

        # Zero provenance
        prov_count = db.execute(
            "SELECT COUNT(*) as n FROM normalized_provenance").fetchone()["n"]
        assert prov_count == 0

        # Raw preserved
        raw = db.execute(
            "SELECT COUNT(*) as n FROM raw_assets WHERE data_version='v2'"
        ).fetchone()
        assert raw["n"] == 1

        db.close()

    # ── scenario 5: mid-normalization exception → atomic rollback ────────

    @pytest.mark.asyncio
    async def test_scenario_5_mid_normalization_exception_atomic(self):
        """If normalization fails partway through rows, all statement/provenance
        projection must be atomically rolled back. Raw preserved.
        Attempt → PARSE_ERROR, checkpoint failed/is_complete=0."""
        from catalyst_data.update_pipeline import execute_update
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(11)
        plan = self._make_plan(cell_suffix="5")

        # First row valid, second row is not a dict → should fail atomically
        payload = [
            {"date": "2025-09-30", "period": "FY", "reportedCurrency": "USD",
             "revenue": 100},
            "not-a-dict",
        ]

        report = await execute_update(db=db, plan=plan,
                                       transport=self._fake_transport(payload))

        # Attempt must be PARSE_ERROR
        attempts = db.execute(
            "SELECT status FROM provider_request_attempts").fetchall()
        for a in attempts:
            assert a["status"] == "PARSE_ERROR",                 f"Expected PARSE_ERROR, got {a['status']}"

        # Checkpoint: failed/is_complete=0
        cp = db.execute(
            "SELECT status, is_complete, items_received FROM source_checkpoints"
        ).fetchone()
        assert cp["status"] == "failed"
        assert cp["is_complete"] == 0

        # Zero statement rows — atomic, no partial first row
        stmt_count = db.execute(
            "SELECT COUNT(*) as n FROM fundamental_statements").fetchone()["n"]
        assert stmt_count == 0

        # Zero provenance
        prov_count = db.execute(
            "SELECT COUNT(*) as n FROM normalized_provenance").fetchone()["n"]
        assert prov_count == 0

        # Raw preserved
        raw = db.execute(
            "SELECT COUNT(*) as n FROM raw_assets WHERE data_version='v2'"
        ).fetchone()
        assert raw["n"] == 1

        db.close()

    # ── scenario 6: true same-DB idempotent replay ───────────────────────

    @pytest.mark.asyncio
    async def test_scenario_6_true_idempotent_replay_same_db(self):
        """Execute same cell+payload twice on the SAME database.
        Second execution must not insert duplicate statement rows
        or provenance rows. Row counts must stay identical."""
        from catalyst_data.update_pipeline import execute_update
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(11)
        plan = self._make_plan(cell_suffix="6")

        payload = [
            {"date": "2025-09-30", "period": "FY", "reportedCurrency": "USD",
             "revenue": 100},
        ]
        transport = self._fake_transport(payload)

        # First execution
        await execute_update(db=db, plan=plan, transport=transport)

        stmt_ids_1 = sorted(
            r[0] for r in db.execute(
                "SELECT statement_id FROM fundamental_statements").fetchall()
        )
        prov_ids_1 = sorted(
            r[0] for r in db.execute(
                "SELECT entity_id FROM normalized_provenance "
                "WHERE entity_type='fundamental_snapshot'").fetchall()
        )
        assert len(stmt_ids_1) == 1
        assert len(prov_ids_1) == 1

        # Second execution — same DB, same plan, same payload
        await execute_update(db=db, plan=plan, transport=transport)

        stmt_ids_2 = sorted(
            r[0] for r in db.execute(
                "SELECT statement_id FROM fundamental_statements").fetchall()
        )
        prov_ids_2 = sorted(
            r[0] for r in db.execute(
                "SELECT entity_id FROM normalized_provenance "
                "WHERE entity_type='fundamental_snapshot'").fetchall()
        )

        # Statement row count must stay at 1 (INSERT OR IGNORE)
        assert len(stmt_ids_2) == 1,             f"Idempotency violation: {len(stmt_ids_2)} statement rows (was 1)"

        # Provenance rows: 2 is correct (one per raw_asset per execution)
        # Each execution creates its own raw_asset_id
        assert len(prov_ids_2) == 2,             f"Expected 2 provenance rows (one per execution), got {len(prov_ids_2)}"

        # Statement IDs must be identical (same statement_id from identity hash)
        assert stmt_ids_1 == stmt_ids_2,             f"Statement IDs changed across replay: {stmt_ids_1} → {stmt_ids_2}"
        # Provenance IDs: first-execution ids must be subset of second-execution
        assert set(prov_ids_1).issubset(set(prov_ids_2)),             f"Provenance from first execution not preserved: {prov_ids_1} not subset of {prov_ids_2}"

        db.close()

    # ── scenario 7: three-endpoint coverage ──────────────────────────────

    @pytest.mark.asyncio
    async def test_scenario_7_all_three_endpoints_normalize_correctly(self):
        """income_statement, balance_sheet, cash_flow each produce
        correctly-typed fundamental_statements rows."""
        from catalyst_data.update_pipeline import execute_update
        from conftest import _fresh_db_at_version

        endpoints = {
            "income_statement": [
                {"date": "2025-09-30", "period": "FY", "reportedCurrency": "USD",
                 "revenue": 100},
            ],
            "balance_sheet": [
                {"date": "2025-09-30", "period": "FY", "reportedCurrency": "USD",
                 "totalAssets": 1000},
            ],
            "cash_flow": [
                {"date": "2025-09-30", "period": "FY", "reportedCurrency": "USD",
                 "operatingCashFlow": 200},
            ],
        }

        ep_suffixes = {"income_statement": "a", "balance_sheet": "b", "cash_flow": "c"}
        ep_dates = {"income_statement": "2026-01-14", "balance_sheet": "2026-01-15", "cash_flow": "2026-01-16"}
        for ep, payload in endpoints.items():
            db = _fresh_db_at_version(11)
            plan = self._make_plan(
                endpoint=ep, cell_suffix=ep_suffixes[ep],
                date=ep_dates[ep],
            )

            await execute_update(db=db, plan=plan,
                                 transport=self._fake_transport(payload))

            # Each endpoint produces exactly 1 statement row
            stmts = db.execute(
                "SELECT statement_type FROM fundamental_statements").fetchall()
            assert len(stmts) == 1
            assert stmts[0]["statement_type"] == ep

            # Checkpoint is success/is_complete=1
            cp = db.execute(
                "SELECT status, is_complete FROM source_checkpoints").fetchone()
            assert cp["status"] == "success"
            assert cp["is_complete"] == 1

            db.close()

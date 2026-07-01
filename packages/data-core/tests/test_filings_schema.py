import pytest
import sqlite3
from catalyst_data.storage.sqlite import init_db, upsert_filing, upsert_filing_document


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    init_db(c)
    return c


class TestFilingsSchema:
    def test_tables_created(self, conn):
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('filings','filing_documents')"
        ).fetchall()
        names = {r[0] for r in tables}
        assert "filings" in names
        assert "filing_documents" in names

    def test_tables_idempotent(self, conn):
        init_db(conn)
        init_db(conn)
        # Should not raise

    def test_upsert_filing_insert(self, conn):
        upsert_filing(
            conn,
            filing_id="sec:0000320193:0000320193-25-000055",
            cik="0000320193", ticker="AAPL", form_type="8-K",
            filed_at="2025-05-01", accession_number="0000320193-25-000055",
            url="https://www.sec.gov/example.htm",
        )
        row = conn.execute(
            "SELECT ticker, form_type, filed_at FROM filings WHERE filing_id = ?",
            ("sec:0000320193:0000320193-25-000055",)
        ).fetchone()
        assert row is not None
        assert row[0] == "AAPL"

    def test_upsert_filing_update(self, conn):
        fid = "sec:0000320193:TEST123"
        upsert_filing(conn, filing_id=fid, cik="0000320193", ticker="AAPL",
                       form_type="8-K", filed_at="2025-01-01",
                       accession_number="TEST123", url="https://example.com")
        upsert_filing(conn, filing_id=fid, cik="0000320193", ticker="AAPL",
                       form_type="8-K", filed_at="2025-06-01",
                       accession_number="TEST123", url="https://example.com")
        row = conn.execute("SELECT filed_at FROM filings WHERE filing_id=?", (fid,)).fetchone()
        assert row[0] == "2025-06-01"

    def test_upsert_filing_document(self, conn):
        fid = "sec:0000320193:DOC_TEST"
        upsert_filing(conn, filing_id=fid, cik="0000320193", ticker="AAPL",
                       form_type="8-K", filed_at="2025-01-01",
                       accession_number="DOC_TEST", url="https://example.com")
        upsert_filing_document(conn, filing_id=fid, document_url="https://example.com/doc.htm",
                               text="Extracted text content", char_len=22,
                               content_type="text/html", byte_size=4000,
                               extraction_status="success")
        row = conn.execute(
            "SELECT text, extraction_status FROM filing_documents WHERE filing_id=?",
            (fid,)
        ).fetchone()
        assert row is not None
        assert row[0] == "Extracted text content"
        assert row[1] == "success"

    def test_extraction_status_check(self, conn):
        fid = "sec:0000320193:CHECK_TEST"
        upsert_filing(conn, filing_id=fid, cik="0000320193", ticker="AAPL",
                       form_type="8-K", filed_at="2025-01-01",
                       accession_number="CHECK_TEST", url="https://example.com")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO filing_documents(filing_id, document_url, extraction_status) VALUES(?,?,?)",
                (fid, "https://x.com", "invalid_status")
            )

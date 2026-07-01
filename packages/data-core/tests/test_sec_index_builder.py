import pytest
import sqlite3
from catalyst_data.storage.sqlite import init_db, upsert_filing, upsert_filing_document
from catalyst_data.index_builder import build_filing_records, compute_content_hash


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    init_db(c)
    return c


def _insert_8k(conn, filing_id, ticker="AAPL", filed_at="2025-05-01", items_json='["2.02","9.01"]',
               doc_text=None, extraction_status=None):
    upsert_filing(conn, filing_id=filing_id, cik="0000320193", ticker=ticker,
                  form_type="8-K", filed_at=filed_at, accession_number=filing_id.split(":")[-1],
                  url="https://example.com", items_json=items_json, is_rag_eligible=1)
    if doc_text and extraction_status:
        upsert_filing_document(conn, filing_id=filing_id, document_url=f"https://example.com/{filing_id}.htm",
                               text=doc_text, char_len=len(doc_text), content_type="text/html",
                               byte_size=4000, extraction_status=extraction_status)


def _insert_10q(conn, filing_id, ticker="AAPL", filed_at="2025-05-01"):
    upsert_filing(conn, filing_id=filing_id, cik="0000320193", ticker=ticker,
                  form_type="10-Q", filed_at=filed_at, accession_number=filing_id.split(":")[-1],
                  url="https://example.com", items_json="[]", is_rag_eligible=1)


class TestBuildFilingRecords:
    def test_l1_per_filing(self, conn):
        _insert_8k(conn, "sec:0000320193:8K001", doc_text="Earnings release with revenue numbers.", extraction_status="success")
        records = build_filing_records(conn)
        l1 = [r for r in records if r["chunk_level"] == "l1"]
        assert len(l1) == 1
        assert l1[0]["source_kind"] == "filing"
        assert l1[0]["corpus_item_id"] == "sec:0000320193:8K001"

    def test_metadata_only_filing_still_gets_l1(self, conn):
        _insert_10q(conn, "sec:0000320193:10Q001")
        records = build_filing_records(conn)
        l1 = [r for r in records if r["chunk_level"] == "l1"]
        assert len(l1) == 1
        # content_hash from title only (empty body)
        assert "10-Q filed" in l1[0]["content_text"]

    def test_l2_from_long_body(self, conn):
        long_body = "Sentence one. " * 200  # ~2800 chars, well over 800
        _insert_8k(conn, "sec:0000320193:LONG8K", doc_text=long_body, extraction_status="success")
        records = build_filing_records(conn, min_l2_chars=800)
        l2 = [r for r in records if r["chunk_level"] == "l2"]
        assert len(l2) > 0

    def test_no_l2_below_threshold(self, conn):
        short_body = "Short summary."
        _insert_8k(conn, "sec:0000320193:SHORT", doc_text=short_body, extraction_status="success")
        records = build_filing_records(conn, min_l2_chars=800)
        l2 = [r for r in records if r["chunk_level"] == "l2"]
        assert len(l2) == 0

    def test_filing_id_format(self, conn):
        _insert_8k(conn, "sec:0000320193:0000320193-25-000055", doc_text="Test content.", extraction_status="success")
        records = build_filing_records(conn)
        assert records[0]["corpus_item_id"] == "sec:0000320193:0000320193-25-000055"

    def test_guard_l1_count_equals_rag_eligible(self, conn):
        _insert_8k(conn, "sec:0000320193:F1", doc_text="Content.", extraction_status="success")
        _insert_10q(conn, "sec:0000320193:F2")
        _insert_8k(conn, "sec:0000320193:F3", doc_text="More.", extraction_status="success")
        records = build_filing_records(conn)
        l1 = [r for r in records if r["chunk_level"] == "l1"]
        rag_count = conn.execute("SELECT COUNT(*) FROM filings WHERE is_rag_eligible=1").fetchone()[0]
        assert len(l1) == rag_count

    def test_failed_extraction_filing_still_l1(self, conn):
        _insert_8k(conn, "sec:0000320193:FAIL", doc_text=None, extraction_status=None)  # no doc row
        records = build_filing_records(conn)
        l1 = [r for r in records if r["chunk_level"] == "l1"]
        assert len(l1) == 1
        # content_hash from title only
        assert l1[0]["source_kind"] == "filing"


class TestExhibitPreferenceDiscriminating:
    """Real-fixture test: a rag-eligible 8-K with both cover + EX-99 docs
    must produce exactly ONE L1 with EX-99 content, NOT cover boilerplate."""

    def test_exhibit_chosen_over_cover(self, conn):
        """Build records from real fixtures via mock fetcher flow.
        Assert: L1 contains earnings tokens, NOT cover boilerplate, exactly 1 L1."""
        import json, asyncio
        from pathlib import Path
        from types import SimpleNamespace
        from catalyst_data.pipeline.sec_normalize import normalize_submissions, resolve_filing_documents
        from catalyst_data.index_builder import build_filing_records
        from catalyst_data.storage.sqlite import upsert_filing, upsert_filing_document

        FIXTURES = Path(__file__).parent / "fixtures"
        with open(FIXTURES / "sec_submissions_AAPL.json") as f:
            submissions = json.load(f)

        # Normalize one 8-K with Item 2.02
        filings = normalize_submissions(submissions, "AAPL", "0000320193", "2026-04-01", "2026-05-31")
        earning_8k = [f for f in filings if f["form_type"] == "8-K" and "2.02" in f.get("items_json", "[]")]
        assert earning_8k, "No Item 2.02 8-K in fixture"
        filing_dict = earning_8k[0]

        # Store the filing
        upsert_filing(conn,
            filing_id=filing_dict["filing_id"], cik=filing_dict["cik"],
            ticker=filing_dict["ticker"], form_type=filing_dict["form_type"],
            filed_at=filing_dict["filed_at"], accession_number=filing_dict["accession_number"],
            url=filing_dict["url"], items_json=filing_dict["items_json"],
            is_rag_eligible=filing_dict["is_rag_eligible"])

        # Mock fetcher: maps URL → fixture bytes
        wrapper_bytes = (FIXTURES / "sec_8k_primary_wrapper.htm").read_bytes()
        exhibit_bytes = (FIXTURES / "sec_8k_exhibit_99_1.htm").read_bytes()
        index_bytes = (FIXTURES / "sec_8k_index_headers.htm").read_bytes()

        async def mock_fetch_document(url):
            if "index-headers" in url:
                content = index_bytes
            elif "ex99" in url.lower() or "a8-kex991" in url or "ex991" in url:
                content = exhibit_bytes
            else:
                content = wrapper_bytes
            # Use the real connector's text extractor
            from catalyst_data.pipeline.sec_normalize import extract_text_from_html
            text = extract_text_from_html(content.decode("latin-1", errors="replace"))
            return SimpleNamespace(
                status=200,
                data={"url": url, "text": text, "content_type": "text/html",
                      "byte_size": len(content), "extraction_status": "success" if text else "empty"}
            )

        mock_fetcher = SimpleNamespace(fetch_document=mock_fetch_document)

        # Resolve documents
        docs = asyncio.run(resolve_filing_documents(mock_fetcher, filing_dict))
        assert len(docs) >= 2, f"Expected at least 2 docs (cover + exhibit), got {len(docs)}"

        # Store both documents
        for d in docs:
            upsert_filing_document(conn,
                filing_id=d["filing_id"], document_url=d["document_url"],
                document_type=d.get("document_type", "primary_doc"),
                text=d.get("text"), char_len=d.get("char_len"),
                content_type=d.get("content_type"), byte_size=d.get("byte_size"),
                extraction_status=d.get("extraction_status"))

        doc_count = conn.execute("SELECT COUNT(*) FROM filing_documents").fetchone()[0]
        assert doc_count >= 2, f"Expected 2+ filing_documents rows, got {doc_count}"

        # Build records
        records = build_filing_records(conn, min_l2_chars=800)
        l1 = [r for r in records if r["chunk_level"] == "l1"]
        l2 = [r for r in records if r["chunk_level"] == "l2"]

        # (a) Exactly ONE L1
        assert len(l1) == 1, f"Expected exactly 1 L1, got {len(l1)}"

        l1_text = l1[0]["content_text"]

        # (b) Contains earnings tokens from real EX-99.1
        has_earnings = any(tok in l1_text.lower() for tok in [
            'net sales', 'earnings per share', 'revenue', 'diluted', 'apple reports',
            'services revenue', 'operating income'
        ])
        assert has_earnings, (
            f"L1 content does NOT contain earnings tokens. "
            f"Got (first 500 chars): {l1_text[:500]}"
        )

        # (c) Does NOT contain cover boilerplate
        boilerplate_tokens = ['check the appropriate box', 'securities and exchange commission',
                              'united states securities']
        has_boilerplate = any(tok in l1_text.lower() for tok in boilerplate_tokens)
        assert not has_boilerplate, (
            f"L1 content contains cover boilerplate — exhibit was NOT preferred. "
            f"Got: {l1_text[:300]}"
        )

        # (d) Guard: L1 count matches rag-eligible filings
        rag_count = conn.execute(
            "SELECT COUNT(*) FROM filings WHERE is_rag_eligible=1"
        ).fetchone()[0]
        assert len(l1) == rag_count

        # L2 should exist because EX-99.1 body is long
        assert len(l2) > 0, "Expected L2 records from long exhibit body"

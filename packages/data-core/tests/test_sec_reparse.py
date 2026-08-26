"""M3-4: SEC document reparse tests (execution-lock §A.2/§B/§C).

``reparse_filing`` yields primary-document non-empty extraction, section keys +
ordinals, per-section degradation flags, a document hash, parse_quality, and a
versioned parser identity. ``section_parse_degraded`` is a separately reported
quality flag and never folds into the DATA-01 primary-document gate.
"""
from __future__ import annotations

import hashlib

from catalyst_data.sec.extract import SEC_EXTRACT_PARSER_VERSION, extract_document_text
from catalyst_data.sec.reparse import FilingParseResult, ReparsedSection, reparse_filing
from catalyst_data.corpus.filing_v3 import FilingV3Profile


def _html_8k() -> bytes:
    body = (
        "Item 1.01 Entry into a Material Definitive Agreement.\n"
        "On January 5, 2026, the registrant entered into a material definitive "
        "agreement with a third party. The agreement governs the provision of "
        "services over a multi-year term and includes customary representations, "
        "warranties, and covenants. "
        + ("Additional substantive disclosure follows. " * 8)
        + "\nItem 2.02 Results of Operations and Financial Condition.\n"
        "The registrant announced financial results for the quarter. Revenue "
        "increased year over year and operating expenses remained in line with "
        "guidance. "
        + ("Management commentary on results follows. " * 8)
    )
    return ("<html><body>" + body + "</body></html>").encode("utf-8")


def _html_10k() -> bytes:
    body = (
        "Part I\nItem 1. Business.\nThe registrant designs, manufactures, and "
        "markets products and services worldwide. The company competes in "
        "multiple segments and faces competitive pressures. "
        + ("Business description follows. " * 8)
        + "\nItem 1A. Risk Factors.\nInvesting in the registrant involves risk. "
        "The company faces operational, financial, and regulatory risks that "
        "could materially affect results. "
        + ("Risk factor discussion follows. " * 8)
    )
    return ("<html><body>" + body + "</body></html>").encode("utf-8")


def _html_no_items() -> bytes:
    body = (
        "This filing contains a lengthy narrative without any itemized section "
        "structure that the section parser can recognize. "
        + ("Narrative text continues. " * 15)
    )
    return ("<html><body>" + body + "</body></html>").encode("utf-8")


def test_reparse_8k_nonempty_primary_and_sections():
    result = reparse_filing(_html_8k(), accession="0000320193-26-000001")
    assert isinstance(result, FilingParseResult)
    assert result.accession == "0000320193-26-000001"
    assert result.primary_document_extracted is True
    assert result.document_hash is not None
    assert len(result.document_hash) == 64
    assert result.document_hash.islower()
    assert result.parse_quality == "full"
    keys = {s.section_key for s in result.sections}
    assert any(k.startswith("item_1.01") for k in keys)
    assert any(k.startswith("item_2.02") for k in keys)
    assert all(isinstance(s, ReparsedSection) for s in result.sections)
    assert all(len(s.ordinal) == 4 and s.ordinal.isdigit() for s in result.sections)
    assert all(not s.section_parse_degraded for s in result.sections)


def test_reparse_10k_sections():
    result = reparse_filing(_html_10k(), accession="0000320193-26-000001")
    assert result.primary_document_extracted is True
    assert result.parse_quality == "full"
    keys = {s.section_key for s in result.sections}
    assert any(k.startswith("item_1") for k in keys)
    assert any(k.startswith("item_1a") for k in keys)


def test_reparse_deterministic_same_input_same_output():
    first = reparse_filing(_html_8k(), accession="0000320193-26-000001")
    second = reparse_filing(_html_8k(), accession="0000320193-26-000001")
    assert first.document_hash == second.document_hash
    assert first.sections == second.sections
    assert first.parse_quality == second.parse_quality
    # Byte-identical serialized output for the same input + parser version.
    assert _serialize(first) == _serialize(second)


def test_reparse_different_parser_version_recorded():
    first = reparse_filing(_html_8k(), accession="0000320193-26-000001")
    second = reparse_filing(
        _html_8k(), accession="0000320193-26-000001", parser_version="sec_extract_v2"
    )
    assert first.parser_version == "sec_extract_v1"
    assert second.parser_version == "sec_extract_v2"


def test_reparse_degraded_sections_flagged_without_failing_primary():
    result = reparse_filing(_html_no_items(), accession="0000320193-26-000001")
    assert result.primary_document_extracted is True
    assert result.document_hash is not None
    assert result.parse_quality == "degraded"
    assert len(result.sections) >= 1
    assert all(s.section_parse_degraded for s in result.sections)
    assert result.sections[0].section_key == "unknown_000"


def test_reparse_empty_primary_not_extracted():
    result = reparse_filing(b"   \n\t  ", accession="0000320193-26-000001")
    assert result.primary_document_extracted is False
    assert result.document_hash is None
    assert result.parse_quality == "not_applicable"
    assert result.sections == ()


def test_reparse_pdf_primary_not_extracted_not_applicable():
    result = reparse_filing(b"%PDF-1.4 fake", accession="0000320193-26-000001")
    assert result.primary_document_extracted is False
    assert result.document_hash is None
    assert result.parse_quality == "not_applicable"


def test_extract_records_versioned_parser_identity():
    outcome = extract_document_text(
        _html_8k(), content_type="text/html", is_primary=True
    )
    assert outcome.status == "success"
    assert outcome.parser_version == SEC_EXTRACT_PARSER_VERSION == "sec_extract_v1"


def test_filing_v3_profile_consumes_reparse_degradation_override():
    text = "Item 1.01 Entry\n\nBody one\n"
    chunks = FilingV3Profile().chunk(
        {
            "document_id": "d1",
            "filing_type": "8-K",
            "raw_text": text,
            "available_at": "2025-08-01T00:00:00Z",
            "ticker_associations": '["AAPL"]',
            "source_class": "official_government",
            "eligibility": "eligible",
            "section_parse_degraded": True,  # reparse result consumed by the profile
        }
    )
    assert chunks
    assert all(c.section_parse_degraded for c in chunks)


def _serialize(result: FilingParseResult) -> tuple:
    return (
        result.accession,
        result.parser_version,
        result.primary_document_extracted,
        result.document_hash,
        result.parse_quality,
        tuple((s.section_key, s.ordinal, s.section_parse_degraded) for s in result.sections),
    )


# ---------------------------------------------------------------------------
# Batch B: repository-owned reparse persistence (B1-B3)
# ---------------------------------------------------------------------------


def _reparse_db(*, with_primary: bool = True) -> tuple[sqlite3.Connection, str, str]:
    """In-memory DB with one filing + one primary document row."""
    import sqlite3

    from catalyst_data.storage.sqlite import init_db, upsert_filing

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    upsert_filing(
        conn,
        filing_id="filing-8k-0000320193-26-000001",
        cik="0000320193",
        ticker="AAPL",
        form_type="8-K",
        filed_at="2026-01-05",
        accession_number="0000320193-26-000001",
        primary_document="a.htm",
        url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/a.htm",
    )
    document_id = "a" * 64
    if with_primary:
        conn.execute(
            """INSERT INTO filing_documents (
                   filing_id, document_url, document_type, text, char_len,
                   content_type, byte_size, extraction_status, extracted_at,
                   document_id
               ) VALUES (?, ?, 'primary_doc', NULL, NULL, 'text/html', NULL,
                         'fetch_failed', '2026-01-05T20:00:00Z', ?)""",
            ("filing-8k-0000320193-26-000001",
             "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/a.htm",
             document_id),
        )
        conn.commit()
    return conn, "filing-8k-0000320193-26-000001", document_id


def _success_result() -> FilingParseResult:
    return reparse_filing(_html_8k(), accession="0000320193-26-000001")


def test_persist_filing_document_reparse_writes_columns_and_text():
    from catalyst_data.sec.reparse import persist_filing_document_reparse

    conn, filing_id, document_id = _reparse_db()
    result = _success_result()
    persist_filing_document_reparse(
        conn,
        filing_id=filing_id,
        document_id=document_id,
        result=result,
        extracted_text=_normalize_reparse_text(_html_8k()),
    )
    row = conn.execute(
        "SELECT * FROM filing_documents WHERE filing_id=? AND document_id=?",
        (filing_id, document_id),
    ).fetchone()
    assert row["parser_version"] == SEC_EXTRACT_PARSER_VERSION
    assert row["document_hash"] == result.document_hash
    assert len(row["document_hash"]) == 64
    assert row["document_hash"].islower()
    assert row["parse_quality"] == result.parse_quality
    assert row["section_parse_degraded"] in (0, 1)
    assert row["extraction_status"] == "success"
    assert row["text"] is not None
    import hashlib as _hl

    from catalyst_data.corpus.news_v2 import _normalize_text

    assert _hl.sha256(_normalize_text(row["text"]).encode("utf-8")).hexdigest() == row["document_hash"]
    conn.close()


def _normalize_reparse_text(raw: bytes) -> str:
    from catalyst_data.sec.extract import extract_document_text
    from catalyst_data.corpus.news_v2 import _normalize_text

    outcome = extract_document_text(
        raw, content_type="text/html", is_primary=True,
        parser_version=SEC_EXTRACT_PARSER_VERSION,
    )
    return _normalize_text(outcome.text)


def test_persist_filing_document_reparse_idempotent():
    from catalyst_data.sec.reparse import persist_filing_document_reparse

    conn, filing_id, document_id = _reparse_db()
    result = _success_result()
    text = _normalize_reparse_text(_html_8k())
    persist_filing_document_reparse(
        conn, filing_id=filing_id, document_id=document_id,
        result=result, extracted_text=text,
    )
    first = conn.execute(
        "SELECT parser_version, document_hash, parse_quality, text FROM filing_documents"
    ).fetchone()
    persist_filing_document_reparse(
        conn, filing_id=filing_id, document_id=document_id,
        result=result, extracted_text=text,
    )
    second = conn.execute(
        "SELECT parser_version, document_hash, parse_quality, text FROM filing_documents"
    ).fetchone()
    assert tuple(first) == tuple(second)
    conn.close()


def test_persist_filing_document_reparse_unknown_ids_fail_closed():
    import pytest

    from catalyst_data.sec.reparse import persist_filing_document_reparse

    conn, filing_id, document_id = _reparse_db()
    result = _success_result()
    text = _normalize_reparse_text(_html_8k())
    with pytest.raises(ValueError, match="filing"):
        persist_filing_document_reparse(
            conn, filing_id="filing-missing", document_id=document_id,
            result=result, extracted_text=text,
        )
    with pytest.raises(ValueError, match="document"):
        persist_filing_document_reparse(
            conn, filing_id=filing_id, document_id="f" * 64,
            result=result, extracted_text=text,
        )
    assert conn.execute("SELECT COUNT(*) FROM filing_documents WHERE parser_version IS NOT NULL").fetchone()[0] == 0
    conn.close()


def test_persist_filing_document_reparse_accession_mismatch_fails_closed():
    import pytest

    from catalyst_data.sec.reparse import persist_filing_document_reparse

    conn, filing_id, document_id = _reparse_db()
    result = _success_result()
    # result.accession does not match parent filings.accession_number.
    wrong = FilingParseResult(
        accession="0000320193-26-999999",
        parser_version=result.parser_version,
        primary_document_extracted=result.primary_document_extracted,
        document_hash=result.document_hash,
        parse_quality=result.parse_quality,
        sections=result.sections,
    )
    with pytest.raises(ValueError, match="accession"):
        persist_filing_document_reparse(
            conn, filing_id=filing_id, document_id=document_id,
            result=wrong, extracted_text=_normalize_reparse_text(_html_8k()),
        )
    assert conn.execute("SELECT COUNT(*) FROM filing_documents WHERE parser_version IS NOT NULL").fetchone()[0] == 0
    conn.close()


def test_persist_filing_document_reparse_bad_document_hash_fails_closed():
    import pytest

    from catalyst_data.sec.reparse import persist_filing_document_reparse

    conn, filing_id, document_id = _reparse_db()
    result = _success_result()
    bad_hash = FilingParseResult(
        accession=result.accession,
        parser_version=result.parser_version,
        primary_document_extracted=True,
        document_hash="NOTHEX",
        parse_quality="full",
        sections=result.sections,
    )
    with pytest.raises(ValueError, match="document_hash"):
        persist_filing_document_reparse(
            conn, filing_id=filing_id, document_id=document_id,
            result=bad_hash, extracted_text=_normalize_reparse_text(_html_8k()),
        )
    assert conn.execute("SELECT COUNT(*) FROM filing_documents WHERE parser_version IS NOT NULL").fetchone()[0] == 0
    conn.close()


def test_persist_filing_document_reparse_extracted_requires_text_fails_closed():
    import pytest

    from catalyst_data.sec.reparse import persist_filing_document_reparse

    conn, filing_id, document_id = _reparse_db()
    result = _success_result()
    with pytest.raises(ValueError, match="extracted_text"):
        persist_filing_document_reparse(
            conn, filing_id=filing_id, document_id=document_id,
            result=result, extracted_text="   \n\t  ",
        )
    assert conn.execute("SELECT COUNT(*) FROM filing_documents WHERE parser_version IS NOT NULL").fetchone()[0] == 0
    conn.close()


def test_persist_filing_document_reparse_failed_result_metadata_only():
    from catalyst_data.sec.reparse import persist_filing_document_reparse

    conn, filing_id, document_id = _reparse_db()
    result = reparse_filing(b"   \n\t  ", accession="0000320193-26-000001")
    assert result.primary_document_extracted is False
    persist_filing_document_reparse(
        conn, filing_id=filing_id, document_id=document_id,
        result=result, extracted_text=None,
    )
    row = conn.execute(
        "SELECT * FROM filing_documents WHERE filing_id=? AND document_id=?",
        (filing_id, document_id),
    ).fetchone()
    assert row["parser_version"] == SEC_EXTRACT_PARSER_VERSION
    assert row["document_hash"] is None
    assert row["parse_quality"] == result.parse_quality
    assert row["section_parse_degraded"] is None
    # Text is not overwritten for a failed reparse.
    assert row["text"] is None
    assert row["extraction_status"] == "fetch_failed"
    conn.close()

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

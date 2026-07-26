"""Tests for filing_v2 chunk profile — section-aware 8-K / EX-99.x chunking."""
from __future__ import annotations


def test_filing_v2_section_boundaries_respected():
    """Chunks never cross section boundaries in an 8-K."""
    from catalyst_data.corpus.filing_v2 import FilingV2Profile

    profile = FilingV2Profile()
    document = {
        "document_id": "sec:0000320193-26-000001:8-K",
        "filing_type": "8-K",
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
        "sections": [
            {
                "section_key": "item_1.01",
                "heading": "Item 1.01 Entry into a Material Definitive Agreement",
                "text": "On January 14, 2026, the Company entered into an agreement with Partner Corp.",
            },
            {
                "section_key": "item_9.01",
                "heading": "Item 9.01 Financial Statements and Exhibits",
                "text": "Exhibits are filed herewith.",
            },
        ],
    }
    chunks = profile.chunk(document)
    # Each section should produce at least one chunk
    section_keys = {c.section_key for c in chunks}
    assert "item_1.01" in section_keys
    assert "item_9.01" in section_keys
    # No chunk should mix text from both sections
    for c in chunks:
        if c.section_key == "item_1.01":
            assert "agreement" in c.content_text.lower()
        elif c.section_key == "item_9.01":
            assert "exhibits" in c.content_text.lower()


def test_filing_v2_duplicate_items_suffixed():
    """Duplicate item headings get _02, _03 suffixes."""
    from catalyst_data.corpus.filing_v2 import FilingV2Profile

    profile = FilingV2Profile()
    document = {
        "document_id": "sec:dup:8-K",
        "filing_type": "8-K",
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
        "sections": [
            {
                "section_key": "item_1.01",
                "heading": "Item 1.01 First Agreement",
                "text": "First agreement details.",
            },
            {
                "section_key": "item_1.01",
                "heading": "Item 1.01 Second Agreement",
                "text": "Second agreement details.",
            },
        ],
    }
    chunks = profile.chunk(document)
    section_keys = {c.section_key for c in chunks}
    assert "item_1.01_02" in section_keys or len(section_keys) == 2


def test_filing_v2_exhibit():
    """EX-99.1 exhibit uses section key ex_99_1."""
    from catalyst_data.corpus.filing_v2 import FilingV2Profile

    profile = FilingV2Profile()
    document = {
        "document_id": "sec:0000320193-26-000002:EX-99.1",
        "filing_type": "EX-99.1",
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
        "sections": [
            {
                "section_key": "ex_99_1",
                "heading": "Press Release",
                "text": "Apple Reports First Quarter Results. Revenue reached $94.8 billion.",
            },
        ],
    }
    chunks = profile.chunk(document)
    assert len(chunks) >= 1
    for c in chunks:
        assert c.section_key == "ex_99_1"


def test_filing_v2_parse_failure_unknown():
    """Parse failure emits bounded unknown_000 chunks with degraded flag."""
    from catalyst_data.corpus.filing_v2 import FilingV2Profile

    profile = FilingV2Profile()
    document = {
        "document_id": "sec:badparse:8-K",
        "filing_type": "8-K",
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
        # No sections — parse failure
        "sections": [],
        "raw_text": "Some raw 8-K text without recognizable headings.",
    }
    chunks = profile.chunk(document)
    assert len(chunks) >= 1
    for c in chunks:
        assert c.section_key == "unknown_000"
        assert c.section_parse_degraded == 1


def test_filing_v2_no_chunk_exceeds_max():
    """No filing_v2 chunk exceeds 384 tokens."""
    from catalyst_data.corpus.filing_v2 import FilingV2Profile
    from catalyst_data.corpus.tokenizer import count_tokens

    profile = FilingV2Profile()
    document = {
        "document_id": "sec:long:8-K",
        "filing_type": "8-K",
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
        "sections": [
            {
                "section_key": "item_1.01",
                "heading": "Item 1.01 Entry into Agreement",
                "text": (
                    "This is a very long section. " * 50 +
                    "Additional text to make it even longer. " * 30 +
                    "More content needed for multiple chunks. " * 20
                ),
            },
        ],
    }
    chunks = profile.chunk(document)
    for c in chunks:
        tokens = count_tokens(c.content_text)
        assert tokens <= 384, f"Chunk {c.ordinal} in {c.section_key} has {tokens} tokens"


def test_filing_v2_empty_section_no_chunks():
    """Empty section emits zero chunks."""
    from catalyst_data.corpus.filing_v2 import FilingV2Profile

    profile = FilingV2Profile()
    document = {
        "document_id": "sec:empty:8-K",
        "filing_type": "8-K",
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
        "sections": [
            {
                "section_key": "item_1.01",
                "heading": "Item 1.01 Empty",
                "text": "",
            },
        ],
    }
    chunks = profile.chunk(document)
    # Empty section should produce zero chunks
    assert all(c.section_key != "item_1.01" or len(chunks) == 0
               for c in chunks if c.section_key == "item_1.01")


def test_filing_v2_parses_raw_8k_item_headings():
    """Raw 8-K text is split at supported item headings before windowing."""
    from catalyst_data.corpus.filing_v2 import FilingV2Profile

    chunks = FilingV2Profile().chunk({
        "document_id": "sec:raw-items:8-K",
        "filing_type": "8-K",
        "raw_text": (
            "Cover page\n"
            "Item 1.01 Entry into a Material Agreement\nFirst section body.\n"
            "ITEM 9.01 Financial Statements and Exhibits\nSecond section body."
        ),
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
    })

    assert [chunk.section_key for chunk in chunks] == [
        "unknown_000", "item_1.01", "item_9.01",
    ]
    assert all(chunk.section_parse_degraded == 0 for chunk in chunks)
    assert "Second section body" not in chunks[1].content_text


def test_filing_v2_hashes_metadata_with_filing_profile_version():
    """filing_v2 metadata identity must not inherit news_v2 from its splitter."""
    import hashlib
    import json

    from catalyst_data.corpus.filing_v2 import FilingV2Profile

    document = {
        "document_id": "sec:metadata:8-K",
        "filing_type": "8-K",
        "sections": [{
            "section_key": "item_1.01",
            "heading": "Item 1.01",
            "text": "Agreement details.",
        }],
        "available_at": "2026-01-15T14:00:00Z",
        "ticker_associations": '["AAPL"]',
        "source_class": "official_government",
        "eligibility": "eligible",
    }
    chunk = FilingV2Profile().chunk(document)[0]
    expected = hashlib.sha256(json.dumps({
        "document_id": document["document_id"],
        "ticker_associations": document["ticker_associations"],
        "available_at": document["available_at"],
        "source_class": document["source_class"],
        "dedup_cluster_id": None,
        "representative_document_id": None,
        "eligibility": "eligible",
        "chunk_profile_version": "filing_v2",
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert chunk.metadata_hash == expected


def test_filing_v2_accepts_canonical_section_title_field():
    """Canonical ordered sections use their title as the audited heading prefix."""
    from catalyst_data.corpus.filing_v2 import FilingV2Profile

    chunk = FilingV2Profile().chunk({
        "document_id": "sec:title:8-K",
        "sections": [{
            "section_key": "item_2.03",
            "title": "Item 2.03 Creation of a Direct Financial Obligation",
            "text": "The registrant entered into a credit facility.",
        }],
        "available_at": "2026-01-15T14:00:00Z",
    })[0]
    assert chunk.content_text.startswith("Item 2.03 Creation")


def test_filing_v2_matches_duplicate_and_exhibit_fixtures():
    from catalyst_data.corpus.filing_v2 import FilingV2Profile
    from corpus_fixtures import (
        EXPECTED_EXHIBIT_BOUNDARY_KINDS,
        exhibit_99_1_fixture,
        raw_8k_duplicate_item_fixture,
        stable_unique,
    )

    duplicate_chunks = FilingV2Profile().chunk(raw_8k_duplicate_item_fixture())
    assert stable_unique([chunk.section_key for chunk in duplicate_chunks]) == [
        "unknown_000", "item_1.01", "item_1.01_02", "item_2.03",
    ]
    exhibit_chunks = FilingV2Profile().chunk(exhibit_99_1_fixture())
    assert {chunk.section_key for chunk in exhibit_chunks} == {"ex_99_1"}
    assert [chunk.boundary_kind for chunk in exhibit_chunks] == EXPECTED_EXHIBIT_BOUNDARY_KINDS

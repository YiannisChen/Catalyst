"""filing_v3 section oracles and token constants."""

from catalyst_data.corpus.filing_v3 import FilingV3Profile, MAX_OVERLAP, MAX_TOKENS, TARGET_TOKENS, MAX_PREFIX_TOKENS


def test_index_builder_filing_v3_uses_persisted_document_id(tmp_path):
    """B3→Pre-B6: seeded filing_document(document_id) → filing_v3 chunks."""
    import json
    import sqlite3
    from pathlib import Path
    from catalyst_data.storage.sqlite import init_db
    from catalyst_data.migrations import run_migrations
    from catalyst_data.index_builder import build_corpus

    conn = sqlite3.connect(tmp_path / "fv3.db")
    init_db(conn)
    run_migrations(conn)
    doc_id = "a" * 64
    long_text = "ITEM 2.02 Results of Operations\n\n" + ("Revenue growth earnings guidance. " * 40)
    conn.execute(
        """INSERT INTO filings
           (filing_id, cik, ticker, form_type, filed_at, accession_number, url,
            is_canonical, is_rag_eligible)
           VALUES ('sec:f1','1','AAPL','8-K','2025-08-15T12:00:00Z','acc','http://x',1,1)"""
    )
    conn.execute(
        """INSERT INTO filing_documents
           (filing_id, document_url, document_type, text, extraction_status, document_id)
           VALUES ('sec:f1','http://x/a.htm','primary_doc',?,'success',?)""",
        (long_text, doc_id),
    )
    conn.commit()
    result = build_corpus(conn, certified_snapshot_identity="snap" * 16)
    assert result.manifest_id
    # persisted document_id on chunks
    rows = conn.execute(
        "SELECT document_id, chunk_profile_version, available_at FROM corpus_chunks "
        "WHERE document_id=?",
        (doc_id,),
    ).fetchall()
    assert len(rows) >= 1
    assert all(r[0] == doc_id for r in rows)
    assert all(r[1] == "filing_v3" for r in rows)
    assert all(str(r[2]).endswith("Z") for r in rows)
    man = conn.execute(
        "SELECT manifest_json FROM corpus_manifest WHERE manifest_id=?",
        (result.manifest_id,),
    ).fetchone()
    assert man is not None
    mj = json.loads(man[0])
    assert mj.get("chunk_profile_versions", {}).get("filing") == "filing_v3"
    conn.close()
from catalyst_data.corpus import news_v2


def test_token_constants_384_320_48_64():
    assert MAX_TOKENS == 384
    assert TARGET_TOKENS == 320
    assert MAX_OVERLAP == 48
    assert MAX_PREFIX_TOKENS == 64
    assert news_v2.MAX_TOKENS == 384


def test_8k_item_section_keys_oracle():
    text = "Intro\n\nItem 1.01 Entry\nBody one\n\nItem 2.02 Results\nBody two\n"
    chunks = FilingV3Profile().chunk(
        {
            "document_id": "d1",
            "filing_type": "8-K",
            "raw_text": text,
            "available_at": "2025-08-01T00:00:00Z",
            "ticker_associations": '["AAPL"]',
            "source_class": "official_government",
            "eligibility": "eligible",
        }
    )
    keys = {c.section_key for c in chunks}
    assert any(k.startswith("item_1.01") for k in keys)
    assert any(k.startswith("item_2.02") for k in keys)


def test_ex99_role_is_section_root():
    chunks = FilingV3Profile().chunk(
        {
            "document_id": "d1",
            "document_role": "exhibit_99_1",
            "filing_type": "EX-99.1",
            "raw_text": "Press release content " * 20,
            "available_at": "2025-08-01T00:00:00Z",
            "ticker_associations": '["AAPL"]',
            "source_class": "official_government",
            "eligibility": "eligible",
        }
    )
    assert chunks
    assert all("exhibit_99" in c.section_key for c in chunks)


def test_6k_role_bounded():
    chunks = FilingV3Profile().chunk(
        {
            "document_id": "d1",
            "filing_type": "6-K",
            "raw_text": "Item 1 Disclosure\nHello world content " * 10,
            "available_at": "2025-08-01T00:00:00Z",
            "ticker_associations": '["ASML"]',
            "source_class": "official_government",
            "eligibility": "eligible",
        }
    )
    assert chunks


def test_10q_part_item_oracle():
    text = "Item 1 Financial\nNumbers\n\nItem 2 MD&A\nAnalysis text\n"
    chunks = FilingV3Profile().chunk(
        {
            "document_id": "d1",
            "filing_type": "10-Q",
            "raw_text": text,
            "available_at": "2025-08-01T00:00:00Z",
            "ticker_associations": '["AAPL"]',
            "source_class": "official_government",
            "eligibility": "eligible",
        }
    )
    keys = {c.section_key for c in chunks}
    assert any("item_1" in k for k in keys)


def test_10k_part_item_oracle():
    text = "Item 1 Business\nBiz\n\nItem 1A Risk\nRisks\n"
    chunks = FilingV3Profile().chunk(
        {
            "document_id": "d1",
            "filing_type": "10-K",
            "raw_text": text,
            "available_at": "2025-08-01T00:00:00Z",
            "ticker_associations": '["AAPL"]',
            "source_class": "official_government",
            "eligibility": "eligible",
        }
    )
    assert chunks


def test_20f_item_1_to_19_oracle():
    text = "Item 1 Identity\nX\n\nItem 3 Key Info\nY\n"
    chunks = FilingV3Profile().chunk(
        {
            "document_id": "d1",
            "filing_type": "20-F",
            "raw_text": text,
            "available_at": "2025-08-01T00:00:00Z",
            "ticker_associations": '["TSM"]',
            "source_class": "official_government",
            "eligibility": "eligible",
        }
    )
    assert chunks


def test_parse_failure_unknown_000_degraded_bounded():
    chunks = FilingV3Profile().chunk(
        {
            "document_id": "d1",
            "filing_type": "8-K",
            "raw_text": "No item headings just prose " * 30,
            "available_at": "2025-08-01T00:00:00Z",
            "ticker_associations": '["AAPL"]',
            "source_class": "official_government",
            "eligibility": "eligible",
        }
    )
    assert chunks
    assert any(c.section_key.startswith("unknown_000") for c in chunks)
    assert any(c.section_parse_degraded for c in chunks)


def test_long_doc_multiple_chunks_max_384():
    # long text forces multiple chunks under 384 max
    text = "Item 1.01 Entry\n" + ("word " * 2000)
    chunks = FilingV3Profile().chunk(
        {
            "document_id": "d1",
            "filing_type": "8-K",
            "raw_text": text,
            "available_at": "2025-08-01T00:00:00Z",
            "ticker_associations": '["AAPL"]',
            "source_class": "official_government",
            "eligibility": "eligible",
        }
    )
    assert len(chunks) >= 2


def test_long_section_heading_is_prefix_once_and_body_ranges_are_lossless():
    from catalyst_data.corpus.news_v2 import _encoding, _normalize_text, _prefix

    heading = "Item 1.01 Entry into Agreement"
    body = ("alpha beta gamma. " * 1000).strip()
    document = {
        "document_id": "d" * 64,
        "filing_type": "8-K",
        "raw_text": f"{heading}\n{body}",
        "available_at": "2025-08-01T00:00:00Z",
        "ticker_associations": '["AAPL"]',
        "source_class": "official_government",
        "eligibility": "eligible",
    }

    first = FilingV3Profile().chunk(document)
    second = FilingV3Profile().chunk(document)
    normalized_body = _normalize_text(body)
    token_ids, offsets = _encoding(normalized_body)
    prefix, _, _ = _prefix(heading)
    covered: set[int] = set()

    assert len(first) > 1
    for chunk in first:
        expected_body = normalized_body[
            offsets[chunk.body_token_start][0]:offsets[chunk.body_token_end - 1][1]
        ]
        assert chunk.content_text == prefix + expected_body
        assert chunk.content_text.count(heading) == 1
        covered.update(range(chunk.body_token_start, chunk.body_token_end))

    assert covered == set(range(len(token_ids)))
    assert [vars(chunk) for chunk in first] == [vars(chunk) for chunk in second]


def test_no_whole_filing_single_chunk_fallback():
    text = "Item 1.01 A\n" + ("alpha " * 800) + "\nItem 2.02 B\n" + ("beta " * 800)
    chunks = FilingV3Profile().chunk(
        {
            "document_id": "d1",
            "filing_type": "8-K",
            "raw_text": text,
            "available_at": "2025-08-01T00:00:00Z",
            "ticker_associations": '["AAPL"]',
            "source_class": "official_government",
            "eligibility": "eligible",
        }
    )
    assert len(chunks) > 1

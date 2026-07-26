"""Seven strongest-illegal candidate tests + contract validation — Item 4."""
from __future__ import annotations

from retrieval_fixtures import (
    MANIFEST_A, MANIFEST_B, build_fts5, fresh_v10_db, insert_corpus_chunk,
)


_fresh_v10_db = fresh_v10_db


def _seed_and_build(db, chunk_id, document_id, content_text, available_at,
                    manifest_id=MANIFEST_A, status="active",
                    eligibility="eligible", source_class="reported_news",
                    ticker_associations='["AAPL"]',
                    chunk_profile_version="news_v2"):
    import json
    insert_corpus_chunk(
        db, chunk_id=chunk_id, document_id=document_id,
        content_text=content_text, available_at=available_at,
        manifest_id=manifest_id, status=status, eligibility=eligibility,
        source_class=source_class,
        ticker_associations=tuple(json.loads(ticker_associations)),
        chunk_profile_version=chunk_profile_version,
    )
    db.commit()


def _build_fts5(db, manifest_id=MANIFEST_A):
    build_fts5(db, manifest_id)


def _add_legal(db):
    """Add one strong legal candidate."""
    _seed_and_build(db, "poly:legal:news_v2:body:0001", "poly:legal",
                    "AAPL earnings surprise revenue beat growth strong results",
                    "2026-01-01T09:00:00Z")


def retrieve(db, query="AAPL earnings"):
    from catalyst_data.retrieval.fts5 import retrieve_lexical
    return retrieve_lexical(db, query=query, ticker="AAPL",
                            cutoff="2026-01-15T21:00:00Z",
                            requested_manifest_id=MANIFEST_A,
                            top_k=3, candidate_depth=10)


# ── Seven strongest-illegal candidate tests ──────────────────────────────────

def test_illegal_wrong_manifest_excluded():
    """Strongest match with wrong manifest_id excluded."""
    db = _fresh_v10_db()
    _add_legal(db)
    _seed_and_build(db, "poly:illegal:news_v2:body:0001", "poly:illegal",
                    "AAPL earnings surprise revenue beat growth strong results",
                    "2026-01-01T09:00:00Z", manifest_id=MANIFEST_B)
    _seed_and_build(db, "poly:legal2:news_v2:body:0001", "poly:legal2",
                    "AAPL earnings surprise revenue beat growth strong results",
                    "2026-01-01T09:00:00Z", manifest_id=MANIFEST_A)
    _build_fts5(db)
    result = retrieve(db)
    # strongest illegal (wrong manifest) must not appear
    ids = {r.chunk_id for r in result.results}
    assert "poly:illegal:news_v2:body:0001" not in ids


def test_illegal_tombstoned_excluded():
    """Strongest match with tombstoned status excluded."""
    db = _fresh_v10_db()
    _add_legal(db)
    _seed_and_build(db, "poly:illegal:news_v2:body:0001", "poly:illegal",
                    "AAPL earnings surprise revenue beat growth strong results",
                    "2026-01-01T09:00:00Z", status="tombstoned")
    _build_fts5(db)
    result = retrieve(db)
    ids = {r.chunk_id for r in result.results}
    assert "poly:illegal:news_v2:body:0001" not in ids


def test_illegal_post_cutoff_excluded():
    """Strongest match after cutoff excluded."""
    db = _fresh_v10_db()
    _add_legal(db)
    _seed_and_build(db, "poly:illegal:news_v2:body:0001", "poly:illegal",
                    "AAPL earnings surprise revenue beat growth strong results",
                    "2026-01-16T09:00:00Z")  # after cutoff
    _build_fts5(db)
    result = retrieve(db)
    ids = {r.chunk_id for r in result.results}
    assert "poly:illegal:news_v2:body:0001" not in ids


def test_illegal_wrong_ticker_excluded():
    """Strongest match with wrong ticker excluded."""
    db = _fresh_v10_db()
    _add_legal(db)
    _seed_and_build(db, "poly:illegal:news_v2:body:0001", "poly:illegal",
                    "AAPL earnings surprise revenue beat growth strong results",
                    "2026-01-01T09:00:00Z", ticker_associations='["MSFT"]')
    _build_fts5(db)
    result = retrieve(db)
    ids = {r.chunk_id for r in result.results}
    assert "poly:illegal:news_v2:body:0001" not in ids


def test_illegal_wrong_source_class_excluded():
    """Strongest match with excluded source_class excluded."""
    db = _fresh_v10_db()
    _add_legal(db)
    _seed_and_build(db, "poly:illegal:news_v2:body:0001", "poly:illegal",
                    "AAPL earnings surprise revenue beat growth strong results",
                    "2026-01-01T09:00:00Z", source_class="analysis_opinion")
    _build_fts5(db)
    from catalyst_data.retrieval.fts5 import retrieve_lexical
    result = retrieve_lexical(db, query="AAPL earnings", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A,
                              source_classes=("reported_news",),
                              top_k=3, candidate_depth=10)
    ids = {r.chunk_id for r in result.results}
    assert "poly:illegal:news_v2:body:0001" not in ids


def test_illegal_wrong_profile_excluded():
    """Strongest match with excluded evidence_type excluded."""
    db = _fresh_v10_db()
    _add_legal(db)
    _seed_and_build(db, "poly:illegal:news_v2:body:0001", "poly:illegal",
                    "AAPL earnings surprise revenue beat growth strong results",
                    "2026-01-01T09:00:00Z", chunk_profile_version="filing_v2")
    _build_fts5(db)
    from catalyst_data.retrieval.fts5 import retrieve_lexical
    result = retrieve_lexical(db, query="AAPL earnings", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A,
                              evidence_types=("news_v2",),
                              top_k=3, candidate_depth=10)
    ids = {r.chunk_id for r in result.results}
    assert "poly:illegal:news_v2:body:0001" not in ids


def test_illegal_ineligible_excluded():
    """Strongest match with ineligible eligibility excluded."""
    db = _fresh_v10_db()
    _add_legal(db)
    _seed_and_build(db, "poly:illegal:news_v2:body:0001", "poly:illegal",
                    "AAPL earnings surprise revenue beat growth strong results",
                    "2026-01-01T09:00:00Z", eligibility="ineligible")
    _build_fts5(db)
    result = retrieve(db)
    ids = {r.chunk_id for r in result.results}
    assert "poly:illegal:news_v2:body:0001" not in ids


# ── FTS injection prevention ─────────────────────────────────────────────────

def test_fts_operators_not_injectable():
    """Raw FTS operators (AND, OR, NOT) in query text do not alter query
    semantics — they become literal quoted matches."""
    db = _fresh_v10_db()
    _seed_and_build(db, "poly:a:news_v2:body:0001", "poly:a",
                    "The company AND its subsidiaries reported earnings",
                    "2026-01-01T09:00:00Z")
    _build_fts5(db)

    from catalyst_data.retrieval.fts5 import retrieve_lexical
    # Query contains "AND" — it should be treated as a literal term, not operator
    result = retrieve_lexical(db, query="AND subsidiaries", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A)
    # The chunk should match because "AND" and "subsidiaries" are both literal terms
    ids = {r.chunk_id for r in result.results}
    assert "poly:a:news_v2:body:0001" in ids


# ── Contract validation ─────────────────────────────────────────────────────

def test_retrieval_rejects_non_hex_manifest():
    """Non-hex manifest_id raises RetrievalContractError."""
    import pytest
    from catalyst_data.retrieval.fts5 import retrieve_lexical, RetrievalContractError
    db = _fresh_v10_db()

    with pytest.raises(RetrievalContractError, match="invalid_manifest_id"):
        retrieve_lexical(db, query="test", ticker="AAPL",
                         cutoff="2026-01-15T21:00:00Z",
                         requested_manifest_id="not-hex-manifest-v1")


def test_retrieval_rejects_empty_source_classes():
    """Empty source_classes tuple raises invalid_filter."""
    import pytest
    from catalyst_data.retrieval.fts5 import retrieve_lexical, RetrievalContractError
    db = _fresh_v10_db()

    with pytest.raises(RetrievalContractError, match="invalid_filter"):
        retrieve_lexical(db, query="test", ticker="AAPL",
                         cutoff="2026-01-15T21:00:00Z",
                         requested_manifest_id=MANIFEST_A,
                         source_classes=())


def test_retrieval_rejects_invalid_depth():
    """top_k > candidate_depth raises invalid_depth."""
    import pytest
    from catalyst_data.retrieval.fts5 import retrieve_lexical, RetrievalContractError
    db = _fresh_v10_db()

    with pytest.raises(RetrievalContractError, match="invalid_depth"):
        retrieve_lexical(db, query="test", ticker="AAPL",
                         cutoff="2026-01-15T21:00:00Z",
                         requested_manifest_id=MANIFEST_A,
                         top_k=50, candidate_depth=20)


def test_filters_applied_not_empty():
    """filters_applied is a proper RetrievalFilters, not an empty dict."""
    db = _fresh_v10_db()
    _add_legal(db)
    _build_fts5(db)
    result = retrieve(db)

    for r in result.results:
        f = r.filters_applied
        assert isinstance(f, __import__('catalyst_data.retrieval.result', fromlist=['RetrievalFilters']).RetrievalFilters)
        assert f.ticker == "AAPL"
        assert f.requested_manifest_id == MANIFEST_A
        assert f.cutoff == "2026-01-15T21:00:00Z"

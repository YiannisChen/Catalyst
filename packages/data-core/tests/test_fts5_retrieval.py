"""Tests for FTS5 lexical retrieval — filter-before-score, BM25, degraded mode."""
from __future__ import annotations

from retrieval_fixtures import (
    MANIFEST_A, MANIFEST_B, build_fts5, fresh_v10_db, insert_corpus_chunk,
)


_fresh_v10_db = fresh_v10_db


def _seed_chunk(db, chunk_id, document_id, content_text, available_at,
               eligibility="eligible", ticker_associations='["AAPL"]',
               manifest_id=MANIFEST_A, status="active", source_class="reported_news",
               chunk_profile_version="news_v2", ordinal="0001"):
    import json
    insert_corpus_chunk(
        db, chunk_id=chunk_id, document_id=document_id,
        content_text=content_text, available_at=available_at,
        eligibility=eligibility,
        ticker_associations=tuple(json.loads(ticker_associations)),
        manifest_id=manifest_id, status=status, source_class=source_class,
        chunk_profile_version=chunk_profile_version, ordinal=ordinal,
    )
    db.commit()


def _build_fts5(db, manifest_id=MANIFEST_A):
    build_fts5(db, manifest_id)


# ── Task 9: RetrievalResult ──────────────────────────────────────────────────

def test_retrieval_result_all_fields():
    """RetrievalResult contains all contract fields."""
    from catalyst_data.retrieval.result import RetrievalResult, RetrievalFilters

    rr = RetrievalResult(
        chunk_id="test:1:news_v2:body:0001", document_id="test:1",
        available_at="2026-01-01T09:00:00Z", cutoff="2026-01-15T21:00:00Z",
        filters_applied=RetrievalFilters(ticker="AAPL", requested_manifest_id=MANIFEST_A, cutoff="2026-01-15T21:00:00Z"), source_class="reported_news",
        lexical_raw_score=-3.5, lexical_rank=1,
        corpus_manifest_id=MANIFEST_A, mode_requested="lexical",
        mode_served="fts5", is_degraded=False, timing_ms=12.3,
    )
    assert rr.chunk_id == "test:1:news_v2:body:0001"
    assert rr.lexical_raw_score == -3.5
    assert rr.mode_requested == "lexical"
    assert rr.mode_served == "fts5"
    assert not rr.is_degraded


# ── Task 8: Filter-before-score ──────────────────────────────────────────────

def test_filter_before_score_post_cutoff_excluded():
    """Post-cutoff chunk excluded even if it would rank first."""
    db = _fresh_v10_db()
    _seed_chunk(db, "poly:pre:news_v2:body:0001", "poly:pre",
                "AAPL earnings report strong results",
                "2026-01-01T09:00:00Z")
    _seed_chunk(db, "poly:post:news_v2:body:0001", "poly:post",
                "AAPL earnings report strong results",
                "2026-01-16T09:00:00Z")
    _build_fts5(db)

    from catalyst_data.retrieval.fts5 import retrieve_lexical
    result = retrieve_lexical(db, query="AAPL earnings", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A)
    chunk_ids = {r.chunk_id for r in result.results}
    assert "poly:post:news_v2:body:0001" not in chunk_ids
    assert "poly:pre:news_v2:body:0001" in chunk_ids


def test_ticker_filter():
    """Ticker filter restricts to associated tickers."""
    db = _fresh_v10_db()
    _seed_chunk(db, "poly:aap:news_v2:body:0001", "poly:aap",
                "AAPL earnings", "2026-01-01T09:00:00Z",
                ticker_associations='["AAPL"]')
    _seed_chunk(db, "poly:msf:news_v2:body:0001", "poly:msf",
                "MSFT earnings", "2026-01-01T09:00:00Z",
                ticker_associations='["MSFT"]')
    _build_fts5(db)

    from catalyst_data.retrieval.fts5 import retrieve_lexical
    result = retrieve_lexical(db, query="earnings", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A)
    chunk_ids = {r.chunk_id for r in result.results}
    assert "poly:aap:news_v2:body:0001" in chunk_ids
    assert "poly:msf:news_v2:body:0001" not in chunk_ids


def test_wrong_manifest_excluded():
    """Wrong manifest chunks excluded."""
    db = _fresh_v10_db()
    _seed_chunk(db, "poly:a:news_v2:body:0001", "poly:a",
                "AAPL earnings", "2026-01-01T09:00:00Z",
                manifest_id=MANIFEST_B)
    _build_fts5(db)  # builds with MANIFEST_A

    from catalyst_data.retrieval.fts5 import retrieve_lexical
    result = retrieve_lexical(db, query="AAPL earnings", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A)
    chunk_ids = {r.chunk_id for r in result.results}
    assert "poly:a:news_v2:body:0001" not in chunk_ids


def test_tombstoned_status_excluded():
    """Tombstoned chunks excluded."""
    db = _fresh_v10_db()
    _seed_chunk(db, "poly:tomb:news_v2:body:0001", "poly:tomb",
                "AAPL earnings strong results report",
                "2026-01-01T09:00:00Z", status="tombstoned")
    _build_fts5(db)

    from catalyst_data.retrieval.fts5 import retrieve_lexical
    result = retrieve_lexical(db, query="AAPL earnings", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A)
    chunk_ids = {r.chunk_id for r in result.results}
    assert "poly:tomb:news_v2:body:0001" not in chunk_ids


# ── Task 9: BM25 core ────────────────────────────────────────────────────────

def test_fts5_lower_is_better():
    """bm25() lower value ranks first."""
    db = _fresh_v10_db()
    _seed_chunk(db, "poly:a:news_v2:body:0001", "poly:a",
                "AAPL earnings surprise revenue beat growth outlook strong results",
                "2026-01-01T09:00:00Z")
    _seed_chunk(db, "poly:b:news_v2:body:0001", "poly:b",
                "AAPL product launch earnings revenue event",
                "2026-01-01T09:00:00Z")
    _build_fts5(db)

    from catalyst_data.retrieval.fts5 import retrieve_lexical
    result = retrieve_lexical(db, query="earnings revenue", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A)
    assert len(result.results) >= 2
    assert result.results[0].lexical_rank == 1
    assert result.results[1].lexical_rank == 2


def test_lexical_rank_is_one_based():
    """lexical_rank starts at 1, not 0."""
    db = _fresh_v10_db()
    _seed_chunk(db, "poly:a:news_v2:body:0001", "poly:a",
                "AAPL earnings report strong results",
                "2026-01-01T09:00:00Z")
    _build_fts5(db)

    from catalyst_data.retrieval.fts5 import retrieve_lexical
    result = retrieve_lexical(db, query="AAPL", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A)
    assert len(result.results) >= 1
    assert result.results[0].lexical_rank == 1


# ── Task 11: FTS5 build and degraded mode ────────────────────────────────────

def test_fts5_build_from_corpus():
    """FTS5 index built from corpus_chunks."""
    db = _fresh_v10_db()
    _seed_chunk(db, "poly:a:news_v2:body:0001", "poly:a",
                "AAPL earnings", "2026-01-01T09:00:00Z")
    _build_fts5(db)

    table = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='corpus_chunks_fts'"
    ).fetchone()
    assert table is not None
    assert "fts5" in table[0].lower()


def test_degraded_mode_no_fts_table():
    """Stale FTS index → sql_like fallback."""
    db = _fresh_v10_db()
    _seed_chunk(db, "poly:a:news_v2:body:0001", "poly:a",
                "AAPL earnings report strong results",
                "2026-01-01T09:00:00Z")
    # Don't build FTS5 index (manifest already seeded by _fresh_v10_db)

    from catalyst_data.retrieval.fts5 import retrieve_lexical
    result = retrieve_lexical(db, query="earnings", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A)
    assert result.is_degraded
    assert result.mode_served == "sql_like"
    assert result.fallback_reason == "fts5_stale"


def test_empty_query_returns_empty():
    """Empty query returns empty result set with empty_query fallback."""
    db = _fresh_v10_db()
    _seed_chunk(db, "poly:a:news_v2:body:0001", "poly:a",
                "AAPL earnings", "2026-01-01T09:00:00Z")
    _build_fts5(db)

    from catalyst_data.retrieval.fts5 import retrieve_lexical
    result = retrieve_lexical(db, query="   ", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A)
    assert len(result.results) == 0
    assert result.fallback_reason == "empty_query"

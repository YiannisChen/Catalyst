"""filing_v3 evidence profile alignment (Finding 4).

The production evidence profile is ``{news_v2, filing_v3}``; ``filing_v2`` is
only a historical frozen artifact and must be rejected by current retrieval
filters with a typed error.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from retrieval_fixtures import (
    MANIFEST_A, build_fts5, fresh_v10_db, insert_corpus_chunk,
)
from retrieval_model_fixtures import make_results


def _seed(db, chunk_id, *, profile, text="AAPL earnings report strong results"):
    insert_corpus_chunk(
        db, chunk_id=chunk_id, document_id=f"doc:{chunk_id}",
        content_text=text, available_at="2026-01-01T09:00:00Z",
        eligibility="eligible", ticker_associations=("AAPL",),
        manifest_id=MANIFEST_A, status="active",
        source_class="reported_news", chunk_profile_version=profile,
    )
    db.commit()


def test_evidence_types_are_news_v2_and_filing_v3():
    from catalyst_data.retrieval.result import EVIDENCE_TYPES

    assert EVIDENCE_TYPES == {"news_v2", "filing_v3"}
    assert "filing_v2" not in EVIDENCE_TYPES


def test_lexical_accepts_filing_v3_and_news_v2():
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    db = fresh_v10_db()
    _seed(db, "poly:news:news_v2:body:0001", profile="news_v2")
    _seed(db, "poly:filing:filing_v3:body:0001", profile="filing_v3")
    build_fts5(db, MANIFEST_A)

    news = retrieve_lexical(
        db, query="AAPL earnings", ticker="AAPL", cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id=MANIFEST_A, top_k=5, candidate_depth=10,
        evidence_types=("news_v2",),
    )
    filing = retrieve_lexical(
        db, query="AAPL earnings", ticker="AAPL", cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id=MANIFEST_A, top_k=5, candidate_depth=10,
        evidence_types=("filing_v3",),
    )
    both = retrieve_lexical(
        db, query="AAPL earnings", ticker="AAPL", cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id=MANIFEST_A, top_k=5, candidate_depth=10,
        evidence_types=("filing_v3", "news_v2"),
    )
    assert {item.chunk_id for item in news.results} == {"poly:news:news_v2:body:0001"}
    assert {item.chunk_id for item in filing.results} == {"poly:filing:filing_v3:body:0001"}
    assert {item.chunk_id for item in both.results} == {
        "poly:news:news_v2:body:0001", "poly:filing:filing_v3:body:0001",
    }


def test_lexical_rejects_illegal_profile_with_typed_error():
    from catalyst_data.retrieval.fts5 import retrieve_lexical
    from catalyst_data.retrieval.result import RetrievalContractError

    db = fresh_v10_db()
    _seed(db, "poly:legacy:filing_v2:body:0001", profile="filing_v2")
    build_fts5(db, MANIFEST_A)
    with pytest.raises(RetrievalContractError) as excinfo:
        retrieve_lexical(
            db, query="AAPL earnings", ticker="AAPL", cutoff="2026-01-15T21:00:00Z",
            requested_manifest_id=MANIFEST_A, top_k=5, candidate_depth=10,
            evidence_types=("filing_v2",),
        )
    assert excinfo.value.code == "invalid_filter"


class _EmptyLanceQuery:
    def __init__(self):
        self.where_calls = []

    def where(self, predicate, *, prefilter=False):
        self.where_calls.append(predicate)
        return self

    def limit(self, value):
        return self

    def to_list(self):
        return []


class _EmptyLanceTable:
    def __init__(self):
        self.query = _EmptyLanceQuery()

    def search(self, query_embedding, *, query_type="vector"):
        return self.query


def test_dense_accepts_filing_v3_and_news_v2():
    from catalyst_data.retrieval.dense import _lancedb_prefilter, retrieve_dense

    for profile in ("filing_v3", "news_v2"):
        table = _EmptyLanceTable()
        result = retrieve_dense(
            None,
            np.ones(1024, dtype=np.float32),
            ticker="AAPL",
            cutoff="2026-01-15T21:00:00Z",
            requested_manifest_id=MANIFEST_A,
            index_manifest_id="1" * 64,
            lancedb_table=table,
            source_classes=None,
            evidence_types=(profile,),
        )
        assert result is not None
        predicate = _lancedb_prefilter(
            ticker="AAPL", cutoff="2026-01-15T21:00:00Z",
            requested_manifest_id=MANIFEST_A, index_manifest_id="1" * 64,
            source_classes=None, evidence_types=(profile,),
        )
        assert f"chunk_profile_version IN ('{profile}')" in predicate


def test_dense_rejects_filing_v2_with_typed_error():
    from catalyst_data.retrieval.dense import retrieve_dense
    from catalyst_data.retrieval.result import RetrievalContractError

    with pytest.raises(RetrievalContractError) as excinfo:
        retrieve_dense(
            None,
            np.ones(1024, dtype=np.float32),
            ticker="AAPL",
            cutoff="2026-01-15T21:00:00Z",
            requested_manifest_id=MANIFEST_A,
            index_manifest_id="1" * 64,
            lancedb_table=object(),
            evidence_types=("filing_v2",),
        )
    assert excinfo.value.code == "invalid_filter"


def test_hybrid_passes_filing_v3_identically_to_both_arms(monkeypatch):
    from catalyst_data.retrieval.hybrid import retrieve_hybrid

    calls = []
    lexical_values = tuple(make_results(2, "lex"))
    dense_values = tuple(make_results(2, "den", mode_requested="dense", mode_served="dense"))

    def fake_lexical(*args, **kwargs):
        calls.append(("lexical", kwargs))
        from catalyst_data.retrieval.result import RetrievalResultSet

        return RetrievalResultSet(
            candidates=lexical_values, results=lexical_values, candidate_count=2,
            mode_requested="lexical", mode_served="fts5", is_degraded=False,
        )

    def fake_dense(*args, **kwargs):
        calls.append(("dense", kwargs))
        from catalyst_data.retrieval.result import RetrievalResultSet

        return RetrievalResultSet(
            candidates=dense_values, results=dense_values, candidate_count=2,
            mode_requested="dense", mode_served="dense", is_degraded=False,
        )

    monkeypatch.setattr("catalyst_data.retrieval.hybrid.retrieve_lexical", fake_lexical)
    monkeypatch.setattr("catalyst_data.retrieval.hybrid.retrieve_dense", fake_dense)

    class _FakeCursor:
        def fetchone(self):
            return (MANIFEST_A,)

    class _FakeDb:
        def execute(self, sql, params=()):
            return _FakeCursor()

    result = retrieve_hybrid(
        _FakeDb(), query="AAPL earnings", ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z", mode="hybrid",
        query_embedding=np.ones(1024, dtype=np.float32),
        requested_manifest_id=MANIFEST_A, index_manifest_id="1" * 64,
        lancedb_table=object(),
        evidence_types=("filing_v3", "news_v2"),
    )
    assert result.mode_served == "hybrid"
    assert [name for name, _ in calls] == ["lexical", "dense"]
    assert calls[0][1]["evidence_types"] == ("filing_v3", "news_v2")
    assert calls[1][1]["evidence_types"] == ("filing_v3", "news_v2")
    assert calls[0][1]["cutoff"] == calls[1][1]["cutoff"] == "2026-01-15T21:00:00Z"
    assert calls[0][1]["ticker"] == calls[1][1]["ticker"] == "AAPL"

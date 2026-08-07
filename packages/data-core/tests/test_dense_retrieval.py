from __future__ import annotations

import inspect

import numpy as np

from retrieval_model_fixtures import MANIFEST_A


class _RecordingLanceQuery:
    def __init__(self, rows):
        self.rows = rows
        self.where_calls = []
        self.limit_calls = []

    def where(self, predicate, *, prefilter=False):
        self.where_calls.append((predicate, prefilter))
        return self

    def limit(self, value):
        self.limit_calls.append(value)
        return self

    def to_list(self):
        return list(self.rows)


class _RecordingLanceTable:
    def __init__(self, rows):
        self.rows = rows
        self.search_calls = []
        self.query = _RecordingLanceQuery(rows)

    def search(self, query_embedding, *, query_type="vector"):
        self.search_calls.append((query_embedding, query_type))
        return self.query


def _row(chunk_id: str, *, ticker: str = "AAPL", profile: str = "news_v2"):
    return {
        "chunk_id": chunk_id,
        "document_id": f"doc:{chunk_id}",
        "content_text": "AAPL earnings",
        "available_at": "2026-01-01T00:00:00Z",
        "ticker_associations": [ticker],
        "source_class": "reported_news",
        "chunk_profile_version": profile,
        "corpus_manifest_id": MANIFEST_A,
        "index_manifest_id": "1" * 64,
        "status": "active",
        "eligibility": "eligible",
        "_distance": 0.1,
    }


def test_dense_uses_lancedb_search_with_prefilter_and_limit():
    from catalyst_data.retrieval.dense import retrieve_dense

    table = _RecordingLanceTable([_row("aapl:00")])
    result = retrieve_dense(
        None,
        np.ones(1024, dtype=np.float32),
        ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id=MANIFEST_A,
        index_manifest_id="1" * 64,
        lancedb_table=table,
        source_classes=("reported_news",),
        evidence_types=("news_v2",),
    )

    assert table.search_calls and table.search_calls[0][1] == "vector"
    predicate, prefilter = table.query.where_calls[0]
    assert prefilter is True
    assert "array_contains(ticker_associations, 'AAPL')" in predicate
    assert "available_at <= '2026-01-15T21:00:00Z'" in predicate
    assert "source_class IN ('reported_news')" in predicate
    assert "chunk_profile_version IN ('news_v2')" in predicate
    assert table.query.limit_calls == [20]
    assert [item.chunk_id for item in result.results] == ["aapl:00"]


def test_dense_fixture_scale_lancedb_filters_before_vector_limit(tmp_path):
    lancedb = __import__("lancedb")
    from catalyst_data.retrieval.dense import retrieve_dense

    db = lancedb.connect(str(tmp_path / "lance"))
    table = db.create_table(
        "vectors",
        data=[
            {**{k: v for k, v in _row("aapl:00").items() if k != "_distance"}, "vector": [1.0] + [0.0] * 1023},
            {**{k: v for k, v in _row("msft:00", ticker="MSFT").items() if k != "_distance"}, "vector": [0.9] + [0.1] * 1023},
            {**{k: v for k, v in _row("aapl:01", profile="filing_v3").items() if k != "_distance"}, "vector": [0.8] + [0.2] * 1023},
        ],
    )
    result = retrieve_dense(
        None,
        np.r_[1.0, np.zeros(1023, dtype=np.float32)],
        ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id=MANIFEST_A,
        index_manifest_id="1" * 64,
        lancedb_table=table,
        source_classes=("reported_news",),
        evidence_types=("news_v2",),
    )

    assert [item.chunk_id for item in result.results] == ["aapl:00"]
    assert result.results[0].dense_score == 1.0


def test_dense_production_has_no_numpy_vector_scan():
    from catalyst_data.retrieval import dense

    source = inspect.getsource(dense)
    assert "np.load" not in source
    assert "vectors.npy" not in source
    assert "_cosine" not in source
    assert ".search(" in source


def test_dense_rejects_empty_optional_filter_like_lexical():
    import pytest
    from catalyst_data.retrieval.dense import retrieve_dense
    from catalyst_data.retrieval.result import RetrievalContractError

    with pytest.raises(RetrievalContractError):
        retrieve_dense(
            None, np.ones(1024, dtype=np.float32), ticker="AAPL",
            cutoff="2026-01-15T21:00:00Z", requested_manifest_id=MANIFEST_A,
            index_manifest_id="1" * 64, lancedb_table=_RecordingLanceTable([]),
            source_classes=(),
        )

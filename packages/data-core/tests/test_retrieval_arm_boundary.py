"""Backend boundary conversion tests for hybrid retrieval arms (Task 4).

Only confirmed SQLite/LanceDB availability failures may be converted to
RetrievalArmUnavailableError; corruption, filter/programmer errors, and
identity drift always propagate.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from retrieval_model_fixtures import MANIFEST_A

_HEX64 = "1" * 64


class _FailingConn:
    def __init__(self, exc: Exception):
        self._exc = exc

    def execute(self, sql, params=()):
        raise self._exc


def test_lexical_converts_confirmed_sqlite_availability_to_typed_unavailable():
    from catalyst_data.retrieval.fts5 import retrieve_lexical
    from catalyst_data.retrieval.result import RetrievalArmUnavailableError

    with pytest.raises(RetrievalArmUnavailableError) as captured:
        retrieve_lexical(
            _FailingConn(sqlite3.OperationalError("unable to open database file")),
            "AAPL earnings", ticker="AAPL", cutoff="2026-01-15T21:00:00Z",
            requested_manifest_id=MANIFEST_A,
        )
    assert captured.value.arm == "lexical"
    assert captured.value.code == "fts5_unavailable"


def test_lexical_propagates_database_corruption():
    from catalyst_data.retrieval.fts5 import retrieve_lexical
    from catalyst_data.retrieval.result import RetrievalArmUnavailableError

    with pytest.raises(sqlite3.DatabaseError):
        retrieve_lexical(
            _FailingConn(sqlite3.DatabaseError("database disk image is malformed")),
            "AAPL earnings", ticker="AAPL", cutoff="2026-01-15T21:00:00Z",
            requested_manifest_id=MANIFEST_A,
        )


def test_lexical_propagates_fts_syntax_error():
    from catalyst_data.retrieval.fts5 import retrieve_lexical
    from catalyst_data.retrieval.result import RetrievalArmUnavailableError

    with pytest.raises(sqlite3.OperationalError, match="syntax error"):
        retrieve_lexical(
            _FailingConn(sqlite3.OperationalError("fts5: syntax error near \"x\"")),
            "AAPL earnings", ticker="AAPL", cutoff="2026-01-15T21:00:00Z",
            requested_manifest_id=MANIFEST_A,
        )


class _IoFailTable:
    def search(self, query_embedding, *, query_type="vector"):
        return self

    def where(self, predicate, prefilter=True):
        return self

    def limit(self, value):
        return self

    def to_list(self):
        raise RuntimeError(
            "lance error: LanceError(IO): Generic N/A error: Object at location /tmp/x not found"
        )


class _NotFoundTable(_IoFailTable):
    def to_list(self):
        raise ValueError("Table 'vectors' was not found")


class _BadFilterTable(_IoFailTable):
    def to_list(self):
        raise ValueError("no column named bogus_col")


def test_dense_converts_lance_io_error_to_typed_unavailable():
    from catalyst_data.retrieval.dense import retrieve_dense
    from catalyst_data.retrieval.result import RetrievalArmUnavailableError

    with pytest.raises(RetrievalArmUnavailableError) as captured:
        retrieve_dense(
            None, np.ones(1024, dtype=np.float32), ticker="AAPL",
            cutoff="2026-01-15T21:00:00Z", requested_manifest_id=MANIFEST_A,
            index_manifest_id=_HEX64, lancedb_table=_IoFailTable(),
        )
    assert captured.value.arm == "dense"
    assert captured.value.code == "dense_unavailable"


def test_dense_converts_table_not_found_to_typed_unavailable():
    from catalyst_data.retrieval.dense import retrieve_dense
    from catalyst_data.retrieval.result import RetrievalArmUnavailableError

    with pytest.raises(RetrievalArmUnavailableError) as captured:
        retrieve_dense(
            None, np.ones(1024, dtype=np.float32), ticker="AAPL",
            cutoff="2026-01-15T21:00:00Z", requested_manifest_id=MANIFEST_A,
            index_manifest_id=_HEX64, lancedb_table=_NotFoundTable(),
        )
    assert captured.value.code == "dense_unavailable"


def test_dense_propagates_filter_programmer_error():
    from catalyst_data.retrieval.dense import retrieve_dense
    from catalyst_data.retrieval.result import RetrievalArmUnavailableError

    with pytest.raises(ValueError, match="bogus_col"):
        retrieve_dense(
            None, np.ones(1024, dtype=np.float32), ticker="AAPL",
            cutoff="2026-01-15T21:00:00Z", requested_manifest_id=MANIFEST_A,
            index_manifest_id=_HEX64, lancedb_table=_BadFilterTable(),
        )


def test_dense_propagates_persisted_identity_drift():
    from catalyst_data.retrieval.dense import retrieve_dense
    from catalyst_data.retrieval.result import RetrievalArmUnavailableError

    class CorruptRowTable(_IoFailTable):
        def to_list(self):
            return [{
                "chunk_id": "c:1", "document_id": "doc", "content_text": "x",
                "available_at": "2026-01-01T00:00:00Z",
                "ticker_associations": ["AAPL"], "source_class": "reported_news",
                "chunk_profile_version": "news_v2", "status": "active",
                "eligibility": "eligible",
                "corpus_manifest_id": "9" * 64,
                "index_manifest_id": _HEX64, "_distance": 0.1,
            }]

    with pytest.raises(ValueError, match="corpus_manifest_id"):
        retrieve_dense(
            None, np.ones(1024, dtype=np.float32), ticker="AAPL",
            cutoff="2026-01-15T21:00:00Z", requested_manifest_id=MANIFEST_A,
            index_manifest_id=_HEX64, lancedb_table=CorruptRowTable(),
        )


def test_typed_unavailable_error_carries_stable_arm_and_code():
    from catalyst_data.retrieval.result import RetrievalArmUnavailableError

    error = RetrievalArmUnavailableError("dense", "dense_unavailable", "boom")
    assert error.arm == "dense"
    assert error.code == "dense_unavailable"
    assert "boom" not in error.code
    with pytest.raises(ValueError):
        RetrievalArmUnavailableError("bogus", "code")
    with pytest.raises(ValueError):
        RetrievalArmUnavailableError("lexical", "has spaces")

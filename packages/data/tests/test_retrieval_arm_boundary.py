"""Backend boundary conversion tests for hybrid retrieval arms (Task 4).

Only confirmed SQLite/LanceDB availability failures may be converted to
RetrievalArmUnavailableError; corruption, filter/programmer errors, and
identity drift always propagate.
"""

from __future__ import annotations

import sqlite3

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

    with pytest.raises(sqlite3.DatabaseError):
        retrieve_lexical(
            _FailingConn(sqlite3.DatabaseError("database disk image is malformed")),
            "AAPL earnings", ticker="AAPL", cutoff="2026-01-15T21:00:00Z",
            requested_manifest_id=MANIFEST_A,
        )


def test_lexical_propagates_fts_syntax_error():
    from catalyst_data.retrieval.fts5 import retrieve_lexical

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

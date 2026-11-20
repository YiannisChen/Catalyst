from __future__ import annotations

import inspect


def test_retrieval_fts5_builder_uses_bounded_batches():
    from catalyst_data.retrieval.fts5_builder import build_fts5_index

    source = inspect.getsource(build_fts5_index)
    assert ".fetchall()" not in source
    assert "fetchmany(500)" in source

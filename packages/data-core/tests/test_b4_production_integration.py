from __future__ import annotations

from catalyst_data.index_builder import build_corpus_and_lexical_index
from catalyst_data.retrieval_policy import retrieve_lexical
from test_index_builder import _make_db


class _LocalTokenizer:
    def __call__(self, text, **kwargs):
        offsets = [(index, index + 1) for index in range(len(text)) if not text[index].isspace()]
        return {"input_ids": list(range(len(offsets))), "offset_mapping": offsets}

    def encode(self, text, **kwargs):
        return self(text)["input_ids"]


def test_production_corpus_build_publishes_lexical_index_and_retrieves(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "catalyst_data.corpus.tokenizer.get_tokenizer", lambda: _LocalTokenizer()
    )
    conn = _make_db(str(tmp_path / "b4-integration.db"))
    build = build_corpus_and_lexical_index(
        conn,
        certified_snapshot_identity="b4-integration-snapshot",
        clock=lambda: "2026-01-16T00:00:00Z",
    )

    result = retrieve_lexical(
        conn,
        "MSFT earnings",
        ticker="MSFT",
        cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id=build.corpus.manifest_id,
        include_trace=True,
    )

    assert build.lexical.manifest_id == build.corpus.manifest_id
    assert build.lexical.row_count == len(build.corpus.chunks)
    assert [row.document_id for row in result.results] == ["poly:a4"]
    assert result.trace is not None
    assert result.trace.candidate_count == 1
    assert result.trace.final_count == 1

"""T5-T8 four-arm runner contracts with literal independent oracles.

Expected union order, arm chunk IDs, identity values, and failure outcomes are
literal fixtures. No production fusion/pool/hash helper computes an expected
value at test time.

Amendment P2: one shared query embedding and one shared candidate universe per
case; all four arms are derived from a single retrieve_hybrid(mode='reranked')
call. P4: run-level staging, no caller-supplied tokens, strict success-token
gate. P6: real latency and contract-only mode_served values.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from tests.post_import_fixtures import (
    CUTOFF,
    EXPECTED_UNION,
    LITERAL_DENSE,
    LITERAL_FTS5,
    LITERAL_HYBRID,
    LITERAL_RERANKED,
    MockQueryEmbedder,
    RecordingReranker,
    fresh_lance_table,
    fresh_runner_db,
    make_hybrid_result,
    make_result_set,
)
from catalyst_eval.post_import.case_pack import SCHEMA_VERSION as CASE_PACK_SCHEMA_VERSION
from catalyst_eval.post_import.case_pack import CasePackCase, build_smoke_case_pack, compute_case_pack_id
from catalyst_eval.post_import.t4_evidence import APPROVED_T4_CONTRACT
from catalyst_eval.post_import.four_arm import (
    ARM_ORDER,
    META_SCHEMA_VERSION,
    EmbeddingBoundary,
    RunIdentities,
    run_four_arm_cases,
    validate_embedding_boundary,
    validate_query_vector,
)
from catalyst_eval.post_import.index_identity import ResolvedRuntimeIdentity
from catalyst_eval.post_import.t4_evidence import ValidatedT4Evidence

APPROVED = {
    "code_revision": "bb43ebe20f29a13ef426e0a1a7c3aefc6d15ffd8",
    "git_head": "8dd9ee9b5f04e848e3d8248dad6470189af79573",
    "snapshot_id": "7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49",
    "corpus_manifest_id": "3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc",
    "source_bundle_id": "8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2",
    "probe_report_id": "25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23",
    "postbuild_readiness_id": "9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b",
    "index_manifest_id": "c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083",
    "lancedb_dir": "data/lancedb_gold/b6g_8ffae891b4e1",
    "active_table_name": "chunks__staging__b3761f4b943542a8",
    "model_name": "BAAI/bge-m3",
    "model_revision": "5617a9f61b028005a4858fdac845db406aefb181",
    "tokenizer_revision": "5617a9f61b028005a4858fdac845db406aefb181",
    "reranker_model": "BAAI/bge-reranker-v2-m3",
    "reranker_revision": "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
    "dimension": 1024,
    "dtype": "float32",
    "normalization_mode": "l2",
    "vector_count": 295506,
    "lancedb_row_count": 295506,
    "db_path": "data/snapshots/catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db",
    "db_sha256": "bb37b213091e256033fa00272cb7a85617dbcddf69d6fe9b515840cd9f1ebe40",
    "db_user_version": 13,
    "db_foreign_key_violations": 0,
}

FULL_CASE_PACK_ID = "05333690c3a3f074e34b038a17792837568bf55870abf5678b15ec85c41a03aa"


def _identities(**overrides) -> RunIdentities:
    values = dict(APPROVED)
    values.update(overrides)
    return RunIdentities(**values)


def _case(case_id: str = "B001") -> CasePackCase:
    return CasePackCase(
        schema_version=CASE_PACK_SCHEMA_VERSION,
        case_id=case_id, ticker="AAPL", session_date="2026-01-15",
        cutoff=CUTOFF, query="Why did AAPL move on 2026-01-15?", source_set="fixture",
        golden={"golden_id": case_id, "should_refuse": False},
    )


def _mock_boundary(**overrides) -> EmbeddingBoundary:
    values = {
        "embedding_mode": "mock_unit_test",
        "dimension": 1024,
        "model_revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "tokenizer_revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "is_mock": True,
        "cuda_available": False,
    }
    values.update(overrides)
    return EmbeddingBoundary(**values)


def _validated_evidence(case_pack_id: str = FULL_CASE_PACK_ID) -> ValidatedT4Evidence:
    return ValidatedT4Evidence(
        evidence_dir=Path("data/run_reports/post_import/t4_case_pack_probe_amended_20260810"),
        case_pack_id=case_pack_id,
        case_count=10,
        passed_count=10,
        db_sha256="bb37b213091e256033fa00272cb7a85617dbcddf69d6fe9b515840cd9f1ebe40",
        db_user_version=13,
        db_foreign_key_violations=0,
        snapshot_id=APPROVED["snapshot_id"],
        corpus_manifest_id=APPROVED["corpus_manifest_id"],
        source_bundle_id=APPROVED["source_bundle_id"],
        probe_report_id=APPROVED["probe_report_id"],
        postbuild_readiness_id=APPROVED["postbuild_readiness_id"],
        index_manifest_id=APPROVED["index_manifest_id"],
        active_table_name=APPROVED["active_table_name"],
        model_name=APPROVED["model_name"],
        model_revision=APPROVED["model_revision"],
        tokenizer_revision=APPROVED["tokenizer_revision"],
        dimension=1024,
        dtype="float32",
        normalization_mode="l2",
        runtime_git_head=APPROVED["git_head"],
        index_build_code_revision=APPROVED["code_revision"],
        probe_report_sha256="a" * 64,
    )


def _validated_runtime() -> ResolvedRuntimeIdentity:
    return ResolvedRuntimeIdentity(
        lancedb_dir=Path(APPROVED["lancedb_dir"]),
        active_table_name=APPROVED["active_table_name"],
        snapshot_id=APPROVED["snapshot_id"],
        corpus_manifest_id=APPROVED["corpus_manifest_id"],
        source_bundle_id=APPROVED["source_bundle_id"],
        index_manifest_id=APPROVED["index_manifest_id"],
        probe_report_id=APPROVED["probe_report_id"],
        postbuild_readiness_id=APPROVED["postbuild_readiness_id"],
        code_revision=APPROVED["code_revision"],
        git_head=APPROVED["git_head"],
        model_name=APPROVED["model_name"],
        model_revision=APPROVED["model_revision"],
        tokenizer_revision=APPROVED["tokenizer_revision"],
        dimension=1024,
        dtype="float32",
        normalization_mode="l2",
        vector_count=295506,
        db_path=Path("data/snapshots/catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db"),
        db_sha256="bb37b213091e256033fa00272cb7a85617dbcddf69d6fe9b515840cd9f1ebe40",
        db_user_version=13,
        db_foreign_key_violations=0,
        lancedb_row_count=295506,
    )


def _ok_hybrid(**overrides):
    """Single shared retrieval result: all four arms derived from one call."""
    return make_hybrid_result(
        mode_requested="reranked", mode_served="reranked", **overrides,
    )


def _monkeypatch_single_call(monkeypatch, *, hybrid=None):
    """Patch only the shared retrieve_hybrid(mode='reranked') primitive."""
    import catalyst_eval.post_import.four_arm as four_arm

    monkeypatch.setattr(
        four_arm, "_retrieve_hybrid",
        lambda *a, **k: hybrid if hybrid is not None else _ok_hybrid(),
    )


def _run_once(tmp_path, monkeypatch, *, cases=None, run_id="run1", **kwargs):
    return run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=cases or [_case()],
        run_id=run_id,
        output_root=tmp_path,
        identities=_identities(),
        boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=RecordingReranker(),
        case_pack_id=FULL_CASE_PACK_ID,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# T5: runner orchestration and artifact layout
# ---------------------------------------------------------------------------


def test_arm_order_is_literal():
    assert ARM_ORDER == ("fts5", "dense", "hybrid", "reranked")


def test_meta_schema_version_is_literal():
    assert META_SCHEMA_VERSION == "post_import_run_meta_v1"


def test_runner_writes_arms_pool_and_meta(tmp_path, monkeypatch):
    _monkeypatch_single_call(monkeypatch)

    run_dir = tmp_path / "run1"
    summary = _run_once(tmp_path, monkeypatch)

    assert summary.case_count == 1
    assert summary.arms_written == 1
    assert summary.pools_written == 1
    assert (run_dir / "arms" / "B001.json").is_file()
    assert (run_dir / "pool" / "B001.json").is_file()
    assert (run_dir / "meta.json").is_file()

    from catalyst_data.retrieval.artifacts import load_arm_artifact

    loaded_arm = load_arm_artifact(run_dir / "arms" / "B001.json")
    assert list(loaded_arm.arms) == ["fts5", "dense", "hybrid", "reranked"]
    arm = loaded_arm.to_dict()
    assert [r["chunk_id"] for r in arm["arms"]["fts5"]["results"]] == ["a", "b", "c"]
    assert [r["chunk_id"] for r in arm["arms"]["dense"]["results"]] == ["b", "d"]
    assert [r["chunk_id"] for r in arm["arms"]["hybrid"]["results"]] == ["d", "a", "e"]
    assert arm["arms"]["hybrid"]["mode_served"] == "hybrid"
    assert arm["arms"]["reranked"]["mode_served"] == "reranked"
    assert [r["chunk_id"] for r in arm["arms"]["reranked"]["results"]] == ["e", "f"]

    pool = json.loads((run_dir / "pool" / "B001.json").read_text())
    assert tuple(pool["chunk_ids"]) == EXPECTED_UNION
    assert pool["per_arm_chunk_ids"]["fts5"] == list(LITERAL_FTS5)
    assert pool["per_arm_chunk_ids"]["dense"] == list(LITERAL_DENSE)
    assert pool["per_arm_chunk_ids"]["hybrid"] == list(LITERAL_HYBRID)
    assert pool["per_arm_chunk_ids"]["reranked"] == list(LITERAL_RERANKED)
    assert pool["source_artifact_id"] == arm["artifact_id"]


# ---------------------------------------------------------------------------
# Amendment P2: single shared query vector and candidate universe
# ---------------------------------------------------------------------------


def test_embedding_fn_called_exactly_once_per_case(tmp_path, monkeypatch):
    import catalyst_eval.post_import.four_arm as four_arm

    calls = []

    def embedding_fn(query):
        calls.append(query)
        return MockQueryEmbedder().embed_query(query)

    monkeypatch.setattr(
        four_arm, "_retrieve_hybrid",
        lambda *a, **k: _ok_hybrid(),
    )
    run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=[_case()], run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=embedding_fn,
        reranker=RecordingReranker(),
        case_pack_id=FULL_CASE_PACK_ID,
    )
    assert len(calls) == 1


def test_hybrid_pipeline_called_exactly_once_per_case(tmp_path, monkeypatch):
    import catalyst_eval.post_import.four_arm as four_arm

    calls = []

    def hybrid_fn(*args, **kwargs):
        calls.append(kwargs)
        return _ok_hybrid()

    monkeypatch.setattr(four_arm, "_retrieve_hybrid", hybrid_fn)
    run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=[_case()], run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=RecordingReranker(),
        case_pack_id=FULL_CASE_PACK_ID,
    )
    assert len(calls) == 1
    assert calls[0]["mode"] == "reranked"


def test_four_arms_share_same_query_cutoff_manifest_index_identity(tmp_path, monkeypatch):
    import catalyst_eval.post_import.four_arm as four_arm

    captured = {}

    def hybrid_fn(*args, **kwargs):
        captured.update(kwargs)
        return _ok_hybrid()

    monkeypatch.setattr(four_arm, "_retrieve_hybrid", hybrid_fn)
    run_dir = tmp_path / "run1"
    run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=[_case()], run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=RecordingReranker(),
        case_pack_id=FULL_CASE_PACK_ID,
    )
    arm = json.loads((run_dir / "arms" / "B001.json").read_text())
    filters = arm["filters"]
    assert captured["query"] == "Why did AAPL move on 2026-01-15?"
    assert captured["cutoff"] == CUTOFF
    assert captured["ticker"] == "AAPL"
    assert captured["requested_manifest_id"] == APPROVED["corpus_manifest_id"]
    assert captured["index_manifest_id"] == APPROVED["index_manifest_id"]
    assert filters["corpus_manifest_id"] == APPROVED["corpus_manifest_id"]
    assert filters["index_manifest_id"] == APPROVED["index_manifest_id"]
    for arm_name in ARM_ORDER:
        assert arm["arms"][arm_name]["mode_requested"] == arm_name


def test_reranker_input_set_equals_fused_candidate_set(tmp_path, monkeypatch):
    import catalyst_eval.post_import.four_arm as four_arm
    from catalyst_data.retrieval.hybrid import HybridRetrievalResult

    reranker = RecordingReranker()

    def reranked_pipeline(*args, **kwargs):
        """Mirror retrieve_hybrid(mode='reranked'): reranker scores fusion."""
        base = _ok_hybrid()
        fusion = list(base.fusion_results)
        scores = reranker.score(kwargs["query"], fusion)
        reranked = [
            item.model_copy(update={"reranker_score": score, "reranker_rank": idx})
            for idx, (item, score) in enumerate(zip(fusion, scores), start=1)
        ]
        return HybridRetrievalResult(
            mode_requested="reranked",
            mode_served="reranked",
            lexical_results=base.lexical_results,
            dense_results=base.dense_results,
            fusion_results=base.fusion_results,
            reranker_results=make_result_set("reranked", "reranked", LITERAL_RERANKED),
            final_results=tuple(reranked),
        )

    monkeypatch.setattr(four_arm, "_retrieve_hybrid", reranked_pipeline)
    run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=[_case()], run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=reranker,
        case_pack_id=FULL_CASE_PACK_ID,
    )
    assert len(reranker.calls) == 1
    reranked_input_ids = set(reranker.calls[0][1])
    assert reranked_input_ids == set(LITERAL_HYBRID)


def test_all_arms_derived_from_same_retrieval_result(tmp_path, monkeypatch):
    """fts5/dense/hybrid/reranked artifacts come from one HybridRetrievalResult."""
    import catalyst_eval.post_import.four_arm as four_arm

    seen = []

    def hybrid_fn(*args, **kwargs):
        result = _ok_hybrid()
        seen.append(id(result))
        return result

    monkeypatch.setattr(four_arm, "_retrieve_hybrid", hybrid_fn)
    run_dir = tmp_path / "run1"
    run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=[_case()], run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=RecordingReranker(),
        case_pack_id=FULL_CASE_PACK_ID,
    )
    assert len(seen) == 1
    arm = json.loads((run_dir / "arms" / "B001.json").read_text())
    assert [r["chunk_id"] for r in arm["arms"]["fts5"]["results"]] == ["a", "b", "c"]
    assert [r["chunk_id"] for r in arm["arms"]["dense"]["results"]] == ["b", "d"]
    assert [r["chunk_id"] for r in arm["arms"]["hybrid"]["results"]] == ["d", "a", "e"]
    assert arm["arms"]["hybrid"]["mode_served"] == "hybrid"
    assert arm["arms"]["reranked"]["mode_served"] == "reranked"
    assert [r["chunk_id"] for r in arm["arms"]["reranked"]["results"]] == ["e", "f"]


# ---------------------------------------------------------------------------
# T6: cutoff / ticker / filter / look-ahead contracts
# ---------------------------------------------------------------------------


def test_runner_rejects_post_cutoff_evidence(tmp_path, monkeypatch):
    import catalyst_eval.post_import.four_arm as four_arm
    from catalyst_eval.post_import.four_arm import RunnerValidationError

    bad = make_result_set("lexical", "fts5", ("a", "b", "c"))
    bad = bad.model_copy(update={
        "results": tuple(
            item.model_copy(update={"available_at": "2026-02-01T00:00:00Z"})
            if item.chunk_id == "b" else item
            for item in bad.results
        ),
    })
    monkeypatch.setattr(
        four_arm, "_retrieve_hybrid",
        lambda *a, **k: _ok_hybrid(lexical_ids=("a", "b", "c")) if False else _ok_hybrid(),
    )

    # Build a hybrid result whose lexical slice has a post-cutoff chunk.
    hybrid = make_hybrid_result(
        mode_requested="reranked", mode_served="reranked",
        lexical_override=bad,
    )
    monkeypatch.setattr(four_arm, "_retrieve_hybrid", lambda *a, **k: hybrid)

    run_dir = tmp_path / "run1"
    with pytest.raises(RunnerValidationError, match="look-ahead|post-cutoff|post_cutoff"):
        run_four_arm_cases(
            db=fresh_runner_db(tmp_path),
            lancedb_table=fresh_lance_table(tmp_path),
            cases=[_case()], run_id="run1", output_root=tmp_path,
            identities=_identities(), boundary=_mock_boundary(),
            query_embedding_fn=MockQueryEmbedder().embed_query,
            reranker=None,
            case_pack_id=FULL_CASE_PACK_ID,
        )
    assert not (run_dir / "arms").exists()
    assert not (run_dir / "pool").exists()
    assert not (run_dir / "meta.json").exists()


def test_runner_rejects_wrong_ticker_evidence(tmp_path, monkeypatch):
    import catalyst_eval.post_import.four_arm as four_arm
    from catalyst_eval.post_import.four_arm import RunnerValidationError

    bad = make_result_set("dense", "dense", ("b", "d"))
    bad = bad.model_copy(update={
        "results": tuple(
            item.model_copy(update={"ticker_associations": ("MSFT",)})
            if item.chunk_id == "d" else item
            for item in bad.results
        ),
    })
    hybrid = make_hybrid_result(
        mode_requested="reranked", mode_served="reranked",
        dense_override=bad,
    )
    monkeypatch.setattr(four_arm, "_retrieve_hybrid", lambda *a, **k: hybrid)

    run_dir = tmp_path / "run1"
    with pytest.raises(RunnerValidationError, match="wrong-ticker|wrong_ticker"):
        run_four_arm_cases(
            db=fresh_runner_db(tmp_path),
            lancedb_table=fresh_lance_table(tmp_path),
            cases=[_case()], run_id="run1", output_root=tmp_path,
            identities=_identities(), boundary=_mock_boundary(),
            query_embedding_fn=MockQueryEmbedder().embed_query,
            reranker=None,
            case_pack_id=FULL_CASE_PACK_ID,
        )
    assert not (run_dir / "arms").exists()
    assert not (run_dir / "meta.json").exists()


def test_runner_rejects_malformed_cutoff(tmp_path, monkeypatch):
    import catalyst_eval.post_import.four_arm as four_arm
    from catalyst_data.retrieval.result import RetrievalContractError

    def bad_hybrid(*args, **kwargs):
        raise RetrievalContractError("invalid_cutoff")

    monkeypatch.setattr(four_arm, "_retrieve_hybrid", bad_hybrid)
    run_dir = tmp_path / "run1"
    with pytest.raises(RetrievalContractError, match="invalid_cutoff"):
        run_four_arm_cases(
            db=fresh_runner_db(tmp_path),
            lancedb_table=fresh_lance_table(tmp_path),
            cases=[CasePackCase(
                schema_version=CASE_PACK_SCHEMA_VERSION, case_id="bad", ticker="AAPL",
                session_date="2026-01-15", cutoff="not-a-cutoff", query="q",
                source_set="fixture", golden={"golden_id": "bad"},
            )],
            run_id="run1", output_root=tmp_path,
            identities=_identities(), boundary=_mock_boundary(),
            query_embedding_fn=MockQueryEmbedder().embed_query,
            reranker=None,
            case_pack_id=FULL_CASE_PACK_ID,
        )
    assert not (run_dir / "meta.json").exists()


def test_runner_manifest_mismatch_fails_closed(tmp_path, monkeypatch):
    import catalyst_eval.post_import.four_arm as four_arm
    from catalyst_data.retrieval.result import RetrievalContractError

    def bad_hybrid(*args, **kwargs):
        raise RetrievalContractError("manifest_not_found")

    monkeypatch.setattr(four_arm, "_retrieve_hybrid", bad_hybrid)
    run_dir = tmp_path / "run1"
    with pytest.raises(RetrievalContractError, match="manifest_not_found"):
        run_four_arm_cases(
            db=fresh_runner_db(tmp_path),
            lancedb_table=fresh_lance_table(tmp_path),
            cases=[_case()], run_id="run1", output_root=tmp_path,
            identities=_identities(corpus_manifest_id="b" * 64),
            boundary=_mock_boundary(),
            query_embedding_fn=MockQueryEmbedder().embed_query,
            reranker=None,
            case_pack_id=FULL_CASE_PACK_ID,
        )
    assert not (run_dir / "meta.json").exists()


def test_no_reranker_call_for_hybrid_mode(tmp_path, monkeypatch):
    import catalyst_eval.post_import.four_arm as four_arm

    hybrid_calls = []

    def record_hybrid(*args, **kwargs):
        hybrid_calls.append(kwargs)
        return _ok_hybrid()

    reranker = RecordingReranker()
    monkeypatch.setattr(four_arm, "_retrieve_hybrid", record_hybrid)
    run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=[_case()], run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=reranker,
        case_pack_id=FULL_CASE_PACK_ID,
    )
    # One shared reranked pipeline call; the hybrid arm itself never reranks.
    assert len(hybrid_calls) == 1
    assert hybrid_calls[0]["mode"] == "reranked"
    # Hybrid arm artifact is the fusion order, not a reranked order.
    arm = json.loads((tmp_path / "run1" / "arms" / "B001.json").read_text())
    assert [r["chunk_id"] for r in arm["arms"]["hybrid"]["results"]] == list(LITERAL_HYBRID)


def test_reranked_fallback_preserves_exact_hybrid_order(tmp_path, monkeypatch):
    import catalyst_eval.post_import.four_arm as four_arm

    monkeypatch.setattr(
        four_arm, "_retrieve_hybrid",
        lambda *a, **k: make_hybrid_result(
            mode_requested="reranked", mode_served="hybrid",
            reranked_ids=None, degradation_reasons=("reranker_error",),
        ),
    )

    run_dir = tmp_path / "run1"
    run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=[_case()], run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=RecordingReranker(),
        case_pack_id=FULL_CASE_PACK_ID,
    )
    arm = json.loads((run_dir / "arms" / "B001.json").read_text())
    assert arm["arms"]["reranked"]["mode_requested"] == "reranked"
    assert arm["arms"]["reranked"]["mode_served"] == "hybrid"
    assert [r["chunk_id"] for r in arm["arms"]["reranked"]["results"]] == list(LITERAL_HYBRID)
    assert "reranker_error" in arm["arms"]["reranked"]["degradation_reasons"]


def test_one_arm_failure_is_not_labeled_hybrid(tmp_path, monkeypatch):
    import catalyst_eval.post_import.four_arm as four_arm

    monkeypatch.setattr(
        four_arm, "_retrieve_hybrid",
        lambda *a, **k: make_hybrid_result(
            mode_requested="reranked", mode_served="fts5",
            dense_ids=None, reranked_ids=None,
            degradation_reasons=("dense_unavailable",),
        ),
    )

    run_dir = tmp_path / "run1"
    run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=[_case()], run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=None,
        case_pack_id=FULL_CASE_PACK_ID,
    )
    arm = json.loads((run_dir / "arms" / "B001.json").read_text())
    assert arm["arms"]["dense"]["status"] == "failed"
    assert arm["arms"]["hybrid"]["mode_served"] == "fts5"
    assert "dense_unavailable" in arm["arms"]["hybrid"]["degradation_reasons"]
    assert arm["arms"]["reranked"]["mode_served"] == "fts5"
    assert [r["chunk_id"] for r in arm["arms"]["hybrid"]["results"]] == list(LITERAL_FTS5)


# ---------------------------------------------------------------------------
# T7: identity-bound meta and artifact identity
# ---------------------------------------------------------------------------


def test_meta_json_is_complete_before_success_claim(tmp_path, monkeypatch):
    _monkeypatch_single_call(monkeypatch)

    run_dir = tmp_path / "run1"
    summary = _run_once(tmp_path, monkeypatch)
    meta = json.loads((run_dir / "meta.json").read_text())
    required = {
        "schema_version", "code_revision", "index_build_revision", "git_head",
        "runtime_git_head", "snapshot_id", "corpus_manifest_id", "source_bundle_id",
        "probe_report_id", "postbuild_readiness_id", "index_manifest_id",
        "lancedb_dir", "active_table_name", "case_pack_id", "case_pack_path",
        "embedding_mode", "model_name", "model_revision", "tokenizer_revision",
        "reranker_model", "reranker_revision", "started_at", "completed_at",
        "case_count", "arm_order", "cutoff_ticker_validation",
    }
    assert required <= set(meta)
    assert summary.meta_path == run_dir / "meta.json"
    assert meta["case_count"] == 1
    assert meta["arm_order"] == ["fts5", "dense", "hybrid", "reranked"]
    assert meta["embedding_mode"] == "mock_unit_test"
    assert not list(run_dir.glob("*.tmp"))


def test_all_production_identities_match_approved_set(tmp_path, monkeypatch):
    _monkeypatch_single_call(monkeypatch)

    run_dir = tmp_path / "run1"
    run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=[_case()], run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=None,
        case_pack_id=FULL_CASE_PACK_ID,
    )
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["code_revision"] == APPROVED["code_revision"]
    assert meta["index_build_revision"] == APPROVED["code_revision"]
    assert meta["git_head"] == APPROVED["git_head"]
    assert meta["corpus_manifest_id"] == APPROVED["corpus_manifest_id"]
    assert meta["index_manifest_id"] == APPROVED["index_manifest_id"]
    assert meta["snapshot_id"] == APPROVED["snapshot_id"]
    assert meta["source_bundle_id"] == APPROVED["source_bundle_id"]
    assert meta["probe_report_id"] == APPROVED["probe_report_id"]
    assert meta["postbuild_readiness_id"] == APPROVED["postbuild_readiness_id"]
    assert meta["model_revision"] == APPROVED["model_revision"]
    assert meta["tokenizer_revision"] == APPROVED["tokenizer_revision"]
    assert meta["reranker_model"] == APPROVED["reranker_model"]
    assert meta["reranker_revision"] == APPROVED["reranker_revision"]


def test_index_build_revision_and_runtime_git_head_remain_separate(tmp_path, monkeypatch):
    _monkeypatch_single_call(monkeypatch)

    run_dir = tmp_path / "run1"
    run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=[_case()], run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=None,
        case_pack_id=FULL_CASE_PACK_ID,
    )
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["code_revision"] != meta["git_head"]
    assert "revision_mismatch" in meta
    assert meta["revision_mismatch"]["code_revision"] == APPROVED["code_revision"]
    assert meta["revision_mismatch"]["git_head"] == APPROVED["git_head"]


def test_artifact_id_stable_when_only_time_changes(tmp_path, monkeypatch):
    _monkeypatch_single_call(monkeypatch)

    ids = []
    for idx, started_at in enumerate(("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")):
        out = tmp_path / f"out{idx}"
        run_four_arm_cases(
            db=fresh_runner_db(tmp_path, suffix=f"db{idx}"),
            lancedb_table=fresh_lance_table(tmp_path),
            cases=[_case()], run_id="same-run", output_root=out,
            identities=_identities(), boundary=_mock_boundary(),
            query_embedding_fn=MockQueryEmbedder().embed_query,
            reranker=None,
            started_at=started_at,
            case_pack_id=FULL_CASE_PACK_ID,
        )
        arm = json.loads((out / "same-run" / "arms" / "B001.json").read_text())
        ids.append(arm["artifact_id"])
    assert ids[0] == ids[1]


def test_artifact_id_changes_on_semantic_mutation(tmp_path, monkeypatch):
    import catalyst_eval.post_import.four_arm as four_arm

    def run_one(run_id, fusion_ids):
        monkeypatch.setattr(
            four_arm, "_retrieve_hybrid",
            lambda *a, **k: make_hybrid_result(
                mode_requested="reranked", mode_served="reranked",
                fusion_ids=fusion_ids, reranked_ids=fusion_ids,
            ),
        )
        run_four_arm_cases(
            db=fresh_runner_db(tmp_path, suffix=run_id),
            lancedb_table=fresh_lance_table(tmp_path),
            cases=[_case()], run_id=run_id, output_root=tmp_path,
            identities=_identities(), boundary=_mock_boundary(),
            query_embedding_fn=MockQueryEmbedder().embed_query,
            reranker=None,
            case_pack_id=FULL_CASE_PACK_ID,
        )
        return json.loads((tmp_path / run_id / "arms" / "B001.json").read_text())["artifact_id"]

    id_a = run_one("runA", ("d", "a", "e"))
    id_b = run_one("runB", ("e", "a", "d"))
    assert id_a != id_b


def test_corrupt_artifact_fails_closed(tmp_path, monkeypatch):
    from catalyst_data.retrieval.artifacts import ArtifactValidationError
    from catalyst_data.retrieval.pool import generate_union_pool

    _monkeypatch_single_call(monkeypatch)
    run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=[_case()], run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=None,
        case_pack_id=FULL_CASE_PACK_ID,
    )
    arm_path = tmp_path / "run1" / "arms" / "B001.json"
    raw = json.loads(arm_path.read_text())
    raw["arms"]["dense"]["results"].append(copy.deepcopy(raw["arms"]["dense"]["results"][1]))
    raw["arms"]["dense"]["results"][-1]["rank"] = 3
    arm_path.write_text(json.dumps(raw))
    with pytest.raises((ArtifactValidationError, ValueError)):
        generate_union_pool(arm_path)


# ---------------------------------------------------------------------------
# Amendment P6: real latency and contract-only mode_served
# ---------------------------------------------------------------------------


def test_hybrid_and_reranked_latency_are_real_nonzero(tmp_path, monkeypatch):
    import time

    import catalyst_eval.post_import.four_arm as four_arm

    def slow_hybrid(*args, **kwargs):
        time.sleep(0.01)
        return _ok_hybrid()

    monkeypatch.setattr(four_arm, "_retrieve_hybrid", slow_hybrid)
    run_dir = tmp_path / "run1"
    run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=[_case()], run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=RecordingReranker(),
        case_pack_id=FULL_CASE_PACK_ID,
    )
    arm = json.loads((run_dir / "arms" / "B001.json").read_text())
    assert arm["arms"]["hybrid"]["latency_ms"] > 0
    assert arm["arms"]["reranked"]["latency_ms"] > 0


def test_mode_served_rejects_sql_like(tmp_path, monkeypatch):
    import catalyst_eval.post_import.four_arm as four_arm
    from catalyst_eval.post_import.four_arm import RunnerValidationError

    sql_like = make_result_set("lexical", "sql_like", LITERAL_FTS5)
    hybrid = make_hybrid_result(
        mode_requested="reranked", mode_served="reranked",
        lexical_override=sql_like,
    )
    monkeypatch.setattr(four_arm, "_retrieve_hybrid", lambda *a, **k: hybrid)

    run_dir = tmp_path / "run1"
    with pytest.raises(RunnerValidationError, match="mode_served"):
        run_four_arm_cases(
            db=fresh_runner_db(tmp_path),
            lancedb_table=fresh_lance_table(tmp_path),
            cases=[_case()], run_id="run1", output_root=tmp_path,
            identities=_identities(), boundary=_mock_boundary(),
            query_embedding_fn=MockQueryEmbedder().embed_query,
            reranker=None,
            case_pack_id=FULL_CASE_PACK_ID,
        )
    assert not (run_dir / "arms").exists()


# ---------------------------------------------------------------------------
# Amendment P4: run staging + success token gate
# ---------------------------------------------------------------------------


def test_existing_run_id_is_rejected(tmp_path, monkeypatch):
    _monkeypatch_single_call(monkeypatch)
    (tmp_path / "run1").mkdir()
    with pytest.raises(ValueError, match="exists|already"):
        run_four_arm_cases(
            db=fresh_runner_db(tmp_path),
            lancedb_table=fresh_lance_table(tmp_path),
            cases=[_case()], run_id="run1", output_root=tmp_path,
            identities=_identities(), boundary=_mock_boundary(),
            query_embedding_fn=MockQueryEmbedder().embed_query,
            reranker=None,
            case_pack_id=FULL_CASE_PACK_ID,
        )


def test_mid_run_exception_leaves_no_final_run_dir(tmp_path, monkeypatch):
    import catalyst_eval.post_import.four_arm as four_arm
    from catalyst_eval.post_import.four_arm import RunnerValidationError

    bad = make_result_set("lexical", "fts5", ("a", "b", "c"))
    bad = bad.model_copy(update={
        "results": tuple(
            item.model_copy(update={"available_at": "2026-02-01T00:00:00Z"})
            if item.chunk_id == "b" else item
            for item in bad.results
        ),
    })
    hybrid = make_hybrid_result(
        mode_requested="reranked", mode_served="reranked",
        lexical_override=bad,
    )
    monkeypatch.setattr(four_arm, "_retrieve_hybrid", lambda *a, **k: hybrid)

    with pytest.raises(RunnerValidationError):
        run_four_arm_cases(
            db=fresh_runner_db(tmp_path),
            lancedb_table=fresh_lance_table(tmp_path),
            cases=[_case()], run_id="run1", output_root=tmp_path,
            identities=_identities(), boundary=_mock_boundary(),
            query_embedding_fn=MockQueryEmbedder().embed_query,
            reranker=None,
            case_pack_id=FULL_CASE_PACK_ID,
        )
    assert not (tmp_path / "run1").exists()
    assert not list(tmp_path.glob(".run1*"))


def test_limit_run_never_writes_token(tmp_path, monkeypatch):
    """--limit on the approved pack still never writes the token."""
    summary = _production_run(tmp_path, monkeypatch, limit=1)
    _assert_no_token(tmp_path, summary)


def test_failed_base_arm_never_writes_token(tmp_path, monkeypatch):
    """A failed base arm on the approved pack still never writes the token."""
    import catalyst_eval.post_import.four_arm as four_arm

    cases = _approved_cases()
    evidence_dir, validated, resolved = _real_evidence(tmp_path, cases)
    monkeypatch.setattr(
        four_arm, "_retrieve_hybrid",
        lambda *a, **k: make_hybrid_result(
            mode_requested="reranked", mode_served="fts5",
            dense_ids=None, reranked_ids=None,
            degradation_reasons=("dense_unavailable",),
            ticker=k.get("ticker", "AAPL"),
        ),
    )
    monkeypatch.setattr(four_arm, "_chunk_served_for_case", lambda conn, **k: True)
    summary = run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=cases, run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(embedding_mode="production_pinned", is_mock=False, cuda_available=True),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=None,
        case_pack_id=compute_case_pack_id(cases),
        validated_evidence=validated,
        validated_runtime_identity=resolved,
    )
    _assert_no_token(tmp_path, summary)


def test_mock_run_never_writes_token(tmp_path, monkeypatch):
    _monkeypatch_single_call(monkeypatch)
    summary = run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=[_case()], run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=None,
        case_pack_id=FULL_CASE_PACK_ID,
    )
    assert summary.token_written is False
    assert not (tmp_path / "run1" / "WAVE_TOKEN.txt").exists()


def _approved_cases():
    from pathlib import Path as _Path

    golden = _Path(__file__).resolve().parents[1] / "golden_set"
    return build_smoke_case_pack(golden)


def test_full_production_contract_writes_exact_token(tmp_path, monkeypatch):
    """Only the exact manager-approved 10-case pack with 10/10 evidence can
    write FOUR_ARM_E2E_OK."""
    import catalyst_eval.post_import.four_arm as four_arm

    cases = _approved_cases()
    case_pack_id = compute_case_pack_id(cases)
    assert case_pack_id == APPROVED_T4_CONTRACT.approved_case_pack_id
    assert len(cases) == APPROVED_T4_CONTRACT.expected_case_count
    monkeypatch.setattr(
        four_arm, "_retrieve_hybrid",
        lambda *a, **k: make_hybrid_result(ticker=k.get("ticker", "AAPL")),
    )
    # Smoke cases use cutoffs absent from the fixture DB; the token-gate
    # behavior under test is the approved-pack contract, not DB serving.
    monkeypatch.setattr(four_arm, "_chunk_served_for_case", lambda conn, **k: True)
    evidence_dir, validated, resolved = _real_evidence(tmp_path, cases)
    summary = run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=cases, run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(embedding_mode="production_pinned", is_mock=False, cuda_available=True),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=None,
        case_pack_id=case_pack_id,
        validated_evidence=validated,
        validated_runtime_identity=resolved,
    )
    assert summary.token_written is True
    assert (tmp_path / "run1" / "WAVE_TOKEN.txt").read_text().strip() == "FOUR_ARM_E2E_OK"


def test_nine_case_pack_fails_at_library_production_boundary(tmp_path, monkeypatch):
    """A 9-case pack cannot pass the library production boundary."""
    cases = _approved_cases()[:9]
    with pytest.raises(ValueError, match="exactly 10|case count"):
        run_four_arm_cases(
            db=fresh_runner_db(tmp_path),
            lancedb_table=fresh_lance_table(tmp_path),
            cases=cases, run_id="run1", output_root=tmp_path,
            identities=_identities(), boundary=_mock_boundary(embedding_mode="production_pinned", is_mock=False, cuda_available=True),
            query_embedding_fn=MockQueryEmbedder().embed_query,
            reranker=None,
            case_pack_id=compute_case_pack_id(cases),
        )
    assert not (tmp_path / "run1").exists()


def test_non_approved_10_case_pack_fails_at_library_production_boundary(tmp_path, monkeypatch):
    """An arbitrary 10-case pack (not the approved one) cannot pass the library boundary."""
    cases = [replace(c, case_id=f"B{i:03d}") for i, c in enumerate(_approved_cases(), start=1)]
    with pytest.raises(ValueError, match="order|approved|contract"):
        run_four_arm_cases(
            db=fresh_runner_db(tmp_path),
            lancedb_table=fresh_lance_table(tmp_path),
            cases=cases, run_id="run1", output_root=tmp_path,
            identities=_identities(), boundary=_mock_boundary(embedding_mode="production_pinned", is_mock=False, cuda_available=True),
            query_embedding_fn=MockQueryEmbedder().embed_query,
            reranker=None,
            case_pack_id=compute_case_pack_id(cases),
        )
    assert not (tmp_path / "run1").exists()


def test_case_pack_id_mismatch_never_writes_token(tmp_path, monkeypatch):
    """The approved pack with a forged case_pack_id param cannot write the token."""
    summary = _production_run(tmp_path, monkeypatch, case_pack_id="9" * 64)
    _assert_no_token(tmp_path, summary)


def test_partial_case_count_never_writes_token(tmp_path, monkeypatch):
    """A full_case_count larger than the executed pack cannot write the token."""
    summary = _production_run(tmp_path, monkeypatch, full_case_count=11)
    _assert_no_token(tmp_path, summary)


def test_token_requires_validated_evidence_object(tmp_path, monkeypatch):
    summary = _production_run(tmp_path, monkeypatch, evidence=None)
    _assert_no_token(tmp_path, summary)


def test_token_requires_validated_runtime_identity_object(tmp_path, monkeypatch):
    summary = _production_run(tmp_path, monkeypatch, runtime=None)
    _assert_no_token(tmp_path, summary)


def test_token_rejects_runtime_identity_mismatch(tmp_path, monkeypatch):
    """Validated runtime identity corpus manifest must match run identities."""
    _evidence_dir, validated, resolved = _real_evidence(tmp_path)
    runtime = replace(resolved, corpus_manifest_id="e" * 64)
    summary = _production_run(tmp_path, monkeypatch, runtime=runtime)
    _assert_no_token(tmp_path, summary)


# ---------------------------------------------------------------------------
# Embedding/reranker boundary (mock vs production)
# ---------------------------------------------------------------------------


def test_production_pinned_rejects_mock_embedder():
    with pytest.raises(ValueError, match="mock"):
        validate_embedding_boundary(_mock_boundary(embedding_mode="production_pinned"))


def test_production_pinned_rejects_wrong_revision():
    with pytest.raises(ValueError, match="revision"):
        validate_embedding_boundary(_mock_boundary(
            embedding_mode="production_pinned", is_mock=False,
            model_revision="0" * 40,
        ))


def test_production_pinned_rejects_wrong_tokenizer_revision():
    with pytest.raises(ValueError, match="tokenizer"):
        validate_embedding_boundary(_mock_boundary(
            embedding_mode="production_pinned", is_mock=False,
            tokenizer_revision="0" * 40,
        ))


def test_production_pinned_rejects_wrong_dimension():
    with pytest.raises(ValueError, match="dimension"):
        validate_embedding_boundary(_mock_boundary(
            embedding_mode="production_pinned", is_mock=False, dimension=512,
        ))


def test_production_pinned_rejects_missing_cuda():
    with pytest.raises(RuntimeError, match="CUDA"):
        validate_embedding_boundary(_mock_boundary(
            embedding_mode="production_pinned", is_mock=False, cuda_available=False,
        ))


def test_production_pinned_requires_normalized_vectors():
    """The actual per-case query vector must be L2-normalized (no probe query)."""
    boundary = _mock_boundary(embedding_mode="production_pinned", is_mock=False, cuda_available=True)
    validate_embedding_boundary(boundary)
    with pytest.raises(ValueError, match="normalized"):
        validate_query_vector(np.ones(1024, dtype=np.float32) * 2.0, dimension=1024)
    with pytest.raises(ValueError, match="normalized"):
        validate_query_vector(np.ones(1024, dtype=np.float32), dimension=1024)


def test_query_vector_validation_contract():
    """1-D, dimension=1024, finite, L2-normalized."""
    good = MockQueryEmbedder().embed_query("q")
    validate_query_vector(good, dimension=1024)
    with pytest.raises(ValueError, match="dimension"):
        validate_query_vector(good[:512], dimension=1024)
    with pytest.raises(ValueError, match="finite"):
        validate_query_vector(np.array([np.inf] * 1024, dtype=np.float32), dimension=1024)


def test_production_embedding_called_exactly_n_times_no_probe_query(tmp_path, monkeypatch):
    """AMEND-2 P6: N cases => exactly N embedding calls; no extra probe-query."""
    import catalyst_eval.post_import.four_arm as four_arm

    cases = _approved_cases()
    evidence_dir, validated, resolved = _real_evidence(tmp_path, cases)
    calls: list[str] = []

    def embedding_fn(query):
        calls.append(query)
        return MockQueryEmbedder().embed_query(query)

    monkeypatch.setattr(
        four_arm, "_retrieve_hybrid",
        lambda *a, **k: _ok_hybrid(ticker=k.get("ticker", "AAPL")),
    )
    monkeypatch.setattr(four_arm, "_chunk_served_for_case", lambda conn, **k: True)
    run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=cases, run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(embedding_mode="production_pinned", is_mock=False, cuda_available=True),
        query_embedding_fn=embedding_fn,
        reranker=None,
        case_pack_id=compute_case_pack_id(cases),
        validated_evidence=validated,
        validated_runtime_identity=resolved,
    )
    assert len(calls) == 10
    assert all("probe-query" not in call for call in calls)


def test_mock_unit_test_requires_mock_embedder():
    with pytest.raises(ValueError, match="mock_unit_test"):
        validate_embedding_boundary(_mock_boundary(is_mock=False))


def test_mock_unit_test_allowed():
    validate_embedding_boundary(_mock_boundary())


def test_degraded_mode_fails_closed_without_manager_authorization():
    with pytest.raises(ValueError, match="manager authorization|degraded"):
        validate_embedding_boundary(_mock_boundary(
            embedding_mode="degraded", is_mock=False, cuda_available=True,
        ))


def test_degraded_mode_requires_authorization_artifact(tmp_path):
    boundary = _mock_boundary(
        embedding_mode="degraded", is_mock=False, cuda_available=True,
    )
    missing = tmp_path / "missing-auth.json"
    with pytest.raises(ValueError, match="manager authorization"):
        validate_embedding_boundary(
            boundary, manager_authorization_path=missing,
        )
    auth = tmp_path / "manager-auth.json"
    auth.write_text(json.dumps({"authorized": True}))
    validate_embedding_boundary(
        boundary, manager_authorization_path=auth,
    )


# ---------------------------------------------------------------------------
# AMEND-4 Task 1: batch reranker single-flight (shared RerankerGate)
# ---------------------------------------------------------------------------


def test_four_arm_batch_reuses_one_reranker_gate(tmp_path, monkeypatch):
    """Every case must receive the same non-None RerankerGate."""
    import catalyst_eval.post_import.four_arm as four_arm

    seen_gates: list = []

    def recording_hybrid(*args, **kwargs):
        seen_gates.append(kwargs.get("reranker_gate"))
        return _ok_hybrid()

    monkeypatch.setattr(four_arm, "_retrieve_hybrid", recording_hybrid)
    cases = [_case("B001"), _case("B002")]
    run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=cases, run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=RecordingReranker(),
        case_pack_id=FULL_CASE_PACK_ID,
    )
    assert len(seen_gates) == 2
    assert all(gate is not None for gate in seen_gates)
    assert seen_gates[0] is seen_gates[1]


def test_four_arm_timeout_never_starts_second_reranker_worker(tmp_path, monkeypatch):
    """A timed-out inference blocks a second case: reranker_busy, max 1 worker."""
    import time

    import catalyst_eval.post_import.four_arm as four_arm
    from catalyst_data.retrieval.reranker import RerankerGate

    class SlowReranker:
        def score(self, query, candidates):
            time.sleep(0.25)
            return [float(100 - i) for i in range(len(candidates))]

    started_workers: list = []
    original_acquire = RerankerGate.acquire

    def spy_acquire(self, *, target, name):
        worker = original_acquire(self, target=target, name=name)
        started_workers.append(worker)
        return worker

    monkeypatch.setattr(RerankerGate, "acquire", spy_acquire)
    cases = [_case("B001"), _case("B002")]
    summary = run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=cases, run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=SlowReranker(),
        reranker_timeout_seconds=0.05,
        case_pack_id=FULL_CASE_PACK_ID,
    )
    run_dir = tmp_path / "run1"
    first_arm = json.loads((run_dir / "arms" / "B001.json").read_text())
    second_arm = json.loads((run_dir / "arms" / "B002.json").read_text())
    assert "reranker_timeout" in first_arm["arms"]["reranked"]["degradation_reasons"]
    assert "reranker_busy" in second_arm["arms"]["reranked"]["degradation_reasons"]
    # Exactly one worker ever starts; the second acquire is refused (None).
    started = [worker for worker in started_workers if worker is not None]
    assert len(started) == 1
    assert len(started_workers) == 2
    assert started_workers[1] is None
    assert summary.token_written is False
    assert not (run_dir / "WAVE_TOKEN.txt").exists()


# ---------------------------------------------------------------------------
# AMEND-4 Task 2: caller-forgeable validated evidence/runtime fails closed
# ---------------------------------------------------------------------------


def _evidence_kwargs() -> dict:
    return {
        "db_sha256": APPROVED["db_sha256"],
        "corpus_manifest_id": APPROVED["corpus_manifest_id"],
        "snapshot_id": APPROVED["snapshot_id"],
        "source_bundle_id": APPROVED["source_bundle_id"],
        "probe_report_id": APPROVED["probe_report_id"],
        "postbuild_readiness_id": APPROVED["postbuild_readiness_id"],
        "index_manifest_id": APPROVED["index_manifest_id"],
        "db_path": APPROVED["db_path"],
        "db_user_version": APPROVED["db_user_version"],
        "db_foreign_key_violations": APPROVED["db_foreign_key_violations"],
        "lancedb_dir": APPROVED["lancedb_dir"],
        "active_table_name": APPROVED["active_table_name"],
        "model_name": APPROVED["model_name"],
        "model_revision": APPROVED["model_revision"],
        "tokenizer_revision": APPROVED["tokenizer_revision"],
        "dimension": APPROVED["dimension"],
        "dtype": APPROVED["dtype"],
        "normalization_mode": APPROVED["normalization_mode"],
        "embedding_mode": "mock_unit_test",
    }


def _real_evidence(tmp_path, cases=None):
    """Build a real T4 evidence directory on disk and validate it for real."""
    from catalyst_eval.post_import.case_pack import write_case_pack
    from catalyst_eval.post_import.probe import (
        CaseProbeResult,
        ServedCorpusProbeReport,
        write_probe_evidence,
    )
    from catalyst_eval.post_import.t4_evidence import validate_t4_evidence

    cases = cases if cases is not None else _approved_cases()
    evidence_dir = tmp_path / "evidence"
    write_case_pack(cases, evidence_dir / "case_pack.jsonl")
    report = ServedCorpusProbeReport(
        schema_version="served_corpus_probe_v1",
        corpus_manifest_id=APPROVED["corpus_manifest_id"],
        case_count=len(cases), passed_count=len(cases), all_passed=True,
        per_case=tuple(CaseProbeResult(c.case_id, c.ticker, c.cutoff, 1) for c in cases),
    )
    write_probe_evidence(
        report, run_dir=evidence_dir,
        case_pack_id=compute_case_pack_id(cases),
        case_pack_path="case_pack.jsonl",
        runtime_git_head=APPROVED["git_head"],
        index_build_code_revision=APPROVED["code_revision"],
        **_evidence_kwargs(),
    )
    resolved = _validated_runtime()
    validated = validate_t4_evidence(
        evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved,
    )
    return evidence_dir, validated, resolved


_USE_VALIDATED = object()


def _production_run(tmp_path, monkeypatch, *, cases=None, evidence=_USE_VALIDATED,
                    runtime=_USE_VALIDATED, identities=None, case_pack_id=None,
                    **kwargs):
    import catalyst_eval.post_import.four_arm as four_arm

    cases = cases if cases is not None else _approved_cases()
    _evidence_dir, validated, resolved = _real_evidence(tmp_path, cases)
    monkeypatch.setattr(
        four_arm, "_retrieve_hybrid",
        lambda *a, **k: _ok_hybrid(ticker=k.get("ticker", "AAPL")),
    )
    monkeypatch.setattr(four_arm, "_chunk_served_for_case", lambda conn, **k: True)
    return run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=cases, run_id="run1", output_root=tmp_path,
        identities=identities if identities is not None else _identities(),
        boundary=_mock_boundary(
            embedding_mode="production_pinned", is_mock=False, cuda_available=True,
        ),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=None,
        case_pack_id=case_pack_id if case_pack_id is not None else compute_case_pack_id(cases),
        validated_evidence=validated if evidence is _USE_VALIDATED else evidence,
        validated_runtime_identity=resolved if runtime is _USE_VALIDATED else runtime,
        **kwargs,
    )


def _assert_no_token(tmp_path, summary):
    assert summary.token_written is False
    assert not (tmp_path / "run1" / "WAVE_TOKEN.txt").exists()


def test_poisoned_passed_count_never_writes_token(tmp_path, monkeypatch):
    _evidence_dir, validated, _resolved = _real_evidence(tmp_path)
    summary = _production_run(
        tmp_path, monkeypatch, evidence=replace(validated, passed_count=1),
    )
    _assert_no_token(tmp_path, summary)


def test_poisoned_db_sha256_never_writes_token(tmp_path, monkeypatch):
    _evidence_dir, validated, _resolved = _real_evidence(tmp_path)
    summary = _production_run(
        tmp_path, monkeypatch, evidence=replace(validated, db_sha256="0" * 64),
    )
    _assert_no_token(tmp_path, summary)


@pytest.mark.parametrize("field,value", [
    ("db_user_version", 12),
    ("db_foreign_key_violations", 1),
])
def test_poisoned_db_user_version_and_fk_never_write_token(tmp_path, monkeypatch, field, value):
    _evidence_dir, validated, _resolved = _real_evidence(tmp_path)
    summary = _production_run(
        tmp_path, monkeypatch, evidence=replace(validated, **{field: value}),
    )
    _assert_no_token(tmp_path, summary)


def test_poisoned_runtime_git_head_never_writes_token(tmp_path, monkeypatch):
    _evidence_dir, validated, resolved = _real_evidence(tmp_path)
    poisoned_runtime = replace(resolved, git_head="0" * 40)
    summary = _production_run(tmp_path, monkeypatch, runtime=poisoned_runtime)
    _assert_no_token(tmp_path, summary)


@pytest.mark.parametrize("field,value", [
    ("vector_count", 1),
    ("lancedb_row_count", 1),
])
def test_poisoned_runtime_vector_count_and_row_count_never_write_token(tmp_path, monkeypatch, field, value):
    _evidence_dir, validated, resolved = _real_evidence(tmp_path)
    poisoned_runtime = replace(resolved, **{field: value})
    summary = _production_run(tmp_path, monkeypatch, runtime=poisoned_runtime)
    _assert_no_token(tmp_path, summary)


@pytest.mark.parametrize("field,value", [
    ("model_revision", "0" * 40),
    ("tokenizer_revision", "0" * 40),
])
def test_poisoned_model_and_tokenizer_revision_never_write_token(tmp_path, monkeypatch, field, value):
    _evidence_dir, validated, _resolved = _real_evidence(tmp_path)
    summary = _production_run(
        tmp_path, monkeypatch, evidence=replace(validated, **{field: value}),
    )
    _assert_no_token(tmp_path, summary)


@pytest.mark.parametrize("field,value", [
    ("dimension", 512),
    ("dtype", "float64"),
    ("normalization_mode", "none"),
])
def test_poisoned_vector_contract_never_writes_token(tmp_path, monkeypatch, field, value):
    _evidence_dir, validated, _resolved = _real_evidence(tmp_path)
    summary = _production_run(
        tmp_path, monkeypatch, evidence=replace(validated, **{field: value}),
    )
    _assert_no_token(tmp_path, summary)


def test_reordered_approved_cases_fail_at_library_production_boundary(tmp_path, monkeypatch):
    """Reordering the approved pack must fail closed at the library boundary."""
    cases = _approved_cases()
    reordered = [cases[1], cases[0], *cases[2:]]
    evidence_dir, validated, resolved = _real_evidence(tmp_path, cases)
    with pytest.raises(ValueError, match="order|approved|contract"):
        run_four_arm_cases(
            db=fresh_runner_db(tmp_path),
            lancedb_table=fresh_lance_table(tmp_path),
            cases=reordered, run_id="run1", output_root=tmp_path,
            identities=_identities(),
            boundary=_mock_boundary(
                embedding_mode="production_pinned", is_mock=False, cuda_available=True,
            ),
            query_embedding_fn=MockQueryEmbedder().embed_query,
            reranker=None,
            case_pack_id=compute_case_pack_id(reordered),
            validated_evidence=validated,
            validated_runtime_identity=resolved,
        )
    assert not (tmp_path / "run1").exists()


def test_validated_object_not_backed_by_real_evidence_dir_never_writes_token(tmp_path, monkeypatch):
    _evidence_dir, validated, _resolved = _real_evidence(tmp_path)
    forged = replace(validated, evidence_dir=tmp_path / "missing_evidence")
    summary = _production_run(tmp_path, monkeypatch, evidence=forged)
    _assert_no_token(tmp_path, summary)


# ---------------------------------------------------------------------------
# AMEND-4 Task 7: reranker timeout configuration recording
# ---------------------------------------------------------------------------


def test_meta_records_reranker_timeout_seconds(tmp_path, monkeypatch):
    _monkeypatch_single_call(monkeypatch)
    summary = run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=[_case()], run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=RecordingReranker(),
        reranker_timeout_seconds=2.0,
        case_pack_id=FULL_CASE_PACK_ID,
    )
    meta = json.loads((tmp_path / "run1" / "meta.json").read_text())
    assert meta["reranker_timeout_seconds"] == 2.0


def test_arm_retrieval_config_records_reranker_timeout_seconds(tmp_path, monkeypatch):
    _monkeypatch_single_call(monkeypatch)
    run_four_arm_cases(
        db=fresh_runner_db(tmp_path),
        lancedb_table=fresh_lance_table(tmp_path),
        cases=[_case()], run_id="run1", output_root=tmp_path,
        identities=_identities(), boundary=_mock_boundary(),
        query_embedding_fn=MockQueryEmbedder().embed_query,
        reranker=RecordingReranker(),
        reranker_timeout_seconds=1.5,
        case_pack_id=FULL_CASE_PACK_ID,
    )
    arm = json.loads((tmp_path / "run1" / "arms" / "B001.json").read_text())
    assert arm["retrieval_config"]["reranker_timeout_seconds"] == 1.5


@pytest.mark.parametrize("bad_timeout", [0.0, -1.0])
def test_non_positive_reranker_timeout_rejected_before_case_loop(
    tmp_path, monkeypatch, bad_timeout,
):
    """Non-positive timeouts must be rejected before any embedding/retrieval."""
    import catalyst_eval.post_import.four_arm as four_arm

    calls: list[str] = []
    monkeypatch.setattr(four_arm, "_retrieve_hybrid", lambda *a, **k: _ok_hybrid())

    def embedding_fn(query):
        calls.append(query)
        return MockQueryEmbedder().embed_query(query)

    with pytest.raises(ValueError, match="timeout"):
        run_four_arm_cases(
            db=fresh_runner_db(tmp_path),
            lancedb_table=fresh_lance_table(tmp_path),
            cases=[_case()], run_id="run1", output_root=tmp_path,
            identities=_identities(), boundary=_mock_boundary(),
            query_embedding_fn=embedding_fn,
            reranker=RecordingReranker(),
            reranker_timeout_seconds=bad_timeout,
            case_pack_id=FULL_CASE_PACK_ID,
        )
    assert calls == []
    assert not (tmp_path / "run1").exists()
    assert not list(tmp_path.glob(".run1*"))

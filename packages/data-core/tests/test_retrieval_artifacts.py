from __future__ import annotations

import copy
import json

import pytest

from retrieval_model_fixtures import (
    EXPECTED_ARM_ARTIFACT_ID,
    FOUR_COMPLETE_ARM_RESULTS,
    SEMANTIC_ARTIFACT_MUTATIONS,
    make_arm_artifact,
)
from catalyst_data.config import BGE_M3_REVISION, BGE_RERANKER_REVISION


def test_arm_artifact_path_schema_and_identity_are_exact(tmp_path):
    from catalyst_data.retrieval.artifacts import load_arm_artifact, write_arm_artifact

    path = write_arm_artifact(
        root=tmp_path, run_id="run-1", case_id="B001",
        query="AAPL earnings", cutoff_ts="2026-01-15T21:00:00Z",
        filters={"ticker": "AAPL", "evidence_types": [], "source_classes": [], "corpus_manifest_id": "a" * 64, "index_manifest_id": "1" * 64},
        retrieval_config={"lexical_top_k": 20, "dense_top_k": 20, "fusion_k": 60, "fused_top_k": 20, "display_top_k": 8, "embedding_revision": BGE_M3_REVISION, "reranker_revision": BGE_RERANKER_REVISION, "reranker_timeout_seconds": 2.0},
        arms=FOUR_COMPLETE_ARM_RESULTS,
        created_at="2026-07-22T00:00:00Z",
    )
    loaded = load_arm_artifact(path)
    assert path == tmp_path / "run-1" / "B001.json"
    from catalyst_data.retrieval.artifacts import ARM_ARTIFACT_SCHEMA_VERSION

    assert loaded.schema_version == ARM_ARTIFACT_SCHEMA_VERSION
    assert list(loaded.arms) == ["fts5", "dense", "hybrid", "reranked"]
    assert loaded.artifact_id
    assert not list(path.parent.glob("*.tmp"))


def test_artifact_id_matches_independent_literal_golden():
    """The canonical fixture artifact id must equal the hardcoded literal
    golden (not a production-derived expected value)."""
    from catalyst_data.retrieval.artifacts import compute_arm_artifact_id

    assert compute_arm_artifact_id(make_arm_artifact()) == EXPECTED_ARM_ARTIFACT_ID


def test_artifact_id_excludes_time_but_bites_on_results(tmp_path):
    from catalyst_data.retrieval.artifacts import compute_arm_artifact_id

    base = make_arm_artifact()
    timing_only = copy.deepcopy(base)
    timing_only["created_at"] = "2026-07-23T00:00:00Z"
    timing_only["arms"]["fts5"]["latency_ms"] = 999.0
    assert compute_arm_artifact_id(base) == compute_arm_artifact_id(timing_only)
    changed = copy.deepcopy(base)
    changed["arms"]["fts5"]["results"][0]["chunk_id"] = "changed"
    assert compute_arm_artifact_id(base) != compute_arm_artifact_id(changed)


def test_artifact_rejects_duplicate_chunks_wrong_order_and_unknown_mode(tmp_path):
    from catalyst_data.retrieval.artifacts import ArtifactValidationError, write_arm_artifact

    arms = copy.deepcopy(FOUR_COMPLETE_ARM_RESULTS)
    arms["dense"]["results"] = [{"chunk_id": "same", "rank": 1}, {"chunk_id": "same", "rank": 2}]
    with pytest.raises(ArtifactValidationError):
        write_arm_artifact(root=tmp_path, run_id="r", case_id="c", query="q", cutoff_ts="2026-01-01T00:00:00Z", filters={}, retrieval_config={}, arms=arms)


def test_artifact_rejects_missing_contract_fields(tmp_path):
    from catalyst_data.retrieval.artifacts import ArtifactValidationError, write_arm_artifact

    arms = copy.deepcopy(FOUR_COMPLETE_ARM_RESULTS)
    del arms["dense"]["results"][0]["document_id"]
    with pytest.raises(ArtifactValidationError, match="result fields"):
        write_arm_artifact(
            root=tmp_path, run_id="r", case_id="c", query="q",
            cutoff_ts="2026-01-01T00:00:00Z",
        filters={"ticker": "AAPL", "evidence_types": [], "source_classes": [], "corpus_manifest_id": "a" * 64, "index_manifest_id": "1" * 64},
            retrieval_config={"lexical_top_k": 20, "dense_top_k": 20, "fusion_k": 60, "fused_top_k": 20, "display_top_k": 8, "embedding_revision": BGE_M3_REVISION, "reranker_revision": BGE_RERANKER_REVISION, "reranker_timeout_seconds": 2.0}, arms=arms,
        )


def test_artifact_rejects_unpinned_retrieval_config(tmp_path):
    from catalyst_data.retrieval.artifacts import ArtifactValidationError, write_arm_artifact

    with pytest.raises(ArtifactValidationError, match="retrieval_config"):
        write_arm_artifact(
            root=tmp_path, run_id="r", case_id="c", query="q",
            cutoff_ts="2026-01-01T00:00:00Z",
        filters={"ticker": "AAPL", "evidence_types": [], "source_classes": [], "corpus_manifest_id": "a" * 64, "index_manifest_id": "1" * 64},
            retrieval_config={}, arms=FOUR_COMPLETE_ARM_RESULTS,
        )

    arms = copy.deepcopy(FOUR_COMPLETE_ARM_RESULTS)
    arms["dense"]["mode_served"] = "unknown"
    with pytest.raises(ArtifactValidationError):
        write_arm_artifact(root=tmp_path, run_id="r", case_id="c", query="q", cutoff_ts="2026-01-01T00:00:00Z", filters={}, retrieval_config={}, arms=arms)


def test_artifact_accepts_optional_identity_filter_chain(tmp_path):
    """Optional identity filters carry the candidate chain on the arm artifact."""
    from catalyst_data.retrieval.artifacts import load_arm_artifact, write_arm_artifact

    filters = {
        "ticker": "AAPL", "evidence_types": [], "source_classes": [],
        "corpus_manifest_id": "a" * 64, "index_manifest_id": "1" * 64,
        "build_id": "b" * 64, "source_bundle_id": "c" * 64,
        "snapshot_id": "d" * 64, "probe_report_id": "e" * 64,
        "postbuild_readiness_id": "f" * 64, "table_name": "candidate_abcd",
        "model_name": "BAAI/bge-m3", "model_revision": "g" * 40,
        "embedding_dimension": 1024, "reranker_model": "BAAI/bge-reranker-v2-m3",
        "reranker_revision": "h" * 40,
    }
    path = write_arm_artifact(
        root=tmp_path, run_id="run-cand", case_id="B001",
        query="AAPL earnings", cutoff_ts="2026-01-15T21:00:00Z",
        filters=filters,
        retrieval_config={"lexical_top_k": 20, "dense_top_k": 20, "fusion_k": 60, "fused_top_k": 20, "display_top_k": 8, "embedding_revision": BGE_M3_REVISION, "reranker_revision": BGE_RERANKER_REVISION, "reranker_timeout_seconds": 2.0},
        arms=FOUR_COMPLETE_ARM_RESULTS,
        created_at="2026-07-22T00:00:00Z",
    )
    loaded = load_arm_artifact(path)
    assert loaded.filters == filters
    assert loaded.artifact_id == load_arm_artifact(path).artifact_id


def test_artifact_rejects_unknown_filter_key(tmp_path):
    from catalyst_data.retrieval.artifacts import ArtifactValidationError, write_arm_artifact

    filters = {
        "ticker": "AAPL", "evidence_types": [], "source_classes": [],
        "corpus_manifest_id": "a" * 64, "index_manifest_id": "1" * 64,
        "not_a_real_filter": "x",
    }
    with pytest.raises(ArtifactValidationError, match="filters fields mismatch"):
        write_arm_artifact(
            root=tmp_path, run_id="r", case_id="c", query="q",
            cutoff_ts="2026-01-01T00:00:00Z", filters=filters,
            retrieval_config={"lexical_top_k": 20, "dense_top_k": 20, "fusion_k": 60, "fused_top_k": 20, "display_top_k": 8, "embedding_revision": BGE_M3_REVISION, "reranker_revision": BGE_RERANKER_REVISION, "reranker_timeout_seconds": 2.0},
            arms=FOUR_COMPLETE_ARM_RESULTS,
        )


def test_artifact_rejects_missing_or_string_rank(tmp_path):
    from catalyst_data.retrieval.artifacts import ArtifactValidationError, write_arm_artifact

    for mutation in (lambda item: item.pop("rank"), lambda item: item.__setitem__("rank", "1")):
        arms = copy.deepcopy(FOUR_COMPLETE_ARM_RESULTS)
        mutation(arms["dense"]["results"][0])
        with pytest.raises(ArtifactValidationError, match="rank"):
            write_arm_artifact(
                root=tmp_path, run_id="r", case_id="c", query="q",
                cutoff_ts="2026-01-01T00:00:00Z",
        filters={"ticker": "AAPL", "evidence_types": [], "source_classes": [], "corpus_manifest_id": "a" * 64, "index_manifest_id": "1" * 64},
                retrieval_config={"lexical_top_k": 20, "dense_top_k": 20, "fusion_k": 60, "fused_top_k": 20, "display_top_k": 8, "embedding_revision": BGE_M3_REVISION, "reranker_revision": BGE_RERANKER_REVISION, "reranker_timeout_seconds": 2.0},
                arms=arms,
            )


def test_artifact_id_changes_for_every_semantic_mutation():
    """Every semantic mutation must change artifact_id (literal fixture set)."""
    from catalyst_data.retrieval.artifacts import compute_arm_artifact_id

    base = make_arm_artifact()
    base_id = compute_arm_artifact_id(base)
    for mutation in SEMANTIC_ARTIFACT_MUTATIONS:
        changed = mutation(copy.deepcopy(base))
        assert compute_arm_artifact_id(changed) != base_id


def test_legacy_1_0_0_rejected_as_incompatible(tmp_path):
    from catalyst_data.retrieval.artifacts import (
        ARM_ARTIFACT_SCHEMA_VERSION,
        ArtifactValidationError,
        load_arm_artifact,
    )

    payload = make_arm_artifact()
    payload["schema_version"] = "1.0.0"
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="legacy incompatible"):
        load_arm_artifact(path)
    assert ARM_ARTIFACT_SCHEMA_VERSION == "1.1.0"


def _write_scored_production_artifact(tmp_path, **kwargs):
    """Write a mode_served=reranked arm with valid scores/ranks (production shape)."""
    from catalyst_data.retrieval.artifacts import write_arm_artifact

    arms = copy.deepcopy(FOUR_COMPLETE_ARM_RESULTS)
    for position, result in enumerate(arms["reranked"]["results"], start=1):
        result["reranker_score"] = float(10 - position)
        result["reranker_rank"] = position
    path = write_arm_artifact(
        root=tmp_path, run_id="run-1", case_id="B001",
        query="AAPL earnings", cutoff_ts="2026-01-15T21:00:00Z",
        filters={"ticker": "AAPL", "evidence_types": [], "source_classes": [],
                 "corpus_manifest_id": "a" * 64, "index_manifest_id": "1" * 64},
        retrieval_config={"lexical_top_k": 20, "dense_top_k": 20, "fusion_k": 60,
                          "fused_top_k": 20, "display_top_k": 8,
                          "embedding_revision": BGE_M3_REVISION,
                          "reranker_revision": BGE_RERANKER_REVISION,
                          "reranker_timeout_seconds": 2.0},
        arms=arms,
        created_at="2026-07-22T00:00:00Z",
        **kwargs,
    )
    return path, arms


def test_effect_metrics_tamper_fails_closed(tmp_path):
    from catalyst_data.retrieval.artifacts import (
        ArtifactValidationError,
        load_arm_artifact,
    )

    path, _ = _write_scored_production_artifact(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    for key in ("lexical_count", "reranker_output_count", "reranker_input_count"):
        tampered = copy.deepcopy(raw)
        tampered["effect_metrics"][key] = 999
        path.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(ArtifactValidationError):
            load_arm_artifact(path)
    # provenance order tamper
    tampered = copy.deepcopy(raw)
    if tampered["effect_metrics"]["reranker_provenance"]:
        tampered["effect_metrics"]["reranker_provenance"][0]["chunk_id"] = "tampered"
        path.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(ArtifactValidationError):
            load_arm_artifact(path)


def test_production_reranked_null_score_fails_load(tmp_path):
    """mode_served=reranked+ok must reject missing/null reranker_score on any hit."""
    from catalyst_data.retrieval.artifacts import (
        ArtifactValidationError,
        compute_arm_artifact_id,
        load_arm_artifact,
    )

    path, _ = _write_scored_production_artifact(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Null one score; keep provenance consistent so only the score gate fires.
    raw["arms"]["reranked"]["results"][0]["reranker_score"] = None
    raw["effect_metrics"]["reranker_provenance"][0]["reranker_score"] = None
    raw["artifact_id"] = compute_arm_artifact_id(raw)
    path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="reranker_score"):
        load_arm_artifact(path)


def test_production_reranked_all_null_scores_fail_load(tmp_path):
    """All-null scores must not slip through any()-partial heuristics."""
    from catalyst_data.retrieval.artifacts import (
        ArtifactValidationError,
        compute_arm_artifact_id,
        load_arm_artifact,
    )

    path, _ = _write_scored_production_artifact(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    for result, prov in zip(
        raw["arms"]["reranked"]["results"],
        raw["effect_metrics"]["reranker_provenance"],
        strict=True,
    ):
        result["reranker_score"] = None
        result["reranker_rank"] = None
        prov["reranker_score"] = None
        prov["reranker_rank"] = None
    raw["artifact_id"] = compute_arm_artifact_id(raw)
    path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="reranker_score|reranker_rank"):
        load_arm_artifact(path)


def test_production_reranked_null_rank_fails_load(tmp_path):
    from catalyst_data.retrieval.artifacts import (
        ArtifactValidationError,
        compute_arm_artifact_id,
        load_arm_artifact,
    )

    path, _ = _write_scored_production_artifact(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["arms"]["reranked"]["results"][1]["reranker_rank"] = None
    raw["effect_metrics"]["reranker_provenance"][1]["reranker_rank"] = None
    raw["artifact_id"] = compute_arm_artifact_id(raw)
    path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="reranker_rank"):
        load_arm_artifact(path)


def test_mode_served_reranked_ok_without_scores_fails_write(tmp_path):
    """write_arm_artifact must reject reranked+ok results lacking scores/ranks."""
    from catalyst_data.retrieval.artifacts import ArtifactValidationError, write_arm_artifact

    arms = copy.deepcopy(FOUR_COMPLETE_ARM_RESULTS)
    assert arms["reranked"]["mode_served"] == "reranked"
    assert arms["reranked"]["status"] == "ok"
    # Force all-null scores/ranks — successful reranked serve is invalid.
    for result in arms["reranked"]["results"]:
        result["reranker_score"] = None
        result["reranker_rank"] = None
    with pytest.raises(ArtifactValidationError, match="reranker_score|reranker_rank"):
        write_arm_artifact(
            root=tmp_path, run_id="run-1", case_id="B001",
            query="AAPL earnings", cutoff_ts="2026-01-15T21:00:00Z",
            filters={"ticker": "AAPL", "evidence_types": [], "source_classes": [],
                     "corpus_manifest_id": "a" * 64, "index_manifest_id": "1" * 64},
            retrieval_config={"lexical_top_k": 20, "dense_top_k": 20, "fusion_k": 60,
                              "fused_top_k": 20, "display_top_k": 8,
                              "embedding_revision": BGE_M3_REVISION,
                              "reranker_revision": BGE_RERANKER_REVISION,
                              "reranker_timeout_seconds": 2.0},
            arms=arms,
        )


# ── AMEND-5.2: finite reranker scores (reject None/bool/NaN/±Inf) ─────────────

_SENTINEL = object()


def _tamper_score_and_provenance(path, score_value, *, rank_value=_SENTINEL):
    from catalyst_data.retrieval.artifacts import compute_arm_artifact_id

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["arms"]["reranked"]["results"][0]["reranker_score"] = score_value
    raw["effect_metrics"]["reranker_provenance"][0]["reranker_score"] = score_value
    if rank_value is not _SENTINEL:
        raw["arms"]["reranked"]["results"][0]["reranker_rank"] = rank_value
        raw["effect_metrics"]["reranker_provenance"][0]["reranker_rank"] = rank_value
        # Keep result.rank contiguous/order; only reranker_rank is tampered when needed.
    raw["artifact_id"] = compute_arm_artifact_id(raw)
    path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    return raw


@pytest.mark.parametrize(
    "bad_score",
    [None, True, False, float("nan"), float("inf"), float("-inf")],
    ids=["None", "True", "False", "NaN", "+Inf", "-Inf"],
)
def test_production_reranked_rejects_non_finite_scores(tmp_path, bad_score):
    from catalyst_data.retrieval.artifacts import ArtifactValidationError, load_arm_artifact

    path, _ = _write_scored_production_artifact(tmp_path)
    _tamper_score_and_provenance(path, bad_score)
    with pytest.raises(ArtifactValidationError, match="reranker_score|finite"):
        load_arm_artifact(path)


@pytest.mark.parametrize(
    "bad_score",
    [None, True, False, float("nan"), float("inf"), float("-inf")],
    ids=["None", "True", "False", "NaN", "+Inf", "-Inf"],
)
def test_provenance_rejects_non_finite_scores(tmp_path, bad_score):
    """Provenance path must reject non-finite scores even when result is fixed separately."""
    from catalyst_data.retrieval.artifacts import (
        ArtifactValidationError,
        compute_arm_artifact_id,
        load_arm_artifact,
    )

    path, _ = _write_scored_production_artifact(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Keep result finite; only provenance is non-finite → mismatch or score invalid.
    raw["effect_metrics"]["reranker_provenance"][0]["reranker_score"] = bad_score
    raw["artifact_id"] = compute_arm_artifact_id(raw)
    path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="reranker_score|provenance|finite"):
        load_arm_artifact(path)


@pytest.mark.parametrize("bad_rank", [None, 0, -1, 1.5, True, "1"], ids=["None", "0", "neg", "float", "bool", "str"])
def test_production_reranked_rejects_invalid_ranks(tmp_path, bad_rank):
    from catalyst_data.retrieval.artifacts import ArtifactValidationError, load_arm_artifact

    path, _ = _write_scored_production_artifact(tmp_path)
    _tamper_score_and_provenance(path, 9.0, rank_value=bad_rank)
    with pytest.raises(ArtifactValidationError, match="reranker_rank|rank"):
        load_arm_artifact(path)


def test_production_reranked_rejects_non_contiguous_ranks(tmp_path):
    from catalyst_data.retrieval.artifacts import (
        ArtifactValidationError,
        compute_arm_artifact_id,
        load_arm_artifact,
    )

    path, _ = _write_scored_production_artifact(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Swap ranks so order is non-contiguous relative to position.
    raw["arms"]["reranked"]["results"][0]["reranker_rank"] = 2
    raw["arms"]["reranked"]["results"][1]["reranker_rank"] = 1
    raw["effect_metrics"]["reranker_provenance"][0]["reranker_rank"] = 2
    raw["effect_metrics"]["reranker_provenance"][1]["reranker_rank"] = 1
    raw["artifact_id"] = compute_arm_artifact_id(raw)
    path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="reranker_rank|contiguous|order"):
        load_arm_artifact(path)


def test_is_finite_number_predicate_unit():
    from catalyst_data.retrieval.artifacts import is_finite_number

    assert is_finite_number(0) is True
    assert is_finite_number(1.5) is True
    assert is_finite_number(-3) is True
    for bad in (None, True, False, "1", float("nan"), float("inf"), float("-inf"), object()):
        assert is_finite_number(bad) is False

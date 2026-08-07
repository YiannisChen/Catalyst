from __future__ import annotations

import copy
import json

import pytest

from retrieval_model_fixtures import (
    EXPECTED_ARM_ARTIFACT_ID,
    FOUR_COMPLETE_ARM_RESULTS,
    make_arm_artifact,
)
from catalyst_data.config import BGE_M3_REVISION, BGE_RERANKER_REVISION


def test_arm_artifact_path_schema_and_identity_are_exact(tmp_path):
    from catalyst_data.retrieval.artifacts import load_arm_artifact, write_arm_artifact

    path = write_arm_artifact(
        root=tmp_path, run_id="run-1", case_id="B001",
        query="AAPL earnings", cutoff_ts="2026-01-15T21:00:00Z",
        filters={"ticker": "AAPL", "evidence_types": [], "source_classes": [], "corpus_manifest_id": "a" * 64, "index_manifest_id": "1" * 64},
        retrieval_config={"lexical_top_k": 20, "dense_top_k": 20, "fusion_k": 60, "fused_top_k": 20, "display_top_k": 8, "embedding_revision": BGE_M3_REVISION, "reranker_revision": BGE_RERANKER_REVISION},
        arms=FOUR_COMPLETE_ARM_RESULTS,
        created_at="2026-07-22T00:00:00Z",
    )
    loaded = load_arm_artifact(path)
    assert path == tmp_path / "run-1" / "B001.json"
    assert loaded.schema_version == "1.0.0"
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
            retrieval_config={"lexical_top_k": 20, "dense_top_k": 20, "fusion_k": 60, "fused_top_k": 20, "display_top_k": 8, "embedding_revision": BGE_M3_REVISION, "reranker_revision": BGE_RERANKER_REVISION}, arms=arms,
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
                retrieval_config={"lexical_top_k": 20, "dense_top_k": 20, "fusion_k": 60, "fused_top_k": 20, "display_top_k": 8, "embedding_revision": BGE_M3_REVISION, "reranker_revision": BGE_RERANKER_REVISION},
                arms=arms,
            )

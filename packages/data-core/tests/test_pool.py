"""Union judgment pool contract with an independent literal oracle.

The expected union order, per-arm chunk-id tuples, manifest identities, and
source artifact id are hardcoded literals. No test derives an expected value
from ``pool.per_arm_chunk_ids``, ``generate_union_pool``, or any production
helper.
"""

from __future__ import annotations

import copy
import json

import pytest

from retrieval_model_fixtures import (
    EXPECTED_UNION_CHUNK_IDS,
    FOUR_COMPLETE_ARM_RESULTS,
    LITERAL_DENSE_CHUNK_IDS,
    LITERAL_FTS5_CHUNK_IDS,
    LITERAL_HYBRID_CHUNK_IDS,
    LITERAL_RERANKED_CHUNK_IDS,
)
from catalyst_data.config import BGE_M3_REVISION, BGE_RERANKER_REVISION

# Independent literal oracles for the exact write_arm_artifact call below.
LITERAL_CORPUS_MANIFEST_ID = "a" * 64
LITERAL_INDEX_MANIFEST_ID = "1" * 64
LITERAL_ARTIFACT_ID = "8bdcd1eb61665d71e250373302a8844a5877417548339d368632b071f9d446cb"

FILTERS = {
    "ticker": "AAPL",
    "evidence_types": [],
    "source_classes": [],
    "corpus_manifest_id": LITERAL_CORPUS_MANIFEST_ID,
    "index_manifest_id": LITERAL_INDEX_MANIFEST_ID,
}
RETRIEVAL_CONFIG = {
    "lexical_top_k": 20,
    "dense_top_k": 20,
    "fusion_k": 60,
    "fused_top_k": 20,
    "display_top_k": 8,
    "embedding_revision": BGE_M3_REVISION,
    "reranker_revision": BGE_RERANKER_REVISION,
    "reranker_timeout_seconds": 2.0,
}


def _write_literal_artifact(tmp_path) -> object:
    from catalyst_data.retrieval.artifacts import write_arm_artifact

    return write_arm_artifact(
        root=tmp_path, run_id="run-1", case_id="B001", query="q",
        cutoff_ts="2026-01-01T00:00:00Z",
        filters=FILTERS,
        retrieval_config=RETRIEVAL_CONFIG,
        arms=FOUR_COMPLETE_ARM_RESULTS,
    )


def test_union_pool_orders_first_appearance_and_dedupes_across_arms(tmp_path):
    from catalyst_data.retrieval.pool import generate_union_pool

    path = _write_literal_artifact(tmp_path)
    pool = generate_union_pool(path)

    # Exact per-arm order, including duplicate chunk ids across arms.
    assert pool.per_arm_chunk_ids == {
        "fts5": LITERAL_FTS5_CHUNK_IDS,
        "dense": LITERAL_DENSE_CHUNK_IDS,
        "hybrid": LITERAL_HYBRID_CHUNK_IDS,
        "reranked": LITERAL_RERANKED_CHUNK_IDS,
    }
    # Cross-arm dedup keeps the first appearance order: a,b,c,d,e,f.
    assert pool.chunk_ids == EXPECTED_UNION_CHUNK_IDS
    assert pool.chunk_ids == ("a", "b", "c", "d", "e", "f")
    assert len(set(pool.chunk_ids)) == len(pool.chunk_ids)


def test_union_pool_carries_literal_manifest_and_source_identities(tmp_path):
    from catalyst_data.retrieval.pool import generate_union_pool

    path = _write_literal_artifact(tmp_path)
    pool = generate_union_pool(path)

    assert pool.schema_version == "1.0.0"
    assert pool.corpus_manifest_id == LITERAL_CORPUS_MANIFEST_ID
    assert pool.index_manifest_id == LITERAL_INDEX_MANIFEST_ID
    assert pool.source_artifact_id == LITERAL_ARTIFACT_ID


def test_union_pool_persists_and_reloads_literal_identity(tmp_path):
    from catalyst_data.retrieval.pool import generate_union_pool, load_union_pool, write_union_pool

    path = _write_literal_artifact(tmp_path)
    pool = generate_union_pool(path)
    output = write_union_pool(pool, tmp_path / "pool.json")
    assert output.is_file()

    reloaded = load_union_pool(output)
    assert reloaded.chunk_ids == ("a", "b", "c", "d", "e", "f")
    assert reloaded.source_artifact_id == LITERAL_ARTIFACT_ID
    assert reloaded.per_arm_chunk_ids["hybrid"] == ("d", "a", "e")
    raw = json.loads(output.read_text())
    assert raw["source_artifact_id"] == LITERAL_ARTIFACT_ID


def test_load_union_pool_rejects_missing_arm(tmp_path):
    from catalyst_data.retrieval.pool import load_union_pool

    payload = {
        "schema_version": "1.0.0",
        "case_id": "B001",
        "chunk_ids": ["a", "b", "c"],
        "per_arm_chunk_ids": {
            "fts5": ["a", "b", "c"],
            "dense": ["b", "d"],
            "hybrid": ["d", "a", "e"],
        },
        "corpus_manifest_id": LITERAL_CORPUS_MANIFEST_ID,
        "index_manifest_id": LITERAL_INDEX_MANIFEST_ID,
        "source_artifact_id": LITERAL_ARTIFACT_ID,
    }
    path = tmp_path / "pool.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="arm set mismatch"):
        load_union_pool(path)


def test_load_union_pool_rejects_malformed_chunk_id(tmp_path):
    from catalyst_data.retrieval.pool import load_union_pool

    payload = {
        "schema_version": "1.0.0",
        "case_id": "B001",
        "chunk_ids": ["a", "", "c"],
        "per_arm_chunk_ids": {
            "fts5": ["a", "b", "c"],
            "dense": ["b", "d"],
            "hybrid": ["d", "a", "e"],
            "reranked": ["e", "f"],
        },
        "corpus_manifest_id": LITERAL_CORPUS_MANIFEST_ID,
        "index_manifest_id": LITERAL_INDEX_MANIFEST_ID,
        "source_artifact_id": LITERAL_ARTIFACT_ID,
    }
    path = tmp_path / "pool.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="chunk order or identity mismatch"):
        load_union_pool(path)


def test_load_union_pool_rejects_empty_identity(tmp_path):
    from catalyst_data.retrieval.pool import load_union_pool

    payload = {
        "schema_version": "1.0.0",
        "case_id": "B001",
        "chunk_ids": [],
        "per_arm_chunk_ids": {name: [] for name in ("fts5", "dense", "hybrid", "reranked")},
        "corpus_manifest_id": "",
        "index_manifest_id": "1" * 64,
        "source_artifact_id": "",
    }
    path = tmp_path / "pool.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="identity"):
        load_union_pool(path)


def _tampered_artifact(tmp_path, mutator) -> object:
    from catalyst_data.retrieval.artifacts import write_arm_artifact

    path = write_arm_artifact(
        root=tmp_path, run_id="run-1", case_id="B001", query="q",
        cutoff_ts="2026-01-01T00:00:00Z",
        filters=FILTERS, retrieval_config=RETRIEVAL_CONFIG,
        arms=FOUR_COMPLETE_ARM_RESULTS,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    mutator(raw)
    raw["artifact_id"] = ""
    path.write_text(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def test_generate_rejects_missing_arm(tmp_path):
    from catalyst_data.retrieval.pool import generate_union_pool

    def drop_reranked(raw):
        del raw["arms"]["reranked"]

    path = _tampered_artifact(tmp_path, drop_reranked)
    with pytest.raises(ValueError, match="all four retrieval arms are required"):
        generate_union_pool(path)


def test_generate_rejects_duplicate_chunk_within_one_arm(tmp_path):
    from catalyst_data.retrieval.pool import generate_union_pool

    def duplicate_in_dense(raw):
        raw["arms"]["dense"]["results"].append(copy.deepcopy(raw["arms"]["dense"]["results"][1]))
        raw["arms"]["dense"]["results"][-1]["rank"] = 3

    path = _tampered_artifact(tmp_path, duplicate_in_dense)
    with pytest.raises(ValueError, match="duplicate or missing chunk_id"):
        generate_union_pool(path)


def test_generate_rejects_malformed_chunk_id(tmp_path):
    from catalyst_data.retrieval.pool import generate_union_pool

    def blank_chunk_id(raw):
        raw["arms"]["hybrid"]["results"][0]["chunk_id"] = ""

    path = _tampered_artifact(tmp_path, blank_chunk_id)
    with pytest.raises(ValueError, match="duplicate or missing chunk_id"):
        generate_union_pool(path)


def test_generate_rejects_source_artifact_identity_mismatch(tmp_path):
    from catalyst_data.retrieval.pool import generate_union_pool

    def tamper_artifact_id(raw):
        raw["artifact_id"] = "9" * 64

    path = _tampered_artifact(tmp_path, tamper_artifact_id)
    with pytest.raises(ValueError, match="artifact_id mismatch"):
        generate_union_pool(path)


# ---------------------------------------------------------------------------
# B6-L final convergence: per-arm load/write validation (Item 5)
# ---------------------------------------------------------------------------


def _literal_pool_payload(**overrides) -> dict:
    payload = {
        "schema_version": "1.0.0",
        "case_id": "B001",
        "chunk_ids": ["a", "b", "c", "d", "e", "f"],
        "per_arm_chunk_ids": {
            "fts5": ["a", "b", "c"],
            "dense": ["b", "d"],
            "hybrid": ["d", "a", "e"],
            "reranked": ["e", "f"],
        },
        "corpus_manifest_id": LITERAL_CORPUS_MANIFEST_ID,
        "index_manifest_id": LITERAL_INDEX_MANIFEST_ID,
        "source_artifact_id": LITERAL_ARTIFACT_ID,
    }
    payload.update(overrides)
    return payload


def test_load_union_pool_rejects_duplicate_chunk_within_one_arm(tmp_path):
    from catalyst_data.retrieval.pool import load_union_pool

    # duplicate inside one arm is hidden by the final union dedup
    payload = _literal_pool_payload(
        per_arm_chunk_ids={
            "fts5": ["a", "b", "c"],
            "dense": ["b", "d", "b"],
            "hybrid": ["d", "a", "e"],
            "reranked": ["e", "f"],
        }
    )
    path = tmp_path / "pool.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="duplicate"):
        load_union_pool(path)


def test_load_union_pool_rejects_per_arm_non_sequence(tmp_path):
    from catalyst_data.retrieval.pool import load_union_pool

    payload = _literal_pool_payload(
        per_arm_chunk_ids={
            "fts5": "abc",
            "dense": ["b", "d"],
            "hybrid": ["d", "a", "e"],
            "reranked": ["e", "f"],
        }
    )
    path = tmp_path / "pool.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="sequence"):
        load_union_pool(path)


def test_load_union_pool_rejects_non_string_chunk_id(tmp_path):
    from catalyst_data.retrieval.pool import load_union_pool

    payload = _literal_pool_payload(
        per_arm_chunk_ids={
            "fts5": ["a", "b", "c"],
            "dense": ["b", 123],
            "hybrid": ["d", "a", "e"],
            "reranked": ["e", "f"],
        },
        chunk_ids=["a", "b", "c", "d", "e", "f", 123],
    )
    path = tmp_path / "pool.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="non-empty string"):
        load_union_pool(path)


def test_write_union_pool_rejects_duplicate_within_one_arm(tmp_path):
    from catalyst_data.retrieval.pool import UnionJudgmentPool, write_union_pool

    pool = UnionJudgmentPool(
        schema_version="1.0.0",
        case_id="B001",
        chunk_ids=("a", "b", "c", "d", "e", "f"),
        per_arm_chunk_ids={
            "fts5": ("a", "b", "c"),
            "dense": ("b", "d", "b"),
            "hybrid": ("d", "a", "e"),
            "reranked": ("e", "f"),
        },
        corpus_manifest_id=LITERAL_CORPUS_MANIFEST_ID,
        index_manifest_id=LITERAL_INDEX_MANIFEST_ID,
        source_artifact_id=LITERAL_ARTIFACT_ID,
    )
    with pytest.raises(ValueError, match="duplicate"):
        write_union_pool(pool, tmp_path / "pool.json")


# ---------------------------------------------------------------------------
# T8: pool source discipline and data-core isolation
# ---------------------------------------------------------------------------


def test_pool_never_generates_from_ephemeral_in_memory_arrays(tmp_path):
    """generate_union_pool accepts only a persisted artifact path, never a dict."""
    from catalyst_data.retrieval.pool import generate_union_pool

    with pytest.raises(TypeError):
        generate_union_pool({"arms": {}})


def test_data_core_retrieval_package_has_no_catalyst_eval_import():
    """Landmine: B6 serializes data-core's own UnionJudgmentPool only."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "catalyst_data" / "retrieval"
    for path in sorted(root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "catalyst_eval" not in text, path
        assert "packages/eval" not in text, path


def test_pool_source_artifact_identity_verified_when_generating(tmp_path):
    """Pool must reject a persisted artifact whose artifact_id was tampered."""
    from catalyst_data.retrieval.pool import generate_union_pool

    path = _write_literal_artifact(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["artifact_id"] = "f" * 64
    path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact_id"):
        generate_union_pool(path)

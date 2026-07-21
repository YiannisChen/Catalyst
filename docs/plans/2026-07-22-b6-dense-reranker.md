# B6 — Dense Retrieval and Reranker Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a pinned BGE-M3 dense index on the server, combine lexical and dense candidates with RRF (k=60), run a reranker without changing the candidate universe, persist all retrieval-arm outputs, and generate the union judgment pool. B6 does not make the final keep/kill decision (deferred to B7 grading).

**Architecture:** Extract and adapt the existing LanceDB/RRF/reranker implementation behind the B4 retrieval interfaces instead of creating a second algorithm. Mac runs deterministic fixture-scale adapter tests; an exact-revision model load and full index build are separately authorized server steps. IndexManifest binds vectors to corpus and model identities.

**Tech Stack:** Python 3.12+, BGE-M3 (pinned revision), LanceDB, FlagEmbedding (optional extra), pytest

**Binding Contract:** `docs/plans/2026-07-21-b2-b7-technical-contracts.md` §8

---

## 0. Execution Rules

- B4 and B5 must be independently verified first. B6 changes retrieval only and does not alter agent workflow behavior.
- Existing `reciprocal_rank_fusion()`, reranker loading, and LanceDB search in `storage/lancedb_store.py` are the migration source, not dead code. Extract or wrap them with compatibility tests; do not duplicate them.
- Do not stage, commit, push, download models, call providers, access a GPU server, or mutate canonical DB files without separate authorization.
- Pin BGE-M3 to the locally verified snapshot `5617a9f61b028005a4858fdac845db406aefb181` and BGE reranker v2-m3 to `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`. Placeholders, tags, branches, and abbreviated SHAs are invalid.
- Every retrieval adapter returns one documented `RetrievalResultSet`; tests may not alternate between list and object APIs.
- Cosine similarity is validated in `[-1, 1]`. Candidate-set preservation compares exact sets and cardinality before taking the top-8 presentation slice.
- Fixture tests use injected deterministic embedders/rerankers; they do not load heavyweight model dependencies.


## 1. Objective

Deliver B6 that passes Core Exit Gate C (Retrieval — dense, hybrid, reranker portions) and prepares the union judgment pool for B7 labeling. Specifically:

- Exact BGE-M3 revision pin; 1024-dimensional output
- Vector normalization mode recorded in manifest
- Tokenizer revision in manifest
- IndexManifest with model/tokenizer revision, dtype, dimension, corpus_manifest_id, artifact hashes
- Incremental embedding by content_hash; metadata update by metadata_hash; tombstones
- Lexical top-20, dense top-20, RRF k=60 fusion, fused top-20
- Reranker candidate-set preservation (input set = output set, only reorder)
- Reranker top-8 presentation; timeout/failure → RRF fallback
- Retrieval traces for all arms
- FTS5, dense, hybrid, reranked outputs persisted
- Union judgment pool generation (no keep/kill decisions)
- Mac-local fixture build works; full build requires server authorization

## 2. Current Verified State

### Implemented

| Component | File | Status |
|---|---|---|
| LanceDB store | `catalyst_data/storage/lancedb_store.py` | implemented — needs BGE-M3 adaptation |
| Embedding builder (GPU) | `packages/data-core/scripts/build_embeddings_gpu.py` | implemented — needs revision pin |
| Index builder | `packages/data-core/catalyst_data/index_builder.py` | implemented — needs RRF/reranker |
| `index_state` table | `catalyst_data/storage/sqlite.py` | implemented |
| Source tier filter | `catalyst_data/source_tier.py` | implemented |
| FTS5 retrieval | B4 deliverable | consumed as baseline arm |
| RetrievalResult | B4 deliverable | extended for dense/hybrid/reranked |

### Partial

| Component | Gap |
|---|---|
| FlagEmbedding/BGE-M3 revision | may not be pinned to exact commit hash |
| Dense retrieval | LanceDB cosine similarity exists but not integrated with B3 chunk profiles |
| RRF | implemented in `storage/lancedb_store.py`; not yet exposed through the B4 retrieval contract |
| Reranker | implemented in `storage/lancedb_store.py`; lacks exact revision pin, candidate-preserving result contract, and explicit fallback trace |
| Retrieval traces | B4 lexical only; needs dense/hybrid/reranked extension |
| Union judgment pool | not implemented |

### Missing

| Component | File to create |
|---|---|
| Dense retrieval adapter | `packages/data-core/catalyst_data/retrieval/dense.py` (new) |
| RRF fusion | `packages/data-core/catalyst_data/retrieval/fusion.py` (new) |
| Reranker adapter | `packages/data-core/catalyst_data/retrieval/reranker.py` (new) |
| Hybrid retrieval (orchestrator) | `packages/data-core/catalyst_data/retrieval/hybrid.py` (new) |
| IndexManifest | `packages/data-core/catalyst_data/retrieval/index_manifest.py` (new) |
| Retrieval-arm artifact writer | `packages/data-core/catalyst_data/retrieval/artifacts.py` (new) |
| Union pool generator | `packages/data-core/catalyst_data/retrieval/pool.py` (new) |
| Incremental embedder | `packages/data-core/catalyst_data/retrieval/embedder.py` (new) |
| Dense/reranker tests | `packages/data-core/tests/test_dense_retrieval.py` |
| Fusion tests | `packages/data-core/tests/test_fusion.py` |
| Reranker tests | `packages/data-core/tests/test_reranker.py` |

## 3. Scope / Non-goals

### Scope

- Pin BGE-M3 revision (commit hash) in a config/constant
- Dense embedding with 1024-dim output, optional normalization
- IndexManifest with all contract fields
- Incremental embedding: compare content_hash, re-embed only changes
- Metadata-only update path (metadata_hash change → update index metadata)
- Tombstone handling for removed chunks
- Candidate full rebuild capability
- Dense retrieval: cosine similarity, top-20 identical filters as lexical
- RRF k=60: `RRF(d) = sum(1/(60+rank_m(d)))` for m in {lexical, dense}
- Fused top-20
- Reranker adapter: reorders fused top-20, preserves candidate set
- Reranker top-8; timeout/failure → explicit RRF fallback
- Retrieval traces for all arms
- Persist all arm outputs
- Union judgment pool: union of top-K from FTS5, dense, hybrid, reranked
- Mac: fixture-scale build (small corpus, verified); full build on GPU server with operator authorization

### Non-goals

- Automatic fallback to another embedding model
- Mac full-build (only fixture-scale)
- Candidate expansion during reranking
- Keep/kill decisions (B7)
- Metric computation (B7)
- GPU server automation (operator-initiated only)
- Cross-encoder model selection (use pinned revision from contract)

## 4. Dependencies

### Inputs

- B3: corpus_chunks with content_hash, metadata_hash, tombstones, CorpusManifest
- B4: FTS5 baseline retrieval, RetrievalResult type, cutoff computation
- Existing LanceDB store
- Existing embedding build script

### Output Artifacts

- Dense index (LanceDB) with BGE-M3 vectors
- IndexManifest
- Atomic `data/retrieval_arm_outputs/<run_id>/<case_id>.json` artifacts conforming to contract §8.5
- Versioned `UnionJudgmentPool` JSON (data-core artifact, not eval `PoolManifest`)
- Retrieval traces for all arms

### Consumed By

- B7: union pool for human grading, keep/kill decisions, metric computation
- B7: API surfaces for retrieval

## 5. Schema and Artifact Ownership

### Owned

- `IndexManifest` (model_revision, tokenizer_revision, normalization_mode, dtype, dimension, corpus_manifest_id, artifact_hashes)
- `UnionJudgmentPool` (`schema_version`, `case_id`, `chunk_ids`, `per_arm_chunk_ids`, `corpus_manifest_id`, `index_manifest_id`, `source_artifact_id`)
- `RetrievalArmOutputArtifact` schema `1.0.0` and its semantic `artifact_id` formula from contract §8.5
- Dense index vectors (LanceDB)
- Union judgment pool JSON serialized from `UnionJudgmentPool`

### Not Owned

- CorpusManifest (B3)
- FTS5 index (B4)
- RetrievalResult base type (B4 — extended with dense_score, fusion_score, reranker_score)
- Migration v10 (B4) — B6 adds no new SQLite migration

## 6. File Allowlist

### New Files

```
packages/data-core/catalyst_data/retrieval/dense.py
packages/data-core/catalyst_data/retrieval/fusion.py
packages/data-core/catalyst_data/retrieval/reranker.py
packages/data-core/catalyst_data/retrieval/hybrid.py
packages/data-core/catalyst_data/retrieval/index_manifest.py
packages/data-core/catalyst_data/retrieval/artifacts.py
packages/data-core/catalyst_data/retrieval/pool.py
packages/data-core/catalyst_data/retrieval/embedder.py
packages/data-core/tests/test_dense_retrieval.py
packages/data-core/tests/test_fusion.py
packages/data-core/tests/test_reranker.py
packages/data-core/tests/test_index_manifest.py
packages/data-core/tests/test_retrieval_artifacts.py
packages/data-core/tests/test_pool.py
packages/data-core/tests/retrieval_model_fixtures.py
```

### Allowed to Modify

```
packages/data-core/catalyst_data/storage/lancedb_store.py        — BGE-M3 adaptation
packages/data-core/scripts/build_embeddings_gpu.py               — revision pin, manifest
packages/data-core/catalyst_data/index_builder.py                — incremental embed, hybrid
packages/data-core/catalyst_data/retrieval/result.py             — extend with new score fields
packages/data-core/tests/test_lancedb.py                         — BGE-M3 fixture tests
packages/data-core/tests/test_index_builder.py                   — dense path tests
packages/data-core/catalyst_data/config.py                       — BGE-M3 revision constant
```

### Files Explicitly Forbidden

- `data/catalyst_eval_frozen_v2.db` — frozen
- `packages/agents/` — B5 domain
- `packages/eval/` — B7 domain
- GPU server without operator authorization
- Embedding model downloaded without pinned revision

## 7. TDD Tasks

### Task 0: Deterministic retrieval-model fixtures

Create `packages/data-core/tests/retrieval_model_fixtures.py` with normalized 1024-d vectors, a recording embedder, a recording deterministic candidate-preserving reranker, temporary LanceDB setup, and exact expected rankings. It also defines `COMPLETE_FILTERS`, `PINNED_RETRIEVAL_CONFIG`, `FOUR_COMPLETE_ARM_RESULTS`, `EXPECTED_ARM_ARTIFACT_ID`, `SEMANTIC_ARTIFACT_MUTATIONS`, `INVALID_ARM_ARTIFACTS`, `EXPECTED_UNION_CHUNK_IDS`, and the artifact/failure fixture helpers referenced by Tasks 5–6. Expected identities and orderings are literal independent oracles and may not call production artifact hashing, fusion, or pool generation. These fixtures never import or download model weights. All later `_fresh_db_with_*` and `make_result*` helpers come from this file.

### Task 1: Pin BGE-M3 revision

**Step 1: Write revision pin test**

```python
# packages/data-core/tests/test_index_manifest.py

def test_bge_m3_revision_is_pinned():
    """BGE-M3 revision is an exact commit hash, not a branch or tag."""
    import re
    from catalyst_data.config import BGE_M3_REVISION

    assert re.fullmatch(r"[0-9a-f]{40}", BGE_M3_REVISION)


def test_bge_m3_output_dimension():
    """BGE-M3 output dimension is 1024."""
    from catalyst_data.config import BGE_M3_DIMENSION
    assert BGE_M3_DIMENSION == 1024


def test_embedding_revision_in_manifest():
    """IndexManifest records model revision, tokenizer revision, normalization, dtype, dimension."""
    from catalyst_data.config import BGE_M3_REVISION
    from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION
    from catalyst_data.retrieval.index_manifest import IndexManifest

    manifest = IndexManifest(
        model_revision=BGE_M3_REVISION,
        tokenizer_revision=TOKENIZER_REVISION,
        normalization_mode="l2",
        dtype="float32",
        dimension=1024,
        corpus_manifest_id="corpus-manifest-v1",
        artifact_hashes={"vectors.lance": "4f" * 32},
    )
    assert manifest.dimension == 1024
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_index_manifest.py -q
```

Expected: FAIL.

**Step 3: Implement**

- `catalyst_data/config.py`: `BGE_M3_REVISION="5617a9f61b028005a4858fdac845db406aefb181"`, `BGE_RERANKER_REVISION="953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"`, `BGE_M3_DIMENSION=1024`
- `catalyst_data/retrieval/index_manifest.py`: `IndexManifest` model

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_index_manifest.py -q
```

Expected: PASS.

### Task 2: Dense retrieval adapter

**Step 1: Write dense retrieval test**

```python
# packages/data-core/tests/test_dense_retrieval.py

def test_dense_similarity_normalized():
    """If vectors are normalized, score = dot product."""
    import numpy as np

    q = np.array([1.0, 0.0, 0.0])
    d = np.array([0.707, 0.707, 0.0])
    q_norm = q / np.linalg.norm(q)
    d_norm = d / np.linalg.norm(d)
    score = np.dot(q_norm, d_norm)
    assert 0.0 < score <= 1.0


def test_dense_top_20_same_filters():
    """Dense retrieval uses identical cutoff, ticker, source filters as lexical."""
    from catalyst_data.retrieval.dense import retrieve_dense

    db = _fresh_db_with_chunks_and_embeddings()
    result = retrieve_dense(db, query_embedding=np.ones(1024), ticker="AAPL",
                            cutoff="2026-01-15T21:00:00Z", top_k=20)

    # Same filter rules as lexical
    for r in result.results:
        assert r.available_at <= "2026-01-15T21:00:00Z"


def test_dense_score_is_cosine():
    """dense_score is cosine similarity (or dot product if normalized)."""
    from catalyst_data.retrieval.dense import retrieve_dense

    db = _fresh_db_with_chunks_and_embeddings()
    result = retrieve_dense(db, query_embedding=np.ones(1024), ticker="AAPL",
                            cutoff="2026-01-15T21:00:00Z", top_k=20)

    if result.results:
        assert -1.0 <= result.results[0].dense_score <= 1.0
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_dense_retrieval.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_data/retrieval/dense.py`**

- `retrieve_dense(db, query_embedding, ticker, cutoff, top_k=20, filters=None) → RetrievalResultSet`
- Uses LanceDB cosine search with filter predicates
- Populates `dense_score` and `dense_rank`

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_dense_retrieval.py -q
```

Expected: all PASS.

### Task 3: RRF fusion

**Step 1: Write RRF test**

```python
# packages/data-core/tests/test_fusion.py

def test_rrf_formula():
    """RRF(d) = sum over m in {lexical, dense} of 1/(60 + rank_m(d))."""
    from catalyst_data.retrieval.fusion import compute_rrf

    # Chunk ranked #1 in lexical, #5 in dense
    score = compute_rrf(lexical_rank=1, dense_rank=5, k=60)
    expected = 1/(60+1) + 1/(60+5)
    assert score == pytest.approx(expected)


def test_rrf_fuses_top_20_each():
    """RRF takes top-20 from lexical and top-20 from dense, fuses, returns top-20."""
    from catalyst_data.retrieval.fusion import fuse

    lexical_results = make_results(n=20, prefix="lex")
    dense_results = make_results(n=20, prefix="den")

    # Some overlap
    overlap = make_result(chunk_id="shared:1", lexical_rank=1, dense_rank=20)
    lexical_results.append(overlap)
    dense_results.append(overlap)

    fused = fuse(lexical_results, dense_results, k=60, output_k=20)
    assert len(fused) <= 20


def test_rrf_stable_ties():
    """Tied RRF scores: best contributing rank, then chunk_id ASC."""
    from catalyst_data.retrieval.fusion import fuse

    # Two chunks with identical ranks → tie
    a = make_result(chunk_id="a:1", lexical_rank=1, dense_rank=None)
    b = make_result(chunk_id="b:1", lexical_rank=1, dense_rank=None)

    fused = fuse([b, a], [], k=60, output_k=20)
    assert [item.chunk_id for item in fused] == ["a:1", "b:1"]
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_fusion.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_data/retrieval/fusion.py`**

- Move or wrap the existing tested `reciprocal_rank_fusion()` behavior from `storage/lancedb_store.py`; retain a deprecated compatibility re-export there.
- `compute_rrf(lexical_rank, dense_rank, k=60) → float`
- `fuse(lexical_results, dense_results, k=60, output_k=20) → list[RetrievalResult]`
- Stable ties: best contributing rank, then chunk_id ASC

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_fusion.py -q
```

Expected: all PASS.

### Task 4: Reranker adapter

**Step 1: Write reranker test**

```python
# packages/data-core/tests/test_reranker.py

def test_reranker_candidate_preservation():
    """set(reranker_input) == set(reranker_output). Only reorders, never drops."""
    from catalyst_data.retrieval.reranker import rerank

    candidates = [make_result(chunk_id=f"c{i}") for i in range(20)]
    reranked = rerank(query="Why did AAPL drop?", candidates=candidates)

    input_ids = {c.chunk_id for c in candidates}
    output_ids = {c.chunk_id for c in reranked.results}
    assert input_ids == output_ids
    assert len(reranked.results) == len(candidates)


def test_reranker_top_8_presentation():
    """Reranker returns top-8 from the reordered 20."""
    from catalyst_data.retrieval.reranker import rerank

    candidates = [make_result(chunk_id=f"c{i}") for i in range(20)]
    reranked = rerank(query="test", candidates=candidates)

    # Returns all 20 reordered + marks top-8
    assert len(reranked.results) == 20
    top8 = [r for r in reranked.results if r.reranker_rank <= 8]
    assert len(top8) == 8


def test_reranker_timeout_falls_back_to_rrf():
    """Timeout or model failure → explicit fallback to RRF ordering."""
    from catalyst_data.retrieval.reranker import rerank

    # Simulate timeout
    result = rerank(query="test", candidates=make_results(20),
                    timeout_seconds=0.001, fallback_ordering="rrf")

    assert result.is_degraded
    assert result.fallback_reason is not None
    # RRF ordering preserved
    for i, r in enumerate(result.results):
        assert r.fusion_rank is not None


def test_reranker_never_retrieves_new_candidates():
    """Reranker cannot add chunks outside the input candidate set."""
    from catalyst_data.retrieval.reranker import rerank

    candidates = [make_result(chunk_id="a"), make_result(chunk_id="b")]
    reranked = rerank(query="test", candidates=candidates)
    for r in reranked.results:
        assert r.chunk_id in {"a", "b"}
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_reranker.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_data/retrieval/reranker.py`**

- Adapt the existing `load_reranker()` / `_apply_reranker()` path rather than introducing another model loader.
- `rerank(query, candidates, timeout_seconds, fallback_ordering="rrf") → RetrievalResultSet`
- Input set = output set invariant
- Timeout → explicit RRF fallback with degraded flag

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_reranker.py -q
```

Expected: all PASS.

### Task 5: Hybrid retrieval orchestrator

**Step 1: Write hybrid test**

```python
# packages/data-core/tests/test_fusion.py

def test_hybrid_orchestrates_all_arms():
    """mode='hybrid' is lexical + dense + RRF only and never calls reranker."""
    from catalyst_data.retrieval.hybrid import retrieve_hybrid

    db = _fresh_db_with_all()
    result = retrieve_hybrid(db, query="AAPL earnings", ticker="AAPL",
                             cutoff="2026-01-15T21:00:00Z", mode="hybrid")

    assert result.lexical_results is not None
    assert result.dense_results is not None
    assert result.fusion_results is not None
    assert result.mode_requested == "hybrid"
    assert result.mode_served == "hybrid"
    assert result.reranker_results is None
    assert recording_reranker.call_count == 0
    assert result.final_results is not None


def test_hybrid_mode_reranked():
    """mode='reranked' includes reranker step."""
    from catalyst_data.retrieval.hybrid import retrieve_hybrid

    db = _fresh_db_with_all()
    result = retrieve_hybrid(db, query="AAPL earnings", ticker="AAPL",
                             cutoff="2026-01-15T21:00:00Z", mode="reranked")

    assert result.mode_requested == "reranked"
    assert result.mode_served == "reranked"
    assert result.reranker_results is not None


def test_reranker_failure_serves_exact_hybrid_order():
    """Requested reranked falls back to mode_served=hybrid without reordering RRF."""
    result = retrieve_with_failing_reranker_fixture(mode="reranked")
    assert result.mode_requested == "reranked"
    assert result.mode_served == "hybrid"
    assert result.final_results.chunk_ids == result.fusion_results.chunk_ids
    assert result.degradation_reasons == ["reranker_failed"]


@pytest.mark.parametrize("failed_arm,served", [("dense", "fts5"), ("fts5", "dense")])
def test_one_arm_failure_is_not_labeled_hybrid_and_skips_reranker(failed_arm, served):
    """One-arm fallback serves that arm directly for hybrid and reranked requests."""
    result, reranker = retrieve_with_failed_arm_fixture(mode="reranked", failed_arm=failed_arm)
    assert result.mode_requested == "reranked"
    assert result.mode_served == served
    assert result.degradation_reasons == [f"{failed_arm}_failed"]
    assert reranker.call_count == 0
```

**Step 2: Implement `catalyst_data/retrieval/hybrid.py`**

Implement contract §8.4 exactly. `mode="hybrid"` stops after RRF and never invokes the reranker. `mode="reranked"` invokes the reranker only after both retrieval arms and RRF succeed. Every return path sets exact `mode_requested`, `mode_served`, and ordered degradation reasons.

**Step 3: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_fusion.py -q
```

Expected: all PASS.

### Task 5b: Persist retrieval-arm output artifacts

**Step 1: Write artifact contract tests**

```python
# packages/data-core/tests/test_retrieval_artifacts.py

def test_arm_artifact_path_schema_and_identity_are_exact(tmp_path):
    """One atomic JSON file preserves all four arms and contract §8.5 identities."""
    from catalyst_data.retrieval.artifacts import load_arm_artifact, write_arm_artifact

    path = write_arm_artifact(
        root=tmp_path,
        run_id="run-1",
        case_id="B001",
        query="AAPL earnings",
        cutoff_ts="2026-01-15T21:00:00Z",
        filters=COMPLETE_FILTERS,
        retrieval_config=PINNED_RETRIEVAL_CONFIG,
        arms=FOUR_COMPLETE_ARM_RESULTS,
        created_at="2026-07-22T00:00:00Z",
    )
    assert path == tmp_path / "run-1" / "B001.json"
    loaded = load_arm_artifact(path)
    assert loaded.schema_version == "1.0.0"
    assert list(loaded.arms) == ["fts5", "dense", "hybrid", "reranked"]
    assert loaded.artifact_id == EXPECTED_ARM_ARTIFACT_ID
    assert not list(path.parent.glob("*.tmp"))


def test_artifact_id_excludes_time_but_bites_on_results_modes_and_manifests(tmp_path):
    """Timing changes do not alter identity; every semantic field does."""
    base = make_arm_artifact(created_at="2026-07-22T00:00:00Z", latency_ms=1.0)
    timing_only = make_arm_artifact(created_at="2026-07-23T00:00:00Z", latency_ms=999.0)
    assert compute_arm_artifact_id(base) == compute_arm_artifact_id(timing_only)

    for mutation in SEMANTIC_ARTIFACT_MUTATIONS:
        changed = mutation(copy.deepcopy(base))
        assert compute_arm_artifact_id(changed) != compute_arm_artifact_id(base)


def test_artifact_rejects_duplicate_chunks_wrong_order_and_unknown_mode(tmp_path):
    """Served-order arrays, unique IDs, and mode enums are schema-enforced."""
    for invalid in INVALID_ARM_ARTIFACTS:
        with pytest.raises(ArtifactValidationError):
            write_arm_artifact(root=tmp_path, **invalid)
```

**Step 2: Run the focused tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_retrieval_artifacts.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_data/retrieval/artifacts.py`**

Implement contract §8.5 exactly: versioned typed schema, canonical semantic hash excluding only `artifact_id`, `created_at`, and latency values, strict arm/result validation, path `<root>/<run_id>/<case_id>.json`, and temp-file + fsync + atomic rename. The default root is `data/retrieval_arm_outputs`; tests always inject a temporary root.

**Step 4: Run the focused tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_retrieval_artifacts.py -q
```

Expected: PASS.

### Task 6: Union judgment pool

**Step 1: Write pool test**

```python
# packages/data-core/tests/test_pool.py

def test_union_pool_combines_persisted_arm_artifact():
    """Union pool is derived from the persisted four-arm artifact, not memory."""
    from catalyst_data.retrieval.pool import generate_union_pool

    artifact_path = write_complete_arm_artifact_fixture()
    pool = generate_union_pool(
        artifact_path=artifact_path,
    )
    assert pool.schema_version == "1.0.0"
    assert pool.source_artifact_id == EXPECTED_ARM_ARTIFACT_ID
    assert pool.chunk_ids == EXPECTED_UNION_CHUNK_IDS
    assert set(pool.per_arm_chunk_ids) == {"fts5", "dense", "hybrid", "reranked"}


def test_pool_includes_manifest_identities():
    """Pool records which corpus/index manifests were used."""
    from catalyst_data.retrieval.pool import generate_union_pool

    pool = generate_union_pool(artifact_path=write_complete_arm_artifact_fixture())
    assert pool.schema_version == "1.0.0"
    assert pool.corpus_manifest_id == "corpus-v1"
    assert pool.index_manifest_id == "index-v1"
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_pool.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_data/retrieval/pool.py`**

- Define the data-core-native, versioned `UnionJudgmentPool`; do not import or reuse `catalyst_eval.benchmark.PoolManifest`. Version `1.0.0` requires `schema_version`, `case_id`, `chunk_ids`, `per_arm_chunk_ids`, `corpus_manifest_id`, `index_manifest_id`, and `source_artifact_id`.
- `generate_union_pool(artifact_path) → UnionJudgmentPool`; it validates and reads the persisted contract §8.5 artifact, requires all four arms, preserves served-order arm membership, and derives both manifest identities from the artifact. No API accepts ephemeral arm-result objects.
- Union of chunk_ids across all arms, preserving per-arm provenance

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_pool.py -q
```

Expected: all PASS.

### Task 7: Incremental embedding

**Step 1: Write incremental embedding test**

```python
# packages/data-core/tests/test_index_builder.py

def test_incremental_embedding_only_content_changed():
    """Only chunks with new content_hash are re-embedded."""
    from catalyst_data.retrieval.embedder import plan_embedding_work

    index_state = {
        "chunk:a:1": {"content_hash": "same", "metadata_hash": "same"},
        "chunk:b:1": {"content_hash": "different", "metadata_hash": "different"},
        "chunk:c:1": {"content_hash": "same", "metadata_hash": "changed"},
    }
    active = {
        "chunk:a:1": {"content_hash": "same", "metadata_hash": "same"},
        "chunk:b:1": {"content_hash": "new_hash", "metadata_hash": "new_meta"},
        "chunk:c:1": {"content_hash": "same", "metadata_hash": "newer"},
    }
    plan = plan_embedding_work(index_state, active)
    assert "chunk:a:1" not in plan.to_embed  # unchanged
    assert "chunk:b:1" in plan.to_embed      # content changed
    assert "chunk:c:1" in plan.to_update_metadata  # metadata only
```

**Step 2: Implement `catalyst_data/retrieval/embedder.py`**

**Step 3: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_index_builder.py -k "incremental" -q
```

Expected: PASS.

## 8. Landmine Tests

1. **Dense and lexical use different cutoffs** — verify same cutoff function called.
2. **Reranker drops candidates** — `test_reranker_candidate_preservation`.
3. **RRF uses k=60 (not 40, 100)** — `test_rrf_formula` verifies k=60.
4. **Fallback to BGE-small or other model** — `test_bge_m3_revision_is_pinned` + search for other model names in retrieval code.
5. **Whole-file dump instead of incremental** — verify index builder uses content_hash comparison.
6. **Union pool missing an arm** — verify all four arms present in pool generation.
7. **B6 makes keep/kill decision** — verify pool generation only, no metric computation.
8. **Duplicate RRF/reranker algorithms** — compatibility tests must show the old public functions and new adapters produce identical ordering on the same fixtures; only one implementation body remains.
9. **Fake revision accepted** — tests reject any revision not matching `^[0-9a-f]{40}$`; model loading passes that revision explicitly to tokenizer/model constructors.
10. **Different filters by arm** — one post-cutoff and one wrong-manifest chunk must be absent from lexical, dense, hybrid, and reranked outputs.
11. **Pool boundary inversion** — `rg "catalyst_eval" packages/data-core/` must return zero; B6 serializes only the versioned plain-JSON `UnionJudgmentPool` artifact.
12. **Hybrid silently reranks** — a recording reranker has zero calls for `mode_requested=hybrid`.
13. **Fallback lies about served mode** — every arm/reranker failure asserts exact `mode_requested`, `mode_served`, degradation reasons, and final ordering per contract §8.4.
14. **Arm outputs exist only in memory** — pool generation accepts only a validated persisted artifact path; kill the process after artifact write and prove a fresh process can recreate the identical pool.

## 9. Verification Ladder

```bash
# New tests
.venv/bin/python -m pytest packages/data-core/tests/test_index_manifest.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_dense_retrieval.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_fusion.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_reranker.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_retrieval_artifacts.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_pool.py -q

# Existing related
.venv/bin/python -m pytest packages/data-core/tests/test_lancedb.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_index_builder.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_fts5_retrieval.py -q

# Canonical
.venv/bin/python -m pytest packages/data-core -q
.venv/bin/python -m pytest packages/agents -q
.venv/bin/python -m pytest packages/eval -q
.venv/bin/python -m pytest packages/app -q

# DB SHA
shasum -a 256 data/catalyst_dev_ws4b.db
shasum -a 256 data/catalyst_eval_frozen_v2.db

git diff --check
```

## 10. Evidence Report Template

```markdown
## B6 Completion Report

### Files Changed
- [list]

### Focused Test Counts
- test_index_manifest: X passed
- test_dense_retrieval: X passed
- test_fusion: X passed
- test_reranker: X passed
- test_pool: X passed

### IndexManifest
- model_revision: BAAI/bge-m3@[commit]
- dimension: 1024
- corpus_manifest_id: [manifest-id]

### Union Pool Generated
- case_ids: [B001...]
- total unique chunks: N

### Mac Fixture Build
- fixture-scale dense index verified: [yes/no]

### Canonical Counts
- data-core: X passed (was 758)
- agents: X passed (was 237)
- eval: X passed (was 93)
- app: X passed (was 128)

### DB SHA
- Dev DB: [unchanged from B5]
- Frozen DB: unchanged

### Unresolved Risks
- Server GPU build pending operator authorization
- Reranker revision pin pending

### Confirmation
- [ ] Next package (B7) not started
- [ ] No keep/kill decisions made
```

## 11. Git Boundaries

1. `feat(data-core): pin BGE-M3 revision and add IndexManifest`
2. `feat(data-core): add dense retrieval adapter with cosine similarity`
3. `feat(data-core): add RRF fusion (k=60)`
4. `feat(data-core): add reranker adapter with candidate-set preservation`
5. `feat(data-core): add hybrid retrieval orchestrator`
6. `feat(data-core): add incremental embedding and union pool generator`

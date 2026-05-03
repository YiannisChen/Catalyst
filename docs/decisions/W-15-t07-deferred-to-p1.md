# W-15: Defer T-07 Vector Index Rebuild to P1

**Decision ID:** W-15
**Status:** APPROVED
**Issued:** 2026-05-03
**Issuer:** Senior Reviewer (Tier-1 architecture gate)
**Supersedes:** Plan §L T-07 (lines 1027–1048 of `2026-04-30-p0-construction-plan-v3.md`)
**Affects:** T-07, T-08, T-10, T-13a, T-13b, T-14, T-15, T-16

---

## 1. Decision

T-07 (Vector Index Rebuild) is **deferred in full to P1**. The P0 sprint continues from T-08 onward without a LanceDB gold index. All downstream tasks that reference `gold_chunks`, `lancedb_gold/eval_frozen/`, or `bge-m3` embeddings are amended below.

## 2. Rationale

bge-m3 inference on the available hardware (MPS/CPU-only Mac) is infeasible within the P0 time budget. Embedding ~10,000 clean_assets rows with a 1024-dim model on CPU would exceed the remaining sprint allocation by a wide margin. No GPU-enabled environment is available for P0.

The vector index is a **retrieval quality enhancer**, not a functional prerequisite. The agent control plane (Validator, Router, Critic, Judge) operates independently of how retrieval is implemented internally. P0 can ship with SQL-only retrieval and demonstrate the full agent graph, eval harness, and defense artifacts without vector similarity search.

## 3. Scope of Deferral

| Item deferred | Where it was specified | P1 prerequisite |
|---|---|---|
| `data/lancedb_gold/eval_frozen/` directory | §G, T-07 | GPU-enabled environment or quantized model |
| `gold_chunks` LanceDB table | §G.1, T-07 | Same |
| bge-m3 embedding generation | §G.2, T-07 | Same |
| Health check §G.4 + consistency check §G.5 | T-07 verification | Same |
| Smoke retrieval against LanceDB | T-07 exit criterion | Same |
| LanceDB hybrid query path (BM25 + vector) in retrieval | §H.1 | T-07 completion in P1 |

## 4. What Is NOT Deferred

The following remain in P0 scope, unchanged:

- `catalyst_eval_frozen.db` with all clean_assets, ohlcv, source_checkpoints, ingestion_runs (T-06 done)
- Retrieval policy code structure (`Layer.DIRECT`, `Layer.MACRO`, `Layer.RELATED` raises) — T-10
- Validator, Critic, Router, Judge, Trace — T-08, T-09, T-11
- Eval harness, direct_llm baseline, comparison report — T-12, T-13, T-14
- Defense artifacts — T-15, T-16

## 5. Amended Entry/Exit Dependencies

### T-08: Validator + 4-State Output + Critic Decision Contract

**Original entry:** T-07 complete.
**Amended entry:** T-06 complete.
**Justification:** T-08 builds `state.py` (OutputStatus, Phase, CriticDecision), `validator.py` (4 checks), and wires them into the graph. None of these depend on the vector index. The validator checks evidence-id existence, time-window, schema, and magnitude — all operate on structured data, not embeddings.
**Exit criteria:** Unchanged.

### T-09: DecisionRouter

**Original entry:** T-08 complete.
**Amended entry:** Unchanged (T-08 complete).
**Justification:** Pure-function dispatcher. No retrieval dependency.
**Exit criteria:** Unchanged.

### T-10: Retrieval Policy Layer 1 + Layer 2

**Original entry:** T-09 complete.
**Amended entry:** Unchanged (T-09 complete).
**Exit criteria — AMENDED:**

- `policy.py` implements `Layer.DIRECT` and `Layer.MACRO` per §H.1 with a **SQL-only retrieval fallback**.
- The SQL-only path uses: `SELECT * FROM clean_assets WHERE ticker = ? AND reference_date BETWEEN ? AND ? AND source_type IN (...)` with ordering by `reference_date DESC` and `LIMIT top_k`.
- The LanceDB hybrid query branch is **code-complete but gated** behind `if lancedb_available:` — when `data/lancedb_gold/eval_frozen/` does not exist, retrieval falls back to SQL-only mode automatically.
- `Layer.RELATED` still raises `NotImplementedError`.
- Retrieval signature remains rerank-ready: `retrieve(query, layer, metadata, *, rerank=None)`.
- Unit tests use the existing in-memory fixture (no LanceDB dependency in tests).
- **New exit criterion:** `retrieve()` returns results from SQL-only path when LanceDB path is absent. At least 1 test explicitly covers the SQL-only fallback.

### T-11: Trace Persistence

**Original entry:** T-08 schema stable.
**Amended entry:** Unchanged.
**Exit criteria:** Unchanged. No vector index dependency.

### T-12: direct_llm Baseline + Failure Taxonomy

**Original entry:** T-08 + T-11 complete.
**Amended entry:** Unchanged.
**Exit criteria:** Unchanged. `direct_llm` is the no-agent baseline; it never touches retrieval.

### T-13a: Pre-Eval Preflight + Golden-Set Freeze + SHA Capture

**Original entry:** T-10, T-11, T-12 complete.
**Amended entry:** Unchanged.
**Exit criteria — AMENDED:**

- Freeze marker captures `db_sha256` as before.
- `lancedb_dir_sha256` field is set to the literal string `"DEFERRED_P1"` (not null, not omitted — explicitly signals the deferral).
- Preflight script must NOT fail on missing `data/lancedb_gold/eval_frozen/`. Add a check: if directory absent, record `lancedb_status: "deferred_p1"` in preflight JSON and continue.
- All other preflight checks (macro_doc_count, direct_evidence_per_ticker_min, geo_tier2_doc_count) are unchanged.

### T-13b: Eval Runs + Threshold Calibration

**Original entry:** T-13a complete.
**Amended entry:** Unchanged.
**Exit criteria — AMENDED:**

- `mcj_full` runs use SQL-only retrieval (the fallback from T-10). This is a **known degradation** — retrieval quality is lower without vector similarity, which may affect Critic `magnitude_coverage` and evidence hit rates. Calibration thresholds (K/M values) are calibrated against the SQL-only retrieval path.
- Eval-run headers include `lancedb_dir_sha256: "DEFERRED_P1"` instead of a real hash.
- All other exit criteria unchanged.

### T-14: Eval Comparison Report + Gate Script

**Original entry:** T-13b complete.
**Amended entry:** Unchanged.
**Exit criteria — AMENDED:**

- Comparison report `header` includes `lancedb_dir_sha256: "DEFERRED_P1"`.
- Gate script (`check_p0_gate.py`) must not fail on the `DEFERRED_P1` sentinel. It should log a warning: `"W-15: vector index deferred to P1 — retrieval quality may be degraded"`.
- All four gates (`evidence_validity`, `schema_validity`, `trace_completeness`, `should_refuse_hit_rate`) are evaluated as before. If evidence_validity fails specifically due to sparse retrieval, the gate outcome is documented but the failure is attributed to W-15 (not a code defect).

### T-15: Day 10 Gate + Verification Audit

**Original entry:** T-14 complete.
**Amended entry:** Unchanged.
**Exit criteria — AMENDED:**

- T-07 verification row in the audit log is marked `DEFERRED_P1 (W-15)` instead of PASS/FAIL.
- §J.6 source-freeze invariant: the grep pattern for `data/lancedb_gold/eval_frozen/` violations is removed (directory does not exist).
- Tier-A reproducibility rerun uses SQL-only retrieval, same as T-13b.

### T-16: Dry-Run + Freeze + Deck + Rehearsal

**Original entry:** T-15 complete.
**Amended entry:** Unchanged.
**Exit criteria — AMENDED:**

- Demo notebook uses SQL-only retrieval.
- Deck slide covering retrieval architecture must note: "Vector similarity search (bge-m3 / LanceDB) deferred to P1; P0 uses SQL time-window retrieval as a functional baseline."

## 6. Updated Dependency DAG

```
T-01 ──┐
       ├─► T-02 ──► T-03 ──► T-04 ──► T-06 ──► T-08 ──┬─► T-09 ──► T-10 ──┐
                                                        └─► T-11 ───────────┘
       T-05 (parallel to T-04) ──┘                                          ▼
                                                                          T-12 ──► T-13a ──► T-13b ──► T-14 ──► T-15 ──► T-16

       T-07 ── [DEFERRED TO P1]
```

**Change from original:** T-06 now feeds directly into T-08 (was T-06 → T-07 → T-08). T-07 is removed from the critical path entirely.

## 7. Residual Risk

| Risk | Severity | Mitigation |
|---|---|---|
| SQL-only retrieval produces lower-quality evidence for Critic | Medium | Calibrate thresholds against SQL-only path at T-13b. Document as known limitation in deck. |
| `evidence_validity` gate may score lower without vector search | Medium | If gate fails, attribute to W-15 in audit log. Not a code defect. |
| P1 vector index build may require threshold recalibration | Low | Captured in P1 roadmap. Thresholds are already parameterized. |
| Defense audience asks about vector search | Low | Deck explicitly discloses SQL-only baseline with P1 upgrade path. Transparency > concealment. |

## 8. P1 Re-Entry Criteria

T-07 re-enters the roadmap in P1 when:

1. A GPU-enabled environment is available (cloud VM, Colab, or local GPU), OR
2. A quantized/distilled bge-m3 variant (e.g., ONNX int8) is validated for CPU inference within acceptable latency (~10,000 chunks in <30 minutes).

Upon T-07 completion in P1:
- Retrieval policy in `policy.py` activates the LanceDB hybrid path (code already gated).
- Critic thresholds are recalibrated against vector-enhanced retrieval.
- `lancedb_dir_sha256` is replaced with a real hash in all frozen artifacts.

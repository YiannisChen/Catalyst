# Catalyst P1/P2 Architecture Plan (v2)

**Author:** Senior Architect (Claude)
**Date:** 2026-05-04
**Revision:** v2 — W-06 promoted to P1; ADR numbering aligned with exec-spec §15; frozen-sample protocol tightened; G5 disposition added
**Status:** PROPOSED — pending Yiannis approval
**Relationship to P0:** This plan is the **continuation** of the P0 sprint (`2026-04-30-p0-construction-plan-v3.md`). P0 artifacts, frozen gates, and the `v1_2_p0_set.jsonl` golden set remain valid and are not superseded — they serve as the regression baseline for all P1/P2 work.
**Canonical references:** `full-version-execution-spec.md` (§5–§15), `full-version-design-delta.md`, `p0_completion_summary.md`, `2026-04-27-catalyst-fullversion-strategy.md` (G5 definition, sample-protocol rule)

---

## 0. Context & Constraints

### P0 Delivered State
- T-01 through T-16 all closed. Defense artifacts frozen at `20260503_154053`.
- Agent graph: Miner → Critic → DecisionRouter → Judge → Validator → Finalizer.
- 4-state output (SUFFICIENT / PARTIAL / INSUFFICIENT / SYSTEM_ERROR) operational.
- Eval harness: `direct_llm` vs `mcj_full` comparison on **10-case** frozen golden set (`v1_2_p0_set.jsonl`).
- Retrieval: **SQL-only** (W-15). LanceDB code gated but vector index absent.
- Tier-A reproducibility: zero status mismatches on rerun.
- Distribution: 3× INSUFFICIENT, 2× PARTIAL, 5× SUFFICIENT.

### P0 Waivers Carried Forward

| Waiver | Description | Target |
|--------|------------|--------|
| W-01 | `rag_only` baseline missing from eval matrix | **P1** |
| W-02 | Layer 3 retrieval (related entities) | P2 |
| W-03 | Critic LLM-graded sufficiency | P2 |
| W-04 | Budget circuit breaker | P2 |
| W-05 | Model routing (cheap critic / strong judge) | P2 |
| W-06 | LangSmith integration | **P1** *(promoted from P2 in v2)* |
| W-09 | GDELT corpus ingestion | **P1** |
| W-11 | Cross-source semantic dedup | P1 (conditional) |
| W-14 | Two-level chunking + reranker + sentence-offset | **P1** |
| W-15 | LanceDB vector index (bge-m3) | **P1** |

### Frozen-Sample Protocol (策略文档 §6.1 immutability rule)

The **P0 10-case set** (`v1_2_p0_set.jsonl`, distribution 5/2/3) and its frozen gate results are **immutable**. P1 may introduce an **expanded** golden set (`v1_2_p1_set.jsonl`) that is a strict superset of the P0 set, but the P0 set and P0 gate baselines are never retroactively modified. Any comparison that claims "P1 improved over P0" must report both the P0-set-only results (apples-to-apples) and the expanded-set results (if applicable) side by side.

### Working Constraints
- **Compute:** Mac CPU primary. Cloud GPU (rental) available for embedding batch jobs.
- **Time:** Quality-first, no hard deadline. Estimated cadence: P1 ~3–4 weeks, P2 ~3–4 weeks.
- **Collaboration:** Yiannis handles communication + confirmation; Claude (PM/architect) produces design, ADRs, task cards, reviews; Cursor executes coding.
- **Engineering standards:** All CLAUDE.md rules apply — conventional commits, no AI attribution, spec-driven coding, TDD.

### ADR Numbering Alignment

Exec-spec §15 reserves ADR-004 through ADR-007 for specific topics. Existing ADRs in repo: ADR-001, ADR-002, ADR-003. The following table is the **canonical assignment**:

| ADR | Reserved Topic (exec-spec §15) | Status |
|-----|-------------------------------|--------|
| ADR-004 | Adaptive 3-layer retrieval ordering (`direct → macro → related`) | **To write** — gate for P1-T04 |
| ADR-005 | Output status contract and downgrade policy | **Retrospective** — P0 implemented 4-state + validator; ADR documents rationale post-hoc |
| ADR-006 | Trace persistence schema and LangSmith alignment | **To write** — gate for P1-T14 (LangSmith) |
| ADR-007 | Budget circuit breaker thresholds and model routing policy | **To write** — gate for P2-T03/T04 |

New topics introduced by P1/P2 planning start at ADR-008:

| ADR | New Topic | Gate |
|-----|-----------|------|
| ADR-008 | Embedding generation strategy: cloud GPU batch → local LanceDB index build | Before P1-T01 |
| ADR-009 | Two-level chunking strategy + reranker model selection | Before P1-T03 |
| ADR-010 | Golden set expansion: statistical power analysis + sample protocol for P1 | Before P1-T10 |
| ADR-011 | G5 disposition: `packages/eval` standalone extraction — go/no-go and scope | Before P1-T15 or P2-T06 |

---

## 1. P1 Mission: "Vector Retrieval + LangSmith + Credible Eval Comparison"

### 1.1 Strategic Objective

P1 must answer the committee's core challenge: **"比直接问Claude强在哪？"**

This requires three things:
1. **Retrieval quality upgrade** — Vector-backed hybrid search so the evidence pipeline is actually competitive.
2. **Observability** — LangSmith integration so every run is inspectable and traceable end-to-end, making the "replayable" claim concrete.
3. **Three-config eval comparison** — `direct_llm` vs `rag_only` vs `mcj_full` on the same golden set, with statistical rigor.

Without (1), the eval measures a hobbled system. Without (2), "replayable" is a claim, not a fact. Without (3), there is no falsifiable argument.

### 1.2 P1 Task Breakdown

#### Phase P1-A: Data & Retrieval Foundation

**P1-T01: Cloud GPU Embedding Pipeline**
- **What:** Build a script (`scripts/build_embeddings_gpu.py`) that runs on a cloud GPU (Colab / Lambda / RunPod) to generate bge-m3 embeddings for all `clean_assets` rows in `catalyst_eval_frozen.db`.
- **Output:** Serialized embeddings file (`data/embeddings/bge_m3_eval_frozen.npy` + `data/embeddings/asset_id_index.json`).
- **Design decision:** Separate embedding generation (GPU) from index building (local). This avoids requiring GPU on the dev machine.
- **Acceptance:**
  - All `clean_assets` rows in frozen DB have corresponding embedding vectors.
  - Embedding dimensions = 1024 (bge-m3 default).
  - SHA256 of output files recorded for reproducibility.
- **ADR gate:** ADR-008 (embedding generation strategy) must be approved before implementation begins.

**P1-T02: LanceDB Gold Index Build (T-07 Revival)**
- **What:** Consume pre-computed embeddings from P1-T01 and build `data/lancedb_gold/eval_frozen/gold_chunks` table locally.
- **Entry:** P1-T01 complete.
- **Implementation:**
  - `scripts/build_index.py` already exists; extend to accept pre-computed embeddings instead of running bge-m3 inline.
  - Index includes: `asset_id`, `content_md`, `ticker`, `source_type`, `reference_date`, `vector` (1024-dim).
  - Health check: row count matches `clean_assets`, zero null vectors.
- **Acceptance:**
  - `data/lancedb_gold/eval_frozen/` contains valid `gold_chunks` table.
  - `lancedb_dir_sha256` is a real hash (replaces `DEFERRED_P1` sentinel).
  - Smoke retrieval: `SELECT * WHERE ticker='NVDA' LIMIT 5` returns results with vector similarity scores.

**P1-T03: Two-Level Chunking + Reranker (W-14)**
- **What:** Implement the chunking and reranking pipeline specified in the design delta §2.
- **Entry:** P1-T02 complete.
- **Implementation:**
  - Level 1: Document-level chunks (current `content_md` as-is).
  - Level 2: Sentence-level chunks with `parent_asset_id` back-pointer for context expansion.
  - Reranker: bge-reranker-v2-m3 (or lighter ONNX variant if CPU-only). Reranker can also run as a cloud batch job if needed.
  - Wire into `lancedb_store.py:hybrid_search()` — vector search → rerank top-K → return with `rerank_score`.
- **Acceptance:**
  - `hybrid_search(query, ticker, date_range)` returns results with both `rrf_score` and `rerank_score`.
  - Reranker path is gated: `if reranker_available:` — falls back to RRF-only gracefully.
  - Unit test covers both paths.
- **ADR gate:** ADR-009 (chunking strategy + reranker selection).

**P1-T04: Activate Vector-Backed Retrieval in Policy (W-15 Closure)**
- **What:** Flip the retrieval policy from SQL-only to hybrid (BM25 + vector + reranker) for Layer 1 and Layer 2.
- **Entry:** P1-T03 complete.
- **Implementation:**
  - `retrieval/policy.py` — `Layer.DIRECT` and `Layer.MACRO` now use `lancedb_store.hybrid_search()` when LanceDB is available.
  - SQL-only path remains as tested fallback (if `lancedb_gold/` absent).
  - Sufficiency check function (from design delta §2): `check_sufficiency(chunks, min_count=5, min_mean_score=0.02)`.
  - `Layer.RELATED` still raises `NotImplementedError` (deferred to P2-T01).
- **Acceptance:**
  - End-to-end run with vector retrieval produces higher evidence hit rates than SQL-only baseline.
  - W-15 sentinel replaced with real `lancedb_dir_sha256` in eval headers.
- **ADR gate:** ADR-004 (adaptive 3-layer retrieval ordering) — exec-spec §15 reserved slot.

**P1-T05: GDELT Corpus Ingestion (W-09)**
- **What:** Add GDELT connector to `data-core` for macro/geopolitical news.
- **Entry:** Independent (can parallel with P1-T01–T04).
- **Implementation:**
  - New connector: `catalyst_data/connectors/gdelt.py`.
  - Follows existing closure-based pattern (like Polygon/FMP).
  - Source type: `gdelt_news` (maps to `macro`/`geopolitical` in retrieval Layer 2).
  - Wrapped with `with_retry(...)`.
  - Backfill script for evaluation date range.
- **Acceptance:**
  - `gdelt_news` rows present in `clean_assets` for evaluation date window.
  - Provider audit passes for GDELT (same format as existing provider audits).
  - Dedup fingerprint applied (uses existing `dedup/hard.py`).
- **Design note:** GDELT strengthens Layer 2 (macro evidence) significantly. This is critical for the ~15 macro-driven golden events in the full 50-case set.

#### Phase P1-B: Agent & Eval Hardening

**P1-T06: rag_only Baseline Adapter (W-01)**
- **What:** Add a `rag_only` predict function that does retrieval + single-pass LLM synthesis without the Critic/Judge loop.
- **Entry:** P1-T04 complete (needs vector retrieval for fair comparison).
- **Implementation:**
  - New adapter: `catalyst_agents/adapter.py:rag_only_predict()`.
  - Prompt: retrieval context + query → single LLM call → structured `AttributionResult`.
  - No Critic grading, no policy expansion, no validation loop.
  - Uses same retrieval layer as `mcj_full` (Layer 1 only, no expansion).
- **Acceptance:**
  - Produces valid `AttributionResult` with `retrieved_evidence` populated.
  - `evidence_ids` are real (evidence_validity measurable).
  - Registered in experiment runner configs.

**P1-T07: Error Classification Enhancement (Design Delta §3)**
- **What:** Split the insufficient-evidence handler into system-error vs data-gap vs low-quality-evidence paths.
- **Entry:** P1-T04 complete.
- **Implementation:**
  - Add `error_log: list[dict]` to `AttributionState` (schema from design delta §3).
  - DecisionRouter: check `error_log` for `api_failure`/`parse_failure` → route to `SYSTEM_ERROR` before checking evidence.
  - Critic outputs now include `error_type` classification.
  - Failure taxonomy doc updated with new error paths.
- **Acceptance:**
  - `SYSTEM_ERROR` status is reachable and distinguishable from `INSUFFICIENT`.
  - At least 1 regression test per error path.
  - Failure taxonomy doc has corresponding entries.

**P1-T08: Connector Retry Unification (Design Delta §4)**
- **What:** Ensure all connectors (including FRED, yfinance, and new GDELT) use `with_retry(...)` with consistent error classification.
- **Entry:** Independent.
- **Implementation:**
  - FRED connector: wrap with `with_retry(...)`.
  - yfinance fallback: wrap with `with_retry(...)`.
  - GDELT (from P1-T05): already wrapped at creation.
  - Unified retry policy: 429 → exponential + Retry-After; 5xx → exponential max 3; timeout → immediate once then exponential; 4xx (non-429) → no retry.
  - Each connector emits structured error into `error_log` format (feeds P1-T07).
- **Acceptance:**
  - `grep -r "with_retry" packages/data-core/catalyst_data/connectors/` covers all connector files.
  - Retry policy table documented in code docstring.

**P1-T09: Cross-Source Semantic Dedup (W-11)**
- **What:** Implement semantic dedup to catch near-duplicate articles across different providers (e.g., same news from Polygon and GDELT).
- **Entry:** P1-T05 complete (GDELT adds cross-source overlap).
- **Implementation:**
  - `catalyst_data/dedup/semantic.py` — uses embedding similarity (threshold to calibrate).
  - Operates at Silver layer: after `clean.py`, before Gold index build.
  - Fallback: if embeddings unavailable, skip semantic dedup (title-based dedup from `hard.py` still active).
- **Acceptance:**
  - Duplicate articles from different providers are flagged with `dedup_cluster_id`.
  - Only one representative per cluster enters Gold index.
  - Unit test with known duplicate pair.
- **Conditional:** Only implement if GDELT ingestion produces measurable overlap with Polygon news. Check overlap rate first; if <5%, defer to P2.

#### Phase P1-C: Observability

**P1-T14: LangSmith Integration (W-06) — promoted from P2**
- **What:** Enable LangSmith auto-tracing via env vars, aligned with existing local `trace_id`.
- **Entry:** P1-T04 complete (vector retrieval active, so traces capture the real pipeline).
- **Rationale for promotion:** The exec-spec (§10) positions trace persistence as a P1 foundation concern, not a P2 polish item. ADR-006 (exec-spec §15) explicitly pairs trace schema with LangSmith alignment. Additionally, the "replayable attribution system" value proposition requires observable traces — this is a differentiator to present to the committee, not a nice-to-have.
- **Implementation:**
  - Set `LANGCHAIN_TRACING_V2=true`, `LANGCHAIN_PROJECT=catalyst`.
  - Each `graph.invoke()` passes `trace_id` as run metadata.
  - LangSmith is visualization layer; local SQLite trace table remains source of truth.
  - Query utility: "Show traces for NVDA on 2025-01-27, sorted by cost."
  - Graceful degradation: `LANGCHAIN_TRACING_V2=false` (no LangSmith dependency for core flow).
- **Acceptance:**
  - Traces visible in LangSmith dashboard for P1 eval runs.
  - `trace_id` links local DB trace to LangSmith run.
  - Core graph runs with identical behavior when LangSmith env vars are absent.
  - P1 comparison report headers include `langsmith_project` field.
- **ADR gate:** ADR-006 (trace persistence schema and LangSmith alignment) — exec-spec §15 reserved slot.

#### Phase P1-D: Evaluation & Comparison

**P1-T10: Golden Set Expansion**
- **What:** Expand the golden set from the P0 10-case base (`v1_2_p0_set.jsonl`) to a P1 set that enables statistically meaningful three-config comparison.
- **Entry:** Independent analysis task. Must complete before P1-T11.
- **Frozen-sample rule:** The P1 set (`v1_2_p1_set.jsonl`) must be a **strict superset** of the P0 set. The P0 10-case set is never modified. The P1 set adds new cases; it does not alter, remove, or re-weight any P0 case.
- **Statistical power target:** A Wilcoxon signed-rank test with n=10 can only detect very large effects (Cohen's d > 0.8). For moderate effect detection (d ≈ 0.5) at α=0.05, power=0.80, the minimum sample is ~27 paired observations. Recommendation: expand to ≥30 cases; use the full 50-case `v1_2.jsonl` if annotation quality permits.
- **Implementation:**
  - Validate and annotate additional cases from `v1_2.jsonl` (40 remaining).
  - Ensure category distribution covers: earnings, macro, sector rotation, cross-entity events.
  - Each new case follows existing `GoldenEvent` schema with AR-weighted causes.
- **Acceptance:**
  - `v1_2_p1_set.jsonl` exists with ≥30 cases.
  - First 10 lines are byte-identical to `v1_2_p0_set.jsonl`.
  - Category distribution documented.
- **ADR gate:** ADR-010 (golden set expansion + statistical power analysis).

**P1-T11: Full Three-Config Experiment Runner (Design Delta §8)**
- **What:** Run `direct_llm` vs `rag_only` vs `mcj_full` on the P1 golden set with statistical reporting.
- **Entry:** P1-T06 + P1-T04 + P1-T10 + P1-T14 all complete.
- **Frozen-sample rule:** The experiment runner executes on `v1_2_p1_set.jsonl`. Additionally, it **must** produce a separate "P0-subset" comparison restricted to the original 10 P0 cases, enabling an apples-to-apples delta report against the P0 frozen baselines. The P0-subset results are the primary evidence for "P1 improved over P0"; the full-set results are the primary evidence for "MCJ beats direct_llm."
- **Implementation:**
  - Extend `scripts/run_experiments.py` to support all three configs.
  - Output: `ComparisonReport` as both Markdown table and JSON.
  - Statistical rigor (from design delta §8c):
    - Mean ± std for each metric per config.
    - Paired Wilcoxon signed-rank test between configs (full set).
    - Per-category breakdown (earnings vs macro vs sector).
    - P0-subset comparison table (10-case apples-to-apples).
  - Cost/latency reported alongside quality metrics (per execution spec §11).
  - All runs traced to LangSmith (P1-T14).
- **Acceptance:**
  - `data/eval_reports/p1_experiment_comparison.{md,json}` generated.
  - Report includes: (a) full-set results, (b) P0-subset results, (c) quality + cost + latency.
  - At least one metric shows statistically significant improvement of `mcj_full` over `direct_llm` (p < 0.05), OR the failure is documented with root cause analysis and honest disclosure.

**P1-T12: Threshold Recalibration**
- **What:** Recalibrate eval thresholds (K_partial, K_sufficient, M_threshold) against vector-enhanced retrieval.
- **Entry:** P1-T11 complete.
- **Implementation:**
  - Rerun threshold sweep from T-13b but against vector retrieval results on the P1 golden set.
  - Compare with P0 SQL-only thresholds.
  - Document any changes and rationale.
- **Acceptance:**
  - New thresholds documented with calibration curve.
  - Comparison with P0 thresholds shows whether vector retrieval changed the calibration landscape.

**P1-T13: P1 Freeze + Verification Audit**
- **What:** Freeze P1 artifacts, run verification audit, update defense materials.
- **Entry:** All P1-T01 through P1-T12 and P1-T14 complete.
- **Implementation:**
  - New frozen comparison with vector retrieval.
  - Update `lancedb_dir_sha256` in all headers (real hash replaces `DEFERRED_P1`).
  - Tier-A reproducibility rerun on P1 golden set.
  - Update deck to reflect P1 improvements.
- **Acceptance:**
  - All P0 gates remain GREEN when evaluated on P0-subset.
  - W-15 fully resolved (real `lancedb_dir_sha256`).
  - W-06 resolved (LangSmith traces present).
  - P1 comparison report shows delta vs P0 baseline.

#### Phase P1-E: G5 Decision Gate

**P1-T15: G5 Disposition — `packages/eval` Standalone Extraction**
- **What:** Make the go/no-go decision on extracting `packages/eval` as a standalone, finance-agnostic eval harness (the "G5" item from strategy doc §2.2).
- **Entry:** P1-T13 complete (need stable eval package to assess extraction feasibility).
- **Context:** The strategy doc marked G5 as "explicitly cuttable" from P0. Now that P0 is delivered and P1 eval improvements are landing, the decision must be made: extract now, defer to P2, or drop from scope.
- **Decision criteria:**
  - **Go (P1):** If the eval harness is already package-independent (no imports from `catalyst_agents` or `catalyst_data` in the metrics/schema/runner core), extraction is low-cost and creates a reusable career asset per Teacher Mac's philosophy.
  - **Go (P2):** If significant refactoring is needed to decouple, defer to P2-T06 (package independence) and bundle with data-core extraction.
  - **No-go:** If eval is too tightly coupled to the attribution domain and "finance-agnostic" is not achievable without rewriting — document as a post-v1 aspiration.
- **Acceptance:**
  - ADR-011 written with the decision and rationale.
  - If "Go (P1)": extraction task scoped and added to P1 backlog.
  - If "Go (P2)" or "No-go": documented with clear justification.

### 1.3 P1 Dependency DAG

```
                         ┌─────────────────────────────────────────────────────────────────┐
                         │                    CRITICAL PATH (red)                           │
                         └─────────────────────────────────────────────────────────────────┘

P1-T01 (GPU embed) ──► P1-T02 (Lance index) ──► P1-T03 (chunk+rerank) ──► P1-T04 (vector retrieval)
  [ADR-008]                                        [ADR-009]                  [ADR-004]
                                                                                  │
                                                                    ┌─────────────┼─────────────┐
                                                                    ▼             ▼             ▼
                                                              P1-T06          P1-T07       P1-T14
                                                            (rag_only)     (error class)  (LangSmith)
                                                              [W-01]        [Delta §3]     [W-06]
                                                                    │                    [ADR-006]
PARALLEL TRACKS:                                                    │             │
                                                                    ▼             │
P1-T05 (GDELT) ─► P1-T09 (semantic dedup, cond.)                   │             │
  [W-09]            [W-11]                                          │             │
                                                                    ▼             ▼
P1-T08 (retry unification) ──────────────────────► joins ──► P1-T11 (3-config experiment)
  [Delta §4]                                                    [Delta §8, ADR-010]
                                                                        │
P1-T10 (golden set expansion) ──────────────────────────────► joins ────┘
  [ADR-010]                                                             │
                                                                        ▼
                                                                  P1-T12 (threshold recalib)
                                                                        │
                                                                        ▼
                                                                  P1-T13 (P1 freeze + audit)
                                                                        │
                                                                        ▼
                                                                  P1-T15 (G5 decision gate)
                                                                    [ADR-011]
```

**Critical path:** T01 → T02 → T03 → T04 → T06 → T11 → T12 → T13 → T15

**Parallel tracks (can start day 1):**
- P1-T05 (GDELT) + P1-T08 (retry) + P1-T10 (golden set expansion)
- P1-T14 (LangSmith) starts after T04, runs parallel with T06/T07

### 1.4 P1 ADR Backlog (aligned with exec-spec §15)

| ADR | Topic | Source | Gate |
|-----|-------|--------|------|
| **ADR-004** | Adaptive 3-layer retrieval ordering (`direct → macro → related`) | exec-spec §15 | Before P1-T04 |
| **ADR-005** | Output status contract and downgrade policy *(retrospective)* | exec-spec §15 | P1 (documents P0 decisions) |
| **ADR-006** | Trace persistence schema and LangSmith alignment | exec-spec §15 | Before P1-T14 |
| **ADR-008** | Embedding generation strategy: cloud GPU batch → local index | P1 plan | Before P1-T01 |
| **ADR-009** | Two-level chunking strategy + reranker model selection | P1 plan | Before P1-T03 |
| **ADR-010** | Golden set expansion: statistical power analysis + P1 sample protocol | P1 plan | Before P1-T10 |
| **ADR-011** | G5 disposition: eval package extraction go/no-go | Strategy §2.2 | At P1-T15 |

### 1.5 P1 Exit Criteria

- [ ] W-15 resolved: `lancedb_dir_sha256` is a real hash in frozen artifacts.
- [ ] W-01 resolved: `rag_only` baseline in experiment comparison.
- [ ] W-06 resolved: LangSmith traces present for P1 eval runs, linked by `trace_id`.
- [ ] W-14 resolved: reranker integrated (or ONNX fallback documented in ADR-009).
- [ ] W-09 resolved: GDELT corpus ingested and available in retrieval Layer 2.
- [ ] Three-config comparison report published with: (a) full-set statistical tests, (b) P0-subset apples-to-apples delta.
- [ ] All P0 gates remain GREEN when evaluated on P0-subset with vector retrieval.
- [ ] Tier-A reproducibility rerun passes (zero status mismatches).
- [ ] G5 disposition documented in ADR-011.
- [ ] ADR-004 through ADR-006, ADR-008 through ADR-011 all written.

---

## 2. P2 Mission: "Governance, Cost Control, and Operational Maturity"

### 2.1 Strategic Objective

P2 transforms Catalyst from "a system that works" into "a system you can trust and operate." The focus shifts from retrieval quality to operational governance: budget control, model routing, and package maturity.

### 2.2 P2 Task Breakdown

**P2-T01: Layer 3 Retrieval — Related Entities (W-02)**
- **What:** Implement the third retrieval layer using static peer entity map.
- **Implementation:**
  - `retrieval/policy.py` — `Layer.RELATED` activates when Layer 1 + Layer 2 combined yield < 3 chunks.
  - Static `PEER_MAP` from design delta §2 (NVDA→AMD/TSM/..., etc.).
  - Each chunk tagged with `evidence_tier: 3`.
  - Expansion bounded: `max_layers=3`, `max_expansions=2`.
- **Acceptance:**
  - Layer 3 retrieval functional for all tickers in `PEER_MAP`.
  - `retrieval_metadata` in output shows `layers_used: [1,2,3]` when all layers triggered.
  - Unit test: query for a macro event triggers Layer 2 → Layer 3 escalation.
- **Note:** ADR-004 (written in P1) already covers the 3-layer ordering design. P2-T01 is the implementation of Layer 3 specifically.

**P2-T02: Critic LLM-Graded Sufficiency (W-03)**
- **What:** Replace rule-based sufficiency check with LLM-graded assessment.
- **Implementation:**
  - Critic prompt extended to output structured `sufficiency` grade.
  - `magnitude_coverage: float` — what fraction of the price move is explained by retrieved evidence.
  - `next_action: proceed | expand_macro | expand_related | refuse`.
  - DecisionRouter consumes Critic `next_action` (already wired from P0 T-09).
- **Acceptance:**
  - Critic output includes all three fields.
  - DecisionRouter tests updated to cover all `next_action` values.
  - Eval comparison shows whether LLM-graded sufficiency improves refusal precision.

**P2-T03: Budget Circuit Breaker (W-04)**
- **What:** Enforce per-run budget caps on cost, tokens, and latency.
- **Implementation:**
  - `cost_tracker.py` already exists; extend with hard caps.
  - Caps configurable: `max_cost_usd`, `max_tokens`, `max_latency_ms`.
  - On budget overflow: stop expansion → force `PARTIAL` with `budget_flag=True`.
  - DecisionRouter rule: `budget_overflow → PARTIAL` (from execution spec §6).
- **Acceptance:**
  - Run with artificially low budget → status is `PARTIAL` with budget flag.
  - Normal run stays within default budget.
  - Eval report includes cost/latency alongside quality metrics.
- **ADR gate:** ADR-007 (budget circuit breaker thresholds and model routing policy) — exec-spec §15 reserved slot.

**P2-T04: Model Routing Policy (W-05)**
- **What:** Use cheaper model for Critic (grading), stronger model for Judge (synthesis).
- **Implementation:**
  - Config-driven model selection per node role.
  - Default: Critic → `claude-haiku` or `gemini-flash`; Judge → `claude-sonnet`.
  - `model_id` recorded in trace per node (already in P1-T14 trace schema).
- **Acceptance:**
  - Same golden set, model-split config vs uniform config → comparable quality at lower cost.
  - Eval report compares cost reduction vs quality delta.
- **ADR gate:** ADR-007 (shared with P2-T03).

**P2-T05: data-core Package Independence (Design Delta §11)**
- **What:** Make `catalyst-data` installable as standalone package.
- **Implementation:**
  - Dependency tiers in `pyproject.toml`: core (httpx, pydantic) vs vector (lancedb, FlagEmbedding).
  - `__version__` in `catalyst_data/__init__.py`.
  - CLI entry point: `catalyst-data ingest --ticker NVDA --date 2025-01-27`.
  - Independence test: fresh venv install without ML dependencies succeeds.
- **Acceptance:**
  - `pip install ./packages/data-core && python -c "from catalyst_data.storage.sqlite import init_db; print('OK')"` succeeds.
  - CLI `--help` works.
- **G5 linkage:** If ADR-011 decided "Go (P2)" for eval extraction, bundle eval package extraction into this task or create P2-T05b.

**P2-T06: Eval Matrix Expansion (W-07 partial)**
- **What:** Expand eval to cover more configs and report weekly trends.
- **Implementation:**
  - Add ablation configs: `mcj_no_critic`, `mcj_no_validator`.
  - Per-category breakdown in comparison report.
  - Markdown trend report template in `data/eval_reports/`.
- **Acceptance:**
  - ≥5 configs in comparison matrix.
  - Per-category (earnings, macro, sector) breakdown visible.

**P2-T07: P2 Freeze + Final Defense Update**
- **What:** Freeze P2 artifacts, produce final defense package.
- **Acceptance:**
  - All W-xx waivers resolved or explicitly documented as post-v1 with ADR justification.
  - Complete comparison report with quality + cost + latency across all configs.
  - Updated deck and rehearsal-ready materials.

### 2.3 P2 Dependency DAG

```
P2-T01 (Layer 3)         [W-02] ──────┐
P2-T02 (Critic LLM)      [W-03] ──────┤
P2-T03 (Budget breaker)  [W-04] ──────┼──► P2-T06 (eval matrix) ──► P2-T07 (freeze)
P2-T04 (Model routing)   [W-05] ──────┤        [W-07]
         [ADR-007 gates T03+T04]       │
                                       │
P2-T05 (pkg independence) [§11] ───────┘  (independent, any time in P2)
         [+ G5 if ADR-011 = "Go P2"]
```

Most P2 tasks are independent and can be parallelized.

### 2.4 P2 ADR Backlog

| ADR | Topic | Source | Gate |
|-----|-------|--------|------|
| **ADR-007** | Budget circuit breaker thresholds + model routing policy | exec-spec §15 | Before P2-T03/T04 |

No new ADR slots needed for P2 beyond the exec-spec §15 reservation.

---

## 3. Risk Register

| Risk | Phase | Severity | Mitigation |
|------|-------|----------|------------|
| bge-m3 on cloud GPU takes longer/costs more than expected | P1 | Medium | Fallback: use `all-MiniLM-L6-v2` (384-dim) which runs on CPU. Document as ADR-008 variant. |
| Vector retrieval doesn't improve eval scores significantly | P1 | **High** | Root-cause analysis: retrieval quality vs golden set coverage vs metric sensitivity. Publish honest comparison regardless — the protocol forbids cherry-picking. |
| GDELT API unreliable or data quality low | P1 | Low | GDELT is additive; if quality poor, defer and rely on existing Polygon news for Layer 2. |
| Golden set expansion introduces poorly-annotated cases | P1 | Medium | ADR-010 defines annotation quality criteria. Cases that fail inter-rater check are excluded. |
| P0-subset regression: vector retrieval degrades P0 gate scores | P1 | Medium | If detected, root-cause before proceeding. Do not tune thresholds to paper over regression — report honestly. |
| LangSmith adds latency to critical path | P1 | Low | LangSmith is async/optional; local trace persistence is the source of truth. Graceful degradation tested. |
| Budget breaker changes output distribution | P2 | Low | Run eval with and without budget caps; report delta. |
| G5 extraction requires extensive eval refactoring | P1/P2 | Low | ADR-011 captures go/no-go. If no-go, no work wasted. |

---

## 4. Cursor Task Card Template

Each task card dispatched to Cursor should follow this contract:

```markdown
## Task: P1-TXX — [Title]

### Interface Contract
- Input: [exact file paths and function signatures]
- Output: [exact file paths and expected artifacts]
- Tests: [test file path and minimum test count]

### Specification Reference
- Design delta section: §X
- Execution spec section: §Y
- ADR: ADR-XXX (must be approved before implementation)

### Acceptance Criteria (machine-verifiable)
1. [criterion 1]
2. [criterion 2]

### Verification Command
```bash
[exact command to verify]
```

### Scope Boundary
- IN: [what to implement]
- OUT: [what not to touch]

### Frozen-Sample Rule (if eval-related)
- P0 golden set (`v1_2_p0_set.jsonl`) must not be modified.
- P1 golden set (`v1_2_p1_set.jsonl`) must be a strict superset.
- Comparison reports must include P0-subset results.
```

---

## 5. Review Protocol

For each Cursor-completed task, Claude (PM/Architect) reviews:

1. **Spec compliance:** Does the implementation match the design delta / execution spec / ADR?
2. **Interface stability:** Are public APIs backward-compatible with P0 callers?
3. **Test coverage:** Are failure paths covered, not just happy paths?
4. **Performance:** Does the change introduce unbounded resource consumption?
5. **Isolation:** Does the change stay within its declared scope boundary?
6. **Frozen-sample integrity:** If the task touches eval, are P0 artifacts untouched?

Review verdict format:
```
VERDICT: APPROVE | REQUEST_CHANGES | BLOCK
BLOCKING ISSUES: [list, if any]
NEXT COMMAND: [exact Cursor command or git operation]
```

---

## 6. Success Narrative (for committee/defense)

After P1: "We upgraded from SQL-only retrieval to hybrid vector search with reranking, added macro-event evidence via GDELT, integrated LangSmith for full-run observability, and produced a statistically rigorous three-way comparison on [N] golden events showing that the MCJ pipeline produces [X]% higher evidence validity and [Y]% better attribution F1 than direct LLM answers, at [Z] cost per query. Every run is traceable in LangSmith and reproducible from frozen artifacts."

After P2: "We added budget governance with per-run cost caps, model routing for cost efficiency, and published the data-core package as a standalone reusable asset. The system now has deterministic failure handling, budget circuit breakers, and a complete eval matrix across 5+ configurations with per-category breakdowns."

This is the "evidence-governed attribution system" positioning from the value strategy — measurable, falsifiable, and honest about limitations.

---

## Appendix A: Full Waiver-to-Task Traceability

| Waiver | Description | Phase | Task(s) | ADR |
|--------|------------|-------|---------|-----|
| W-01 | `rag_only` baseline | P1 | P1-T06 | — |
| W-02 | Layer 3 retrieval | P2 | P2-T01 | ADR-004 |
| W-03 | Critic LLM-graded sufficiency | P2 | P2-T02 | — |
| W-04 | Budget circuit breaker | P2 | P2-T03 | ADR-007 |
| W-05 | Model routing | P2 | P2-T04 | ADR-007 |
| W-06 | LangSmith | P1 | P1-T14 | ADR-006 |
| W-07 | Full eval matrix | P2 | P2-T06 | — |
| W-08 | Substituted in P0 | — | — | — |
| W-09 | GDELT corpus | P1 | P1-T05 | — |
| W-10 | N/A (numbering gap) | — | — | — |
| W-11 | Semantic dedup | P1 | P1-T09 (cond.) | — |
| W-12 | Backfill window (conditional) | — | Not triggered | — |
| W-13 | Notebook fallback (dormant) | — | Not active | — |
| W-14 | Chunking + reranker | P1 | P1-T03 | ADR-009 |
| W-15 | LanceDB vector index | P1 | P1-T01/T02/T04 | ADR-008 |

## Appendix B: Design Delta Traceability

| Delta § | Topic | Phase | Task(s) | Status |
|---------|-------|-------|---------|--------|
| §1 | Orchestrator concurrency | — | — | Closed in midterm |
| §2 | Tiered retrieval (3-layer) | P1+P2 | P1-T04 (L1+L2), P2-T01 (L3) | ADR-004 |
| §3 | Error classification | P1 | P1-T07 | — |
| §4 | Connector retry unification | P1 | P1-T08 | — |
| §5 | Eval schema fix | — | — | Closed in P0 |
| §6 | Attribution F1 calibration | — | — | Closed in P0 (threshold cal) |
| §7 | Dedup consolidation | P1 | P1-T09 | Closed in P0 + semantic in P1 |
| §8 | Baseline experiments | P1 | P1-T11 | ADR-010 (golden set) |
| §9 | Observability / LangSmith | P1 | P1-T14 | ADR-006 |
| §10 | SequentialRunner elimination | — | — | Closed in midterm |
| §11 | Package independence | P2 | P2-T05 | ADR-011 (G5) |

## Appendix C: Exec-Spec §13 Phase Mapping

| Exec-Spec Phase | Description | Catalyst Phase | Status |
|-----------------|-------------|---------------|--------|
| Phase 1 (foundation) | Status enum, validator, trace, direct_llm | P0 | **Done** |
| Phase 2 (agentic core) | 3-layer retrieval, critic contract, router | P1 (L1+L2) + P2 (L3) | **P1 in progress** |
| Phase 3 (hardening) | Harness taxonomy, budget breaker, model routing | P2 | Planned |
| Phase 4 (evaluation) | Full baseline comparison, quality+cost+latency | P1 (3-config) + P2 (matrix) | **P1 in progress** |

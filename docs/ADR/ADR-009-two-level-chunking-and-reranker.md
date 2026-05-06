> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# ADR-009: Two-Level Chunking and Reranker (L1 Asset / L2 Sentence)

**Status:** Proposed  
**Date:** 2026-05-04  
**Decision:** Standardize **L1 document-level chunks** aligned to **`clean_assets` rows** plus **L2 sentence-level chunks** with **`parent_asset_id`**, and require **`hybrid_search`** outputs to expose both **`rrf_score`** (fusion rank proxy) and **`rerank_score`** when a reranker is attached — with graceful **CPU / ONNX / absent-model** degradation.

## Context

[ADR-002](docs/ADR/ADR-002-hybrid-rag-retrieval.md) locked hybrid BM25 + vector + RRF, followed by cross-encoder reranking, for attribution quality. P0 retrieval often operated on coarse `content_md` units; P1 (**W-14**) needs explicit **two-level chunking** so the Miner can surface fine-grained evidence while preserving a stable upward link for context expansion.

Today, `packages/data-core/catalyst_data/storage/lancedb_store.py` documents that **RRF fusion** happens inside `hybrid_search`, while `_apply_reranker` in the same module can attach `rerank_score` when invoked from callers (see Miner/policy wiring).

## Decision

- **L1 (document chunk):** One logical chunk per **`clean_assets` row**, using **`content_md`** as the canonical body (equivalent semantics to today’s asset row in SQL fallback).
- **L2 (sentence chunk):** Sentence-split (or equivalently bounded sub-document units) chunks stored as **indexed rows**, each carrying:
  - `parent_asset_id` → deterministic foreign key to the L1 `asset_id`
  - shared metadata inherited from parent: `ticker`, `source_type`, `reference_date`, etc., as applicable to query filters
- **L2 embeddings:** L2 sentence chunks are embedded individually (each row carries its own 1024-dim vector aligned with ADR-008). The ADR-008 coverage invariant extends to include L2 rows produced by the chunking pipeline.
- **Index / Lance schema expectations (logical contract):**
  - Vector column remains **aligned with ADR-008** (1024 dims for canonical bge-m3).
  - Rows must remain filterable by **ticker** (L1/direct) vs **macro source types without ticker** (L2/macro policy path) consistent with `retrieval/policy.py` source-type sets.
  - **`parent_asset_id`** is mandatory on L2 rows; null only if the row itself is L1 (single-level legacy mode — forbidden for P1 shipped path once L2 exists).
- **Reranker model:** Canonical cross-encoder is **`BAAI/bge-reranker-v2-m3`** (per ADR-002 / `lancedb_store.py` constant). ONNX or int8 variants are acceptable **provided** they preserve the `(query, chunk_text)` scoring contract within a calibrated error budget documented in calibration notes.
- **Degradation policy:** At runtime `if reranker_available:` use reranked ordering; otherwise return RRF-ranked chunks with **`rerank_score` omitted or explicitly null** — never crash the pipeline. CPU-only ONNX falls into this graded availability ladder.
- **Score contract on returned chunk dicts:**
  - **`rrf_score`:** Present for fused hybrid outputs (preserve existing RRF-derived semantics).
  - **`rerank_score`:** Present when reranker ran successfully on that chunk after fusion shortlist selection.
  - Consumers (Critic thresholds, sufficiency checks) MUST NOT assume rerank scores exist when gated off — use `rrf_score` for fallback statistics (consistent with architecture plan §1.2 P1-T04 `check_sufficiency` examples using mean RRF).
- **Testing expectations (ADR level):**
  - At least **one automated test** where reranker is **stubbed/null** → results include **`rrf_score`**, rerank stage skipped.
  - At least **one automated test** where reranker is **present** → results include **both** `rrf_score` and `rerank_score` keys for returned rows.

## Alternatives considered

- **Vectors on sentences only:** Rejected — loses efficient document-coarse retrieval stage and balloons index without proven eval gain.
- **Always-on heavy reranker:** Rejected — violates graceful degradation requirement for constrained laptops / CI.
- **Single-level chunk with character windows:** Rejected — harder provenance storytelling vs explicit `parent_asset_id`.

## Consequences

**Positive**

- Better precision for citations while retaining stable asset lineage.
- Explicit score fields make Critic-grade debugging and LangSmith overlays interpretable.

**Negative**

- Index row multiplication increases build time / storage vs L1-only.
- Sentence splitter edge cases require QA (financial tickers vs sentence boundaries).

**Follow-up tasks**

- Run an explicit **`T03-pre`** parameter-selection gate before shipping **P1-T03**, recording the chosen **sentence splitter**, **`max_sentences_per_asset`**, and reranker shortlist / top-k settings in **`docs/decisions/chunking-ablation-{date}.md`**.
- Extend `hybrid_search` / store layer so rerank attachment behavior matches this ADR uniformly (P1-T03 scope).
- Calibrate thresholds when mean score statistics mix RRF-only and rerank-available runs.

## References

- `docs/full-version-design-delta.md` — tiered retrieval and `check_sufficiency` sketch (§2).
- `docs/full-version-execution-spec.md` — retrieval + observability context (§§5, 10).
- `docs/plans/2026-05-04-p1-p2-architecture-plan.md` — P1-T03 acceptance (`hybrid_search` returns `rrf_score` + `rerank_score`, gated reranker).
- [ADR-002](docs/ADR/ADR-002-hybrid-rag-retrieval.md) — original hybrid + rerank decision.

## Implementation notes

- **Planning guardrail:** `T03-pre` is a **parameter-selection gate only**. It is not a scope-reduction gate and must not be used to defer **L2 / W-14** to P2.
- **L2 scope lock (T03-preb):** L2 sentence splitting is prose-only (starting with `polygon_news`); structured types remain L1-only.
- **Primary wiring surface:** `catalyst_data.storage.lancedb_store.hybrid_search` and `catalyst_data.storage.lancedb_store._apply_reranker` (rerank enrichment pattern).
- **Policy integration:** `catalyst_agents.retrieval.policy.retrieve` optional `rerank` parameter already forwards to `_apply_reranker` when Lance path is active.
- Miner/graph code paths that bypass `retrieve(..., rerank=...)` must be audited during P1-T03 so scores remain consistent.
- **Open coordination note:** The exact canonical embedding object consumed by the shipped P1 index (**L1 `clean_assets` rows** vs **final L1+L2 chunk rows**) remains an ADR-008/ADR-009 coordination decision unless separately frozen by architect instruction; this ADR does not resolve that question by itself.

### Related ADRs

- **ADR-008** — Where 1024-dim embeddings are produced before chunk rows land in Lance.
- **ADR-004** — Layer 1 vs Layer 2 activation reads these chunks through policy.
- **ADR-006** — Traces should remain interpretable when reranker gates change between runs.

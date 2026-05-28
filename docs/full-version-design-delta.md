> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# Catalyst Full-Version Design Delta

**Author:** Yiannis Chen
**Date:** 2026-04-15
**Status:** Draft — baseline refreshed
**Base document:** [System Design Spec v1.1](superpowers/specs/2026-04-02-catalyst-system-design.md)

> This document enumerates every point where the full-version system design must diverge from the v1.1 spec. Each delta references the original spec section, states what must change, and provides the design rationale.

---

## 1. Orchestrator Concurrency Model (Spec §3, Orchestrator)

### Current baseline

The midterm baseline already runs sources concurrently via `asyncio.gather(...)` inside `process_request(...)`. The real signature is:

```
process_request(ticker, date, sources, db_path, fetch_fn, limiter=None, *, conn=None)
```

`conn` is deprecated and ignored. SQLite writes are still synchronous at the storage layer, but they are offloaded with `asyncio.to_thread(...)`, and each worker opens its own connection from `db_path`. This avoids shared-connection thread hazards without introducing `aiosqlite`.

### Full-version delta

The main architectural question is no longer "sequential vs concurrent" because concurrency is already implemented. The remaining full-version decision is whether to keep the current async-orchestrator + thread-offloaded SQLite model or move to a fully async storage layer.

**Option A:** Keep the current model.

```
process_request(...)
    └── asyncio.gather(...)
            └── asyncio.to_thread(_store_bronze_and_silver, db_path=...)
```

This is acceptable for the current single-user research baseline and keeps the storage code simple.

**Option B:** Move storage to `aiosqlite` later if end-to-end async semantics become important.

This would be a future simplification or throughput improvement, not a prerequisite to make the baseline honest or correct.

---

## 2. Tiered Retrieval Strategy (Spec §4.3, Miner Node + §6.4, RAG Pipeline)

### Current spec assumption

The Miner performs a single hybrid search filtered by `ticker + date ±3 days`. The spec does not describe what happens when this retrieval returns insufficient or low-quality evidence for cross-entity events (tariffs, Fed decisions, sector rotations).

### What must change

Replace the single-pass retrieval with a **three-layer progressive strategy**:

```
Layer 1 — Direct evidence
    WHERE ticker = $ticker AND date BETWEEN $start AND $end
    → If reranked >= 5 high-relevance chunks → STOP, proceed to Critic.

Layer 2 — Related entities
    → Triggered when Layer 1 yields < 5 chunks OR mean RRF score < threshold.
    → Expand search to peer tickers from a static entity graph:
        NVDA → {AMD, TSM, MSFT, META, GOOGL}
        AAPL → {TSM, QCOM, AVGO}
    → WHERE ticker IN ($peers) AND date BETWEEN $start AND $end
    → Merge with Layer 1 via RRF, de-duplicate by asset_id.

Layer 3 — Market-wide fallback
    → Triggered when Layer 1 + Layer 2 combined yield < 3 chunks.
    → Remove ticker filter entirely.
    → WHERE date BETWEEN $start AND $end AND source_type IN ('macro', 'geopolitical')
    → This captures tariff announcements, Fed decisions, broad market events.
```

Each chunk carries an `evidence_tier: 1 | 2 | 3` tag. The Critic can use this as a prior (direct evidence is more likely relevant than market-wide fallback).

### New state field

Add to `AttributionState`:

```
retrieval_metadata: dict  # {layers_used: [1,2,3], sufficiency_score: float, peer_tickers_queried: list[str]}
```

### Sufficiency check function

A lightweight rule-based check (no LLM) between each layer:

```
def check_sufficiency(chunks, min_count=5, min_mean_score=0.02) -> bool:
    if len(chunks) < min_count:
        return False
    mean_score = sum(c["rrf_score"] for c in chunks) / len(chunks)
    return mean_score >= min_mean_score
```

### Entity graph

For the midterm-to-full transition, a static mapping table is sufficient:

```python
PEER_MAP = {
    "NVDA": ["AMD", "TSM", "MSFT", "META", "GOOGL"],
    "TSLA": ["NIO", "RIVN", "F", "GM"],
    "AAPL": ["TSM", "QCOM", "AVGO", "MSFT"],
    "AMZN": ["MSFT", "GOOGL", "SHOP"],
    "UNH":  ["HUM", "CI", "ELV", "CVS"],
    "GOOGL": ["META", "MSFT", "AMZN"],
    "META": ["GOOGL", "SNAP", "PINS"],
    "MSFT": ["GOOGL", "AMZN", "CRM"],
    "AMD":  ["NVDA", "INTC", "TSM"],
    "JPM":  ["GS", "MS", "BAC", "C"],
}
```

Post-thesis: replace with a dynamically constructed graph from supply-chain data or sector ETF holdings.

### Impact on golden set coverage

Of the 50 golden events in v1_2.jsonl:
- ~20 events are company-specific (earnings, guidance) — Layer 1 is sufficient.
- ~15 events are cross-entity (tariff shocks, sector rotations) — Layer 2 required.
- ~15 events are macro-driven (Fed policy, broad market rallies) — Layer 3 required.

Without tiered retrieval, ~60% of the golden set cannot be correctly attributed because the relevant evidence does not exist under the target ticker.

---

## 3. Error Classification (Spec §4.3, Insufficient Evidence Gate)

### Current spec assumption

The spec describes a single fallback path: if the Critic filters all evidence below threshold, route to `insufficient_handler`. This conflates two distinct failure modes.

### What must change

Introduce an explicit error taxonomy in `AttributionState`:

```
error_log: list[dict]
# Each entry: {
#     node: str,
#     error_type: "api_failure" | "parse_failure" | "timeout" | "rate_limit" | "insufficient_evidence",
#     message: str,
#     recoverable: bool,
#     timestamp: str,
# }
```

And split the routing logic:

```
After Critic:
    if error_log has api_failure or parse_failure:
        → route to "error_handler" (new node)
        → output: "System error: attribution could not be completed. Details: ..."
        → grounding_rate: None
        → causes: [{text: "System error", category: "error", ...}]

    elif graded_evidence is empty:
        → route to "insufficient_handler" (existing node)
        → output: "Insufficient evidence..." (existing message)

    else:
        → route to "judge"
```

The user/eval harness can distinguish:
- **System failure** (Critic API down) — retry later or escalate.
- **Data gap** (no relevant evidence exists) — expected for some events, informative for coverage analysis.
- **Low-quality evidence** (evidence exists but all below threshold) — suggests retrieval or data quality issues.

---

## 4. Connector Architecture (Spec §3.1, Provider Strategy)

### Current baseline

The current baseline does **not** use a `SourceConnector` protocol or abstract base class. Connectors are simple factory functions that return `async def fetch(ticker, endpoint, date) -> FetchResult` closures. Shared transport output is standardized via `FetchResult`.

Retry wiring is partially landed:

- `catalyst_data.retry.with_retry(...)` is already wrapped around Polygon and FMP fetchers.
- Both Polygon and FMP propagate structured `retry_after_seconds` from HTTP `Retry-After`.
- FRED still uses the simpler direct fetch path and is not yet wrapped by `with_retry(...)`.

This means the repo is past the "retry exists only on paper" stage, but it is not yet fully unified across all connectors.

### Full-version delta

If the full version grows the connector set again, it may be worth introducing a shared connector abstraction. That should be framed as a cleanup/consistency improvement, not as resolving an undecided baseline architecture.

Possible future scope:

- HTTP client lifecycle (injected `httpx.AsyncClient`)
- Rate limiter integration (`TokenBucketLimiter.acquire()` context manager)
- Retry with exponential backoff for every provider, including consistent 429 / 5xx / timeout handling
- Latency measurement
- Structured error classification (feeds into §3 error taxonomy)

Concrete baseline note: the immediate delta is **not** "remove `SourceConnector`" because that protocol is already absent from `base.py`. The real remaining delta is finishing retry/error-handling unification without breaking the working closure-based API.

One reasonable target policy remains:

```
Retry policy per error class:
    429 → exponential backoff, respect Retry-After header
    5xx → exponential backoff, max 3 attempts
    Timeout → immediate retry once, then exponential, max 3 attempts
    4xx (non-429) → no retry, return error
```

---

## 5. Eval Schema Fix (Spec §5.2, Schema)

### Current spec assumption

`AttributionResult.retrieved_chunks: list[str]` — the spec does not specify whether these are content strings or IDs.

### What must change

The `GroundingRate` metric compares `cause.evidence_ids` (which are `asset_id` hashes) against `retrieved_chunks`. For this comparison to work, `retrieved_chunks` must contain comparable identifiers, not raw content strings.

**Change `AttributionResult`:**

```python
class AttributionResult(BaseModel):
    # ... existing fields ...
    retrieved_chunks: list[str]          # CHANGED: list of asset_id strings
    retrieved_contents: list[str]        # NEW: list of content_md strings (for faithfulness checks)
```

Or, more cleanly:

```python
class RetrievedChunk(BaseModel):
    asset_id: str
    content_md: str
    source_type: str
    rrf_score: float

class AttributionResult(BaseModel):
    # ... existing fields ...
    retrieved_evidence: list[RetrievedChunk]  # replaces retrieved_chunks
```

The second approach is better because it preserves both the ID (for grounding checks) and the content (for faithfulness checks) in a single typed structure.

**GroundingRate metric update:**

```python
def compute(self, predicted, golden):
    available_ids = {chunk.asset_id for chunk in predicted.retrieved_evidence}
    grounded = sum(
        1 for cause in predicted.causes
        if any(eid in available_ids for eid in cause.evidence_ids)
    )
    return grounded / len(predicted.causes) if predicted.causes else 0.0
```

### Impact

Every callsite that constructs `AttributionResult` must be updated: `e2e_strict.py`, the eval adapter, and any future API response builder.

---

## 6. Attribution F1 Calibration (Spec §5.3, Metrics)

### Current spec assumption

The spec states: "Embed predicted + golden causes with bge-m3, cosine match (>0.8 = match)."

### What actually happened

The implementation uses **Jaccard word overlap** with threshold **0.2**, not embedding cosine with threshold 0.8.

### What must change

Two sub-issues:

**6a. Matching method.** The spec's embedding-based cosine similarity is the correct approach. Jaccard on whitespace-tokenized text has no semantic understanding. "Revenue missed expectations" and "Earnings fell short of estimates" have near-zero Jaccard overlap but are semantically identical.

Options (from most to least rigorous):
1. Embed with bge-m3 (already available in the stack), cosine threshold 0.8 — spec-compliant.
2. Embed with all-MiniLM-L6-v2 (lighter), cosine threshold to be calibrated — practical for CI.
3. Keep Jaccard but raise threshold to calibrated value — least effort but weakest.

**6b. Threshold calibration.** Regardless of matching method, the threshold must be empirically calibrated:

1. For all 50 golden events × their causes, compute a pairwise similarity matrix (predicted vs. golden).
2. Sweep thresholds from 0.1 to 0.95 in steps of 0.05.
3. At each threshold, compute precision, recall, F1 under the greedy assignment.
4. Select the threshold that maximizes F1 on this calibration set.
5. Document the threshold, the curve, and the reasoning in an ADR.

If the calibration shows the metric is degenerate (F1 is flat across thresholds), the matching method itself needs to change.

---

## 7. Dedup Consolidation (Spec §3.5, Deduplication)

### Current spec assumption

The spec defines three dedup layers: PK dedup, cross-source title dedup, and semantic dedup. The cross-source dedup function is specified once.

### What actually happened

The same fingerprint function is implemented twice:
- `catalyst_data/dedup/hard.py:compute_dedup_fingerprint`
- `catalyst_data/pipeline/clean.py:_compute_dedup_fingerprint`

The `dedup/hard.py` module is not imported by any other module.

### What must change

- Delete `_compute_dedup_fingerprint` from `clean.py`.
- Import `compute_dedup_fingerprint` and `deduplicate_articles` from `dedup.hard` into `clean.py`.
- Verify `dedup/hard.py` is the single source of truth for title-based dedup logic.
- `dedup/semantic.py` (specified in the design but not yet implemented) remains a future task — document this gap.

---

## 8. Baseline Experiment Infrastructure (Spec §5.5, Experiments)

### Current spec assumption

The spec defines experiments E1–E7 with comparison infrastructure via `compare(configs, golden_set, metrics) -> ComparisonReport`.

### What actually happened

- `experiment.py` exists but only as a stub.
- No experiment has been run against the full golden set.
- The `direct_llm` baseline (give the LLM the query without retrieval) does not exist.
- The e2e_strict script runs a single hardcoded case.

### What must change

**8a. Direct-LLM baseline adapter.**

A new predict function that calls the LLM with only the query, no retrieval context:

```
Prompt: "You are a financial analyst. Explain why {ticker} moved {price_move_pct}% on {trade_date}.
         Return JSON: {causes: [{text, category, confidence, evidence_ids: [], direction}], summary_md}"
```

This produces an `AttributionResult` with `retrieved_evidence: []` and `evidence_ids: []` for every cause. Grounding rate will be 0.0 by construction (correct — no retrieval means no grounding). F1 and category accuracy can still be computed.

**8b. Experiment runner script.**

`scripts/run_experiments.py` that:

1. Loads golden_set/v1_2.jsonl (all 50 events).
2. For each config (direct_llm, rag_only, full_mcj), calls `evaluate()` from the harness.
3. Outputs a `ComparisonReport` as both Markdown table and JSON.
4. Writes results to `data/eval_reports/experiment_comparison.md`.

**8c. Statistical rigor.**

With 50 events, report:
- Mean ± standard deviation for each metric.
- Paired t-test or Wilcoxon signed-rank between configs (p-value for "is MCJ significantly better than direct_llm?").
- Per-category breakdown (is the system better at earnings attribution than macro?).

---

## 9. Observability (Spec §8.1, LangSmith)

### Current spec assumption

"Set env vars — all LangGraph node executions auto-trace."

### What actually happened

No LangSmith integration. No structured logging. No trace persistence.

### What must change

**Minimum viable observability (pre-thesis):**

1. Generate a `trace_id` (UUID) at the start of each `graph.invoke()`.
2. Each node logs structured entries to `AttributionState.trace_log: list[dict]`:
   ```
   {trace_id, node, started_at, ended_at, input_tokens, output_tokens, cost_usd, error: str|None}
   ```
3. After graph completion, persist `trace_log` to a new `traces` table in SQLite.
4. A query utility: "Show me all traces for NVDA on 2025-01-27, sorted by cost."

**Full observability (thesis):**

5. Enable LangSmith auto-tracing via env vars (the spec is correct that this is near-zero code).
6. Extract LangSmith trace data for the observability analysis described in Spec §5.6.

---

## 10. SequentialRunner Elimination (Spec §4.4, Graph Construction)

### Current baseline

The spec only describes `StateGraph` construction. No fallback runner is mentioned.

### What actually happened

This delta has already been resolved in the current midterm baseline. `graph.py` now requires `langgraph` and raises an explicit `ImportError` if the dependency is missing. There is no `_SequentialRunner` fallback in the current branch.

### Full-version delta

No further architectural delta is required here unless the project later decides to re-introduce a non-LangGraph execution mode on purpose. As of the current baseline, this should be treated as closed, not pending.

---

## 11. data-core Package Independence (Spec §1.2, Three Publishable Packages)

### Current spec assumption

`catalyst-data` is a standalone PyPI package. "Anyone building financial AI can use this."

### What must change

**11a. Dependency tiers.**

```toml
[project]
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.0",
    # NO: lancedb, FlagEmbedding, sentence-transformers, torch
]

[project.optional-dependencies]
vector = ["lancedb>=0.15", "FlagEmbedding>=1.2"]
dev = ["pytest", "pytest-asyncio", "respx"]
```

The core package (connectors + pipeline + SQLite storage) installs with zero ML dependencies. The Gold layer (LanceDB + embeddings) is opt-in via `pip install catalyst-data[vector]`.

**11b. Package metadata.**

- Add `__version__` to `catalyst_data/__init__.py`.
- Add a standalone `README.md` inside `packages/data-core/`.
- Add a CLI entry point: `catalyst-data ingest --ticker NVDA --date 2025-01-27 --sources polygon_news,fmp_fundamentals`.

**11c. Independence test.**

Acceptance criterion: in a fresh virtualenv, `pip install ./packages/data-core && python -c "from catalyst_data.storage.sqlite import init_db; print('OK')"` succeeds without touching any ML library.

---

## Summary: Design Delta Checklist

| # | Spec Section | Delta | Priority |
|---|---|---|---|
| 1 | §3 Orchestrator | Baseline already concurrent; only future storage-model cleanup remains | P0 |
| 2 | §4.3 Miner + §6.4 RAG | Single-pass → tiered retrieval (3 layers) | P1 |
| 3 | §4.3 Insufficient Gate | Single fallback → error classification (system vs. data) | P1 |
| 4 | §3.1 Connectors | Finish retry/error-handling unification across connectors | P1 |
| 5 | §5.2 Schema | Fix `retrieved_chunks` type for GroundingRate | P0 |
| 6 | §5.3 Metrics | Jaccard 0.2 → embedding cosine with calibrated threshold | P0 |
| 7 | §3.5 Dedup | Remove duplicate implementation, wire `dedup/hard.py` | P0 |
| 8 | §5.5 Experiments | Add direct-LLM baseline, run full golden set | P1 |
| 9 | §8.1 LangSmith | Add trace persistence + optional LangSmith | P2 |
| 10 | §4.4 Graph | Closed in current baseline; no active delta | P2 |
| 11 | §1.2 Packages | Tier dependencies, add CLI, add independence test | P2 |

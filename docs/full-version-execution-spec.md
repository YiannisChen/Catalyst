# Catalyst Full-Version Execution Spec (v2)

**Status:** Proposed (ready for implementation)
**Date:** 2026-04-21
**Owner:** Catalyst core
**Supersedes:** `docs/full-version-design-delta.md` as day-to-day execution baseline

## 1. Positioning

Catalyst full version is an **evidence-bounded market-move explanation system**, not a causal proof engine.

Core commitment:
- Every output claim must map to retrieved evidence in the current run.
- The system can return `SUFFICIENT`, `PARTIAL`, or `INSUFFICIENT` instead of forcing a complete story.
- The full decision path is replayable.

Non-commitment:
- Catalyst does not claim to prove true economic causality.
- Catalyst does not claim guaranteed attribution accuracy on all events.

## 2. Product Goal And Success Criteria

Primary goal:
- Improve reliability and auditability versus direct LLM answers (`Claude/Gemini`) on the same cases.

Required success criteria (v1):
- Evidence validity >= 0.95 (all `evidence_ids` exist in run evidence set).
- Meaningful refusal behavior: non-zero refusal precision and recall on "should refuse" subset.
- Baseline comparison report exists and is reproducible (`direct_llm`, `rag_only`, `mcj_full`).
- 100% run-level trace persistence for agent executions.

Secondary criteria:
- Stable cost/latency distributions with explicit budgets.
- No silent fallback from system failure to "insufficient evidence".

## 3. Scope

In scope (v1):
- NLP question input support.
- 3-layer retrieval policy with adaptive expansion.
- Policy-driven MCJ graph with explicit output states.
- Harness failure taxonomy with fixed remediation actions.
- Local trace persistence + optional LangSmith sync.
- Head-to-head eval against direct LLM baseline.

Out of scope (v1):
- Full product API/frontend.
- Dynamic knowledge graph construction.
- True causal inference model.
- Multimodal ingestion.

## 4. Architecture (v1)

Execution graph:

`Parser -> RetrievalPolicy -> Miner -> Critic -> DecisionRouter -> (Judge | ExpandRetrieval | Partial/Refuse | SystemError) -> Validator -> Finalizer`

Design rule:
- LLM handles semantic judgment only where needed.
- Control flow, validation, budget guards, and final status assignment are deterministic.

## 5. Retrieval Design (Adaptive 3-Layer)

### Layer 1: Direct Evidence
- Filter by target ticker + date window.
- Sources: company news, filings, fundamentals-related reports.
- Cheapest path. Stop here when sufficient.

### Layer 2: Macro/Market Evidence
- Trigger when Layer 1 is insufficient.
- Remove ticker filter.
- Query by date window + macro/geopolitical/market source types.
- Captures common cross-asset drivers (rates, tariff policy, CPI, broad selloff).

### Layer 3: Related-Entity Evidence
- Trigger when Layer 2 still insufficient.
- Query peers/suppliers/sector ETF entities via static entity map.
- Keep static map for v1; dynamic graph is post-v1.

### Expansion policy
- Not every query runs all layers.
- Expansion is bounded (`max_layers=3`, `max_expansions=2`).
- Stop on any of:
- `sufficiency` reached
- budget exceeded
- layer exhaustion

## 6. Agent Decision Contract

Critic output must include:
- `sufficiency`: `sufficient | partial | insufficient`
- `magnitude_coverage`: float in `[0,1]`
- `next_action`: `proceed | expand_macro | expand_related | refuse`
- `reasoning`: concise audit text

DecisionRouter rules (deterministic):
- `system_error` flag -> `SYSTEM_ERROR`
- `next_action=proceed` and validator pass -> `SUFFICIENT` or `PARTIAL`
- `next_action=expand_*` and budget available -> next retrieval layer
- `next_action=refuse` or layers exhausted -> `INSUFFICIENT`
- budget overflow at any step -> `PARTIAL` with budget flag

## 7. Output States

Final state enum:
- `SUFFICIENT`: evidence-bounded explanation considered complete enough.
- `PARTIAL`: only part of move is explainable with current evidence.
- `INSUFFICIENT`: no enough public evidence after policy expansion.
- `SYSTEM_ERROR`: execution failure (model/provider/infra), not data-gap.

Every response includes:
- `status`
- `summary_md`
- `causes[]` with `evidence_ids`
- `retrieval_metadata` (layers used, expansion count)
- `trace_id`
- `cost/latency summary`

## 8. Validator Layer (Mandatory)

Validator runs after Judge, before final output:
- Evidence ID check: each cited `evidence_id` exists in retrieved evidence set.
- Time-window check: evidence date within allowed window.
- Schema check: strict output schema.
- Magnitude sanity check: if coverage too low for a "complete" claim, downgrade to `PARTIAL`.

On validator failure:
- First attempt: regenerate once with strict correction instruction.
- Second failure: downgrade to `PARTIAL` or `SYSTEM_ERROR` per failure type.

## 9. Harness Engineering

Harness definition:
- Failure classification + fixed response policy + regression test backfill.

Failure taxonomy (v1):
- `retrieval_failure`: no/weak evidence.
- `model_failure`: timeout, parse failure, invalid JSON.
- `consistency_failure`: invalid citations/schema mismatch.
- `budget_failure`: token/cost/latency limit exceeded.
- `provider_failure`: repeated upstream errors/429/5xx.

Required mapping:
- Each failure type must map to one deterministic action.
- Each failure type must have at least one regression test case.
- No "manual rerun as default strategy" in canonical flow.

## 10. Observability And Trace

Tracing model:
- Single run-level `trace_id`.
- Node-level events persisted locally for every execution.

Minimum trace fields:
- `trace_id`
- `run_id`
- `node`
- `started_at`, `ended_at`, `latency_ms`
- `model_id`
- `input_tokens`, `output_tokens`
- `cost_usd`
- `decision` (if applicable)
- `error_type`, `error_message`
- `status_before`, `status_after`

Storage:
- Persist to local SQLite trace table.
- Query utility required (`by ticker/date`, `by error_type`, `by high cost`).

LangSmith:
- Optional in v1 but integrated via same `trace_id`.
- Treat LangSmith as visualization/debug layer, not source of truth.

## 11. Cost Governance

Required controls:
- Per-run budget caps on `cost_usd`, `tokens`, `latency_ms`.
- Critic/Judge model split (cheap model for grading, stronger model for synthesis).
- Budget circuit breaker: stop expansions when over budget; return `PARTIAL`.

Reportability:
- Eval output must include quality and cost/latency together.
- No quality-only improvement claims.

## 12. Evaluation Redesign

Baseline set (fixed):
- `direct_llm`: direct prompt to external LLM.
- `rag_only`: retrieval + single synthesis (no critic/policy loop).
- `mcj_full`: this full-version design.

Core metrics:
- Existing: `attribution_f1`, `category_accuracy`, `grounding_rate`, `temporal_precision`, `confidence_calibration`.
- Added: `evidence_validity`, `refusal_precision`, `refusal_recall`, `avg_latency_ms`, `avg_cost_usd`.

Required experiment output:
- Same golden set across all configs.
- Markdown and JSON comparison reports.
- Versioned run metadata for reproducibility.

## 13. Implementation Order (Recommended)

Phase 1 (foundation):
- Add output status enum and validator gate.
- Add trace persistence schema and writer.
- Add direct LLM baseline pipeline.

Phase 2 (agentic core):
- Implement 3-layer adaptive retrieval policy.
- Add Critic decision contract (`next_action`, `sufficiency`, `magnitude_coverage`).
- Add DecisionRouter deterministic policy.

Phase 3 (hardening):
- Implement harness taxonomy mapping + regression suite.
- Add budget circuit breaker and model routing.

Phase 4 (evaluation):
- Run full baseline comparison on frozen golden set.
- Publish quality + cost + latency report.

## 14. Documentation Strategy (to prevent design loss)

Use a 4-document stack, each with one job:

1. `Execution Spec` (this file)
- Stable source of truth for implementers.
- Updated only when architecture/policy changes.

2. `ADR` (one decision per file, short)
- Why a specific choice was made and alternatives rejected.
- Example: retrieval layer order, validator downgrade policy, budget thresholds.

3. `Eval Report` (versioned artifacts)
- What actually happened in experiments.
- No design text, only results and interpretation.

4. `Whitepaper` (optional external narrative)
- For presentation/portfolio only.
- Must never be treated as implementation source of truth.

Recommendation:
- **Do not use PRD as the main artifact** for this stage.
- This repo is already engineering-led; use execution spec + ADRs as primary control docs.
- Data ingestion/backfill/daily operations are governed by:
- `docs/data-ingestion-operating-spec.md`

## 15. Immediate ADR Backlog

Create next ADRs before major coding:
- ADR-004: Adaptive 3-layer retrieval ordering (`direct -> macro -> related`).
- ADR-005: Output status contract and downgrade policy.
- ADR-006: Trace persistence schema and LangSmith alignment.
- ADR-007: Budget circuit breaker thresholds and model routing policy.

> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# Catalyst Value Strategy Design (Post-Midterm -> Full Version)

**Date:** 2026-04-26  
**Status:** Proposed  
**Owner:** Catalyst core

## 1. Problem Framing

Catalyst has crossed MVP viability at package level (`data-core`, `agents`, `eval`), but still faces a core existential question:

> Why should users use Catalyst for attribution instead of asking Claude directly?

As of 2026-04-26, this is a valid challenge. Direct LLM attribution quality has improved, citations are often real links, and user friction is lower.

This document defines:
- a brutally honest current evaluation,
- strategic options,
- the recommended value wedge,
- a 30/60/90-day execution plan with kill criteria.

## 2. Brutal Evaluation (Where We Stand Today)

### 2.1 What already works (non-toy assets)

- Data ingestion + cleaning + storage pipeline is operational with audit artifacts.
- MCJ graph is runnable and has role separation (`Miner -> Critic -> Judge`).
- Eval harness exists and supports apples-to-apples config comparison.
- Provider audit has real operational signal (not synthetic assumptions):
  - `polygon_news`: stable and text quality fit for RAG.
  - `polygon_ohlcv`: mostly stable, needs compensation rerun path.
  - `fred_macro`: generally stable.
  - `fmp_fundamentals`: currently constrained by 429 risk for high-frequency primary path.

### 2.2 Where we currently lose to direct Claude

- Time-to-answer: direct chat is faster and has less setup.
- UX friction: users do not need local pipeline/data ops.
- Perceived quality: for many easy events, direct LLM answers are "good enough."
- Product surface: Catalyst remains engineering-first, not end-user-first.

### 2.3 The hard truth

If Catalyst is framed as "a better answer bot than Claude," it will likely lose on convenience and may not win enough on quality to justify complexity.

## 3. Strategic Options

### Option A: Compete as a better attribution chatbot

- Positioning: "Higher quality explanations than direct LLM."
- Pros: easy to understand narrative.
- Cons:
  - direct head-on competition with frontier models' strongest UX.
  - small model/provider improvements can erase differentiation quickly.
  - high risk of becoming a feature, not a product.

Verdict: **Not recommended** as primary strategy.

### Option B: Position as an evidence-governed attribution system (recommended)

- Positioning: "Catalyst is attribution infrastructure with reproducibility, policy control, and auditability."
- Core promise:
  - bounded evidence set,
  - deterministic control flow and output states,
  - replayable traces,
  - measurable baseline deltas over time.
- Pros:
  - avoids competing on pure chat convenience.
  - maps to real institutional pain (compliance, research reproducibility, model governance).
  - creates compounding assets (golden set, traces, failure taxonomy, regression harness).
- Cons:
  - requires discipline in instrumentation and evaluation publication.
  - value is strongest for teams/workflows, weaker for casual one-off queries.

Verdict: **Recommended.**

### Option C: Pivot to data infra only

- Positioning: "Best open financial ingestion + cleaning package."
- Pros: clearer engineering moat, less agent uncertainty.
- Cons: abandons core attribution thesis and weakens project uniqueness.

Verdict: feasible fallback if attribution layer fails to demonstrate measurable benefit.

## 4. Recommended Value Wedge

## 4.1 New one-line positioning

Catalyst is **not** a causal proof engine and **not** a chat replacement.  
Catalyst is an **evidence-constrained, replayable attribution system** for teams that need defensible outputs.

## 4.2 "Why not Claude?" decision boundary

Use direct Claude when:
- you need a fast exploratory answer for one event,
- auditability/reproducibility is not required.

Use Catalyst when:
- outputs must be traceable to a fixed evidence set,
- refusal/partial-state behavior must be explicit and measured,
- runs must be replayable and comparable across model versions,
- quality/cost/latency must be reported as a system SLO, not anecdotal.

This boundary makes the two approaches complementary, not contradictory.

## 5. Product-Value Pillars To Build (Must-Haves)

1. **Evidence Integrity**
- Every claim references valid `evidence_id`.
- Validator enforces schema/time-window/magnitude sanity.
- Explicit status enum: `SUFFICIENT | PARTIAL | INSUFFICIENT | SYSTEM_ERROR`.

2. **Deterministic Decision Policy**
- Retrieval expansion policy (`direct -> macro -> related`) is explicit and bounded.
- Critic outputs `next_action` + `sufficiency`, not only scores.
- DecisionRouter is deterministic and testable.

3. **Reproducible Evaluation**
- Fixed baseline suite: `direct_llm`, `rag_only`, `mcj_full`.
- Same golden set, same report schema, versioned metadata.
- Required report: quality + cost + latency together.

4. **Operational Governance**
- Failure taxonomy with deterministic remediation actions.
- Budget circuit breakers and model routing policy.
- Trace persistence as source of truth (LangSmith optional view layer).

## 6. 30/60/90-Day Execution Plan

## Day 0-30: Prove defensibility (not feature breadth)

Targets:
- Implement status enum + Validator + DecisionRouter.
- Implement baseline runner (`direct_llm` / `rag_only` / `mcj_full`).
- Persist full run traces in local DB with query tool.

Acceptance metrics:
- `evidence_validity >= 0.95`
- Trace persistence coverage = 100% of runs
- Baseline comparison report generated from same golden set

Kill criteria:
- If `mcj_full` does not show measurable gain in evidence validity/refusal quality over `direct_llm`, freeze new feature work and reassess architecture.

## Day 31-60: Prove operational reliability

Targets:
- Data backfill checkpoint + idempotent rerun.
- Daily sync + compensation task.
- Failure taxonomy regression tests.

Acceptance metrics:
- Daily ingestion success >= 99% (core universe)
- Checkpoint rerun correctness (no duplicate dirty writes)
- Each failure class mapped to deterministic action + test case

Kill criteria:
- If ingestion reliability remains unstable, pause attribution model tuning and prioritize data SRE hardening.

## Day 61-90: Prove institutional usefulness

Targets:
- Publish weekly benchmark dashboard (quality/cost/latency trends).
- Add "explanation packet" output (summary + evidence map + trace_id + status).
- Run repeated-evaluation stability test across model updates.

Acceptance metrics:
- Week-over-week metric drift is explainable and traceable.
- `PARTIAL/INSUFFICIENT` precision improves without collapsing recall.
- Cost ceilings are enforced without silent degradation.

Kill criteria:
- If outputs remain non-reproducible across reruns, Catalyst should be repositioned as research tooling rather than decision-support system.

## 7. Metrics That Actually Differentiate Catalyst

Do not anchor differentiation on generic narrative quality alone.  
Anchor on metrics direct chat cannot reliably provide as a system:

- `evidence_validity`: cited evidence IDs exist in run evidence set.
- `refusal_precision`, `refusal_recall`: model refuses when evidence is truly insufficient.
- `trace_completeness`: percentage of runs with full node-level trace.
- `replay_consistency`: rerun consistency under same config window.
- `cost_per_successful_sufficient_report`: real efficiency signal for production use.

## 8. Concrete Answer Template For Committee / Stakeholders

Question: "Why not just use Claude attribution?"

Suggested answer:

1. We agree direct Claude is strong for ad-hoc exploration.  
2. Our target problem is different: auditable and reproducible attribution under explicit policy constraints.  
3. Catalyst enforces deterministic status outputs, evidence validation, and replayable traces.  
4. We evaluate against direct LLM baseline on the same golden set and publish quality/cost/latency together.  
5. If Catalyst cannot beat baseline on governance-critical metrics, we will narrow scope instead of over-claiming.

This answer is defensible because it is measurable and falsifiable.

## 9. Immediate Backlog (Priority Order)

P0:
- Implement `Validator` node and state enum contract.
- Add `DecisionRouter` deterministic policy logic.
- Ship `direct_llm` baseline adapter and comparison report script.

P1:
- Add trace DB schema + query CLI.
- Add ingestion checkpoint tables and rerun command.
- Add refusal and evidence-validity metrics to eval package.

P2:
- Add budget circuit breaker + model routing policy.
- Publish weekly benchmark summary in `data/eval_reports/`.

## 10. Non-Goals (To Avoid Scope Drift)

- Do not claim true economic causality.
- Do not chase full multimodal support in v1.
- Do not build broad frontend polish before baseline evidence is complete.
- Do not claim "production-ready" without failure taxonomy + replay + baseline reports.

## 11. Final Recommendation

Catalyst should not try to win by being a more fluent chatbot.  
Catalyst should win by being a **measurable, governable attribution system** that organizations can trust, audit, and improve over time.

If this value wedge is executed with strict baseline reporting and operational discipline, Catalyst has clear value even in a world where direct Claude answers are strong.

# B5 — Attribution Workflow and Runtime Assurance Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement the deterministic Context Builder, strengthen the Miner → Critic → Router → Judge → Validator → Finalizer workflow with typed outputs, hypothesis prerequisites, counter-evidence, explicit abstention, lexicographic ranking, and one RunAssuranceRecord per run owned by `catalyst_agents/runtime/assurance/`.

**Architecture:** Context Builder is deterministic and injected via ContextProvider protocol. Existing nodes (critic, judge, validator, finalizer, decision_router, miner) are strengthened with contract-specific gates, typed output schemas, and ranking rubric. Runtime assurance module lives in `catalyst_agents/runtime/assurance/`. No multi-agent personas, no Falsifier node, no confidence scores.

**Tech Stack:** Python 3.12+, LangGraph, pydantic, pytest

**Binding Contract:** `docs/plans/2026-07-21-b2-b7-technical-contracts.md` §7

---

## 0. Execution Rules

- B4 must be independently verified first. Agents consumes B4 protocols and artifacts but never imports eval or opens the corpus SQLite database directly.
- B5 owns `ContextProvider` and a deterministic fixture implementation. The production SQLite-backed adapter is composed in B7 under `catalyst-app`; this resolves the previous contradictory “data-core implementation” claim.
- Do not stage, commit, push, call providers, or mutate canonical DB files.
- `ABSTAIN` replaces new `INSUFFICIENT` writes. A compatibility decoder accepts historical `INSUFFICIENT` trace/run rows and maps them to `ABSTAIN`; tests must cover both old reads and new writes.
- Ranking tests assert the complete exact order and each criterion independently. `A or B` assertions are prohibited.
- Runtime-assurance tests construct persisted run artifacts and independently compute expected failures; hand-constructing an already-passing assurance record is schema coverage only.
- Trace-version tests create a temporary trace DB, verify the registry row, upgrade behavior, and fail loudly on an unknown future version.
- Relationship gates consume one reviewed manifest at `packages/agents/catalyst_agents/attribution/manifests/relationships_core_v1.json`. Required fields are `schema_version`, `manifest_id`, and `edges[]`; each edge has `edge_id`, `from_ticker`, `to_ticker`, `relationship_type`, `effective_from`, optional `effective_to`, `review_source`, and `reviewed_at`. `manifest_id` is the SHA-256 of canonical JSON containing `schema_version` and deterministically sorted `edges`, explicitly excluding the `manifest_id` field itself. No runtime graph discovery is allowed.

### 0.1 Completion and no-deferral contract

- B5 is one execution unit. The executor must complete Tasks 0–8, including every production-wiring task in Task 7, before reporting completion. Passing schema/unit tests alone is not B5 completion.
- “Requires refactoring”, “legacy path is complex”, “production integration is large”, or “can be completed in a later amendment” are not blockers and are not valid reasons to stop. Continue with the smallest coherent refactor inside the allowlist.
- Do not report `COMPLETE` while any required file, exact public API, graph edge, runtime persistence path, compatibility decoder, landmine, or integration test in this plan is missing. A partial report must say `NOT COMPLETE`, list exact unchecked gates, and continue execution unless an external permission/safety boundary makes further work impossible.
- No required production function may contain placeholder behavior (`pass`, unconditional empty return, `NotImplementedError`, TODO-only body), and no required test may merely inspect source text when behavior can be executed.
- Every task follows RED → GREEN → focused regression. Capture the RED failure reason before implementing. A collection error caused by an undefined fixture/import is an invalid RED and must be fixed before production code is written.
- Do not weaken, delete, skip, or xfail an existing test to make B5 pass. Intentional `INSUFFICIENT` → `ABSTAIN` expectation changes must preserve a separate historical-read compatibility test.
- The only intentionally deferred production component is B7's SQLite-backed `ContextProvider`/`Retriever` composition adapter. B5 must nevertheless execute the complete production graph and runtime using injected deterministic implementations of both protocols; no node may open corpus SQLite.

### 0.2 Binding B5 runtime interfaces

These interfaces are binding; do not create parallel ad-hoc dependency shapes:

```python
@dataclass(frozen=True)
class ContextInputs:
    ticker: str
    session_date: date
    cutoff: str
    target_close: float | None
    previous_target_close: float | None
    target_volume: float | None
    expected_prior_sessions: tuple[str, ...]
    prior_volumes_by_session: Mapping[str, float | None]
    benchmark_ticker: str | None
    benchmark_return_pct: float | None
    sector_ticker: str | None
    sector_return_pct: float | None
    peer_returns_by_ticker: Mapping[str, float | None]

class ContextProvider(Protocol):
    def load_context_inputs(
        self, *, ticker: str, session_date: str, cutoff: str
    ) -> ContextInputs: ...

@dataclass(frozen=True)
class RetrievedEvidence:
    chunk_id: str
    document_id: str
    content_text: str
    available_at: str
    source_class: str
    ticker_associations: tuple[str, ...]
    dedup_cluster_id: str | None
    cluster_first_available_at: str
    representative_document_id: str
    is_novel: bool
    lexical_raw_score: float | None
    lexical_rank: int
    corpus_manifest_id: str
    index_manifest_id: str | None
    mode_requested: str
    mode_served: str
    is_degraded: bool
    fallback_reason: str | None

class Retriever(Protocol):
    def retrieve(
        self, query: str, *, ticker: str, cutoff: str,
        requested_manifest_id: str, top_k: int = 8, candidate_depth: int = 20,
    ) -> tuple[RetrievedEvidence, ...]: ...
```

`Retriever` is the agents-side injected protocol. The B7 app adapter owns the SQLite connection, calls B4 retrieval, and hydrates `content_text`; B5 fixture retrieval returns the same agents-side records. Miner consumes only this protocol. It must not import `sqlite3`, `default_db_path`, or legacy LanceDB/SQL fallback functions after B5.

`build_attribution_graph()` gains required keyword dependencies `context_provider`, `retriever`, `cutoff_policy`, and `requested_manifest_id`; the manifest identity has no fixture/default production value. Existing model/reranker arguments remain only where existing non-B5 tests require compatibility. Context Builder is the graph entry node. A missing required B5 dependency fails before graph execution with a typed dependency error; it must not silently select the old SQLite path.

Run assurance is persisted in the agents trace DB, not as an unspecified side file. Trace schema version `2.0.0` adds exactly:

```sql
CREATE TABLE IF NOT EXISTS run_assurance (
    run_id          TEXT PRIMARY KEY,
    schema_version  TEXT NOT NULL CHECK (schema_version = '1.0.0'),
    record_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id)
);
```

`RunAssuranceRecord` is a frozen, extra-forbid model with `schema_version='1.0.0'`, `run_id`, `trace_id`, `output_status`, `cutoff`, nullable corpus/index manifest IDs, sorted `model_ids`, sorted `prompt_versions`, ordered `checks`, source-support flags, `retry_count`, `repair_count`, `budget_exhausted`, `is_degraded`, and `created_at`. Each `AssuranceCheck` has `check_name`, `status: pass|fail|not_applicable`, `detail`, and `checked_at`. Required check order is cutoff, citation resolution, Judge visibility, prerequisite gates, legal path, trace completeness, identities, budget/retry/repair, degraded state, and structured-context support. Exactly one row is written for every completed, abstained, partial, or system-error graph run. Repeated persistence for the same `run_id` is idempotent only when canonical `record_json` is identical; otherwise it raises `AssuranceConflictError`.

Unknown model pricing is represented per cost entry by `cost_status='unknown'` and `cost_usd=None`. If any entry is unknown, run-level `cost_status='unknown'` and `total_cost_usd=None`; zero is never used as a substitute for unknown cost.

### 0.3 Formula, gate, and ranking definitions

All percentage returns use `(current_close / previous_close - 1) * 100`. Both closes must be finite and strictly positive; otherwise that return is unavailable with a named reason. Do not substitute zero. The additive decomposition is emitted only when target, benchmark, and sector returns are all available. If benchmark is unavailable, all three decomposition components are null. If sector is unavailable, `market_component` may be reported descriptively but `sector_excess` and `company_specific` are null and `decomposition_available=false`. Peer median uses all finite same-session peer returns supplied by `ContextInputs`; an empty valid set yields literal `"not_available"`.

`ContextBuilderArtifact` is a frozen, extra-forbid model with no wall-clock field. It contains `schema_version='1.0.0'`, `formula_revision='b5-context-v1'`, ticker/session/cutoff, target current/previous close and return, target volume, all 20 expected prior session IDs, valid prior session IDs and values in session order, volume denominator/ratio/availability/reason, benchmark and sector identities/returns, the three decomposition components and availability/reason, sorted peer-return pairs, peer median, target-vs-peers, and sorted data-quality flags. Canonical bytes are UTF-8 JSON with sorted keys and compact separators. Two builds from equal `ContextInputs` must have byte-identical canonical output; tests compare the complete bytes, not selected fields.

The fixed cause enum and tie-break order are:

```text
market, sector, earnings_guidance, product_demand, legal_regulatory,
macro, peer_propagation, supply_chain_propagation, mixed, unexplained
```

`ABSTAIN` is an output status, never a hypothesis cause. `mixed` requires at least two distinct gate-passed causes other than `mixed` and `unexplained`. `unexplained` passes only when context quality is adequate and no other explanatory cause passes. Evidence-driven causes require at least one cited supporting item with `relevance > 0.5`, `temporal_match=true`, and `available_at <= cutoff`. Exact Critic-category compatibility is: earnings_guidance→earnings; legal_regulatory→regulatory; macro→macro or geopolitical; sector→sector; product_demand→other unless a later typed event tag explicitly narrows it. Market and sector descriptive decompositions require their OHLCV context; any narrative event claim additionally requires compatible cited evidence.

Direction validation is deterministic where structured context supplies polarity: `market` uses `market_component`, `sector` uses `sector_excess`, and `peer_propagation` uses `target_vs_peers`; positive values require `positive`, negative values require `negative`, and an explicit contradictory direction emits `direction_mismatch` and fails the gate. `unknown` is allowed when a structured component is zero or unavailable. B5 narrative evidence has no typed polarity field, so earnings/product/legal/macro/supply-chain direction remains an explicitly recorded inference and is not falsely self-certified by Validator.

The model does not self-certify gates or ranking fields. Judge parses into frozen `HypothesisDraft` containing only `cause_label`, `direction`, `transmission_mechanism`, supporting/counter evidence ID tuples, missing evidence, change condition, facts, calculations, inferences, and unavailable evidence. Extra fields such as `prerequisite_gate_passed`, `source_support_flags`, relevance, or confidence in Judge JSON are rejected. Validator resolves IDs into frozen `EvidenceRef` records by joining retrieval metadata and Critic grades, then emits frozen `Hypothesis` values with computed gate result/reason, direct-support flag, cluster count, maximum relevance, source flags, counter relevance, novelty, and validation violations. Finalizer consumes only these enriched values.

Peer and supply-chain gates require all of: an effective reviewed relationship edge for the target/counterparty/session date, same-session finite peer context for that counterparty, and pre-cutoff supporting evidence whose `ticker_associations` contains the counterparty. Edge direction alone never supplies hypothesis direction. Relationship types are exactly `peer`, `supplier`, `customer`, and `competitor`; duplicate edge IDs, invalid date intervals, unknown types, future `reviewed_at`, and hash mismatch reject the whole manifest.

Direct support is deterministic: a supporting item is direct only when it passes the relevance/temporal/cutoff checks and its source class is one of `official_government`, `issuer_disclosure`, `corporate_press_release`, or `reported_news`. `analysis_opinion` and `aggregated_unknown` are never direct. Independent cluster identity is `dedup_cluster_id` when present, otherwise `chunk:<chunk_id>`. Cluster count is the number of unique supporting identities, capped at two. Maximum relevance is zero with no valid support. Maximum counter relevance is zero with no valid counter-evidence. A hypothesis is novel when at least one valid supporting item has `is_novel=true`.

Source-support flags are computed from valid supporting evidence only:

- `opinion_only_support`: non-empty support and every source is `analysis_opinion`;
- `unknown_origin_support`: any source is `aggregated_unknown`;
- `issuer_claim_only_support`: non-empty support and every source is `issuer_disclosure` or `corporate_press_release`;
- degradation count: number of true flags above.

Sort ascending by this exact key; booleans are converted to 0 for preferred/true and 1 for disfavored/false:

```python
(
    0 if prerequisite_gate_passed else 1,
    0 if direct_support_exists else 1,
    -min(independent_supporting_cluster_count, 2),
    -max_supporting_critic_relevance,
    source_support_degradation_count,
    max_counter_evidence_relevance,
    0 if is_novel else 1,
    CAUSE_ORDER[cause_label],
)
```

No input order may participate in a tie. The eight fields above are the only semantic ranking criteria. If all eight are equal, resolve the otherwise exact tie by ascending canonical hypothesis JSON (`model_dump(mode='json')`, sorted keys, compact separators); this resolver does not alter any criterion and exists only to make permutations byte-stable. Tests vary each criterion independently while holding the other seven equal, assert one complete mixed ordering, and permute an exact same-cause tie.


## 1. Objective

Deliver B5 that passes Core Exit Gates D (Agent workflow), E (Runtime assurance), and contributes to Gate G (Contributor experience). Specifically:

- Deterministic Context Builder producing market/sector/peer decomposition;
- Injected ContextProvider and Retriever protocols (fixture-backed for B5 execution; SQLite-backed app adapters land in B7);
- Strengthened typed output schemas for all nodes;
- Hypothesis prerequisite gates (deterministic);
- Competing hypothesis schema with supporting/counter/missing evidence;
- Transmission mechanism and change condition fields;
- Lexicographic deterministic final ranking (no fake confidence);
- SUFFICIENT, PARTIAL, ABSTAIN, SYSTEM_ERROR output states;
- RunAssuranceRecord per run in `catalyst_agents/runtime/assurance/`;
- Trace schema versioning for agents trace DB;
- Unknown model cost handling (cost_status="unknown", cost_usd=None);
- Agent runtime never depends on eval.

## 2. Current Verified State

### Implemented

| Component | File | Status |
|---|---|---|
| Graph assembly | `catalyst_agents/graph.py` | implemented |
| State typing | `catalyst_agents/state.py` | implemented |
| Critic node | `catalyst_agents/nodes/critic.py` | implemented — needs typed output strengthen |
| Judge node | `catalyst_agents/nodes/judge.py` | implemented — needs hypothesis schema |
| Validator node | `catalyst_agents/nodes/validator.py` | implemented — needs gate checks |
| Finalizer node | `catalyst_agents/nodes/finalizer.py` | implemented — needs lexicographic ranking |
| DecisionRouter | `catalyst_agents/nodes/decision_router.py` | implemented |
| Miner node | `catalyst_agents/nodes/miner.py` | implemented — needs cutoff-safe retrieval |
| Trace writer | `catalyst_agents/trace/writer.py` | implemented |
| Trace schema | `catalyst_agents/trace/schema.py` | implemented |
| Trace artifacts | `catalyst_agents/trace/artifacts.py` | implemented |
| Runtime runner | `catalyst_agents/runtime/runner.py` | implemented |
| Runtime service | `catalyst_agents/runtime/service.py` | implemented |
| Runtime status | `catalyst_agents/runtime/status.py` | implemented |
| Runtime validation | `catalyst_agents/runtime/validation.py` | implemented |
| Cost tracker | `catalyst_agents/cost_tracker.py` | implemented |
| Backoff | `catalyst_agents/backoff.py` | implemented |
| Retrieval policy | `catalyst_agents/retrieval/policy.py` | implemented |
| Dependencies | `catalyst_agents/runtime/dependencies.py` | implemented |

### Partial

| Component | Gap |
|---|---|
| Context Builder | does not exist as separate deterministic step; context is ad-hoc |
| Miner | not using B4 cutoff-safe FTS5 retrieval |
| Judge output | lacks structured hypothesis schema with supporting/counter/missing evidence |
| Validator | lacks prerequisite gate checks, counter-evidence validation |
| Finalizer | may use weighting instead of lexicographic ranking |
| RunAssuranceRecord | not produced per run |
| Trace schema versioning | trace DB may not have explicit version registry |
| Agent-eval isolation | `adapter.py` moved to eval already; verify no new imports |

### Missing

| Component | File to create |
|---|---|
| Context Builder | `catalyst_agents/attribution/context_builder.py` (new) |
| ContextProvider protocol | `catalyst_agents/attribution/provider.py` (new) |
| Hypothesis schema | `catalyst_agents/attribution/hypothesis.py` (new) |
| Hypothesis gates | `catalyst_agents/attribution/gates.py` (new) |
| Lexicographic ranking rubric | `catalyst_agents/attribution/ranking.py` (new) |
| Output status types | `catalyst_agents/attribution/output_status.py` (new) |
| RunAssuranceRecord | `catalyst_agents/runtime/assurance/__init__.py` (new) |
| Assurance record schema | `catalyst_agents/runtime/assurance/record.py` (new) |
| Assurance checks | `catalyst_agents/runtime/assurance/checks.py` (new) |
| Trace schema version registry | `catalyst_agents/trace/version.py` (new) |

## 3. Scope / Non-goals

### Scope

- Deterministic Context Builder (target return, volume ratio, market/sector/peer decomposition)
- ContextProvider protocol + deterministic fixture implementation; production adapter is wired by B7 composition
- Typed hypothesis schema with gates, evidence, transmission mechanism, change condition
- Critic → evidence grading (unchanged: one LLM call)
- Judge → competing hypotheses with structured output
- Validator → prerequisite gates, evidence IDs, cutoff compliance, one exceptional repair
- Finalizer → lexicographic ranking (8 criteria per contract §7.3)
- Output status: SUFFICIENT, PARTIAL, ABSTAIN, SYSTEM_ERROR
- RunAssuranceRecord per run (cutoff, citation resolution, visibility, gates, path, identity, budget, source-support flags)
- Agents trace DB schema versioning
- Cost tracking with `cost_status="unknown"`, `cost_usd=None`
- Agents never imports eval (existing boundary; verify)

### Non-goals

- Context Builder opening SQLite directly (uses injected ContextProvider)
- Critic split or additional LLM calls
- Falsifier node
- Multi-agent personas
- Confidence scores or self-reported probabilities
- Bounded retrieval expansion (deferred until measured need)
- Peer/supply-chain edge manifest (versioned manifest, not dynamic discovery)
- Finnhub peer expansion (Showcase, not Core)

## 4. Dependencies

### Inputs

- B4 completion: FTS5/BM25 retrieval, RetrievalResult, cutoff computation, Eval Foundation schemas
- Existing agents codebase (nodes, graph, trace, runtime)
- Dev DB with B3 corpus_chunks
- B2 request ledger for RunAssuranceRecord identity fields

### Output Artifacts

- RunAssuranceRecord (canonical JSON persisted in the agents trace DB `run_assurance` table, one per graph run)
- Trace DB with schema version registry
- AttributionRun result type

### Consumed By

- B6 (dense/reranker): FTS5 baseline reference for RRF comparison
- B7 (eval metrics): RunAssuranceRecord consumed by Surface B checks

## 5. Schema and Artifact Ownership

### Owned Schemas (agents)

- `ContextBuilderArtifact` (deterministic output: target return, decomposition, flags)
- `Hypothesis` (typed: cause_label, transmission_mechanism, supporting_evidence, counter_evidence, missing_evidence, change_condition, prerequisite_gate_passed, source_support_flags)
- `AttributionOutput` (SUFFICIENT|PARTIAL|ABSTAIN|SYSTEM_ERROR, ranked hypotheses)
- `AssuranceCheck` and `RunAssuranceRecord` with the exact fields, check order, identity rules, and persistence semantics in §0.2

### Owned Trace Schema

- Agents trace DB version registry (not data-core `user_version`)

### Not Owned

- RetrievalResult (B4)
- CorpusManifest (B3)
- BenchmarkCase (B4 eval foundation)
- MetricRecord/ResultPack (B7)

## 6. File Allowlist

### New Files

```
packages/agents/catalyst_agents/attribution/__init__.py
packages/agents/catalyst_agents/attribution/context_builder.py
packages/agents/catalyst_agents/attribution/provider.py
packages/agents/catalyst_agents/attribution/hypothesis.py
packages/agents/catalyst_agents/attribution/gates.py
packages/agents/catalyst_agents/attribution/ranking.py
packages/agents/catalyst_agents/attribution/output_status.py
packages/agents/catalyst_agents/attribution/manifests/relationships_core_v1.json
packages/agents/catalyst_agents/runtime/assurance/__init__.py
packages/agents/catalyst_agents/runtime/assurance/record.py
packages/agents/catalyst_agents/runtime/assurance/checks.py
packages/agents/catalyst_agents/trace/version.py
packages/agents/tests/test_context_builder.py
packages/agents/tests/test_hypothesis_gates.py
packages/agents/tests/test_ranking.py
packages/agents/tests/test_output_status.py
packages/agents/tests/test_runtime_assurance.py
packages/agents/tests/test_trace_version.py
packages/agents/tests/test_finalizer.py
packages/agents/tests/attribution_fixtures.py
packages/agents/tests/test_attribution_fixtures.py
packages/agents/tests/test_b5_integration.py
packages/agents/tests/test_cost_tracker.py
```

### Allowed to Modify

```
packages/agents/catalyst_agents/state.py                     — add typed output fields
packages/agents/catalyst_agents/nodes/miner.py               — use cutoff-safe retrieval
packages/agents/catalyst_agents/retrieval/policy.py           — remove direct SQLite access; delegate through injected B4 Retriever
packages/agents/catalyst_agents/retrieval/__init__.py         — re-export the single Retriever contract/facade
packages/agents/catalyst_agents/nodes/critic.py              — strengthen output typing
packages/agents/catalyst_agents/nodes/judge.py               — structured hypothesis output
packages/agents/catalyst_agents/nodes/validator.py           — gate checks, repair
packages/agents/catalyst_agents/nodes/finalizer.py           — lexicographic ranking
packages/agents/catalyst_agents/nodes/decision_router.py     — timeline/novelty projection
packages/agents/catalyst_agents/graph.py                     — add Context Builder node
packages/agents/catalyst_agents/runtime/runner.py            — produce RunAssuranceRecord
packages/agents/catalyst_agents/runtime/service.py           — wire assurance
packages/agents/catalyst_agents/runtime/status.py            — historical INSUFFICIENT decoder and ABSTAIN writes
packages/agents/catalyst_agents/runtime/dependencies.py      — expose injected B5 protocols without node-owned SQLite
packages/agents/catalyst_agents/cost_tracker.py              — unknown model cost
packages/agents/catalyst_agents/trace/schema.py              — version registry and run_assurance table
packages/agents/catalyst_agents/trace/writer.py              — persist exactly one assurance record per run
packages/agents/catalyst_agents/trace/projection.py          — persist context/hypothesis artifacts required by assurance
packages/agents/catalyst_agents/trace/artifacts.py           — typed artifact read/write support for new B5 artifact types
packages/agents/catalyst_agents/trace/__init__.py            — trace-version public exports
packages/agents/catalyst_agents/prompts/judge.md              — exact structured Hypothesis JSON output
packages/agents/tests/test_graph.py                          — add Context Builder tests
packages/agents/tests/test_critic.py                         — strengthen output tests
packages/agents/tests/test_judge.py                          — hypothesis schema tests
packages/agents/tests/test_validator.py                      — gate check tests
packages/agents/tests/test_finalizer.py                      — ranking tests
packages/agents/tests/test_miner.py                          — cutoff-safe retrieval
packages/agents/tests/test_retrieval_policy.py               — injected Retriever compatibility
packages/agents/tests/test_live_run_service.py               — persisted assurance lifecycle
packages/agents/tests/test_runtime_status.py                 — historical status compatibility
packages/agents/tests/test_trace.py                          — v1→v2 trace migration and assurance persistence
packages/agents/tests/test_failure_taxonomy.py               — inject B5 dependencies and preserve failure classifications
packages/agents/tests/test_critic_v2.py                      — intentional typed-output compatibility assertions only
packages/agents/tests/test_state.py                          — new state/output contract
packages/agents/tests/test_runtime_dependencies.py           — injected protocol dependency shape
packages/app/catalyst_app/schemas.py                         — accept canonical ABSTAIN runtime/trace status
packages/app/catalyst_app/workspace_projection.py            — project ABSTAIN terminal state
packages/app/tests/test_failure_paths.py                      — historical INSUFFICIENT decodes to ABSTAIN at API boundary
```

If another existing `packages/agents/tests/test_*.py` fails solely because a binding B5 API now requires injected dependencies or emits `ABSTAIN`, it may be changed only to import the shared Task 0 fixture and assert the new contract. Record the file and old/new assertion in the evidence report. Do not remove behavioral coverage, replace exact assertions with membership assertions, or introduce local mocks.

### Files Explicitly Forbidden

- `packages/eval/` — agents must not import eval
- Other `packages/app/` files — B7 domain; B5 may touch only the three status-compatibility files above because the binding ABSTAIN decoder contract crosses the existing API boundary
- `packages/data-core/` — consume public B4 protocols only; no B5 modifications
- `docs/plans/2026-07-21-b2-b7-technical-contracts.md`

## 7. TDD Tasks

### Task 0: Deterministic attribution fixtures

Create `packages/agents/tests/attribution_fixtures.py` with `FixtureContextProvider`, `FixtureRetriever`, `StubModelClient`, exact OHLCV/session data, hypothesis builders exposing all eight ranking criteria, one shared recording cutoff policy, a valid relationship-manifest fixture, and persisted-run artifact builders used below. It also defines `EXPECTED_20_SESSIONS`, `VALID_12_VOLUMES`, `EXPECTED_VALID_12_SESSION_IDS`, and `volume_window_provider`; these are literal independent fixtures and must include an older out-of-window observation that would change the median if the implementation illegally expanded its lookback. Fixture helpers return immutable copies and fail on unregistered tickers/sessions.

Add `packages/agents/tests/test_attribution_fixtures.py` proving literal expected values, immutability, unknown-key failure, deterministic stub call counts, and that expected assurance/status objects are not generated by production serializers. Run these tests before Task 1. Later test files import these helpers explicitly and must not define ad-hoc mocks.

Every snippet below that uses `pytest`, `make_hypothesis`, `mock_provider`, `mock_provider_with_ohlcv`, `volume_window_provider`, `mock_run_artifacts`, `create_temp_trace_db`, or `initialize_trace_schema` must import it explicitly from `pytest` or `attribution_fixtures` as appropriate. Undefined fixture names or collection failures are not acceptable RED states.

### Task 1: Context Builder

**Step 1: Write failing Context Builder test**

```python
# packages/agents/tests/test_context_builder.py

import statistics

import pytest

def test_context_builder_additive_decomposition():
    """r_target = market_component + sector_excess + company_specific."""
    from catalyst_agents.attribution.context_builder import ContextBuilder

    builder = ContextBuilder(provider=mock_provider_with_ohlcv())
    artifact = builder.build(ticker="AAPL", session_date="2026-01-15",
                             cutoff="2026-01-15T21:00:00Z")

    r_target = artifact.target_return_pct
    assert r_target == pytest.approx(
        artifact.market_component + artifact.sector_excess + artifact.company_specific,
        abs=0.01
    )


def test_context_builder_return_formula():
    """r_target,t = (C_target,t / C_target,t-1 - 1) * 100."""
    from catalyst_agents.attribution.context_builder import ContextBuilder

    builder = ContextBuilder(provider=mock_provider(
        close_prices={"2026-01-15": 150.0, "2026-01-14": 155.0}
    ))
    artifact = builder.build(ticker="AAPL", session_date="2026-01-15",
                             cutoff="2026-01-15T21:00:00Z")
    # (150/155 - 1) * 100 ≈ -3.2258
    assert artifact.target_return_pct == pytest.approx((150.0 / 155.0 - 1) * 100, abs=0.01)


def test_volume_ratio_requires_10_sessions():
    """volume_ratio unavailable when fewer than 10 valid prior sessions."""
    from catalyst_agents.attribution.context_builder import ContextBuilder

    builder = ContextBuilder(provider=mock_provider(n_sessions=5))
    artifact = builder.build(ticker="AAPL", session_date="2026-01-15",
                             cutoff="2026-01-15T21:00:00Z")
    assert artifact.volume_context_available is False


def test_volume_ratio_with_20_sessions():
    """volume_ratio = V_target,t / median(V_target,t-20...V_target,t-1)."""
    from catalyst_agents.attribution.context_builder import ContextBuilder

    builder = ContextBuilder(provider=mock_provider(n_sessions=25))
    artifact = builder.build(ticker="AAPL", session_date="2026-01-15",
                             cutoff="2026-01-15T21:00:00Z")
    assert artifact.volume_context_available is True
    assert artifact.volume_ratio is not None


def test_volume_ratio_uses_all_valid_values_inside_fixed_20_session_window():
    """With 10–19 valid values, use those values only; never extend before t-20."""
    from catalyst_agents.attribution.context_builder import ContextBuilder

    provider = volume_window_provider(
        expected_prior_sessions=EXPECTED_20_SESSIONS,
        valid_prior_volumes=VALID_12_VOLUMES,
        older_volume=9_999_999,
        target_volume=1_200,
    )
    artifact = ContextBuilder(provider=provider).build(
        ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z"
    )
    assert artifact.volume_context_available is True
    assert artifact.volume_ratio == pytest.approx(1_200 / statistics.median(VALID_12_VOLUMES))
    assert artifact.volume_valid_sessions == EXPECTED_VALID_12_SESSION_IDS
    assert artifact.volume_denominator == statistics.median(VALID_12_VOLUMES)
    assert 9_999_999 not in artifact.volume_prior_values


@pytest.mark.parametrize("invalid_target_volume", [None, 0, -1, float("nan")])
def test_volume_ratio_rejects_missing_or_non_positive_target_volume(invalid_target_volume):
    """Invalid current-session volume produces unavailable context, not infinity or zero."""
    from catalyst_agents.attribution.context_builder import ContextBuilder

    artifact = ContextBuilder(provider=volume_window_provider(
        expected_prior_sessions=EXPECTED_20_SESSIONS,
        valid_prior_volumes=VALID_12_VOLUMES,
        target_volume=invalid_target_volume,
    )).build(ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z")
    assert artifact.volume_context_available is False
    assert artifact.volume_ratio is None
    assert artifact.volume_unavailable_reason == "invalid_target_volume"


def test_peer_median_missing_is_not_available():
    """Missing peer data is 'not_available', never zero."""
    from catalyst_agents.attribution.context_builder import ContextBuilder

    builder = ContextBuilder(provider=mock_provider(peer_data={}))
    artifact = builder.build(ticker="AAPL", session_date="2026-01-15",
                             cutoff="2026-01-15T21:00:00Z")
    assert artifact.target_vs_peers == "not_available"


def test_context_builder_emits_deterministic_artifact():
    """Same inputs → byte-identical artifact (idempotent)."""
    from catalyst_agents.attribution.context_builder import canonical_context_bytes

    provider = mock_provider_with_ohlcv()
    b1 = ContextBuilder(provider=provider).build(ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z")
    b2 = ContextBuilder(provider=provider).build(ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z")
    assert canonical_context_bytes(b1) == canonical_context_bytes(b2)
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/agents/tests/test_context_builder.py -q
```

Expected: FAIL.

**Step 3: Implement**

- `catalyst_agents/attribution/provider.py`: `ContextProvider` protocol
- `catalyst_agents/attribution/context_builder.py`: `ContextBuilder` class
- Formulas per contract §7.1, including the fixed previous-20-session window, 10-valid minimum, no lookback expansion, positive/finite volume rules, and auditable denominator/session fields

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/agents/tests/test_context_builder.py -q
```

Expected: all PASS.

### Task 2: Hypothesis schema

**Step 1: Write hypothesis schema test**

```python
# packages/agents/tests/test_hypothesis_gates.py

def test_hypothesis_has_all_fields():
    """Hypothesis carries typed fields per contract §7.2–§7.3."""
    from catalyst_agents.attribution.hypothesis import HypothesisDraft

    h = HypothesisDraft(
        cause_label="earnings_guidance",
        direction="negative",
        transmission_mechanism="Reduced forward guidance lowered revenue expectations",
        supporting_evidence_ids=("poly:1:news_v2:body:0001",),
        counter_evidence_ids=(),
        missing_evidence=["Actual EPS figure not yet released"],
        change_condition="If actual EPS exceeds consensus, reassess",
        facts=["EPS guidance was lowered from $2.00 to $1.50"],
        calculations=[],
        inferences=["Market interpreted guidance cut as demand weakness signal"],
        unavailable_evidence=["Full earnings transcript"],
    )
    assert h.cause_label == "earnings_guidance"
    assert not hasattr(h, "prerequisite_gate_passed")


def test_market_hypothesis_requires_benchmark_ohlcv():
    """Market hypothesis gate: corresponding benchmark OHLCV must exist."""
    from catalyst_agents.attribution.gates import check_prerequisite

    passed, reason = check_prerequisite(
        cause="market",
        context={"benchmark_ohlcv_exists": False},
        evidence=[],
        edges={},
    )
    assert not passed
    assert "OHLCV" in reason


def test_peer_propagation_requires_edge():
    """Peer propagation gate: versioned edge + co-movement + peer evidence."""
    from catalyst_agents.attribution.gates import check_prerequisite

    passed, reason = check_prerequisite(
        cause="peer_propagation",
        context={"peer_context_available": True},
        evidence=[{"chunk_id": "poly:peer1"}],
        edges={"AAPL": {"supplier": ["peer_corp"]}},  # no edge for this peer
    )
    assert not passed


def test_edge_alone_insufficient():
    """An edge plus co-movement is never sufficient; peer evidence required."""
    from catalyst_agents.attribution.gates import check_prerequisite

    passed, reason = check_prerequisite(
        cause="peer_propagation",
        context={"peer_context_available": True},
        evidence=[],  # no peer evidence
        edges={"AAPL": {"peer": ["peer_corp"]}},
    )
    assert not passed


def test_abstain_when_all_gates_fail():
    """ABSTAIN when all hypothesis gates fail."""
    from catalyst_agents.attribution.gates import all_gates_failed

    results = [
        ("market", False, "no OHLCV"),
        ("sector", False, "no ETF data"),
        ("earnings_guidance", False, "no evidence"),
    ]
    assert all_gates_failed(results)
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/agents/tests/test_hypothesis_gates.py -q
```

Expected: FAIL.

**Step 3: Implement**

- `catalyst_agents/attribution/hypothesis.py`: `Hypothesis` typed dict/dataclass
- `catalyst_agents/attribution/gates.py`: `check_prerequisite()`, `all_gates_failed()`
- All 10 causes: market, sector, earnings/guidance, product/demand, legal/regulatory, macro, peer_propagation, supply_chain_propagation, mixed, unexplained

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/agents/tests/test_hypothesis_gates.py -q
```

Expected: all PASS.

### Task 3: Lexicographic ranking

**Step 1: Write ranking test**

```python
# packages/agents/tests/test_ranking.py

def test_lexicographic_order():
    """Ranking is deterministic lexicographic, not weighted confidence."""
    from catalyst_agents.attribution.ranking import rank_hypotheses

    h1 = make_hypothesis(
        cause="earnings_guidance", gate_passed=True, direct_support=True,
        dedup_clusters=2, max_relevance=0.9, degradation_flags=0,
        max_counter_relevance=0.1, is_novel=True,
    )
    h2 = make_hypothesis(
        cause="market", gate_passed=True, direct_support=True,
        dedup_clusters=1, max_relevance=1.0, degradation_flags=0,
        max_counter_relevance=0.0, is_novel=True,
    )
    h3 = make_hypothesis(
        cause="product_demand", gate_passed=True, direct_support=True,
        dedup_clusters=2, max_relevance=0.8, degradation_flags=0,
        max_counter_relevance=0.1, is_novel=True,
    )

    ranked = rank_hypotheses([h1, h2, h3])
    assert [item.cause_label for item in ranked] == [
        "earnings_guidance", "product_demand", "market",
    ]


def test_gate_failed_ranks_last():
    """Gate-failed hypotheses rank below gate-passed regardless of evidence."""
    from catalyst_agents.attribution.ranking import rank_hypotheses

    h1 = make_hypothesis(cause="market", gate_passed=False, direct_support=True,
                         dedup_clusters=2, max_relevance=1.0)
    h2 = make_hypothesis(cause="unexplained", gate_passed=True, direct_support=False,
                         dedup_clusters=0, max_relevance=0.0)

    ranked = rank_hypotheses([h1, h2])
    assert [item.cause_label for item in ranked] == ["unexplained", "market"]


def test_no_confidence_score_in_output():
    """Output must not contain self-reported probability or confidence score."""
    from catalyst_agents.attribution.ranking import rank_hypotheses

    h = make_hypothesis(cause="market", gate_passed=True)
    ranked = rank_hypotheses([h])

    # Check ranked output has no confidence field
    for r in ranked:
        assert not hasattr(r, "confidence_score")
        assert not hasattr(r, "probability")


def test_stable_tie_breaker():
    """Stable hypothesis enum order breaks ties."""
    from catalyst_agents.attribution.ranking import rank_hypotheses

    h1 = make_hypothesis(cause="earnings_guidance", gate_passed=True, direct_support=True,
                         dedup_clusters=1, max_relevance=0.8, degradation_flags=0,
                         max_counter_relevance=0.1, is_novel=True)
    h2 = make_hypothesis(cause="market", gate_passed=True, direct_support=True,
                         dedup_clusters=1, max_relevance=0.8, degradation_flags=0,
                         max_counter_relevance=0.1, is_novel=True)

    ranked = rank_hypotheses([h2, h1])  # order shouldn't matter
    # Contract enum order determines the exact tie-breaker.
    assert [item.cause_label for item in ranked] == ["market", "earnings_guidance"]
    ranked2 = rank_hypotheses([h1, h2])
    assert [item.cause_label for item in ranked2] == ["market", "earnings_guidance"]
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/agents/tests/test_ranking.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_agents/attribution/ranking.py`**

8-criteria lexicographic ranking per contract §7.3:
1. prerequisite gate passed
2. direct pre-cutoff support exists
3. number of independent supporting dedup clusters (capped at 2)
4. maximum Critic relevance among supporting chunks
5. fewer source-support degradation flags
6. lower maximum counter-evidence relevance
7. novel before previously-known evidence
8. stable hypothesis enum tie-breaker

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/agents/tests/test_ranking.py -q
```

Expected: all PASS.

### Task 4: Output status types

**Step 1: Write output status test**

```python
# packages/agents/tests/test_output_status.py

def test_output_status_values():
    """Four output states: SUFFICIENT, PARTIAL, ABSTAIN, SYSTEM_ERROR."""
    from catalyst_agents.attribution.output_status import OutputStatus

    assert OutputStatus.SUFFICIENT == "SUFFICIENT"
    assert OutputStatus.PARTIAL == "PARTIAL"
    assert OutputStatus.ABSTAIN == "ABSTAIN"
    assert OutputStatus.SYSTEM_ERROR == "SYSTEM_ERROR"


def test_sufficient_requires_one_gate_passed():
    """SUFFICIENT: at least one gate-passed hypothesis, citations resolve, no cutoff violation."""
    from catalyst_agents.attribution.output_status import determine_status

    hypotheses = [make_hypothesis(gate_passed=True)]
    status = determine_status(hypotheses, cutoff_violations=0, citation_all_resolve=True,
                               coverage_degraded=False, context_quality_ok=True)
    assert status == "SUFFICIENT"


def test_partial_when_coverage_degraded():
    """Partial coverage or opinion-only support → PARTIAL."""
    from catalyst_agents.attribution.output_status import determine_status

    hypotheses = [make_hypothesis(gate_passed=True)]
    status = determine_status(hypotheses, cutoff_violations=0, citation_all_resolve=True,
                               coverage_degraded=True, context_quality_ok=True)
    assert status == "PARTIAL"


def test_abstain_when_no_gates_passed():
    """No gates passed → ABSTAIN."""
    from catalyst_agents.attribution.output_status import determine_status

    status = determine_status([], cutoff_violations=0, citation_all_resolve=True,
                               coverage_degraded=False, context_quality_ok=True)
    assert status == "ABSTAIN"


def test_system_error_on_schema_failure():
    """Schema, model, DB, or runtime failure → SYSTEM_ERROR."""
    from catalyst_agents.attribution.output_status import determine_status

    status = determine_status(None, cutoff_violations=0, citation_all_resolve=True,
                               coverage_degraded=False, context_quality_ok=True,
                               error_occurred=True)
    assert status == "SYSTEM_ERROR"
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/agents/tests/test_output_status.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_agents/attribution/output_status.py`**

Move the canonical enum to this module and re-export it from `state.py`. Update runtime normalization so historical `INSUFFICIENT` inputs map to the new abstention status, while serializers emit only `ABSTAIN`. Update affected existing tests for this intentional contract change without weakening any other assertion.

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/agents/tests/test_output_status.py -q
```

Expected: all PASS.

### Task 5: RunAssuranceRecord

**Step 1: Write assurance test**

```python
# packages/agents/tests/test_runtime_assurance.py

def test_assurance_record_produced_per_run():
    """Every attribution run produces one RunAssuranceRecord."""
    from catalyst_agents.runtime.assurance.record import RunAssuranceRecord
    from attribution_fixtures import VALID_ASSURANCE_RECORD

    record = RunAssuranceRecord.model_validate(VALID_ASSURANCE_RECORD)
    assert [check.check_name for check in record.checks] == [
        "cutoff", "citation_resolution", "judge_visibility",
        "prerequisite_gates", "legal_path", "trace_completeness",
        "identities", "budget_retry_repair", "degraded_state",
        "structured_context_support",
    ]


def test_assurance_without_eval_import():
    """Assurance module does not import catalyst_eval."""
    import catalyst_agents.runtime.assurance as a
    # Must not have eval import
    import sys
    assert "catalyst_eval" not in sys.modules


def test_assurance_deterministic():
    """Same run artifacts → same assurance record (deterministic)."""
    from catalyst_agents.runtime.assurance.checks import run_all_checks

    artifacts1 = mock_run_artifacts(cutoff="2026-01-15T21:00:00Z", citations=["c1", "c2"])
    artifacts2 = mock_run_artifacts(cutoff="2026-01-15T21:00:00Z", citations=["c1", "c2"])

    checks1 = run_all_checks("run-001", artifacts1)
    checks2 = run_all_checks("run-001", artifacts2)

    assert checks1 == checks2


def test_source_support_flags_in_assurance():
    """opinion_only_support, unknown_origin_support, issuer_claim_only_support in assurance."""
    from catalyst_agents.runtime.assurance.checks import compute_source_flags

    evidence = [
        {"source_class": "analysis_opinion"},
        {"source_class": "reported_news"},
    ]
    flags = compute_source_flags(evidence)
    assert flags["opinion_only_support"] is False
    assert flags["unknown_origin_support"] is False

    opinion_only = compute_source_flags([{"source_class": "analysis_opinion"}])
    assert opinion_only["opinion_only_support"] is True


def test_cost_unknown_mode():
    """When model cost is unknown, cost_status='unknown', cost_usd=None."""
    from catalyst_agents.cost_tracker import CostEstimate

    est = CostEstimate(model_id="unpriced-model", tokens_prompt=1000, tokens_completion=200)
    assert est.cost_status == "unknown"
    assert est.cost_usd is None
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/agents/tests/test_runtime_assurance.py -q
```

Expected: FAIL.

**Step 3: Implement**

- `catalyst_agents/runtime/assurance/record.py`: `RunAssuranceRecord`
- `catalyst_agents/runtime/assurance/checks.py`: `run_all_checks()`, `compute_source_flags()`
- Implement all ten checks in the exact order and with the exact persisted model from §0.2. A schema-only hand-constructed record does not satisfy this task; the independent mutation tests and Task 7D runtime persistence are required.

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/agents/tests/test_runtime_assurance.py -q
```

Expected: all PASS.

### Task 6: Trace schema versioning

**Step 1: Write trace version test**

```python
# packages/agents/tests/test_trace_version.py

def test_trace_schema_has_version():
    """Agents trace DB has explicit schema version registry."""
    from catalyst_agents.trace.version import TRACE_SCHEMA_VERSION, get_trace_schema_version

    db = create_temp_trace_db()
    initialize_trace_schema(db)
    assert get_trace_schema_version(db) == TRACE_SCHEMA_VERSION
    assert db.execute("SELECT COUNT(*) FROM trace_schema_version").fetchone()[0] == 1


def test_trace_version_not_data_core_user_version():
    """Agents trace version is independent of data-core user_version."""
    from catalyst_agents.trace.version import TRACE_SCHEMA_VERSION
    db = create_temp_trace_db()
    initialize_trace_schema(db)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    assert get_trace_schema_version(db) == TRACE_SCHEMA_VERSION


def test_unknown_future_trace_version_fails_closed():
    db = create_temp_trace_db(schema_version="999.0.0")
    with pytest.raises(UnsupportedTraceSchemaVersion):
        open_trace_reader(db)
```

**Step 2: Implement `catalyst_agents/trace/version.py`**

Use a singleton table `trace_schema_version(singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1), schema_version TEXT NOT NULL, applied_at TEXT NOT NULL)`. `init_trace_db()` upgrades an unversioned existing trace schema as v1 input to `2.0.0` in one transaction, creates `run_assurance`, preserves all existing run/event/artifact/link rows, and leaves `PRAGMA user_version=0`. A known older version upgrades; an unknown newer version raises `UnsupportedTraceSchemaVersion` before DDL/DML. Do not catch broad `OperationalError` and continue.

**Step 3: Run tests**

```bash
.venv/bin/python -m pytest packages/agents/tests/test_trace_version.py -q
```

Expected: PASS.

### Task 7: Production workflow and runtime integration

Task 7 is required implementation, not optional cleanup. Execute all five sub-tasks in order and keep their focused tests green after each sub-task.

#### Task 7A: Replace node-owned retrieval and cutoff logic

1. Add RED tests in `test_retrieval_policy.py` and `test_miner.py` using `FixtureRetriever` and one shared recording cutoff policy.
2. Prove Miner passes the exact query, ticker, canonical cutoff, requested corpus manifest, `top_k=8`, and `candidate_depth=20` to the injected Retriever.
3. Prove Miner maps every `RetrievedEvidence` field into Judge-visible evidence without dropping chunk/document identity, available time, source class, served mode, degradation reason, or manifest identities.
4. Prove Retriever exceptions become a typed system error and never trigger the old SQLite/LanceDB path.
5. Remove direct SQLite, `default_db_path`, ±3-calendar-day calculation, and old SQL fallback ownership from `catalyst_agents/retrieval/policy.py` and `nodes/miner.py`. Keep only a compatibility facade that delegates to the injected protocol if an existing import path must survive.
6. Inject the same cutoff-policy object into Validator. Execute Miner and Validator paths and assert identical `(ticker, session_date, mode)` calls and cutoff values; calling the policy twice directly is prohibited as a tautological test.

Run after RED and after GREEN:

```bash
.venv/bin/python -m pytest packages/agents/tests/test_retrieval_policy.py packages/agents/tests/test_miner.py packages/agents/tests/test_validator.py -q
```

#### Task 7B: Put Context Builder into the real graph

1. Add a RED graph test asserting invocation order starts `context_builder → miner`; inspect executed fixture calls, not graph source text.
2. Extend `AttributionState` with typed context artifact, cutoff, corpus/index identities, hypotheses, source-support flags, assurance inputs, run-level cost status, and repair/retry counters. Remove new writes of legacy confidence-bearing causes.
3. Update `build_attribution_graph()` to require `context_provider`, `retriever`, and `cutoff_policy`, bind them into nodes, and set Context Builder as the entry point.
4. Preserve old imports only through one-line re-export/deprecation facades; do not retain a second executable graph or retrieval body.
5. Add a missing-dependency test that fails before node execution with `AttributionDependencyError`, with zero model and zero retrieval calls.

Run:

```bash
.venv/bin/python -m pytest packages/agents/tests/test_graph.py packages/agents/tests/test_state.py -q
```

#### Task 7C: Strengthen Judge, Validator, and Finalizer end to end

1. Add RED tests for the exact Judge JSON schema and update `prompts/judge.md` to request only that schema. `_parse_judge_response()` must reject missing/extra hypothesis fields rather than fill them with defaults.
2. Judge emits typed `Hypothesis` values with supporting, counter, missing and unavailable evidence plus facts/calculations/inferences, transmission mechanism, and change condition.
3. Validator independently checks evidence resolution, Judge visibility, `available_at <= cutoff`, relationship manifest validity/effectivity, every prerequisite gate, and direction support. It emits deterministic violation codes and may invoke the model at most once for exceptional repair.
4. Finalizer calls the single `rank_hypotheses()` implementation and `determine_status()`. Gate-failed hypotheses cannot produce SUFFICIENT/PARTIAL output. No weighted score or confidence field survives in the new output.
5. A counting stub proves normal execution makes exactly Critic + Judge model calls; a repair case makes exactly one additional Validator call. Retrieval expansion may not add another normal-path model call in B5.

Run:

```bash
.venv/bin/python -m pytest packages/agents/tests/test_judge.py packages/agents/tests/test_validator.py packages/agents/tests/test_finalizer.py packages/agents/tests/test_critic.py -q
```

#### Task 7D: Persist trace version and one assurance record per run

1. Add RED tests that initialize an empty DB at trace schema `2.0.0`, migrate a real v1-shaped temporary trace DB to v2 without losing rows, keep `PRAGMA user_version=0`, and reject `999.0.0` before any run read/write.
2. Implement the exact `run_assurance` DDL from §0.2 and atomic `persist_assurance_record(conn, record)`. Do not use `INSERT OR REPLACE`.
3. Build assurance inputs from persisted trace events plus final state. Independently mutate citation ID, Judge visibility, cutoff, gate result, legal path, identity, and repair count; each mutation must fail only its corresponding check where the contracts are otherwise independent.
4. Integrate persistence into the real traced runtime lifecycle. Success, PARTIAL, ABSTAIN, and SYSTEM_ERROR graph runs each leave exactly one assurance row. A failed-request row that never starts the graph is outside B5's attribution-run assurance requirement.
5. Add service read coverage proving the persisted JSON round-trips to the frozen model and is available after runner completion, not only returned ephemerally.

Run:

```bash
.venv/bin/python -m pytest packages/agents/tests/test_trace_version.py packages/agents/tests/test_trace.py packages/agents/tests/test_runtime_assurance.py packages/agents/tests/test_live_run_service.py -q
```

#### Task 7E: Complete status and cost compatibility

1. Add RED tests reading persisted historical `INSUFFICIENT` from `agent_runs`, trace events, and state dictionaries; every public decoder returns `ABSTAIN` while preserving the historical reason.
2. Assert every new state, trace, service and assurance write uses `ABSTAIN`, never `INSUFFICIENT`.
3. Add known- and unknown-price tests at per-node and run aggregate levels. Unknown cost is nullable and contaminates the aggregate to unknown; it never becomes `$0`.
4. Update all terminal-status and retryability tables consistently. ABSTAIN is terminal and follows the existing non-system insufficiency retry policy unless an explicit sub-reason says otherwise.

Run:

```bash
.venv/bin/python -m pytest packages/agents/tests/test_output_status.py packages/agents/tests/test_runtime_status.py packages/agents/tests/test_cost_tracker.py -q
```

#### Task 7F: B5 closure integration test

Add `packages/agents/tests/test_b5_integration.py` and execute one complete runtime run with literal fixtures, `FixtureContextProvider`, `FixtureRetriever`, the shared cutoff policy, and `StubModelClient`. Assert exact node order, two normal model calls, ranked hypotheses, status, citation set, cutoff, corpus identity, trace version/events/artifacts, one assurance row, source flags, cost status, and zero network. Install a real socket guard that raises on any connection attempt. Add separate ABSTAIN and SYSTEM_ERROR cases and prove each persists its matching trace and assurance state.

Run:

```bash
.venv/bin/python -m pytest packages/agents/tests/test_b5_integration.py -q
.venv/bin/python -m pytest packages/agents -q
```

Expected: both commands PASS with exact counts reported. `237+ passed` is not an acceptable completion assertion; report the observed count.

### Task 8: Verify agents-eval isolation

Extend the boundary test so it performs both checks: static scan of every agents `.py` file and `pyproject.toml`, plus a subprocess/import-hook smoke test that raises immediately on any `catalyst_eval` import while importing the public agents graph, attribution, runtime assurance, trace, and retrieval modules. Merely checking `sys.modules` after one module import is insufficient.

```bash
.venv/bin/python -m pytest packages/agents/tests/test_package_boundaries.py -q
```

Expected: PASS (no eval imports in agents).

## 8. Landmine Tests

1. **Agents imports eval** — `rg "catalyst_eval\|from eval\|import eval" packages/agents/catalyst_agents/` must return zero.
2. **Confidence score in output** — `rg "confidence\|probability.*score\|self_reported" packages/agents/catalyst_agents/attribution/` must return zero in ranking/output logic.
3. **Context Builder opens SQLite** — `rg "sqlite3\|sqlite\|\.db\|aqlite" packages/agents/catalyst_agents/attribution/context_builder.py` must return zero.
4. **Validator makes more than one repair call** — bounded to 1.
5. **Cost shows fabricated values** — `cost_status` must be "unknown" for models without pricing data.
6. **Market/sector event narratives without cited evidence** — Validator must flag.
7. **direction inferred from edge alone** — gate test must reject peer propagation with edge but no evidence.
8. **Cutoff policy diverges by node** — inject one recording `ExchangeCutoffPolicy` into Miner and Validator, execute both paths, and assert identical ticker/session/mode arguments and identical cutoff values.
9. **Historical status breakage** — a persisted `INSUFFICIENT` row must decode as `ABSTAIN`, while every newly persisted abstention must write `ABSTAIN` only.
10. **Assurance self-certifies** — corrupt one citation ID, one visibility set, and one cutoff timestamp independently; each mutation must turn the corresponding assurance check red.
11. **Agents retrieval opens SQLite** — `catalyst_agents/retrieval/policy.py` and Miner must accept an injected B4 Retriever; `rg "sqlite3|default_db_path|\.connect\(" packages/agents/catalyst_agents/retrieval packages/agents/catalyst_agents/nodes/miner.py` must return zero production matches.
12. **Unversioned relationship edge** — reject missing/expired edges, unknown relationship types, duplicate `edge_id`, or a manifest whose computed canonical hash does not equal `manifest_id`.
13. **Foundation-only completion** — `test_b5_integration.py` must execute the production graph and runner; the task matrix may not mark B5 complete based only on Tasks 0–6.
14. **Assurance returned but not persisted** — after each terminal graph run, query `run_assurance` and assert exactly one canonical record; an in-memory object alone fails.
15. **Parallel legacy workflow survives** — source and behavioral tests prove there is one Miner retrieval body, one ranking body, one canonical OutputStatus enum, and one graph entry path.
16. **Normal call budget drifts** — counting stub asserts exactly two normal model calls and at most one exceptional Validator repair.
17. **Hollow integration fixtures** — expected statuses, node order, assurance JSON, and corruption outcomes are literal test data and are never generated from production serializers/check functions.

## 9. Verification Ladder

```bash
# 1. New focused tests
.venv/bin/python -m pytest packages/agents/tests/test_attribution_fixtures.py -q
.venv/bin/python -m pytest packages/agents/tests/test_context_builder.py -q
.venv/bin/python -m pytest packages/agents/tests/test_hypothesis_gates.py -q
.venv/bin/python -m pytest packages/agents/tests/test_ranking.py -q
.venv/bin/python -m pytest packages/agents/tests/test_output_status.py -q
.venv/bin/python -m pytest packages/agents/tests/test_runtime_assurance.py -q
.venv/bin/python -m pytest packages/agents/tests/test_trace_version.py -q
.venv/bin/python -m pytest packages/agents/tests/test_cost_tracker.py -q
.venv/bin/python -m pytest packages/agents/tests/test_b5_integration.py -q

# 2. Existing node tests
.venv/bin/python -m pytest packages/agents/tests/test_critic.py -q
.venv/bin/python -m pytest packages/agents/tests/test_judge.py -q
.venv/bin/python -m pytest packages/agents/tests/test_validator.py -q
.venv/bin/python -m pytest packages/agents/tests/test_finalizer.py -q
.venv/bin/python -m pytest packages/agents/tests/test_miner.py -q
.venv/bin/python -m pytest packages/agents/tests/test_decision_router.py -q
.venv/bin/python -m pytest packages/agents/tests/test_graph.py -q
.venv/bin/python -m pytest packages/agents/tests/test_trace.py -q
.venv/bin/python -m pytest packages/agents/tests/test_runtime_status.py -q

# 3. Package canonical
.venv/bin/python -m pytest packages/agents -q

# 4. Package boundary
.venv/bin/python -m pytest packages/agents/tests/test_package_boundaries.py -q

# 5. Dependency regression
HF_HUB_OFFLINE=1 .venv/bin/python -m pytest packages/data-core -q
.venv/bin/python -m pytest packages/eval -q
.venv/bin/python -m pytest packages/app -q

# 6. DB SHA
shasum -a 256 data/catalyst_dev_ws4b.db
shasum -a 256 data/catalyst_eval_frozen_v2.db

# 7. Git
git diff --check
```

## 10. Evidence Report Template

```markdown
## B5 Completion Report

### Files Changed
- [list]

### Focused Test Counts
- test_attribution_fixtures: X passed
- test_context_builder: X passed
- test_hypothesis_gates: X passed
- test_ranking: X passed
- test_output_status: X passed
- test_runtime_assurance: X passed
- test_trace_version: X passed
- test_cost_tracker: X passed
- test_b5_integration: X passed

### Required Task Matrix
- Task 0 fixtures: COMPLETE
- Task 1 Context Builder: COMPLETE
- Task 2 hypothesis/gates: COMPLETE
- Task 3 ranking: COMPLETE
- Task 4 statuses: COMPLETE
- Task 5 assurance checks: COMPLETE
- Task 6 trace version: COMPLETE
- Task 7A retrieval/cutoff wiring: COMPLETE
- Task 7B graph wiring: COMPLETE
- Task 7C node contracts: COMPLETE
- Task 7D assurance persistence: COMPLETE
- Task 7E status/cost compatibility: COMPLETE
- Task 7F end-to-end runtime: COMPLETE
- Task 8 package boundary: COMPLETE

### Canonical Counts
- agents: X passed (B4 baseline 237)
- data-core: X passed (B4 baseline 942 passed, 1 skipped, 1 xfailed; run offline)
- eval: X passed (B4 baseline 115)
- app: X passed (was 128)

### Boundary Check
- agents-eval isolation: PASS
- agents does not import catalyst_eval: confirmed

### DB SHA
- Dev DB: [unchanged from B4]
- Frozen DB: unchanged

### Unresolved Risks
- [list]

### Confirmation
- [ ] Next package (B6) not started
```

## 11. Git Boundaries

1. `feat(agents): add deterministic Context Builder and ContextProvider protocol`
2. `feat(agents): add typed hypothesis schema and prerequisite gates`
3. `feat(agents): add lexicographic ranking and output status types`
4. `feat(agents): strengthen Judge for structured hypothesis output`
5. `feat(agents): strengthen Validator for gate checks and one-repair limit`
6. `feat(agents): strengthen Finalizer for lexicographic ranking`
7. `feat(agents): add RunAssuranceRecord in runtime/assurance/`
8. `feat(agents): add trace schema version registry`
9. `feat(agents): handle unknown model cost in cost_tracker`

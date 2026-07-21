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


## 1. Objective

Deliver B5 that passes Core Exit Gates D (Agent workflow), E (Runtime assurance), and contributes to Gate G (Contributor experience). Specifically:

- Deterministic Context Builder producing market/sector/peer decomposition;
- Injected ContextProvider protocol (backed by data-core);
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

- RunAssuranceRecord (JSON, per run)
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
- `RunAssuranceRecord` (run_id, check_name, status, detail, checked_at)

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
```

### Allowed to Modify

```
packages/agents/catalyst_agents/state.py                     — add typed output fields
packages/agents/catalyst_agents/nodes/miner.py               — use cutoff-safe retrieval
packages/agents/catalyst_agents/retrieval/policy.py           — remove direct SQLite access; delegate through injected B4 Retriever
packages/agents/catalyst_agents/nodes/critic.py              — strengthen output typing
packages/agents/catalyst_agents/nodes/judge.py               — structured hypothesis output
packages/agents/catalyst_agents/nodes/validator.py           — gate checks, repair
packages/agents/catalyst_agents/nodes/finalizer.py           — lexicographic ranking
packages/agents/catalyst_agents/nodes/decision_router.py     — timeline/novelty projection
packages/agents/catalyst_agents/graph.py                     — add Context Builder node
packages/agents/catalyst_agents/runtime/runner.py            — produce RunAssuranceRecord
packages/agents/catalyst_agents/runtime/service.py           — wire assurance
packages/agents/catalyst_agents/cost_tracker.py              — unknown model cost
packages/agents/tests/test_graph.py                          — add Context Builder tests
packages/agents/tests/test_critic.py                         — strengthen output tests
packages/agents/tests/test_judge.py                          — hypothesis schema tests
packages/agents/tests/test_validator.py                      — gate check tests
packages/agents/tests/test_finalizer.py                      — ranking tests
packages/agents/tests/test_miner.py                          — cutoff-safe retrieval
```

### Files Explicitly Forbidden

- `packages/eval/` — agents must not import eval
- `packages/app/` — B7 domain
- `packages/data-core/` — consume public B4 protocols only; no B5 modifications
- `docs/plans/2026-07-21-b2-b7-technical-contracts.md`

## 7. TDD Tasks

### Task 0: Deterministic attribution fixtures

Create `packages/agents/tests/attribution_fixtures.py` with `FixtureContextProvider`, exact OHLCV/session data, hypothesis builders exposing all eight ranking criteria, a recording cutoff policy, a valid relationship-manifest fixture, and persisted-run artifact builders used below. It also defines `EXPECTED_20_SESSIONS`, `VALID_12_VOLUMES`, `EXPECTED_VALID_12_SESSION_IDS`, and `volume_window_provider`; these are literal independent fixtures and must include an older out-of-window observation that would change the median if the implementation illegally expanded its lookback. Fixture helpers return immutable copies and fail on unregistered tickers/sessions. Run their self-tests before Task 1; later snippets must not define ad-hoc mocks.

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
    provider = mock_provider_with_ohlcv()
    b1 = ContextBuilder(provider=provider).build(ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z")
    b2 = ContextBuilder(provider=provider).build(ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z")
    assert b1.target_return_pct == b2.target_return_pct
    assert b1.market_component == b2.market_component
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
    from catalyst_agents.attribution.hypothesis import Hypothesis

    h = Hypothesis(
        cause_label="earnings_guidance",
        direction="negative",
        transmission_mechanism="Reduced forward guidance lowered revenue expectations",
        supporting_evidence=[{"chunk_id": "poly:1:1::001", "relevance": 2}],
        counter_evidence=[],
        missing_evidence=["Actual EPS figure not yet released"],
        change_condition="If actual EPS exceeds consensus, reassess",
        prerequisite_gate_passed=True,
        source_support_flags={"opinion_only_support": False, "unknown_origin_support": False},
        facts=["EPS guidance was lowered from $2.00 to $1.50"],
        calculations=[],
        inferences=["Market interpreted guidance cut as demand weakness signal"],
        unavailable_evidence=["Full earnings transcript"],
    )
    assert h.cause_label == "earnings_guidance"
    assert h.prerequisite_gate_passed is True


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

    record = RunAssuranceRecord(
        run_id="run-001",
        checks=[
            {"check_name": "cutoff_violation", "status": "pass", "detail": "0 violations", "checked_at": "2026-01-01T00:00:00Z"},
            {"check_name": "citation_resolution", "status": "pass", "detail": "all resolved", "checked_at": "2026-01-01T00:00:00Z"},
            {"check_name": "visibility_ok", "status": "pass", "detail": "all visible", "checked_at": "2026-01-01T00:00:00Z"},
            {"check_name": "gate_violation", "status": "pass", "detail": "0 violations", "checked_at": "2026-01-01T00:00:00Z"},
            {"check_name": "trace_completeness", "status": "pass", "detail": "complete", "checked_at": "2026-01-01T00:00:00Z"},
            {"check_name": "identity_ok", "status": "pass", "detail": "all present", "checked_at": "2026-01-01T00:00:00Z"},
        ]
    )
    assert len(record.checks) == 6


def test_assurance_without_eval_import():
    """Assurance module does not import catalyst_eval."""
    import catalyst_agents.runtime.assurance as a
    # Must not have eval import
    import sys
    assert "catalyst_eval" not in str(sys.modules.get("catalyst_agents.runtime.assurance", ""))


def test_assurance_deterministic():
    """Same run artifacts → same assurance record (deterministic)."""
    from catalyst_agents.runtime.assurance.checks import run_all_checks

    artifacts1 = mock_run_artifacts(cutoff="2026-01-15T21:00:00Z", citations=["c1", "c2"])
    artifacts2 = mock_run_artifacts(cutoff="2026-01-15T21:00:00Z", citations=["c1", "c2"])

    checks1 = run_all_checks("run-001", artifacts1)
    checks2 = run_all_checks("run-001", artifacts2)

    for c1, c2 in zip(checks1, checks2):
        assert c1["status"] == c2["status"]


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

    est = CostEstimate(tokens_prompt=1000, tokens_completion=200)
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
- All L0 checks: cutoff_violation, citation_resolution, visibility_ok, gate_violation, trace_completeness, identity_ok
- Plus [R]-tagged L3 checks: structured_context_support

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

**Step 3: Run tests**

```bash
.venv/bin/python -m pytest packages/agents/tests/test_trace_version.py -q
```

Expected: PASS.

### Task 7: Node strengthening

**Step 1: Modify Judge for structured hypothesis output**

Update `catalyst_agents/nodes/judge.py`:
- Output typed `Hypothesis` list, not free-form text
- Include `supporting_evidence`, `counter_evidence`, `missing_evidence`, `transmission_mechanism`, `change_condition`
- Separate `facts`, `calculations`, `inferences`, `unavailable_evidence`

**Step 2: Modify Validator for gate checks**

Update `catalyst_agents/nodes/validator.py`:
- Verify prerequisite gates for each emitted hypothesis
- Verify cited evidence IDs resolve and were Judge-visible
- Verify cutoff compliance on all evidence
- At most one exceptional repair call
- Emit `gate_violation_count`

**Step 3: Modify Finalizer for lexicographic ranking**

Update `catalyst_agents/nodes/finalizer.py`:
- Replace any weighted scoring with lexicographic `rank_hypotheses()`
- Assign `SUFFICIENT|PARTIAL|ABSTAIN|SYSTEM_ERROR` per `determine_status()`

**Step 4: Wire Context Builder into graph**

Update `catalyst_agents/graph.py`:
- Add Context Builder as first node (before Miner)
- Pass ContextBuilderArtifact through state

**Step 5: Modify Runner for assurance**

Update `catalyst_agents/runtime/runner.py`:
- After run completes, call `run_all_checks()`
- Persist `RunAssuranceRecord` alongside trace

**Step 4 (for all node changes): Run full test suite**

```bash
.venv/bin/python -m pytest packages/agents -q
```

Expected: 237+ passed, with new tests adding to the count.

### Task 8: Verify agents-eval isolation

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

## 9. Verification Ladder

```bash
# 1. New focused tests
.venv/bin/python -m pytest packages/agents/tests/test_context_builder.py -q
.venv/bin/python -m pytest packages/agents/tests/test_hypothesis_gates.py -q
.venv/bin/python -m pytest packages/agents/tests/test_ranking.py -q
.venv/bin/python -m pytest packages/agents/tests/test_output_status.py -q
.venv/bin/python -m pytest packages/agents/tests/test_runtime_assurance.py -q
.venv/bin/python -m pytest packages/agents/tests/test_trace_version.py -q

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
.venv/bin/python -m pytest packages/data-core -q
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
- test_context_builder: X passed
- test_hypothesis_gates: X passed
- test_ranking: X passed
- test_output_status: X passed
- test_runtime_assurance: X passed

### Canonical Counts
- agents: X passed (was 237)
- data-core: X passed (was 758)
- eval: X passed (was 93)
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

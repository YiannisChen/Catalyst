# B4 — Cutoff-Safe Lexical Retrieval and Eval Foundation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Create the Eval Foundation schemas (BenchmarkCase, evidence judgment, pool manifest, lineage validator, dataset versioning, grade tooling) before any comparison pool is persisted. Build cutoff-safe FTS5/BM25 retrieval with filter-before-score, ticker/time/evidence-type filters, retrieval trace, and RetrievalResult.

**Architecture:** B4 has two coordinated work streams: (A) agent-agnostic Eval Foundation schemas without labels; (B) one shared exchange-session cutoff policy plus persistent FTS5/BM25 retrieval. Because Core requires a reproducible persistent lexical index, B4 owns migration v10 rather than leaving it optional.

**Tech Stack:** Python 3.12+, SQLite FTS5, pydantic, pytest, BGE-M3 tokenizer (already pinned in B3)

**Binding Contract:** `docs/plans/2026-07-21-b2-b7-technical-contracts.md` §6

---

## 0. Execution Rules

- B3 must be independently verified first. Test v10 only on temporary databases and disposable copies.
- Do not stage, commit, push, author benchmark labels, call providers, or mutate canonical DB files.
- The current trading calendar does not contain session-close timestamps or early-close sessions. Implement and test them before retrieval.
- `filter-before-score` must be demonstrated behaviorally with an illegal post-cutoff document that would otherwise rank first; SQL-text inspection alone is insufficient.
- Every retrieval fixture includes exact status, manifest, cutoff, ticker, and source-class controls. Tests assert all five dimensions independently.
- Eval tests do not import agents. Cross-package cutoff parity is proven later by dependency injection into agents, not by importing Validator into data-core.
- No undefined fixture helper, broad `Exception`, conditional assertion, or self-comparison is permitted.
- `catalyst_data/retrieval/` is the canonical data-core retrieval home after B4. Existing `catalyst_data/retrieval_policy.py` becomes a compatibility facade that delegates to the package and is scheduled for removal after downstream callers migrate. `catalyst_agents/retrieval/policy.py` is an agents adapter and must not implement scoring or open SQLite after B5.


## 1. Objective

Deliver B4 that passes Core Exit Gate C (Retrieval, FTS5/BM25 baseline) and establishes the evaluation scaffold for Core Exit Gate F (Evaluation). Two sub-objectives:

**Stream A — Eval Foundation:**
- `BenchmarkCase` schema with all contract fields
- Evidence judgment schema (grade 2/1/0 with rationale)
- Pool manifest schema
- Lineage validator
- Dataset versioning
- Grade tooling

**Stream B — Cutoff-safe lexical retrieval:**
- FTS5/BM25 index on corpus chunks
- Filter-before-score: `available_at <= cutoff`, ticker, evidence-type, source filters
- Official session close with early-close support, explicit UTC `as_of` for intraday
- FTS5 `bm25()` lower-is-better → `lexical_raw_score`, one-based `lexical_rank`
- Top-20 candidates, stable tie-breaking, final top-8
- `RetrievalResult` with all contract fields
- Retrieval trace (node events consumed by agents trace)
- Explicit SQL degraded mode
- No ±3-day symmetric future leakage

## 2. Current Verified State

### Stream A — Eval Foundation

| Component | File | Status |
|---|---|---|
| GoldenEvent schema (legacy) | `catalyst_eval/schema/golden_event.py` | implemented but legacy; needs BenchmarkCase replacement |
| Result schema | `catalyst_eval/schema/result.py` | implemented — partial |
| Metrics base | `catalyst_eval/metrics/base.py` | implemented |
| Judge base | `catalyst_eval/metrics/judge_base.py` | implemented |
| Harness | `catalyst_eval/harness/` | implemented |
| Adapters | `catalyst_eval/adapters/agents.py` | implemented |
| Frozen eval | `catalyst_eval/harness/frozen_eval.py` | implemented |

### Stream B — Retrieval

| Component | File | Status |
|---|---|---|
| Retrieval policy (agents side) | `catalyst_agents/retrieval/policy.py` | implemented — but not cutoff-safe |
| Index builder | `catalyst_data/index_builder.py` | existing — needs FTS5 integration |
| LanceDB store | `catalyst_data/storage/lancedb_store.py` | implemented — dense only |
| Source tier filter | `catalyst_data/source_tier.py` | implemented |
| Trading calendar | `catalyst_data/trading_calendar.py` | partial — trading dates and holidays exist; session-close times, timezone conversion, and early closes are missing |
| `index_state` table | `catalyst_data/storage/sqlite.py` | implemented |
| Data-core retrieval policy | `catalyst_data/retrieval_policy.py` | partial |

### Missing (both streams)

| Component | File to create |
|---|---|
| BenchmarkCase schema | `packages/eval/catalyst_eval/benchmark/case.py` (new) |
| Evidence judgment schema | `packages/eval/catalyst_eval/benchmark/judgment.py` (new) |
| Pool manifest | `packages/eval/catalyst_eval/benchmark/pool_manifest.py` (new) |
| Lineage validator | `packages/eval/catalyst_eval/benchmark/lineage.py` (new) |
| Dataset versioning | `packages/eval/catalyst_eval/benchmark/versioning.py` (new) |
| Grade tooling | `packages/eval/catalyst_eval/benchmark/grade.py` (new) |
| FTS5 retrieval module | `packages/data-core/catalyst_data/retrieval/fts5.py` (new, target layout) |
| RetrievalResult schema | `packages/data-core/catalyst_data/retrieval/result.py` (new) |
| Cutoff filter | `packages/data-core/catalyst_data/retrieval/cutoff.py` (new) |
| Retrieval trace writer | `packages/data-core/catalyst_data/retrieval/trace.py` (new) |
| FTS5 index builder | `packages/data-core/catalyst_data/retrieval/fts5_builder.py` (new) |
| Degraded SQL mode | `packages/data-core/catalyst_data/retrieval/degraded.py` (new) |

## 3. Scope / Non-goals

### Scope — Stream A

- BenchmarkCase schema (case_id, dataset_version, split, ticker, session_date, cutoff_ts, observable_facts, answerability, expected_abstention_reason_class, acceptable_cause_labels, evidence_judgments, pool_manifest, unjudged handling, lineage)
- Evidence judgment schema (chunk_id, grade 2/1/0, one-line rationale)
- Pool manifest (arm identities, versions, chunk inventory)
- Lineage validator (batch-stamp detection, timestamps before session dates, real per-action timestamps required)
- Dataset versioning (dataset_version in BenchmarkCase)
- Grade tooling (grade 2/1/0 constants, helpers)
- No labels authored in B4
- No pool persisted in B4

### Scope — Stream B

- FTS5 full-text index on `corpus_chunks.content_text`
- `filter_before_score()`: `active AND manifest_id match AND available_at <= cutoff AND ticker/evidence/source filters`
- Get official session close from trading calendar (including early-close)
- Intraday: explicit UTC `as_of` instant instead of session close
- FTS5 `bm25()` raw score; `lexical_rank` is one-based ordering of `bm25()` ascending
- Top-20 candidates, final top-8
- Stable ties: `chunk_id ASC`
- `RetrievalResult` per chunk: chunk_id, document_id, available_at, cutoff, filters, source_class, `lexical_raw_score`, `lexical_rank`, corpus_manifest_id, degraded/fallback state, timing
- Retrieval trace (stage events: filter counts, candidate count, final count, timing)
- Explicit degraded mode: SQL-only fallback when FTS5 unavailable

### Non-goals

- BGE-M3 dense retrieval (B6)
- RRF fusion (B6)
- Reranker (B6)
- Union judgment pool generation (B6)
- Human labeling or grading (delayed second pass after B6)
- Metric computation (B7)
- ResultPack/scorecard (B7)
- Any frontend or API surface
- ±3-day symmetric window for cutoff (prohibited)

## 4. Dependencies

### Inputs

- B3 completion: corpus_chunks table, CorpusManifest, chunk identities, source_class
- Existing trading_calendar with session close times
- Existing `catalyst-eval` package structure with adapters

### Output Artifacts

- Eval Foundation schemas (no data)
- FTS5 index on corpus_chunks
- RetrievalResult type
- Retrieval trace events

### Consumed By

- B5 (attribution): consumes RetrievalResult and retrieval trace
- B6 (dense/reranker): consumes FTS5 baseline, same cutoff contract
- B7 (eval metrics): consumes BenchmarkCase schema, evidence judgment schema

## 5. Schema and Artifact Ownership

### Owned SQLite Migration

**v10** (owned by B4 and required for the persistent Core lexical index):

- FTS5 virtual table on `corpus_chunks.content_text`
- FTS5 metadata table (schema version, last build timestamp)

### Owned Schemas (Eval Foundation)

- `BenchmarkCase` (pydantic model)
- `EvidenceJudgment` (pydantic model)
- `PoolManifest` (pydantic model): `schema_version`, `pool_id`, `case_id`, `arms`,
  `chunk_inventory`, `corpus_manifest_id`, `index_manifest_id`, `source_artifact_id`, `created_at`.
  The corpus/index identities are required lineage, not optional metadata.
- `DatasetVersion` (pydantic model)
- Grade constants (2/1/0)

### Owned Schemas (Retrieval)

- `RetrievalResult` (pydantic model / dataclass)
- `RetrievalTraceEvent` (typed dict)

### Not Owned

- CorpusChunk schema (B3)
- CorpusManifest (B3)
- Trace writer/schema (B5 — agents trace)
- MetricRecord/ResultPack (B7)
- Dense index (B6)
- IndexManifest (B6)

## 6. File Allowlist

### Stream A — Allowed to Create

```
packages/eval/catalyst_eval/benchmark/__init__.py
packages/eval/catalyst_eval/benchmark/case.py
packages/eval/catalyst_eval/benchmark/judgment.py
packages/eval/catalyst_eval/benchmark/pool_manifest.py
packages/eval/catalyst_eval/benchmark/lineage.py
packages/eval/catalyst_eval/benchmark/versioning.py
packages/eval/catalyst_eval/benchmark/grade.py
packages/eval/tests/test_benchmark_case.py
packages/eval/tests/test_judgment.py
packages/eval/tests/test_pool_manifest.py
packages/eval/tests/test_lineage.py
packages/eval/tests/test_grade.py
packages/eval/tests/benchmark_fixtures.py
```

### Stream A — Allowed to Modify

```
packages/eval/catalyst_eval/schema/__init__.py          — re-export BenchmarkCase
packages/eval/catalyst_eval/__init__.py                 — public API
packages/eval/tests/test_schema.py                      — add BenchmarkCase tests
```

### Stream B — Allowed to Create

```
packages/data-core/catalyst_data/retrieval/__init__.py
packages/data-core/catalyst_data/retrieval/fts5.py
packages/data-core/catalyst_data/retrieval/result.py
packages/data-core/catalyst_data/retrieval/cutoff.py
packages/data-core/catalyst_data/retrieval/trace.py
packages/data-core/catalyst_data/retrieval/fts5_builder.py
packages/data-core/catalyst_data/retrieval/degraded.py
packages/data-core/tests/test_fts5_retrieval.py
packages/data-core/tests/test_retrieval_cutoff.py
packages/data-core/tests/test_retrieval_result.py
packages/data-core/tests/retrieval_fixtures.py
```

### Stream B — Allowed to Modify

```
packages/data-core/catalyst_data/migrations.py          — required v10
packages/data-core/catalyst_data/storage/sqlite.py      — FTS5 virtual table
packages/data-core/catalyst_data/index_builder.py       — add FTS5 build step
packages/data-core/catalyst_data/retrieval_policy.py    — integrate with new retrieval
packages/data-core/catalyst_data/trading_calendar.py    — official close and early-close policy
packages/data-core/tests/test_migrations.py             — v10 tests
```

### Files Explicitly Forbidden

- `data/catalyst_eval_frozen_v2.db` — frozen, no modification
- `packages/agents/` — B5 domain (read-only for agents tests)
- `packages/app/` — B7 domain
- `docs/plans/2026-07-21-b2-b7-technical-contracts.md`

## 7. TDD Tasks

Before Stream A Task 1, create `packages/eval/tests/benchmark_fixtures.py` with `min_benchmark_fields()`, valid lineage records, and complete pool identities. Before Stream B Task 7, create the listed `retrieval_fixtures.py` with temporary v9/v10 DB builders and exact legal/illegal candidate inventories. Its `insert_corpus_chunk()` helper must use an explicit column list covering all required v9 fields, including `manifest_id`; positional `INSERT ... VALUES` is prohibited. No later helper name may be left undefined.

### Stream A Task 1: BenchmarkCase schema

**Step 1: Write failing BenchmarkCase test**

```python
# packages/eval/tests/test_benchmark_case.py

def test_benchmark_case_minimum_fields():
    """BenchmarkCase has all contract-required fields."""
    from catalyst_eval.benchmark.case import BenchmarkCase

    case = BenchmarkCase(
        case_id="B001",
        schema_version="1.0.0",
        dataset_version="1.0.0",
        split="core_answerable",
        ticker="AAPL",
        session_date="2026-01-15",
        cutoff_ts="2026-01-15T21:00:00Z",
        observable_facts={"close_return_pct": -3.5},
        answerability="answerable",
        acceptable_cause_labels=[],
        evidence_judgments=[],
        pool_manifest=None,
        lineage=None,
        annotator_notes="",
    )
    assert case.case_id == "B001"
    assert case.cutoff_ts is not None


def test_benchmark_case_splits_are_valid():
    """Splits: core_answerable, core_abstain, retrieval_only, adversarial."""
    from catalyst_eval.benchmark.case import BenchmarkCase, VALID_SPLITS

    assert "core_answerable" in VALID_SPLITS
    assert "core_abstain" in VALID_SPLITS
    assert "retrieval_only" in VALID_SPLITS

    # adversarial requires parent_case_id
    with pytest.raises(ValidationError):
        BenchmarkCase(split="adversarial", parent_case_id=None, **min_benchmark_fields())


def test_benchmark_case_evidence_judgments():
    """Evidence judgments require chunk_id, grade, rationale."""
    from catalyst_eval.benchmark.case import BenchmarkCase
    from catalyst_eval.benchmark.judgment import EvidenceJudgment

    judgment = EvidenceJudgment(
        chunk_id="poly:art1:v1::001",
        grade=2,
        rationale="Directly describes the product recall event.",
        annotator="fixture",
        judged_at="2026-07-22T00:00:00Z",
    )
    case = BenchmarkCase(
        evidence_judgments=[judgment],
        **min_benchmark_fields()
    )
    assert len(case.evidence_judgments) == 1
    assert case.evidence_judgments[0].grade == 2


def test_unjudged_handling():
    """Unjudged handling field: chunks outside pool are explicitly unjudged."""
    from catalyst_eval.benchmark.case import BenchmarkCase

    case = BenchmarkCase(
        unjudged_handling="chunks_outside_pool_explicitly_unjudged",
        **min_benchmark_fields()
    )
    assert case.unjudged_handling is not None
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_benchmark_case.py -q
```

Expected: FAIL.

**Step 3: Implement**

- `packages/eval/catalyst_eval/benchmark/case.py`: `BenchmarkCase` pydantic model
- `packages/eval/catalyst_eval/benchmark/judgment.py`: `EvidenceJudgment` model with required `chunk_id`, grade 2/1/0, non-empty rationale, non-empty annotator, and UTC `judged_at`; absence from the mapping means UNJUDGED and is never represented as grade 0
- All contract fields from eval architecture review §D

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_benchmark_case.py -q
```

Expected: all PASS.

### Stream A Task 2: Grade constants and tooling

**Step 1: Write grade test**

```python
# packages/eval/tests/test_grade.py

def test_grade_values():
    """Grade constants are 2, 1, 0."""
    from catalyst_eval.benchmark.grade import RELEVANT_DIRECT, RELEVANT_INDIRECT, NOT_RELEVANT

    assert RELEVANT_DIRECT == 2
    assert RELEVANT_INDIRECT == 1
    assert NOT_RELEVANT == 0


def test_is_relevant():
    """is_relevant(grade) returns True for 1 or 2."""
    from catalyst_eval.benchmark.grade import is_relevant, RELEVANT_DIRECT, RELEVANT_INDIRECT, NOT_RELEVANT

    assert is_relevant(RELEVANT_DIRECT)
    assert is_relevant(RELEVANT_INDIRECT)
    assert not is_relevant(NOT_RELEVANT)
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_grade.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_eval/benchmark/grade.py`**

```python
RELEVANT_DIRECT = 2
RELEVANT_INDIRECT = 1
NOT_RELEVANT = 0

def is_relevant(grade: int) -> bool:
    return grade >= 1
```

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_grade.py -q
```

Expected: PASS.

### Stream A Task 3: Pool manifest

**Step 1: Write pool manifest test**

```python
# packages/eval/tests/test_pool_manifest.py

def test_pool_manifest_tracks_arms():
    """Pool manifest lists which retrieval arms contributed and their versions."""
    from catalyst_eval.benchmark.pool_manifest import PoolManifest

    manifest = PoolManifest(
        schema_version="1.0.0",
        pool_id="pool-001",
        case_id="B001",
        arms=[
            {"arm": "fts5", "version": "1.0.0", "top_k": 20},
            {"arm": "dense", "version": "1.0.0", "top_k": 20},
        ],
        chunk_inventory=["chunk1", "chunk2"],
        corpus_manifest_id="corpus-v1",
        index_manifest_id="index-v1",
        source_artifact_id="retrieval-arm-artifact-v1",
        created_at="2026-01-01T00:00:00Z",
    )
    assert len(manifest.arms) == 2
    assert manifest.corpus_manifest_id == "corpus-v1"
    assert manifest.index_manifest_id == "index-v1"
    assert manifest.source_artifact_id == "retrieval-arm-artifact-v1"
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_pool_manifest.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_eval/benchmark/pool_manifest.py`**

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_pool_manifest.py -q
```

Expected: PASS.

### Stream A Task 4: Lineage validator

**Step 1: Write lineage test**

```python
# packages/eval/tests/test_lineage.py

def test_lineage_rejects_batch_stamps():
    """Uniform batch timestamp → lineage validation failure."""
    from catalyst_eval.benchmark.lineage import validate_lineage

    lineage = {
        "candidate_generation_source": "manual",
        "model_assisted_fields": [],
        "human_confirmed_fields": ["evidence_judgments"],
        "timestamps": {
            "annotation_started": "2026-06-01T00:00:00Z",
            "annotation_completed": "2026-06-01T00:00:00Z",  # batch stamp
        },
        "adjudication_status": "pending"
    }
    errors = validate_lineage(lineage)
    # Should flag identical start/completion times
    assert len(errors) > 0


def test_lineage_rejects_predating_timestamps():
    """Annotation timestamp before session date → failure."""
    from catalyst_eval.benchmark.lineage import validate_lineage

    session_date = "2026-01-15"
    lineage = {
        "timestamps": {"annotation_started": "2025-12-01T00:00:00Z"},
    }
    errors = validate_lineage(lineage, session_date=session_date)
    assert len(errors) > 0


def test_lineage_accepts_valid():
    """Valid lineage with real timestamps passes."""
    from catalyst_eval.benchmark.lineage import validate_lineage

    lineage = {
        "candidate_generation_source": "retrieval_arm_union",
        "model_assisted_fields": ["initial_grade_suggestions"],
        "human_confirmed_fields": ["evidence_judgments", "acceptable_cause_labels"],
        "timestamps": {
            "annotation_started": "2026-06-01T09:00:00Z",
            "annotation_completed": "2026-06-01T11:30:00Z",
            "second_pass_started": "2026-06-08T09:00:00Z",
            "second_pass_completed": "2026-06-08T11:00:00Z",
        },
        "adjudication_status": "resolved"
    }
    errors = validate_lineage(lineage, session_date="2026-01-15")
    assert len(errors) == 0
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_lineage.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_eval/benchmark/lineage.py`**

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_lineage.py -q
```

Expected: PASS.

### Stream A Task 5: Dataset versioning

**Step 1: Write versioning test**

```python
# packages/eval/tests/test_benchmark_case.py

def test_dataset_version_is_required():
    """Every BenchmarkCase must have a dataset_version."""
    with pytest.raises(ValidationError):
        BenchmarkCase(dataset_version=None, **min_benchmark_fields())


def test_dataset_version_increments_on_change():
    """Modifying judgments should require a new dataset_version."""
    from catalyst_eval.benchmark.versioning import bump_version

    v1 = "1.0.0"
    v2 = bump_version(v1, change="judgments_modified")
    assert v2 != v1
```

**Step 2: Implement `catalyst_eval/benchmark/versioning.py`**

**Step 3: Run tests**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_benchmark_case.py -k "version" -q
```

Expected: PASS.

### Stream A Task 6: Eval Foundation public API

Wire BenchmarkCase, EvidenceJudgment, PoolManifest, grade constants into `catalyst_eval.__init__` and `catalyst_eval.benchmark.__init__`.

```bash
.venv/bin/python -m pytest packages/eval -q
```

Expected: all existing 93 + new tests PASS.

### Stream B Task 7: Cutoff computation

Before implementing retrieval, add a focused v10 migration subtask: create the external-content FTS5 table and lexical-index metadata table on a temporary v9 database, register v10, rebuild from `corpus_chunks`, and assert a rollback leaves `user_version=9` and no half-created FTS objects. Do not migrate the canonical Dev DB.

**Step 1: Write cutoff test**

```python
# packages/data-core/tests/test_retrieval_cutoff.py

def test_cutoff_is_session_close():
    """Close-to-close cutoff uses official exchange close, including early-close."""
    from catalyst_data.retrieval.cutoff import compute_cutoff

    # Regular session
    cutoff = compute_cutoff(ticker="AAPL", session_date="2026-01-15",
                            mode="close_to_close")
    assert cutoff.endswith("T21:00:00Z")  # 4 PM ET = 21:00 UTC (standard)

    # Early-close session (e.g., day after Thanksgiving)
    cutoff_early = compute_cutoff(ticker="AAPL", session_date="2025-11-28",
                                  mode="close_to_close")
    assert "T18:00:00Z" in cutoff_early  # 1 PM ET close


def test_intraday_cutoff_is_explicit_utc():
    """Intraday uses explicit UTC as_of, not session close."""
    from catalyst_data.retrieval.cutoff import compute_cutoff

    cutoff = compute_cutoff(ticker="AAPL", session_date="2026-01-15",
                            mode="intraday", as_of="2026-01-15T15:30:00Z")
    assert cutoff == "2026-01-15T15:30:00Z"


def test_cutoff_is_utc():
    """All cutoffs are UTC ISO-8601."""
    from catalyst_data.retrieval.cutoff import compute_cutoff

    for mode in ["close_to_close", "intraday"]:
        cutoff = compute_cutoff(ticker="AAPL", session_date="2026-01-15",
                                mode=mode, as_of="2026-01-15T15:30:00Z")
        assert cutoff.endswith("Z")
        assert "T" in cutoff


def test_no_three_day_symmetric_window():
    """Cutoff is NOT ±3 days around session date."""
    from catalyst_data.retrieval.cutoff import compute_cutoff

    cutoff = compute_cutoff(ticker="AAPL", session_date="2026-01-15",
                            mode="close_to_close")
    # Cutoff should be on or after session date, never before
    assert cutoff == "2026-01-15T21:00:00Z"
    # Never 2026-01-12 (3 days before)
    assert "2026-01-12" not in cutoff[:10]
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_retrieval_cutoff.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_data/retrieval/cutoff.py`**

- `compute_cutoff(ticker, session_date, mode="close_to_close", as_of=None) → str`
- Add `session_close_utc(session_date)` to `trading_calendar.py`: 16:00 America/New_York for regular sessions, 13:00 for an explicit versioned early-close set, converted with `zoneinfo.ZoneInfo`
- Reject non-session dates and dates outside the calendar coverage window
- Only `close_to_close` and `intraday` modes

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_retrieval_cutoff.py -q
```

Expected: all PASS.

### Stream B Task 8: Filter-before-score

**Step 1: Write filter test**

```python
# packages/data-core/tests/test_fts5_retrieval.py

def test_filter_before_score():
    """Filtering happens before FTS5 scoring, not after."""
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    db = _fresh_db_with_chunks()
    # Add a post-cutoff document
    insert_corpus_chunk(
        db, chunk_id="test:post:1::001", document_id="test:post",
        content_text="relevant text about AAPL earnings",
        available_at="2026-01-16T09:00:00Z", eligibility="eligible",
        ticker_associations=["AAPL"], manifest_id="manifest-v1", status="active",
    )

    result = retrieve_lexical(db, query="AAPL earnings", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z", top_k=8)
    # Post-cutoff chunk excluded
    chunk_ids = {r.chunk_id for r in result.results}
    assert "test:post:1::001" not in chunk_ids


def test_ticker_filter():
    """Ticker filter restricts to chunks associated with the ticker."""
    db = _fresh_db_with_chunks()
    insert_corpus_chunk(db, chunk_id="test:aap:1::001", document_id="test:aap",
                        content_text="AAPL news", available_at="2026-01-01T09:00:00Z",
                        eligibility="eligible", ticker_associations=["AAPL"],
                        manifest_id="manifest-v1", status="active")
    insert_corpus_chunk(db, chunk_id="test:msft:1::001", document_id="test:msft",
                        content_text="MSFT news", available_at="2026-01-01T09:00:00Z",
                        eligibility="eligible", ticker_associations=["MSFT"],
                        manifest_id="manifest-v1", status="active")

    result = retrieve_lexical(db, query="news", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z", top_k=8)
    chunk_ids = {r.chunk_id for r in result.results}
    assert "test:aap:1::001" in chunk_ids
    assert "test:msft:1::001" not in chunk_ids


def test_top_k_after_filter():
    """TOP-K is applied AFTER filtering, not before."""
    # Post-cutoff documents cannot displace legal candidates
    db = _fresh_db_with_chunks()
    # Add 10 pre-cutoff chunks + 1 high-relevance post-cutoff
    for i in range(10):
        insert_corpus_chunk(
            db, chunk_id=f"test:pre{i}:1::001", document_id=f"test:pre{i}",
            content_text=f"some text {i}", available_at="2026-01-01T09:00:00Z",
            eligibility="eligible", ticker_associations=["AAPL"],
            manifest_id="manifest-v1", status="active",
        )
    # Post-cutoff high-relevance would rank #1 if not filtered
    insert_corpus_chunk(
        db, chunk_id="test:post:1::001", document_id="test:post",
        content_text="AAPL earnings surprise revenue beat",
        available_at="2026-01-16T09:00:00Z", eligibility="eligible",
        ticker_associations=["AAPL"], manifest_id="manifest-v1", status="active",
    )

    result = retrieve_lexical(db, query="AAPL earnings surprise revenue beat",
                              ticker="AAPL", cutoff="2026-01-15T21:00:00Z", top_k=3)
    chunk_ids = {r.chunk_id for r in result.results}
    assert "test:post:1::001" not in chunk_ids
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_fts5_retrieval.py -k "filter" -q
```

Expected: FAIL.

**Step 3: Implement filter layer**

Add filter predicate to `retrieve_lexical()`: WHERE clause applies availability, ticker, eligibility, source filters before `ORDER BY bm25()`.

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_fts5_retrieval.py -k "filter" -q
```

Expected: all PASS.

### Stream B Task 9: FTS5/BM25 retrieval core

**Step 1: Write retrieval test**

```python
# packages/data-core/tests/test_fts5_retrieval.py

def test_fts5_lower_is_better():
    """bm25() lower value ranks first."""
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    db = _fresh_db_with_chunks()
    result = retrieve_lexical(db, query="earnings", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z", top_k=8)

    # First result should have the lowest raw score
    if len(result.results) >= 2:
        assert result.results[0].lexical_raw_score <= result.results[1].lexical_raw_score


def test_lexical_rank_is_one_based():
    """lexical_rank starts at 1, not 0."""
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    db = _fresh_db_with_chunks()
    result = retrieve_lexical(db, query="test", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z", top_k=8)

    if result.results:
        assert result.results[0].lexical_rank == 1


def test_stable_ties():
    """Tied bm25() scores are broken by chunk_id ASC."""
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    db = _fresh_db_with_chunks()
    # Two chunks with identical content → tied bm25
    result = retrieve_lexical(db, query="identical", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z", top_k=8)

    # Tied results should have deterministic order
    scores = [r.lexical_raw_score for r in result.results]
    for i in range(len(scores) - 1):
        if scores[i] == scores[i + 1]:
            assert result.results[i].chunk_id < result.results[i + 1].chunk_id


def test_default_candidate_depth_20_display_8():
    """Default lexical candidate depth is 20, final display is 8."""
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    db = _fresh_db_with_chunks(n_chunks=30)
    result = retrieve_lexical(db, query="test", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z")

    assert result.candidate_count <= 20
    assert len(result.results) <= 8


def test_retrieval_result_all_fields():
    """RetrievalResult contains all contract fields."""
    from catalyst_data.retrieval.result import RetrievalResult

    rr = RetrievalResult(
        chunk_id="test:1:1::001",
        document_id="test:1",
        available_at="2026-01-01T09:00:00Z",
        cutoff="2026-01-15T21:00:00Z",
        filters_applied={"ticker": "AAPL"},
        source_class="reported_news",
        lexical_raw_score=-3.5,
        lexical_rank=1,
        dense_score=None,
        dense_rank=None,
        fusion_score=None,
        fusion_rank=None,
        reranker_score=None,
        reranker_rank=None,
        corpus_manifest_id="manifest-v1",
        index_manifest_id=None,
        is_degraded=False,
        timing_ms=12.3,
    )
    assert rr.chunk_id is not None
    assert rr.lexical_raw_score is not None
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_fts5_retrieval.py -q
```

Expected: FAIL.

**Step 3: Implement**

- `catalyst_data/retrieval/fts5.py`: `retrieve_lexical(db, query, ticker, cutoff, top_k=8, candidate_depth=20, filters=None) → RetrievalResultSet`
- `catalyst_data/retrieval/result.py`: `RetrievalResult`, `RetrievalResultSet`

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_fts5_retrieval.py -q
```

Expected: all PASS.

### Stream B Task 10: Retrieval trace

**Step 1: Write trace test**

```python
# packages/data-core/tests/test_fts5_retrieval.py

def test_retrieval_trace_events():
    """Retrieval produces trace events with filter counts, candidate count, timing."""
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    db = _fresh_db_with_chunks()
    result = retrieve_lexical(db, query="earnings", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              include_trace=True)

    assert result.trace.stage == "fts5"
    assert result.trace.pre_filter_count >= result.trace.post_filter_count
    assert result.trace.candidate_count == len(result.candidates)
    assert result.trace.timing_ms >= 0
```

**Step 2: Implement `catalyst_data/retrieval/trace.py`**

**Step 3: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_fts5_retrieval.py -k "trace" -q
```

Expected: PASS.

### Stream B Task 11: FTS5 index builder and degraded mode

**Step 1: Write index builder test**

```python
# packages/data-core/tests/test_fts5_retrieval.py

def test_fts5_build_from_corpus():
    """FTS5 index built from corpus_chunks, consumed by retrieve_lexical."""
    from catalyst_data.retrieval.fts5_builder import build_fts5_index

    db = _fresh_db_with_chunks()
    build_fts5_index(db)

    # Verify FTS5 virtual table exists
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%fts%'").fetchall()
    assert len(tables) > 0


def test_degraded_mode_no_fts5():
    """When FTS5 unavailable, falls back to SQL LIKE with explicit degraded flag."""
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    db = _fresh_db_without_fts5()
    result = retrieve_lexical(db, query="earnings", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z")

    assert result.is_degraded
    # Still returns results (SQL LIKE fallback)
    assert hasattr(result, "results")
```

**Step 2: Implement `catalyst_data/retrieval/fts5_builder.py` and `catalyst_data/retrieval/degraded.py`**

**Step 3: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_fts5_retrieval.py -k "build\|degraded" -q
```

Expected: all PASS.

### Stream B Task 12: Publish the shared cutoff contract

**Step 1: Write parity test**

```python
# packages/data-core/tests/test_retrieval_cutoff.py

def test_cutoff_policy_protocol_calls_production_calendar():
    """The exported policy delegates to the official session calendar once."""
    calendar = RecordingSessionCalendar(close_utc="2026-01-15T21:00:00Z")
    policy = ExchangeCutoffPolicy(calendar=calendar)
    assert policy.close_to_close("AAPL", "2026-01-15") == "2026-01-15T21:00:00Z"
    assert calendar.calls == [("2026-01-15",)]
```

**Step 2: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_retrieval_cutoff.py -q
```

Expected: PASS. B5 must inject this exported policy into Miner and Validator and contains the actual cross-path parity test; B4 does not self-prove parity by calling one function twice.

## 8. Landmine Tests

1. **Post-cutoff in top-8** — `test_filter_before_score` verifies no post-cutoff chunks in results.
2. **±3-day symmetric window** — `test_no_three_day_symmetric_window` verifies cutoff never before session date.
3. **TOP-K before filter** — verify ORDER BY runs after WHERE in SQL.
4. **Silent unjudged-as-zero** — grade tooling explicitly flags unjudged, never defaults to 0.
5. **Batch-stamped lineage accepted** — `test_lineage_rejects_batch_stamps` catches uniform timestamps.
6. **FTS5 score compared with dense score** — never; lexical_raw_score is engine-specific, rank is the comparable field.
7. **Eval Foundation creates pools before labels** — verify no judgment data persisted in B4; schema only.
8. **Manifest/status/source filters omitted** — independent fixtures must prove that a wrong-manifest, tombstoned, inactive, or excluded-source chunk never enters the candidate set even when it is the strongest lexical match.
9. **Early close treated as 16:00 ET** — verify 2025-11-28 closes at 18:00 UTC and a 19:00 UTC article is excluded.

## 9. Verification Ladder

```bash
# Stream A — Eval Foundation
.venv/bin/python -m pytest packages/eval/tests/test_benchmark_case.py -q
.venv/bin/python -m pytest packages/eval/tests/test_judgment.py -q
.venv/bin/python -m pytest packages/eval/tests/test_pool_manifest.py -q
.venv/bin/python -m pytest packages/eval/tests/test_lineage.py -q
.venv/bin/python -m pytest packages/eval/tests/test_grade.py -q
.venv/bin/python -m pytest packages/eval -q

# Stream B — FTS5 Retrieval
.venv/bin/python -m pytest packages/data-core/tests/test_retrieval_cutoff.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_fts5_retrieval.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_retrieval_result.py -q

# Package canonical suites
.venv/bin/python -m pytest packages/data-core -q
.venv/bin/python -m pytest packages/agents -q
.venv/bin/python -m pytest packages/eval -q
.venv/bin/python -m pytest packages/app -q

# DB SHA
shasum -a 256 data/catalyst_dev_ws4b.db
shasum -a 256 data/catalyst_eval_frozen_v2.db

# Git
git diff --check
```

## 10. Evidence Report Template

```markdown
## B4 Completion Report

### Stream A: Eval Foundation
- Files changed: [list]
- Schemas created: BenchmarkCase, EvidenceJudgment, PoolManifest, Lineage, Grade
- No labels authored: confirmed
- No pool persisted: confirmed

### Stream B: FTS5 Retrieval
- Files changed: [list]
- FTS5 index built: confirmed
- Cutoff-safe: verified via filter-before-score tests

### Canonical Counts
- data-core: X passed (was 758)
- agents: X passed (was 237)
- eval: X passed (was 93)
- app: X passed (was 128)

### DB SHA
- Dev DB before: [B3 SHA]
- Dev DB after: 92731fb7c5c3e989b4fdcbefb9d1d2060974082a6c60f1bbecad6bc42ee846b0 (unchanged during implementation)
- Frozen DB: unchanged

### Unresolved Risks
- [list]

### Confirmation
- [ ] Next package (B5) not started
```

## 11. Git Boundaries

**Stream A (Eval Foundation):**
1. `feat(eval): add BenchmarkCase schema and evidence judgment types`
2. `feat(eval): add grade constants, pool manifest, lineage validator`
3. `feat(eval): wire Eval Foundation public API`

**Stream B (FTS5 Retrieval):**
4. `feat(data-core): add cutoff computation with early-close support`
5. `feat(data-core): add FTS5/BM25 retrieval with filter-before-score`
6. `feat(data-core): add RetrievalResult and retrieval trace`
7. `feat(data-core): add FTS5 index builder and degraded mode`

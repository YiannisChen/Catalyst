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
- A task is not complete merely because its focused unit tests pass. Completion requires the production schema, production entry point, and the task-to-file-to-test evidence matrix in Section 10. Fixtures may not redefine production-owned tables.

### 0.1 Ratified Execution Contract (authoritative over later pseudocode)

This section removes implementation choices that previously remained implicit. If a later snippet conflicts with this section, amend the snippet and follow this section.

**Identity and fixture rules**

- Every manifest ID in tests is a lowercase 64-character SHA-256 hex string. Use named constants `MANIFEST_A = "a" * 64` and `MANIFEST_B = "b" * 64`; strings such as `manifest-v1`, `corpus-v1`, and `index-v1` are invalid.
- Every chunk ID follows the B3 grammar `<document_id>:<profile_version>:<section_key>:<ordinal:04d>`, for example `polygon:article-1:news_v2:body:0001`.
- Retrieval fixtures must open a temporary database through production `init_db()` plus production migrations through v10. They may insert rows with explicit column lists, but may not issue `CREATE TABLE` for `corpus_chunks`, `corpus_manifest`, `corpus_tombstones`, or lexical-index state. Tests assert `PRAGMA user_version = 10` and inspect production columns/indexes before seeding.
- The fixture publishes `MANIFEST_A` through the production corpus manifest path. It may not bypass foreign keys or triggers with `PRAGMA foreign_keys=OFF`.

**Eligibility predicate and filter vocabulary**

The exact pre-score predicate is:

```sql
c.manifest_id = :requested_manifest_id
AND c.status IN ('active', 'pending_embedding', 'embedded', 'metadata_only')
AND c.eligibility = 'eligible'
AND c.available_at <= :cutoff
AND EXISTS (
  SELECT 1 FROM json_each(c.ticker_associations) t WHERE t.value = :ticker
)
```

Optional `source_classes` adds `c.source_class IN (...)`; values are exactly B3's `structured_market_data`, `official_government`, `issuer_disclosure`, `corporate_press_release`, `reported_news`, `analysis_opinion`, and `aggregated_unknown`. Optional `evidence_types` means B3 `chunk_profile_version` values and adds `c.chunk_profile_version IN (...)`; the only B4 values are `news_v2` and `filing_v2`. Empty or unknown optional-list values are invalid (`invalid_filter`), not aliases for no filter. `tombstoned` is the only non-searchable corpus status; the four listed statuses remain lexically searchable because embedding state does not affect lexical eligibility.

`requested_manifest_id`, `ticker`, and a UTC `cutoff` are mandatory. Missing or malformed values raise `RetrievalContractError` with one of `invalid_manifest_id`, `invalid_ticker`, `invalid_cutoff`, `invalid_filter`, or `invalid_depth`. A requested manifest that does not exist raises `manifest_not_found`.

**Migration v10 and persistent lexical index**

Migration v10 creates this metadata table and attempts this ordinary (text-storing) FTS5 virtual table:

```sql
CREATE TABLE lexical_index_state (
  singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
  schema_version TEXT NOT NULL CHECK (schema_version = '1.0.0'),
  corpus_manifest_id TEXT NOT NULL,
  mode_served TEXT NOT NULL CHECK (mode_served IN ('fts5', 'sql_like')),
  fallback_reason TEXT CHECK (fallback_reason IN ('fts5_unavailable', 'fts5_missing', 'fts5_stale')),
  row_count INTEGER NOT NULL CHECK (row_count >= 0),
  built_at TEXT NOT NULL,
  FOREIGN KEY (corpus_manifest_id) REFERENCES corpus_manifest(manifest_id)
);

CREATE VIRTUAL TABLE corpus_chunks_fts USING fts5(
  manifest_id UNINDEXED,
  chunk_id UNINDEXED,
  content_text,
  tokenize = 'unicode61 remove_diacritics 2'
);
```

`run_migrations()` may suppress exactly `sqlite3.OperationalError("no such module: fts5")` for the v10 virtual-table statement only; it must still create `lexical_index_state` and advance to v10. Every other operational error propagates. This is the only FTS5-unavailable path.

`build_fts5_index(conn, requested_manifest_id, *, clock) -> LexicalIndexBuildResult` first validates that the requested corpus manifest is current and rejects `conn.in_transaction` with `LexicalIndexBuildError(code='transaction_active')`. It then owns one `BEGIN IMMEDIATE` transaction: delete old FTS rows, insert all rows satisfying the status/eligibility/manifest portion of the predicate, upsert `lexical_index_state` with `mode_served='fts5'`, and commit. A failure after delete or insert rolls back both rows and metadata, preserving the previous index. If FTS5 is unavailable, it writes a `sql_like/fts5_unavailable` state with the eligible row count and does not fabricate a virtual table. No code path commits a transaction opened by the caller.

**Query compiler, ranking, and fallback**

- Normalize the query with Unicode NFC and `casefold()`, then extract terms with Python `re.findall(r"[^\W_]+", normalized, re.UNICODE)`. Preserve order while removing duplicate terms.
- An empty term list returns an empty result set with `fallback_reason='empty_query'`; it does not execute `MATCH` or `LIKE`.
- Compile FTS MATCH as the quoted terms joined by ` AND `. Callers cannot supply raw FTS operators or column selectors.
- FTS5 ordering is `bm25(corpus_chunks_fts) ASC, chunk_id ASC`. `lexical_raw_score` is the exact SQLite float and `lexical_rank` is one-based after the eligibility predicate.
- SQL fallback first executes the identical eligibility predicate. It casefolds content in Python, requires every query term to occur, computes `fallback_term_frequency = sum(content.count(term) for term in terms)`, and orders by `fallback_term_frequency DESC, chunk_id ASC`. In fallback results `lexical_raw_score=None`; `lexical_rank` remains the one-based served rank.
- `1 <= top_k <= candidate_depth <= 100`; defaults are `top_k=8`, `candidate_depth=20`. `candidates` contains at most `candidate_depth` rows and `results == candidates[:top_k]`.
- Retrieval never serves stale FTS rows. Missing FTS table, unavailable FTS5, or `lexical_index_state.corpus_manifest_id != requested_manifest_id` serves the deterministic SQL fallback with reason `fts5_missing`, `fts5_unavailable`, or `fts5_stale` respectively.

The public signature is exact:

```python
retrieve_lexical(
    conn,
    query: str,
    *,
    ticker: str,
    cutoff: str,
    requested_manifest_id: str,
    top_k: int = 8,
    candidate_depth: int = 20,
    source_classes: tuple[str, ...] | None = None,
    evidence_types: tuple[str, ...] | None = None,
    include_trace: bool = False,
) -> RetrievalResultSet
```

`mode_requested` is always `lexical`; `mode_served` is `fts5` or `sql_like`. `is_degraded` is exactly `mode_served != 'fts5'`.

**Result and trace schema**

`RetrievalResult` requires: `chunk_id`, `document_id`, `available_at`, `cutoff`, `filters_applied`, `source_class`, nullable `lexical_raw_score`, positive `lexical_rank`, nullable dense/fusion/reranker scores and ranks, `corpus_manifest_id`, nullable `index_manifest_id`, `mode_requested`, `mode_served`, `is_degraded`, nullable `fallback_reason`, and non-negative `timing_ms`. B4 always sets `index_manifest_id=None`; B6 owns `IndexManifest`. `fallback_reason` is one of `empty_query`, `fts5_unavailable`, `fts5_missing`, `fts5_stale`, or null. Every returned row uses retrieval total elapsed time for `timing_ms`.

`filters_applied` is a frozen `RetrievalFilters` model with exactly `ticker`, `requested_manifest_id`, `cutoff`, `statuses` (the four searchable statuses in fixed contract order), `eligibility='eligible'`, `source_classes` (sorted tuple or null), and `evidence_types` (sorted tuple or null). It is identical across rows in one result set.

`RetrievalResultSet` requires `candidates`, `results`, `candidate_count`, `mode_requested`, `mode_served`, `is_degraded`, `fallback_reason`, and optional trace. `candidate_count == len(candidates)`.

`RetrievalTrace` has exact counts: `manifest_row_count` (all rows for the requested manifest), `eligible_row_count` (rows after every pre-score predicate), `matched_row_count` (eligible rows matching all query terms), `candidate_count`, and `final_count`, plus non-negative `filter_ms`, `score_ms`, and `total_ms`. It records requested/served mode and fallback reason. Counts must satisfy `manifest_row_count >= eligible_row_count >= matched_row_count >= candidate_count >= final_count`.

**Cutoff policy**

- `ExchangeCutoffPolicy` supports US equities only and uses the existing versioned 2024-2027 holiday calendar with `America/New_York` via `zoneinfo`; no OHLCV-derived or +/-3-day window is allowed.
- Regular close is 16:00 local. The complete early-close set for the supported range is `2024-07-03`, `2024-11-29`, `2024-12-24`, `2025-07-03`, `2025-11-28`, `2025-12-24`, `2026-07-02`, `2026-11-27`, `2026-12-24`, `2027-07-02`, `2027-11-26`, and `2027-12-23`, all at 13:00 local.
- `close_to_close` rejects a weekend, full holiday, or out-of-coverage date with typed codes `not_a_trading_session` or `calendar_out_of_range`.
- `intraday` requires a parseable UTC `as_of` ending in `Z`, requires its local exchange date to equal `session_date`, and rejects an instant after that session's official close with `invalid_as_of`. Returned timestamps are canonical second-resolution UTC (`YYYY-MM-DDTHH:MM:SSZ`).

**Eval schemas**

- All new Pydantic models use `ConfigDict(extra='forbid', frozen=True)` and UTC-aware datetimes. Semantic versions match `^[0-9]+\.[0-9]+\.[0-9]+$`.
- `EvidenceJudgment.grade` is exactly `Literal[0, 1, 2]`; rationale and annotator are stripped non-empty strings. Absence from `evidence_judgments_by_chunk_id` means UNJUDGED; no nullable/default grade exists.
- `PoolManifest.index_manifest_id` is a required key but nullable in B4. It must be non-null when adapted from a B6 union artifact. `corpus_manifest_id` and any non-null index/source artifact hashes are lowercase 64-character hex. `chunk_inventory` is sorted, unique, and uses B3 chunk IDs.
- `validate_lineage()` returns a tuple of typed `{code, field, message}` errors sorted by `(code, field)`. Exact codes are `missing_action_timestamp`, `non_utc_timestamp`, `annotation_predates_session`, `batch_timestamp`, `invalid_action_order`, and `invalid_adjudication_state`. Required strict order is annotation start < annotation complete; when second pass exists, annotation complete < second-pass start < second-pass complete. Equal timestamps trigger `batch_timestamp`.
- B4 may create schema fixtures containing synthetic judgments, but may not add any non-test benchmark case, label, judgment, or pool artifact to the repository.

The exact model shapes are:

```text
CauseLabel:
  descriptor: stripped 1..20 words
  direction: Literal['positive', 'negative', 'neutral', 'mixed']

EvidenceJudgment:
  chunk_id: B3 chunk ID
  grade: Literal[0, 1, 2]
  rationale: stripped non-empty single line
  annotator: stripped non-empty
  judged_at: UTC-aware datetime

PoolArm:
  arm: Literal['lexical', 'dense', 'hybrid', 'reranked']
  version: semantic version
  top_k: int in [1, 100]

PoolManifest:
  schema_version: semantic version
  pool_id: stripped non-empty
  case_id: stripped non-empty
  arms: non-empty tuple[PoolArm, ...], unique by arm
  chunk_inventory: sorted unique tuple[B3 chunk ID, ...]
  corpus_manifest_id: lowercase SHA-256
  index_manifest_id: lowercase SHA-256 | None
  source_artifact_id: lowercase SHA-256
  created_at: UTC-aware datetime

LineageRecord:
  candidate_generation_source: Literal['manual', 'retrieval_arm_union']
  model_assisted_fields: sorted unique tuple[str, ...]
  human_confirmed_fields: sorted unique tuple[str, ...]
  timestamps: ActionTimestamps
  adjudication_status: Literal['pending', 'in_review', 'resolved']

DatasetVersion:
  version: semantic version
  parent_version: semantic version | None
  change: Literal['metadata_only', 'case_added', 'case_removed',
                  'judgments_modified', 'cutoff_modified', 'schema_modified']
  created_at: UTC-aware datetime

BenchmarkCase:
  case_id: stripped non-empty
  schema_version: semantic version
  dataset_version: semantic version
  split: Literal['core_answerable', 'core_abstain', 'retrieval_only', 'adversarial']
  parent_case_id: str | None (required only for adversarial; forbidden otherwise)
  ticker: uppercase `[A-Z][A-Z0-9.-]{0,9}`
  session_date: date
  cutoff_ts: UTC-aware datetime
  observable_facts: dict[str, bool | int | float | str | None]
  answerability: Literal['answerable', 'abstain', 'retrieval_only']
  expected_abstention_reason_class: str | None (required iff answerability='abstain')
  acceptable_cause_labels: sorted unique tuple[CauseLabel, ...]
  evidence_judgments_by_chunk_id: dict[B3 chunk ID, EvidenceJudgment]
  pool_manifest: PoolManifest | None
  unjudged_handling: Literal['chunks_outside_pool_explicitly_unjudged']
  lineage: LineageRecord | None
  annotator_notes: str
```

Every judgment mapping key must equal its value's `chunk_id`; every judged chunk must appear in a non-null pool manifest inventory. `core_answerable` requires `answerability='answerable'`; `core_abstain` requires `answerability='abstain'`; `retrieval_only` requires `answerability='retrieval_only'`. An adversarial case inherits answerability from its parent and therefore only validates the required parent ID locally.

Collection validators reject rather than silently reorder non-canonical input. Pool arms use fixed order `lexical`, `dense`, `hybrid`, `reranked`; chunk inventories use `chunk_id ASC`; cause labels use `(descriptor, direction) ASC`; string field tuples use lexical order. Judgment rationale is 1..500 characters and contains no CR/LF.

`pool_id` is not an opaque label. It is lowercase SHA-256 of canonical UTF-8 JSON (sorted keys, compact separators, arrays already in canonical order) containing exactly `schema_version`, `case_id`, `arms`, `chunk_inventory`, `corpus_manifest_id`, `index_manifest_id`, and `source_artifact_id`. `pool_id` and `created_at` are excluded from the hash input; model validation recomputes and rejects a mismatch.

`ActionTimestamps.annotation_started` and `annotation_completed` are always required. For `pending`, both second-pass timestamps are null; for `in_review`, `second_pass_started` is required and `second_pass_completed` is null; for `resolved`, both are required. Annotation start must be at or after `session_dateT00:00:00Z`. Any equal action timestamps emit `batch_timestamp`; otherwise violations of the strict order emit `invalid_action_order`.

`bump_version(version, change)` accepts only: `metadata_only` -> patch +1; `case_added` or `case_removed` -> minor +1 and patch=0; `judgments_modified`, `cutoff_modified`, or `schema_modified` -> major +1 and minor=patch=0. Unknown change values raise `ValueError`.


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

- BenchmarkCase schema using the exact Section 0.1 fields, including `evidence_judgments_by_chunk_id`, pool identity, unjudged handling, and lineage
- Evidence judgment schema (chunk_id, grade 2/1/0, one-line rationale)
- Pool manifest (arm identities, versions, chunk inventory)
- Lineage validator (batch-stamp detection, timestamps before session dates, real per-action timestamps required)
- Dataset versioning (dataset_version in BenchmarkCase)
- Grade tooling (grade 2/1/0 constants, helpers)
- No labels authored in B4
- No pool persisted in B4

### Scope — Stream B

- FTS5 full-text index on `corpus_chunks.content_text`
- `filter_before_score()`: exact Section 0.1 status/eligibility/manifest/cutoff/ticker/evidence/source predicate
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

- Ordinary FTS5 virtual table with `manifest_id`, `chunk_id`, and `content_text`, using the exact Section 0.1 DDL
- `lexical_index_state` singleton metadata table with corpus identity, served mode, fallback reason, row count, and build timestamp

### Owned Schemas (Eval Foundation)

- `BenchmarkCase` (pydantic model)
- `EvidenceJudgment` (pydantic model)
- `PoolManifest` (pydantic model): `schema_version`, `pool_id`, `case_id`, `arms`,
  `chunk_inventory`, `corpus_manifest_id`, nullable `index_manifest_id`, `source_artifact_id`, `created_at`.
  Identity keys are required lineage; B4 stores `index_manifest_id=null` because B6 owns IndexManifest.
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

Before Stream A Task 1, create `packages/eval/tests/benchmark_fixtures.py` with `min_benchmark_fields()`, `POOL_ARMS`, `POOL_CHUNK_IDS`, the literal oracle `POOL_ID = "b51ba542ff008a2de777155b8b6f965023729695bab573bc8c83281c286133da"`, valid lineage records, and complete pool identities. A dedicated test calls production `compute_pool_id()` with these fixture values and asserts that literal; it also changes one chunk and asserts a different ID. Before Stream B Task 7, create the listed `retrieval_fixtures.py` through production `init_db()` and migrations with exact legal/illegal candidate inventories. Its `insert_corpus_chunk()` helper must use an explicit column list covering all required v9 fields, including `manifest_id`; positional `INSERT ... VALUES` and fixture-owned production DDL are prohibited. No later helper name may be left undefined.

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
        evidence_judgments_by_chunk_id={},
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
        chunk_id="polygon:article-1:news_v2:body:0001",
        grade=2,
        rationale="Directly describes the product recall event.",
        annotator="fixture",
        judged_at="2026-07-22T00:00:00Z",
    )
    case = BenchmarkCase(
        evidence_judgments_by_chunk_id={judgment.chunk_id: judgment},
        **min_benchmark_fields()
    )
    assert len(case.evidence_judgments_by_chunk_id) == 1
    assert case.evidence_judgments_by_chunk_id[judgment.chunk_id].grade == 2


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
    if grade not in (0, 1, 2):
        raise ValueError("invalid_grade")
    return grade in (1, 2)
```

Also define `JudgmentStatus = Literal['judged', 'unjudged']`. `judgment_for(chunk_id, judgments_by_chunk_id)` returns `(unjudged, None)` when absent and `(judged, grade)` when present; it never synthesizes grade 0.

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
        pool_id=POOL_ID,
        case_id="B001",
        arms=POOL_ARMS,
        chunk_inventory=POOL_CHUNK_IDS,
        corpus_manifest_id="a" * 64,
        index_manifest_id=None,
        source_artifact_id="c" * 64,
        created_at="2026-01-01T00:00:00Z",
    )
    assert len(manifest.arms) == 2
    assert manifest.corpus_manifest_id == "a" * 64
    assert manifest.index_manifest_id is None
    assert manifest.source_artifact_id == "c" * 64
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
        "human_confirmed_fields": ["evidence_judgments_by_chunk_id"],
        "timestamps": {
            "annotation_started": "2026-06-01T00:00:00Z",
            "annotation_completed": "2026-06-01T00:00:00Z",  # batch stamp
        },
        "adjudication_status": "pending"
    }
    errors = validate_lineage(lineage)
    assert [error.code for error in errors] == ["batch_timestamp"]


def test_lineage_rejects_predating_timestamps():
    """Annotation timestamp before session date → failure."""
    from catalyst_eval.benchmark.lineage import validate_lineage

    session_date = "2026-01-15"
    lineage = {
        "timestamps": {"annotation_started": "2025-12-01T00:00:00Z"},
    }
    errors = validate_lineage(lineage, session_date=session_date)
    assert [error.code for error in errors] == ["annotation_predates_session"]


def test_lineage_accepts_valid():
    """Valid lineage with real timestamps passes."""
    from catalyst_eval.benchmark.lineage import validate_lineage

    lineage = {
        "candidate_generation_source": "retrieval_arm_union",
        "model_assisted_fields": ["initial_grade_suggestions"],
        "human_confirmed_fields": ["evidence_judgments_by_chunk_id", "acceptable_cause_labels"],
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
    assert v2 == "2.0.0"
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

Before implementing retrieval, add a focused v10 migration subtask using the exact Section 0.1 DDL. Test both a normal FTS5 connection and a connection shim that raises exactly `no such module: fts5` for `CREATE VIRTUAL TABLE`. A non-FTS failure must leave `user_version=9` with no half-created v10 objects; the explicit unavailable case must reach v10 with metadata available and no FTS table. Do not migrate the canonical Dev DB.

**Step 1: Write cutoff test**

```python
# packages/data-core/tests/test_retrieval_cutoff.py

def test_cutoff_is_session_close():
    """Close-to-close cutoff uses official exchange close, including early-close."""
    from catalyst_data.retrieval.cutoff import compute_cutoff

    # Regular session
    cutoff = compute_cutoff(ticker="AAPL", session_date="2026-01-15",
                            mode="close_to_close")
    assert cutoff == "2026-01-15T21:00:00Z"

    # Early-close session (e.g., day after Thanksgiving)
    cutoff_early = compute_cutoff(ticker="AAPL", session_date="2025-11-28",
                                  mode="close_to_close")
    assert cutoff_early == "2025-11-28T18:00:00Z"


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
- Add `session_close_utc(session_date)` to `trading_calendar.py`: use the complete versioned regular/early-close policy in Section 0.1 and convert with `zoneinfo.ZoneInfo`
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
        db, chunk_id="test:post:news_v2:body:0001", document_id="test:post",
        content_text="relevant text about AAPL earnings",
        available_at="2026-01-16T09:00:00Z", eligibility="eligible",
        ticker_associations=["AAPL"], manifest_id=MANIFEST_A, status="active",
    )

    result = retrieve_lexical(db, query="AAPL earnings", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A, top_k=8)
    # Post-cutoff chunk excluded
    chunk_ids = {r.chunk_id for r in result.results}
    assert "test:post:news_v2:body:0001" not in chunk_ids


def test_ticker_filter():
    """Ticker filter restricts to chunks associated with the ticker."""
    db = _fresh_db_with_chunks()
    insert_corpus_chunk(db, chunk_id="test:aap:news_v2:body:0001", document_id="test:aap",
                        content_text="AAPL news", available_at="2026-01-01T09:00:00Z",
                        eligibility="eligible", ticker_associations=["AAPL"],
                        manifest_id=MANIFEST_A, status="active")
    insert_corpus_chunk(db, chunk_id="test:msft:news_v2:body:0001", document_id="test:msft",
                        content_text="MSFT news", available_at="2026-01-01T09:00:00Z",
                        eligibility="eligible", ticker_associations=["MSFT"],
                        manifest_id=MANIFEST_A, status="active")

    result = retrieve_lexical(db, query="news", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A, top_k=8)
    chunk_ids = {r.chunk_id for r in result.results}
    assert "test:aap:news_v2:body:0001" in chunk_ids
    assert "test:msft:news_v2:body:0001" not in chunk_ids


def test_top_k_after_filter():
    """TOP-K is applied AFTER filtering, not before."""
    # Post-cutoff documents cannot displace legal candidates
    db = _fresh_db_with_chunks()
    # Add 10 pre-cutoff chunks + 1 high-relevance post-cutoff
    for i in range(10):
        insert_corpus_chunk(
            db, chunk_id=f"test:pre{i}:news_v2:body:0001", document_id=f"test:pre{i}",
            content_text=f"AAPL earnings surprise revenue beat report {i}",
            available_at="2026-01-01T09:00:00Z",
            eligibility="eligible", ticker_associations=["AAPL"],
            manifest_id=MANIFEST_A, status="active",
        )
    # Post-cutoff high-relevance would rank #1 if not filtered
    insert_corpus_chunk(
        db, chunk_id="test:post:news_v2:body:0001", document_id="test:post",
        content_text="AAPL earnings surprise revenue beat",
        available_at="2026-01-16T09:00:00Z", eligibility="eligible",
        ticker_associations=["AAPL"], manifest_id=MANIFEST_A, status="active",
    )

    result = retrieve_lexical(db, query="AAPL earnings surprise revenue beat",
                              ticker="AAPL", cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A, top_k=3)
    chunk_ids = {r.chunk_id for r in result.results}
    assert "test:post:news_v2:body:0001" not in chunk_ids
    assert len(chunk_ids) == 3
    assert chunk_ids <= {
        f"test:pre{i}:news_v2:body:0001" for i in range(10)
    }
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_fts5_retrieval.py -k "filter" -q
```

Expected: FAIL.

**Step 3: Implement filter layer**

Implement the exact eligibility CTE from Section 0.1 and join only eligible rows to `corpus_chunks_fts` before `MATCH`, `bm25()`, ordering, and limiting. Add one strongest-match fixture for each independently illegal dimension: wrong manifest, tombstoned status, ineligible eligibility, post-cutoff availability, wrong ticker, excluded source class, and excluded profile/evidence type.

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
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A, top_k=8)

    assert len(result.results) >= 2
    assert result.results[0].lexical_raw_score <= result.results[1].lexical_raw_score


def test_lexical_rank_is_one_based():
    """lexical_rank starts at 1, not 0."""
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    db = _fresh_db_with_chunks()
    result = retrieve_lexical(db, query="test", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A, top_k=8)

    assert result.results
    assert result.results[0].lexical_rank == 1


def test_stable_ties():
    """Tied bm25() scores are broken by chunk_id ASC."""
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    db = _fresh_db_with_chunks()
    # Two chunks with identical content → tied bm25
    result = retrieve_lexical(db, query="identical", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A, top_k=8)

    assert len(result.results) == 2
    assert result.results[0].lexical_raw_score == result.results[1].lexical_raw_score
    assert [row.chunk_id for row in result.results] == sorted(
        row.chunk_id for row in result.results
    )


def test_default_candidate_depth_20_display_8():
    """Default lexical candidate depth is 20, final display is 8."""
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    db = _fresh_db_with_chunks(n_chunks=30)
    result = retrieve_lexical(db, query="test", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A)

    assert result.candidate_count == 20
    assert len(result.results) == 8


def test_retrieval_result_all_fields():
    """RetrievalResult contains all contract fields."""
    from catalyst_data.retrieval.result import RetrievalResult

    rr = RetrievalResult(
        chunk_id="test:1:news_v2:body:0001",
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
        corpus_manifest_id="a" * 64,
        index_manifest_id=None,
        mode_requested="lexical",
        mode_served="fts5",
        is_degraded=False,
        fallback_reason=None,
        timing_ms=12.3,
    )
    assert rr.chunk_id == "test:1:news_v2:body:0001"
    assert rr.lexical_raw_score == -3.5
    assert rr.mode_requested == "lexical"
    assert rr.mode_served == "fts5"
    assert not rr.is_degraded
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_fts5_retrieval.py -q
```

Expected: FAIL.

**Step 3: Implement**

- `catalyst_data/retrieval/fts5.py`: implement the exact keyword-only `retrieve_lexical(...) -> RetrievalResultSet` signature in Section 0.1
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
                              requested_manifest_id=MANIFEST_A,
                              include_trace=True)

    assert result.trace.mode_served == "fts5"
    assert result.trace.manifest_row_count >= result.trace.eligible_row_count
    assert result.trace.eligible_row_count >= result.trace.matched_row_count
    assert result.trace.candidate_count == len(result.candidates)
    assert result.trace.final_count == len(result.results)
    assert result.trace.total_ms >= 0
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
    build_fts5_index(db, MANIFEST_A, clock=FIXED_CLOCK)

    # Verify FTS5 virtual table exists
    table = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='corpus_chunks_fts'"
    ).fetchone()
    assert table is not None
    assert "fts5" in table[0].lower()


def test_degraded_mode_no_fts5():
    """When FTS5 unavailable, falls back to SQL LIKE with explicit degraded flag."""
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    db = _fresh_v10_db_with_missing_fts_table()
    result = retrieve_lexical(db, query="earnings", ticker="AAPL",
                              cutoff="2026-01-15T21:00:00Z",
                              requested_manifest_id=MANIFEST_A)

    assert result.is_degraded
    assert result.mode_served == "sql_like"
    assert result.fallback_reason == "fts5_missing"
    assert result.results
```

Add separate tests for: unavailable FTS5 during v10, missing FTS table, stale index manifest, atomic rebuild failure after delete, exact SQL fallback term-frequency ordering, empty query, escaped FTS operators, and invalid depth/filter contracts.

**Step 2: Implement `catalyst_data/retrieval/fts5_builder.py` and `catalyst_data/retrieval/degraded.py`**

Implement only the transaction, capability, fallback, and ranking algorithms in Section 0.1. In particular, do not treat any missing/stale index as current, do not expose raw MATCH syntax, and do not commit a caller-owned transaction.

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
2. **+/-3-day symmetric window** — behavioral tests reject non-session dates and prove exact regular/early session closes; no source-code grep is used as proof.
3. **TOP-K before filter** — seven strongest-illegal-candidate fixtures prove every predicate dimension before ranking and limiting.
4. **Silent unjudged-as-zero** — grade tooling explicitly flags unjudged, never defaults to 0.
5. **Batch-stamped lineage accepted** — `test_lineage_rejects_batch_stamps` catches uniform timestamps.
6. **FTS5 score compared with dense score** — never; lexical_raw_score is engine-specific, rank is the comparable field.
7. **Eval Foundation creates pools before labels** — verify no judgment data persisted in B4; schema only.
8. **Manifest/status/source filters omitted** — independent fixtures must prove that a wrong-manifest, tombstoned, inactive, or excluded-source chunk never enters the candidate set even when it is the strongest lexical match.
9. **Early close treated as 16:00 ET** — verify 2025-11-28 closes at 18:00 UTC and a 19:00 UTC article is excluded.
10. **Shadow schema fixture** — `retrieval_fixtures.py` must contain no production-table `CREATE TABLE`; schema assertions prove it uses migration v10.
11. **Stale lexical index served** — requested manifest B with index state A must use `sql_like/fts5_stale`, never return FTS rows from A.
12. **Fallback drift** — fixed literal inventories assert exact all-term matching, term-frequency ordering, `chunk_id` tie break, null raw score, and one-based rank.
13. **Conditional test theater** — `rg "if (len\(|result\.results)" packages/data-core/tests/test_fts5_retrieval.py` must return zero; fixtures guarantee asserted cardinality.
14. **Missing production entry point** — an integration test publishes a B3 corpus manifest, invokes `build_fts5_index`, then calls the public `retrieve_lexical` API and verifies all identities/counts.

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

### Task Evidence Matrix
| Task | Production files | RED test and observed failure | GREEN tests | Contract assertions |
|---|---|---|---|---|
| A1-B12 | [exact paths] | [test name + failure summary] | [test names] | [literal oracle/invariant] |

Every task row is mandatory. A task without a production file, observed RED failure, and named GREEN tests remains OPEN.

### Stream A: Eval Foundation
- Exact models and public exports: [list]
- No non-test labels or pools: [git path audit]

### Stream B: FTS5 Retrieval
- v10 production schema objects: [PRAGMA/sqlite_master evidence]
- Production integration entry point: [function + integration test]
- Normal/unavailable/missing/stale/rollback modes: [test names]
- Seven independent pre-score predicate oracles: [test names]

### Canonical Counts
- data-core: X passed (B3 baseline 893 passed, 1 skipped, 1 xfailed)
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
- [ ] All Task Evidence Matrix rows complete
- [ ] No fixture-owned production DDL
- [ ] No conditional assertions or self-comparisons
- [ ] Production B3 -> B4 integration test passes
- [ ] Canonical DB hashes unchanged
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

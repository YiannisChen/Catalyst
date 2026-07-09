# WS4B Step 3F.2 Remediation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Do not run live ingestion, mutate the dev DB, or enter Step 4 until the user explicitly approves the relevant chunk.

**Goal:** Remediate the incomplete WS4B Step 3F.2 live-ingestion run by fixing FRED point-in-time integrity, hardening calendar/freshness edge cases, completing Polygon coverage through the latest closed US trading day, and producing an auditable dev DB that is safe to use before Step 4.

**Architecture:** Treat the current dev DB as contaminated for Plane-2 macro only, while preserving the frozen eval DB as immutable. Make code/test fixes offline first, then replace only dev DB FRED Bronze/Silver/checkpoint state through an explicitly approved controlled live run using scoped PIT windows. Resume Polygon via checkpoints with rate-limit discipline, then run a final read-only audit covering source coverage, provenance, dedup, and Plane-2 separation.

**Tech Stack:** Python 3.12, sqlite3, pytest, stdlib calendar fallback, existing Catalyst data-core connectors/pipelines. No new dependency unless an accepted exchange-calendar package already exists in the repo.

---

## 0. Current Evidence From Read-Only Audit

These were gathered with read-only shell/SQLite inspection on `ws4b/article-level-data`.

- Git status contains unrelated dirty files in `packages/app` and `packages/agents`. Do not touch them.
- Data-core has unstaged changes in:
  - `packages/data-core/catalyst_data/update_pipeline.py`
  - `packages/data-core/catalyst_data/freshness.py`
- Frozen DB SHA is unchanged:
  - `0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf  data/catalyst_eval_frozen_v2.db`
- Dev DB headline:
  - `articles`: Polygon `13,920`, Finnhub `22,941`
  - `filings`: `7`
  - `filing_documents`: `9`
  - `macro_observations`: `98,349`, `12` distinct series, only `2` distinct `released_at` values
  - `macro_observations WHERE released_at > observation_date`: `98,349`
  - `index_state` macro rows: `0`
  - zero-canonical dedup groups: `0` for articles and filings, but current article/article_tickers `dedup_group_id` values are NULL and must be audited as a separate materialization gate.

Expected current bad state:

- Macro rows are not safe for eval because old observation dates such as `1962-01-01` have `released_at` equal to `2026-07-04` or `2026-07-02`.
- Polygon checkpoints extend to `2026-07-02`, but actual Polygon `article_tickers.reference_date` max remains around `2026-05-01`, with `4,074` Polygon association rows having blank/NULL reference dates. This makes checkpoint/data reconciliation mandatory.
- Finnhub ingestion breadth exists, but dedup materialization is not currently proven by the live DB.

Expected fixed state:

- `macro_observations.released_at` comes from per-observation realtime metadata, not fetch date.
- Historical macro observations only have 2026 release dates if the provider metadata truly says they were first released in 2026.
- `index_state` still has zero macro rows.
- Polygon data coverage is current through the latest closed US trading day, or any remaining gaps are explicitly provider-quota/provider-delay exceptions with failed/skipped cells recorded.
- Dedup materialization is populated for eligible article rows and every non-NULL dedup group has exactly one canonical row.

## A. Current-State Verification Checklist

Run these commands only in read-only mode. They do not print environment variables or secrets.

```bash
pwd
git branch --show-current
git status --short --branch
git diff -- packages/data-core/catalyst_data/update_pipeline.py packages/data-core/catalyst_data/freshness.py
shasum -a 256 data/catalyst_eval_frozen_v2.db
```

Use `sqlite3 -readonly` and a timeout to avoid accidental writes:

```bash
sqlite3 -readonly data/catalyst_dev_ws4b.db <<'SQL'
.timeout 5000
.headers on
.mode column

SELECT source_type, provider, COUNT(*) AS rows
FROM articles
GROUP BY source_type, provider
ORDER BY source_type, provider;

SELECT COUNT(*) AS filings FROM filings;
SELECT COUNT(*) AS filing_documents FROM filing_documents;

SELECT COUNT(*) AS rows,
       COUNT(DISTINCT series_id) AS distinct_series,
       COUNT(DISTINCT released_at) AS distinct_released_at
FROM macro_observations;

SELECT released_at, COUNT(*) AS rows
FROM macro_observations
GROUP BY released_at
ORDER BY released_at;

SELECT COUNT(*) AS rows_released_after_observation
FROM macro_observations
WHERE released_at > observation_date;

SELECT series_id, observation_date, released_at, value
FROM macro_observations
WHERE released_at > observation_date
ORDER BY observation_date
LIMIT 10;

SELECT COUNT(*) AS macro_index_state_rows
FROM index_state
WHERE source_kind LIKE '%macro%'
   OR corpus_item_id LIKE 'macro:%'
   OR corpus_item_id IN (
       SELECT series_id || ':' || observation_date FROM macro_observations
   );

SELECT COUNT(*) AS article_zero_canonical_groups
FROM (
    SELECT dedup_group_id
    FROM articles
    WHERE dedup_group_id IS NOT NULL
    GROUP BY dedup_group_id
    HAVING SUM(CASE WHEN is_canonical = 1 THEN 1 ELSE 0 END) = 0
);

SELECT COUNT(*) AS filing_zero_canonical_groups
FROM (
    SELECT dedup_group_id
    FROM filings
    WHERE dedup_group_id IS NOT NULL
    GROUP BY dedup_group_id
    HAVING SUM(CASE WHEN is_canonical = 1 THEN 1 ELSE 0 END) = 0
);

SELECT a.source_type, COUNT(*) AS article_rows_with_null_dedup
FROM articles a
WHERE a.source_type IN ('polygon_news', 'finnhub_company_news')
  AND a.dedup_group_id IS NULL
GROUP BY a.source_type;

SELECT a.source_type, COUNT(*) AS association_rows_with_null_dedup
FROM article_tickers at
JOIN articles a ON a.article_id = at.article_id
WHERE a.source_type IN ('polygon_news', 'finnhub_company_news')
  AND at.dedup_group_id IS NULL
GROUP BY a.source_type;

SELECT at.ticker,
       MIN(NULLIF(at.reference_date, '')) AS min_reference_date,
       MAX(NULLIF(at.reference_date, '')) AS max_reference_date,
       COUNT(*) AS association_rows,
       COUNT(DISTINCT at.article_id) AS distinct_articles
FROM article_tickers at
JOIN articles a ON a.article_id = at.article_id
WHERE a.source_type = 'polygon_news'
GROUP BY at.ticker
ORDER BY at.ticker;

SELECT COUNT(*) AS polygon_blank_reference_dates
FROM article_tickers at
JOIN articles a ON a.article_id = at.article_id
WHERE a.source_type = 'polygon_news'
  AND (at.reference_date IS NULL OR at.reference_date = '');

SELECT source_type, status, COUNT(*) AS rows, MIN(date) AS min_date, MAX(date) AS max_date
FROM source_checkpoints
GROUP BY source_type, status
ORDER BY source_type, status;

SELECT ticker, status, COUNT(*) AS rows, MIN(date) AS min_date, MAX(date) AS max_date
FROM source_checkpoints
WHERE source_type = 'polygon_news'
GROUP BY ticker, status
ORDER BY ticker, status;
SQL
```

SEC/Finnhub validation queries:

```bash
sqlite3 -readonly data/catalyst_dev_ws4b.db <<'SQL'
.timeout 5000
.headers on
.mode column

SELECT COALESCE(NULLIF(publisher_name, ''), '(blank)') AS publisher, COUNT(*) AS rows
FROM articles
WHERE source_type = 'finnhub_company_news'
GROUP BY publisher
ORDER BY rows DESC
LIMIT 20;

SELECT form_type, COUNT(*) AS rows, MIN(filed_at) AS min_filed, MAX(filed_at) AS max_filed
FROM filings
GROUP BY form_type
ORDER BY form_type;

SELECT document_type, extraction_status, COUNT(*) AS rows
FROM filing_documents
GROUP BY document_type, extraction_status
ORDER BY document_type, extraction_status;

SELECT source_type, COUNT(*) AS raw_assets, MIN(reference_date), MAX(reference_date)
FROM raw_assets
GROUP BY source_type
ORDER BY source_type;
SQL
```

Optional read-only SEC Bronze HTML spot-check without printing sensitive data:

```bash
PYTHONPATH=packages/data-core python - <<'PY'
import sqlite3, zlib
conn = sqlite3.connect("data/catalyst_dev_ws4b.db")
conn.execute("PRAGMA query_only = ON")
rows = conn.execute("""
    SELECT asset_id, content_raw
    FROM raw_assets
    WHERE source_type IN ('sec_primary_doc', 'sec_submissions')
    LIMIT 20
""").fetchall()
for asset_id, blob in rows:
    text = zlib.decompress(blob).decode("utf-8", errors="ignore").lower()
    print(asset_id[:12], "<html" in text, len(text))
conn.close()
PY
```

## B. FRED PIT Remediation Design

### Problem

The live dev DB macro table used fetch-date/latest-revised semantics. Every macro row has `released_at > observation_date`, and only two release dates appear across 98,349 rows. These rows must not be used for eval.

### Preferred Strategy

Use a scoped point-in-time eval window instead of trying to full-history fetch every vintage:

- Default window: `2023-01-01` through latest closed US trading day.
- Parameterize `--from-date` and `--to-date`.
- Fetch only the curated FRED series needed by the current attribution/eval work.
- Use `output_type=4` where it returns first-release values with per-observation realtime metadata under the FRED cap.
- If a series/window exceeds the provider cap, split by observation window and/or realtime window, not by blind full-history vintages.

### Option Evaluation

1. `output_type=4` over narrowed observation windows.
   - Preferred first attempt.
   - Request each series with `observation_start`, `observation_end`, `output_type=4`, `realtime_start=1776-07-04`, `realtime_end=9999-12-31`.
   - Keep the scoped default window small enough to stay under the 2,000-vintage/full-history risk.
   - Validate response counts before writing.

2. ALFRED/realtime-window chunking.
   - Fallback if a series still hits cap or if FRED output semantics are insufficient.
   - Chunk `realtime_start/realtime_end` and merge rows by `(series_id, observation_date)` selecting earliest valid realtime row.
   - More complex and higher risk; only use where option 1 fails.

3. Series-specific observation windows.
   - Use cadence-specific windows if needed:
     - daily series: `2023-01-01` through latest closed trading day in yearly or quarterly chunks
     - monthly series: whole scoped window
     - quarterly series: whole scoped window
   - Avoid fetching pre-2023 history unless a downstream eval case explicitly requires it.

### Required Invariants

- `released_at` must be read from observation/realtime metadata for that observation, not from the top-level response field, fetch date, local clock, or raw asset `reference_date`.
- Historical observation rows should not have 2026 fetch-date release timestamps unless the provider observation metadata truly says first release happened in 2026.
- `released_at <= fetched_at::date` for every row after remediation.
- PIT visibility query must be valid:

```sql
SELECT *
FROM macro_observations
WHERE released_at <= :as_of_date
  AND observation_date <= :as_of_date;
```

- `T10Y2Y.released_at = max(DGS10.released_at, DGS2.released_at)` for each derived observation date.
- Macro remains Plane-2:
  - no macro `clean_assets`
  - no macro `index_state`
  - no macro LanceDB embedding path

### Existing Invalid Rows

When R3 is approved, clear and replace only dev DB FRED state:

- `macro_observations`
- `raw_assets WHERE source_type='fred_macro'`
- `source_checkpoints WHERE source_type='fred_macro'`
- any FRED-only audit artifact produced by the failed run, after explicit identification

Never mutate:

- `data/catalyst_eval_frozen_v2.db`
- LanceDB/index artifacts
- Polygon/Finnhub/SEC rows except through their approved source-specific chunks

Rollback before mutation:

```bash
cp data/catalyst_dev_ws4b.db data/catalyst_dev_ws4b.before-fred-pit-$(date -u +%Y%m%dT%H%M%SZ).db
```

### Tests

Add or extend tests under `packages/data-core/tests/`:

- `test_fred_normalize.py`
  - fixture with old observation dates and historical per-observation `realtime_start`
  - assertion that `released_at` is not top-level realtime/fetch date
  - regression that fails if `released_at` is assigned from fetch date
  - `"."` value stores NULL if normalization is responsible for NULL preservation
- `test_3f2_fred_live.py`
  - controlled mocked live orchestration with `from_date`/`to_date`
  - query params include `output_type=4`, `observation_start`, `observation_end`
  - cap/chunk behavior is deterministic and mockable
  - dev DB replacement helper refuses frozen DB realpath
  - `T10Y2Y` value and `released_at=max(component releases)`
  - macro rows never appear in `index_state`
- `test_index_builder.py` or existing Plane-2 test
  - macro rows are not emitted by incremental embedding record builders

## C. Polygon Backfill Completion Design

### Universe and Window

Tickers remain:

```text
AAPL AMD AMZN GOOGL JPM META MSFT NVDA TSLA UNH
```

Source window:

- Existing corpus roughly covers `2025-01-01` to `2026-05-01`.
- Resume live gap-fill from uncovered cells until latest closed US trading day, currently expected to be `2026-07-02` for this audit context because `2026-07-03` was the observed US market holiday before the July 4 weekend.

### Checkpoint-Based Resume

Do not blind re-fetch. Compute missing cells from:

- requested ticker universe
- full trading-day calendar for requested window
- `source_checkpoints.status='success'`
- data/checkpoint reconciliation that flags success checkpoints whose derived data is absent or has blank reference date

Current risk: existing success checkpoints reach `2026-07-02`, while Polygon article associations max around `2026-05-01`. The remediation must classify those cells as not truly complete until the Bronze/Silver rows reconcile.

### Rate-Limit Strategy

Use explicit budgets in the runner:

- `--max-requests-per-day`: default to a conservative free-tier budget configured by the operator; fail closed if unset for live mode.
- `--per-run-cap`: default 25 to 50 cells for free tier until evidence proves a higher safe cap.
- sequential requests by default.
- exponential backoff with jitter for transient 5xx/timeout.
- honor `Retry-After` on 429.
- after quota/rate-limit failure:
  - mark the cell `failed` or quota-specific status with error class
  - stop the run before burning the rest of the budget
  - print exact resume command for next day
- success-empty is valid only when provider returns a successful empty payload for that ticker/date; do not use skipped success for unfetched cells.

### Final Acceptance Gate

- Every ticker is current through latest closed US trading day, or remaining gaps are explicitly accepted provider delay/quota exceptions.
- Failed cells are explainable as provider quota/provider behavior, not logic bugs.
- No duplicate article explosion:
  - article counts grow within expected bounds per ticker/date
  - raw asset ids are deterministic
  - repeat run does not create new raw assets/articles for already successful cells
- `article_tickers` association count remains ticker-lossless:
  - multi-ticker articles retain all ticker associations
  - no blank/NULL `reference_date` for newly rederived rows
- Checkpoint/data reconciliation passes:
  - a success checkpoint must have corresponding raw asset or documented success-empty provider response

## D. Finnhub Validation

Current read-only state:

- `22,941` Finnhub articles
- `420` Finnhub raw assets from `2026-05-04` to `2026-07-02`
- top publishers: Yahoo, Benzinga, CNBC, SeekingAlpha, Finnhub, ChartMill, Fintel

Validation checklist:

- No API key saved in artifacts:
  - inspect `raw_assets.metadata_json` for key-like fields only; do not print env values
  - grep docs/artifacts for accidental `FINNHUB_API_KEY` literal only, not secret value
- Source/publisher distribution is plausible and not a single malformed source.
- `dedup_group_id` is populated for eligible Finnhub/Polygon article rows after cross-source dedup is run.
- Cross-source canonical invariant:

```sql
SELECT dedup_group_id, SUM(is_canonical) AS canonical_count, COUNT(*) AS rows
FROM articles
WHERE dedup_group_id IS NOT NULL
GROUP BY dedup_group_id
HAVING canonical_count != 1;
```

- Finnhub does not overwrite Polygon/SEC provenance:
  - Finnhub `provider='finnhub'`
  - Polygon `provider='polygon'`
  - SEC remains in `filings`/`filing_documents`
  - raw asset `source_type` is source-specific

Further Finnhub fetch:

- No further Finnhub fetch is needed for Step 4 if the current 60-day overlap is enough to validate cross-source dedup and breadth.
- If final Polygon coverage extends beyond Finnhub `2026-07-02`, either accept the current Finnhub 60-day window as Step 4 breadth/discovery or run a tiny checkpointed Finnhub catch-up after Polygon is current.

## E. SEC Validation

Current read-only state:

- `7` SEC filings
- `9` filing documents
- `3` `exhibit_99_1`
- `6` `primary_doc`
- all current `filing_documents.extraction_status='success'`

Validation checklist:

- Bronze raw SEC documents are raw HTML, not extracted text.
- `filing_documents` records extraction success/failure without aborting an entire ticker/day.
- 8-K EX-99.1 is preferred over cover boilerplate when present.
- Zero-filing days write success checkpoints with zero filings/documents and are treated as `success_empty` semantics in the report.
- SEC does not write secrets; only `SEC_USER_AGENT` is used for request headers and must not leak private contact details into artifacts beyond intended public user-agent configuration.

Backfill decision:

- Current tiny run is enough to prove connector/storage/index path.
- Before Step 4, prefer a 90-day SEC backfill for the 10-ticker universe if the goal is richer filing evidence, but do it as a separate approved live chunk. Keep it checkpointed and rate-limited because SEC fair-access matters even without an API key.

## F. `update_pipeline.py` Calendar Fallback Hardening

Current unstaged diff adds `_calendar_trading_days()` and uses it only when `_trading_days_in_window()` finds zero OHLCV rows. That leaves a partial-coverage bug: if OHLCV covers only the start of a requested window, missing later trading dates are silently omitted.

Plan fix:

- Keep dependency-free calendar generation unless an accepted exchange-calendar dependency is already present.
- Compute calendar trading days for the full requested window.
- Query OHLCV trading days for the same window.
- Return either:
  - union of OHLCV dates and calendar-derived dates over the full requested window, sorted; or
  - OHLCV dates plus only missing calendar dates after gap detection.
- Preferred implementation: full-window calendar oracle, with OHLCV used only to resolve universe symbols when tickers are not explicitly passed. This avoids silently dropping future trading days.
- Keep observed US market holidays aligned with `coverage_audit.py`, including Good Friday gaps if the existing list includes them.

Tests in `packages/data-core/tests/test_update_pipeline.py`:

- empty OHLCV window generates calendar fallback dates
- partial OHLCV window includes missing later trading dates
- full OHLCV window behavior unchanged
- US holidays excluded, including `2026-07-03`
- weekends excluded
- `from_date > to_date` returns empty
- failed checkpoints remain retryable
- success checkpoints skip cells

## G. `freshness.py` NO_DATA Hardening

Current unstaged diff adds a guard where grouped rows with `MAX(reference_date) IS NULL` become `NO_DATA` and preserve `days_behind=-1`. Direction is reasonable but needs tests.

Tests in `packages/data-core/tests/test_freshness.py`:

- `MAX(reference_date) IS NULL` returns `NO_DATA`
- `days_behind = -1` sentinel is preserved
- no crash on grouped rows where `article_tickers.reference_date` is NULL/blank
- no crash when there are zero grouped rows and only OHLCV tickers
- normal FRESH/STALE/AHEAD cases unchanged
- optional: blank string reference date is treated like NULL, not parsed as a date

## H. Operational Quality / Non-Toy Requirements

Idempotency:

- deterministic raw asset IDs for source/ticker/date/window
- `INSERT OR REPLACE`/upsert only where current schema already uses it
- repeat dry-run has zero writes
- repeat live run skips success checkpoints unless reconciliation marks them invalid

Checkpointing:

- checkpoint per `(source_type, ticker, date)` cell
- record success, success-empty/skipped semantics, failed, quota/rate-limited error class
- retries should not erase prior success
- interrupted runs should be recoverable by recomputing missing cells

Rate limits:

- live mode requires explicit request budget flags or config
- 429 honors `Retry-After`
- quota failures stop the run and print resume instructions
- SEC remains conservative even without API key

Retries/backoff:

- bounded retries
- backoff with jitter
- retry only transient errors
- permanent malformed payload errors become isolated cell failures

Partial failure isolation:

- one ticker/date failure does not corrupt another cell
- source-specific failures do not mutate unrelated source rows
- FRED replacement happens inside a backup/transactional process with a clear rollback DB copy

No secret leakage:

- never print `.env`
- never print API key values
- redact provider error bodies before logging
- audit artifacts must not contain key query params

Artifact redaction:

- JSON/Markdown audit reports include counts, statuses, dates, and redacted error classes
- do not store request URLs with `api_key=...`

Deterministic dry-run mode:

- default command path is dry-run
- dry-run prints cells that would be fetched, request budgets, and DB rows that would be cleared
- dry-run opens DB read-only or asserts no write counts changed

Safe live mode:

- live mutation requires both `--live --confirm`
- live refuses frozen DB realpath
- FRED replacement requires an additional explicit confirmation such as `--replace-invalid-fred`

Observability/logging:

- per-source summary
- per-ticker coverage table
- failed/quota cell table
- macro PIT integrity summary
- dedup materialization summary
- frozen DB SHA verification line

Audit output suitable for Claude review:

- write a timestamped JSON under `data/provider_discovery/` only after approval
- write a Markdown summary under `docs/reports/` only after approval
- include exact SQL evidence for each acceptance gate

Resume interrupted runs:

- rerun same command with same window and budget
- success checkpoints skipped
- failed/quota cells retried first or listed separately
- no manual DB edits except the approved FRED replacement sequence

Avoid duplicate writes:

- deterministic asset IDs
- dedup runs after all source rederive/classify steps
- final duplicate diagnostics compare before/after counts

Frozen DB immutable:

- all scripts compare realpath against `data/catalyst_eval_frozen_v2.db`
- pre/post SHA must match `0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf`

## I. Implementation Sequence

### 3F.2-R1: Plan + Current-State Audit Only

**Files likely touched:**

- Create: `docs/plans/2026-07-06-ws4b-step3f2-remediation.md`

**Tests/commands:**

- `git status --short --branch`
- `git diff -- packages/data-core/catalyst_data/update_pipeline.py packages/data-core/catalyst_data/freshness.py`
- read-only SQLite queries from section A
- `shasum -a 256 data/catalyst_eval_frozen_v2.db`

**DB mutation:** none.

**Live network:** none.

**Acceptance criteria:**

- plan exists
- terminal report lists git status, DB health, blockers, and next approval needed
- no code/data DB writes

**Rollback plan:**

- delete only the new plan file if the plan is rejected

### 3F.2-R2: Calendar/Freshness Code + Tests, No Network

**Files likely touched:**

- Modify: `packages/data-core/catalyst_data/update_pipeline.py`
- Modify: `packages/data-core/catalyst_data/freshness.py`
- Modify: `packages/data-core/tests/test_update_pipeline.py`
- Modify: `packages/data-core/tests/test_freshness.py`

**Tests to add/run:**

```bash
PYTHONPATH=packages/data-core pytest \
  packages/data-core/tests/test_update_pipeline.py \
  packages/data-core/tests/test_freshness.py -q
```

Optional broader no-network subset:

```bash
PYTHONPATH=packages/data-core pytest packages/data-core/tests/test_coverage_audit.py packages/data-core/tests/test_live_runner.py -q
```

**DB mutation:** none against dev DB. Tests use temp DBs only.

**Live network:** none.

**Acceptance criteria:**

- empty, partial, and full OHLCV calendar cases pass
- US holiday/weekend exclusions pass
- NULL/blank freshness rows return `NO_DATA`
- existing FRESH/STALE/AHEAD behavior unchanged

**Rollback plan:**

- revert only R2 code/test edits if tests show incompatible semantics; do not touch unrelated dirty files

### 3F.2-R3: FRED PIT Remediation + Tests + Controlled Dev DB Replacement

**Files likely touched:**

- Modify: `packages/data-core/catalyst_data/connectors/fred.py`
- Modify: `packages/data-core/catalyst_data/cli_index.py`
- Modify: `packages/data-core/catalyst_data/pipeline/fred_normalize.py`
- Modify: `packages/data-core/catalyst_data/pipeline/fred_manifest.py` only if series/window metadata is needed
- Modify: `packages/data-core/tests/test_fred_connector.py`
- Modify: `packages/data-core/tests/test_fred_normalize.py`
- Modify: `packages/data-core/tests/test_3f2_fred_live.py`

**Tests to add/run before live approval:**

```bash
PYTHONPATH=packages/data-core pytest \
  packages/data-core/tests/test_fred_connector.py \
  packages/data-core/tests/test_fred_normalize.py \
  packages/data-core/tests/test_fred_freshness.py \
  packages/data-core/tests/test_3f2_fred_live.py -q
```

**DB mutation:**

- code/tests: none against dev DB
- after explicit approval only: backup dev DB, clear/replace FRED rows in dev DB only

**Live network:**

- none during code/test phase
- after explicit approval only: FRED scoped PIT fetch

**Acceptance criteria:**

- mocked tests prove `released_at` comes from per-observation realtime metadata
- FRED fetch uses scoped `observation_start`/`observation_end`
- replacement command refuses frozen DB
- post-replacement:
  - invalid current macro rows are gone
  - `released_at` distribution is plausible, not two fetch dates
  - `index_state` macro count remains zero
  - `T10Y2Y` derivation invariant passes

**Rollback plan:**

- restore `data/catalyst_dev_ws4b.before-fred-pit-*.db`
- code rollback only if tests fail and no live replacement has been accepted

### 3F.2-R4: Polygon Resume Runner + Rate-Limit/Checkpoint Audit

**Files likely touched:**

- Modify: `packages/data-core/catalyst_data/update_pipeline.py`
- Modify: `packages/data-core/catalyst_data/coverage_audit.py`
- Modify: `packages/data-core/catalyst_data/cli_index.py`
- Modify: `packages/data-core/tests/test_update_pipeline.py`
- Modify: `packages/data-core/tests/test_3f2_polygon_live.py`
- Modify: `packages/data-core/tests/test_coverage_audit.py`

**Tests to add/run:**

```bash
PYTHONPATH=packages/data-core pytest \
  packages/data-core/tests/test_update_pipeline.py \
  packages/data-core/tests/test_3f2_polygon_live.py \
  packages/data-core/tests/test_coverage_audit.py -q
```

**DB mutation:**

- code/tests: none against dev DB
- after explicit approval only: checkpointed Polygon live resume on dev DB

**Live network:**

- none during tests
- after explicit approval only: Polygon live calls with explicit budget

**Acceptance criteria:**

- partial OHLCV window does not hide missing live dates
- success checkpoints without corresponding data are flagged for review/refetch
- quota failures are recorded and resumable
- current coverage through latest closed US trading day or documented provider delay
- no duplicate explosion
- article ticker associations stay complete

**Rollback plan:**

- dev DB backup before live resume
- rerun from checkpoint after quota resets
- restore DB backup only if logic bug corrupts rows

### 3F.2-R5: Final Full Audit, Data-Core Suite, Commit Recommendation

**Files likely touched:**

- Possibly create a report under `docs/reports/` after approval
- Possibly create timestamped audit JSON under `data/provider_discovery/` after approval
- No app/agents files

**Tests/commands:**

```bash
PYTHONPATH=packages/data-core pytest packages/data-core/tests -q
PYTHONPATH=packages/data-core python -m catalyst_data.cli_index audit-coverage --db data/catalyst_dev_ws4b.db
sqlite3 -readonly data/catalyst_dev_ws4b.db < final_audit.sql
shasum -a 256 data/catalyst_eval_frozen_v2.db
git status --short --branch
```

**DB mutation:** no new mutation in audit phase.

**Live network:** none.

**Acceptance criteria:**

- FRED PIT gates pass
- Polygon coverage/reconciliation gates pass or have accepted provider-delay exceptions
- Finnhub/SEC provenance/dedup gates pass
- macro not embedded
- frozen DB SHA unchanged
- data-core test suite passes
- commit recommendation scopes only data-core/docs plan/report files related to remediation

**Rollback plan:**

- do not commit until audit passes
- if audit fails, return to the failing R2/R3/R4 chunk with temp DB reproduction first

## J. Exact Next Approval Needed

Next action after this plan is not live ingestion. The next approval should be:

```text
Approve 3F.2-R2: implement calendar/freshness tests and code fixes, no network, no dev DB mutation.
```

Do not approve R3 or R4 live work until R2 tests pass and the FRED replacement command has been reviewed in dry-run mode.

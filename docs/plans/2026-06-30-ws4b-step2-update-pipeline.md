# WS4B Step 2 — Update & Backfill Pipeline (Implementation Plan)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> **Status:** PLAN ONLY — for Claude review. Do not implement, edit code, run network calls, or commit.
> **PROVISIONAL — depends on Step 1 outcomes (ticker-scoping mechanism, index_builder API, L2 threshold); re-validate after Step 1 is executed and reviewed.**
> **Prerequisites:** B0–B2 complete (commit `75974e2`); Step 1 (source tier, index builder dry-run, index manifests/state, retrieval policy, CLI status/rebuild-index) merged/reviewed.
> **Archive:** articles = 11,772 canonical Polygon articles; article_tickers = 20,867 lossless; clean_assets polygon_news = 20,867 per-(article, ticker); source_checkpoints = 14,088 (from B0–B2 ingestion); no-merge guard active; frozen DB untouched.

**Goal:** A scriptable, cron-friendly, idempotent update/backfill pipeline with freshness reporting, Polygon-only first, checkpoint/resume via existing `ingestion_runs` + `source_checkpoints` tables, and incremental indexer wired but embedding stubbed to dry-run (no GPU on Mac).

**Architecture:** The pipeline reads the 10-ticker universe, computes freshness gaps against the `ohlcv` trading calendar and `source_checkpoints` table, then fans out per-(ticker, date, source) cells through the existing orchestrator → store → rederive → classify → regenerate → incremental-index (dry-run, delta-only) chain. Every cell's success/failure is check-pointed immediately after Bronze+Silver storage, making the pipeline resumable. A `freshness.py` module provides read-only staleness queries; `update_pipeline.py` drives the update loop; `backfill_pipeline.py` wraps it with date-window chunking for historical ranges.

**Execution scope (Step 2):** code + mocked tests + `--dry-run` only. No real Polygon network calls and no historical backfill during Step 2 execution. Real network calls and backfill are gated to a separate step after Step 2 review.

**Tech Stack:** Python 3.11+, asyncio, sqlite3, existing `orchestrator.process_request`, `rederive.rederive_polygon_news`, `regenerate_clean.regenerate_polygon_clean_assets`, `TokenBucketLimiter`, `with_retry`. No new dependencies.

---

## Task 1 — Freshness Model

### 1.1 New module: `catalyst_data/freshness.py`

**Purpose:** Compute per-(source, ticker) news staleness and per-article index staleness. Pure read-only SQL queries — no side effects. Powers the extended `status` CLI.

**Freshness definitions:**
- **Latest closed US trading day:** `SELECT MAX(date) FROM ohlcv`. This is the authoritative trading calendar. All staleness is measured against it.
- **News staleness per (source, ticker):** `SELECT MAX(reference_date) FROM article_tickers WHERE ticker = ?` (joined to articles for source_type). If this `latest_reference_date` < latest closed trading day, the ticker is STALE (trading days elapsed with no new articles). If equal, FRESH. If greater, AHEAD (should not happen). If no articles exist for ticker, NO_DATA.
- **Index staleness:** For each canonical article in `articles`, check if a corresponding row exists in `index_state` with a non-null `indexed_build_id`. The index_state table is populated ONLY by real embedding runs (Step 4), never by dry-run. **A dry-run incremental indexer cannot report the index as FRESH** — `index_freshness` returns `NO_INDEX` when `index_manifests` has no rows with `status='live'`, regardless of whether dry-run records were built. Only rows with `status='live'` in `index_manifests` (written by Step 4 real embed) count as a real build.

**Functions (prose contracts):**

- `latest_trading_day(conn) -> str` — returns `MAX(date)` from ohlcv. Raises RuntimeError if ohlcv is empty (no trading calendar to measure against).
- `news_freshness(conn) -> dict` — queries article_tickers JOIN articles for per-(source_type, ticker) max reference_date. Returns nested dict keyed by source_type then ticker, each with `{latest_date, status, days_behind}`. Status ∈ {FRESH, STALE, AHEAD, NO_DATA}. Days_behind computed from ISO date difference.
- `index_freshness(conn) -> dict` — reads latest `index_manifests` row WHERE status='live'. If none exists, returns `{status: "NO_INDEX", stale_count: total_articles, ...}`. If a live build exists, LEFT JOINs articles to index_state and counts rows where `indexed_build_id IS NULL OR indexed_build_id != latest_build_id`. Returns `{status: "FRESH"|"STALE", stale_count, total_articles, latest_build_id, last_built_at}`. **Dry-run rows in index_manifests (status='dry_run') are ignored.**
- `freshness_report(conn) -> dict` — combined: `{trading_day, news: {...}, index: {...}}`. Pure function, read-only.

**Edge cases:**
- Ticker with zero articles: NO_DATA status.
- Empty ohlcv table: RuntimeError.
- index_manifests has only dry_run rows, no live rows: NO_INDEX status.
- index_state is empty (no real embed has run): NO_INDEX or STALE depending on index_manifests.

**Files:**
- Create: `packages/data-core/catalyst_data/freshness.py`
- Test: `packages/data-core/tests/test_freshness.py`

**Test assertions (prose):**
1. `latest_trading_day` returns max ohlcv date; raises RuntimeError on empty table.
2. News staleness: STALE when latest_ref_date < trading_day; FRESH when equal; NO_DATA when no rows.
3. Index staleness: NO_INDEX when no live index_manifests row; STALE when articles exist without index_state; FRESH when all articles indexed in latest live build.
4. Dry-run manifest rows (status='dry_run') are ignored — index reports NO_INDEX, not FRESH.
5. `freshness_report` is read-only — no writes, no side effects.

---

## Task 2 — Ingestion Run & Checkpoint Write Helpers

### 2.1 Modify: `catalyst_data/quality.py`

**Purpose:** Add write helper functions for `ingestion_runs` and `source_checkpoints`. These tables already exist (DDL defined in `quality.py`, rows written during B0–B2 ingestion — 3 ingestion_runs, 14,088 source_checkpoints). The module currently only defines DDL and read functions. Add the INSERT helpers the update pipeline needs.

**Existing state to preserve:**
- `ingestion_runs` has 3 rows from B0–B2.
- `source_checkpoints` has 14,088 rows (one per (run_id, source_type, ticker, date) cell).
- `notes` field on `ingestion_runs` stores JSON — use `mode=update|backfill` here.
- `status` on `source_checkpoints` uses CHECK IN ('pending', 'success', 'failed', 'skipped').

**New functions (prose contracts):**

- `open_ingestion_run(conn, *, tickers, sources, mode="update", notes=None) -> str` — INSERTs a new `ingestion_runs` row with `status='running'`, `started_at=now()`, ticker_list_json, source_list_json, and notes. run_id is timestamp-prefixed UUID (`run_YYYYMMDDTHHMMSS_<8hex>`). Returns run_id.
- `close_ingestion_run(conn, *, run_id, success_count, fail_count, status="completed") -> None` — UPDATEs the row with `ended_at=now()`, counts, and final status.
- `write_source_checkpoint(conn, *, run_id, source_type, ticker, date, status, error_class=None, retries=0) -> None` — INSERT OR REPLACE one `source_checkpoints` row. Called immediately after Bronze+Silver storage for a cell. Idempotent (re-running same cell overwrites).
- `close_stale_runs(conn, *, max_age_hours=24) -> int` — UPDATEs any `ingestion_runs` with `status='running'` and `started_at < cutoff` to `status='interrupted'`. Returns count.

**Files:**
- Modify: `packages/data-core/catalyst_data/quality.py`
- Test: `packages/data-core/tests/test_quality_helpers.py`

**Test assertions (prose):**
1. `open_ingestion_run` creates a row with status='running' and returns a unique run_id.
2. `close_ingestion_run` sets ended_at, success_count, fail_count, and status='completed'.
3. `write_source_checkpoint` is idempotent — second write with same (run_id, source_type, ticker, date) does not create a duplicate.
4. `close_stale_runs` marks runs older than max_age_hours as 'interrupted'.

---

## Task 3 — Update Pipeline Engine

### 3.1 New module: `catalyst_data/update_pipeline.py`

**Purpose:** The core update engine. Given a date window and (optional) ticker/source filters, orchestrates the full fetch → archive → normalize → classify → regenerate → incremental-index (dry-run, delta-only) pipeline for polygon_news, with cell-level checkpoint/resume.

**Pipeline flow for a batch (prose):**

1. OPEN ingestion_run (mode=update, tickers, sources, started_at).
2. CLOSE stale runs (>24h running → interrupted).
3. COMPUTE missing cells: for each (ticker, date, source) in window, skip if source_checkpoints(status='success') exists for that cell. Only trading days (from ohlcv calendar) are considered. Non-trading days are excluded from the window. Failed cells are NOT skipped — they are retried.
4. For each missing cell, sequentially (Polygon ~12s interval via TokenBucketLimiter):
   a. CALL orchestrator.process_request(ticker, date, [source], db_path, fetch_fn) — honors rate limiter + with_retry.
   b. On success: write source_checkpoints(status='success') IMMEDIATELY after Bronze+Silver storage.
   c. On failure: write source_checkpoints(status='failed', error_class, retries).
   d. Track success/fail counts.
5. After batch: RE-DERIVE articles + article_tickers (idempotent upsert via rederive_polygon_news).
6. CLASSIFY source tiers (idempotent — only touches NULLs via classify_articles from Step 1).
7. REGENERATE clean_assets per-(article, ticker) (delete + rebuild polygon_news scope).
8. INCREMENTAL INDEX dry-run (delta-only): call index_builder.build_incremental_records() — computes which article_ids are new/changed vs index_state. Builds records only for those. Does NOT write index_state rows and does NOT write index_manifests with status implying embedding. Reports `{new_article_count, changed_article_count, would_embed_count}`.
9. CLOSE ingestion_run with final counts.
10. EMIT report dict with per-source/ticker/date breakdown, freshness before/after, and incremental dry-run delta counts.

**Functions (prose contracts):**

- `compute_missing_cells(conn, *, tickers, sources, from_date, to_date) -> list[(ticker, date, source)]` — queries source_checkpoints to find cells without status='success' in the given window. Only trading days from ohlcv are considered. Failed cells (status='failed') are included as missing. Returns sorted list.
- `run_update_batch(db_path, *, tickers, sources, from_date, to_date, fetch_fn, limiter=None, limit=None, dry_run=False) -> dict` — the main async batch runner. Returns a report dict with keys: run_id, cells_total, cells_success, cells_failed, cells_skipped, articles_upserted, clean_assets_inserted, index_delta_new, index_delta_changed, index_would_embed, elapsed_sec, freshness_before, freshness_after, per_cell_report. When dry_run=True, only computes and returns missing cells — no network calls, no DB writes.
- `_fetch_cell(db_path, ticker, date, source, run_id, fetch_fn, limiter) -> dict` — async wrapper around orchestrator.process_request for one cell. Respects limiter and retry. Returns `{ticker, date, source, status, error, articles_count}`.

**Key design decisions:**
- **Sequential per source:** Polygon is processed sequentially (1 concurrent) via TokenBucketLimiter. If FMP is added later (Step 3), it can run in parallel with Polygon since they have separate limiters.
- **Cell-level checkpoint:** write_source_checkpoint is called immediately after _store_bronze_and_silver succeeds. We wrap orchestrator.process_request: call it, inspect the returned summary, write checkpoints ourselves. The orchestrator is NOT modified (preserves backward compat).
- **Re-derive after batch:** rederive_polygon_news(db_path) re-processes all raw_assets rows — fast (INSERT OR REPLACE, existing rows are no-ops).
- **Regenerate after batch:** regenerate_polygon_clean_assets(db_path) deletes all polygon_news clean_assets rows and rebuilds from article_tickers. Guarantees consistency.
- **No merge ever:** Only run_transform_v2 is called; the merge guard in transform_v2 is active.
- **Dry-run incremental indexer (delta-only):** Computes the diff of articles vs index_state (articles missing from index_state OR with stale content_hash) — called via index_builder.build_incremental_records(). Reports counts only. Does NOT write index_state or index_manifests. Those are populated exclusively by real embedding (Step 4).

**Files:**
- Create: `packages/data-core/catalyst_data/update_pipeline.py`
- Test: `packages/data-core/tests/test_update_pipeline.py`

**Test assertions (prose, all mocked — no real network):**
1. `compute_missing_cells` returns empty list when all cells have source_checkpoints(status='success').
2. `compute_missing_cells` returns uncovered cells correctly.
3. Failed checkpoints (status='failed') are included as missing (retried).
4. Ticker and source filters are respected.
5. Empty date window (from > to) returns empty list.
6. `run_update_batch` with dry_run=True returns cell list without making network calls.
7. Idempotent re-run: second run with same window returns 0 cells to process.
8. Checkpoint skip: a cell with status='success' is skipped on re-run.
9. Ingestion_run lifecycle: opened → cells processed → closed with counts.
10. No-merge invariant: no clean_asset content_md exceeds 50KB.
11. Incremental dry-run delta: only new/changed article_ids appear in the diff; unchanged articles are excluded.
12. Incremental dry-run does NOT write index_state or index_manifests with status='live' or 'dry_run' that would imply embedding completion.

---

## Task 4 — CLI Integration (update-news + backfill + status --freshness)

### 4.1 Modify: `catalyst_data/cli_index.py` (Step 1 artifact)

**Purpose:** Extend the Step 1 CLI module with new subcommands. The existing module has `status` and `rebuild-index --mode dry-run`. Add:
- `status --freshness` — calls freshness_report() and prints formatted report. Shows NO_INDEX when no real embed has run.
- `update-news` — runs run_update_batch with CLI args.
- `backfill` — runs run_backfill with date-window chunking.

**Subcommand: `update-news`**

```
python -m catalyst_data.cli_index update-news \
  --from 2026-06-27 --to 2026-06-30 \
  --tickers AAPL,MSFT,NVDA \
  --sources polygon_news \
  --limit 50 \
  --dry-run
```

Flags: `--from`/`--to` (default: single latest trading day), `--tickers` (default: 10 universe tickers), `--sources` (default: polygon_news), `--limit` (cap cells), `--dry-run` (compute missing cells, no fetch).

**Subcommand: `backfill`**

```
python -m catalyst_data.cli_index backfill \
  --from 2024-12-30 --to 2026-06-30 \
  --tickers AAPL,...,UNH \
  --sources polygon_news \
  --chunk-days 7
```

Flags: `--chunk-days` (split window into N-day chunks, default 7). Each chunk runs independently; checkpoint/resume means interrupted backfill continues from last successful cell. **Real backfill execution is gated — not run during Step 2 execution.**

**Implementation notes:** Uses argparse subparsers. `status --freshness` opens dev DB, calls freshness_report(conn), prints formatted output. `update-news` resolves date defaults from ohlcv, calls asyncio.run(run_update_batch(...)). `backfill` splits window, calls run_update_batch per chunk, accumulates totals.

**Files:**
- Modify: `packages/data-core/catalyst_data/cli_index.py`
- Test: `packages/data-core/tests/test_cli_index.py` (extend Step 1 tests)

**Test assertions (prose):**
1. `status --freshness` exits 0, prints report including NO_INDEX when applicable.
2. `update-news --dry-run` exits 0, prints missing cells, makes zero network calls.
3. `update-news` with bad date format exits non-zero.
4. `backfill --dry-run` exits 0 (window chunking logic only, no network).

---

## Task 5 — Backfill Pipeline

### 5.1 New module: `catalyst_data/backfill_pipeline.py`

**Purpose:** Thin wrapper around update_pipeline.run_update_batch that splits a wide date range into smaller chunks for safety and resumability.

**Function contract:** `run_backfill(db_path, *, tickers, sources, from_date, to_date, fetch_fn, limiter=None, chunk_days=7, limit_per_chunk=None) -> list[dict]` — splits [from_date, to_date] into chunk_days-sized windows, runs each through run_update_batch, returns list of per-chunk report dicts. Each chunk independently checkpoints; interrupted backfill resumes from last successful cell.

**Files:**
- Create: `packages/data-core/catalyst_data/backfill_pipeline.py`
- Test: `packages/data-core/tests/test_backfill_pipeline.py`

**Test assertions (prose):**
1. Date window chunking produces correct non-overlapping sub-windows.
2. Total cells across chunks equals full window cells.
3. Chunk size boundary is respected (no chunk exceeds chunk_days).
4. Interrupted backfill: mock half the chunks as already checkpointed → remaining chunks only.

---

## Task 6 — Incremental Indexer (Dry-Run, Delta-Only)

### 6.1 Modify: `catalyst_data/index_builder.py` (Step 1 artifact)

**Purpose:** Add `build_incremental_records()` that computes the delta of article_ids not yet reflected in index_state, builds records only for those, and reports counts. **Does NOT write index_state or index_manifests.** Those are populated exclusively by real embedding (Step 4).

**Function contract:** `build_incremental_records(conn, *, min_l2_chars=800) -> dict` — LEFT JOINs articles to index_state. Finds article_ids where index_state.article_id IS NULL (new articles) OR index_state.content_hash != articles' computed content_hash (changed articles — title/description edited). Builds L1+L2 records only for those article_ids. Returns `{new_article_count, changed_article_count, total_delta_articles, l1_count, l2_count, would_embed_count, delta_article_ids}`. Does NOT INSERT into index_state. Does NOT INSERT into index_manifests.

**Wire into update pipeline:** After regenerate_polygon_clean_assets in run_update_batch, call build_incremental_records(conn). The result feeds into the batch report as `index_delta_new`, `index_delta_changed`, `index_would_embed`.

**Files:**
- Modify: `packages/data-core/catalyst_data/index_builder.py`
- Test: extend `packages/data-core/tests/test_index_builder.py`

**Test assertions (prose):**
1. When index_state is empty, ALL articles appear in delta.
2. When index_state covers all articles with matching content_hash, delta is empty.
3. When one new article is added, only that article_id appears in delta.
4. When one article's description changes, only that article_id appears in delta.
5. No writes to index_state or index_manifests occur (verify conn state unchanged).

---

## Task 7 — Tests (Full Suite)

### 7.1 Test files and key assertions

| File | Count | Key Assertions |
|------|-------|----------------|
| `tests/test_freshness.py` | 6 | Trading day from ohlcv, news staleness (STALE/FRESH/NO_DATA), index staleness (STALE/FRESH/NO_INDEX), empty calendar raises, dry-run manifests ignored (NO_INDEX), read-only |
| `tests/test_quality_helpers.py` | 4 | open/close ingestion_run lifecycle, checkpoint idempotent INSERT OR REPLACE, stale run closure (>24h) |
| `tests/test_update_pipeline.py` | 12 | Missing cell computation (all covered, partial, failed re-tried), ticker/source filter, empty window, dry-run no network, idempotent re-run (zero cells), checkpoint skip, ingestion_run lifecycle, count report shape, no-merge invariant, incremental delta-only, no index_state writes from dry-run |
| `tests/test_backfill_pipeline.py` | 4 | Window chunking correctness, no overlap, total cells match, checkpoint resume across chunks |
| `tests/test_cli_index.py` (extended) | 5 | `status --freshness` exits 0, `status --freshness` shows NO_INDEX, `update-news --dry-run` exits 0, `backfill --dry-run` exits 0, bad date format exits non-zero |

### 7.2 Test runner

```bash
cd packages/data-core && python -m pytest \
  tests/test_freshness.py \
  tests/test_quality_helpers.py \
  tests/test_update_pipeline.py \
  tests/test_backfill_pipeline.py \
  tests/test_cli_index.py \
  -v
```

### 7.3 Not tested locally

Real network calls to Polygon API are not tested in the local suite. Tests use AsyncMock for fetch_fn. Real network verification and historical backfill are deferred — gated to a separate step after Step 2 review.

---

## Task 8 — Validation Report (Post-Implementation)

| Metric | Expected | Verification |
|--------|----------|--------------|
| Freshness report correct | Stale tickers identified; NO_INDEX when no real embed | `python -m catalyst_data.cli_index status --freshness` |
| Update dry-run lists correct cells | Missing cells shown, 0 network calls | `python -m catalyst_data.cli_index update-news --dry-run` |
| Idempotent re-run | 0 cells processed on second run | Run mock twice; second run cells_skipped == cells_total |
| Checkpoint resume | Interrupted run resumes without duplicating | Mock kill mid-batch, re-run; all previously successful cells skip |
| No-merge invariant | Every clean_asset ≤50KB | `SELECT MAX(LENGTH(content_md)) FROM clean_assets WHERE source_type='polygon_news'` < 50000 |
| Per-(article, ticker) clean_assets | No bundles | clean_assets count == article_tickers count |
| Ingestion run lifecycle | run opened → cells checkpointed → run closed | DB query on ingestion_runs |
| Incremental dry-run delta | Only new/changed article_ids | delta_article_ids ⊆ articles |
| Incremental dry-run writes nothing | index_state + index_manifests unchanged | Count before/after dry-run |
| Frozen DB SHA-256 | Unchanged | `shasum -a 256 data/catalyst_eval_frozen_v2.db` |
| No real network during Step 2 execution | All tests use mocks | Code audit |

---

## Files Summary

### Created
| File | Purpose |
|------|---------|
| `catalyst_data/freshness.py` | Read-only freshness computation (trading day, news staleness, index staleness) |
| `catalyst_data/update_pipeline.py` | Batch update engine with cell-level checkpoint/resume |
| `catalyst_data/backfill_pipeline.py` | Historical backfill with date-window chunking |

### Modified
| File | Change |
|------|--------|
| `catalyst_data/quality.py` | Add open_ingestion_run(), close_ingestion_run(), write_source_checkpoint(), close_stale_runs() |
| `catalyst_data/cli_index.py` | Add --freshness flag to status; add update-news and backfill subcommands |
| `catalyst_data/index_builder.py` | Add build_incremental_records() for delta-only dry-run (no writes) |

### Test files
| File | Tests |
|------|-------|
| `tests/test_freshness.py` | 6 |
| `tests/test_quality_helpers.py` | 4 |
| `tests/test_update_pipeline.py` | 12 |
| `tests/test_backfill_pipeline.py` | 4 |
| `tests/test_cli_index.py` (extended) | 5 |

---

## Task Order

```
1. freshness.py + test                    (independent — read-only SQL)
2. quality.py write helpers + test        (independent — extends existing DDL)
3. update_pipeline.py + test              (depends on 1, 2; imports orchestrator, all mocked)
4. backfill_pipeline.py + test            (depends on 3 — wraps update_pipeline, all mocked)
5. index_builder.py incremental mode      (depends on Step 1 index_builder; delta-only, no writes)
6. cli_index.py integration               (wires 1, 3, 4, 5 into CLI, all mocked)
7. Run full scoped test suite             (all of above)
8. Validation report                      (freshness, idempotency, dry-run counts)
```

Tasks 1 and 2 can run in parallel. All network-dependent code is mocked.

---

## Guardrails

1. **data-core only** — no `packages/app/`, no `packages/agents/`.
2. **Dev DB only** — `data/catalyst_dev_ws4b.db`. Frozen DB `data/catalyst_eval_frozen_v2.db` READ-ONLY.
3. **Reuse existing operational tables** — `ingestion_runs` + `source_checkpoints` already exist (3 + 14,088 rows). Only INSERT, no DDL changes.
4. **Additive DDL only** — Step 1 adds `index_manifests` + `index_state`; Step 2 makes zero DDL changes.
5. **No new dependencies** — all imports from stdlib (`sqlite3`, `asyncio`, `json`, `datetime`) or existing `catalyst_data` modules.
6. **No commit** — all changes stay in working tree for review; the human commits.
7. **No real Polygon network calls during Step 2 execution** — all tests use AsyncMock. `--dry-run` is the only CLI mode exercised. Real network calls and historical backfill are gated to a separate step after Step 2 review.
8. **No embedding on Mac** — incremental indexer is dry-run only (record builder, no model load, no FlagEmbedding, no BGEM3FlagModel, no GPU).
9. **Rate limits honored** — TokenBucketLimiter enforces 12s interval; with_retry wraps every call.
10. **No merge ever** — _transform_article merge guard (TypeError on list) active; only run_transform_v2 called.
11. **Dry-run incremental indexer writes nothing** — index_state and index_manifests are populated exclusively by real embedding (Step 4). index_freshness ignores dry_run manifest rows.

---

## Out of Scope

- **Step 3:** New connectors (SEC, Finnhub, FMP news).
- **Step 4:** Real GPU embedding, LanceDB rebuild, retrieval switchover.
- **Any UI/admin button** — CLI only.
- **Any change to `packages/app/` or `packages/agents/`.**
- **FMP/FRED/ohlcv fetching** — polygon_news only for Step 2.
- **New run-ledger tables** — reuse existing `ingestion_runs` + `source_checkpoints` only.
- **Real network calls or historical backfill during Step 2 execution** — gated separately.

---

## Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Rate limit violation (Polygon free tier = 5 req/min) | Medium | TokenBucketLimiter enforces 12s interval; --limit caps batch; --dry-run for pre-flight |
| Interrupted run leaves stale ingestion_runs row | Low | close_stale_runs() marks runs >24h as 'interrupted' |
| source_checkpoints has 14,088 existing rows with old run_id values | Already handled | compute_missing_cells queries by (source_type, ticker, date) with status='success', ignoring run_id |
| Re-derive + regenerate after every batch is expensive | Low | Re-derive is INSERT OR REPLACE — instant for unchanged data. Regenerate is O(article_tickers) ~20k rows, <5s |
| Step 1 not yet implemented | Step 2 plan assumes it | index_builder API and cli_index.status are Step 1 artifacts; re-validate after Step 1 execution |
| Dry-run incremental indexer accidentally writes index_state | Low | Explicit guard: build_incremental_records returns dict only, no INSERT calls; verified by test assertion 5 |

---

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. All network calls mocked; --dry-run only for CLI. All work on dev DB only; no commit.

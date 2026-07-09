# WS4B Step 3F.2 — Live Ingestion & Validation (v2 — AMENDED)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task-by-task. TDD-first: write each discriminating test, run it, prove it FAILS
> before implementing.

**Goal:** Close the ~62-day staleness gap discovered by the 3F.1 audit — bring all four
evidence planes (Polygon news, Finnhub company-news, SEC filings, FRED macro) to live
coverage through today's latest closed trading day, with idempotent resume, cross-source
dedup, point-in-time integrity, and zero-corruption invariants proven.

**Architecture:** Two pipeline bugs exist in the as-built code that MUST be fixed before
any live run: (B1) FRED update-macro --live is a stub that prints a message and returns;
(B2) the post-batch ordering in run_update_batch runs cross-source dedup BEFORE Polygon
rederive and classify, so Polygon articles from a combined batch are never deduped or
classified. Additionally, (B3) the Polygon live fetch_fn is a dict keyed by source_type
but passes through to process_request un-unwrapped — process_request expects a callable.
Fixing these three bugs is the core implementation of 3F.2. THEN the live paths work,
and the human-executed RUNBOOK can run.

**Tech Stack:** Python 3.12, sqlite3, httpx, stdlib only. No new dependencies.

**Baseline:** `ws4b/article-level-data` branch. 3F.1 committed (c57e379).
Frozen-DB SHA: `0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf`.

---

## Changelog from v1 (orchestrator review)

| # | Finding | Change |
|---|---------|--------|
| B1 | FRED update-macro --live is a STUB: prints "(Live macro fetch not yet implemented in pipeline)" and returns. Runbook Step 15 was fiction. | §4 rewritten: FRED live is now CODE TO BUILD (per-series fetch, Bronze archive, rederive), not "verify". Tests TM1–TM7 expanded to cover live orchestration. |
| B2 | Post-batch ordering bug: `compute_cross_source_dedup` runs BEFORE `rederive_polygon_news` and `classify_articles`. Combined polygon+finnhub batch: Polygon articles never deduped, never classified. | §5.3 rewritten: fix order to redrive ALL → classify → dedup → regenerate. TI1 made discriminating: assert both providers' articles are deduped + classified in the SAME batch; prove it FAILS against current order. |
| B3 | Polygon live fetch_fn routing broken: CLI passes `{"polygon_news": fn}` dict to run_update_batch, which passes it as-is to _fetch_cell → process_request → _fetch_endpoints → `await dict(ticker, ep, date)` → TypeError. | §1.4 added: fix run_update_batch to unwrap `cell_fetch_fn = fetch_fn.get(src, fetch_fn)` for polygon cells. TP7 (new) is a discriminating test: run a live polygon cell through the actual pipeline → assert raw_assets + articles written, no TypeError. |
| M1 | "Add dedup_groups_resolved to report" was wrong — it's already in the return dict. | Removed that task. |
| M2 | Plan assumed polygon live works — it doesn't (see B3). | Fix added. Verified all four paths against real code (see §0 table). |
| M3 | "ZERO new pipeline modules" premise was false for FRED and misleading for polygon routing. | Replaced with a per-source audit table (§0) citing exact file:line for each path. |

---

## 0. Code-Grounded Audit: Which Live Paths Actually Work

Citation format: `file:line_or_function` — verified against as-built code on branch
`ws4b/article-level-data` at commit c57e379.

| Source | Live Path Status | Exact Flow (verified) |
|---|---|---|
| **polygon_news** | **BROKEN (B3)** | CLI `cmd_update_news:274-277` builds `fetch_fn = {"polygon_news": fn}` dict. `run_update_batch:593` calls it. `run_update_batch:706` sets `cell_fetch_fn = fetch_fn` (the dict). `_fetch_cell:244-260` passes it to `process_request`. `orchestrator:_fetch_endpoints:135` calls `await fetch_fn(ticker, ep, date)` — **TypeError: 'dict' object is not callable.** |
| **finnhub_company_news** | **WORKS** | CLI dict is ignored. `run_update_batch:653-663` constructs `finnhub_fetcher_ns = create_finnhub_fetcher(...)` FROM env vars. `run_update_batch:716` sets `cell_fetch_fn = finnhub_fetcher_ns.fetch`. `_fetch_cell:165` dispatches to `_fetch_cell_finnhub`. |
| **sec_filings** | **WORKS** | Same pattern as finnhub. `run_update_batch:665-678` constructs `fetcher_ns = create_sec_fetcher(...)` FROM env vars. `run_update_batch:726` sets `cell_fetch_fn = fetcher_ns.fetch`. `_fetch_cell:133` dispatches to `_fetch_cell_sec`. |
| **fred_macro** | **STUB (B1)** | `cmd_update_macro:509-514` prints "(Live macro fetch not yet implemented in pipeline)" and `return`s. Fetch, Bronze archive, and rederive are never called. |

**Post-batch order in `run_update_batch:755-795` (verified B2):**
Current order: finnhub_rederive → dedup → polygon_rederive → classify → regenerate.
Bug: dedup and classify run before polygon rederive. Combined batch: Polygon articles
are neither deduped nor classified.

**What actually works already (no changes needed):**
- Finnhub `_fetch_cell_finnhub`: fetch → Bronze archive → checkpoint. (`update_pipeline:307-379`)
- SEC `_fetch_cell_sec`: submissions → normalize → resolve docs → Bronze → upsert. (`update_pipeline:381-567`)
- Finnhub `rederive_finnhub_news`: Bronze → articles/article_tickers. (`pipeline/finnhub_normalize:99`)
- FRED `rederive_fred_macro`: Bronze → macro_observations (incl. T10Y2Y). (`pipeline/fred_normalize:124`)
- `compute_cross_source_dedup`: Pass 1 + Pass 2 OR-semantics. (`dedup/cross_source:106`)
- `classify_articles`: tier-by-publisher via `_PUBLISHER_TIER`. (`source_tier.py`)
- All limiters, retry configs, checkpoints, freshness, CLI guard order, redacted config.

---

## 1. Implementation: Fix Three Bugs (Core 3F.2 Work)

### 1.1 B3 Fix — Polygon Live fetch_fn Routing (update_pipeline.py)

In `run_update_batch`, the per-cell dispatch loop (~line 706), when `src` is not
finnhub and not sec (i.e., polygon_news), the code does `cell_fetch_fn = fetch_fn`.
But `fetch_fn` is a dict `{"polygon_news": callable, ...}` from the CLI. The orchestrator's
`process_request` needs a bare async callable, not a dict.

Fix: add an `elif src in fetch_fn` branch that unwraps `cell_fetch_fn = fetch_fn.get(src)`
before the existing finnhub/sec branches. If `fetch_fn` is already a callable (legacy
compatibility), leave it alone.

Target file: `catalyst_data/update_pipeline.py`, lines ~704–728.

### 1.2 B2 Fix — Post-Batch Ordering (update_pipeline.py)

Re-order the post-batch section (~lines 755–795) so the sequence is:

1. `rederive_polygon_news` (if polygon in sources) — Bronze → Silver for Polygon
2. `rederive_finnhub_news` (if finnhub in sources) — Bronze → Silver for Finnhub
3. `classify_articles` (always, for all articles with NULL source_tier) — assign tiers
4. `compute_cross_source_dedup` (if BOTH polygon AND finnhub in sources, or if
   article_tickers.dedup_group_id has NULL rows that could be grouped) — run the
   two-pass dedup, now with BOTH providers' articles present
5. `regenerate_clean_assets` (for polygon if in sources)
6. Incremental index dry-run

Also: the `conn` is re-opened between dedup and polygon rederive (line ~768). This is
correct (dedup closes its own connection). Keep it.

### 1.3 B1 Fix — FRED Live Orchestration (cli_index.py)

Replace the stub in `cmd_update_macro` with the real loop:

1. Resolve series list (from `--series` arg or `FETCHED_SERIES` from the manifest).
2. For each series: call `fetch_fn(series_id, "observations", None)` with the PIT params
   (`output_type=4`, `realtime_start=1776-07-04`, `realtime_end=9999-12-31`).
3. Serialize the raw JSON response to bytes, compute `asset_id` via
   `compute_asset_id(series_id, today, "fred_macro")`, call `upsert_raw_asset(...)`.
4. After all series fetched: call `rederive_fred_macro(db_path)`.
5. Print summary: series count, observations_upserted, T10Y2Y derived.

The `build_fred_fetcher()` already returns `(fetch_fn, limiter)`. The `fetch_fn` is an
async callable `fetch(series_id, endpoint, date) -> FetchResult`.

The FRED connector (`connectors/fred.py`) already supports `output_type=4` and
`realtime_start`/`realtime_end` params — verified: `create_fred_fetcher()` returns a
`fetch()` that passes extra kwargs to the API URL. The live orchestration passes these
as arguments.

Target file: `catalyst_data/cli_index.py`, function `cmd_update_macro`, lines ~505–514.

---

## 2. Test Plan (TDD — Write Failing Tests First)

### 2.1 New Test Files (4 files, ~27 tests)

**tests/test_3f2_polygon_live.py** — Polygon gap-fill + routing fix (~7 tests)

- TPL1 — test_polygon_live_fetchfn_unwrap: Mock fetch_fn as dict `{"polygon_news": mock_callable}`.
  Call run_update_batch with polygon_news source, dry_run=False. Assert mock_callable was awaited.
  Discriminating: this FAILS against current code (dict passed to process_request → TypeError).

- TPL2 — test_polygon_live_raw_asset_written: Mock a Polygon response with 2 articles. Assert
  raw_assets COUNT increases for source_type='polygon_news', articles COUNT increases, and
  article_tickers rows present.

- TPL3 — test_gapfill_idempotent_repeat_run: Two runs with same mocks → second run writes 0 new
  raw_assets (COUNT before == COUNT after for source_type='polygon_news').

- TPL4 — test_multi_ticker_preserved: Article with tickers=["AAPL","MSFT"] → two article_tickers
  rows, both preserved after rederive.

- TPL5 — test_news_soup_guard: Article fetched for AAPL whose Polygon tickers list excludes AAPL
  → article dropped (not in articles). Discriminating: insert deliberately wrong article.

- TPL6 — test_failed_cell_retried: Checkpoint status='failed' → cell appears in missing →
  re-fetched → on success, checkpoint updated to 'success'.

- TPL7 — test_checkpoint_resume: Mock 10 cells, simulate crash after 5 → re-run → only 5 fetched.

**tests/test_3f2_finnhub_live.py** — Finnhub backfill + dedup (~6 tests)

- TFL1 — test_finnhub_idempotent_repeat_run: Two runs → second run 0 new raw_assets.

- TFL2 — test_retry_after_honored: Mock 429 with Retry-After:5 → elapsed ≥ 5s.
  Discriminating: if Retry-After ignored, elapsed < 5s.

- TFL3 — test_cross_source_dedup_polygon_wins: Polygon + Finnhub articles with same fingerprint
  → same dedup_group_id, Polygon is canonical (CROSS_SOURCE_PRIORITY). Assert exactly one
  canonical per group.

- TFL4 — test_cross_source_dedup_or_semantics: Three articles in group → only winner is
  is_canonical=1. Assert no group has zero canonical.

- TFL5 — test_finnhub_tier_by_publisher: Seeking Alpha → T5, Reuters → T2, unknown → T4.

- TFL6 — test_finnhub_daily_incremental_finds_zero: After backfill, compute_missing_cells → 0.

**tests/test_3f2_sec_live.py** — SEC tiny live (~5 tests)

- TSL1 — test_sec_ex99_1_resolved: Mock 8-K with EX-99.1 → filing_documents row with
  document_type='exhibit_99_1' has non-empty text containing financial terms.

- TSL2 — test_sec_bronze_raw_html: Decompress filing_documents raw_bytes → contains "<html"
  (case-insensitive). Discriminating: fails if Bronze stores extracted text.

- TSL3 — test_sec_filing_id_format: filing_id matches `sec:\d{10}:\d{12}-\d{2}-\d{6}`.

- TSL4 — test_sec_tier_one: All filings have source_tier=1 after classify.

- TSL5 — test_sec_filing_documents_count: Assert filing_documents COUNT > 0 with non-null
  filing_id FK.

**tests/test_3f2_fred_live.py** — FRED live orchestration + PIT (~7 tests)

- TML1 — test_fred_live_orchestration: Mock fetcher returns valid output_type=4 JSON. Call the
  live path (cmd_update_macro --live flow via fixture). Assert raw_assets written for
  source_type='fred_macro', macro_observations rows upserted, T10Y2Y derived.
  Discriminating: this FAILS before B1 fix (stub returns without doing anything).

- TML2 — test_pit_released_at_after_observation_date: Mock response where realtime_start is
  2 days after observation_date. Assert released_at > observation_date.
  Discriminating: if released_at = fetch date, this fails.

- TML3 — test_pit_query_excludes_later_revisions: Two observations for same date (initial
  release + revision). Query with as_of between them → only initial returned.
  Discriminating: without PIT filtering, latest-value-wins returns revision.

- TML4 — test_dot_to_null: value="." → stored as NULL in macro_observations.

- TML5 — test_t10y2y_derivation: Mock DGS10 + DGS2 → T10Y2Y rows exist with
  value = DGS10 - DGS2, released_at = max(components).

- TML6 — test_plane2_not_in_index_state: Assert macro_observations COUNT > 0 and
  COUNT(*) FROM index_state WHERE source_kind LIKE '%macro%' = 0.

- TML7 — test_bronze_rederivability_macro: Insert fred_macro raw_asset, run rederive →
  decompress, re-normalize → matches macro_observations rows.

- TML8 — test_fred_repeat_run_idempotent: Two macro updates → second upserts 0 new observations.

### 2.2 Integration Test (1 file, ~2 tests)

**tests/test_3f2_integration.py** — Full end-to-end with ordering fix (~2 tests)

- TII1 — test_combined_polygon_finnhub_batch_dedup_and_classify: Mock both Polygon and Finnhub
  responses for overlapping dates. Run run_update_batch with sources=["polygon_news",
  "finnhub_company_news"]. Assert BOTH providers' articles have non-NULL source_tier, both
  have dedup_group_id populated for overlapping titles, and one-canonical-per-group holds.
  **Discriminating: this FAILS against the current post-batch order** — Polygon articles
  won't be deduped (dedup runs before Polygon rederive) and won't be classified (classify
  runs before Polygon rederive).

- TII2 — test_post_batch_order_all_steps: Assert the post-batch section runs in the correct
  order by tracking which functions are called and in what sequence. Each step's output
  feeds the next (e.g., classify runs AFTER rederive, dedup runs AFTER classify).

### 2.3 Discriminating Bite-Check Table

| Bite-Check | Why It Bites | Counter-Proof |
|---|---|---|
| **TPL1** (dict unwrap) | Mock polygon fetch as dict → process_request crashes with TypeError | Current code passes dict through, never unwraps |
| **TML1** (fred live orchestration) | Mock fetcher, call live path → assert raw_assets + observations written | Current code prints stub message and returns; zero rows |
| **TII1** (combined batch dedup + classify) | Both providers in batch → assert BOTH have tiers + dedup groups | Current order: dedup runs before polygon rederive, classify also before rederive → Polygon articles have NULL tier, NULL dedup |
| **TPL3** (repeat-run idempotent) | Two runs → raw_assets COUNT unchanged | Would fail if checkpoint model broken |
| **TFL2** (Retry-After honored) | 429 mock + Retry-After:5 → elapsed ≥ 5s | Would pass in <5s if header ignored |
| **TSL2** (Bronze raw HTML) | Decompress Bronze → "<html" present | Would fail if Bronze stored extracted text |
| **TML2** (released_at after obs date) | PIT field > observation_date | Would fail if released_at = fetch date |
| **TML3** (PIT excludes revisions) | Query at as_of between releases → only initial | Would return latest value without PIT filter |

---

## 3. Source-by-Source Execution Order (Human Runbook)

Order: Polygon FIRST → Finnhub → SEC → FRED. Polygon must fill its gap before Finnhub
backfills, so the ~60-day overlap window has Polygon rows for dedup.

### 3.0 Pre-Flight

```text
git branch --show-current          # ws4b/article-level-data
git status --short                 # only expected 3F.2 files

# Backup dev DB (kept local, NOT committed)
cp data/catalyst_dev_ws4b.db data/catalyst_dev_ws4b_backup_$(date +%Y%m%d).db

# Frozen DB guard
shasum -a 256 data/catalyst_eval_frozen_v2.db
# Expected: 0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf

# Verify keys (do NOT print values)
python3 -c "
import os
for k in ['POLYGON_API_KEY','FINNHUB_API_KEY','SEC_USER_AGENT','FRED_API_KEY']:
    print(f'{k}: {\"SET\" if os.environ.get(k) else \"MISSING\"}')
"
```

### 3.1 Polygon (backfill, ~440 cells)

```text
# Dry-run
python3 -m catalyst_data.cli_index backfill \
  --from 2026-05-02 --to 2026-07-03 --sources polygon_news \
  --chunk-days 7 --db data/catalyst_dev_ws4b.db

# Small-scope live (1 ticker, recent)
python3 -m catalyst_data.cli_index backfill \
  --from 2026-06-26 --to 2026-07-03 --sources polygon_news \
  --tickers AAPL --chunk-days 7 --live --confirm \
  --db data/catalyst_dev_ws4b.db

# Full gap-fill
python3 -m catalyst_data.cli_index backfill \
  --from 2026-05-02 --to 2026-07-03 --sources polygon_news \
  --chunk-days 7 --live --confirm --db data/catalyst_dev_ws4b.db

# Verify idempotent
python3 -m catalyst_data.cli_index backfill \
  --from 2026-05-02 --to 2026-07-03 --sources polygon_news \
  --chunk-days 7 --db data/catalyst_dev_ws4b.db
# Expected: cells_total=0

# Verify freshness
python3 -m catalyst_data.cli_index status --freshness --db data/catalyst_dev_ws4b.db
# Expected: polygon_news days_behind ≤ 2
```

### 3.2 Finnhub (backfill, ~600 cells for 60-day window)

```text
# Dry-run
python3 -m catalyst_data.cli_index backfill \
  --from 2026-05-04 --to 2026-07-03 --sources finnhub_company_news \
  --chunk-days 7 --db data/catalyst_dev_ws4b.db

# Small-scope live
python3 -m catalyst_data.cli_index backfill \
  --from 2026-06-26 --to 2026-07-03 --sources finnhub_company_news \
  --tickers AAPL --chunk-days 7 --live --confirm \
  --db data/catalyst_dev_ws4b.db

# Full backfill
python3 -m catalyst_data.cli_index backfill \
  --from 2026-05-04 --to 2026-07-03 --sources finnhub_company_news \
  --chunk-days 7 --live --confirm --db data/catalyst_dev_ws4b.db

# Verify dedup (after Polygon gap-fill also complete)
python3 -c "
import sqlite3
conn = sqlite3.connect('data/catalyst_dev_ws4b.db')
groups = conn.execute('''
  SELECT COUNT(*) FROM (
    SELECT dedup_group_id FROM article_tickers
    WHERE dedup_group_id IS NOT NULL
    GROUP BY dedup_group_id HAVING COUNT(DISTINCT article_id) >= 2
  )
''').fetchone()[0]
bad = conn.execute('''
  SELECT dedup_group_id, SUM(is_canonical) as c
  FROM articles a JOIN article_tickers at ON at.article_id = a.article_id
  WHERE at.dedup_group_id IS NOT NULL
  GROUP BY at.dedup_group_id HAVING c != 1
''').fetchall()
print(f'Dedup groups (≥2): {groups}, Bad canonical: {len(bad)}')
conn.close()
"
# Expected: groups > 0, bad = 0

# Verify idempotent
python3 -m catalyst_data.cli_index backfill \
  --from 2026-05-04 --to 2026-07-03 --sources finnhub_company_news \
  --chunk-days 7 --db data/catalyst_dev_ws4b.db
# Expected: cells_total=0
```

### 3.3 SEC (tiny live, update-news, 3 tickers)

```text
# Dry-run
python3 -m catalyst_data.cli_index update-news \
  --from 2026-06-01 --to 2026-07-03 --sources sec_filings \
  --tickers AAPL,JPM,NVDA --db data/catalyst_dev_ws4b.db

# Live
python3 -m catalyst_data.cli_index update-news \
  --from 2026-06-01 --to 2026-07-03 --sources sec_filings \
  --tickers AAPL,JPM,NVDA --live --confirm --db data/catalyst_dev_ws4b.db
# Expected: filings > 0, documents > 0

# Verify EX-99.1 + Bronze HTML
python3 -c "
import sqlite3, zlib
conn = sqlite3.connect('data/catalyst_dev_ws4b.db')
row = conn.execute('''
  SELECT fd.document_type, ra.content_raw
  FROM filing_documents fd JOIN raw_assets ra ON ra.asset_id = fd.raw_asset_id
  WHERE fd.document_type = 'exhibit_99_1' LIMIT 1
''').fetchone()
if row:
    html = zlib.decompress(row[1]).decode('utf-8', errors='replace')
    print(f'Type: {row[0]}, HTML len: {len(html)}, has <html: {chr(60)}html in html.lower()}')
conn.close()
"
# Expected: document_type='exhibit_99_1', contains '<html', non-trivial length
```

### 3.4 FRED (update-macro)

```text
# Dry-run
python3 -m catalyst_data.cli_index update-macro --db data/catalyst_dev_ws4b.db

# Live
python3 -m catalyst_data.cli_index update-macro \
  --live --confirm --db data/catalyst_dev_ws4b.db
# Expected: 11 series fetched, observations_upserted > 0, T10Y2Y derived

# Verify PIT
python3 -c "
import sqlite3
conn = sqlite3.connect('data/catalyst_dev_ws4b.db')
nulls = conn.execute('SELECT COUNT(*) FROM macro_observations WHERE released_at IS NULL').fetchone()[0]
later = conn.execute('SELECT COUNT(*) FROM macro_observations WHERE released_at > observation_date').fetchone()[0]
total = conn.execute('SELECT COUNT(*) FROM macro_observations').fetchone()[0]
asof = conn.execute(\"SELECT COUNT(*) FROM macro_observations WHERE released_at <= '2025-01-01'\").fetchone()[0]
print(f'NULL released_at: {nulls}, later: {later}/{total}, as_of 2025-01-01: {asof}')
conn.close()
"
# Expected: nulls=0, later > 0, asof < total
```

### 3.5 Close the Loop

```text
# Re-run 3F.1 coverage audit
python3 -m catalyst_data.coverage_audit --db data/catalyst_dev_ws4b.db

# Full data-core test suite
cd packages/data-core && python3 -m pytest tests/ -q --timeout=60

# Frozen DB guard
shasum -a 256 data/catalyst_eval_frozen_v2.db
# Must still be: 0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf

# App/agents untouched
git diff --stat packages/app/ packages/agents/
# Expected: empty
```

---

## 4. Implementation Tasks (Sequential)

### Phase 1 — Write All Failing Tests (~6 tasks)

1. Create `tests/test_3f2_polygon_live.py` → TPL1–TPL7 → run, all FAIL.
2. Create `tests/test_3f2_finnhub_live.py` → TFL1–TFL6 → run, all FAIL.
3. Create `tests/test_3f2_sec_live.py` → TSL1–TSL5 → run, all FAIL.
4. Create `tests/test_3f2_fred_live.py` → TML1–TML8 → run, all FAIL.
5. Create `tests/test_3f2_integration.py` → TII1–TII2 → run, all FAIL.
6. Collect failing output for all 7 discriminating tests — paste in report.

### Phase 2 — Fix B3 (Polygon fetch_fn Routing, ~2 tasks)

7. In `update_pipeline.py:~706`, add `elif isinstance(fetch_fn, dict) and src in fetch_fn:
   cell_fetch_fn = fetch_fn[src]` before the finnhub/sec branches.
8. Run TPL1–TPL7 → TPL1, TPL2 PASS (discriminating). TII1, TII2 still FAIL (B2 not fixed yet).

### Phase 3 — Fix B2 (Post-Batch Order, ~2 tasks)

9. In `update_pipeline.py:~755–795`, reorder post-batch: polygon_rederive → finnhub_rederive →
   classify_articles → compute_cross_source_dedup → regenerate_clean → incremental index.
   For classification, call classify_articles unconditionally (not only if polygon in sources).
   For dedup, check if BOTH polygon AND finnhub in sources (or if article_tickers has NULL
   dedup_group_id rows from prior data).
10. Run TII1 → **PASS (discriminating: was FAILING because ordering was wrong).**
    Run TII2 → PASS. Run all polygon + finnhub tests → all pass.

### Phase 4 — Fix B1 (FRED Live Orchestration, ~2 tasks)

11. In `cli_index.py:cmd_update_macro`, replace the stub (~lines 505–514) with:
    - Resolve series (from --series arg or FETCHED_SERIES).
    - Loop: for each series, `await fetch_fn(series_id, "observations", None, output_type=4,
      realtime_start="1776-07-04", realtime_end="9999-12-31")`.
    - Serialize result.data to JSON bytes, compute asset_id, `upsert_raw_asset`.
    - After loop: `rederive_fred_macro(db_path)`.
    - Print summary.
12. Run TML1 → **PASS (discriminating: was FAILING because stub returned nothing).**
    Run all FRED tests → all pass.

### Phase 5 — Full Regression (~2 tasks)

13. Run full data-core test suite: `python3 -m pytest tests/ -q --timeout=60` → all ~530 tests green.
14. Run 3F.1 audit (read-only) on dev DB to confirm pre-live state. Report headline numbers.

### Phase 6 — Human Runbook (§3)

15. Human executes §3.0–3.5 with real API keys.
16. Human reports all command outputs + post-ingest audit.
17. Assert §5 gates hold. Stage. Human commits.

---

## 5. Pass/Fail Gates (from spec §8)

1. **Coverage complete:** polygon_news days_stale ≤ 2 through latest trading day.
2. **All three planes live:** SEC filings > 0 with ≥1 EX-99.1 (Bronze HTML proven);
   Finnhub articles > 0 with cross-source dedup (one-canonical-per-group);
   macro_observations > 0 with per-observation released_at (not fetch date).
3. **No duplicate explosion:** repeat run 0 net new raw_assets; one-canonical-per-group.
4. **Tests green:** full data-core suite + all new 3F.2 tests, each discriminating test
   bite-proven (failing-before output collected).
5. **Frozen DB SHA unchanged:** `0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf`.
6. **Dev DB only:** mutated via human-run --live commands. No automated test writes to it.
7. **No secrets:** zero API keys in code, fixtures, logs. All redacted to [REDACTED].
8. **data-core only:** no app/agents changes.

---

## 6. Open Questions

1. **Polygon 362 skipped cells:** Re-tried during gap-fill. If Polygon daily cap blocks some,
   they remain skipped — acceptable. Document final skipped count in post-ingest audit.

2. **Finnhub daily quota:** Unknown hard cap. 600 requests (10 tickers × 60 days) should fit
   free tier. If 429 persists, resume from checkpoint next day.

3. **SEC full backfill:** DEFERRED beyond 3F.2. Tiny live proves pipeline. Full backfill
   gated behind 3F.3 invariants.

4. **Dev DB backup:** `catalyst_dev_ws4b_backup_YYYYMMDD.db` — local only, NOT committed.
   Delete after 3F.2 confirmed green.

5. **FRED connector output_type=4:** The existing `connectors/fred.py` `fetch()` passes extra
   kwargs to the API. Verified it supports `output_type=4`, `realtime_start`, `realtime_end`.
   If the connector needs a parameter signature adjustment, it's a minor fix in
   `connectors/fred.py` (add to scope).

6. **Retry-After regression:** The 3D fix (dbb7f67) handles Retry-After in the Finnhub
   connector. TFL2 re-asserts with a discriminating test.

---

## Constraints (Standing)

- data-core only; never touch packages/app or packages/agents (leave dirty files untouched
  — no reset/revert/clean/stash).
- Frozen DB SHA: `0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf`
- Dev DB only (catalyst_dev_ws4b.db); no writes to frozen DB.
- No new dependencies (httpx + stdlib). No network in automated tests.
- Never log API keys — redact to [REDACTED].
- English only; Conventional Commits; stage-only, human commits.
- No embeddings/LanceDB on 8GB Mac.

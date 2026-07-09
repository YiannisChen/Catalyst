# WS4B Step 3F.1 — Readiness (Coverage Audit, Schema Reconciliation, Gated Live Runner)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task-by-task. Parts A, B, and C are SEQUENTIAL (B and C both edit cli_index.py —
> do NOT parallelize across parts). Within Part A, subagents are OK for independent audit
> dimensions.

**Goal:** Prepare the codebase for live validation — audit the current corpus state
(read-only, today-anchored staleness oracle), reconcile the dev DB schema to match the
codebase DDL (additive-only, code as oracle), and build the gated `--live --confirm` CLI
path (mock-tested, no real network).

**Architecture:** Three SEQUENTIAL parts (B and C share cli_index.py edits, must not
conflict): (A) a read-only audit module that opens the dev DB with `PRAGMA query_only=ON`
and writes a JSON report to `data/provider_discovery/` (gitignored) using today's trading
calendar as the staleness reference; (B) a dry-run diff-then-apply schema reconciliation
that diff's the dev DB against a fresh code-derived schema (not a hardcoded list) and
additively creates the missing `macro_observations` table; (C) a `--live --confirm` flag
added to three CLI subcommands that constructs real connectors with key/limiter/client and
injects `fetch_fn` into the existing pipeline — gated behind five ordered guards
(confirm-short-circuit first).

**Tech Stack:** Python 3.12, sqlite3, httpx, stdlib only. No new dependencies.

**Baseline:** `ws4b/article-level-data` branch. Frozen DB SHA:
`0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf`.

---

## Changelog from v1 (orchestrator review)

| # | Finding | Change |
|---|---------|--------|
| F1 | Staleness oracle was circular (OHLCV max == articles max → days_stale=0) | D3/D4 now anchor to TODAY's latest closed trading day with weekend + US-market-holiday adjustment. Still report `local_ohlcv_watermark` as a separate field. Add discriminating bite-check test. |
| F2 | D5/D6 query article_tickers.dedup_group_id → crash on real DB (column absent) | D5/D6 degrade gracefully on missing column: report `"dedup_not_materialized"`. Audit reads whatever exists; dedup materialization state is itself an audit finding. Add test for missing-column path. |
| F3 | Part B and C both edit cli_index.py → not safely parallelizable | Parts A, B, C are now SEQUENTIAL. Removed parallelization notes. Updated implementation-order section. |
| F4 | Schema-diff oracle was hardcoded → can drift from real DDL | Oracle is now CODE-DERIVED: build fresh temp DB via `init_db` + `ensure_*` functions, read its `sqlite_master`, diff dev DB against THAT. TB1/TB2 restated accordingly. |
| F5 | Guard order: env-key check ran before --confirm short-circuit → inconsistent with TC1 | Guard order re-sequenced: `--confirm` short-circuit FIRST (exits 0 with friendly message even if keys missing), THEN env-key checks. TC1/TC3 now mutually consistent. |
| F6 | D5 provider-native-ID collision grouped by article_id (PK) → tautologically zero | D5 now groups by per-provider native-id extraction from article_id (split on ":" prefix), or by (provider, article_url) for URL-based collision check. Not tautological. |
| F7 | Frozen-DB guard "by SHA match" hashes 117MB file per call | Guard now uses `os.path.realpath()` comparison. SHA assertion still runs once in before/after integrity check only. |
| F8 | Gitignore covered reports but not untracked scripts | `.gitignore` now also covers `scripts/provider_discovery.py` and `packages/data-core/scripts/fmp_reprobe.py` (spec §7). |
| O1 | Audit location question | Confirmed: `catalyst_data/coverage_audit.py` |
| O2 | NYSE calendar question (REJECTED by F1) | See F1 — today-anchored with holiday list |
| O3 | reconcile-schema default DB | `--apply` REQUIRES explicit `--db`; `--dry-run` defaults to dev DB |
| O4 | Shared httpx client | Deferred to 3F.2 |
| O5 | Backfill chunk size | 7-day default confirmed |

---

---

## Part A — Read-Only Coverage/Integrity Audit

### Architecture

A new module at `catalyst_data/coverage_audit.py` with a single entrypoint
`run_coverage_audit(db_path: str, output_dir: str | None) -> dict`. Opens the dev DB with
`PRAGMA query_only=ON`, runs nine audit queries, writes a JSON report to
`data/provider_discovery/step3f_coverage_YYYYMMDD_HHMMSS.json`, prints a human-readable
summary to stdout, and returns the report dict. Zero writes to any `.db` file.

The report dir is verified to exist; the dir itself is added to `.gitignore` along with
the untracked scripts from spec §7. A `README.md` placeholder explains the purpose.

### Staleness Oracle (AMENDED — F1)

D3 and D4 anchor to TODAY's latest closed trading day, NOT to `MAX(ohlcv.date)`.
The oracle is computed as:

1. Get today's date (UTC).
2. Step backward to the most recent weekday (Mon–Fri).
3. Check against a small hardcoded US-market-holiday list (New Year's Day, MLK Day,
   Presidents' Day, Memorial Day, Juneteenth, Independence Day, Labor Day,
   Thanksgiving, Christmas — using observed dates where applicable). If the candidate
   trading day falls on a holiday, step back one more weekday.
4. Return the result as `latest_closed_trading_day` (ISO format).

This is US-market-aware but dependency-free (a ~15-line function with a frozenset of
holiday date strings). No `pandas_market_calendars`, no new dependency.

The `MAX(ohlcv.date)` value is still reported as a separate field
`local_ohlcv_watermark` — it is informative (shows how stale the local OHLCV data is)
but is NOT used as the staleness reference.

### Audit Dimensions (Contract per Spec §2)

Each dimension produces a named key in the output JSON. The module function runs all nine
in sequence, catching individual dimension failures so one broken query doesn't block
others.

**D1 — Per-source × per-table counts.** Query every relevant table (`raw_assets`,
`clean_assets`, `articles`, `article_tickers`, `filings`, `filing_documents`,
`macro_observations`, `index_state`, `index_manifests`, `source_checkpoints`,
`ohlcv`), group by `source_type` or `source_kind` as appropriate. Missing table →
caught `sqlite3.OperationalError`, reported as `"TABLE_MISSING"` (same pattern as
macro_observations handling). Output: `{table_name: [{"source_type": ..., "count": N}]
| "TABLE_MISSING"}`.

**D2 — Per-ticker × per-source article counts.** Query `articles` joined with
`article_tickers`, group by `(ticker, provider)` or `(ticker, source_type)`. Include
`MIN(published_utc)`, `MAX(published_utc)` per group. Output: list of dicts.

**D3 — Date coverage (AMENDED — today-anchored staleness reference via F1).** For each
source_type, compute `MIN(reference_date)` and `MAX(reference_date)` from
`article_tickers` (and `filings.filed_at` for SEC). Compare `MAX` against
`latest_closed_trading_day` (computed as described above). Report `local_ohlcv_watermark`
separately. Output: `{source_type: {min_date, max_date, latest_trading_day, days_stale,
local_ohlcv_watermark}}`.

**D4 — Missing ranges (trading-day gaps — AMENDED: today-anchored reference).**
For each (ticker, source_type) pair where articles exist, compute sorted
`reference_date` values, detect gaps where two consecutive dates are more than 1 trading
day apart, report gap intervals. Also report the trailing gap from `MAX(reference_date)`
to `latest_closed_trading_day`. Distinguish "internal gap" (gap between two fetched dates)
from "trailing gap" (stale end). Output: list of `{ticker, source_type, gap_start,
gap_end, gap_type}`.

**D5 — Duplicate diagnostics (AMENDED — handles missing column + uses real collision
check per F2, F6).** Degrades gracefully if `article_tickers.dedup_group_id` column is
absent (report `"dedup_not_materialized"`). Sub-queries:

- Count of `dedup_group_id` groups with ≥2 members (from `article_tickers` if column
  exists; from `articles.dedup_group_id` if not — state which column was used).
- Count of articles with `is_canonical=0`.
- Provider-native-ID collisions: extract the per-provider native ID by splitting
  `article_id` on ":" (e.g., `poly:abc123` → `abc123`), group by `(provider, native_id)`,
  count groups with >1 row. This is NOT tautological — the PK is the FULL namespaced
  `article_id`, not the native portion.
- Canonical-URL collisions: groups of articles with identical `article_url` and different
  `article_id`.

Output: dict with counts + collision examples + `dedup_materialization` field.

**D6 — Canonical counts (AMENDED — handles missing column per F2).** Count `is_canonical=1`
per source_type + provider. Count multi-ticker articles: `article_id` appearing in >1
`article_tickers` row. Verify multi-ticker articles are preserved (should be non-zero for
Polygon, zero for Finnhub). If `article_tickers.dedup_group_id` is absent, note it but
don't crash — canonical counts are still reportable from `articles.is_canonical`.

**D7 — Checkpoint reconciliation.** Query `source_checkpoints` grouped by
`(source_type, status)` for a histogram. Compare checkpoint `date` against actual max
`reference_date` from data tables for each (ticker, source) pair. Flag any checkpoint
marked `success` but with absent data. Output: histogram + gap report.

**D8 — Rederivability spot-check.** For `N=20` random `article_id` values, look up
`raw_asset_id`, fetch `content_raw` from `raw_assets`, decompress (zlib), parse JSON,
assert the decompressed payload contains the article's `title` as a substring. Output:
`{spot_checked: N, passed: M, failed_samples: [...]}`.

**D9 — Source-tier distribution.** Histogram of `source_tier` across `articles` (grouped
by `provider`) plus `filings` (where `source_tier=1`). Flag any NULL tiers or tiers
outside {1..6}. Output: dict.

### Failure Modes + Bite-Checks

| Failure Mode | How the Audit Detects It | Discriminating Bite-Check |
|---|---|---|
| Staleness oracle circular (OHLCV max == articles max → days_stale=0) | **AMENDED F1:** D3 compares against today-anchored `latest_closed_trading_day`, NOT `MAX(ohlcv.date)` | Test: mock DB where ohlcv max == articles max == 60 days ago → `days_stale` must be ~60, not 0. The test would FAIL if staleness used the OHLCV watermark. |
| article_tickers.dedup_group_id column missing → crash | **AMENDED F2:** D5/D6 probe column existence first; report `"dedup_not_materialized"` if absent | Test: create DB without the column (matching the real dev DB state) → D5/D6 produce report with `dedup_materialization: false`, no crash |
| provider-native-ID collision query tautological (grouped by article_id PK) | **AMENDED F6:** D5 now splits article_id on ":" prefix to extract native ID, groups by `(provider, native_id)` | Test: insert two articles with different article_id but same native portion (e.g., `poly:abc` and `poly:abc` is impossible — instead insert `poly:dup1` and `poly:dup1` — PK blocks this. The REAL test: insert two articles where one article_id contains the same native id as another `article_url` → URL collision detected) |
| Stale corpus (max date << today) | D3 `days_stale` computed against today's trading day, not OHLCV → catches ~60 days of staleness on the real DB | Test: mock articles ending 45 trading days before today → `days_stale >= 45` |
| dedup_group_id groups with zero canonical | D5 counts groups, D6 counts canonical per group → mismatch flagged | Test: insert three articles with same articles.dedup_group_id, all `is_canonical=0` → report shows group_count=1, canonical_count=0 |
| Multi-ticker article associations lost | D6 counts article_tickers rows per article_id | Test: insert article with 2 article_tickers rows → `multi_ticker_count >= 1` |
| Source-tier NULL detected | D9 counts NULL tiers → `null_tier_count > 0` | Test: insert article with `source_tier=NULL` → D9 flags it |
| Frozen DB accidentally opened | Audit function checks `db_path` realpath against frozen DB realpath, refuses | Test: pass frozen DB path → `RuntimeError` with "frozen" |
| Rederivability check always-passes (tautological) | D8 intentionally uses a mix of valid and invalid raw_assets in the test DB | TA11: insert a raw_asset with mismatched title → `failed_samples` non-empty. Proves D8 actually catches failures. |

### Key Constraint: `PRAGMA query_only=ON`

Opens dev DB with `PRAGMA query_only=ON` immediately after connection. Test (TA1) attempts
a write and asserts `OperationalError`.

### Output

- JSON file: `data/provider_discovery/step3f_coverage_YYYYMMDD_HHMMSS.json`
- Stdout: human-readable summary with anomaly-flagged lines
- Return value: full report dict

---

## Part B — Idempotent Additive Schema Reconciliation

### Architecture

A new CLI command `reconcile-schema` on `cli_index.py`, with two modes:

- **`--dry-run`** (default, dev DB path): builds a FRESH temp DB via `init_db` +
  `ensure_*_tables` (the CODE as oracle), reads its `sqlite_master`, diffs the dev DB
  against that code-derived schema, and prints which tables/columns/indexes are missing.
  Zero writes.
- **`--apply --db <PATH>`** (explicit DB required for writes): runs `ensure_macro_tables
  (conn)` to create `macro_observations`, runs `_migrate_article_tickers_dedup(conn)` if
  the dedup_group_id column is absent, re-runs the diff to confirm zero remaining gaps.
  Additive-only — never drops, never alters existing columns beyond adding new ones.

### Code-as-Oracle Diff (AMENDED — F4)

The dry-run does NOT compare against a hardcoded list. Instead:

1. Create an in-memory temp DB (`:memory:`).
2. Run `init_db(temp_conn)` — this calls all `ensure_*_tables` functions and creates the
   complete current schema as defined by the codebase.
3. Query `sqlite_master` on the temp DB to extract all table DDL, column names per table,
   and index names.
4. Query `sqlite_master` on the dev DB for the same.
5. Diff: tables in code but not in dev → "MISSING TABLE"; columns in code but not in dev
   per table → "MISSING COLUMN"; indexes in code but not in dev → "MISSING INDEX".
6. Report the diff. If empty, schema is current.

This guarantees the diff oracle can never drift from the code — it IS the code.

### What It Reconciles

The current dev DB is known to be missing `macro_observations` (from the 3E DDL) and the
`article_tickers.dedup_group_id` column (from the 3D migration). The reconciliation:

1. Runs `_migrate_article_tickers_dedup(conn)` if column absent (handles pre-3D DBs).
2. Runs `ensure_macro_tables(conn)` to create `macro_observations` + indexes.
3. Re-runs the code-derived diff to confirm zero remaining gaps.

### Apply Mode

- `--apply` REQUIRES `--db <PATH>` (no silent default to dev DB — the write path must be
  explicit, per orchestrator answer O3).
- Opens the named DB normally (not query_only).
- Runs the two additive steps above.
- Commits.
- Re-runs the code-derived diff to confirm all objects exist.
- Refuses if the resolved path matches the frozen DB realpath.

### Failure Modes + Bite-Checks

| Failure Mode | How It's Detected | Discriminating Bite-Check |
|---|---|---|
| Hardcoded oracle drifts from code | **AMENDED F4:** Oracle IS the code (fresh temp DB) → cannot drift | TB1/TB2: temp DB is built by the same `init_db` call the test uses. If DDL changes, both drift together → catchable only by integration tests. The bite-check: TB2 creates a DB with `init_db` and asserts diff is empty. If a new table is added to the code but the test DB was built with old DDL, TB1 catches it. |
| Schema already current | Dry-run reports zero missing objects | TB2: build DB with `init_db` (current code) → diff is empty |
| Frozen DB passed to --apply | Refused with RuntimeError | TB5: pass frozen DB realpath → RuntimeError |
| --apply without --db | Refused: "ERROR: --apply requires --db <PATH>" | Test: call with --apply only → exits 1 with message |
| Migration fails mid-way | Exception caught, error message names the failed step | Test: mock connection that raises on ALTER TABLE → error message contains which step failed |

---

## Part C — Gated Live Runner (`--live --confirm`)

### Architecture

A shared helper module `catalyst_data/live_runner.py` that constructs real connectors,
limiters, and httpx clients, then injects them into the existing pipeline. The CLI
subcommands (`update-news`, `backfill`, `update-macro`) gain `--live` and `--confirm`
flags that call into this module. The default stays dry-run.

### Guard Order (AMENDED — F5: --confirm short-circuit FIRST)

The `--live` path enforces guards in THIS order (re-sequenced so `--confirm` gate
short-circuits BEFORE env-key enforcement, making TC1 and TC3 mutually consistent):

1. **`--confirm` short-circuit.** If `--live` is set but `--confirm` is NOT: print
   a friendly message ("Add --confirm to execute live network calls"), optionally show
   a redacted config preview (which sources would be used, key presence detected), exit
   **0**. This step must NOT crash even if environment keys are entirely missing — the
   user is asking for a preview, not execution.
2. **Frozen DB guard.** Resolve `db_path` via `os.path.realpath()`, compare against the
   frozen DB realpath. Refuse with clear message. **(AMENDED F7: realpath comparison,
   not SHA hash — the SHA is only checked in the before/after integrity assertion.)**
3. **Environment key guard.** For each source requested via `--sources`, check that the
   corresponding env var is set and non-empty. Missing key → `ValueError` naming the env
   var. Only the keys needed for the requested sources are checked (not all keys
   unconditionally).
4. **Redacted config echo.** Print a config summary to stdout: each source, key truncated
   to first-4 + last-4 characters (or "[not set]" if missing, or "[hidden]" if <8 chars),
   rate policy, limiter concurrency. Full keys never appear.
5. **No key in logs/errors.** Every ValueError/RuntimeError message sanitizes key-like
   strings before printing.

### Live Runner Module Contract

`catalyst_data/live_runner.py` exposes one function per source type:

- `build_polygon_fetcher() -> (fetch_fn, limiter)` — reads `POLYGON_API_KEY`, constructs
  `TokenBucketLimiter` from `provider_limits.POLYGON`, returns result.
- `build_finnhub_fetcher() -> (fetch_fn, limiter)` — reads `FINNHUB_API_KEY`.
- `build_sec_fetcher() -> (fetcher_ns, limiter)` — reads `SEC_USER_AGENT`.
- `build_fred_fetcher() -> (fetch_fn, limiter)` — reads `FRED_API_KEY`.

Each raises `ValueError` if the required env var is missing/empty. Each echoes the
redacted config to stdout on construction.

### CLI Changes (AMENDED — sequenced AFTER Part B to avoid cli_index.py conflicts, per F3)

Three subcommands modified (all in `cli_index.py`):

**`update-news`:**
- New flags: `--live`, `--confirm`
- Guard flow: confirm short-circuit → frozen DB check → env key check → config echo →
  connector construction → `run_update_batch(db_path, fetch_fn=..., dry_run=False)`
- Default: dry-run unchanged

**`backfill`:**
- Same flag pattern and guard flow
- Default: dry-run unchanged

**`update-macro`:**
- Same flag pattern
- Live flow: guard checks → FRED fetcher → per-series fetch with `output_type=4` params
  → `upsert_raw_asset` for each response → `rederive_fred_macro()` → T10Y2Y derivation
- Default: dry-run unchanged

### Live Fetch Flow (Common Pattern)

1. Resolve `db_path` → confirm gate → frozen DB check (realpath)
2. Parse `--sources` / `--series` → determine required env keys
3. Check env keys (only for requested sources)
4. Echo redacted config summary
5. Build limiters and connectors via `live_runner.py`
6. Call existing pipeline function with `fetch_fn` and `dry_run=False`
7. Print result summary
8. Note post-batch steps ran (rederive, dedup, classify, regenerate_clean)

### Mock-Testing (Zero Network)

All Part C tests use mocked `httpx.AsyncClient` or `unittest.mock.AsyncMock`. Verify:

- Guard order respected (TC1 exits before key check)
- Frozen DB refused (TC2)
- Missing key raises (TC3)
- Redaction hides keys (TC4)
- Connectors constructed correctly (TC5, TC6)
- Default dry-run unchanged (TC7)
- Pipeline accepts live fetch_fn (TC8)

---

---

## TDD Test Plan (Test Names + What Each Asserts)

### Part A Tests (`tests/test_coverage_audit.py`, ~14 tests)

**TA1 — `test_query_only_pragma_enforced`:** Open DB with query_only=ON, attempt INSERT →
`sqlite3.OperationalError`. Proves read-only.

**TA2 — `test_frozen_db_refused`:** Pass frozen DB realpath → `RuntimeError` with "frozen".

**TA3 — `test_per_source_table_counts`:** Mock DB with known counts → D1 output matches.

**TA4 — `test_staleness_against_today_not_ohlcv` (AMENDED F1, discriminating):** Mock DB:
`MAX(articles.published_utc)` = `MAX(ohlcv.date)` = 60 days before today's
`latest_closed_trading_day`. Run D3 → `days_stale >= 60`, and
`local_ohlcv_watermark` = the stale OHLCV date. If staleness were computed against
OHLCV, `days_stale` would be 0 → this test would FAIL against the old circular logic.

**TA5 — `test_past_holiday_skipped_in_trading_day`:** Today is a Monday after a
Friday holiday. `latest_closed_trading_day` should be Thursday, not Friday (the
holiday). Proves the holiday list works correctly (not just weekday logic).

**TA6 — `test_macro_table_missing_handled`:** DB without `macro_observations` → D1
reports `"TABLE_MISSING"` not a crash.

**TA7 — `test_dedup_column_missing_handled` (AMENDED F2, discriminating):** DB where
`article_tickers` has no `dedup_group_id` column → D5/D6 produce report with
`dedup_materialization: false`, NO crash. Current behavior (before fix): would crash
with "no such column".

**TA8 — `test_dedup_group_zero_canonical_detected`:** Three articles with same
`articles.dedup_group_id`, all `is_canonical=0` → D5/D6 show mismatch.

**TA9 — `test_multi_ticker_preserved_count`:** Article with two `article_tickers` rows
→ D6 `multi_ticker_count >= 1`.

**TA10 — `test_null_tier_detected`:** Article with `source_tier=NULL` → D9 flags it.

**TA11 — `test_checkpoint_histogram`:** Three checkpoints → D7 histogram matches.

**TA12 — `test_rederivability_spot_check`:** Matching raw_asset + article → D8
`passed == N`.

**TA13 — `test_rederivability_failure_detected` (discriminating):** Mismatched
raw_asset title → D8 `failed_samples` non-empty. Proves D8 isn't always-pass.

**TA14 — `test_native_id_collision_detected` (AMENDED F6, discriminating):** Insert
two articles with different article_id but same native-id portion and same provider
(e.g., same `article_url`, different `article_id` prefixes since PK allows it — wait,
the PK is `article_id`, so duplicates are impossible. Instead: test that D5 groups by
`(provider, native_id)` by inserting articles with intentionally distinct
fully-namespaced IDs that share the same native portion after prefix split. Since PK
prevents literal duplicates, use `article_url` collision instead: two articles with
same `article_url`, different `article_id` → D5 URL-groups with count > 0 show
collision. Proves D5 checks something real, not PK-guaranteed.

**TA15 — `test_output_json_written`:** Full audit on mock DB → valid JSON output with
all dimension keys.

### Part B Tests (`tests/test_schema_reconcile.py`, ~6 tests)

**TB1 — `test_dry_run_reports_macro_missing`:** Create DB with pre-3E DDL. Run
dry-run (code-derived oracle) → reports `macro_observations` table missing. Proves
code-derived diff detects the real gap.

**TB2 — `test_dry_run_no_missing_when_current` (discriminating):** Create DB by
calling `init_db` (code path). Run dry-run → zero missing objects. Proves the
code-derived oracle isn't tautological — the temp DB and real DB source their schema
from the SAME code, so a pre-3E DB (TB1) differs, a current DB (TB2) matches.

**TB3 — `test_apply_creates_macro_table`:** DB without macro_observations. Run apply
→ table exists, indexes exist, queryable via `sqlite_master`.

**TB4 — `test_apply_idempotent` (discriminating):** Run apply twice → second run
reports "already reconciled, no changes".

**TB5 — `test_apply_refuses_frozen_db`:** Pass frozen DB realpath → RuntimeError.

**TB6 — `test_apply_requires_explicit_db`:** Run `--apply` without `--db` → exits 1
with "requires --db" message.

### Part C Tests (`tests/test_live_runner.py`, ~8 tests)

**TC1 — `test_live_without_confirm_exits_early_even_without_keys` (AMENDED F5,
discriminating):** Call `--live` without `--confirm`, with ALL env keys UNSET. Exits 0,
stdout contains "Add --confirm". Proves the confirm short-circuit runs BEFORE env-key
checks — if guard order were wrong, this would crash with missing-key ValueError.

**TC2 — `test_frozen_db_refused`:** `--live --confirm` with frozen DB realpath →
RuntimeError.

**TC3 — `test_missing_api_key_raises_after_confirm`:** `--live --confirm` with
`POLYGON_API_KEY` unset → `ValueError` naming the missing var. Proves env-key guard
fires only after confirm gate passes.

**TC4 — `test_redacted_config_echo`:** Set `POLYGON_API_KEY=abcdef1234567890abcdef`.
Call `build_polygon_fetcher()` → stdout contains `key=abcd...cdef`, NOT the full key.

**TC5 — `test_polygon_fetcher_constructed`:** Valid mock env → returns callable with
correct signature, limiter is `TokenBucketLimiter`.

**TC6 — `test_finnhub_fetcher_constructed`:** Same pattern.

**TC7 — `test_dry_run_default_unchanged`:** No `--live` flag → dry-run behavior.

**TC8 — `test_live_pipeline_accepts_fetch_fn`:** Mocked fetch_fn → pipeline runs,
returns expected result dict, checkpoint written.

---

---

## Implementation Order (Bite-Sized Tasks — SEQUENTIAL per Part)

Parts are SEQUENTIAL: A → B → C (B and C both modify `cli_index.py`, per F3).

### Phase 1 — Part A: Coverage Audit (~12 tasks)

1. Add `.gitignore` entries: `data/provider_discovery/*.json`,
   `scripts/provider_discovery.py`, `packages/data-core/scripts/fmp_reprobe.py`.
   Create `data/provider_discovery/README.md` placeholder. **(AMENDED F8)**
2. Write TA1 (query_only pragma) → FAIL.
3. Skeleton `run_coverage_audit()` with query_only → TA1 PASS.
4. Write TA2–TA15 (all Part A tests) → all FAIL.
5. Implement trading-day oracle (today-anchored + holiday list) → TA5 PASS.
6. Implement D1 (per-source table counts, missing-table handling) → TA3, TA6 PASS.
7. Implement D2 (per-ticker) → TA9 PASS.
8. Implement D3 (date coverage, today-anchored) → **TA4 PASS (discriminating: days_stale ~60, not 0).**
9. Implement D4 (missing ranges with trailing gap) → verify on mock DB.
10. Implement D5 (duplicates, missing-column handling, native-ID collision via url) →
    **TA7 PASS (discriminating: no crash on missing dedup_group_id),** TA14 PASS
    (URL collision detected).
11. Implement D6 (canonical counts, missing-column graceful degradation) → TA8 PASS.
12. Implement D7 (checkpoint reconciliation) → TA11 PASS.
13. Implement D8 (rederivability spot-check) → TA12, **TA13 PASS (discriminating:
    catches mismatches).**
14. Implement D9 (source-tier distribution) → TA10 PASS.
15. Implement JSON output + stdout summary → TA15 PASS.
16. Run full audit on real dev DB (read-only, safe). Inspect report.

### Phase 2 — Part B: Schema Reconciliation (~6 tasks)

17. Write TB1–TB6 (all Part B tests) → all FAIL.
18. Add `reconcile-schema` subparser to `cli_index.py` (skeleton, argparse-only).
19. Implement code-derived diff (fresh temp DB via `init_db`, compare `sqlite_master`) →
    TB1, TB2 PASS.
20. Implement apply logic (explicit `--db` required, `ensure_macro_tables` +
    `_migrate_article_tickers_dedup`) → TB3, TB4, TB6 PASS.
21. Implement frozen DB refusal → TB5 PASS.
22. Run `reconcile-schema --apply --db data/catalyst_dev_ws4b.db` → `macro_observations`
    created, `dedup_group_id` column added.

### Phase 3 — Part C: Gated Live Runner (~8 tasks)

23. Write TC1–TC8 (all Part C tests) → all FAIL.
24. Create `catalyst_data/live_runner.py` with `build_*_fetcher` functions (skeleton).
25. Implement guard order: confirm short-circuit first, then frozen DB, then env keys →
    **TC1 PASS (discriminating: exits 0 without keys),** TC2 PASS, TC3 PASS.
26. Implement redacted config echo → TC4 PASS.
27. Implement connector construction → TC5, TC6 PASS.
28. Wire `--live --confirm` into `cli_index.py` `update-news`, `backfill`, `update-macro`
    (these edits are safe since Part B's subparser edits are already committed).
29. Verify default dry-run unchanged → TC7 PASS.
30. Verify live pipeline integration → TC8 PASS.

### Phase 4 — Full Verification (~2 tasks)

31. Run full data-core test suite (all existing + 28 new 3F.1 tests) → all green.
32. Verify frozen DB SHA unchanged (`0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf`).
33. Verify `--dry-run` default preserved on all three subcommands.
34. Stage all changes. Human commits.

---

## Discriminating Bite-Check Summary

Every guard/invariant test has a paired counter-proof:

| Bite-Check | Why It Bites | What Broken Logic It Catches |
|---|---|---|
| **TA4** (staleness computed against today, not OHLCV) | Mock: ohlcv max == articles max == 60d ago, today's watermark = now → days_stale must be ~60 | Catches: circular staleness oracle. If computed against OHLCV, days_stale=0. |
| **TA7** (missing dedup_group_id column → no crash) | DB without the column → D5/D6 report `dedup_not_materialized` | Catches: assuming schema state that doesn't exist on the real dev DB |
| **TA13** (rederivability catches failures) | Raw asset with mismatched title → D8 reports failure | Catches: D8 always-passing tautologically |
| **TA14** (native-ID URL collision detected) | Two articles, same URL, different article_id → D5 reports URL collision | Catches: collision check grouping by PK (always zero) |
| **TB2** (code-derived oracle reports zero diff when current) | DB built via init_db → diff is empty | Catches: oracle hardcoded differently from code |
| **TB4** (apply idempotent) | Second apply reports "already reconciled" | Catches: non-idempotent apply that clobbers |
| **TC1** (confirm gate exits before key check) | `--live` no `--confirm`, no keys set → exits 0 | Catches: guard order putting env-key check before confirm gate |

---

## Open Questions (Resolved)

1. ✅ Audit module location: `catalyst_data/coverage_audit.py`
2. ✅ Staleness oracle: today-anchored with US-market-holiday list (F1)
3. ✅ reconcile-schema `--apply`: REQUIRES explicit `--db` (O3)
4. ✅ Shared httpx client: deferred to 3F.2 (O4)
5. ✅ Backfill chunk size: 7-day accepted (O5)
6. ✅ Frozen DB guard: realpath, not SHA per-call (F7)

## Remaining Assumptions

1. The US-market-holiday hardcoded list covers ~10 dates/year — any holiday not in the
   list means the trading-day oracle falls back to "most recent weekday," which is
   off by at most 1 day. Acceptable for audit purposes.
2. `_migrate_article_tickers_dedup` is idempotent (if column exists, it's a no-op) —
   confirmed from the 3D implementation.
3. The `update-macro --live` flow uses `rederive_fred_macro()` post-fetch — this function
   reads ALL `fred_macro` raw_assets, not just the newly-fetched ones. For 3F.1
   (mock-tested only), this is fine. For 3F.2 live, the T10Y2Y derivation may re-derive
   from all historical DGS10/DGS2 rows — idempotent, but noted.

# WS4B Phase 0 Remediation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development for every behavior change. Use superpowers:systematic-debugging before fixing root-cause items, especially cross-source dedup materialization and checkpoint reconciliation.

**Goal:** Make the current WS4B data-core corpus trustworthy before Step 4 by unifying trading-date semantics, re-materializing prose evidence, materializing dedup, re-fetching FRED with genuine point-in-time release metadata, reconciling checkpoints to materialized data, and enforcing all Phase 0 invariants in one final audit gate.

**Architecture:** Execute one ordered remediation pass with pre-flight backup, TDD-first changes, source-specific materialization, and a fail-stop invariant gate after every phase. The critical path is trading calendar unification, prose rederive, dedup materialization, news checkpoint reconciliation, prose sub-gate, FRED PIT fit-guard/re-ingest, and final coverage audit. FRED PIT re-ingest runs last because macro is Plane-2 and off the embedding-critical path; if FRED fail-stops, the prose sub-gate remains independently reportable. Only P0-4 performs network calls, and every mutating phase runs against the dev DB only after backup and frozen-DB guards.

**Tech Stack:** Python 3.12, sqlite3, pytest, httpx through existing connectors, stdlib date/calendar logic only, existing Catalyst data-core CLI and pipeline modules. No new dependencies.

---

## Non-Negotiable Execution Model

This is a single end-to-end execution plan, not separate approval rounds. After orchestrator review approves this plan, execute phases P0-1 through P0-6 in order, with fail-stop gates. If any gate fails, stop immediately, preserve artifacts, and report the failing invariant; do not proceed to the next phase.

Use TDD per phase: write the bite-test first, run it and prove it fails, implement the minimal root-cause fix, run the bite-test again, then run that phase's targeted tests. Do not write production code before the failing test.

Use systematic debugging before the RC3 and P0-5 fixes: reproduce the no-op or phantom state, trace data flow through the caller, identify the root cause, write a discriminating fixture, then implement.

Commands in this plan are written inline to comply with the no-code-block plan constraint. When executing, run them from the repository root unless a phase explicitly says to run from `packages/data-core`.

## Dependency Graph

P0-1 is the keystone. It must complete before any prose rederive because Polygon and Finnhub both map `published_utc` through a trading calendar. P0-2 depends on P0-1. P0-3 depends on P0-2 because dedup fingerprints require non-blank reference dates and must see the full corrected corpus. P0-5 depends on P0-2 and P0-3, but does not depend on FRED. The prose sub-gate runs after P0-5 and is independently reportable. P0-4 runs after the prose sub-gate because FRED is the only network phase and macro is off the embedding-critical path. P0-6 is the final Gate P0.

Execution order: P0-1 -> P0-2 -> P0-3 -> P0-5 -> Prose Sub-Gate -> P0-4 -> P0-6.

Deterministic materialization order for prose evidence: ingest raw Bronze if live, rederive Silver articles/article_tickers from Bronze, classify source tiers, compute cross-source dedup, reconcile checkpoints to materialized data, audit.

## Current Root Causes To Fix

RC1: Trading-day calendar is OHLCV-bound. `catalyst_data.rederive._load_trading_calendar`, `catalyst_data.pipeline.finnhub_normalize._load_trading_calendar`, `catalyst_data.update_pipeline._trading_days_in_window`, and `catalyst_data.coverage_audit` each carry calendar logic or OHLCV-derived assumptions. This produces three symptoms: Finnhub blank `reference_date`, Polygon blank article_ticker reference dates, and gap-fill windows disappearing after the local OHLCV ceiling.

RC2: FRED PIT is wrong at scale. The current macro rows came from latest-revised/fetch-date semantics. `macro_observations` contains 98,349 invalid rows where `released_at > observation_date`, and only two `released_at` values. Re-derive alone cannot fix this; invalid Bronze must be cleared and FRED must be re-fetched with scoped, genuine point-in-time metadata.

RC3: Cross-source dedup did not materialize in the live corpus. `compute_cross_source_dedup` exists, and recent `run_update_batch` code stages a post-batch dedup call, but the live DB has NULL `dedup_group_id` everywhere. The plan must reproduce why the backfill path failed to run or commit dedup and then materialize it.

RC4: Polygon source availability appears to end around `2026-05-01`. Success checkpoints beyond available data can mask source ceilings. Coverage target must become latest available per source, not always today's latest closed trading day.

## Pre-Flight Controls

Perform these checks before P0-1:

- Confirm branch: `git branch --show-current` must show `ws4b/article-level-data`.
- Confirm dirty scope: `git status --short --branch` must show unrelated `packages/app` and `packages/agents` changes as untouched, and staged R2 data-core changes may already exist.
- Confirm R2 staged files if present: `git diff --cached --name-only` should include only data-core R2 files and any approved plan file.
- Capture frozen SHA: `shasum -a 256 data/catalyst_eval_frozen_v2.db` must equal `0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf`.
- Capture dev DB mtime and size: `stat -f '%m %z %Sm %N' data/catalyst_dev_ws4b.db`.
- Before the first mutating phase, create one backup: `cp data/catalyst_dev_ws4b.db data/catalyst_dev_ws4b.before-phase0-<UTC>.db`.
- Record backup path in the run report.
- Never print `.env`. For P0-4, load keys only through `set -a; . packages/data-core/.env; set +a` in the same shell invocation as the live command, and redact any provider error text to `[REDACTED]`.

Rollback rule: if a mutating phase corrupts the dev DB or fails an invariant after partial mutation, stop and either restore the pre-flight backup or preserve the failed DB copy for inspection, depending on orchestrator instruction. Never touch the frozen DB.

## P0-1 Keystone: Trading Calendar Unification

**Purpose:** Replace scattered OHLCV-bound calendar readers with one authoritative trading-day provider: OHLCV dates union a dependency-free US trading calendar oracle. This module must serve rederive, Finnhub normalize, update missing-cell computation, and coverage audit.

**Mode:** Offline. No network. No dev DB mutation. Tests use temp DBs only.

**Files:**

- Create: `packages/data-core/catalyst_data/trading_calendar.py`
- Modify: `packages/data-core/catalyst_data/rederive.py`
- Modify: `packages/data-core/catalyst_data/pipeline/finnhub_normalize.py`
- Modify: `packages/data-core/catalyst_data/update_pipeline.py`
- Modify: `packages/data-core/catalyst_data/coverage_audit.py`
- Test: `packages/data-core/tests/test_trading_calendar.py`
- Test: `packages/data-core/tests/test_rederive.py`
- Test: `packages/data-core/tests/test_finnhub_normalize.py`
- Test: `packages/data-core/tests/test_update_pipeline.py`
- Test: `packages/data-core/tests/test_coverage_audit.py`

**Design contract:**

- `trading_calendar.py` owns the holiday list and date-window logic.
- The holiday list must align with coverage audit and the R2 calendar fix, including `2026-07-03`.
- Calendar function returns weekdays minus holidays for a requested inclusive window.
- DB-aware function returns sorted unique dates from OHLCV union calendar oracle.
- Re-derive callers must request a calendar extending at least to the maximum publication-derived candidate date in the raw assets, not just to local OHLCV max.
- Weekend and holiday publication mapping follows `map_to_trade_date`: before market close maps to candidate day; after market close maps to next calendar day; then map to the first trading day on or after the candidate.
- If a raw payload has no publication timestamp, preserve existing behavior of blank reference date only for genuinely unalignable article records, but Gate P0 will require zero blank references in materialized corpus.

**TDD bite-tests:**

- Add a Polygon rederive test where OHLCV ends at `2026-05-01` and a raw Polygon article has `published_utc` after that ceiling. It must fail today because `map_to_trade_date` returns `None` and `reference_date` becomes blank. After the fix it maps to a non-blank post-ceiling trading date.
- Add a Finnhub normalize test where OHLCV ends at `2026-05-01` and a Finnhub article timestamp is in June 2026. It must fail today with blank `reference_date`, then pass.
- Keep the R2 partial-OHLCV missing-cell test; it must still pass after moving the logic to `trading_calendar.py`.
- Add a reusable calendar test for `2026-07-02` through `2026-07-06`, expecting `2026-07-03`, weekend dates, and July 4 weekend to be excluded.
- Add a coverage audit test proving latest closed trading day and date coverage use the shared provider rather than a local duplicate holiday set.

**Implementation steps:**

1. Write `test_trading_calendar.py` for calendar oracle holidays, weekends, `from_date > to_date`, and OHLCV union.
2. Run the tests and confirm the new module import fails.
3. Create `trading_calendar.py` with the smallest API needed by callers.
4. Re-run `test_trading_calendar.py` and confirm it passes.
5. Write Polygon post-ceiling rederive failing test in `test_rederive.py`.
6. Run that test and confirm it fails with blank/None `reference_date`.
7. Modify `rederive.py` to use `trading_calendar` and derive a sufficient end date from raw payload publication timestamps.
8. Run the Polygon test and confirm it passes.
9. Write Finnhub post-ceiling failing test in `test_finnhub_normalize.py`.
10. Run it and confirm it fails with blank/None `reference_date`.
11. Modify `finnhub_normalize.py` to use the shared provider and sufficient end date.
12. Run the Finnhub test and confirm it passes.
13. Refactor `update_pipeline.py` to import the shared provider instead of keeping its own holiday set.
14. Refactor `coverage_audit.py` to import the shared provider instead of keeping its own holiday set.
15. Run targeted tests: `python3 -m pytest tests/test_trading_calendar.py tests/test_rederive.py tests/test_finnhub_normalize.py tests/test_update_pipeline.py tests/test_coverage_audit.py -q` from `packages/data-core`.

**Fail-stop gate P0-1:**

- Shared calendar tests pass.
- Polygon and Finnhub post-OHLCV-ceiling bite-tests pass.
- R2 partial-OHLCV test still passes.
- No source file outside data-core is touched.
- No dev DB mtime change.

**Rollback:**

- Revert P0-1 code/test changes only if the shared provider destabilizes existing tests. Do not revert staged R2 unless orchestrator explicitly says to.

## P0-2 Re-Derive Prose Corpus From Existing Bronze

**Purpose:** Re-materialize Finnhub and Polygon prose evidence from existing Bronze only, using the unified calendar, so every article and article_ticker has a trustworthy reference date.

**Mode:** DB mutation. No network. Dev DB only. Requires pre-flight backup.

**Files:**

- Modify or create CLI/script: `packages/data-core/catalyst_data/cli_index.py` or `packages/data-core/scripts/phase0_remediate.py`
- Modify: `packages/data-core/catalyst_data/rederive.py`
- Modify: `packages/data-core/catalyst_data/pipeline/finnhub_normalize.py`
- Test: `packages/data-core/tests/test_phase0_remediate.py`
- Test: `packages/data-core/tests/test_rederive.py`
- Test: `packages/data-core/tests/test_finnhub_normalize.py`

**Design contract:**

- Re-derive from existing `raw_assets` only.
- Do not fetch network data.
- Re-derive is idempotent.
- It may update `articles` and `article_tickers` for `polygon_news` and `finnhub_company_news`.
- It must not mutate SEC, FRED, OHLCV, frozen DB, or index tables.
- After re-deriving Polygon/Finnhub articles, it must regenerate affected clean_assets so Silver stays consistent with corrected articles.
- It must unconditionally reset dedup fields for re-derived prose rows: `dedup_group_id` to NULL on `articles` and `article_tickers`, and `is_canonical` to the default canonical state before P0-3 recomputes dedup over the full corrected corpus.
- It must report before/after counts for blank/NULL `reference_date` in both `articles` and `article_tickers`.

**TDD bite-tests:**

- Temp DB with existing Polygon raw asset after local OHLCV ceiling produces blank reference date before P0-1, then P0-2 rederive produces non-blank reference date.
- Temp DB with existing Finnhub raw asset after local OHLCV ceiling rederives non-blank reference date derived from `published_utc`, not from raw asset `reference_date` or fetch cell date.
- Idempotency test: running the remediation twice leaves article and article_ticker counts unchanged and zero blank references.
- Scope test: SEC filings, FRED macro rows, OHLCV, and index_state counts are unchanged by prose rederive.
- Clean asset consistency test: affected Polygon clean_assets are regenerated from corrected article/reference data.
- Dedup reset test: existing prose dedup fields are unconditionally reset after rederive so P0-3 Pass 1 reprocesses the full corpus.

**Execution steps:**

1. Write temp-DB tests for an offline prose rederive command or function.
2. Run tests and confirm they fail because no phase0 rederive entrypoint exists or because blank refs remain.
3. Implement the smallest data-core entrypoint to rederive Polygon and Finnhub from existing Bronze and print a summary.
4. Run targeted tests and confirm they pass.
5. Pre-mutation: confirm dev DB backup exists and frozen SHA matches.
6. Run the rederive command against `data/catalyst_dev_ws4b.db` with no live flags and no provider keys.
7. Run the blank-reference gate queries against dev DB.

**Fail-stop gate P0-2:**

- `articles.reference_date` has zero NULL/blank rows for `polygon_news` and `finnhub_company_news`.
- `article_tickers.reference_date` has zero NULL/blank rows for `polygon_news` and `finnhub_company_news`.
- Finnhub `reference_date` is derived from `published_utc` and trade-date mapping, not from raw asset fetch date.
- Polygon blank-ref association count goes from the current known bad value to zero.
- Dev DB mutation is limited to prose article materialization tables and any expected clean assets derived from Polygon if explicitly part of the entrypoint.
- Affected clean_assets are regenerated.
- Re-derived prose dedup fields are reset unconditionally.
- Frozen DB SHA unchanged.

**Rollback:**

- Restore pre-flight DB backup if article/reference counts or scope checks fail.

## P0-3 Cross-Source Dedup Root Cause And Materialization

**Purpose:** Reproduce why dedup never materialized in the backfill path, fix the trigger/commit behavior, and materialize `dedup_group_id` with exactly one canonical row per group after P0-2 has completed on the full corrected corpus.

**Mode:** DB mutation. No network. Dev DB only.

**Files:**

- Modify: `packages/data-core/catalyst_data/backfill_pipeline.py`
- Modify: `packages/data-core/catalyst_data/update_pipeline.py`
- Modify: `packages/data-core/catalyst_data/dedup/cross_source.py` only if root-cause evidence points there
- Modify or create CLI/script: `packages/data-core/catalyst_data/cli_index.py` or `packages/data-core/scripts/phase0_remediate.py`
- Test: `packages/data-core/tests/test_3f2_integration.py`
- Test: `packages/data-core/tests/test_backfill_pipeline.py`
- Test: `packages/data-core/tests/test_finnhub_dedup.py`
- Test: `packages/data-core/tests/test_phase0_remediate.py`

**Systematic debugging protocol:**

- Reproduce with a temp DB fixture that mirrors `backfill --sources finnhub_company_news` calling `run_backfill` and `run_update_batch`.
- Confirm whether dedup is skipped because `compute_missing_cells` returns zero cells, because source list excludes Polygon, because post-batch dedup is gated on `finnhub_company_news` but no rows are rederived, because a connection closes before commit, or because rederive overwrites dedup fields after dedup.
- Trace the call path: CLI `cmd_backfill`, `run_backfill`, `run_update_batch`, post-batch rederive/classify/dedup, and final connection close.
- State the root cause in the test name or test comments before implementing the fix.
- Ensure the final production materialization runs only after P0-2 has completed across the full corpus, not per chunk, so fingerprints see all corrected reference dates.

**TDD bite-tests:**

- Backfill post-batch fixture fails if Finnhub-only backfill silently no-ops dedup when eligible Polygon rows already exist in the DB.
- Combined Polygon+Finnhub fixture fails if same-event rows do not get a shared `dedup_group_id` and exactly one canonical row.
- Multi-ticker fixture proves article_tickers associations remain lossless and OR-semantics canonical selection does not zero out a canonical article that wins one ticker group.
- Idempotency fixture proves running materialization twice does not change row counts and keeps canonical counts stable.

**Implementation steps:**

1. Write the backfill no-op reproduction test.
2. Run it and confirm `dedup_group_id` remains NULL.
3. Complete root-cause trace and record the exact failed boundary in the plan execution report.
4. Implement the smallest fix in the caller or materialization entrypoint.
5. Run the bite-test and confirm it passes.
6. Write or extend same-event canonical test.
7. Run it red, then fix if needed.
8. Run targeted dedup tests.
9. Run the offline materialization command against dev DB.
10. Run dedup gate queries.

**Fail-stop gate P0-3:**

- For eligible `polygon_news` and `finnhub_company_news` rows, `articles.dedup_group_id` is non-NULL.
- For eligible `article_tickers` rows, `article_tickers.dedup_group_id` is non-NULL.
- Every non-NULL article dedup group has exactly one canonical row when grouped at the canonical decision grain.
- There are zero dedup groups with zero canonical rows.
- Multi-ticker article_tickers counts are not reduced.
- Source priority remains stable and Polygon/SEC provenance is not overwritten by Finnhub.
- Frozen DB SHA unchanged.

**Rollback:**

- Restore pre-flight DB backup if dedup materialization produces zero-canonical groups, loses multi-ticker associations, or touches unrelated sources.

## P0-5 Checkpoint To Data Reconciliation

**Purpose:** Classify every success checkpoint as materialized, valid success-empty, or phantom; document Polygon source ceiling; repair failed/skipped/phantom checkpoint states without network.

**Mode:** DB mutation. No network. Dev DB only.

**Files:**

- Modify: `packages/data-core/catalyst_data/coverage_audit.py`
- Modify: `packages/data-core/catalyst_data/quality.py` if a new status or metadata table is needed
- Modify: `packages/data-core/catalyst_data/update_pipeline.py` if checkpoint semantics need to write valid empty metadata in future runs
- Modify or create: `packages/data-core/scripts/phase0_remediate.py`
- Test: `packages/data-core/tests/test_coverage_audit.py`
- Test: `packages/data-core/tests/test_quality_helpers.py`
- Test: `packages/data-core/tests/test_update_pipeline.py`
- Test: `packages/data-core/tests/test_phase0_remediate.py`

**Design contract:**

- Do not define coverage as latest closed trading day for every source.
- For Polygon, accept latest available source ceiling when proven by materialized raw/data and provider-empty evidence.
- A success checkpoint is materialized if it has corresponding raw asset and/or materialized rows for that source/ticker/date.
- A success checkpoint is success-empty if the raw provider response exists and proves a successful empty result for that source/ticker/date.
- A success checkpoint is phantom if it has neither materialized rows nor a valid empty raw response.
- Failed and skipped checkpoints must be repaired into explicit terminal classes or left as actionable failures; do not let them masquerade as coverage.
- Avoid expanding `_STATUS_VALUES` unless necessary. If the current schema cannot represent success-empty distinctly, add a sidecar reconciliation report or metadata classification rather than unsafe schema churn.

**Systematic debugging protocol:**

- Build a temp DB with one materialized success, one success-empty raw response, one phantom success, one failed checkpoint, and one skipped checkpoint.
- Run the current audit and prove it cannot distinguish them.
- Trace checkpoint writes in `_fetch_cell`, `_fetch_cell_finnhub`, `_fetch_cell_sec`, and live/backfill callers.
- Decide whether repair belongs in audit classification, checkpoint migration, or future writer semantics.

**TDD bite-tests:**

- Audit fails today to classify phantom success.
- Audit passes after adding materialized/success-empty/phantom buckets.
- A Polygon success checkpoint after the source ceiling with no raw/data is flagged as phantom, not accepted coverage.
- A provider 200 empty raw response is accepted as success-empty.
- Failed and skipped counts remain visible and do not disappear from the report.

**Implementation steps:**

1. Write the checkpoint classification fixture in `test_coverage_audit.py`.
2. Run it and confirm current audit cannot classify phantom success.
3. Trace writer paths and state root cause in execution report.
4. Implement checkpoint reconciliation classification.
5. Run the fixture and confirm it passes.
6. Add repair command tests in `test_phase0_remediate.py`.
7. Implement a no-network repair action that marks or reports phantom, failed, and skipped cells deterministically.
8. Run targeted audit and quality tests.
9. Run repair against dev DB.
10. Run checkpoint gate queries.

**Fail-stop gate P0-5:**

- No phantom success checkpoints remain.
- Failed and skipped checkpoints are either repaired or reported as actionable non-coverage.
- Polygon latest available source ceiling is documented as accepted only where raw success-empty evidence supports it.
- Checkpoint max dates reconcile with materialized data or valid empty evidence.
- No network calls occurred in this phase.
- Frozen DB SHA unchanged.

**Rollback:**

- Restore pre-flight DB backup if reconciliation mutates unrelated checkpoints or hides failures.

## Prose Sub-Gate

**Purpose:** Independently prove the embedding-critical prose corpus is trustworthy before the FRED network phase.

**Mode:** Read-only audit. No network. No DB mutation.

**Gate invariants:**

- Zero blank/NULL `reference_date` in eligible prose `articles`.
- Zero blank/NULL `reference_date` in eligible prose `article_tickers`.
- Dedup is materialized for all eligible Polygon/Finnhub rows.
- Every dedup group has exactly one canonical row at the expected grouping grain.
- Multi-ticker associations remain lossless.
- News checkpoints reconcile with materialized data or valid success-empty evidence.
- Frozen DB SHA unchanged.

If this sub-gate passes and P0-4 later fail-stops, report the prose sub-gate as standing independently.

## P0-4 FRED PIT Re-Ingest

**Purpose:** Replace invalid FRED macro rows and invalid FRED Bronze with scoped, genuinely point-in-time first-release data.

**Mode:** DB mutation and the only network phase. Dev DB only. Requires provider key loaded in the same shell invocation. Automated tests remain offline and mocked.

**Files:**

- Modify: `packages/data-core/catalyst_data/connectors/fred.py`
- Modify: `packages/data-core/catalyst_data/cli_index.py`
- Modify: `packages/data-core/catalyst_data/pipeline/fred_normalize.py`
- Modify: `packages/data-core/catalyst_data/pipeline/fred_manifest.py` if series-specific window metadata is needed
- Modify or create: `packages/data-core/scripts/phase0_remediate.py`
- Test: `packages/data-core/tests/test_fred_connector.py`
- Test: `packages/data-core/tests/test_fred_normalize.py`
- Test: `packages/data-core/tests/test_3f2_fred_live.py`
- Test: `packages/data-core/tests/test_phase0_remediate.py`

**Design contract:**

- Default eval window is `2023-01-01` through latest closed trading day unless orchestrator changes it during review.
- Window is parameterized.
- Re-ingest clears and replaces only dev DB FRED state: `macro_observations`, `raw_assets WHERE source_type='fred_macro'`, and `source_checkpoints WHERE source_type='fred_macro'` if such checkpoints are part of the existing failed macro run.
- Do not use latest-revised values as PIT.
- `released_at` must come from per-observation provider metadata, never fetch date, top-level realtime metadata, or raw asset reference date.
- First strategy: `output_type=4` over narrowed observation windows.
- Fallback strategy: ALFRED/realtime-window chunking where a series/window hits vintage caps or response shape proves `output_type=4` is insufficient.
- Series-specific observation windows are allowed to keep request sizes below provider limits.
- `T10Y2Y.released_at` is max of component `DGS10.released_at` and `DGS2.released_at`.
- Macro remains Plane-2 and is never embedded.
- Before clearing or replacing the existing 98,349 invalid macro rows, run a live fit-guard that validates the scoped window against real FRED response shape/counts for representative series.
- Proceed to clear-and-replace only if `output_type=4` fits the scoped window or the ALFRED realtime-window fallback is fully implemented and tested.
- If neither fit path is proven, fail-stop at P0-4, leave the invalid macro rows in place, and report that they remain quarantined and unused. Never leave macro half-replaced.

**TDD bite-tests:**

- FRED fixture with a 2023 observation and 2023 per-observation release date fails if normalization assigns the current fetch date.
- Connector test proves scoped window parameters are sent: observation start, observation end, output type, realtime start, realtime end.
- Fallback test mocks a cap/error response and proves chunking is selected without network.
- Replacement guard test refuses frozen DB realpath.
- Re-ingest dry-run test reports rows that would be cleared and series/windows that would be fetched, with zero DB writes.
- Fit-guard test proves clear-and-replace is refused unless the representative live shape/count check passes or a tested fallback is available.
- Plane-2 test proves macro rows do not enter `index_state` or clean assets.

**Implementation steps:**

1. Write failing normalization regression for fetch-date `released_at`.
2. Run it and confirm it fails against the bad path or simulated bad assignment.
3. Implement or tighten normalization validation.
4. Write connector parameter tests for scoped windows and output type.
5. Run red, implement, then run green.
6. Write dry-run replacement tests and frozen DB refusal tests.
7. Implement FRED replacement orchestration with explicit dry-run and live modes.
8. Write fallback chunking tests and implement only the fallback behavior needed by the mocked cap case.
9. Run targeted FRED tests.
10. Pre-live: confirm backup, frozen SHA, and dry-run output.
11. Run the P0-4 fit-guard using real FRED response shape/counts for a representative series, loading env in the same shell invocation and redacting keys.
12. If fit is not proven and fallback is not implemented/tested, fail-stop at P0-4 without clearing or replacing macro rows.
13. If fit is proven, run live clear-and-replace. The live command must load env in the same shell invocation and use explicit `--live --confirm`, scoped dates, dev DB path, and any replacement flag required by implementation.
14. After live, run macro PIT gate queries.

**Fail-stop gate P0-4:**

- `macro_observations` has no rows with `released_at` equal to the live fetch date unless provider metadata truly says so.
- Historical 2023 observations have historical release dates, not 2026 fetch dates.
- `released_at <= fetched_at` for all macro rows.
- PIT query at an as-of date excludes observations with later `released_at`.
- `T10Y2Y` value and release-date derivation pass.
- `index_state` macro count is zero.
- FRED raw asset metadata contains no API key or full request URL with secrets.
- Frozen DB SHA unchanged.
- Fit-guard outcome is recorded as fit proven or fail-stopped before replacement.

**Rollback:**

- Restore pre-flight DB backup if the re-ingest writes latest-revised/fetch-date semantics, leaks secrets, or fails Plane-2 separation.

## P0-6 Coverage Audit Gate And Scoped Commit Preparation

**Purpose:** Turn Phase 0 invariants into an enforceable pass/fail audit and prepare a scoped data-core commit set for human review.

**Mode:** Offline audit. No network. No additional DB mutation except optional audit artifact writes after review.

**Files:**

- Modify: `packages/data-core/catalyst_data/coverage_audit.py`
- Modify: `packages/data-core/catalyst_data/cli_index.py` if exposing an audit command is needed
- Test: `packages/data-core/tests/test_coverage_audit.py`
- Possibly create report: `docs/reports/2026-07-06-ws4b-phase0-remediation-audit.md` only if orchestrator wants a report artifact during execution

**Gate P0 invariants:**

- Every eligible prose article has non-blank `articles.reference_date`.
- Every eligible prose article_ticker association has non-blank `article_tickers.reference_date`.
- FRED macro rows are PIT-correct: no fetch-date release-date collapse, `released_at <= fetched_at`, and PIT query excludes look-ahead.
- Macro remains Plane-2: zero macro rows in `index_state`, no macro clean assets, no macro embedding deltas.
- Dedup is materialized for all eligible Polygon/Finnhub rows.
- Every dedup group has exactly one canonical row at the expected grouping grain.
- Multi-ticker associations remain lossless.
- Checkpoints reconcile with materialized data or valid success-empty evidence.
- Polygon source ceiling is documented as latest available per source, not treated as a logic bug.
- Frozen DB SHA unchanged.
- Full data-core suite passes with exact passed/failed/skipped counts reported.

**TDD bite-tests:**

- Coverage audit fails on blank article reference date.
- Coverage audit fails on blank article_tickers reference date.
- Coverage audit fails on invalid FRED fetch-date release collapse.
- Coverage audit fails on NULL dedup materialization for eligible articles.
- Coverage audit fails on zero-canonical dedup group.
- Coverage audit fails on phantom success checkpoint.
- Coverage audit passes on a minimal fully compliant fixture.

**Implementation steps:**

1. Write failing audit invariant tests for each Gate P0 class.
2. Run them and confirm they fail under current audit behavior.
3. Implement pass/fail invariant aggregation in `coverage_audit.py`.
4. Add a concise CLI-visible summary if needed.
5. Run `python3 -m pytest tests/test_coverage_audit.py -q` from `packages/data-core`.
6. Run final read-only audit against dev DB.
7. Run full suite from `packages/data-core` with `PATH="$PWD/.venv/bin:$PATH" python3 -m pytest -q`.
8. Capture exact passed/failed/skipped counts.
9. Confirm frozen SHA before and after.
10. Run `git diff --stat`, `git diff --cached --stat`, and `git status --short --branch`.
11. Stage only data-core remediation files and approved docs/reports artifacts. Do not stage `packages/app` or `packages/agents`. Do not commit.

**Fail-stop gate P0-6:**

- Audit returns pass for Gate P0.
- Full data-core suite has zero failures.
- Frozen SHA matches expected value.
- Git staged scope excludes `packages/app` and `packages/agents`.
- Execution report includes red/green evidence for bite-tests, DB backup path, live FRED command redaction statement, DB mutation phases, and exact test counts.

**Rollback:**

- If the audit fails, do not stage final commit scope beyond already reviewed files. Return to the failing phase with the pre-flight backup preserved.

## Cross-Cutting Safety Requirements

**Frozen DB immutability:** Every mutating command must refuse the frozen DB realpath. The expected frozen SHA is `0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf`. Assert before and after every mutating phase.

**Network boundary:** Only P0-4 may perform network calls. Automated tests must mock all provider calls. P0-1, P0-2, P0-3, P0-5, Prose Sub-Gate, and P0-6 are no-network phases.

**DB mutation boundary:** P0-1 is temp-DB only. P0-2, P0-3, P0-4, and P0-5 mutate the dev DB only after backup. P0-6 is audit-only unless explicitly producing report artifacts.

**Secrets:** Never print `.env`. Never print API keys. Redact provider error payloads to `[REDACTED]`. Do not persist full URLs containing `api_key`.

**Idempotency:** Re-running P0-2, P0-3, and P0-5 must not duplicate rows or change counts after the first successful run, except timestamps/summary artifacts where explicitly documented.

**Observability:** Each phase must print counts before and after: rows scanned, rows changed, blank refs, dedup rows, canonical violations, macro PIT violations, checkpoint classifications, and frozen SHA.

**Commit discipline:** Stage only data-core and approved docs/report files. Human commits. Do not commit from the execution pass unless the human explicitly changes the instruction.

## Open Questions And Defaults

**Default FRED window start:** Default to `2023-01-01` through latest closed trading day. This matches remediation plan §B and limits FRED vintage scope.

**Weekend/holiday publish mapping:** Preserve current `map_to_trade_date` semantics. Before close maps to that ET date, after close maps to next calendar date, then the shared trading calendar maps to the first trading day on or after the candidate.

**Success-empty re-probe policy:** Treat valid success-empty as coverage for Phase 0 if raw provider evidence exists. Re-probing empty cells belongs to a later source-refresh policy, not Gate P0.

**Polygon ceiling:** Accept latest available per source only after P0-5 proves cells beyond the ceiling are success-empty or non-coverage with explicit source availability evidence.

## Final Execution Report Required

At the end of the single execution pass, report:

- Plan file path.
- Backup DB path.
- Frozen SHA before and after.
- Each phase gate result.
- Prose sub-gate result separately from full Gate P0.
- Red and green output summary for every bite-test.
- Confirmed P0-3 dedup root cause.
- P0-4 fit-guard outcome: fit proven or fail-stopped without replacement.
- Full data-core suite exact passed/failed/skipped counts.
- Dev DB mutation phases and confirmation that no mutation happened before backup.
- Confirmation that only P0-4 used network.
- Staged diff stat and staged file list.
- Explicit statement that `packages/app` and `packages/agents` were not staged or modified by Phase 0 remediation.

Plan complete. Execute only after one orchestrator review approves this single-pass plan.

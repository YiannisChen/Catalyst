# Data-Core Failure Groups (W3-A Baseline)

**Created:** 2026-07-11
**Canonical command:** `.venv/bin/python -m pytest packages/data-core -q`
**Baseline counts:** 620 passed, 83 failed, 1 skipped, 1 xfailed, 705 collected
**Python:** 3.13.2
**pytest:** 9.1.1
**pytest-asyncio:** NOT INSTALLED
**Branch:** ws4b/article-level-data

## Integrity Constraints

1. Every currently failing test maps to exactly one root-cause group.
2. Group cohorts sum to 83 (the observed canonical failure count).
3. No group called "pre-existing noise."
4. After plugin installation, every remaining failure must be recollected
   and assigned new root-cause group IDs based on its real post-plugin
   signature. The pre-install groups are "blocking-signature" groups and do
   not prejudge post-plugin outcomes.
5. If one fix does not resolve all tests sharing a group's blocking
   signature, the group must be split after re-collection.

---

## Group G1: Missing pytest-asyncio Plugin

**Group ID:** `G1-missing-pytest-asyncio`

**Shared blocking signature:**

```
async def functions are not natively supported.
You need to install a suitable plugin for your async framework, for example:
  - pytest-asyncio
```

Plus secondary for decorated tests:

```
PytestUnknownMarkWarning: Unknown pytest.mark.asyncio - is this a typo?
```

And:

```
PytestConfigWarning: Unknown config option: asyncio_mode
```

**Root cause:** `pytest-asyncio` is declared as an optional dev dependency in
`packages/data-core/pyproject.toml` at
`[project.optional-dependencies] dev = ["pytest-asyncio>=0.23"]` but was
never installed into the root `.venv`. Python 3.13 does not natively execute
`async def` test functions.

**Classification:** environment (blocking signature)

**Important:** This group captures the current top-level failure signature
only. It does NOT prove that all 83 tests will pass after plugin
installation. Underlying test outcomes remain unknown until the plugin is
installed and the suite is re-run. Any tests that still fail after
installation must be assigned new root-cause group IDs based on their actual
post-plugin failure signatures.

### Cohort U — Undecorated Async Tests (23)

These tests use `async def test_*` without `@pytest.mark.asyncio`, relying on
`asyncio_mode = "auto"` for async detection.

| File | Count | Failing test IDs |
|---|---|---|
| `packages/data-core/tests/test_update_pipeline.py` | 5 | `TestRunUpdateBatchReal::test_mocked_fetch`, `TestRunUpdateBatchReal::test_idempotent_rerun`, `TestRunUpdateBatchReal::test_checkpoint_skip`, `TestRunUpdateBatchReal::test_polygon_only_batch_dedups_existing_finnhub_corpus`, `TestRunUpdateBatchReal::test_no_index_state_writes_from_dry_run` |
| `packages/data-core/tests/test_3f2_finnhub_live.py` | 5 | `TestCrossSourceDedup::test_one_canonical_per_dedup_group`, `TestDailyIncremental::test_daily_incremental_zero_missing_after_backfill`, `TestFinnhubCellSuccess::test_finnhub_cell_succeeds_with_valid_data`, `TestFinnhubIdempotency::test_finnhub_repeat_run_zero_new_raw_assets`, `TestTierByPublisher::test_finnhub_seekingalpha_tier_5` |
| `packages/data-core/tests/test_3f2_fred_live.py` | 1 | `TestFredLiveOrchestration::test_fred_live_mocked_orchestration` |
| `packages/data-core/tests/test_3f2_integration.py` | 2 | `TestCombinedBatchDedupAndTier::test_combined_batch_polygon_articles_not_deduped_or_classified`, `TestPostBatchOrder::test_dedup_has_both_providers_articles` |
| `packages/data-core/tests/test_3f2_polygon_live.py` | 6 | `TestPolygonFetchFnUnwrap::test_dict_fetch_fn_cell_succeeds_after_fix`, `TestPolygonLiveRawAssetWritten::test_polygon_live_raw_asset_and_articles_written`, `TestPolygonIdempotency::test_repeat_run_zero_new_raw_assets`, `TestCheckpointWritten::test_success_checkpoint_written`, `TestFailedCellRetry::test_failed_checkpoint_cell_is_retried`, `TestCheckpointResume::test_checkpoint_resume_zero_new_cells_on_rerun` |
| `packages/data-core/tests/test_3f2_sec_live.py` | 4 | `TestSecDocumentsMaterialized::test_filing_documents_materialized_for_8k`, `TestSecEX991AndBronzeHTML::test_sec_ex99_1_resolved_and_bronze_raw_html`, `TestSecFilingIdFormat::test_filing_id_format_sec_cik_accession`, `TestSecTierOne::test_sec_filings_source_tier_one` |

### Cohort D — Decorated Async Tests (60)

These tests use `@pytest.mark.asyncio` on `async def test_*`. The
`PytestUnknownMarkWarning` is expected when `pytest-asyncio` is absent
(the plugin self-registers its `asyncio` marker on load).

| File | Count | Failing test IDs |
|---|---|---|
| `packages/data-core/tests/test_retry.py` | 7 | `test_with_retry_429_then_success`, `test_with_retry_5xx_then_success`, `test_with_retry_exhausted_returns_last_error`, `test_with_retry_non_retryable_returns_immediately`, `test_with_retry_429_structured_retry_after_used`, `test_with_retry_429_no_header_uses_exponential`, `test_with_retry_5xx_uses_exponential` |
| `packages/data-core/tests/test_rate_limiter.py` | 6 | `test_rate_limiter_enforces_interval`, `test_rate_limiter_daily_budget`, `test_rate_limiter_concurrency`, `test_rate_limiter_daily_budget_resets_on_new_day`, `test_rate_limiter_releases_slot_after_exception`, `test_rate_limiter_honors_general_concurrency_cap` |
| `packages/data-core/tests/test_sec_connector.py` | 7 | `test_sec_submissions_parses_response`, `test_sec_submissions_404_error`, `test_fetch_document_html_extracts_text`, `test_fetch_document_pdf_skipped`, `test_user_agent_passed_to_both`, `test_unknown_endpoint_returns_error`, `test_fetch_document_raw_bytes_preserved` |
| `packages/data-core/tests/test_polygon_connector.py` | 7 | `test_polygon_ohlcv_parses_response`, `test_polygon_news_parses_response`, `test_polygon_error_returns_status`, `test_polygon_429_retry_after_header_extracted`, `test_polygon_server_error_preserves_response_text`, `test_polygon_timeout_returns_error`, `test_polygon_unknown_endpoint_returns_error_without_request` |
| `packages/data-core/tests/test_finnhub_connector.py` | 8 | `test_company_news_200`, `test_company_news_field_mapping`, `test_token_as_query_param`, `test_limiter_honored`, `test_401_non_retryable`, `test_429_retry`, `test_unknown_endpoint`, `test_empty_api_key_raises` |
| `packages/data-core/tests/test_fred_connector.py` | 4 | `test_fred_fetches_series`, `test_fred_preserves_dot_values`, `test_fred_error_returns_status`, `test_fred_timeout_returns_structured_error` |
| `packages/data-core/tests/test_fmp_connector.py` | 1 | `test_fmp_429_retry_after_header_extracted` |
| `packages/data-core/tests/test_orchestrator.py` | 15 | `test_process_request_handles_failed_fetch`, `test_process_request_handles_malformed_news_results_dict`, `test_process_request_handles_string_publisher_shape`, `test_process_request_multiple_sources`, `test_process_request_partial_failure`, `test_process_request_partial_failure_rerun_does_not_corrupt_prior_success`, `test_process_request_persists_ohlcv_rows_on_production_path`, `test_process_request_reports_failed_endpoints_on_partial_success`, `test_process_request_reports_fetch_exceptions_per_source`, `test_process_request_rerun_keeps_single_bronze_and_silver_row`, `test_process_request_skips_empty_news_without_storing_clean_asset`, `test_process_request_skips_empty_ohlcv_without_storing_bar`, `test_process_request_sources_run_concurrently`, `test_process_request_stores_bronze_and_silver`, `test_process_request_yfinance_fundamentals_source_maps_correctly` |
| `packages/data-core/tests/test_live_runner.py` | 1 | `test_live_pipeline_accepts_fetch_fn` |
| `packages/data-core/tests/test_smoke_test.py` | 1 | `test_run_passes_db_path_to_process_request` |
| `packages/data-core/tests/test_phase0_remediate.py` | 3 | `test_fred_fit_guard_proves_output_type4_shape_and_params`, `test_fred_replacement_clears_and_replaces_when_fit_proven`, `test_fred_replacement_refuses_without_fit_guard` |

### Proposed Resolution

1. Install the declared data-core dev extra into the root `.venv`:
   ```
   .venv/bin/pip install -e "packages/data-core[dev]"
   ```
   This pulls in `pytest-asyncio>=0.23` through the package's declared
   `[dev]` optional-dependencies, not a standalone undeclared install.

2. Confirm plugin loading:
   - `pip show pytest-asyncio` shows package installed with version ≥ 0.23.
   - `pytest` configuration output shows `asyncio_mode` recognized.
   - One undecorated async smoke test runs without the plugin-absence
     error.
   - One decorated async smoke test runs without the plugin-absence error
     and without `PytestUnknownMarkWarning`.

3. Re-run the full canonical suite to collect actual post-plugin outcomes.

4. G1 is **resolved** only when the async-plugin failure signatures
   disappear from all 83 tests. Any remaining failures must be assigned
   new root-cause group IDs (G2, G3, …) based on their real post-plugin
   failure signatures.

5. If `PytestUnknownMarkWarning` persists after confirmed plugin loading,
   stop and diagnose the plugin/version/configuration loading path.
   Do NOT immediately add a manual `markers` entry to `pyproject.toml` —
   `pytest-asyncio` self-registers its `asyncio` marker.

### Design rationale for a single group with cohorts

Decorator usage (`@pytest.mark.asyncio` vs bare `async def test_*`) is not a
distinct root cause. Both cohorts fail because, and only because,
`pytest-asyncio` is absent. The cohorts are retained for targeted smoke
testing (one from each cohort) to confirm plugin loading covers both
detection paths (`asyncio_mode = "auto"` and explicit marker).

---

## Integrity Check

- Cohort U: 5 + 5 + 1 + 2 + 6 + 4 = **23**
- Cohort D: 7 + 6 + 7 + 7 + 8 + 4 + 1 + 15 + 1 + 1 + 3 = **60**
- Total: 23 + 60 = **83** ✓ (matches canonical failure count)

Every failing test from the canonical 2026-07-11 run appears in exactly one
cohort of G1.

---

## Post-W3-A State

After `pytest-asyncio` installation and full-suite re-run:

1. The actual pass/fail/skip/xfail counts are recorded — **no prediction**.
2. G1 is marked resolved if its blocking signatures disappear from all 83 tests.
3. Any remaining failures are classified into new root-cause groups
   (G2, G3, …) based on their actual post-plugin failure signatures.
4. `sum(new group counts) == actual post-plugin failure count`.
5. The post-W3-A known-failure manifest (if any failures remain) becomes
   the diff target for all W1 slices.

## Pre-Existing State (Not a Finding of W3-A)

The repo already has the correct declarations:

- `packages/data-core/pyproject.toml`:
  - `[project.optional-dependencies] dev = ["pytest-asyncio>=0.23"]` ✓
  - `[tool.pytest.ini_options] asyncio_mode = "auto"` ✓

The defect is purely that the dev extra was never `pip install`ed into the
root `.venv`. No `pyproject.toml` changes are planned for W3-A unless a
genuine post-plugin compatibility defect is demonstrated.

---

## Resolution Log — W3-A Execution (2026-07-12)

### G1-missing-pytest-asyncio: RESOLVED

**Date:** 2026-07-12
**Action:** Installed `pytest-asyncio 1.4.0` via `.venv/bin/pip install -e "packages/data-core[dev]"` (cached wheel, no network fetch).
**Plugin confirmation:**
- `pip show pytest-asyncio`: Version 1.4.0 ✓
- `pytest --trace-config`: `PLUGIN registered: <module 'pytest_asyncio.plugin'>` ✓
- Warning scan: No `Unknown config option: asyncio_mode` or `PytestUnknownMarkWarning` ✓
- Smoke — undecorated (cohort U): `test_mocked_fetch` → PASSED ✓
- Smoke — decorated (cohort D): `test_with_retry_429_then_success` → PASSED ✓

**Post-fix canonical result:**
- **703 passed, 0 failed, 1 skipped, 1 xfailed** — all 83 blocking-signature failures resolved.
- Collection: 705 (unchanged from pre-fix).
- No async-plugin signatures remain.

**Remaining failures:** None.
**New root-cause groups:** None needed.
**Known-failure manifest:** Empty. The data-core test suite is green.

### DB integrity

- Dev DB SHA (pre):  `92731fb7c5c3e989b4fdcbefb9d1d2060974082a6c60f1bbecad6bc42ee846b0`
- Dev DB SHA (post): `92731fb7c5c3e989b4fdcbefb9d1d2060974082a6c60f1bbecad6bc42ee846b0` — **unchanged** ✓
- Frozen DB SHA (pre):  `0d97a7ec61b6ec8fb5f9263b0b37b0efc9755812739d7afead7f720c3567e8dd`
- Frozen DB SHA (post): `0d97a7ec61b6ec8fb5f9263b0b37b0efc9755812739d7afead7f720c3567e8dd` — **unchanged** ✓

### Production runtime integrity

- `git diff --name-only | rg catalyst_data/` — **empty** (no match) ✓
- `git diff --name-only packages/data-core/tests/` — **empty** ✓
- `git diff --stat data/provider_*` — **empty** ✓

# Data-Core Live Validation Matrix

**Project:** Catalyst  
**Scope:** `packages/data-core` real-provider validation  
**Status:** Manual or scheduled validation only  
**Last Updated:** 2026-04-05

## 1. Purpose

This matrix turns live `data-core` validation into a repeatable operator workflow.

It exists to catch issues that offline tests cannot fully prove:

- provider payload drift
- real no-news days
- real news-heavy days
- real fundamentals availability
- real macro source behavior
- source attribution or URL loss in end-to-end runs

This matrix does **not** run in default CI. It requires real API keys and should be run manually or on a scheduled validation job.

## 2. Canonical Command

Run from:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/data-core
```

Canonical smoke command shape:

```bash
/Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m scripts.smoke_test --ticker AAPL --days 1 --sources polygon_news,polygon_ohlcv,fmp_fundamentals,fred_macro --artifacts
```

Notes:

- replace the absolute Python path with your local `packages/data-core/.venv/bin/python`, or activate `.venv` first and use `python`
- use `--days 1` for a fixed historical check if you are validating a single date manually in the DB afterward
- use `--sources` to keep the run reproducible instead of depending on every available key
- use `--artifacts` when you want per-date summaries written to `data/smoke_artifacts/`

## 3. Fixed Validation Matrix

Use this small matrix first. Expand only if a real failure suggests a gap.

| Case | Goal | Ticker | Date Type | Suggested Sources |
|---|---|---|---|---|
| Quiet news day | Confirm empty-or-light news does not break the pipeline | `AAPL` | Recent normal weekday with little company news | `polygon_news,polygon_ohlcv,fred_macro` |
| News-heavy day | Confirm multiple articles still preserve attribution and URLs | `TSLA` | Known headline-heavy weekday | `polygon_news,polygon_ohlcv` |
| Fundamentals / earnings-relevant day | Confirm fundamentals path still stores Bronze and Silver cleanly | `NVDA` | Earnings-adjacent weekday or strong fundamentals interest day | `fmp_fundamentals,polygon_news,polygon_ohlcv` |
| Macro-sensitive day | Confirm macro series and market context still ingest together | `SPY` or `AAPL` | Day with obvious macro event sensitivity | `fred_macro,polygon_ohlcv,polygon_news` |

## 4. Operator Checklist

For each live run:

1. Confirm required keys are present in `packages/data-core/.env`.
2. Run the smoke command with a fixed `--ticker`, `--days`, and `--sources`.
3. Record the exact command used in your notes or issue comment.
4. Record the actual `reference_date` chosen for that matrix row so the run can be repeated later.
5. Check the console summary for per-source `OK` or `FAIL`.
6. Verify Bronze rows were written:
   - `raw_assets` increased for successful sources
   - `http_status` and endpoint metadata look reasonable
7. Verify Silver rows were written:
   - `clean_assets` exists for successful sources
   - markdown content is readable, not empty, and source labels make sense
8. Inspect attribution-critical fields:
   - article URLs are present in `content_md` when news exists
   - source attribution text is preserved
   - malformed-but-tolerated payloads did not silently erase obvious content
9. If a source fails, record:
   - source name
   - error summary from smoke output
   - whether Bronze or Silver rows were skipped as expected
10. If `--artifacts` was used, inspect the matching JSON file in `data/smoke_artifacts/`.

## 5. Suggested SQL Checks

After a run, inspect the SQLite database:

```sql
SELECT ticker, source_type, reference_date, http_status
FROM raw_assets
ORDER BY fetched_at DESC
LIMIT 20;
```

```sql
SELECT ticker, source_type, reference_date, substr(content_md, 1, 200)
FROM clean_assets
ORDER BY cleaned_at DESC
LIMIT 20;
```

Use these checks to confirm that:

- Bronze contains the expected logical source rows
- Silver contains transformed markdown for the successful sources
- URLs and source attribution survived the transform

## 6. Failure Recording Rules

When a live run fails:

- do not treat it as a default CI regression automatically
- record whether the failure came from provider outage, auth/config, or data-core behavior
- save or attach the smoke summary and any artifact JSON
- if the failure indicates payload drift, convert it into a new offline regression test before changing production code

## 7. Non-CI Status

This matrix is explicitly **outside default CI** because it depends on:

- real network access
- live API keys
- real provider behavior that can vary by date and account

Use it:

- before Phase 2 eval milestones
- after provider-facing connector changes
- on a lightweight recurring manual or scheduled cadence

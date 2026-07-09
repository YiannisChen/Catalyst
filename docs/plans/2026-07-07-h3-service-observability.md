# H3 — Service Observability Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> **Dependency:** Must execute AFTER H2 (error taxonomy + checkpoint columns ready). May forward-reference H1's `doctor()` for post-run validation gate. H4 retro-registers H3's ingestion_runs columns.

**Goal:** Make `run_update_batch` a single button-callable entry `run_update(RunConfig) → RunReport`, with a structured JSON report persisted to disk, `ingestion_runs` extended with progress/state columns, a resume-from-failure capability reading the source of truth (`source_checkpoints`), and an operational `FallbackPolicy` chain (in a new `fallback.py` module).

**Architecture:** A new `run_report.py` module owns RunConfig/RunReport. `fallback.py` owns FallbackPolicy. `update_pipeline.py` is refactored to accept RunConfig and return RunReport. `ingestion_runs` grows 8 columns; `source_checkpoints` gains `fallback_provider` and `fallback_triggered` (H2 owns the base checkpoint schema; H3 adds fallback columns). Resume reads failed+skipped cells from `source_checkpoints WHERE run_id = resume_from AND status IN ('failed','skipped')`. Fallback narrative: the failed primary checkpoint has `raw_asset_id = NULL`; only the successful fallback checkpoint carries its own `raw_asset_id`.

**Tech Stack:** Python 3.11, sqlite3 (WAL), asyncio, dataclasses. No new dependencies.

**Branch:** `ws4b/article-level-data`. DB: `data/catalyst_dev_ws4b.db`. Frozen DB: never change.

---

## Modules Touched

| File | Action | What Changes |
|------|--------|-------------|
| `catalyst_data/run_report.py` | **Create** | RunConfig dataclass, RunReport dataclass, `save_run_report()` JSON persistence |
| `catalyst_data/fallback.py` | **Create** | `FallbackPolicy` dataclass with chain, gating rules, `next_provider()`, `should_attempt()` |
| `catalyst_data/update_pipeline.py` | **Modify** | `run_update(RunConfig) → RunReport` as single entry; resume reads source_checkpoints; fallback orchestration |
| `catalyst_data/quality.py` | **Modify** | Extend ingestion_runs DDL (8 new columns); `open_ingestion_run` accepts RunConfig fields; extend source_checkpoints DDL (2 fallback columns) |
| `catalyst_data/orchestrator.py` | **Modify** | Wire `should_trigger_fallback` to H2 ErrorClass decision table; make operational |
| `catalyst_data/cli_index.py` | **Modify** | `cmd_update_news` calls `run_update()` instead of `run_update_batch()` |
| `tests/test_run_report.py` | **Create** | RunConfig/RunReport/JSON roundtrip tests |
| `tests/test_fallback_policy.py` | **Create** | FallbackPolicy chain tests, fallback narrative tests |
| `tests/test_quality_helpers.py` | **Modify** | Adapt to new ingestion_runs columns |

---

## Data Contract — H3 Owns

### RunConfig (`run_report.py`)

```python
@dataclass
class RunConfig:
    tickers: list[str] | None = None        # None → all OHLCV tickers
    sources: list[str] | None = None        # None → ["polygon_news"]
    from_date: str | None = None            # None → latest OHLCV date
    to_date: str | None = None              # None → latest OHLCV date
    limit: int | None = None                # Cap cells processed
    dry_run: bool = False                   # Compute missing cells only
    resume_from: str | None = None          # Previous run_id to resume from
    enable_fallback: bool = True            # Enable FallbackPolicy chain
    skip_doctor: bool = False               # Skip post-run doctor gate
    notes: str | None = None                # Operator notes
```

### RunReport (`run_report.py`)

Persisted to `data/run_reports/{run_id}.json`.

```json
{
  "run_id": "run_20260707T120000Z_a1b2c3d4",
  "mode": "update",
  "resume_from": null,
  "config": { "...RunConfig fields (except resume_from)..." },
  "started_at": "2026-07-07T12:00:00Z",
  "ended_at": "2026-07-07T12:01:30Z",
  "elapsed_sec": 90.5,
  "providers": {
    "polygon_news": { "cells_total": 50, "cells_success": 45, "cells_failed": 3, "cells_skipped": 2 },
    "finnhub_company_news": { "cells_total": 0, "cells_success": 0, "cells_failed": 0, "cells_skipped": 0 }
  },
  "retry_histogram": {
    "polygon_news": { "0": 40, "1": 5, "2": 3, "3": 2 },
    "total_retries": 18
  },
  "top_error_classes": {
    "rate_limit": 2,
    "timeout": 1
  },
  "fallbacks": {
    "triggered": 0,
    "successful": 0,
    "chain": "polygon_news → finnhub_company_news"
  },
  "rows_changed": {
    "articles_upserted": 120,
    "finnhub_articles_upserted": 0,
    "clean_assets_inserted": 45,
    "dedup_groups_resolved": 5
  },
  "index_state": {
    "delta_new": 10,
    "delta_changed": 3,
    "would_embed": 28
  },
  "doctor": {
    "invoked": true,
    "all_gates_passed": true,
    "failing_gates": []
  },
  "report_path": "data/run_reports/run_20260707T120000Z_a1b2c3d4.json"
}
```

Note: `per_cell_report` is NOT stored in RunReport JSON — the source of truth for resume is `source_checkpoints`.

### Ingestion Runs Extension (`ingestion_runs`)

Eight new columns added via additive ALTER TABLE:

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `current_source` | TEXT | NULL | Source currently being processed (for progress) |
| `current_ticker` | TEXT | NULL | Ticker currently being processed |
| `current_date` | TEXT | NULL | Date currently being processed |
| `cells_total` | INTEGER | 0 | Total cells in this run |
| `cells_done` | INTEGER | 0 | Cells completed |
| `canceled_at` | TEXT | NULL | ISO timestamp if canceled |
| `report_path` | TEXT | NULL | Path to RunReport JSON |
| `run_config_json` | TEXT | NULL | Serialized RunConfig for resumability |

`status` values: `("running", "succeeded", "failed", "canceled", "partial")`.

### FallbackPolicy (`fallback.py`)

```python
@dataclass(frozen=True)
class FallbackPolicy:
    chain: tuple[str, ...]              # ("polygon_news", "finnhub_company_news")
    excluded_error_classes: tuple[str, ...]  # ("auth", "permission_paid", "rate_limit", "budget_exhausted")
    max_fallback_depth: int = 1

    def next_provider(self, current: str) -> str | None: ...
    def should_attempt(self, error_class: str, depth: int) -> bool: ...
```

Default chain: `polygon_news → finnhub_company_news`. SEC and FRED excluded. YFinance/Tiingo diagnostic-only.

### Checkpoint Fallback Columns (`source_checkpoints`)

H3 adds two columns:

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `fallback_provider` | TEXT | NULL | Which provider was the fallback |
| `fallback_triggered` | INTEGER | 0 | 1 if this cell was fetched via fallback chain |

---

## Logic / State Flow

### 1. `run_update(RunConfig) → RunReport`

```
run_update(config)
  → assert_writable(db_path)   [H4 guard, importable after H4]
  → Open DB connection
  → Compute missing cells via compute_missing_cells
  → If config.resume_from:
      → Query source_checkpoints:
          SELECT DISTINCT source_type, ticker, date
          FROM source_checkpoints
          WHERE run_id = ? AND status IN ('failed', 'skipped')
      → Those (source_type, ticker, date) triples are the resume cell list
      → No need to load any JSON file — the DB is the source of truth
      → New run_id generated
  → Apply config.limit
  → Open ingestion_run with progress columns + run_config_json
  → For each cell:
      → Update current_source/current_ticker/current_date in ingestion_runs
      → Increment cells_done after each cell
      → Try primary provider via _fetch_cell
      → If failed AND config.enable_fallback AND error_class fallback-eligible:
          → FallbackPolicy.should_attempt(error_class, depth=0) → True?
          → FallbackPolicy.next_provider(current_source) → fallback_provider
          → _fetch_cell with fallback fetch_fn
          → On success: fallback checkpoint gets fallback_provider + fallback_triggered=1
          → On failure: fallback also fails, both checkpoints "failed"
      → Accumulate per-provider stats in memory
  → Post-batch: rederive → classify_articles → dedup → regenerate → index
  → If not config.skip_doctor: run doctor(), capture result
  → close_ingestion_run with final status (succeeded/partial/failed)
  → Aggregate provider stats, retry histogram, top error classes from checkpoints
  → save_run_report(report, dir="data/run_reports/")
  → Return RunReport
```

### 2. Resume Flow (F3 — reads FROM source_checkpoints, NOT JSON)

```
config.resume_from = "run_20260706T..."
  → conn.execute("""
        SELECT DISTINCT source_type, ticker, date
        FROM source_checkpoints
        WHERE run_id = ? AND status IN ('failed', 'skipped')
        ORDER BY source_type, ticker, date
    """, (config.resume_from,))
  → Returns list of (source_type, ticker, date) triples
  → These are the cells to re-fetch
  → New run_id generated (never reuses old run_id)
  → New ingestion_runs row has run_config_json.resume_from = "run_20260706T..."
  → Only the filtered cells are fetched
  → Old run's data is never mutated
```

Rationale: `source_checkpoints` is the source of truth. Checking it guarantees we pick up any cells that were marked failed mid-run (including partial runs that were interrupted). No JSON file deserialization needed.

### 3. Fallback Chain (F8/F9 — corrected narrative)

```
cell (AAPL, 2025-01-01, polygon_news)
  → _fetch_cell with polygon fetch_fn
  → Result: error_class = "timeout"
  → H2 atomic write: TRANSPORT/TIMEOUT → NO raw_asset stored
  → Checkpoint for polygon_news: status="failed", error_class="timeout",
    raw_asset_id=NULL, fallback_provider=NULL, fallback_triggered=0

  → FallbackPolicy.should_attempt("timeout", depth=0) → True
  → FallbackPolicy.next_provider("polygon_news") → "finnhub_company_news"

  → _fetch_cell with finnhub fetch_fn
  → Result: status = "success", items_count = 3
  → H2 atomic write: raw_asset stored + articles upserted + checkpoint
  → Checkpoint for finnhub_company_news: status="success",
    raw_asset_id=<finnhub_bronze_id>,
    fallback_provider="finnhub_company_news", fallback_triggered=1
```

**Key narrative correction** (F8/F9):
- The failed primary checkpoint NEVER carries a `raw_asset_id` — the primary didn't produce any data (TRANSPORT/TIMEOUT → nothing stored, per H2 split rollback). Even if the primary produced data but failed to parse (MALFORMED_RESPONSE), that raw_asset belongs to the primary's checkpoint, not the fallback.
- The fallback provider's raw_asset IS materialized and has its own `raw_asset_id` in its own checkpoint row.
- There is NO shared `raw_asset_id` across the two checkpoints.
- `fallback_triggered=1` and `fallback_provider` are set ONLY on the fallback provider's checkpoint row, never on the failed primary.

### 4. CLI → run_update() Relationship

```
CLI: cmd_update_news(db_path, ..., live=True, confirm=True)
  → Build RunConfig from CLI args
  → Call run_update(config)
  → Print RunReport summary to stdout
  → Exit 0 if report ended successfully
  → Exit 1 on failure
```

Thin wrapper — tests call `run_update()` directly.

---

## TDD Test Gate List

| # | Test | What It Catches |
|---|------|----------------|
| 3.1 | `test_run_config_defaults` | RunConfig() has sensible defaults |
| 3.2 | `test_run_report_json_roundtrip` | RunReport → JSON → parses back, all fields match |
| 3.3 | `test_run_report_saved_to_disk` | run_update completes → data/run_reports/{run_id}.json exists |
| 3.4 | `test_run_update_returns_run_report` | run_update(config) → RunReport instance |
| 3.5 | `test_run_update_provider_breakdown` | RunReport.providers has per-provider cell counts |
| 3.6 | `test_run_update_retry_histogram` | RunReport.retry_histogram correctly counts retry attempts |
| 3.7 | `test_run_update_top_error_classes` | RunReport.top_error_classes lists most frequent H2 ErrorClass values |
| 3.8 | `test_fallback_policy_chain_order` | FallbackPolicy.next_provider("polygon_news") → "finnhub_company_news" |
| 3.9 | `test_fallback_policy_excludes_auth` | FallbackPolicy.should_attempt("auth", 0) → False |
| 3.10 | `test_fallback_policy_excludes_rate_limit` | FallbackPolicy.should_attempt("rate_limit", 0) → False |
| 3.11 | `test_fallback_policy_depth_exhausted` | next_provider("finnhub_company_news") → None |
| 3.12 | `test_fallback_policy_sec_excluded` | SEC is NOT in the chain tuple |
| 3.13 | `test_fallback_triggers_on_timeout` | polygon fetch times out → finnhub fallback invoked |
| 3.14 | `test_fallback_not_triggered_on_auth` | polygon fetch returns AUTH → fallback NOT attempted |
| 3.15 | `test_fallback_checkpoint_has_fallback_provider` | Fallback success → checkpoint has fallback_provider + fallback_triggered=1 |
| 3.16 | `test_fallback_failed_primary_has_null_raw_asset_id` | Primary failed → its checkpoint has raw_asset_id=NULL |
| 3.17 | `test_fallback_success_carries_own_raw_asset_id` | Fallback's successful checkpoint has its own raw_asset_id, not shared |
| 3.18 | `test_resume_from_reads_source_checkpoints` | resume_from → queries source_checkpoints, only failed+skipped cells re-fetched |
| 3.19 | `test_resume_from_generates_new_run_id` | New run_id, old run untouched |
| 3.20 | `test_resume_from_no_json_dependency` | Deleted JSON file → resume still works (source_checkpoints is the truth) |
| 3.21 | `test_ingestion_run_has_progress_columns` | All 8 new columns exist after migration |
| 3.22 | `test_ingestion_run_status_partial` | Some cells fail → ingestion_runs.status = "partial" |
| 3.23 | `test_ingestion_run_progress_updates` | current_source/ticker/date + cells_done update during run |
| 3.24 | `test_run_update_posts_doctor` | RunReport.doctor.invoked = True after successful run |
| 3.25 | `test_run_update_skip_doctor` | config.skip_doctor=True → RunReport.doctor is null/empty |
| 3.26 | `test_run_update_dry_run_no_writes` | dry_run=True → zero DB writes, zero network |
| 3.27 | `test_cli_wraps_run_update` | CLI --live --confirm calls run_update() internally |
| 3.28 | `test_run_report_does_not_contain_per_cell_report` | RunReport JSON has no per_cell_report key (DB is truth) |

---

## Execution Order Within H3

```
T3.1: Create run_report.py (RunConfig + RunReport, save_run_report)
T3.2: Create fallback.py (FallbackPolicy)
T3.3: Extend ingestion_runs DDL in quality.py (8 columns)
T3.4: Extend source_checkpoints DDL (2 fallback columns)
T3.5: Test FallbackPolicy chain (tests 3.8–3.12)
T3.6: Refactor update_pipeline.py: run_update(RunConfig) → RunReport (tests 3.1–3.7, 3.27)
T3.7: Implement resume from source_checkpoints (tests 3.18–3.20)
T3.8: Wire fallback into cell loop (tests 3.13–3.17)
T3.9: Add progress tracking (tests 3.21–3.23)
T3.10: Integrate doctor post-run gate (tests 3.24–3.25)
T3.11: Wire CLI wrapper (test 3.27)
T3.12: Verify dry-run path (test 3.26)
T3.13: Verify RunReport schema excludes per_cell_report (test 3.28)
```

---

## LangSmith / Observability Mapping

| Failure Mode | RunReport Signal | LangSmith Observable |
|---|---|---|
| Rate limit storm | `top_error_classes.rate_limit > 0` with high count | Spike in 429 status |
| Provider degradation | `providers.X.cells_failed / cells_total > 0.3` | Elevated error rate |
| Fallback chain exhausted | `fallbacks.triggered > 0 AND fallbacks.successful == 0` | Both providers failing |
| Resume incomplete | mode="resume" and status="partial" | Old failed cells remain |
| Doctor post-run failure | `doctor.all_gates_passed = False` | Gate violation after ingestion |
| Fallback checkpoint missing FK | `fallback_triggered=1` and `raw_asset_id=NULL` | Fallback provider failed to store Bronze |

---

## Specific Failure Modes This Plan Prevents

1. **Silent partial failure**: `RunReport.status` explicitly signals "succeeded"/"failed"/"partial".
2. **Resume re-executes succeeded cells**: Reads ONLY from `source_checkpoints WHERE status IN ('failed','skipped')` — the DB is the source of truth, not a stale JSON file.
3. **Fallback on auth/rate-limit**: `FallbackPolicy.excluded_error_classes` prevents self-inflicted secondary storms.
4. **Orphan fallback raw_assets**: The failed primary checkpoint has `raw_asset_id=NULL`. Only the successful fallback's checkpoint carries its own raw_asset_id. No cross-contamination.
5. **Progress invisible during long runs**: `current_source`/`current_ticker`/`current_date`/`cells_done` updated per cell.
6. **Resume survives missing JSON**: If `data/run_reports/` is cleaned, resume still works because it reads `source_checkpoints`, not the JSON file.

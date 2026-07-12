# W1-C: OHLCV Execution and Fallback — Implementation Plan

> **For executor:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make OHLCV a first-class executor source with typed cell handler, precedence-aware storage, empty-valid lifecycle, and checkpoint state machine. All provider/fallback/policy choices gated behind architect decisions.

**Architecture:** Add `polygon_ohlcv` dispatch to `_fetch_cell`. Precedence-aware `upsert_ohlcv` uses a `SOURCE_PRECEDENCE` Python mapping with `INSERT ... ON CONFLICT DO UPDATE`. `FallbackPolicy` gains per-source-type chains. `empty_reason` migration (v7) on source_checkpoints. Checkpoint lifecycle filtering uses named configuration constants (pending AD-LIFECYCLE-1 approval). All implementation tests use mocked connectors and temp DBs.

**Tech Stack:** Python 3.13, sqlite3, pytest + pytest-asyncio, mock connectors, no live providers.

---

## 1. Scope

- OHLCV cell handler in `_fetch_cell` dispatch (source type `"polygon_ohlcv"`)
- Polygon `_build_ohlcv_url` adds `adjusted=true` (pending AD-OHLCV-2)
- Precedence-aware `upsert_ohlcv` with `SOURCE_PRECEDENCE` Python mapping
- Per-source-type fallback chains in `FallbackPolicy` (chain pending AD-OHLCV-1)
- `empty_reason` TEXT migration v7 on `source_checkpoints` (temp DB copies only)
- Checkpoint lifecycle filtering with named constants (pending AD-LIFECYCLE-1)
- Atomic four-way persistence: raw_asset + OHLCV row + checkpoint + fallback provenance in one transaction

**Out of scope:** Planner module changes, news/SEC handlers, service facade (W1-D), CLI (W1-F), live provider calls in automated tests.

---

## 2. Verified Current Implementation

| File:Symbol | Status |
|---|---|
| `update_pipeline.py:395` `_fetch_cell` | Dispatches polygon_news, finnhub, sec — no ohlcv |
| `connectors/polygon.py:34` `_build_ohlcv_url` | No `adjusted` param |
| `connectors/yfinance_fallback.py` | `create_yfinance_fetcher` exists |
| `connectors/fmp.py` | `create_fmp_fetcher` exists — OHLCV contract TBD |
| `storage/sqlite.py:603` `upsert_ohlcv` | `INSERT OR REPLACE` — no precedence |
| `fallback.py` `FallbackPolicy` | Single news chain only |
| `quality.py:55` `source_checkpoints` DDL | No `empty_reason` column |
| `migrations.py:27` `MIGRATIONS` | H4 registry; v7 next available |
| `orchestrator.py:202` | Calls `upsert_ohlcv` — inherits precedence automatically |

---

## 3. Proposed Files

| File | Status | Change |
|---|---|---|
| `catalyst_data/update_pipeline.py` | Modified | OHLCV dispatch, lifecycle filtering, atomic persistence |
| `catalyst_data/connectors/polygon.py` | Modified | `adjusted=true` (conditional on AD-OHLCV-2) |
| `catalyst_data/storage/sqlite.py` | Modified | Precedence-aware `upsert_ohlcv`, `SOURCE_PRECEDENCE` |
| `catalyst_data/fallback.py` | Modified | Per-source-type chains |
| `catalyst_data/migrations.py` | Modified | Register v7 (temp DB only during tests) |
| `packages/data-core/tests/test_ohlcv_execution.py` | **Proposed** | All OHLCV execution tests |

**Forbidden:** planner module, news/SEC handlers, `catalyst_agents/*`.

---

## 4. Contracts

### 4.1 Source vocabulary (explicit, never mixed)

| Context | Token |
|---|---|
| Executor source type (cell dispatch) | `"polygon_ohlcv"` |
| Persisted `ohlcv.source` column | `"polygon"` (existing, verified) or `"yfinance"` |
| Checkpoint `source_type` | `"polygon_ohlcv"` |
| Checkpoint `fallback_provider` | `"yfinance_ohlcv"` or `None` |

### 4.2 EMPTY_VALID specification (exact conditions)

`success_empty` is produced only when ALL of:
1. Transport succeeded (HTTP 200 or equivalent)
2. Response parsed successfully (valid JSON/structured data)
3. Date was a planned valid trading session (from calendar, not a holiday)
4. Provider explicitly returned no aggregate rows (empty `results` array for Polygon)
5. No auth/rate-limit/server/parse error present

An arbitrary falsy result (None, {}, []) from any other condition must NOT produce `success_empty`.

### 4.3 Precedence policy (one contract)

```python
SOURCE_PRECEDENCE: dict[str, int] = {
    "polygon": 100,
    "yfinance": 50,
}

class UnknownOHLCVSource(Exception):
    """Source has no entry in SOURCE_PRECEDENCE."""

class UpsertOutcome(Enum):
    BLOCKED = "blocked"       # lower rank — row unchanged
    UPDATED = "updated"       # equal or higher rank — row inserted/updated

def upsert_ohlcv(conn, *, symbol, date, open, high, low, close, volume,
                 source="polygon", commit=True):
    """Precedence-aware OHLCV upsert.

    Runs inside the caller's existing transaction (BEGIN IMMEDIATE is owned
    by _persist_ohlcv_cell, not by this function).  When commit=False, the
    caller controls commit/rollback.
    """
    rank = SOURCE_PRECEDENCE.get(source)
    if rank is None:
        raise UnknownOHLCVSource(f"Unknown OHLCV source: {source}")

    existing = conn.execute(
        "SELECT source FROM ohlcv WHERE symbol=? AND date=?",
        (symbol, date)
    ).fetchone()

    if existing:
        existing_rank = SOURCE_PRECEDENCE.get(existing[0], -1)
        if existing_rank is None:
            raise UnknownOHLCVSource(f"Unknown persisted source: {existing[0]}")
        if rank < existing_rank:
            return UpsertOutcome.BLOCKED

    conn.execute(
        "INSERT INTO ohlcv (symbol,date,open,high,low,close,volume,source) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol,date) DO UPDATE SET "
        "  open=excluded.open, high=excluded.high, low=excluded.low, "
        "  close=excluded.close, volume=excluded.volume, source=excluded.source",
        (symbol, date, open, high, low, close, volume, source),
    )
    if commit:
        conn.commit()
    return UpsertOutcome.UPDATED
```

Required tests:
- fallback cannot overwrite primary → UpsertOutcome.BLOCKED, zero rows changed
- primary overwrites fallback → UpsertOutcome.UPDATED, one canonical row
- same source updates existing canonical row → UpsertOutcome.UPDATED
- unknown source raises UnknownOHLCVSource and writes nothing
- caller-owned transaction remains active after upsert_ohlcv returns
- rollback of encompassing raw+ohlcv+checkpoint transaction removes every write
- `SOURCE_PRECEDENCE` is a configurable Python dict ✓

### 4.4 Atomic four-way persistence

```python
def _persist_ohlcv_cell(conn, run_id, ticker, date, ohlcv_bar, fetch_result, source, fallback_provider=None):
    """Write raw_asset + ohlcv row + success checkpoint in ONE transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 1. Write raw_asset
        raw_id = _insert_raw_asset(conn, ...)
        # 2. Write OHLCV bar
        upsert_ohlcv(conn, symbol=ticker, date=date, ..., source=_persisted_source(source), commit=False)
        # 3. Write success checkpoint
        write_source_checkpoint(conn, ..., status="success", raw_asset_id=raw_id,
                                fallback_provider=fallback_provider, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
```

Required atomicity tests:
- Failure after raw_asset insert rolls back all three
- Failure after OHLCV upsert rolls back raw + OHLCV + checkpoint
- Checkpoint failure rolls back raw + OHLCV
- Successful cell commits all records
- Repeat execution layered contract (verified against current
`compute_asset_id` + `upsert_raw_asset` + `upsert_ohlcv` implementation):

- Bronze (raw_assets) layer: `compute_asset_id(ticker, date, source_type, data_version)`
  produces a deterministic identity from (ticker, date, source, version).
  `upsert_raw_asset` uses `INSERT OR REPLACE` by asset_id — same identity
  replaces the row.  Same payload rerun does NOT create a duplicate raw_asset row.
- OHLCV layer: one canonical row per (symbol, date) via `INSERT OR REPLACE`
  (once W1-C adds precedence-aware upsert).
- Checkpoint layer: each run creates its own checkpoint row (run_id is part of
  checkpoint PK).

Tests:
- `test_raw_asset_replaced_on_rerun` — two runs with same ticker/date/source produce
  one raw_asset row (INSERT OR REPLACE by asset_id), second run overwrites first
- `test_ohlcv_row_is_single_canonical_per_symbol_date` — two runs, one ohlcv row
- `test_checkpoint_created_per_run` — two runs, two checkpoint rows

### 4.5 Fallback provenance tests

Check both:
- `source_checkpoints.fallback_provider` set correctly
- `ohlcv.source` set correctly (primary vs fallback)

### 4.6 Lifecycle constants (proposed, not approved)

| Constant | Proposed Default | Architect Decision |
|---|---|---|
| `SUCCESS_EMPTY_RECHECK_MAX` | 1 | AD-LIFECYCLE-1 |
| `SUCCESS_EMPTY_RECHECK_DELAY_DAYS` | 1 | AD-LIFECYCLE-1 |
| `SUCCESS_EMPTY_WINDOW_DAYS` | 5 | AD-LIFECYCLE-1 |
| `PERMANENT_FAILURE_THRESHOLD` | 3 | AD-LIFECYCLE-1 |
| `CHRONIC_TRANSIENT_THRESHOLD` | 5 | AD-LIFECYCLE-1 |

All lifecycle constants are defined as named module-level variables in a configurable location (e.g., `update_pipeline.py` top or a dedicated constants section). No magic numbers in planner/executor code or tests. Tests reference the named constants.

---

## 5. Error Taxonomy

| Error | When | Response |
|---|---|---|
| `SourcePrecedenceError` | Unknown source with no defined rank | Fail closed |
| `EmptyValidMisclassificationError` | EMPTY_VALID produced when conditions not met | Test assertion fails |
| Transport/Timeout | fetch_fn raises | Fallback chain (if configured); failed checkpoint |
| Auth/RateLimit | Provider rejects | Failed checkpoint; no fallback for auth errors |

---

## 6. Schema Impact

| Migration | Version | Owner | Statement |
|---|---|---|---|
| empty_reason | v7 | W1-C | `ALTER TABLE source_checkpoints ADD COLUMN empty_reason TEXT` |

**Migration tests use temp DB copies only.** Do not migrate the real Dev DB during automated implementation verification.

---

## 7. TDD Tasks

### Task 1: SOURCE_PRECEDENCE policy + atomic persistence

**Tests:**
- `test_primary_overwrites_fallback`
- `test_fallback_cannot_overwrite_primary`
- `test_same_source_retry_idempotent`
- `test_unknown_source_fails_closed`
- `test_atomic_rollback_on_checkpoint_failure`
- `test_atomic_rollback_on_ohlcv_failure`
- `test_atomic_all_records_committed_on_success`
- `test_raw_asset_replaced_on_rerun`
- `test_ohlcv_row_is_single_canonical_per_symbol_date`
- `test_checkpoint_created_per_run`

**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_ohlcv_execution.py -q -k "precedence or atomic" --tb=short` → all FAIL

**Implementation:** `SOURCE_PRECEDENCE` dict, precedence-aware `upsert_ohlcv`, atomic four-way persist.

**Green:** 8 PASS.

### Task 2: EMPTY_VALID + success_empty handler

**Tests:**
- `test_empty_valid_produced_on_no_bars_response`
- `test_empty_valid_not_produced_on_transport_error`
- `test_empty_valid_not_produced_on_parse_error`
- `test_empty_valid_not_produced_on_auth_error`
- `test_empty_valid_not_produced_on_arbitrary_falsy`

**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_ohlcv_execution.py -q -k "empty_valid" --tb=short` → FAIL

**Implementation:** Exact EMPTY_VALID condition check in OHLCV dispatch.

**Green:** 5 PASS.

### Task 3: OHLCV cell handler + fallback provenance

**Tests:**
- `test_ohlcv_handler_writes_bar_and_checkpoint`
- `test_ohlcv_fallback_sets_checkpoint_provenance`
- `test_ohlcv_fallback_sets_ohlcv_source`
- `test_ohlcv_fallback_on_transport_error` (conditional: only if chain configured per AD-OHLCV-1)

**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_ohlcv_execution.py -q -k "handler or fallback" --tb=short` → FAIL

**Implementation:** Add `"polygon_ohlcv"` dispatch to `_fetch_cell`. Fallback chain wiring conditional on AD-OHLCV-1.

**Green:** 3-4 PASS (count depends on AD-OHLCV-1).

### Task 4: empty_reason migration v7 (temp DB)

**Test:** `test_empty_reason_column_exists_after_v7_migration`

```python
def test_empty_reason_column_exists_after_v7_migration(self, tmp_path):
    db_path = str(tmp_path / "migrate.db")
    conn = sqlite3.connect(db_path)
    # Create pre-v7 schema
    conn.execute("""CREATE TABLE source_checkpoints (
        run_id TEXT, source_type TEXT, ticker TEXT, date TEXT, status TEXT,
        error_class TEXT, retries INTEGER DEFAULT 0,
        error_message_redacted TEXT, http_status INTEGER,
        retry_after_seconds REAL, provider_latency_ms REAL,
        raw_asset_id TEXT, items_count INTEGER,
        fallback_provider TEXT, fallback_triggered INTEGER DEFAULT 0)""")
    conn.commit(); conn.close()
    from catalyst_data.migrations import run_migrations
    conn = sqlite3.connect(db_path)
    run_migrations(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(source_checkpoints)")]
    assert "empty_reason" in cols
    conn.close()
```

**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_ohlcv_execution.py::test_empty_reason_column_exists_after_v7_migration -q --tb=short` → FAIL

**Implementation:** Register v7 in `MIGRATIONS` list.

**Green:** PASS. Temp DB only — real Dev DB untouched.

### Task 5: Lifecycle state machine (with named constants)

**Tests:**
- `test_success_empty_recheck_count_limited_by_constant`
- `test_permanent_failure_excluded_at_configured_threshold`
- `test_chronic_transient_flag_at_configured_threshold`

Tests read the named constants, don't embed magic numbers.

**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_ohlcv_execution.py -q -k "lifecycle" --tb=short` → FAIL

**Implementation:** Filter in `compute_missing_cells` using named constants.

**Green:** 3 PASS.

### Task 6: Focused regression + canonical

```
.venv/bin/python -m pytest packages/data-core/tests/test_ohlcv_execution.py -q --tb=short
.venv/bin/python -m pytest packages/data-core -q
```

No new failures.

---

## 8. Git Boundary

**Commit:** `feat(data-core): OHLCV first-class source with fallback`

**Stage manifest:**
- `packages/data-core/catalyst_data/connectors/polygon.py` — `adjusted=true` (if AD-OHLCV-2 approved)
- `packages/data-core/catalyst_data/storage/sqlite.py` — `SOURCE_PRECEDENCE`, precedence-aware `upsert_ohlcv`
- `packages/data-core/catalyst_data/fallback.py` — per-source-type chains
- `packages/data-core/catalyst_data/update_pipeline.py` — OHLCV dispatch, lifecycle filtering, atomic persistence
- `packages/data-core/catalyst_data/migrations.py` — v7
- `packages/data-core/tests/test_ohlcv_execution.py` — new test file

---

## 9. Landmines

1. Polygon single-day aggs return empty on holidays — but holidays never planned (calendar). Empty on planned day = valid empty.
2. yfinance date semantics must normalize to exchange date.
3. `orchestrator.py` tests may assume last-write-wins — pre-register as W3-B.
4. Migration tests must use temp DB copies — real Dev DB untouched during implementation.
5. No live provider calls in automated tests. Mocked connectors only.

---

## 10. Exit Gate

1. OHLCV atomic persistence (raw + bar + checkpoint in one transaction)
2. Precedence policy: fallback cannot overwrite primary
3. EMPTY_VALID only on exact conditions
4. Fallback provenance set on both checkpoint and OHLCV row (if chain configured)
5. Lifecycle state machine tests pass with named constants
6. Migration v7 applies to temp DB; `empty_reason` column exists
7. No new canonical failures

---

## 11. Orchestrator Verification

1. `PRAGMA table_info(source_checkpoints)` on temp migrated DB — `empty_reason` exists
2. Fixture: yfinance row overwrite polygon → must refuse
3. Fixture: transaction rollback test — half-failed write leaves zero records
4. Migration test applied to temp copy only — real Dev DB SHA unchanged
5. No live provider calls in any test output

---

## 12. Architect Decision Gates

| ID | Decision | Recommendation | Blocks Task |
|---|---|---|---|
| AD-OHLCV-1 | Fallback order: yfinance vs FMP | yfinance (connector exists, simpler) | Task 3 (fallback) |
| AD-OHLCV-2 | Adjusted-only vs raw vs both | Adjusted-only | Task 1 (Polygon URL) |
| AD-LIFECYCLE-1 | Lifecycle thresholds (1/5/3/5) | 1 recheck, 5d window, 3 permanent, 5 chronic | Task 5 (lifecycle) |

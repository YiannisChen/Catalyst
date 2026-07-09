# H2 — Runtime Resilience Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> **Dependency:** Must execute AFTER H1 (doctor/materialize-tiers/read-only dry-run). H3 + H4 depend on H2's error taxonomy and checkpoint columns.

**Goal:** Replace the coarse-grained 3-class `ErrorClass` enum with a 12-class taxonomy that drives retry/terminal/fallback decisions, and make the per-cell write atomic (one raw_asset row ↔ exactly one non-failed checkpoint, all within a single DB transaction with network I/O outside it).

**Architecture:** A new `error_taxonomy.py` module owns the taxonomy. `retry.py` re-exports the taxonomy and the decision table. All three source paths (polygon/Finnhub/SEC) in `_fetch_cell` and `orchestrator.process_request` are refactored to share a single connection + `BEGIN IMMEDIATE` transaction per cell — network I/O outside the transaction, then Bronze (raw_asset) + Silver (articles/filings) + checkpoint committed or rolled back atomically. `source_checkpoints` gets six new columns via additive ALTER TABLE; the status CHECK constraint is REMOVED from init_db DDL AND physically dropped from existing DBs via a table-rebuild migration (SQLite: CREATE new table without CHECK → INSERT INTO ... SELECT * → DROP old → ALTER TABLE RENAME), then enforced in `write_source_checkpoint` (app-layer validation, see F2). `FetchResult` grows `error_class` and structured error metadata. Split rollback semantics: TRANSPORT/TIMEOUT failures store nothing; MALFORMED_RESPONSE/PARSE_FAILURE store raw_asset + failed checkpoint forensically.

**Tech Stack:** Python 3.11, sqlite3 (WAL mode, BEGIN IMMEDIATE), asyncio. No new dependencies.

**Branch:** `ws4b/article-level-data`. DB: `data/catalyst_dev_ws4b.db`. Frozen DB: never change.

---

## Modules Touched

| File | Action | What Changes |
|------|--------|-------------|
| `catalyst_data/error_taxonomy.py` | **Create** | 12-class ErrorClass enum, `classify_fetch_error()` function, decision table |
| `catalyst_data/retry.py` | **Modify** | Delete old 3-class ErrorClass; re-export from error_taxonomy; adapt `_retry_rule_for_result` to new classes |
| `catalyst_data/quality.py` | **Modify** | Add 6 columns to source_checkpoints DDL; REMOVE status CHECK from DDL; **rebuild source_checkpoints on existing DBs** to drop legacy CHECK (CREATE-without-CHECK → copy → drop → rename, preserving PK + all columns); `write_source_checkpoint()` enforces valid status in app layer + accepts new params |
| `catalyst_data/connectors/base.py` | **Modify** | Add `error_class` field to `FetchResult` dataclass |
| `catalyst_data/update_pipeline.py` | **Modify** | Refactor `_fetch_cell` for atomic write across all three source paths; wire `classify_fetch_error`; split rollback by ErrorClass |
| `catalyst_data/orchestrator.py` | **Modify** | Thread single connection through `process_request` → `_store_bronze_and_silver` → checkpoint write, all within one `BEGIN IMMEDIATE`; accept external connection parameter |
| `tests/test_error_taxonomy.py` | **Create** | Full taxonomy + decision table tests |
| `tests/test_atomic_cell_write.py` | **Create** | Atomic cell write invariant tests including split rollback |
| `tests/test_retry.py` | **Modify** | Adapt to new taxonomy |
| `tests/test_quality_helpers.py` | **Modify** | Adapt to new checkpoint columns |

---

## Data Contract — H2 Owns

### ErrorClass Enum (`error_taxonomy.py`)

```python
class ErrorClass(str, Enum):
    AUTH = "auth"                          # 401, 403
    PERMISSION_PAID = "permission_paid"    # 402, 451
    RATE_LIMIT = "rate_limit"              # 429
    TIMEOUT = "timeout"                    # connect/read timeout (status==0 + timeout in error)
    TRANSPORT = "transport"                # DNS, connection refused, TLS errors
    PROVIDER_5XX = "provider_5xx"          # 500-599 (except recognized sub-codes)
    MALFORMED_RESPONSE = "malformed_response"  # 200 but unparseable body
    PARSE_FAILURE = "parse_failure"        # JSON decode error, schema mismatch
    EMPTY_VALID = "empty_valid"            # 200 + valid JSON + zero results
    BUDGET_EXHAUSTED = "budget_exhausted"  # DailyBudgetExhausted caught
    PARTIAL_SUCCESS = "partial_success"    # Some items stored, some failed
    UNKNOWN = "unknown"                    # Fallback for unclassified errors
```

### `classify_fetch_error()` Signature

```
classify_fetch_error(
    status_code: int | None,
    error_message: str | None,
    *,
    is_timeout: bool = False,
    exception_type: str | None = None,
) -> ErrorClass
```

Returns the appropriate `ErrorClass` based on status code, error message pattern matching, timeout flag, and exception type.

### Decision Table

| ErrorClass | Retry? | Terminal? | Fallback-Eligible? | Bronze Forensics? |
|---|---|---|---|---|
| AUTH | No | Yes | **No** | No |
| PERMISSION_PAID | No | Yes | **No** | No |
| RATE_LIMIT | Yes (with Retry-After) | After max_retries | **No** | No |
| TIMEOUT | Yes (exponential backoff) | After max_retries | Yes | No |
| TRANSPORT | Yes (limited) | After 2 retries | Yes | **No** — stores nothing |
| PROVIDER_5XX | Yes (exponential backoff) | After max_retries | Yes | No |
| MALFORMED_RESPONSE | No | Yes | Yes | **Yes** — stores raw_asset + failed checkpoint |
| PARSE_FAILURE | No | Yes | No | **Yes** — stores raw_asset + failed checkpoint |
| EMPTY_VALID | No | No (success-empty) | Yes | Yes (stores raw_asset + success_empty checkpoint) |
| BUDGET_EXHAUSTED | No | Yes (for day) | No | No |
| PARTIAL_SUCCESS | No | No | No | No |
| UNKNOWN | Yes (1 retry) | After 1 retry | Yes | No |

### Checkpoint Column Changes (`source_checkpoints`)

Six new columns added via additive ALTER:

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `error_message_redacted` | TEXT | NULL | Error text with API keys/secrets replaced by `[REDACTED]` |
| `http_status` | INTEGER | NULL | Raw HTTP status code observed |
| `retry_after_seconds` | REAL | NULL | Value from Retry-After header (if 429) |
| `provider_latency_ms` | REAL | NULL | Network round-trip time in milliseconds |
| `raw_asset_id` | TEXT | NULL | FK to raw_assets (filled on success/success_empty/malformed/parse_failure; NULL on transport/timeout) |
| `items_count` | INTEGER | NULL | Number of articles/filings/docs materialized from this cell |

**Status CHECK constraint REMOVED from DDL + physically dropped from existing DBs** (F2):

Problem: Removing the CHECK from the DDL only fixes fresh DBs. The existing dev DB still carries the legacy `CHECK (status IN ('pending','success','failed','skipped'))`, which rejects `success_empty` at runtime with `CHECK constraint failed`.

Solution — table-rebuild migration (runs once, idempotent):
```
1. CREATE TABLE source_checkpoints_new (
     -- same schema as source_checkpoints but WITHOUT the status CHECK
     -- includes all existing + new H2 columns
   )
2. INSERT INTO source_checkpoints_new SELECT * FROM source_checkpoints
3. DROP TABLE source_checkpoints
4. ALTER TABLE source_checkpoints_new RENAME TO source_checkpoints
5. Recreate indexes (idx_index_state_corpus, etc.)
6. PRAGMA user_version = <migration_version>
```

This migration is registered in H4's `MIGRATIONS` registry (version that runs AFTER v1 adds the six columns) and is safe because: (a) all data is preserved via `INSERT ... SELECT *`, (b) the PK `(run_id, source_type, ticker, date)` is preserved, (c) existing rows with legacy-valid statuses ('pending','success','failed','skipped') all copy fine — `success_empty` only appears in NEW rows written after migration.

After the table rebuild, `write_source_checkpoint` enforces valid status via app-layer assertion:
```python
_VALID_CHECKPOINT_STATUSES = frozenset({"pending", "success", "success_empty", "failed", "skipped"})
assert status in _VALID_CHECKPOINT_STATUSES, f"Invalid checkpoint status: {status}"
```

Fresh `init_db` creates `source_checkpoints` without any CHECK. Migrated DBs have the CHECK physically dropped. H4 drift test compares honestly (no CHECK normalization needed).

### `FetchResult` Extension (`connectors/base.py`)

```python
@dataclass
class FetchResult:
    status: int
    data: dict | None = None
    error: str | None = None
    latency_ms: float = 0.0
    source_label: str = ""
    retry_after_seconds: float | None = None
    error_class: str | None = None        # NEW: ErrorClass value
    items_count: int | None = None        # NEW: number of items in data
```

---

## Logic / State Flow

### 1. Per-Cell Atomic Transaction (Single Connection Threaded Through All Layers)

Current state: Three separate `sqlite3.connect(db_path)` calls per cell — one in `_fetch_cell` for checkpoint, one in `upsert_raw_asset`, one in `orchestrator.process_request` for articles. No atomicity.

Target state — **one connection threaded through all layers**, network I/O outside the transaction:

```
async fetch completes → FetchResult returned
  → conn = sqlite3.connect(db_path)
  → conn.execute("PRAGMA query_only = OFF")  (if needed, dev DB only)
  → conn.execute("BEGIN IMMEDIATE")
  → classify result:
      ├─ TRANSPORT / TIMEOUT (no response received):
      │    → ROLLBACK — store nothing (no raw_asset, no checkpoint)
      │    → write_source_checkpoint on a SEPARATE connection (non-transactional)
      │       with status="failed", error_class, raw_asset_id=NULL
      │
      ├─ MALFORMED_RESPONSE / PARSE_FAILURE (response received, unparseable):
      │    → upsert_raw_asset(conn, ...) — Bronze forensics
      │    → write_source_checkpoint(conn, ...) with status="failed",
      │       raw_asset_id=<bronze_id>, items_count=NULL
      │    → COMMIT — both Bronze and failed checkpoint persist atomically
      │
      ├─ EMPTY_VALID (valid response, zero items):
      │    → upsert_raw_asset(conn, ...)
      │    → write_source_checkpoint(conn, ...) with status="success_empty",
      │       raw_asset_id=<bronze_id>, items_count=0
      │    → COMMIT
      │
      └─ SUCCESS (valid response, items > 0):
           → upsert_raw_asset(conn, ...) — Bronze
           → For polygon_news: process_request (receive external conn) →
               _store_bronze_and_silver → upsert articles ON THE SAME conn
           → For finnhub: _fetch_cell_finnhub writes on THE SAME conn
           → For SEC: upsert_filing + upsert_filing_document ON THE SAME conn
           → write_source_checkpoint(conn, ...) with status="success",
             raw_asset_id=<bronze_id>, items_count=N
           → COMMIT — all three layers commit or rollback together
  → conn.close()
```

For the polygon path, `orchestrator.process_request` is refactored to accept an optional `conn` parameter. When present, `_store_bronze_and_silver` uses it instead of opening its own. When absent (standalone tests), it opens its own as before.

**Connection threading for polygon path specifically:**
```
_fetch_cell opens conn, BEGIN IMMEDIATE
  → upsert_raw_asset(conn, asset_id=..., ...)     -- Bronze on shared conn
  → process_request(..., conn=conn)                -- receives shared conn
      → _store_bronze_and_silver(conn, ...)        -- Silver on same conn
          → INSERTS into articles, article_tickers, clean_assets
  → write_source_checkpoint(conn, ...)             -- Checkpoint on same conn
  → COMMIT
```

Invariant: **Every materialized `raw_asset` row has exactly one non-failed checkpoint** (status = "success" or "success_empty"). No orphan raw_assets without checkpoints. No duplicate checkpoints for the same asset. The only exception: MALFORMED_RESPONSE/PARSE_FAILURE stores raw_asset with status="failed" (forensic preservation).

### 2. `classify_fetch_error` Flow

```
Input: FetchResult (status, error, latency_ms) or raw Exception
  → If status 401/403 → AUTH
  → If status 402/451 → PERMISSION_PAID
  → If status 429 → RATE_LIMIT
  → If status==0 and is_timeout → TIMEOUT
  → If status 500-599 → PROVIDER_5XX
  → If status 200 and data is None/empty → EMPTY_VALID
  → If status 200 and data parse fails → MALFORMED_RESPONSE
  → If JSONDecodeError → PARSE_FAILURE
  → If DailyBudgetExhausted → BUDGET_EXHAUSTED
  → If exception is ConnectionError/OSError → TRANSPORT
  → Else → UNKNOWN
```

### 3. Retry Decision Flow

```
classify_fetch_error(result) → ErrorClass
  → Look up decision table for this class
  → If terminal: stop immediately, return result
  → If retryable and attempt < max_retries:
      → compute backoff from rule (rate_limit/5xx/timeout)
      → sleep(backoff)
      → retry
  → If fallback-eligible: signal to orchestrator
  → If RATE_LIMIT: use retry_after_seconds from header
  → If all retries exhausted: mark terminal, return
```

### 4. Checkpoint Status Transition (Split Rollback)

```
cell starts → no checkpoint row (implied pending)
  → network I/O returns response:
      ├─ 200 + valid + N>0 items:
      │    → COMMIT (raw_asset + articles) + status "success", raw_asset_id set
      ├─ 200 + valid + 0 items:
      │    → COMMIT (raw_asset) + status "success_empty", raw_asset_id set
      ├─ 200 + malformed / parse failure:
      │    → COMMIT (raw_asset FORENSIC) + status "failed", raw_asset_id set
      └─ 5xx / other non-retryable:
           → COMMIT (raw_asset FORENSIC if response received) + status "failed"
  → network I/O had NO response (transport/timeout):
      → NO raw_asset stored (rolled back or never written)
      → status "failed" written on SEPARATE non-transactional connection
      → raw_asset_id = NULL
  → previous run's cell was "success" → skip (idempotent)
```

---

## TDD Test Gate List

| # | Test | What It Catches |
|---|------|----------------|
| 2.1 | `test_classify_fetch_error_auth_401` | HTTP 401 → ErrorClass.AUTH |
| 2.2 | `test_classify_fetch_error_auth_403` | HTTP 403 → ErrorClass.AUTH |
| 2.3 | `test_classify_fetch_error_permission_paid_402` | HTTP 402 → ErrorClass.PERMISSION_PAID |
| 2.4 | `test_classify_fetch_error_rate_limit_429` | HTTP 429 → ErrorClass.RATE_LIMIT |
| 2.5 | `test_classify_fetch_error_timeout` | status=0 + "timeout" in error → ErrorClass.TIMEOUT |
| 2.6 | `test_classify_fetch_error_transport` | ConnectionRefusedError → ErrorClass.TRANSPORT |
| 2.7 | `test_classify_fetch_error_provider_5xx` | HTTP 500/502/503/504 → ErrorClass.PROVIDER_5XX |
| 2.8 | `test_classify_fetch_error_malformed_200` | HTTP 200 + unparseable JSON → ErrorClass.MALFORMED_RESPONSE |
| 2.9 | `test_classify_fetch_error_parse_failure` | json.JSONDecodeError → ErrorClass.PARSE_FAILURE |
| 2.10 | `test_classify_fetch_error_empty_valid` | HTTP 200 + valid JSON + 0 results → ErrorClass.EMPTY_VALID |
| 2.11 | `test_classify_fetch_error_budget_exhausted` | DailyBudgetExhausted → ErrorClass.BUDGET_EXHAUSTED |
| 2.12 | `test_classify_fetch_error_partial_success` | Explicit PARTIAL_SUCCESS return code |
| 2.13 | `test_classify_fetch_error_unknown` | Unrecognized error pattern → ErrorClass.UNKNOWN |
| 2.14 | `test_classify_fetch_error_none_status` | status=None + no error → ErrorClass.UNKNOWN |
| 2.15 | `test_decision_table_auth_no_retry_no_fallback` | AUTH: terminal, not retryable, not fallback-eligible |
| 2.16 | `test_decision_table_rate_limit_retry_no_fallback` | RATE_LIMIT: retryable, not fallback-eligible |
| 2.17 | `test_decision_table_timeout_fallback` | TIMEOUT: retryable, fallback-eligible |
| 2.18 | `test_decision_table_transport_fallback` | TRANSPORT: retryable, fallback-eligible |
| 2.19 | `test_atomic_cell_success_writes_all_three_layers` | Successful cell → raw_asset + articles + checkpoint ALL exist, on same transaction |
| 2.20 | `test_atomic_cell_empty_writes_success_empty` | Empty valid response → raw_asset exists AND checkpoint status="success_empty" |
| 2.21 | `test_atomic_cell_transport_stores_nothing` | TRANSPORT/TIMEOUT → NO raw_asset row exists, checkpoint has raw_asset_id=NULL |
| 2.22 | `test_atomic_cell_transport_checkpoint_exists` | TRANSPORT → failed checkpoint row EXISTS on separate connection (no raw_asset FK) |
| 2.23 | `test_atomic_cell_malformed_stores_raw_asset` | MALFORMED_RESPONSE → raw_asset row EXISTS (forensic) + failed checkpoint with raw_asset_id FK, atomically |
| 2.24 | `test_atomic_cell_malformed_checkpoint_failed` | MALFORMED_RESPONSE → checkpoint status="failed", raw_asset_id points to forensic Bronze |
| 2.25 | `test_atomic_cell_no_orphan_raw_asset` | Rollback on mid-transaction failure → NO raw_asset row survives |
| 2.26 | `test_atomic_cell_one_checkpoint_per_pk` | Success → exactly 1 non-failed checkpoint row per (run_id, source_type, ticker, date) |
| 2.27 | `test_checkpoint_has_new_columns` | After migration: error_message_redacted, http_status, retry_after_seconds, provider_latency_ms, raw_asset_id, items_count all exist |
| 2.28 | `test_checkpoint_no_status_check_in_ddl` | PRAGMA table_info(source_checkpoints) → no CHECK constraint on status column (app-layer only) |
| 2.29 | `test_success_empty_on_legacy_db_after_migration` | DB created from legacy DDL (WITH status CHECK) → after table-rebuild migration → write_source_checkpoint(status="success_empty") succeeds, no CHECK constraint failure |
| 2.30 | `test_write_checkpoint_rejects_invalid_status` | write_source_checkpoint(status="bogus") → AssertionError |
| 2.31 | `test_checkpoint_error_message_redacted` | API key "sk-abc123" in error → stored as "[REDACTED]" |
| 2.32 | `test_fetch_result_has_error_class_field` | FetchResult.error_class is set after classify |
| 2.33 | `test_retry_with_retry_stops_on_terminal_auth` | with_retry on AUTH → no retry, immediate return |
| 2.34 | `test_retry_with_retry_uses_retry_after` | with_retry on 429 with Retry-After=5 → waits exactly 5s |
| 2.35 | `test_process_request_shares_connection` | process_request with conn= param → writes articles on shared conn, does NOT open own |

---

## Execution Order Within H2

```
T2.1: Create error_taxonomy.py (enum + classify + decision table)
T2.2: Test error_taxonomy.py (tests 2.1–2.18)
T2.3: Modify FetchResult in connectors/base.py
T2.4: Modify source_checkpoints: add 6 columns to DDL, REMOVE status CHECK from DDL, rebuild table on existing DBs to drop legacy CHECK, wire app-layer validation (tests 2.27–2.30)
T2.5: Refactor write_source_checkpoint: app-layer status validation, new params, secret redaction (test 2.32)
T2.6: Refactor orchestrator.process_request: accept optional conn param, thread to _store_bronze_and_silver (test 2.35)
T2.7: Refactor _fetch_cell for atomic transaction + split rollback (tests 2.19–2.26)
T2.8: Adapt retry.py to new taxonomy (tests 2.32–2.33)
T2.9: Wire classify_fetch_error into all three fetch paths
```

---

## LangSmith / Observability Mapping

| Failure Mode | ErrorClass | Observable Signal |
|---|---|---|
| API key expired/invalid | AUTH | `error_class="auth"`, http_status=401; doctor flags in P0 |
| Rate limit hit | RATE_LIMIT | `error_class="rate_limit"`, http_status=429, `retry_after_seconds` populated |
| Provider down | PROVIDER_5XX | `error_class="provider_5xx"`, http_status=500-599; fallback chain triggered |
| Network partition | TIMEOUT | `error_class="timeout"`, latency_ms null (no response); no raw_asset stored |
| DNS failure | TRANSPORT | `error_class="transport"`, error_message_redacted contains socket error; no raw_asset |
| Provider changed API shape | MALFORMED_RESPONSE | `error_class="malformed_response"`, http_status=200; raw_asset stored forensically, checkpoint failed |
| Provider returns zero results | EMPTY_VALID | `error_class="empty_valid"`, status="success_empty"; downstream staleness detected |
| Daily budget hit | BUDGET_EXHAUSTED | `error_class="budget_exhausted"`; all remaining cells for that provider fail |

---

## Specific Failure Modes This Plan Prevents

1. **Orphan raw_asset**: Currently, a cell can succeed at Bronze but fail at Silver → raw_asset exists with no checkpoint. After fix: `BEGIN IMMEDIATE` rolls back both or neither.
2. **Silent transport failures leave no trace**: Currently, transport errors might or might not write a checkpoint. After fix: transport/timeout always writes a failed checkpoint on a separate connection (no raw_asset), ensuring the cell is marked for retry/resume.
3. **Unparseable responses lost**: Currently, malformed 200 responses are discarded. After fix: raw_asset stored forensically with a failed checkpoint, enabling post-mortem debugging.
4. **Auth errors retried indefinitely**: Old 3-class system treated 401/403 as RETRYABLE. New taxonomy marks AUTH as terminal immediately.
5. **Rate limit storms fallback**: Old system could trigger fallback on rate limits. New decision table: RATE_LIMIT → no fallback.
6. **API key leakage in error logs**: Old error_class stored raw exception strings. New: `error_message_redacted` with regex-based secret scrubbing.

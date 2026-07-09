# H4 — Schema Safety & Hygiene Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> **Dependency:** Must execute AFTER H2 + H3 (their columns are registered as migrations v1–vN). H4 is the final hardening phase — no further schema changes are planned after this.

**Goal:** Establish a `PRAGMA user_version`-based ordered migration registry, retro-register all H2/H3 additive columns, create a drift test verifying that a fresh `init_db` matches a migrated old DB, centralize DB paths in `db_paths.py` with a frozen-DB write guard, delete the 0-byte shadow file, and replace the fragile `Path(__file__)` walk in `live_runner.py`.

**Architecture:** A new `migrations.py` module owns the registry and `run_migrations()`. `init_db()` folds `_QUALITY_TABLES_SQL` into its DDL (so source_checkpoints/ingestion_runs are created before migrations run) and calls `run_migrations()` after all CREATE TABLE statements. `ensure_ingestion_quality_tables` is kept as a thin shim delegating to `init_db` (for backward compat across 9 callers). `db_paths.py` is the single source of truth for DB paths. The drift test creates two in-memory DBs and diffs `sqlite_master`. `run_migrations` catches `OperationalError` per ALTER statement (F4) and handles partial column existence (3 of 6 already present).

**Tech Stack:** Python 3.11, sqlite3, pathlib. No new dependencies.

**Branch:** `ws4b/article-level-data`. DB: `data/catalyst_dev_ws4b.db`. Frozen DB path: `data/catalyst_eval_frozen_v2.db` (SHA `0dfc81b1…ecdcdf`).

---

## Modules Touched

| File | Action | What Changes |
|------|--------|-------------|
| `catalyst_data/migrations.py` | **Create** | `MIGRATIONS` registry, `run_migrations()` with per-statement OperationalError catch |
| `catalyst_data/db_paths.py` | **Create** | `DEV_DB`, `FROZEN_DB`, `APP_DB`, `assert_writable()` |
| `catalyst_data/storage/sqlite.py` | **Modify** | `init_db()` folds `_QUALITY_TABLES_SQL` into main DDL; calls `run_migrations()` after all CREATE TABLE; `ensure_ingestion_quality_tables` becomes thin shim |
| `catalyst_data/live_runner.py` | **Modify** | Replace `Path(__file__)` walk with `db_paths.FROZEN_DB` + `assert_writable()` |
| `catalyst_data/coverage_audit.py` | **Modify** | Replace `Path(__file__)` walk with `db_paths.FROZEN_DB` |
| `catalyst_data/cli_index.py` | **Modify** | Replace `Path(__file__)` walk in `cmd_reconcile_schema` frozen guard with `db_paths.FROZEN_DB` |
| `tests/test_migrations.py` | **Create** | Drift test, ordering test, idempotency test, partial-column test |
| `tests/test_db_paths.py` | **Create** | assert_writable tests, path resolution tests |

**Also:** Delete `packages/data-core/data/catalyst_eval_frozen_v2.db` (0-byte shadow). Covered by `*.db` in `.gitignore`.

---

## Data Contract — H4 Owns

### Migration Registry (`migrations.py`)

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Migration:
    version: int          # PRAGMA user_version value
    name: str             # Human-readable label
    statements: list[str] # Ordered ALTER TABLE statements (each may raise OperationalError independently)
    reversible: bool = False

MIGRATIONS: list[Migration] = [
    Migration(version=1, name="h2_checkpoint_columns", statements=[
        "ALTER TABLE source_checkpoints ADD COLUMN error_message_redacted TEXT",
        "ALTER TABLE source_checkpoints ADD COLUMN http_status INTEGER",
        "ALTER TABLE source_checkpoints ADD COLUMN retry_after_seconds REAL",
        "ALTER TABLE source_checkpoints ADD COLUMN provider_latency_ms REAL",
        "ALTER TABLE source_checkpoints ADD COLUMN raw_asset_id TEXT",
        "ALTER TABLE source_checkpoints ADD COLUMN items_count INTEGER",
    ]),
    Migration(version=2, name="h2_drop_legacy_status_check", statements=[
        # Rebuilds source_checkpoints without the status CHECK constraint.
        # SQLite does not support ALTER TABLE DROP CHECK, so we create a new
        # table (same schema minus the CHECK), copy all rows, drop old, rename.
        # Preserves PK (run_id, source_type, ticker, date) and all indexes.
        "-- h2_drop_legacy_status_check: --",
        "CREATE TABLE source_checkpoints_new (run_id TEXT NOT NULL, source_type TEXT NOT NULL, ticker TEXT NOT NULL, date TEXT NOT NULL, status TEXT NOT NULL, error_class TEXT, retries INTEGER NOT NULL DEFAULT 0, error_message_redacted TEXT, http_status INTEGER, retry_after_seconds REAL, provider_latency_ms REAL, raw_asset_id TEXT, items_count INTEGER, PRIMARY KEY (run_id, source_type, ticker, date))",
        "INSERT INTO source_checkpoints_new SELECT run_id, source_type, ticker, date, status, error_class, retries, error_message_redacted, http_status, retry_after_seconds, provider_latency_ms, raw_asset_id, items_count FROM source_checkpoints",
        "DROP TABLE source_checkpoints",
        "ALTER TABLE source_checkpoints_new RENAME TO source_checkpoints",
        "CREATE INDEX IF NOT EXISTS idx_source_checkpoints_run ON source_checkpoints(run_id)",
        "CREATE INDEX IF NOT EXISTS idx_source_checkpoints_cell ON source_checkpoints(source_type, ticker, date)",
    ]),
    Migration(version=3, name="h2_fetch_result_error_class", statements=[
        "-- No DDL change. FetchResult.error_class is a Python-only field. Registered for ordering.",
    ]),
    Migration(version=4, name="h3_ingestion_run_progress", statements=[
        "-- No DDL change. FetchResult.error_class is Python-only. Registered for ordering.",
    ]),
    Migration(version=5, name="h3_fallback_checkpoint_columns", statements=[
        "ALTER TABLE source_checkpoints ADD COLUMN fallback_provider TEXT",
        "ALTER TABLE source_checkpoints ADD COLUMN fallback_triggered INTEGER DEFAULT 0",
    ]),
]
```

### `run_migrations()` Logic (F4 — per-statement catch)

```
run_migrations(conn)
  → current_version = conn.execute("PRAGMA user_version").fetchone()[0]
  → For each Migration in MIGRATIONS sorted by version:
      → If migration.version > current_version:
          → all_applied = True
          → For each statement in migration.statements:
              → Skip if statement starts with "--" (comment-only migration)
              → Try:
                  → conn.execute(statement)
              → On sqlite3.OperationalError:
                  → If "duplicate column name" or "already exists" in error:
                      → Log: f"Column already exists, skipping: {statement[:80]}..."
                      → Continue to next statement
                  → Else: re-raise
          → conn.execute(f"PRAGMA user_version = {migration.version}")
          → Log: f"Applied migration v{migration.version} ({migration.name})"
  → Return final user_version
```

Key property: **per-statement idempotent** (F4). If a migration has 6 ALTER TABLE statements and 3 columns already exist (common on real dev DBs where H2 columns were added out-of-band), the other 3 are applied. `user_version` is bumped to the migration's version regardless — the migration is considered "applied" when all its statements either succeed or are skipped as already-existing.

### `init_db()` Integration (F5)

```
init_db(conn)
  → PRAGMAs (WAL, sync, timeout, foreign_keys)
  → CREATE TABLE IF NOT EXISTS for all tables:
      → raw_assets, clean_assets, ohlcv, news_alignment, attributions,
        golden_events, index_manifests, index_state
      → ingestion_runs             ← folded from _QUALITY_TABLES_SQL
      → source_checkpoints         ← folded from _QUALITY_TABLES_SQL (NO status CHECK!)
      → asset_quality_flags        ← folded from _QUALITY_TABLES_SQL
      → articles (via ensure_articles_table)
      → filings + filing_documents (via ensure_filings_tables)
      → macro_observations (via ensure_macro_tables)
  → CREATE INDEX IF NOT EXISTS for all indexes
  → ensure_clean_provenance(conn)
  → run_migrations(conn)           ← NEW: apply ordered migrations v1–v5
  → conn.commit()
```

**`ensure_ingestion_quality_tables` preserved as thin shim** (F5):

```python
def ensure_ingestion_quality_tables(conn):
    """Thin shim — delegates to init_db for backward compat with ~9 callers.

    Callers in update_pipeline.py, live_runner.py, cli_index.py, tests, etc.
    all call this. It is now a no-op if init_db already ran, or a full init
    if called standalone.
    """
    init_db(conn)
```

No caller changes needed — all 9 existing call sites continue to work.

### `db_paths.py` (Single Source of Truth)

```python
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

DEV_DB = _PROJECT_ROOT / "data" / "catalyst_dev_ws4b.db"
FROZEN_DB = _PROJECT_ROOT / "data" / "catalyst_eval_frozen_v2.db"
APP_DB = _PROJECT_ROOT / "data" / "catalyst_app.db"

def assert_writable(db_path: Path | str) -> None:
    """Raise RuntimeError if db_path is the frozen DB or a 0-byte file with 'frozen' in name."""
    resolved = Path(db_path).resolve()
    frozen_resolved = FROZEN_DB.resolve()

    if resolved == frozen_resolved:
        raise RuntimeError(
            f"Refusing to write to frozen eval DB: {resolved}\n"
            f"Use the dev DB: {DEV_DB}"
        )

    if resolved.exists() and resolved.stat().st_size == 0 and "frozen" in resolved.name.lower():
        raise RuntimeError(
            f"Refusing to write to 0-byte frozen DB shadow: {resolved}\n"
            f"This file is a stale copy artifact. Delete it and use: {DEV_DB}"
        )
```

### Drift Test Invariant (Honest Comparison)

```
test_schema_drift:
  → Create fresh DB via init_db (in-memory)
  → Dump full schema: SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name
  → Also dump: PRAGMA table_info(...) for each table (column names + types)
  → Also dump: PRAGMA foreign_key_list(...) for each table
  → Create old DB (in-memory):
      → Run baseline DDL (CREATE TABLE statements without H2/H3 columns)
      → Include source_checkpoints WITH the legacy status CHECK (simulates pre-H2 state)
      → Run run_migrations() v1–v5
  → Dump full schema from migrated DB (same method, including CHECK constraints)
  → Assert: fresh schema == migrated schema (tables, columns, indexes, CHECK constraints ALL match)
  → PRAGMA user_version on both == max(v5)
```

Since H2's v2 migration physically drops the legacy CHECK via table rebuild, fresh and migrated DBs have genuinely identical DDL — no normalization needed. The drift test compares CHECK constraints honestly; any divergence is a real bug.

---

## Logic / State Flow

### 1. Migration Execution (Per-Statement Idempotent)

```
Application starts
  → sqlite3.connect(db_path)
  → init_db(conn)
      → CREATE TABLE IF NOT EXISTS source_checkpoints (...)  -- no status CHECK
      → CREATE TABLE IF NOT EXISTS ingestion_runs (...)
      → CREATE TABLE IF NOT EXISTS asset_quality_flags (...)
      → ... all other tables ...
      → run_migrations(conn)
          → PRAGMA user_version → 0
          → v1: 6 ALTER TABLE statements
              → On real dev DB: 3 columns already exist (added by early H2 dev),
                3 are new → 3 OperationalErrors caught, 3 succeed
              → user_version = 1
          → v2: table rebuild (drop legacy CHECK), user_version = 2
          → v3: comment-only (Python field), user_version = 3
          → v4: comment-only (Python field), user_version = 4
              → On fresh DB: all 8 succeed
              → On real dev DB: all 8 are new (H3 not applied yet)
              → user_version = 4
          → v6: 2 ALTER TABLE statements (fallback columns)
              → user_version = 5
      → conn.commit()
```

### 2. Assert Writable Guard

```
Any write entrypoint:
  → assert_writable(db_path)
      → Resolve path realpath
      → Compare to FROZEN_DB realpath → raise if match
      → Check 0-byte + "frozen" in name → raise if match
  → Proceed
```

Replace all three existing `Path(__file__).resolve().parent.parent.parent.parent / "data" / "catalyst_eval_frozen_v2.db"` patterns with `db_paths.FROZEN_DB`.

### 3. 0-byte Shadow Cleanup

File: `packages/data-core/data/catalyst_eval_frozen_v2.db` (0 bytes, Jul 3 11:33). Delete it. Covered by `*.db` gitignore. `assert_writable` 0-byte guard prevents accidental recreation.

---

## TDD Test Gate List

| # | Test | What It Catches |
|---|------|----------------|
| 4.1 | `test_migrations_ordered_by_version` | MIGRATIONS sorted ascending by version |
| 4.2 | `test_migrations_no_duplicate_versions` | No two entries share the same version |
| 4.3 | `test_run_migrations_idempotent` | Running twice does not error; user_version unchanged on second run |
| 4.4 | `test_run_migrations_sets_user_version` | PRAGMA user_version == max version after run (v6) |
| 4.5 | `test_run_migrations_skips_applied` | Second run logs 0 "Applied migration" messages |
| 4.6 | `test_run_migrations_per_statement_catch` | Migration with 6 statements, 3 already-existing → 3 skipped, 3 applied, user_version still bumped |
| 4.7 | `test_run_migrations_partial_columns_bumps_version` | DB seeded with 3 of v1's 6 columns → other 3 added, user_version → 1 |
| 4.8 | `test_run_migrations_unknown_operational_error_reraises` | Non-duplicate OperationalError (e.g., syntax error) → not caught, propagates |
| 4.9 | `test_schema_drift_fresh_vs_migrated` | Fresh init_db full schema == migrated-DB full schema (tables, columns, indexes, CHECK constraints all match) |
| 4.10 | `test_schema_drift_includes_check_constraints` | Drift test compares CHECK constraints honestly; source_checkpoints.status CHECK is absent in BOTH fresh and migrated after v2 migration |
| 4.11 | `test_schema_drift_detects_real_difference` | Intentional column mismatch → test FAILS (inverse validated) |
| 4.12 | `test_db_paths_dev_db_exists` | DEV_DB path resolves to existing file |
| 4.13 | `test_db_paths_frozen_db_exists` | FROZEN_DB path resolves to non-zero-byte file |
| 4.14 | `test_assert_writable_blocks_frozen` | assert_writable(FROZEN_DB) → RuntimeError |
| 4.15 | `test_assert_writable_blocks_zero_byte_frozen` | 0-byte file with "frozen" → RuntimeError |
| 4.16 | `test_assert_writable_allows_dev_db` | assert_writable(DEV_DB) → no error |
| 4.17 | `test_assert_writable_allows_new_path` | assert_writable("/tmp/nonexistent.db") → no error |
| 4.18 | `test_live_runner_uses_db_paths` | live_runner frozen guard calls assert_writable(), no Path(__file__) walk |
| 4.19 | `test_coverage_audit_uses_db_paths` | coverage_audit frozen guard uses db_paths.FROZEN_DB |
| 4.20 | `test_cli_reconcile_uses_db_paths` | cmd_reconcile_schema frozen guard uses db_paths.FROZEN_DB |
| 4.21 | `test_zero_byte_shadow_deleted` | packages/data-core/data/catalyst_eval_frozen_v2.db does not exist |
| 4.22 | `test_init_db_folds_quality_tables` | After init_db: ingestion_runs, source_checkpoints, asset_quality_flags all exist without calling ensure_ingestion_quality_tables |
| 4.23 | `test_ensure_ingestion_quality_tables_is_thin_shim` | Calling ensure_ingestion_quality_tables(conn) after init_db(conn) is a no-op |
| 4.24 | `test_init_db_runs_migrations` | After init_db, PRAGMA user_version == 5 and all columns exist |
| 4.25 | `test_init_db_fresh_equals_migrated_full` | Full integration: fresh DB schema == migrated DB schema (honest comparison, CHECK constraints included) |

---

## Execution Order Within H4

```
T4.1: Delete 0-byte shadow file
T4.2: Create db_paths.py (DEV_DB, FROZEN_DB, APP_DB, assert_writable)
T4.3: Test db_paths (tests 4.12–4.17)
T4.4: Create migrations.py with per-statement catch (tests 4.1–4.2)
T4.5: Fold _QUALITY_TABLES_SQL into init_db; make ensure_ingestion_quality_tables a thin shim (tests 4.22–4.23)
T4.6: Integrate run_migrations into init_db
T4.7: Test migration idempotency + per-statement catch (tests 4.3–4.8)
T4.8: Write drift test with honest CHECK comparison (tests 4.9–4.11)
T4.9: Replace Path(__file__) walks in live_runner.py (test 4.18)
T4.10: Replace Path(__file__) walks in coverage_audit.py (test 4.19)
T4.11: Replace Path(__file__) walks in cli_index.py (test 4.20)
T4.12: Full integration test (test 4.25)
T4.13: Verify shadow deletion (test 4.21)
```

---

## LangSmith / Observability Mapping

| Failure Mode | Observable Signal | Root Cause |
|---|---|---|
| Schema drift | Drift test FAILS in CI | Migration missing from registry or DDL mismatch |
| Migration applied out of order | `PRAGMA user_version` skips a version | Missing Migration entry |
| Frozen DB corrupted | `assert_writable` RuntimeError in logs | Guard bypassed or path misconfigured |
| 0-byte shadow recreated | `assert_writable` catches 0-byte frozen file | Copy artifact |
| Partial migration failure | OperationalError not "duplicate column" → propagated to caller | Real schema issue, not idempotency |
| CHECK constraint mismatch | Drift test FAILS honestly | H2 v2 migration didn't drop legacy CHECK — investigate |

---

## Specific Failure Modes This Plan Prevents

1. **Schema drift**: Fresh DB ≠ migrated DB. After fix: drift test catches any divergence honestly — CHECK constraints, columns, indexes all compared.
2. **Fragile path resolution**: `Path(__file__).parent.parent.parent.parent` breaks if modules move. After fix: `db_paths.py` single source of truth.
3. **Accidental frozen DB mutation**: Any code can open frozen DB for write. After fix: `assert_writable()` at every write entrypoint.
4. **0-byte shadow confusion**: After fix: deleted + guard prevents recreation.
5. **Migration partial-application failure**: 3 of 6 columns already present → migration fails entirely. After fix: per-statement OperationalError catch skips existing columns, applies the rest, bumps user_version.
6. **9 call sites break when quality tables move**: After fix: `ensure_ingestion_quality_tables` stays as thin shim, all callers unchanged.

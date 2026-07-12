# W1-A: Zero-Write Update Planner — Implementation Plan

> **For executor:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extract pure read-only update planning into `update_planner.py` with a deterministic `UpdatePlan` and `plan_hash`; redirect both dry_run paths to delegate to the planner.

**Architecture:** New module `catalyst_data/update_planner.py` opens the SQLite DB in read-only mode (`mode=ro`, `uri=True`). It computes the universe (explicit config or OHLCV-derived compatibility fallback with typed `universe-fallback` warning), expected sessions, missing cells, request estimates, and a SHA-256 `plan_hash` over canonical JSON. Raises `SchemaOutOfDate` on missing tables — never migrates. `run_update(dry_run=True)` and `run_update_batch(dry_run=True)` delegate to `plan_update`, returning a plan-preview report shape with a deprecation warning.

**Tech Stack:** Python 3.13, sqlite3 URI mode=ro, hashlib sha256, dataclasses, pytest + pytest-asyncio.

---

## 1. Scope

**In scope:**
- Create `packages/data-core/catalyst_data/update_planner.py` with `plan_update`, `UpdatePlan`, `compute_plan_hash`, `SchemaOutOfDate`
- Universe resolution: explicit tickers → `"explicit-config"`; OHLCV-derived → `"ohlcv-derived-fallback"` with typed `universe-fallback` warning
- W1-A preserves the labeled OHLCV-derived compatibility fallback; it is not acceptable for certified execution
- Date defaults preserved at minimum compatibility: single OHLCV watermark for window; per-ticker defaults are W1-B ownership
- Redirect `run_update(dry_run=True)` and `run_update_batch(dry_run=True)` to delegate to `plan_update`
- Tests: zero-write proof (DB SHA, size, mtime, directory listing, WAL/SHM/journal sidecars, ingestion_runs count, report directory listing), hash determinism, hash sensitivity, SchemaOutOfDate, socket/network guard, read-only enforcement

**Out of scope:** Executor loop changes, schema migrations, calendar expectation changes (W1-B), OHLCV source handling (W1-C), service facade (W1-D), CLI (W1-F).

---

## 2. Non-Goals

- No executor refactor
- No schema changes
- No new connector logic
- No removal of `run_update_batch` or `run_update`
- No touching `catalyst_agents/*`
- No per-ticker default windows (W1-B ownership)

---

## 3. Verified Current Implementation

### 3.1 Existing files (all verified 2026-07-12)

| Path | Key symbols | Notes |
|---|---|---|
| `packages/data-core/catalyst_data/update_pipeline.py` | `compute_missing_cells` (L76), `_trading_days_in_window` (L63), `run_update` (L1277), `run_update_batch` (L870), `_CANCEL_REQUESTS` (L46) | Planning logic to extract; dry_run branches to redirect |
| `packages/data-core/catalyst_data/freshness.py` | `latest_local_ohlcv_date` (L31) | Watermark coupling; W1-A preserves for compatibility, W1-B replaces |
| `packages/data-core/catalyst_data/trading_calendar.py` | `calendar_trading_days` (L26), `trading_days_for_window` (L69), `US_MARKET_HOLIDAYS` (L13) | Calendar primitives |
| `packages/data-core/catalyst_data/run_report.py` | `RunConfig` (L17), `RunReport` (L34), `save_run_report` (L62) | Config consumed; adapters return report-shaped objects |
| `packages/data-core/catalyst_data/provider_limits.py` | Per-provider rate policies | Duration estimates |
| `packages/data-core/catalyst_data/storage/sqlite.py` | `FROZEN_PATHS` (L28) | Guard — planner checks this |
| `packages/data-core/catalyst_data/config.py` | `RATE_POLICIES` | Provider rate config |

### 3.2 Defects W1-A fixes

1. `run_update(dry_run=True)` writes DDL (init_db), a run row, and a report file.
2. `run_update_batch(dry_run=True)` calls `init_db` on connect.
3. Planning logic is embedded inside write-path entrypoints.

---

## 4. Proposed Files

| File | Status | Purpose |
|---|---|---|
| `packages/data-core/catalyst_data/update_planner.py` | **Proposed** | Pure planning: `plan_update`, `UpdatePlan`, `compute_plan_hash`, `SchemaOutOfDate`, `PlanWarning` |
| `packages/data-core/catalyst_data/update_pipeline.py` | Modified | Redirect dry_run branches to delegate to plan_update |
| `packages/data-core/tests/test_update_planner.py` | **Proposed** | Planner unit tests |

### Forbidden files

- `packages/data-core/catalyst_data/storage/sqlite.py` — no changes
- `packages/data-core/catalyst_data/quality.py` — no changes
- `packages/data-core/catalyst_data/migrations.py` — no new migrations
- `packages/data-core/catalyst_data/connectors/*` — no changes
- `packages/agents/*` — out of scope
- Existing test files in `packages/data-core/tests/` — preserved intact; contract-drift deferred to W3-B

---

## 5. Contracts

### 5.1 plan_update

```python
def plan_update(
    db_path: str,
    *,
    tickers: list[str] | None = None,
    sources: list[str] | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    reference_today: str | None = None,
) -> UpdatePlan:
```

- Opens DB with `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)`
- Defaults: `sources=["polygon_news"]`, universe from OHLCV if tickers is None (with `provenance="ohlcv-derived-fallback"` and typed `universe-fallback` warning), window from `latest_local_ohlcv_date` if dates are None
- Checks schema: required tables exist via `SELECT name FROM sqlite_master`
- Raises `SchemaOutOfDate` if tables/columns missing — never calls `init_db`
- Returns `UpdatePlan` with `plan_hash`
- Makes zero network calls (provable via socket guard test)

### 5.2 UpdatePlan dataclass

```python
@dataclass
class UpdatePlan:
    plan_schema_version: int  # 1
    created_at: str           # ISO-8601 UTC, EXCLUDED from hash
    db_path: str              # logical path only; not in hash
    db_sha256: str
    db_user_version: int
    config: dict[str, Any]
    universe: dict[str, Any]  # {"tickers": [...], "provenance": "explicit-config"|"ohlcv-derived-fallback"}
    reference_today: str
    latest_closed_session: str
    stages: dict[str, Any]    # {"market": {...}, "evidence": {...}} — evidence always provisional in W1-A
    estimates: dict[str, Any]
    warnings: list[dict[str, str]]  # [{"type": "universe-fallback", "message": "..."}]
    plan_hash: str            # SHA-256 over canonical JSON of everything above except created_at
```

### 5.3 PlanWarning typed warnings

```python
@dataclass
class PlanWarning:
    type: str      # "universe-fallback", "schema-behind", "watermark-behind-news"
    message: str
```

### 5.4 Dry-run delegation

```python
# In run_update (L1340 area):
if config.dry_run:
    import warnings
    warnings.warn("run_update(dry_run=True) is deprecated; use plan_update", DeprecationWarning, stacklevel=2)
    from catalyst_data.update_planner import plan_update as _plan_update
    plan = _plan_update(config.db_path, tickers=config.tickers, sources=config.sources,
                        from_date=config.from_date, to_date=config.to_date)
    return _plan_to_report_adapter(plan)  # mode="plan-preview", run_id=""
```

Same pattern for `run_update_batch`.

### 5.5 Plan hash canonicalization

- JSON with `sort_keys=True`, no whitespace variance
- Lists sorted deterministically (cells by source, ticker, date)
- Floats at fixed precision (`round(x, 6)`)
- DB identified by content SHA (not path)
- `created_at` excluded from hash payload
- `import hashlib` — `hashlib.sha256(canonical_json.encode()).hexdigest()`

---

## 6. Data Structures

### 6.1 SchemaOutOfDate

```python
class SchemaOutOfDate(Exception):
    def __init__(self, missing: list[str], fix_command: str | None = None):
        self.missing = missing
        self.fix_command = fix_command or (
            ".venv/bin/python packages/data-core/scripts/migrate_ingestion_quality.py"
        )
        super().__init__(f"Schema out of date. Missing: {missing}. Fix: {self.fix_command}")
```

---

## 7. Error Taxonomy

| Error | When | Response |
|---|---|---|
| `SchemaOutOfDate` | Required tables missing from DB | Raise; suggest migration command |
| `ValueError` | Invalid date format, empty sources | Raise with descriptive message |
| `FileNotFoundError` | db_path doesn't exist | Raise |
| `sqlite3.OperationalError` | Write attempt through read-only connection | Let propagate (proves enforcement) |
| `FrozenDBError` | db_path resolves to a FROZEN_PATHS entry | Raise with path |

---

## 8. Schema Impact

**None.** W1-A adds no migrations, no columns, no tables.

---

## 9. TDD Implementation Tasks

### Task 1: Create shared fixture helper and SchemaOutOfDate test

**Files:**
- Create: `packages/data-core/tests/test_update_planner.py`

**Step 1: Shared fixture helper** (avoid brittle DDL duplication)

```python
"""Tests for update_planner — zero-write planning."""
import hashlib
import json
import os
import socket
import sqlite3
import tempfile
from pathlib import Path

import pytest
from catalyst_data.update_planner import (
    SchemaOutOfDate, UpdatePlan, PlanWarning, plan_update, compute_plan_hash,
)

# -- minimal fixture builder --
MINIMAL_SCHEMA = {
    "ohlcv": """CREATE TABLE ohlcv (
        symbol TEXT, date TEXT, open REAL, high REAL, low REAL,
        close REAL, volume REAL, source TEXT)""",
    "source_checkpoints": """CREATE TABLE source_checkpoints (
        run_id TEXT, source_type TEXT, ticker TEXT, date TEXT,
        status TEXT, error_class TEXT, retries INTEGER DEFAULT 0,
        error_message_redacted TEXT, http_status INTEGER,
        retry_after_seconds REAL, provider_latency_ms REAL,
        raw_asset_id TEXT, items_count INTEGER,
        fallback_provider TEXT, fallback_triggered INTEGER DEFAULT 0)""",
}

def create_fixture_db(tmp_path, *, tables=None, extra_sql=None) -> str:
    """Create a minimal temp DB with required tables and optional data."""
    db_path = str(tmp_path / "fixture.db")
    conn = sqlite3.connect(db_path)
    for name in (tables or MINIMAL_SCHEMA.keys()):
        conn.execute(MINIMAL_SCHEMA[name])
    if extra_sql:
        for stmt in extra_sql:
            conn.execute(stmt)
    conn.commit()
    conn.close()
    return db_path
```

**Step 2: SchemaOutOfDate test**

```python
class TestSchemaOutOfDate:
    def test_empty_db_raises_schema_out_of_date(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE irrelevant (x INT)")
            conn.commit()
            conn.close()
            with pytest.raises(SchemaOutOfDate) as exc_info:
                plan_update(db_path)
            assert "ohlcv" in str(exc_info.value).lower()
            assert "migrate" in str(exc_info.value).lower()
        finally:
            os.unlink(db_path)
```

**Red command:**
```
.venv/bin/python -m pytest packages/data-core/tests/test_update_planner.py::TestSchemaOutOfDate -q --tb=short
```
Expected red: `ModuleNotFoundError` or `ImportError`

**Implementation:** Create `update_planner.py` with `SchemaOutOfDate` class and stub `plan_update` that raises it.

**Green command:**
```
.venv/bin/python -m pytest packages/data-core/tests/test_update_planner.py::TestSchemaOutOfDate -q --tb=short
```
Expected green: PASS

### Task 2: Read-only enforcement + socket guard

**Test:**
```python
class TestReadOnlyEnforcement:
    def test_planner_connection_refuses_writes(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT)")
            conn.execute("CREATE TABLE source_checkpoints (run_id TEXT, source_type TEXT, ticker TEXT, date TEXT, status TEXT)")
            conn.commit()
            conn.close()
            ro_uri = f"file:{db_path}?mode=ro"
            ro_conn = sqlite3.connect(ro_uri, uri=True)
            with pytest.raises(sqlite3.OperationalError):
                ro_conn.execute("CREATE TABLE should_fail (x INT)")
            ro_conn.close()
        finally:
            os.unlink(db_path)

    def test_planner_makes_no_network_calls(self, tmp_path):
        """Socket guard proves planner never accesses network."""
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
        ])
        original_socket = socket.socket
        def _blocking_socket(*args, **kwargs):
            raise RuntimeError("Network call blocked by test guard")
        socket.socket = _blocking_socket
        try:
            plan = plan_update(db_path, tickers=["AAPL"], sources=["polygon_news"])
            assert plan.plan_hash
        finally:
            socket.socket = original_socket
```

**Red:** second test fails with `RuntimeError` if planner makes network calls (it shouldn't, but guard proves it)

**Green:** both PASS

### Task 3: Implement real plan_update

**Test:**
```python
class TestPlanUpdateBasic:
    def test_plan_update_returns_update_plan(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
        ])
        plan = plan_update(db_path, tickers=["AAPL"], sources=["polygon_news"])
        assert isinstance(plan, UpdatePlan)
        assert len(plan.plan_hash) == 64
        assert plan.db_sha256
        assert plan.universe["tickers"] == ["AAPL"]
        assert plan.universe["provenance"] == "explicit-config"

    def test_default_universe_emits_fallback_warning(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
        ])
        plan = plan_update(db_path)
        assert plan.universe["provenance"] == "ohlcv-derived-fallback"
        assert any(w["type"] == "universe-fallback" for w in plan.warnings)
```

**Red:** fail (stub raises SchemaOutOfDate)

**Implementation:** Fill in `plan_update` to open read-only, check schema, compute universe, trading days, missing cells, estimates, hash.

**Green command:**
```
.venv/bin/python -m pytest packages/data-core/tests/test_update_planner.py::TestPlanUpdateBasic -q --tb=short
```
Expected: 2 PASS

### Task 4: Plan hash determinism + sensitivity

```python
class TestPlanHashDeterminism:
    def test_identical_inputs_produce_identical_hash(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
        ])
        p1 = plan_update(db_path, tickers=["AAPL"], sources=["polygon_news"])
        p2 = plan_update(db_path, tickers=["AAPL"], sources=["polygon_news"])
        assert p1.plan_hash == p2.plan_hash

    def test_hash_excludes_created_at(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
        ])
        p1 = plan_update(db_path, tickers=["AAPL"], sources=["polygon_news"])
        import time; time.sleep(0.2)
        p2 = plan_update(db_path, tickers=["AAPL"], sources=["polygon_news"])
        assert p1.plan_hash == p2.plan_hash

    def test_checkpoint_flip_changes_hash(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
        ])
        p1 = plan_update(db_path, tickers=["AAPL"], sources=["polygon_news"])
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO source_checkpoints (run_id,source_type,ticker,date,status) VALUES ('t','polygon_news','AAPL','2026-07-09','success')")
        conn.commit(); conn.close()
        p2 = plan_update(db_path, tickers=["AAPL"], sources=["polygon_news"])
        assert p1.plan_hash != p2.plan_hash
```

**Green:** all 3 PASS

### Task 5: Zero-write proof protocol

```python
class TestZeroWriteProof:
    def test_planner_does_not_modify_db(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
        ])
        db_file = Path(db_path)
        before_sha = hashlib.sha256(db_file.read_bytes()).hexdigest()
        before_size = db_file.stat().st_size
        before_mtime = db_file.stat().st_mtime
        before_files = set(os.listdir(str(tmp_path)))
        # Check WAL/SHM/journal sidecars
        wal_path = Path(str(db_path) + "-wal")
        shm_path = Path(str(db_path) + "-shm")
        wal_existed = wal_path.exists()
        shm_existed = shm_path.exists()

        plan = plan_update(db_path, tickers=["AAPL"], sources=["polygon_news"])

        after_sha = hashlib.sha256(db_file.read_bytes()).hexdigest()
        after_size = db_file.stat().st_size
        after_files = set(os.listdir(str(tmp_path)))

        assert before_sha == after_sha, f"DB SHA changed: {before_sha} → {after_sha}"
        assert before_size == after_size, f"DB size changed: {before_size} → {after_size}"
        new_files = after_files - before_files
        assert not new_files, f"New files: {new_files}"
        # WAL/SHM unchanged
        assert wal_path.exists() == wal_existed, "WAL sidecar changed"
        assert shm_path.exists() == shm_existed, "SHM sidecar changed"
        assert plan.plan_hash

    def test_planner_source_has_no_write_imports(self):
        import catalyst_data.update_planner as up
        source = Path(up.__file__).read_text()
        # Check for write-capable imports (not generic UPDATE strings)
        forbidden = [
            "open_ingestion_run", "write_source_checkpoint",
            "ensure_ingestion_quality_tables", "init_db",
            "save_run_report", "close_ingestion_run",
        ]
        for token in forbidden:
            assert token not in source, f"update_planner.py imports {token}"
```

**Green:** 2 PASS

### Task 6: Dry-run delegation

```python
class TestDryRunDelegation:
    def test_run_update_dry_run_returns_plan_preview(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
        ])
        from catalyst_data.run_report import RunConfig
        config = RunConfig(db_path=db_path, tickers=["AAPL"], sources=["polygon_news"], dry_run=True)
        before_sha = hashlib.sha256(Path(db_path).read_bytes()).hexdigest()
        with pytest.deprecated_call():
            result = run_update(config)  # noqa
        after_sha = hashlib.sha256(Path(db_path).read_bytes()).hexdigest()
        assert before_sha == after_sha
        assert result.mode in ("plan-preview", "dry-run")

    def test_run_update_batch_dry_run_returns_plan_preview(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
        ])
        import asyncio
        before_sha = hashlib.sha256(Path(db_path).read_bytes()).hexdigest()
        with pytest.deprecated_call():
            result = asyncio.run(run_update_batch(db_path=db_path, tickers=["AAPL"], sources=["polygon_news"], dry_run=True))
        after_sha = hashlib.sha256(Path(db_path).read_bytes()).hexdigest()
        assert before_sha == after_sha
        assert result["mode"] in ("plan-preview", "dry-run")
```

**Required imports in test:** `from catalyst_data.update_pipeline import run_update, run_update_batch`

**Green:** 2 PASS

### Task 7: Focused regression + canonical suite

```
.venv/bin/python -m pytest packages/data-core/tests/test_update_planner.py -q --tb=short
.venv/bin/python -m pytest packages/data-core -q
```

No new failures from W3-A baseline (703/0/1/1).

---

## 10. Canonical Package Test

```
.venv/bin/python -m pytest packages/data-core -q
```

Must show zero new failures from W3-A baseline.

---

## 11. Database Safety Protocol

1. **Before all tests:** Record Dev DB SHA (`shasum -a 256 data/catalyst_dev_ws4b.db`)
2. **After all tests:** Re-record and compare — must be identical
3. **Frozen DB:** SHA must not change; never opened writable
4. **Planner connection:** Must use `mode=ro` URI; any write attempt raises
5. **WAL/SHM:** Check before/after — no new sidecars from read-only access

---

## 12. Git Boundary

**Commit:** `feat(data-core): true zero-write update planning`

**Stage manifest:**
- `packages/data-core/catalyst_data/update_planner.py` — new
- `packages/data-core/catalyst_data/update_pipeline.py` — dry_run redirection
- `packages/data-core/tests/test_update_planner.py` — new

**Does NOT contain:** Migrations, schema changes, `catalyst_agents/*`.

---

## 13. Landmines

1. **WAL/SHM siblings:** Read-only planning on a DB with pending WAL may appear to "change" when WAL is checkpointed. Zero-write proof test must use a fresh fixture DB.
2. **mode=ro URI:** `sqlite3.connect(f"file:{path}?mode=ro", uri=True)` is required. Omitting `uri=True` treats it as a filename.
3. **DeprecationWarning:** pytest may filter by default. Tests use `pytest.deprecated_call()` explicitly.
4. **Existing test_update_pipeline tests:** May use `run_update(dry_run=True)` and assert run rows/report files. These are a named contract-drift group for W3-B — do NOT fix in W1-A.
5. **reference_today injection:** Expose `reference_today` parameter or hash determinism tests flake at midnight.
6. **Frozen DB:** Planner must refuse frozen DB path. Check against `FROZEN_PATHS` before opening.
7. **No network:** Socket guard test must block all network access.

---

## 14. Exit Gate

1. Zero-write proof: DB SHA, size, mtime, WAL/SHM, directory listing unchanged
2. `plan_hash` deterministic: 3 identical invocations → identical hash
3. `plan_hash` sensitive: checkpoint flip → different hash
4. `SchemaOutOfDate` on stale fixture (no silent migration)
5. Dry-run delegation returns plan-preview shape (no DB writes)
6. Socket guard test proves no network calls
7. Planner source contains zero write-capable function imports
8. No new canonical failures (703/0/1/1 baseline)

---

## 15. Worker Handoff Report Format

1. Pre-fix: DB SHAs, canonical counts
2. Planner module: line count, functions exported, plan_hash implementation
3. Focused test results: `test_update_planner.py` (all green)
4. Canonical suite: exact pass/fail/skip/xfail counts
5. Zero-write proof transcript: DB SHA, size, mtime, file listing, WAL/SHM before/after
6. Socket guard test result
7. `git diff --stat`
8. DB SHA comparison (before == after)
9. Any DeprecationWarning-producing existing tests (W3-B pre-registration)

---

## 16. Orchestrator Independent Verification

1. Run zero-write proof personally against Dev DB copy:
   - Record SHA, size, mtime, directory listing, WAL/SHM
   - Run `plan_update` twice
   - Compare all properties
2. Inspect planner source for write-capable imports:
   - `rg "open_ingestion_run\|write_source_checkpoint\|ensure_ingestion_quality_tables\|init_db\|save_run_report\|close_ingestion_run" catalyst_data/update_planner.py` must be empty
3. Run `plan_update` twice with identical inputs → identical hash
4. Verify dry_run delegation returns plan-preview shape with zero writes
5. Canonical suite: `.venv/bin/python -m pytest packages/data-core -q`
6. `git diff --stat` — only expected files
7. Frozen DB SHA unchanged

---

## 17. Rollback Strategy

Revert commit. `update_pipeline.py` dry_run branches restored from prior commit.
No schema to roll back. No data affected.

---

## 18. Architect Decision Gates

| ID | Decision | Required By | Status |
|---|---|---|---|
| AD-GIT-1 | Commit W3-A before W1-A? | W1-A pre-slice | Unresolved |
| AD-UNIVERSE-1 | Final ten-ticker universe | W1-B | Unresolved |
| AD-DATE-1 | Historical start date | W1-B | Unresolved |

**W1-A does not depend on these decisions.** It preserves the OHLCV-derived compatibility fallback with typed warnings until W1-B resolves them.

---

## 19. Dependencies

**Prerequisites:** W3-A verified (703/0/1/1 baseline)
**Depended on by:** W1-B (calendar/OHLCV planning)

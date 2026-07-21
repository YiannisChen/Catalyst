# B2 — Data Update and Provenance Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Finish the two-stage OHLCV-first update service with append-only request ledger, raw-response provenance, normalized provenance, pagination, partial-result semantics, plan drift detection, durable run control, cancellation, and checkpoint-based reruns.

**Architecture:** Extends the existing zero-write planner, but implements `PlanDriftError` and the two-stage OHLCV-first executor as new B2 behavior; neither exists on the current branch. Adds request-attempt ledger, request-scoped raw payloads, normalized provenance, scripted pagination, redaction, and durable run control. Uses migration v8, tested only on disposable databases until a separately authorized operator migration.

**Tech Stack:** Python 3.12+, SQLite, httpx, pytest

**Binding Contract:** `docs/plans/2026-07-21-b2-b7-technical-contracts.md` §4

---

## 0. Execution Rules

- Run in the main `.venv`; do not use `--rootdir` overrides.
- Do not stage, commit, push, call live providers, inspect secrets, or mutate either canonical DB during plan execution.
- All migration tests use shared `_fresh_db_at_version()` / `apply_migration_v8()` helpers implemented first in `packages/data-core/tests/db_fixtures.py`; no undefined pseudocode helpers are permitted.
- All connector tests inject a scripted fake HTTP transport with sanitized page fixtures. A test that can reach the network is invalid.
- Applying v8 to `data/catalyst_dev_ws4b.db` is not part of this plan. After code review, an operator may separately authorize backup, migration, integrity checks, and a new SHA.
- Existing tests may be updated only for an intentional contract change; assertions may not be deleted, weakened, skipped, or xfailed.
- Every exception assertion names the exact exception type. Every integration assertion uses an isolated DB and exact IDs/counts, never `>=` against pre-existing rows.


## 1. Objective

Deliver the B2 data update and provenance package that passes Core Exit Gates A (Data lineage) and H (Local security minimum). Specifically:

- Append-only request-attempt ledger with per-attempt identities;
- Request-scoped append-only raw payloads (no `INSERT OR REPLACE`);
- Normalized provenance linking every canonical entity to its raw source;
- Polygon pagination with page lineage, loop detection, budget enforcement;
- Plan drift detection (`PlanDriftError`);
- Two-stage OHLCV-first then evidence-second execution;
- Durable run status, cooperative cancellation, checkpoint resume;
- Connector-side secret redaction;
- Provider canary as separate human-authorized step.

## 2. Current Verified State

### Implemented

| Component | File | Status |
|---|---|---|
| Zero-write update planner | `catalyst_data/update_planner.py` | partial — `UpdatePlan`, `plan_hash`, and `cell_id` exist; `PlanDriftError` does not |
| Two-stage pipeline skeleton | `catalyst_data/update_pipeline.py` | missing for real execution — dry-run exposes planner stages, but non-dry-run remains a flat cell loop |
| Ingestion runs | `catalyst_data/storage/sqlite.py` | implemented — `ingestion_runs` table |
| Source checkpoints | `catalyst_data/storage/sqlite.py` | implemented — `source_checkpoints` table |
| Raw assets (old semantics) | `catalyst_data/storage/sqlite.py` | implemented — `raw_assets` with `INSERT OR REPLACE` |
| Connector base | `catalyst_data/connectors/base.py` | implemented — protocol |
| Polygon connector | `catalyst_data/connectors/polygon.py` | partial — requests news, no pagination loop |
| Finnhub/FMP/FRED/SEC connectors | `catalyst_data/connectors/` | implemented — one-shot requests |
| OHLCV fallback | `catalyst_data/connectors/yfinance_fallback.py`, `catalyst_data/fallback.py` | implemented |
| Rate limiter/retry | `catalyst_data/rate_limiter.py`, `catalyst_data/retry.py` | implemented |
| Migration registry v1–v7 | `catalyst_data/migrations.py` | implemented |
| Plan hash computation | `catalyst_data/update_planner.py` | partial — `compute_plan_hash()` exists but may not exclude all runtime fields per contract §4.1 |

### Partial

| Component | Gap |
|---|---|
| `source_checkpoints.get_raw_asset_id()` | legacy; needs `logical_fetch_id`, `request_count`, `pages_received`, `items_received`, `is_complete` |
| `raw_assets` upsert | uses `INSERT OR REPLACE`, not append-only; needs `response_sha256`, `request_id`, `page_no`, `content_encoding` + append-only trigger |
| Plan drift detection | missing — add `PlanDriftError` and compare a fresh execution plan before network or writes |
| Durable run control | `source_checkpoints` has terminal statuses but no cooperative cancellation flag or single-writer lease |
| Secret redaction | not systematic; connector adapters may log raw URLs |

### Missing

| Component | File to create |
|---|---|
| `provider_request_attempts` table | migration v8 in `catalyst_data/migrations.py` |
| Request-attempt model | `catalyst_data/ingestion/request_ledger.py` (new, target layout) |
| Append-only raw write semantics | `catalyst_data/ingestion/raw_store.py` (new) |
| `normalized_provenance` table | migration v8 in `catalyst_data/migrations.py` |
| Normalized provenance writer | `catalyst_data/ingestion/provenance.py` (new) |
| Connector redaction module | `catalyst_data/ingestion/redaction.py` (new) |
| Polygon pagination loop | `catalyst_data/connectors/polygon.py` (modify) |
| Durable cancel/resume | `catalyst_data/ingestion/run_control.py` (new) |
| Two-stage OHLCV-then-evidence with re-plan | `catalyst_data/update_pipeline.py` (modify) |

### Obsolete

| Component | Reason |
|---|---|
| `compute_asset_id()` for raw identity | replaced by `request_id`-based identity |
| `upsert_raw_asset()` | replaced by append-only insert |
| Legacy request summary fields in checkpoints | replaced by `provider_request_attempts` |

### Proposed (design-only, not existing)

- Provider canary step (separate operator authorization)
- `allow_stale_ohlcv` override

## 3. Scope / Non-goals

### Scope

- Migration v8: add `provider_request_attempts`, extend `source_checkpoints`, extend `raw_assets`, add `normalized_provenance`, add triggers, add `ingestion_runs` fields
- Request ledger model, fingerprint, redaction
- Append-only raw payload writes
- Normalized provenance on every entity upsert
- Polygon pagination (page loop, cursor dedup, budget enforcement, partial-OK)
- Plan drift detection on execution start
- Two-stage OHLCV-first with evidence re-plan after OHLCV commit
- Durable run status (PLANNED → RUNNING_OHLCV → RUNNING_EVIDENCE → SUCCEEDED|PARTIAL|FAILED|CANCELLED)
- Cooperative cancellation at cell, page, fallback, and commit boundaries
- Checkpoint-based rerun (resume from last committed stage)
- Connector-side secret redaction
- Single-writer lease
- Provider canary (separate authorized step)
- All hash formulas from technical contract §4.1–§4.2

### Non-goals

- Backfill/reprocessing of historical raw assets (compatibility read only)
- FMP Fundamentals payload normalization (B3 concern)
- FRED/SEC content changes beyond provenance wiring
- Finnhub multi-ticker (target-only, per core gate)
- Sub-cell progress bars or ETA (Showcase, not Core)
- Provider discovery or new connectors
- Retrieval index synchronization (B4/B6)
- Any frontend or API surface

## 4. Dependencies

### Inputs

- B1 completion (verified: data-core 758 passed, DB SHA match)
- Existing `update_planner.py` (inherit, not rebuild)
- Existing `update_pipeline.py` (enhance, not rewrite)
- Existing connector implementations (instrument, not replace)
- Dev DB at `data/catalyst_dev_ws4b.db` SHA `92731fb7c5…`

### Output Artifacts

- Migration v8 proven on a disposable copy of the Dev DB; canonical Dev DB remains untouched during implementation
- Run report with provenance chain
- Updated test suite (new tests + existing passing)

### Consumed By

- B3 (corpus and chunking): consumes canonical articles with normalized provenance; needs `articles.raw_asset_id` compatibility + `normalized_provenance` records
- B4 (retrieval): consumes manifest identity and certified snapshot; needs append-only raw store
- B5 (attribution): needs request lineage for RunAssuranceRecord

## 5. Schema and Artifact Ownership

### Owned SQLite Migration

**v8** (owned by B2):

- `provider_request_attempts` — full schema, indexes, and transition triggers per contract §4.6
- `normalized_provenance` — `(entity_type, entity_id, entity_version, raw_asset_id)` PK
- Extend `source_checkpoints`: `logical_fetch_id`, `request_count`, `pages_received`, `items_received`, `is_complete`
- Extend `raw_assets`: `response_sha256`, `request_id`, `page_no`, `content_encoding`
- Extend `ingestion_runs`: `plan_hash`, `expected_plan_hash`, `allow_stale_ohlcv`, `allow_stale_ohlcv_overridden`, `cancel_requested`, `lease_holder`, `lease_expires_at`, `parent_run_id`
- Trigger: reject UPDATE/DELETE on v2 request-scoped raw rows

### Owned Run Artifacts

- Run report (JSON) with provenance summary
- Update plan with plan_hash

### Not Owned

- `articles`, `article_tickers`, `ohlcv`, `filings`, `macro_observations` schemas (B3)
- Chunk profiles (B3)
- Retrieval manifests (B4/B6)
- Trace schemas (B5)
- BenchmarkCase schemas (B4 eval foundation)

## 6. File Allowlist

### Existing Files Allowed to Modify

```
packages/data-core/catalyst_data/migrations.py          — add v8
packages/data-core/catalyst_data/storage/sqlite.py      — extend tables, triggers
packages/data-core/catalyst_data/update_planner.py       — plan_hash completeness, drift check
packages/data-core/catalyst_data/update_pipeline.py      — two-stage, cancel, lease, re-plan
packages/data-core/catalyst_data/connectors/polygon.py   — pagination loop
packages/data-core/catalyst_data/connectors/base.py      — redacted request helpers
packages/data-core/catalyst_data/connectors/finnhub.py   — redaction
packages/data-core/catalyst_data/connectors/fmp.py       — redaction
packages/data-core/catalyst_data/connectors/fred.py      — redaction
packages/data-core/catalyst_data/connectors/sec.py       — redaction
packages/data-core/catalyst_data/connectors/yfinance_fallback.py — redaction
packages/data-core/catalyst_data/connectors/__init__.py
packages/data-core/catalyst_data/error_taxonomy.py       — add integrity error class
packages/data-core/tests/test_migrations.py              — add v8 tests
packages/data-core/tests/test_update_planner.py          — add drift tests
packages/data-core/tests/test_update_pipeline.py         — add provenance tests
```

### New Files Allowed to Create

```
packages/data-core/catalyst_data/ingestion/__init__.py
packages/data-core/catalyst_data/ingestion/request_ledger.py
packages/data-core/catalyst_data/ingestion/raw_store.py
packages/data-core/catalyst_data/ingestion/provenance.py
packages/data-core/catalyst_data/ingestion/redaction.py
packages/data-core/catalyst_data/ingestion/run_control.py
packages/data-core/tests/test_request_ledger.py
packages/data-core/tests/test_raw_store.py
packages/data-core/tests/test_provenance.py
packages/data-core/tests/test_redaction.py
packages/data-core/tests/test_run_control.py
packages/data-core/tests/test_polygon_pagination.py
packages/data-core/tests/db_fixtures.py
packages/data-core/tests/http_fixtures.py
packages/data-core/tests/fixtures/polygon_news_page_1.json
packages/data-core/tests/fixtures/polygon_news_page_2.json
packages/data-core/catalyst_data/provider_canary.py
```

### Files Explicitly Forbidden

- `data/catalyst_eval_frozen_v2.db` — frozen, realpath guard, no migration, no repin
- `data/catalyst_dev_ws4b.db` — do not open writable or migrate during implementation; use a disposable copy
- `packages/agents/` — B5 domain
- `packages/eval/` — B4/B7 domain
- `packages/app/` — B7 domain
- `docs/plans/2026-07-21-b2-b7-technical-contracts.md` — binding, not to be modified
- Any existing test that currently passes — do not weaken

## 7. TDD Tasks

### Task 0: Executable isolated fixtures

**Files:**
- Create: `packages/data-core/tests/db_fixtures.py`
- Create: `packages/data-core/tests/http_fixtures.py`
- Create: the two sanitized Polygon page fixtures listed above

Implement `_fresh_db_at_version(version)`, version-specific migration helpers, `NoNetworkTransport`, and `ScriptedTransport`. The DB fixture module must also define every schema/state helper referenced below: `assert_table_contract`, `EXPECTED_REQUEST_ATTEMPT_COLUMNS`, `EXPECTED_NORMALIZED_PROVENANCE_COLUMNS`, `index_columns`, `unique_index_columns`, `contract_trigger_names`, `V8_TRIGGER_ABORT_CASES`, `execute_invalid_v8_case`, `seed_v2_raw_row`, `seed_v2_ingestion_run`, `insert_attempt_row`, `transition_attempt_row`, `transition_v2_run`, `CHECKPOINT_STATUS_TO_STORAGE`, `seed_and_dump_legacy_ingestion_rows`, `dump_legacy_ingestion_rows`, and `base_attempt`. `base_attempt` is a module-level dict fixture holding one fully contract-valid `STARTED` attempt (every NOT NULL field present; `attempt_no=1`; `page_no=1`; a 64-lowercase-hex `request_fingerprint`; `request_params_redacted='{}'` or other valid JSON; `cursor_fingerprint=None`); tests spread it with `{**base_attempt, ...}` and override only the fields under test, and must never override a field into a value that violates contract §4.6. These helpers inspect SQLite metadata or execute literal fixture operations; they must not reuse production migration validators or transition functions as their oracle. `_fresh_db_at_version` opens every fixture connection with `PRAGMA foreign_keys=ON`, matching production, so the §4.6 foreign keys (`ON DELETE RESTRICT`) are enforced in tests. Consequently every `seed_v2_*`/`insert_attempt_row` helper that writes a row referencing `raw_assets(asset_id)`, `ingestion_runs(run_id)`, or a parent request first seeds the referenced parent; and every test that invokes a production writer for a `run_id` (`fetch_paginated_news`, `execute_update`, connector instrumentation) first seeds that `ingestion_runs` row via `seed_v2_ingestion_run` (production `execute_update` creates the run itself and therefore does not require pre-seeding). `V8_TRIGGER_ABORT_CASES` contains one literal invalid INSERT/UPDATE/DELETE setup for every trigger in contract §4.6 and its exact expected abort code.

Add deterministic fixture factories named `two_stage_transport`, `partial_ohlcv_transport`, `stale_ohlcv_transport`, `polygon_two_article_transport`, and `valid_empty_transport`. Add `expected_ohlcv_cell_count` and `expected_news_cell_count` as fixture-oracle helpers backed by explicit expected cell inventories; they must not call the production planner to calculate their answers. Prove the DB helper creates only temporary files and every transport raises on an unregistered request. All later B2 snippets depend on these helpers; do not start Task 1 until their focused tests pass.

### Task 1: Write migration v8 schema

**Step 1: Write the failing schema test**

```python
# packages/data-core/tests/test_migrations.py

def test_v8_adds_provider_request_attempts_table():
    """v8 creates provider_request_attempts with all contract columns."""
    db = _fresh_db_at_version(7)
    apply_migration_v8(db)
    cols = _table_columns(db, "provider_request_attempts")
    required = [
        "request_id", "run_id", "logical_fetch_id", "source_type",
        "provider", "endpoint_name", "ticker_or_series", "window_start",
        "window_end", "attempt_no", "page_no", "parent_request_id",
        "request_fingerprint", "request_params_redacted", "cursor_fingerprint",
        "started_at", "completed_at", "status", "http_status", "latency_ms",
        "items_count", "retry_after_seconds", "rate_limit_remaining",
        "provider_request_id", "error_class", "error_message_redacted",
        "raw_asset_id", "response_sha256", "response_bytes"
    ]
    for col in required:
        assert col in cols, f"Missing column {col}"


def test_v8_adds_normalized_provenance_table():
    """v8 creates normalized_provenance with the exact composite-key contract."""
    db = _fresh_db_at_version(7)
    apply_migration_v8(db)
    assert_table_contract(db, "normalized_provenance",
                          EXPECTED_NORMALIZED_PROVENANCE_COLUMNS)
    assert index_columns(db, "idx_normalized_provenance_raw") == [
        "raw_asset_id", "entity_type", "entity_id"
    ]
    assert index_columns(db, "idx_normalized_provenance_entity") == [
        "entity_type", "entity_id", "entity_version"
    ]


def test_v8_trigger_inventory_is_exact():
    """All contract guards exist under stable names."""
    db = _fresh_db_at_version(8)
    assert contract_trigger_names(db) == {
        "trg_raw_assets_v2_insert_guard",
        "trg_raw_assets_v2_update_guard",
        "trg_raw_assets_v2_delete_guard",
        "trg_request_attempt_insert_guard",
        "trg_request_attempt_identity_guard",
        "trg_request_attempt_transition_guard",
        "trg_request_attempt_delete_guard",
        "trg_checkpoint_v2_insert_guard",
        "trg_checkpoint_v2_update_guard",
        "trg_ingestion_run_v2_insert_guard",
        "trg_ingestion_run_v2_transition_guard",
    }


@pytest.mark.parametrize("case_name,expected_code", V8_TRIGGER_ABORT_CASES)
def test_v8_trigger_abort_codes_are_exact(case_name, expected_code):
    """Every guard has an independently seeded invalid operation and stable code."""
    db = _fresh_db_at_version(8)
    with pytest.raises(sqlite3.IntegrityError, match=f"^{expected_code}$"):
        execute_invalid_v8_case(db, case_name)


def test_v8_extends_raw_assets():
    """v8 adds response_sha256, request_id, page_no, content_encoding to raw_assets."""
    db = _fresh_db_at_version(7)
    apply_migration_v8(db)
    cols = _table_columns(db, "raw_assets")
    for col in ["response_sha256", "request_id", "page_no", "content_encoding"]:
        assert col in cols


def test_v8_raw_assets_rejects_update():
    """v8 trigger rejects UPDATE on raw_assets for v2 rows."""
    db = _fresh_db_at_version(8)
    seed_v2_raw_row(db, asset_id="raw:request-1", request_id="request-1")
    with pytest.raises(sqlite3.IntegrityError, match="raw_asset_v2_immutable"):
        db.execute("UPDATE raw_assets SET response_sha256 = 'x' WHERE request_id = 'request-1'")


def test_v8_raw_assets_rejects_delete():
    """v8 trigger rejects DELETE on raw_assets for v2 rows."""
    db = _fresh_db_at_version(8)
    seed_v2_raw_row(db, asset_id="raw:request-1", request_id="request-1")
    with pytest.raises(sqlite3.IntegrityError, match="raw_asset_v2_immutable"):
        db.execute("DELETE FROM raw_assets WHERE request_id = 'request-1'")


def test_v8_extends_source_checkpoints():
    """v8 adds logical_fetch_id, request_count, pages_received, items_received, is_complete."""
    db = _fresh_db_at_version(7)
    apply_migration_v8(db)
    cols = _table_columns(db, "source_checkpoints")
    for col in ["logical_fetch_id", "request_count", "pages_received",
                "items_received", "is_complete"]:
        assert col in cols


def test_v8_extends_ingestion_runs():
    """v8 adds plan_hash, expected_plan_hash, cancel_requested, lease fields."""
    db = _fresh_db_at_version(7)
    apply_migration_v8(db)
    cols = _table_columns(db, "ingestion_runs")
    for col in ["plan_hash", "expected_plan_hash", "allow_stale_ohlcv",
                "allow_stale_ohlcv_overridden", "cancel_requested",
                "lease_holder", "lease_expires_at", "parent_run_id"]:
        assert col in cols


def test_v8_request_attempt_constraints_and_indexes_match_contract():
    """Column types/nullability/PK, unique key, FKs and indexes match contract §4.6."""
    db = _fresh_db_at_version(7)
    apply_migration_v8(db)
    assert_table_contract(db, "provider_request_attempts", EXPECTED_REQUEST_ATTEMPT_COLUMNS)
    assert index_columns(db, "idx_request_attempts_run") == ["run_id", "started_at", "request_id"]
    assert index_columns(db, "idx_request_attempts_fetch") == ["logical_fetch_id", "page_no", "attempt_no"]
    assert index_columns(db, "idx_request_attempts_status") == ["status", "provider", "started_at"]
    assert unique_index_columns(db, "provider_request_attempts") == {
        ("logical_fetch_id", "attempt_no", "page_no")
    }


def test_v8_request_attempt_state_machine_is_enforced():
    """Only STARTED insertion and one STARTED→terminal transition are legal."""
    db = _fresh_db_at_version(8)
    seed_v2_ingestion_run(db, run_id="run-1")
    with pytest.raises(sqlite3.IntegrityError, match="request_attempt_initial_status"):
        insert_attempt_row(db, request_id="req-bad", run_id="run-1", status="SUCCEEDED")

    insert_attempt_row(db, request_id="req-1", run_id="run-1", status="STARTED")
    transition_attempt_row(db, "req-1", status="SUCCEEDED")
    with pytest.raises(sqlite3.IntegrityError, match="request_attempt_illegal_transition"):
        transition_attempt_row(db, "req-1", status="TIMEOUT")
    with pytest.raises(sqlite3.IntegrityError, match="request_attempt_identity_immutable"):
        db.execute("UPDATE provider_request_attempts SET page_no=2 WHERE request_id='req-1'")


def test_v8_ingestion_run_state_machine_and_checkpoint_mapping():
    """v2 run transitions and lowercase checkpoint storage mapping are exact."""
    db = _fresh_db_at_version(8)
    seed_v2_ingestion_run(db, run_id="run-1", status="PLANNED")
    transition_v2_run(db, "run-1", "RUNNING_OHLCV")
    transition_v2_run(db, "run-1", "RUNNING_EVIDENCE")
    transition_v2_run(db, "run-1", "SUCCEEDED")
    with pytest.raises(sqlite3.IntegrityError, match="ingestion_run_illegal_transition"):
        transition_v2_run(db, "run-1", "FAILED")

    assert CHECKPOINT_STATUS_TO_STORAGE == {
        "PLANNED": "pending", "RUNNING": "running", "SUCCEEDED": "success",
        "SUCCESS_EMPTY": "success_empty", "PARTIAL": "partial",
        "FAILED": "failed", "CANCELLED": "cancelled", "SKIPPED": "skipped",
    }


def test_v8_preserves_legacy_rows_and_only_guards_v2_rows():
    """Migration is additive: legacy raw/run/checkpoint rows remain byte-for-byte readable."""
    db = _fresh_db_at_version(7)
    before = seed_and_dump_legacy_ingestion_rows(db)
    apply_migration_v8(db)
    assert dump_legacy_ingestion_rows(db) == before
    assert db.execute("SELECT request_id FROM raw_assets WHERE asset_id='legacy-raw'").fetchone()[0] is None
```

**Step 2: Run test to verify it fails**

```bash
cd /Users/yiannischen/Desktop/Catalyst
.venv/bin/python -m pytest packages/data-core/tests/test_migrations.py::test_v8_adds_provider_request_attempts_table -q
```

Expected: FAIL — v8 migration not defined.

**Step 3: Write v8 migration in `catalyst_data/migrations.py`**

Add `MIGRATION_8_SQL` implementing contract §4.6 exactly: both new tables, all declared indexes, additive legacy columns, v2 raw-row guards, request-attempt identity/status triggers, v2 ingestion-run transitions, checkpoint counter guards, and the uppercase-domain/lowercase-storage status mapping. Register it in the migration registry. Task 0 must define the schema-introspection and deterministic seed/dump helpers referenced above before this test is written.

**Step 4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_migrations.py -k "v8" -q
```

Expected: all v8 tests PASS.

**Step 5: Review checkpoint**

Record the exact diff and test output. Do not stage or commit; Git boundaries are prepared only after independent review and architect authorization.

### Task 2: Plan hash completeness and PlanDriftError

**Step 1: Write failing plan_hash completeness test**

```python
# packages/data-core/tests/test_update_planner.py

def test_plan_hash_excludes_runtime_fields():
    """plan_hash is deterministic and excludes created_at, run_id, progress, PID."""
    plan1 = make_plan(ticker="AAPL", start="2026-01-01", end="2026-01-05")
    h1 = plan1.plan_hash
    plan1.created_at = "2099-01-01T00:00:00Z"
    plan1.run_id = "different-run"
    h2 = compute_plan_hash(plan1)
    assert h1 == h2, "plan_hash must exclude runtime fields"


def test_plan_hash_includes_config_fields():
    """plan_hash changes when universe, window, stage, or config change."""
    plan1 = make_plan(ticker="AAPL", start="2026-01-01", end="2026-01-05")
    plan2 = make_plan(ticker="MSFT", start="2026-01-01", end="2026-01-05")
    assert plan1.plan_hash != plan2.plan_hash


@pytest.mark.asyncio
async def test_plan_drift_error_on_mismatch():
    """Starting execution with a different expected_plan_hash raises PlanDriftError."""
    db = _fresh_db_at_version(8)
    plan = make_plan(ticker="AAPL", start="2026-01-01", end="2026-01-05")
    plan.expected_plan_hash = "different-hash"
    with pytest.raises(PlanDriftError):
        await execute_update(db=db, plan=plan, transport=NoNetworkTransport())
    assert request_attempt_count(db) == 0
    assert ingestion_run_count(db) == 0


def test_cell_id_matches_contract():
    """cell_id follows SHA256(source_type, endpoint_name, ticker, window, stage, profile)."""
    cell = make_cell(source_type="news", endpoint_name="polygon_news",
                     ticker="AAPL", window_start="2026-01-01", window_end="2026-01-02",
                     stage="evidence", provider_profile_version="v1")
    cid = compute_cell_id(cell)
    assert len(cid) == 64
    # Same inputs → same cell_id
    cell2 = make_cell(source_type="news", endpoint_name="polygon_news",
                      ticker="AAPL", window_start="2026-01-01", window_end="2026-01-02",
                      stage="evidence", provider_profile_version="v1")
    assert compute_cell_id(cell2) == cid
    # Different inputs → different cell_id
    cell3 = make_cell(source_type="news", endpoint_name="polygon_news",
                      ticker="MSFT", window_start="2026-01-01", window_end="2026-01-02",
                      stage="evidence", provider_profile_version="v1")
    assert compute_cell_id(cell3) != cid
```

**Step 2: Run test to verify specific failures**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_update_planner.py::test_plan_hash_excludes_runtime_fields -q
```

Expected: FAIL if plan_hash currently includes runtime fields.

**Step 3: Fix plan_hash computation**

Audit `compute_plan_hash()` in `update_planner.py`. Ensure it strips `created_at`, `run_id`, execution progress, report path, PID, heartbeat, lease owner, measured latency. Ensure it includes ticker/series universe, source/endpoint, start/end, stage order, calendar version, provider profile version, request/page caps, fallback chain, peer tier, configuration version. Use canonical JSON per contract §3 rule 7.

**Step 4: Run tests to verify pass**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_update_planner.py -q
```

Expected: all planner tests PASS.

### Task 3: Request fingerprint and redaction

**Step 1: Write failing redaction test**

```python
# packages/data-core/tests/test_redaction.py

def test_request_fingerprint_excludes_api_key():
    """request_fingerprint contains no apiKey even when URL has it."""
    from catalyst_data.ingestion.redaction import redact_request, compute_request_fingerprint

    req = {
        "method": "GET",
        "url": "https://api.polygon.io/v2/reference/news?ticker=AAPL&apiKey=SECRET123&limit=50",
        "headers": {"Authorization": "Bearer SECRET123"},
        "params": {"ticker": "AAPL", "apiKey": "SECRET123"},
        "body": None,
        "provider_profile_version": "v1"
    }
    redacted = redact_request(req)
    fingerprint = compute_request_fingerprint(redacted)
    assert "SECRET123" not in fingerprint
    assert "apiKey" not in str(redacted.get("params", {}))
    assert "Authorization" not in str(redacted.get("headers", {}))


def test_cursor_not_stored_in_clear():
    """Pagination cursor fingerprint is hashed, not stored raw (unless proven safe)."""
    from catalyst_data.ingestion.redaction import compute_cursor_fingerprint
    cursor_url = "https://api.polygon.io/v2/reference/news?cursor=cursor-fixture-1"
    fp = compute_cursor_fingerprint(cursor_url)
    assert fp != cursor_url  # hashed
    assert len(fp) == 64  # SHA256


def test_request_params_are_sorted_and_redacted():
    """Secret query params are removed before fingerprint; non-secret sorted."""
    from catalyst_data.ingestion.redaction import redact_request, compute_request_fingerprint

    req1 = {
        "method": "GET",
        "url": "https://api.example.com/data?z=1&a=2&apiKey=SECRET",
        "headers": {},
        "params": {"z": "1", "a": "2", "apiKey": "SECRET"},
        "body": None,
        "provider_profile_version": "v1"
    }
    req2 = {
        "method": "GET",
        "url": "https://api.example.com/data?a=2&z=1&apiKey=DIFFERENT",
        "headers": {},
        "params": {"a": "2", "z": "1", "apiKey": "DIFFERENT"},
        "body": None,
        "provider_profile_version": "v1"
    }
    fp1 = compute_request_fingerprint(redact_request(req1))
    fp2 = compute_request_fingerprint(redact_request(req2))
    # Same fingerprint even though different API keys
    assert fp1 == fp2
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_redaction.py -q
```

Expected: FAIL — redaction module not created.

**Step 3: Implement redaction module**

Create `packages/data-core/catalyst_data/ingestion/redaction.py` with:
- `redact_request(raw_request) → dict`: strips `apiKey`, `Authorization`, `Cookie` from URL, headers, params; normalizes host/path
- `compute_request_fingerprint(redacted_request) → str`: SHA256 of canonical JSON per contract §3 rule 7
- `compute_cursor_fingerprint(raw_cursor_url) → str`: SHA256 hash

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_redaction.py -q
```

Expected: all PASS.

### Task 4: Request-attempt ledger

**Step 1: Write failing ledger test**

```python
# packages/data-core/tests/test_request_ledger.py

def test_insert_request_attempt():
    """Insert one attempt row with all required contract fields."""
    from catalyst_data.ingestion.request_ledger import insert_attempt

    attempt = {
        "request_id": "req-001",
        "run_id": "run-001",
        "logical_fetch_id": "lf-001",
        "source_type": "news",
        "provider": "polygon",
        "endpoint_name": "polygon_news",
        "ticker_or_series": "AAPL",
        "window_start": "2026-01-01",
        "window_end": "2026-01-01",
        "attempt_no": 1,
        "page_no": 1,
        "parent_request_id": None,
        "request_fingerprint": "a1" * 32,  # 64-hex per contract §4.6 CHECK
        "request_params_redacted": '{"ticker":"AAPL","limit":50}',
        "cursor_fingerprint": None,
        "started_at": "2026-01-01T09:30:00Z",
        "completed_at": None,
        "status": "STARTED",
    }
    db = _fresh_db_at_version(8)
    insert_attempt(db, attempt)

    row = db.execute("SELECT * FROM provider_request_attempts WHERE request_id = ?",
                     ("req-001",)).fetchone()
    assert row is not None
    assert row["status"] == "STARTED"


def test_request_id_is_unique():
    """Duplicate request_id raises integrity error."""
    db = _fresh_db_at_version(8)
    insert_attempt(db, {**base_attempt, "request_id": "req-dup"})
    with pytest.raises(sqlite3.IntegrityError):
        insert_attempt(db, {**base_attempt, "request_id": "req-dup"})


def test_attempt_transitions_to_terminal():
    """Attempt STARTED → SUCCEEDED is valid; STARTED → STARTED on a different outcome is not."""
    db = _fresh_db_at_version(8)
    insert_attempt(db, {**base_attempt, "request_id": "req-t", "status": "STARTED"})
    from catalyst_data.ingestion.request_ledger import transition_attempt
    transition_attempt(db, "req-t", "SUCCEEDED", http_status=200, items_count=30)
    row = db.execute("SELECT status, http_status, items_count FROM provider_request_attempts WHERE request_id = ?",
                     ("req-t",)).fetchone()
    assert row["status"] == "SUCCEEDED"
    assert row["http_status"] == 200


def test_logical_fetch_id_matches_contract():
    """logical_fetch_id = SHA256(run_id + ':' + cell_id)."""
    from catalyst_data.ingestion.request_ledger import compute_logical_fetch_id
    lfid = compute_logical_fetch_id("run-001", "cell-abc")
    assert len(lfid) == 64
    assert compute_logical_fetch_id("run-001", "cell-abc") == lfid
    assert compute_logical_fetch_id("run-002", "cell-abc") != lfid
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_request_ledger.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_data/ingestion/request_ledger.py`**

- `compute_logical_fetch_id(run_id, cell_id) → str`
- `insert_attempt(db, attempt) → None`
- `transition_attempt(db, request_id, status, **kwargs) → None`
- `logical_fetch_summary(db, logical_fetch_id) → dict`

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_request_ledger.py -q
```

Expected: all PASS.

### Task 5: Append-only raw store

**Step 1: Write failing raw store test**

```python
# packages/data-core/tests/test_raw_store.py

import hashlib

def test_raw_store_is_append_only():
    """Identical request_id/second_response raises integrity error."""
    from catalyst_data.ingestion.raw_store import store_raw_response, RawResponseIntegrityError

    db = _fresh_db_at_version(8)
    store_raw_response(db, request_id="req-1", response_bytes=b"hello",
                       content_encoding="identity", response_sha256=hashlib.sha256(b"hello").hexdigest())
    # Same request_id, different bytes → integrity error
    with pytest.raises(RawResponseIntegrityError):
        store_raw_response(db, request_id="req-1", response_bytes=b"different",
                           content_encoding="identity", response_sha256=hashlib.sha256(b"different").hexdigest())


def test_raw_store_idempotent():
    """Same request_id + same hash is a no-op."""
    from catalyst_data.ingestion.raw_store import store_raw_response

    db = _fresh_db_at_version(8)
    h = hashlib.sha256(b"payload").hexdigest()
    store_raw_response(db, request_id="req-1", response_bytes=b"payload",
                       content_encoding="identity", response_sha256=h)
    # Same again → no error, no duplicate
    store_raw_response(db, request_id="req-1", response_bytes=b"payload",
                       content_encoding="identity", response_sha256=h)
    count = db.execute("SELECT COUNT(*) FROM raw_assets WHERE request_id = 'req-1'").fetchone()[0]
    assert count == 1


def test_raw_store_preserves_decoded_body():
    """Stored body is HTTP-client-decoded; content_encoding is recorded separately."""
    from catalyst_data.ingestion.raw_store import store_raw_response

    db = _fresh_db_at_version(8)
    store_raw_response(db, request_id="req-2", response_bytes=b'{"results":[]}',
                       content_encoding="gzip", response_sha256=hashlib.sha256(b'{"results":[]}').hexdigest())
    row = db.execute("SELECT content_encoding, response_sha256 FROM raw_assets WHERE request_id = 'req-2'").fetchone()
    assert row["content_encoding"] == "gzip"
    assert row["response_sha256"] == hashlib.sha256(b'{"results":[]}').hexdigest()
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_raw_store.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_data/ingestion/raw_store.py`**

- `store_raw_response(db, request_id, response_bytes, content_encoding, response_sha256, page_no=1) → str` (returns `raw_asset_id`, which is exactly `'raw:' + request_id` per contract §4.6). `page_no` defaults to 1 for a single-page store; the pagination path (Task 7) passes the actual page number so each page's raw row records its own `page_no >= 1`.
- Existing identical hash: idempotent no-op
- Existing different hash: `RawResponseIntegrityError`
- New: INSERT with `data_version='v2'`, `asset_id='raw:'+request_id`, `request_id`, `page_no`, `content_encoding` (literal `identity` when the response had no content-encoding header), and 64-lowercase-hex `response_sha256`

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_raw_store.py -q
```

Expected: all PASS.

### Task 6: Normalized provenance

**Step 1: Write failing provenance test**

```python
# packages/data-core/tests/test_provenance.py

import hashlib

# entity_version is a 64-hex SHA-256 per contract §4.6; raw_asset_id has a
# FK to raw_assets(asset_id) ON DELETE RESTRICT, so each referenced raw row
# must be seeded first via the Task 0 helper seed_v2_raw_row.
EV = hashlib.sha256(b"article|normalized-v1").hexdigest()


def test_provenance_records_entity_to_raw_link():
    """Every normalized entity records provenance."""
    from catalyst_data.ingestion.provenance import record_provenance

    db = _fresh_db_at_version(8)
    seed_v2_raw_row(db, asset_id="raw:req-1", request_id="req-1")
    record_provenance(db, entity_type="article", entity_id="poly:article123",
                      entity_version=EV, raw_asset_id="raw:req-1",
                      normalizer_version="1.0.0")
    rows = db.execute(
        "SELECT * FROM normalized_provenance WHERE entity_id = ?",
        ("poly:article123",)).fetchall()
    assert len(rows) == 1


def test_provenance_handles_multi_source():
    """Same article from two raw sources records both."""
    from catalyst_data.ingestion.provenance import record_provenance

    db = _fresh_db_at_version(8)
    seed_v2_raw_row(db, asset_id="raw:req-a", request_id="req-a")
    seed_v2_raw_row(db, asset_id="raw:req-b", request_id="req-b")
    record_provenance(db, entity_type="article", entity_id="poly:articleX",
                      entity_version=EV, raw_asset_id="raw:req-a",
                      normalizer_version="1.0.0")
    record_provenance(db, entity_type="article", entity_id="poly:articleX",
                      entity_version=EV, raw_asset_id="raw:req-b",
                      normalizer_version="1.0.0")
    rows = db.execute(
        "SELECT raw_asset_id FROM normalized_provenance WHERE entity_id = ?",
        ("poly:articleX",)).fetchall()
    assert len(rows) == 2


def test_provenance_is_idempotent():
    """Duplicate provenance record is a no-op (composite PK)."""
    from catalyst_data.ingestion.provenance import record_provenance

    db = _fresh_db_at_version(8)
    seed_v2_raw_row(db, asset_id="raw:req-1", request_id="req-1")
    record_provenance(db, entity_type="article", entity_id="poly:dup",
                      entity_version=EV, raw_asset_id="raw:req-1",
                      normalizer_version="1.0.0")
    # Same again — no error
    record_provenance(db, entity_type="article", entity_id="poly:dup",
                      entity_version=EV, raw_asset_id="raw:req-1",
                      normalizer_version="1.0.0")
    count = db.execute(
        "SELECT COUNT(*) FROM normalized_provenance WHERE entity_id = 'poly:dup'"
    ).fetchone()[0]
    assert count == 1
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_provenance.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_data/ingestion/provenance.py`**

- `record_provenance(db, entity_type, entity_id, entity_version, raw_asset_id, normalizer_version) → None`
- Idempotent via composite PK

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_provenance.py -q
```

Expected: all PASS.

### Task 7: Polygon pagination

**Step 1: Write failing pagination test**

```python
# packages/data-core/tests/test_polygon_pagination.py

def test_pagination_creates_separate_request_rows():
    """Each page is a distinct request attempt with page_no incremented."""
    from catalyst_data.connectors.polygon import fetch_paginated_news
    from catalyst_data.ingestion.request_ledger import count_attempts_for

    db = _fresh_db_at_version(8)
    # Use a recorded fixture with two pages
    fetch_paginated_news(db, run_id="run-p", ticker="AAPL", date="2026-01-01",
                         plan={"page_limit": 3, "item_limit": 100})

    attempts = db.execute(
        "SELECT page_no FROM provider_request_attempts WHERE run_id = ? ORDER BY page_no",
        ("run-p",)).fetchall()
    pages = {a["page_no"] for a in attempts}
    assert 1 in pages
    assert 2 in pages  # at least two pages


def test_pagination_partial_stop_is_not_full_success():
    """Page-limit exhaustion marks logical fetch PARTIAL, not SUCCEEDED."""
    db = _fresh_db_at_version(8)
    from catalyst_data.connectors.polygon import fetch_paginated_news
    fetch_paginated_news(db, run_id="run-p", ticker="AAPL", date="2026-01-01",
                         plan={"page_limit": 1, "item_limit": 1000})

    cp = db.execute(
        "SELECT status, is_complete FROM source_checkpoints WHERE run_id = ?",
        ("run-p",)).fetchone()
    assert cp["is_complete"] == 0
    assert cp["status"] in ("PARTIAL",)


def test_pagination_content_dedup():
    """Overlapping articles across pages are deduplicated by provider-native ID."""
    db = _fresh_db_at_version(8)
    from catalyst_data.connectors.polygon import fetch_paginated_news
    total = fetch_paginated_news(db, run_id="run-d", ticker="AAPL", date="2026-01-01",
                                 plan={"page_limit": 3, "item_limit": 100})
    # Total articles ≤ sum of per-page items (some may overlap)
    raw_items = db.execute(
        "SELECT SUM(items_count) FROM provider_request_attempts WHERE run_id = ?",
        ("run-d",)).fetchone()[0]
    assert total <= raw_items


def test_pagination_cursor_loop_detected():
    """Repeating cursor value raises error, not infinite loop."""
    db = _fresh_db_at_version(8)
    from catalyst_data.connectors.polygon import fetch_paginated_news
    transport = ScriptedTransport.repeating_cursor("cursor-1")
    with pytest.raises(PaginationCursorLoopError):
        fetch_paginated_news(
            db, run_id="run-loop", ticker="AAPL", date="2026-01-01",
            plan={"page_limit": 5, "item_limit": 100}, transport=transport,
        )
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_polygon_pagination.py -q
```

Expected: FAIL — pagination loop not yet implemented.

**Step 3: Implement pagination in Polygon connector**

Modify `packages/data-core/catalyst_data/connectors/polygon.py`:
- Follow `next_url` for each request
- Each page = one `request_attempt` record
- Deduplicate by Polygon article ID
- Record page lineage (`parent_request_id`)
- Enforce `page_limit` and `item_limit` from plan
- Detect cursor loop (hash seen twice)
- Mark logical fetch `PARTIAL` when limits hit, `SUCCEEDED` when exhausted naturally

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_polygon_pagination.py -q
```

Expected: all PASS.

### Task 8: Durable run control — cancel, lease, resume

**Step 1: Write failing run control test**

```python
# packages/data-core/tests/test_run_control.py

def test_cancel_requested_before_cell():
    """Cell execution checks cancel_requested before making a request."""
    from catalyst_data.ingestion.run_control import is_cancelled, request_cancel

    db = _fresh_db_at_version(8)
    run_id = "run-cancel-test"
    db.execute("INSERT INTO ingestion_runs (run_id, status, cancel_requested) VALUES (?, 'RUNNING_EVIDENCE', 0)",
               (run_id,))
    assert not is_cancelled(db, run_id)
    request_cancel(db, run_id)
    assert is_cancelled(db, run_id)


def test_cancel_between_pages():
    """Pagination loop checks cancel between pages."""
    db = _fresh_db_at_version(8)
    run_id = "run-cancel-pages"
    db.execute("INSERT INTO ingestion_runs (run_id, status, cancel_requested) VALUES (?, 'RUNNING_EVIDENCE', 0)",
               (run_id,))
    # After page 1, mark cancelled
    request_cancel(db, run_id)
    # Next page should NOT execute
    from catalyst_data.ingestion.run_control import is_cancelled
    assert is_cancelled(db, run_id)


def test_resume_from_checkpoint():
    """Rerunning a PARTIAL run resumes from the last committed stage."""
    db = _fresh_db_at_version(8)
    run_id = "run-resume"
    db.execute("INSERT INTO ingestion_runs (run_id, status, cancel_requested) VALUES (?, 'PARTIAL', 0)",
               (run_id,))
    db.execute("""INSERT INTO source_checkpoints (run_id, source_type, ticker, window_start, window_end, status)
                  VALUES (?, 'ohlcv', 'AAPL', '2026-01-01', '2026-01-05', 'SUCCEEDED')""",
               (run_id,))
    from catalyst_data.ingestion.run_control import get_next_stage
    stage = get_next_stage(db, run_id)
    assert stage == "RUNNING_EVIDENCE"  # OHLCV done, evidence next


def test_lease_prevents_concurrent_writers():
    """Single-writer lease: acquiring while held raises."""
    db = _fresh_db_at_version(8)
    run_id = "run-lease"
    db.execute("INSERT INTO ingestion_runs (run_id, status, cancel_requested) VALUES (?, 'RUNNING_OHLCV', 0)",
               (run_id,))
    from catalyst_data.ingestion.run_control import acquire_lease, release_lease
    assert acquire_lease(db, run_id, holder="worker-1", ttl_seconds=300)
    assert not acquire_lease(db, run_id, holder="worker-2", ttl_seconds=300)
    release_lease(db, run_id, holder="worker-1")
    assert acquire_lease(db, run_id, holder="worker-3", ttl_seconds=300)
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_run_control.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_data/ingestion/run_control.py`**

- `request_cancel(db, run_id) → None`
- `is_cancelled(db, run_id) → bool`
- `get_next_stage(db, run_id) → str` (PLANNED → RUNNING_OHLCV → RUNNING_EVIDENCE → terminal)
- `acquire_lease(db, run_id, holder, ttl_seconds) → bool`
- `release_lease(db, run_id, holder) → None`

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_run_control.py -q
```

Expected: all PASS.

### Task 9: Two-stage OHLCV-first with evidence re-plan

**Step 1: Write failing two-stage test**

```python
# packages/data-core/tests/test_update_pipeline.py

@pytest.mark.asyncio
async def test_two_stage_execution_ohlcv_first():
    """OHLCV cells complete and commit before evidence cells are planned."""
    db = _fresh_db_at_version(8)
    plan = make_plan(ticker="AAPL", start="2026-01-01", end="2026-01-05",
                     stages=["ohlcv", "evidence"])
    report = await execute_update(db=db, plan=plan, transport=two_stage_transport())

    # OHLCV stage committed
    ohlcv_cps = db.execute(
        "SELECT status FROM source_checkpoints WHERE run_id = ? AND source_type = 'ohlcv'",
        (report.run_id,)).fetchall()
    assert len(ohlcv_cps) == expected_ohlcv_cell_count(plan)
    assert all(r["status"] == "SUCCEEDED" for r in ohlcv_cps)

    # Evidence cells use the refreshed watermark
    evidence_cps = db.execute(
        "SELECT window_start FROM source_checkpoints WHERE run_id = ? AND source_type = 'news'",
        (report.run_id,)).fetchall()
    assert len(evidence_cps) == expected_news_cell_count(plan)


@pytest.mark.asyncio
async def test_ohlcv_completion_triggers_evidence_replan():
    """After OHLCV commit, evidence cells are re-planned against the refreshed trading-session watermark."""
    db = _fresh_db_at_version(8)
    plan = make_plan(ticker="AAPL", start="2026-01-01", end="2026-01-05")
    report = await execute_update(db=db, plan=plan, transport=two_stage_transport())

    assert report.stage_sequence == ["ohlcv", "evidence"]
    assert report.evidence_plan_input_watermark == report.committed_ohlcv_watermark
    assert report.evidence_plan_hash == compute_plan_hash(report.evidence_plan)


@pytest.mark.asyncio
async def test_partial_ohlcv_produces_partial_run():
    """Failed OHLCV cell → run PARTIAL, evidence stage may still proceed on available data."""
    db = _fresh_db_at_version(8)
    plan = make_plan(ticker="AAPL", start="2026-01-01", end="2026-01-05")
    report = await execute_update(db=db, plan=plan, transport=partial_ohlcv_transport())
    assert report.status == "PARTIAL"


@pytest.mark.asyncio
async def test_allow_stale_ohlcv_override_is_recorded():
    """allow_stale_ohlcv must be explicitly set in plan and recorded in report."""
    db = _fresh_db_at_version(8)
    plan = make_plan(ticker="AAPL", start="2026-01-01", end="2026-01-05",
                     allow_stale_ohlcv=True)
    report = await execute_update(db=db, plan=plan, transport=stale_ohlcv_transport())
    assert report.allow_stale_ohlcv_overridden is True
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_update_pipeline.py -k "two_stage" -q
```

Expected: FAIL if current pipeline doesn't enforce two-stage with re-plan.

**Step 3: Implement two-stage in `update_pipeline.py`**

Define one asynchronous entry point, `async execute_update(*, db, plan, transport) -> UpdateReport`, and make `run_update_batch()` delegate to it. Do not add a same-named synchronous wrapper. Then:
1. Filter to OHLCV cells, execute, commit atomically
2. Refresh trading-session watermark
3. Re-plan evidence cells against refreshed watermark
4. Compare refreshed plan hash to confirmed contract → `PlanDriftError` on mismatch
5. Execute evidence cells
6. Persist terminal run report

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_update_pipeline.py -q
```

Expected: all PASS (enhanced pipeline, existing tests preserved).

### Task 10: Connector instrumentation and integration

**Step 1: Write integration test**

```python
# packages/data-core/tests/test_update_pipeline.py

@pytest.mark.asyncio
async def test_full_provenance_chain_two_articles():
    """One raw response with 2 articles → 1 raw row, 2 articles, 2 provenance records."""
    db = _fresh_db_at_version(8)
    plan = make_plan(ticker="AAPL", start="2026-01-01", end="2026-01-01",
                     stages=["evidence"], sources=["polygon_news"])
    report = await execute_update(db=db, plan=plan, transport=polygon_two_article_transport())

    raw_count = db.execute(
        "SELECT COUNT(*) FROM raw_assets WHERE request_id IN (SELECT request_id FROM provider_request_attempts WHERE run_id = ?)",
        (report.run_id,)).fetchone()[0]
    article_count = db.execute(
        "SELECT COUNT(*) FROM articles WHERE raw_asset_id IN (SELECT asset_id FROM raw_assets WHERE request_id IN (SELECT request_id FROM provider_request_attempts WHERE run_id = ?))",
        (report.run_id,)).fetchone()[0]
    prov_count = db.execute(
        "SELECT COUNT(*) FROM normalized_provenance WHERE entity_type = 'article' AND raw_asset_id IN (SELECT asset_id FROM raw_assets WHERE request_id IN (SELECT request_id FROM provider_request_attempts WHERE run_id = ?))",
        (report.run_id,)).fetchone()[0]

    assert raw_count == 1
    assert article_count == 2
    assert prov_count == 2


@pytest.mark.asyncio
async def test_success_empty_is_not_error():
    """Empty valid response → SUCCESS_EMPTY, not FAILED."""
    db = _fresh_db_at_version(8)
    plan = make_plan(ticker="ZVZZT", start="2026-01-01", end="2026-01-01",
                     stages=["evidence"], sources=["polygon_news"])
    report = await execute_update(db=db, plan=plan, transport=valid_empty_transport())
    # SUCCESS_EMPTY is a valid outcome, not a failure
    cp = db.execute(
        "SELECT status FROM source_checkpoints WHERE run_id = ?",
        (report.run_id,)).fetchone()
    assert cp["status"] == "SUCCESS_EMPTY"


@pytest.mark.asyncio
async def test_request_fingerprint_in_ledger_has_no_secrets():
    """Every request_attempt row has a fingerprint with no secrets."""
    db = _fresh_db_at_version(8)
    plan = make_plan(ticker="AAPL", start="2026-01-01", end="2026-01-01",
                     stages=["evidence"], sources=["polygon_news"])
    await execute_update(db=db, plan=plan, transport=polygon_two_article_transport())

    attempts = db.execute(
        "SELECT request_fingerprint, request_params_redacted, error_message_redacted FROM provider_request_attempts"
    ).fetchall()
    for a in attempts:
        fp = a["request_fingerprint"]
        params = a["request_params_redacted"] or ""
        error = a["error_message_redacted"] or ""
        # No apiKey anywhere
        combined = fp + params + error
        assert "apiKey" not in combined.lower()
        assert "apikey" not in combined.lower()
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_update_pipeline.py -k "provenance" -q
```

Expected: FAIL or needs fixture.

**Step 3: Implement connector instrumentation**

In each connector (polygon, finnhub, fmp, fred, sec, yfinance_fallback):
- Before request: `insert_attempt()` with status `STARTED`
- After response: `transition_attempt()` to terminal status
- After normalization: `record_provenance()` for each entity
- Store raw: `store_raw_response()` instead of old `upsert_raw_asset()`
- Redact before logging: `redact_request()` before any log/error

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_update_pipeline.py -k "provenance" -q
```

Expected: all PASS.

### Task 11: Provider canary (separate authorized step)

During autonomous B2 execution, implement and test only the authorization guard and request-cap behavior with injected fake transport. Do not invoke the `authorized=True` path, load credentials, or make a real provider request. A supervised live canary remains a later operator action requiring explicit architect authorization.

**Step 1: Write canary test**

```python
# packages/data-core/tests/test_update_pipeline.py

def test_provider_canary_requires_explicit_authorization():
    """Provider canary is a separate function, never called automatically."""
    from catalyst_data.provider_canary import run_provider_canary

    # Canary function exists but requires authorization flag
    with pytest.raises(ValueError, match="auth"):
        run_provider_canary(ticker="AAPL", authorized=False)
```

**Step 2: Implement `catalyst_data/provider_canary.py`**

- `run_provider_canary(provider, profile, ticker, authorized=False, request_cap=1) → dict`: makes at most one request for the explicitly selected provider/profile. It never defaults to all providers and requires `authorized=True`.

**Step 3: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_update_pipeline.py -k "canary" -q
```

Expected: PASS.

## 8. Landmine Tests

1. **`INSERT OR REPLACE` still used for new raw writes** — `rg "INSERT OR REPLACE" packages/data-core/catalyst_data/ingestion/raw_store.py` must return zero matches.
2. **Compatibility read for old raw_assets breaks** — `rg "asset_id" packages/data-core/catalyst_data/articles.py` must still resolve old deterministic IDs.
3. **Secret leaks in logged request objects** — `rg "apiKey|apikey|Authorization:" packages/data-core/catalyst_data/ingestion/` must return zero matches (redaction helper comparisons allowed).
4. **plan_hash changes on re-plan without plan change** — `test_plan_hash_deterministic` across two calls with identical plan must produce same hash.
5. **Pagination infinite loop** — `test_pagination_cursor_loop_detected` must catch repeated cursor.
6. **Cancel ignored after cell starts** — `test_cancel_between_pages` must verify cancel check between pages.
7. **Lease leaked after crash** — `test_lease_ttl_expired_acquirable` must allow reacquisition after TTL expires.
8. **Partial normalized write survives failure** — inject an exception after the raw row and first entity are prepared; assert request attempt, raw row, normalized entities, provenance edges, and checkpoint progress all roll back together.
9. **Plan-hash omission** — parameterize every contract-required field and prove each field changes the hash; independently construct one canonical JSON payload and assert its exact SHA256. Runtime-only fields must not change it.
10. **DDL implemented by interpretation** — compare `PRAGMA table_info`, `PRAGMA index_list/index_info`, foreign keys, trigger names, and trigger behavior against contract §4.6; column-presence-only tests are insufficient.
11. **Status case drift** — domain statuses remain uppercase and checkpoint storage statuses remain lowercase through the one explicit mapping; no caller persists an uppercase value into `source_checkpoints.status`.

## 9. Verification Ladder

```bash
# 1. New focused tests
.venv/bin/python -m pytest packages/data-core/tests/test_migrations.py -k "v8" -q
.venv/bin/python -m pytest packages/data-core/tests/test_request_ledger.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_raw_store.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_provenance.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_redaction.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_run_control.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_polygon_pagination.py -q

# 2. Existing related modules
.venv/bin/python -m pytest packages/data-core/tests/test_update_planner.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_update_pipeline.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_polygon_connector.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_finnhub_connector.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_fmp_connector.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_fred_connector.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_sec_connector.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_fallback.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_storage.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_orchestrator.py -q

# 3. Package canonical suite
.venv/bin/python -m pytest packages/data-core -q

# 4. Dependency package regression
.venv/bin/python -m pytest packages/agents -q
.venv/bin/python -m pytest packages/eval -q
.venv/bin/python -m pytest packages/app -q

# 5. DB SHA
shasum -a 256 data/catalyst_dev_ws4b.db
shasum -a 256 data/catalyst_eval_frozen_v2.db

# 6. Git hygiene
git diff --check

# 7. Staged files
git diff --cached --name-status
```

## 10. Evidence Report Template

```markdown
## B2 Completion Report

### Files Changed
- [list]

### Migrations Applied
- v8 applied to Dev DB (copy)

### Focused Test Counts
- test_migrations (v8): X passed
- test_request_ledger: X passed
- test_raw_store: X passed
- test_provenance: X passed
- test_redaction: X passed
- test_run_control: X passed
- test_polygon_pagination: X passed

### Canonical Package Counts
- data-core: X passed, Y skipped, Z xfailed (was 758, 1, 1)
- agents: X passed (was 237)
- eval: X passed (was 93)
- app: X passed (was 128)

### Landmine Results
- [pass/fail per landmine]

### Before/After DB SHA
- Dev DB before: 92731fb7c5c3e989b4fdcbefb9d1d2060974082a6c60f1bbecad6bc42ee846b0
- Dev DB after: 92731fb7c5c3e989b4fdcbefb9d1d2060974082a6c60f1bbecad6bc42ee846b0 (must remain unchanged during implementation)
- Frozen DB: 0d97a7ec61b6ec8fb5f9263b0b37b0efc9755812739d7afead7f720c3567e8dd (unchanged)

### Provider/Network Actions
- [list any supervised provider canary run, with authorization record]

### Artifacts Generated
- Run report

### Git Index State
- [git status --short]

### Unresolved Risks
- [list]

### Confirmation
- [ ] Next package (B3) not started
```

## 11. Git Boundaries

Recommended commit sequence (stage only; no commit without authorization):

1. `feat(data-core): add migration v8 — request ledger, provenance, raw guards`
2. `feat(data-core): add plan_hash completeness and PlanDriftError enforcement`
3. `feat(data-core): add request fingerprint and connector redaction`
4. `feat(data-core): add request-attempt ledger and raw store modules`
5. `feat(data-core): add normalized_provenance writer`
6. `feat(data-core): add Polygon pagination loop`
7. `feat(data-core): add durable run control — cancel, lease, resume`
8. `feat(data-core): enforce two-stage OHLCV-first with evidence re-plan`
9. `feat(data-core): instrument all connectors with ledger, redaction, provenance`
10. `feat(data-core): add provider canary module`

Migration (v8) must be its own separate commit. Runtime code and test fixtures are separate commits from tests.

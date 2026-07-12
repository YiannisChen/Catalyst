# W1-D: Two-Stage Unified Update Service — Implementation Plan

> **For executor:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Create the service facade (`update_service.py`) with `plan_update`, `execute_update`, `inspect_update`; two-stage execution with per-ticker ceilings; run-plan artifact persistence; PlanDriftError with strict/non-strict modes; W1-D migration (stage, plan_hash, plan_path). Cancel/resume are deferred to W1-E.

**Architecture:** `update_service.py` wraps `update_pipeline.py` (reshaped to accept explicit cell lists). `execute_update` re-plans before any write. Strict hash mismatch → `PlanDriftError` before writes (zero side effects). Non-strict mode logs drift and proceeds with actual plan. Artifact persisted atomically before first cell. Stage 2 final amendment before first evidence checkpoint. Temp artifact directories in all tests.

**Tech Stack:** Python 3.13, sqlite3, asyncio, dataclasses, json.

---

## 1. Scope

- `catalyst_data/update_service.py` — `plan_update`, `execute_update`, `inspect_update`
- Cancel/resume: raise `DurableControlUnavailableError` (not silent stubs) — implemented in W1-E
- Two-stage orchestration with per-ticker evidence ceilings
- Run-plan artifact persistence (temp + fsync + rename, before first cell)
- `PlanDriftError` with strict and non-strict modes
- Migration v8: `ingestion_runs` + stage, plan_hash, plan_path
- `RunConfig` additions: allow_stale_ohlcv, strict
- `RunReport` additive fields; inventory all existing JSON consumers

**Out of scope:** CLI (W1-F), durable cancel/resume (W1-E), heartbeat (W1-E).

---

## 2. Verified Current State

| File | Key facts |
|---|---|
| `update_pipeline.py` | `run_update` (L1277), `run_update_batch` (L870) |
| `quality.py` | `open_ingestion_run` (L450), `close_ingestion_run` (L496). No stage/plan_hash/plan_path |
| `run_report.py` | `RunConfig` (L17), `RunReport` (L34), `save_run_report` (L62) |
| `update_planner.py` (W1-A/B) | `plan_update`, `UpdatePlan` |
| `migrations.py` | v7 registered in W1-C |

---

## 3. Proposed Files

| File | Status |
|---|---|
| `catalyst_data/update_service.py` | **Proposed** |
| `catalyst_data/update_pipeline.py` | Modified (reshaped entry) |
| `catalyst_data/quality.py` | Modified (migration DDL) |
| `catalyst_data/migrations.py` | Modified (v8) |
| `catalyst_data/run_report.py` | Modified (+fields) |
| `packages/data-core/tests/test_update_service.py` | **Proposed** |

---

## 4. Contracts

### 4.1 Verb ownership (explicit, no silent stubs)

W1-D exports:
- `plan_update(RunConfig) -> UpdatePlan` — delegates to planner
- `execute_update(RunConfig, expected_plan_hash=None, strict=False) -> RunReport` — two-stage
- `inspect_update(run_id) -> RunStatus` — from ingestion_runs + checkpoints
- `cancel_update(run_id)` — raises `DurableControlUnavailableError("cancel_update available after W1-E")`
- `resume_update(parent_run_id)` — raises `DurableControlUnavailableError("resume_update available after W1-E")`

Tests must prove `DurableControlUnavailableError` is raised and zero writes occur.

### 4.2 PlanDriftError — strict vs non-strict

**Strict** (`strict=True`, `expected_plan_hash` provided, mismatch):
- `PlanDriftError` raised
- No run row created
- No plan artifact written
- No report generated
- No checkpoints written

**Non-strict** (`strict=False`, or no `expected_plan_hash`):
- `plan_drift` field in RunReport records expected and actual hashes
- Execution proceeds under the freshly computed actual plan
- Artifact and run row store the **actual executed** plan hash (not the expected one)
- Test both paths explicitly

### 4.3 Artifact contract (exact JSON schema)

```json
{
  "artifact_schema_version": 1,
  "run_id": "run_20260712T...",
  "parent_run_id": null,
  "initial_execution_plan": { /* full UpdatePlan */ },
  "stage2_provisional": { /* provisional evidence plan */ },
  "stage2_final": null,
  "executed_plan_hash": "sha256...",
  "amendment_state": "initial"
}
```

After stage 2 final planning:
- `stage2_final` populated
- `amendment_state` → `"stage2_final"`

**Semantic `plan_hash` meanings (never ambiguous):**
- Preview `plan_hash`: `initial_execution_plan.plan_hash` — what was shown to operator
- Executed `plan_hash`: `executed_plan_hash` — what was actually run (may differ in non-strict mode)
- Run row `plan_hash`: the executed one
- `content_hash`: SHA-256 over the full artifact JSON (excluding volatile fields)

### 4.4 Ordering/failure tests

| Scenario | Expected |
|---|---|
| Stale strict hash → zero writes | No run row, no artifact, no checkpoint, no report |
| Run-row open succeeds, artifact write fails | Failed run with zero cells; resume refuses |
| Artifact rename succeeds, plan_path DB update fails | Defined recovery (try update; if fail, mark run failed) |
| Stage-2 final artifact rewrite fails | No stage-2 cell executes; stage 1 results intact |
| First checkpoint callback asserts artifact + plan_path exist | Assertion in persist_cell or caller |
| stage2_final persisted before first evidence checkpoint | Ordering assert |
| Crash before stage2_final | W1-E recovery contract (deferred) |
| Per-ticker watermark ceiling | Stuck ticker doesn't block others |
| FRED/non-ticker source not capped by ticker watermark | Assert FRED gets full window |
| allow_stale_ohlcv recorded | Plan warnings, artifact, and report all show it |

---

## 5. Schema Impact

| Migration | Version | Owner |
|---|---|---|
| stage, plan_hash, plan_path | v8 | W1-D |

**Migration tests use temp DB copies only.** Real Dev DB untouched during implementation verification.

---

## 6. TDD Tasks

### Task 1: PlanDriftError strict + non-strict

**Tests:** `test_strict_hash_mismatch_zero_writes`, `test_non_strict_hash_mismatch_proceeds`, `test_non_strict_records_drift_in_report`

**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_update_service.py -q -k "plan_drift" --tb=short` → FAIL

**Implementation:** `PlanDriftError`, strict/non-strict logic in `execute_update`.

**Green:** 3 PASS.

### Task 2: Run-plan artifact

**Tests:** `test_artifact_exists_before_first_cell`, `test_artifact_atomic_write`, `test_artifact_failure_produces_failed_run`, `test_stage2_final_persisted_before_first_evidence_checkpoint`

Temp artifact directory per test (not real `data/run_plans/`).

**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_update_service.py -q -k "artifact" --tb=short` → FAIL

**Green:** 4 PASS.

### Task 3: Two-stage run

**Tests:** `test_two_stage_run_on_fixture`, `test_stage2_differs_when_stage1_moves_watermark`, `test_per_ticker_ceiling_stuck_ticker_does_not_block_others`, `test_fred_not_capped_by_ticker_watermark`, `test_allow_stale_ohlcv_recorded_in_all_artifacts`

**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_update_service.py -q -k "two_stage" --tb=short` → FAIL

**Green:** 5 PASS.

### Task 4: DurableControlUnavailableError

**Tests:** `test_cancel_raises_durable_control_unavailable`, `test_resume_raises_durable_control_unavailable`, `test_cancel_performs_zero_writes`

**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_update_service.py -q -k "durable_control" --tb=short` → FAIL

**Green:** 3 PASS.

### Task 5: Compatibility delegates

**Tests:** `test_run_update_delegate_still_works`, `test_run_update_batch_delegate_still_works`

**Green:** 2 PASS.

### Task 6: Migration v8 + full canonical

```
.venv/bin/python -m pytest packages/data-core/tests/test_update_service.py -q --tb=short
.venv/bin/python -m pytest packages/data-core -q
```

No new failures.

---

## 7. Git Boundary

**Commit:** `feat(data-core): two-stage unified update service facade`

**Stage manifest:**
- `catalyst_data/update_service.py` — new
- `catalyst_data/update_pipeline.py` — reshaped entry
- `catalyst_data/quality.py` — v8 DDL
- `catalyst_data/migrations.py` — v8 registered
- `catalyst_data/run_report.py` — +fields
- `packages/data-core/tests/test_update_service.py` — new

---

## 8. Landmines

1. `run_update` stays as thin delegate — signature must not break.
2. fetch_fn injection seams survive for mocking.
3. Inventory all RunReport JSON consumers before adding keys.
4. All test artifacts use temp directories — never `data/run_plans/`.
5. Migration tests use temp DB copies.

## 9. Exit Gate

See §10 orchestrator verification. Key: two-stage on fixture, PlanDriftError before writes, artifact before cells, per-ticker ceilings, migration columns exist.

## 10. Orchestrator Verification

- `PRAGMA table_info(ingestion_runs)` on temp migrated DB — stage, plan_hash, plan_path
- Stale hash under strict → zero new rows in `ingestion_runs`
- Verify artifact written before first checkpoint (inspect fixture DB)
- Non-strict: `plan_drift` field populated in RunReport


## Database Safety Protocol

1. **Before:** Record Dev DB SHA (`shasum -a 256 data/catalyst_dev_ws4b.db`)
2. **After tests:** Re-record; compare. Dev DB unchanged unless slice owns a migration.
3. **Frozen DB:** SHA must not change. Never opened writable.
4. **Migration tests:** Temp DB copies only. Real Dev DB untouched during implementation.
5. **WAL/SHM:** Check before/after — no new sidecars from read-only access.

## Rollback Strategy

Revert commit. No schema to roll back (if no migration in this slice). No data affected.
Migration v8 is additive ALTER TABLE. Revert drops the migration registration; existing rows keep the columns (SQLite does not support DROP COLUMN easily). Safe.

## Worker Handoff Report

1. Pre-fix: DB SHAs, canonical counts
2. Focused test results: all green
3. Canonical suite: exact pass/fail/skip/xfail counts
4. `git diff --stat` against pre-slice snapshot
5. Allowlist check: all paths in manifest
6. DB SHA comparison (before == after, or migration on temp copy)
7. Frozen DB SHA unchanged
8. Any unexpected findings

## Orchestrator Verification

1. `PRAGMA table_info(ingestion_runs)` on temp migrated DB — stage, plan_hash, plan_path exist
2. Stale hash under strict → zero new rows in `ingestion_runs`
3. Verify artifact written before first checkpoint (inspect fixture DB)
4. Non-strict: `plan_drift` field populated in RunReport

## Dependencies

**Prerequisites:** W1-C committed and verified.
**Depended on by:** W1-E.

## Architect Decision Gates

None — schema/contract decisions resolved in W1-C and W1-E.

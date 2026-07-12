# W1-E: Durable Run Control — Implementation Plan

> **For executor:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace in-process cancel with durable cross-process control via ingestion_runs columns, lease-based single-writer enforcement, injectable-heartbeat task, and plan-based resume.

**Architecture:** Injectable heartbeat interval/staleness/clock for fast deterministic tests. `WriterConflictError` tested through service boundary, not raw SQLite locking. Defined function signatures with explicit `db_path` parameters. Kill/crash tests use fixture-only child processes with explicit timeouts and `finally` cleanup.

**Tech Stack:** Python 3.13, sqlite3, asyncio, multiprocessing (tests only), os.getpid().

---

## 1. Scope

- Migration v9: heartbeat_at, pid, cancel_requested_at, parent_run_id
- WriterConflictError — service-level lease acquisition
- Heartbeat task with injectable interval + clock
- Durable cancel: cancel_requested_at column, polled in cell loop
- Plan-based resume with full error handling
- RunStatus read-model

---

## 2. Contracts

### 2.1 Explicit function signatures

```python
def cancel_update(db_path: str, run_id: str) -> CancelAck: ...
def inspect_update(db_path: str, run_id: str) -> RunStatus: ...
def resume_update(db_path: str, parent_run_id: str, *, fetch_fn=None) -> RunReport: ...
```

### 2.2 Injectable heartbeat

```python
@dataclass
class HeartbeatConfig:
    interval_sec: float = 60.0
    stale_threshold_sec: float = 600.0

def _now_iso() -> str:
    """Injectable clock for deterministic tests."""
    return datetime.now(timezone.utc).isoformat()

async def _heartbeat_loop(
    db_path: str,
    run_id: str,
    stop_event: asyncio.Event,
    *,
    config: HeartbeatConfig | None = None,
    now_fn: Callable[[], str] = _now_iso,
    sleep_fn: Callable[[float], Any] = asyncio.sleep,
):
    """Background heartbeat task.  Owned by the run lifecycle."""
    if config is None:
        config = HeartbeatConfig()  # immutable default after instantiation
    while not stop_event.is_set():
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute("PRAGMA busy_timeout = 2000")
                conn.execute(
                    "UPDATE ingestion_runs SET heartbeat_at=? WHERE run_id=?",
                    (now_fn(), run_id),
                )
                conn.commit()
        except Exception:
            logger.warning("Heartbeat tick failed for %s", run_id, exc_info=True)
            # Failed tick is logged but does not kill execution
        try:
            await sleep_fn(config.interval_sec)
        except asyncio.CancelledError:
            break  # clean shutdown — suppress at owning lifecycle boundary

# Heartbeat task handle and shutdown:
#   hb_task = asyncio.create_task(_heartbeat_loop(...))
#   On completion/cancel/crash:
#       stop_event.set()
#       hb_task.cancel()
#       await hb_task  # suppress CancelledError here
#       hb_task = None  # no leaked reference

Tests (deterministic, no real sleeps):
- task exits immediately when stop_event is set
- no heartbeat UPDATE after run completion
- no pending task warning (hb_task is None after shutdown)
- cancellation during sleep_fn does not wait for production interval
- failed heartbeat connection is logged and retried without killing execution
- inject now_fn and sleep_fn for fast, deterministic test execution
```

Tests inject `now_fn=...`, `sleep_fn=...`, and `HeartbeatConfig(interval_sec=0.01, stale_threshold_sec=0.05)` for fast deterministic execution.

### 2.3 Cancellation states

| State | Response |
|---|---|
| Unknown run_id | `CancelError("run not found")` |
| Already completed | `CancelError("run already completed")` |
| Already canceled | Idempotent — return same ack |
| Active run | Set cancel_requested_at; return `CancelAck` |
| During slow fetch | Cell loop polls between cells; stops after current cell |
| Before first cell | Executor checks after plan persistence |
| Between stages | Executor checks at stage boundary |

### 2.4 Heartbeat lifecycle

- Starts after lease ownership established
- Stops on success/failure/cancel via `stop_event.set()`
- No leaked tasks: `finally: stop_event.set()`
- Missed tick logged but not fatal
- Uses short-lived connection with busy timeout
- Never holds transaction across provider fetch
- kill/crash stops heartbeat naturally (process exits)

### 2.5 Resume error handling

| Condition | Response |
|---|---|
| parent null plan_path | `ResumeError("parent run has no plan artifact")` |
| Corrupt artifact (invalid JSON) | `ResumeError("plan artifact corrupt")` |
| Missing artifact file | `ResumeError("plan artifact not found")` |
| Artifact hash mismatch | `ResumeError("plan artifact hash mismatch")` |
| Failed cells | Restored |
| Skipped cells | Restored |
| Never-attempted cells | Restored |
| Success cells | Excluded |
| Success_empty cells | Excluded |
| Resume-of-resume | parent_run_id chain linked |
| Crash before stage2_final | Follows W1-D recovery contract |
| Crash after stage2_final | Resume from stage 2 boundary |
| Duplicate completion by another run | Idempotent — no-op or already-completed |

---

## 3. TDD Tasks

### Task 1: WriterConflictError (service-level)

**Tests:** `test_concurrent_open_refused_with_holder_info`, `test_stale_lease_interrupted_and_replaced`, `test_reused_pid_not_treated_as_live`, `test_live_heartbeat_wins`

Tests call `execute_update`, not raw SQLite connections.

**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_durable_control.py -q -k "conflict" --tb=short` → FAIL

**Green:** 4 PASS.

### Task 2: Cancellation states

**Tests:** `test_cancel_unknown_run`, `test_cancel_already_completed`, `test_cancel_idempotent`, `test_cancel_active_run`, `test_cancel_during_slow_fetch`, `test_cancel_before_first_cell`, `test_cancel_between_stages`

**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_durable_control.py -q -k "cancel" --tb=short` → FAIL

**Green:** 7 PASS. All use injectable clock; no real sleeps.

### Task 3: Heartbeat

**Tests:** `test_heartbeat_ticks_during_slow_cell` (injectable short interval, not 90s), `test_orphan_detected`, `test_heartbeat_stops_on_completion`, `test_heartbeat_stops_on_cancel`, `test_no_leaked_task`

**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_durable_control.py -q -k "heartbeat" --tb=short` → FAIL

**Green:** 5 PASS.

### Task 4: Plan-based resume

**Tests:** All §2.5 error conditions + `test_resume_recovers_all_non_success_cells`, `test_resume_excludes_success_cells`, `test_resume_excludes_success_empty_cells`, `test_resume_of_resume_chain`, `test_crash_recovery`

**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_durable_control.py -q -k "resume" --tb=short` → FAIL

**Green:** 10+ PASS.

### Task 5: Kill test (fixture only)

```python
def test_kill9_child_resume_equals_control():
    """Fixture DB only. Child process. Explicit timeout. finally cleanup."""
    import multiprocessing, signal, time
    # Spawn child executing fixture run
    # kill -9 child
    # Resume from parent
    # Assert final checkpoint set equals control run
```

**Green:** PASS.

### Task 6: Migration v9 + canonical

```
.venv/bin/python -m pytest packages/data-core/tests/test_durable_control.py -q --tb=short
.venv/bin/python -m pytest packages/data-core -q
```

No new failures. Migration tested on temp DB copies only.

---

## 4. Schema Impact

| Migration | Version | Owner |
|---|---|---|
| heartbeat_at, pid, cancel_requested_at, parent_run_id | v9 | W1-E |

---

## 5. Git Boundary

**Commit:** `feat(data-core): durable cancel, heartbeat, single-writer, resume hardening`

**Stage manifest:**
- `catalyst_data/quality.py` — v9 DDL, lease, RunStatus
- `catalyst_data/update_pipeline.py` — durable cancel poll, heartbeat task
- `catalyst_data/update_service.py` — cancel_update, resume_update
- `catalyst_data/migrations.py` — v9
- `catalyst_data/doctor.py` — heartbeat-based orphan
- `packages/data-core/tests/test_durable_control.py` — new

---

## 6. Landmines

1. Pre-migration null heartbeat rows — 24h age backstop persists.
2. Lease uses short transactions with busy timeout — never held across fetches.
3. Heartbeat task: `asyncio.create_task` + `finally` cancel — no leaked tasks.
4. PID not sufficient alone — lease checks heartbeat staleness too.
5. Frozen DB migration refusal test.
6. Kill test: fixture DB only, child process only, explicit timeout, `finally` cleanup. No real DB. No provider calls.

## 7. Exit Gate

- Cross-process cancel on fixture
- WriterConflictError with holder info
- Heartbeat ticks during slow cells (injectable config)
- Plan-based resume recovers all non-success cells
- Kill-9 child resume equals control (fixture)
- PRAGMA shows all 7 W1-D/W1-E columns on temp migrated DB

## 8. Orchestrator Verification

- `PRAGMA table_info(ingestion_runs)` on temp DB — 4 new columns
- Run cross-process cancel drill on fixture personally
- `rg "_CANCEL_REQUESTS"` in non-deprecated paths
- Confirm resume recovers failed + skipped + never-attempted


## Database Safety Protocol

1. **Before:** Record Dev DB SHA (`shasum -a 256 data/catalyst_dev_ws4b.db`)
2. **After tests:** Re-record; compare. Dev DB unchanged unless slice owns a migration.
3. **Frozen DB:** SHA must not change. Never opened writable.
4. **Migration tests:** Temp DB copies only. Real Dev DB untouched during implementation.
5. **WAL/SHM:** Check before/after — no new sidecars from read-only access.

## Rollback Strategy

Revert commit. No schema to roll back (if no migration in this slice). No data affected.
Migration v9 is additive ALTER TABLE. Revert drops the migration registration; existing rows keep the columns. Safe.

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

1. `PRAGMA table_info(ingestion_runs)` on temp DB — 4 new columns
2. Run cross-process cancel drill on fixture personally
3. `rg "_CANCEL_REQUESTS"` in non-deprecated paths
4. Confirm resume recovers failed + skipped + never-attempted

## Dependencies

**Prerequisites:** W1-D committed and verified.
**Depended on by:** W1-F.

## Architect Decision Gates

None — heartbeat/staleness constants are configurable defaults, not architecture decisions.

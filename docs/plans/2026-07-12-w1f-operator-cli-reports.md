# W1-F: Operator CLI and Reports — Implementation Plan

> **For executor:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build `scripts/update.py` CLI wrapping service verbs with explicit exit codes, preview-confirm flow, canary redaction via centralized `_redact_secrets`, and an operator runbook.

**Architecture:** `scripts/update.py` uses argparse with subcommands: plan, execute (`run` alias), status, cancel, resume, doctor, backup. Thin presentation — no planner/executor logic duplication. JSON mode never prompts. Redaction reuses `catalyst_data.doctor._redact_secrets` — no duplicate logic. Backup uses SQLite-safe API or closed-file copy.

**Tech Stack:** Python 3.13, argparse, json, update_service.py verbs.

---

## 1. Scope

- `scripts/update.py` subcommands: plan, execute (alias: run), status, cancel, resume, doctor, backup
- JSON and human-readable output modes; JSON never prompts interactively
- Explicit exit codes (one mapping, no ambiguity)
- Preview-confirm flow: `plan --out PATH` → review → `execute --plan PATH --expected-plan-hash HASH`
- Duration estimates as ranges, not false precision
- Centralized redaction via `catalyst_data.doctor._redact_secrets`
- `scripts/backfill.py` deprecation pointer
- `docs/runbooks/update-operations.md` operator runbook
- Runbook tested via executable doc extraction where practical

**Out of scope:** HTTP API, frontend, service logic changes.

---

## 2. Verified Paths

| Path | Notes |
|---|---|
| `packages/data-core/scripts/backfill.py` | Legacy backfill |
| `packages/data-core/catalyst_data/backfill_pipeline.py` | Backfill pipeline engine |
| `catalyst_data/doctor.py` `_redact_secrets` | Existing centralized redaction |
| `scripts/update.py` | **Proposed** — repo root |

---

## 3. CLI Contract

### Subcommands

```
update.py plan [--tickers ...] [--sources ...] [--from DATE] [--to DATE] [--json] [--out PATH]
update.py execute [--tickers ...] [--sources ...] [--plan PATH] [--expected-plan-hash HASH] [--strict] [--allow-stale-ohlcv] [--yes] [--backup] [--json]
update.py run     # alias for execute, documented and tested
update.py status [RUN_ID|--latest] [--watch] [--json]
update.py cancel RUN_ID [--json]
update.py resume PARENT_RUN_ID [--json]
update.py doctor [--json]
update.py backup [--out PATH]
```

### Exit codes (one stable mapping)

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Usage/argument error |
| 2 | Planning error (SchemaOutOfDate, CalendarCoverageError) |
| 3 | Plan drift (strict mode) |
| 4 | Writer conflict (another process running) |
| 5 | Execution failure |
| 6 | Cancelled by operator |
| 7 | Unknown run ID |
| 8 | Certification failure (W2 integration) |

### Preview-confirm flow

```
.venv/bin/python scripts/update.py plan --out /tmp/plan.json
# Operator reviews estimates
.venv/bin/python scripts/update.py execute --plan /tmp/plan.json --expected-plan-hash <hash> --yes
```

- `--yes` required for automation; without it, human mode prompts for confirmation
- JSON mode (`--json`) never prompts interactively
- Stale plan: strict mode → PlanDriftError (exit 3); non-strict → proceeds with drift logged

### Duration estimates

Report as range: `min_provider_throughput → max_provider_throughput`. Label as approximate. No false precision.

### Redaction (centralized, not duplicated)

```python
# Reuse, do not duplicate:
from catalyst_data.doctor import _redact_secrets as _redact

def _redact_all_output(data):
    """Apply centralized redaction to any output surface."""
    if isinstance(data, str):
        return _redact(data)
    if isinstance(data, dict):
        return {k: _redact_all_output(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_redact_all_output(v) for v in data]
    return data
```

Redaction test surfaces: stdout, stderr, exception text, JSON output, saved report file, dict keys, nested structures, query-string API keys, canary with punctuation. Canary proven absent via exact byte search.

---

## 4. TDD Tasks

### Task 1: CLI exit codes
**Tests:** One test per exit code. Verify `sys.exit(code)` called.
**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_update_cli.py -q -k "exit" --tb=short` → FAIL
**Green:** 8 PASS.

### Task 2: Canary redaction
**Tests:** All surfaces from §3 redaction spec. Use literal canary `sk-test-canary-deadbeef` and `grep` output for it.
**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_update_cli.py -q -k "redact" --tb=short` → FAIL
**Green:** all PASS; canary byte search — zero matches.

### Task 3: Preview-confirm + JSON
**Tests:** `test_json_mode_never_prompts`, `test_plan_execute_roundtrip`, `test_stale_plan_strict_exits_3`
**Green:** 3 PASS.

### Task 4: Backup subcommand
**Tests:** `test_backup_produces_sha_pinned_copy`, `test_backup_source_unchanged`, `test_backup_destination_collision`
Temp DB only. No live DB copy.

### Task 5: Runbook executable extraction
**Tests:** `test_runbook_commands_parse` — extract code blocks from `docs/runbooks/update-operations.md` and verify they are syntactically valid.

### Task 6: Full canonical

```
.venv/bin/python -m pytest packages/data-core/tests/test_update_cli.py -q --tb=short
.venv/bin/python -m pytest packages/data-core -q
```

No new failures from baseline.

---

## 5. Git Boundary

**Commit:** `feat(data-core): operator CLI and report extensions`

**Stage manifest:**
- `scripts/update.py` — new
- `docs/runbooks/update-operations.md` — new
- `packages/data-core/scripts/backfill.py` — deprecation pointer
- `packages/data-core/tests/test_update_cli.py` — new

---

## 6. Landmines

1. CLI imports nothing that writes at import time.
2. `--out` default must not land inside `data/`.
3. Watch-mode polling uses `inspect_update` only.
4. Backup uses SQLite-safe API or explicitly safe closed-file copy; no raw `cp` of live DB.
5. Redaction is centralized — no duplicate regex.
6. `run` alias is documented and tested.

## 7. Exit Gate

- All subcommands exit with documented codes
- canary byte search across all output surfaces: zero matches
- plan --execute roundtrip via --plan PATH
- --backup produces SHA-pinned copy
- No new canonical failures


## Database Safety Protocol

1. **Before:** Record Dev DB SHA (`shasum -a 256 data/catalyst_dev_ws4b.db`)
2. **After tests:** Re-record; compare. Dev DB unchanged unless slice owns a migration.
3. **Frozen DB:** SHA must not change. Never opened writable.
4. **Migration tests:** Temp DB copies only. Real Dev DB untouched during implementation.
5. **WAL/SHM:** Check before/after — no new sidecars from read-only access.

## Rollback Strategy

Revert commit. No schema to roll back (if no migration in this slice). No data affected.
No migration. Revert commit.

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

1. Run every subcommand from fresh shell using runbook
2. grep captured output for canary key — must be empty
3. Verify exit codes match documented mapping

## Dependencies

**Prerequisites:** W1-E committed and verified.
**Depended on by:** W3-B.

## Architect Decision Gates

None — CLI is presentation layer only.

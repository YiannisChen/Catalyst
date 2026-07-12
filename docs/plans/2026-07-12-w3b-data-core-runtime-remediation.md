# W3-B: Data-Core Runtime Remediation — Implementation Plan

> **For executor:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Drive data-core test suite to full green (or architect-approved quarantine) by classifying and fixing all real post-W1-F failures.

**Architecture:** Post-W1-F canonical run establishes actual failure count. Every failure is classified into exactly one root-cause group with signature, classification, and resolution. Environment fixes first, then contract-drift, then regressions. No skip/xfail without architect approval. Production-code changes allowed only when a reproduced regression requires them and are traceable to a failure group.

**Tech Stack:** Python 3.13, pytest, existing test infrastructure.

**Note:** Current W3-A baseline is 703 passed, zero failures. CD-1 through CD-4 are **hypotheses to inspect only if matching failures actually occur** — not pre-assigned group IDs.

---

## 1. Scope

- Run canonical suite post-W1-F
- Classify every real failure into root-cause group
- Fix environment groups
- Fix contract-drift groups (W1 deliberate changes — only if they actually produce failures)
- Fix regression groups (production-code changes allowed only when a reproduced regression requires them, mapped to a failure group)
- Delete obsolete tests only with architect per-case approval and caller-search evidence

**Out of scope:** New features, schema changes, behavior changes not traceable to a failure group.

---

## 2. Hypotheses (NOT pre-registered groups)

CD-1 through CD-4 may appear only as hypotheses to inspect if matching failures actually occur:
- **CD-1:** dry_run delegation returns plan-preview (test_update_pipeline.py dry_run tests)
- **CD-2:** Calendar-derived defaults change window
- **CD-3:** upsert_ohlcv precedence changes behavior
- **CD-4:** Status vocabulary canonicalized

Do not assign group IDs until real failures are observed.

---

## 3. Execution Workflow

### Phase A: Establish post-W1-F baseline

```
.venv/bin/python -m pytest packages/data-core -q
```
Record exact pass/fail counts. Extract every `FAILED` node ID.

### Phase B: Classify every failure

For each failed node ID:
1. Capture failure signature (full traceback or assertion message)
2. Classify: environment / regression / contract-drift / obsolete
3. Assign to exactly one root-cause group
4. Group must have: group ID, shared signature, affected test IDs, count, classification, root cause, proposed resolution

**Reconcile:** `sum(group counts) == actual failure count`

### Phase C: Fix environment groups first

If any environment failures exist, fix them. Re-run suite after each fix. Environment fixes may unmask new failures — re-classify.

### Phase D: Fix contract-drift groups

For each CD group with actual failures:
1. Identify exact affected tests
2. Update assertions to match current normative contract
3. Verify no assertion weakening (compare old vs new assertion intent)
4. Run focused verification: `.venv/bin/python -m pytest packages/data-core/tests/<file>.py -q --tb=short`

### Phase E: Fix regression groups

For each regression:
1. Reproduce on fixture
2. Root-cause in production code
3. Fix in production code (allowed only when regression is reproduced)
4. Every production change maps to a failure group
5. No unrelated behavior changes

### Phase F: Handle obsolete tests

Only with architect per-case approval:
- Group ID + reason + owner + expiry/removal condition
- Caller-search evidence (grep entire codebase for test function references)
- Delete test file/function only after approval

### Phase G: Full green

```
.venv/bin/python -m pytest packages/data-core -q
```

Must be 0 failures (or architect-approved quarantine with group IDs, reasons, and expiry conditions).

---

## 4. Forbidden

- No skip/xfail without architect approval + group ID + reason + owner + expiry
- No assert loosening without group ID
- No behavior changes not traceable to a group
- No "quarantine by default" — skip/xfail is exceptional

## 5. Exit Gate

- Full package green or architect-approved quarantine
- Every failure classified
- No unexplained failures

## 6. Orchestrator Verification

- Roadmap §K W3 procedure: pick 3 random groups, repro pre-fix, verify fix, reconcile counts
- Verify no skip/xfail without approval + group ID
- Verify every production change maps to a group


## Database Safety Protocol

1. **Before:** Record Dev DB SHA (`shasum -a 256 data/catalyst_dev_ws4b.db`)
2. **After tests:** Re-record; compare. Dev DB unchanged unless slice owns a migration.
3. **Frozen DB:** SHA must not change. Never opened writable.
4. **Migration tests:** Temp DB copies only. Real Dev DB untouched during implementation.
5. **WAL/SHM:** Check before/after — no new sidecars from read-only access.

## Rollback Strategy

Revert commit. No schema to roll back (if no migration in this slice). No data affected.
No migration. Revert per-group fix commits.

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

1. Roadmap §K W3 procedure: pick 3 random groups, repro pre-fix, verify fix, reconcile counts
2. Verify no skip/xfail without approval + group ID
3. Verify every production change maps to a group

## Dependencies

**Prerequisites:** W1-F committed and verified.
**Depended on by:** W2.

## Architect Decision Gates

AD-W3B-1: obsolete test deletion requires per-case architect approval.

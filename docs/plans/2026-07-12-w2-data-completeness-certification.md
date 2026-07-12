# W2: Data Completeness Certification — Implementation Plan

> **For executor:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a deterministic, read-only certification generator that evaluates Dev DB completeness across all required dimensions with architect-approved thresholds, produces tri-state checks, and separates deterministic payload from volatile envelope.

**Architecture:** `catalyst_data/certification.py` reads the DB via `mode=ro` and the planner. Checks use approved thresholds from configuration (included in deterministic hash). Output: deterministic payload + volatile envelope at `data/certifications/<content_hash>.json`. `require_certified()` raises `CertificationFailed` if overall result is fail. No placeholder W7 tests — W2 owns its gate.

**Tech Stack:** Python 3.13, hashlib, json, update_planner.py, freshness.py.

---

## 1. Scope

- `catalyst_data/certification.py` — certification generator
- `scripts/certify.py` — CLI entry
- Certification dimensions with exact denominators and N/A rules
- Tri-state: pass / fail / not_applicable (must never improve pass denominator silently)
- Deterministic payload separated from volatile envelope
- `require_certified()` gate — `CertificationFailed` if overall fail
- Read-only proof: mode=ro, no migrations, before/after SHA

**Out of scope:** Data fixes, update service changes, HTTP API, frontend.

---

## 2. Artifact Contract

The actual `db_user_version` value is derived at runtime from
`catalyst_data.migrations.MIGRATIONS` max version — do not hardcode.
The JSON example shows `9` as the planned value after W1-E migration v9.

### Schema version derivation

Certification derives the expected schema version from the canonical
migration registry (`catalyst_data.migrations.MIGRATIONS` max version)
or from a single exported constant (`EXPECTED_SCHEMA_VERSION`).  Never
duplicate a magic number.

Tests:
- DB user_version below registry max → schema.version check fails
- DB user_version equal to registry max → passes
- DB user_version above registry max → fail closed (unsupported schema)

### Deterministic payload (covered by content_hash)

```json
{
  "certification_schema_version": 1,
  "db_sha256": "...",
  "db_user_version": 9,
  "universe": {"tickers": [...], "provenance": "explicit-config"},
  "config": {"historical_start": "2024-12-30", "reference_today": "2026-07-12"},
  "thresholds": {"ohlcv_coverage_pct": null, "news_coverage_pct": null, ...},
  "checks": [{"check_id": "...", "status": "pass", "measured_value": ..., "threshold": ..., "reason": "..."}],
  "summary": {"pass": 5, "fail": 0, "not_applicable": 2},
  "overall": "pass"
}
```

### Volatile envelope (excluded from hash)

```json
{
  "generated_at": "2026-07-12T...",
  "output_path": "data/certifications/<content_hash>.json",
  "operator": "..."
}
```

- `db_path` (absolute path) excluded — use logical identity + SHA
- `content_hash` = SHA-256 over canonical deterministic payload only

### Deterministic artifact (immutable)

**Path:** `data/certifications/<content_hash>.json`
**Content:** canonical deterministic payload only (certification_schema_version,
db_sha256, db_user_version, universe, config, thresholds, checks, summary, overall).
**Behavior:** Repeated certification on identical DB/config produces
**byte-identical** JSON output. Overwrite is safe — same bytes.

### Volatile report (may differ across runs)

**Path:** `data/certifications/<content_hash>-report.json` or stdout.
**Content:** `generated_at`, `operator`, `output_path`, `elapsed_sec`.
**Behavior:** Repeated certification may produce different volatile metadata
without touching the deterministic artifact.

### content_hash contract

`content_hash` = SHA-256 over the canonical deterministic JSON bytes.
Test with byte-for-byte comparison, not just equal-hash equality.

```python
def test_deterministic_artifact_byte_identical(self, tmp_path):
    """Two certifications on same DB produce byte-identical deterministic JSON."""
    cert1 = run_certification(db_path, config)
    cert2 = run_certification(db_path, config)
    path1 = Path(cert1.artifact_path)
    path2 = Path(cert2.artifact_path)
    assert path1.read_bytes() == path2.read_bytes()
    assert hashlib.sha256(path1.read_bytes()).hexdigest() == cert1.content_hash
```

- No absolute local path in deterministic JSON.
- Use logical DB identity + SHA, not `/Users/...` paths.


## 3. Thresholds

Approved thresholds come from configuration (included in deterministic hash).
Recommendations below are AD-CERT-1 only; they are NOT normative until approved.

| Check | Denominator | Recommendation | Architect Decision |
|---|---|---|---|
| ohlcv.coverage | Calendar sessions x tickers | >= 95% | AD-CERT-1 |
| ohlcv.freshness | Days since last bar | <= 1 trading day | AD-CERT-1 |
| news.coverage | Calendar sessions x tickers where news configured | >= 80% | AD-CERT-1 |
| filings.coverage | Expected filings per SEC model | Model-dependent | AD-FILINGS-1 |
| macro.coverage | Configured FRED series present | 100% | AD-CERT-1 |
| checkpoint.exhaustiveness | Planned cells vs terminal checkpoints | 100% | AD-CERT-1 |

**Fail-closed rule:** If required thresholds are absent from configuration,
certification fails. No silent defaults in production.

## 4. N/A Rules

- `not_applicable` must never improve the pass denominator silently.
- Required-but-unconfigured must fail, not become `not_applicable`.
- SEC filings: `not_applicable` only if no approved expected-filings model exists.
- News coverage: denominator = calendar sessions x tickers. If no news sources
  are configured, that check is `not_applicable`.
- Every check whose status is `not_applicable` must carry an explicit reason
  in the `reason` field.

## 5. TDD Tasks

### Task 1: CertCheck + Certification dataclasses + deterministic hash

**Tests:** `test_certification_hash_excludes_volatile_fields`, `test_two_runs_identical_hash`, `test_db_sha_differs_hash_differs`

### Task 2: Tri-state exhaustiveness

**Test:** `test_every_check_is_pass_fail_or_na` — iterate all checks; every status in {pass, fail, not_applicable}

### Task 3: Required-but-unconfigured fails closed

**Test:** `test_missing_thresholds_cause_fail` — certification with absent thresholds → `overall: "fail"`

### Task 4: Coverage vs calendar (circularity)

**Test:** Delete one OHLCV row → coverage% drops. Delete calendar day → coverage unchanged (denominator is calendar).

### Task 5: require_certified gate

**Test:** `test_certification_failed_raises`, `test_certification_passed_returns_true`

### Task 6: Read-only proof

**Test:** `test_certification_read_only` — mode=ro, no migrations, before/after SHA identical, artifact output only outside DB

### Task 7: Direct-SQL adversarial verification for selected checks

**Test:** Spot-check ohlcv coverage by direct SQL count vs planner calendar count.

### Task 8: Full canonical green

```
.venv/bin/python -m pytest packages/data-core/tests/test_certification.py -q --tb=short
.venv/bin/python -m pytest packages/data-core -q
```

---

## 6. Schema Impact

**None.** Artifact lives outside DB.

---

## 7. Git Boundary

**Commit:** `feat(data-core): completeness certification generator`

**Stage manifest:**
- `catalyst_data/certification.py` — new
- `scripts/certify.py` — new
- `packages/data-core/tests/test_certification.py` — new

---

## 8. Architect Decision Gates

| ID | Decision | Required By |
|---|---|---|
| AD-CERT-1 | Certification thresholds and SLAs | W2 implementation |
| AD-FILINGS-1 | Required SEC forms and expected-filing model | W2 filings check |

## 9. Exit Gate

- Certification artifact green (every required check represented)
- Tri-state exhaustiveness
- Repeated certification on identical DB/config → identical content_hash
- `require_certified()` raises `CertificationFailed` on fail
- Read-only proof: DB SHA unchanged before/after
- Full package green


## Database Safety Protocol

1. **Before:** Record Dev DB SHA (`shasum -a 256 data/catalyst_dev_ws4b.db`)
2. **After tests:** Re-record; compare. Dev DB unchanged unless slice owns a migration.
3. **Frozen DB:** SHA must not change. Never opened writable.
4. **Migration tests:** Temp DB copies only. Real Dev DB untouched during implementation.
5. **WAL/SHM:** Check before/after — no new sidecars from read-only access.

## Rollback Strategy

Revert commit. No schema to roll back (if no migration in this slice). No data affected.
No migration. Artifact lives outside DB. Revert commit.

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

1. Regenerate cert and diff against dscodex output
2. Spot-check one ticker-month by direct SQL vs calendar count
3. Verify cert gate blocks export tooling (W7) — W2-owned `require_certified()` contract
4. Byte-for-byte deterministic artifact comparison

## Dependencies

**Prerequisites:** W3-B committed and verified.
**Depended on by:** Phase 2 (W7).

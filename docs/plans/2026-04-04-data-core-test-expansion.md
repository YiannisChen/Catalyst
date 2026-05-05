> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# Data-Core Test Expansion Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Systematically harden `packages/data-core` by expanding tests in the recommended order: per-source failure coverage and malformed payload handling first, live validation matrix second, and concurrency/rate-limit validation last.

**Architecture:** Keep default CI offline and deterministic, using mocked provider responses and SQLite-backed integration tests for the first wave. Add a separate live validation matrix around `scripts/smoke_test.py` for real-provider drift detection, then add targeted concurrency and rate-limit checks as a later integration layer so the core offline suite stays stable.

**Tech Stack:** Python 3.11+ (see `packages/data-core/pyproject.toml`), pytest, pytest-asyncio, sqlite3, httpx, Catalyst `catalyst_data` package, provider fixture data, smoke test script

---

## Canonical offline gate (human-verified)

Run from `packages/data-core` after `pip install -e ".[dev]"` in that package’s `.venv`:

```bash
cd packages/data-core
.venv/bin/python -m pytest tests/ -q --tb=short
```

On a given machine the interpreter may be written as an absolute path, e.g.  
`/path/to/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/ -q --tb=short`.

Use `PYTHONPATH=.` only if imports fail without an editable install.

## Git / review policy

**Agents do not run `git commit` or `git push`.** They may stage files only when the repo owner explicitly allows. The owner (or designated human) reviews diffs, runs the offline gate locally, writes the commit message (Conventional Commits), and merges.

## Implementation status (execution wave)

| Task | Scope | Status |
|------|--------|--------|
| 1 | Baseline mapping + regression discipline | Done |
| 2 | Per-source failure matrix (connectors + orchestrator) | Done |
| 3 | Malformed / missing-field (pipeline + orchestrator) | Done |
| 4 | Live validation matrix doc + `smoke_test --sources` + tests | Done |
| 5 | Rate limiter concurrency / exception release tests | Done (limiter-focused; orchestrator-wide parallel `process_request` stress remains optional backlog) |
| 6 | Human close-out below | Pending owner sign-off |

---

### Task 1: Lock the Phase 1 acceptance baseline

**Files:**
- Reference: `docs/testing/data-core-test-design.md`
- Reference: `packages/data-core/tests/`
- Reference: `packages/data-core/scripts/smoke_test.py`

**Step 1:** Re-read `docs/testing/data-core-test-design.md` and map the remaining gaps into three buckets: offline failure coverage, live validation, and concurrency/pressure validation.

**Step 2:** Record the baseline verification command for all offline work (see **Canonical offline gate** above).

**Step 3:** Treat the current passing suite as the regression baseline and do not weaken existing assertions while expanding coverage.

### Task 2: Expand per-source failure matrix coverage first

**Files:**
- Modify: `packages/data-core/tests/test_polygon_connector.py`
- Modify: `packages/data-core/tests/test_fmp_connector.py`
- Modify: `packages/data-core/tests/test_fred_connector.py`
- Modify: `packages/data-core/tests/test_orchestrator.py`
- Reference: `packages/data-core/catalyst_data/connectors/polygon.py`
- Reference: `packages/data-core/catalyst_data/connectors/fmp.py`
- Reference: `packages/data-core/catalyst_data/connectors/fred.py`
- Reference: `packages/data-core/catalyst_data/orchestrator.py`

**Step 1:** Write one failing test per high-value source path for representative failures the design doc calls out: `429`, `500/502/503/504`, timeout, unsupported endpoint, and partial sibling-endpoint failure where applicable.

**Step 2:** Run only the targeted failing tests and confirm each fails for the intended reason before touching production code.

**Step 3:** Implement the smallest production fixes necessary so connectors and orchestrator return structured, non-crashing summaries instead of dropping context or raising unexpectedly.

**Step 4:** Re-run the targeted source-specific tests until they pass cleanly.

**Step 5:** Re-run the full offline suite with `PYTHONPATH=. python -m pytest tests/ -q` and keep it green before moving on.

### Task 3: Expand malformed and missing-field coverage second

**Files:**
- Modify: `packages/data-core/tests/test_pipeline.py`
- Modify: `packages/data-core/tests/test_orchestrator.py`
- Add or Modify: `packages/data-core/tests/fixtures/` as needed for realistic drift payloads
- Reference: `packages/data-core/catalyst_data/pipeline/ingest.py`
- Reference: `packages/data-core/catalyst_data/pipeline/clean.py`
- Reference: `packages/data-core/catalyst_data/pipeline/transform.py`

**Step 1:** Write failing tests for realistic provider drift cases, prioritizing payloads that would silently produce wrong outputs or crash the pipeline.

**Step 2:** Cover at least these scenarios with clear assertions: news list vs single-object shape drift, missing `title`, missing `published_utc`, missing URL/source fields, fundamentals endpoint returning empty list, wrong scalar type, or partial dict.

**Step 3:** Run the new targeted tests and verify the failures are due to missing resilience rather than bad test setup.

**Step 4:** Make only the minimal pipeline changes needed so malformed but recoverable payloads degrade predictably, while truly unusable payloads still surface structured errors.

**Step 5:** Re-run the malformed-payload tests, then re-run `PYTHONPATH=. python -m pytest tests/ -q`.

### Task 4: Formalize the live validation matrix after offline resilience is stable

**Files:**
- Modify: `docs/testing/data-core-test-design.md`
- Modify: `docs/API_Documentation/fmp_api.md` only if live learnings change provider guidance
- Modify: `packages/data-core/scripts/smoke_test.py` only if small usability changes are needed for repeatable matrix runs
- Create or Modify: `docs/testing/` live-validation checklist document if needed

**Step 1:** Define a small fixed live matrix of ticker/date/source combinations that represent the real-world cases most likely to drift.

**Step 2:** Include at least one quiet-news day, one news-heavy day, one earnings or fundamentals-relevant day, and one macro-sensitive day.

**Step 3:** Define the exact operator checklist for each live run: command used, keys/providers enabled, Bronze row created, Silver row created, source attribution preserved, URLs preserved, and failure summaries recorded when a provider degrades.

**Step 4:** Keep this matrix out of default CI and document it as manual or scheduled validation only.

**Step 5:** If the current smoke script lacks one small affordance needed for repeatable runs, add it under TDD and re-verify both the targeted script behavior and the full offline suite.

### Task 5: Add concurrency and rate-limit validation last

**Files:**
- Modify: `packages/data-core/tests/test_rate_limiter.py`
- Modify: `packages/data-core/tests/test_orchestrator.py`
- Possibly Add: `packages/data-core/tests/test_integration_concurrency.py`
- Reference: `packages/data-core/catalyst_data/rate_limiter.py`
- Reference: `packages/data-core/catalyst_data/orchestrator.py`

**Step 1:** Write failing tests that simulate the smallest high-value pressure cases first, rather than broad stress tests.

**Step 2:** Prioritize these checks: concurrency cap actually limits in-flight work, rate-limit classification stays structured, and one slow or failing request does not corrupt sibling source summaries.

**Step 3:** Keep these tests offline and deterministic when possible; if a heavier scenario is needed, mark it clearly as non-default integration coverage.

**Step 4:** Make minimal production changes only if the new pressure tests expose real correctness bugs.

**Step 5:** Re-run the targeted pressure tests and then `PYTHONPATH=. python -m pytest tests/ -q`.

### Task 6: Close out with explicit acceptance criteria

**Files:**
- Reference: `docs/testing/data-core-test-design.md`
- Reference: `docs/plans/2026-04-04-data-core-test-expansion.md`

**Owner checklist (human sign-off — do not skip):**

- [ ] **Offline:** Run the **Canonical offline gate**; record `N passed` and exit code 0 (expected **132+** as of last agent report; re-run after any local edits).
- [ ] **Live docs:** `docs/testing/data-core-live-validation-matrix.md` is usable without oral context (matrix, checklist, SQL hints, non-CI statement).
- [ ] **Scope:** Changes improve resilience and observability without freezing every intermediate pipeline stage as a permanent product API.
- [ ] **Debt logged:** `packages/data-core/catalyst_data/models.py` vs Bronze/Silver in `storage/sqlite.py` remains an explicit **Phase 1.5** item in `data-core-test-design.md` §4 — not closed by this expansion.

**Step 1:** Confirm offline acceptance using the canonical command (not `PYTHONPATH=.` unless editable install is absent).

**Step 2:** Confirm documentation acceptance: the live validation matrix and operator checklist exist and are understandable without oral context.

**Step 3:** Confirm scope discipline: the work improved resilience and observability without turning every intermediate pipeline operation into a persistent product contract.

**Step 4:** Track the remaining known non-test debt separately, especially the `models.py` vs Bronze/Silver storage contract mismatch.

---

## Solo close-out (personal repo — no PR required)

A *pull request* is only for hosted workflows where another person reviews before merge. For a personal project, **finish with local review → `git commit` → `git push` if you want a remote backup**; merge branches with `git merge` as you prefer.

Optional notes for a commit message or journal:

- Offline: `cd packages/data-core && .venv/bin/python -m pytest tests/ -q --tb=short` → N passed.
- Live: `docs/testing/data-core-live-validation-matrix.md` (manual only).
- Follow-ups: optional orchestrator parallel stress tests; `models.py` vs Bronze/Silver (Phase 1.5).

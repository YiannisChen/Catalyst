# W3-A: Data-Core Test Environment Baseline — Implementation Plan

**Role:** Plan authoring (dscodex). No implementation in this document.
**Branch:** ws4b/article-level-data
**Date:** 2026-07-11
**Governs:** Roadmap workstream W3-A (docs/plans/2026-07-09-backend-first-roadmap.md, §B Phase 1, §D).
**Serialized prerequisite for:** W1-A through W2.
**Process rule:** Per CLAUDE.md, this plan contains no implementation code.
Exact shell commands are allowed. Config snippets are included only when a
task genuinely requires a config edit.

---

## Goal

Establish a trustworthy canonical data-core test baseline before W1 changes
runtime behavior. Fix one environment defect: install the declared
`pytest-asyncio` dev dependency so that async tests execute rather than
failing with plugin-absence errors. Produce the known-failure manifest
(`docs/testing/data-core-failure-groups.md`) that every subsequent W1 slice's
full-suite run is diffed against.

---

## Architecture / Approach

The root `.venv` has `pytest` 9.1.1 but not `pytest-asyncio`. The
`packages/data-core/pyproject.toml` already correctly declares
`pytest-asyncio>=0.23` as a `[dev]` optional dependency and sets
`asyncio_mode = "auto"`. The defect is purely that the dev extra was never
installed.

The fix: install the declared dev extra. After installation, the 83 currently
failing async tests will execute. Their outcomes (pass, fail with real
assertion, or fail with new errors) are unknown until post-install execution.
Every post-install failure must be recollected and assigned new root-cause
group IDs.

No production runtime code changes. No assertion changes. No skip/xfail
markers added. No `pyproject.toml` changes unless a genuine post-plugin
compatibility defect is demonstrated.

---

## Scope

**In scope:**
- Install the data-core `[dev]` extra into `.venv` using the package's
  declared dependency specification.
- Update repository setup guidance in README.md, packages/data-core/README.md,
  and conftest.py to include the dev extra.
- Confirm plugin loading with targeted smoke tests.
- Re-run canonical suite; collect actual post-plugin counts.
- Mark G1 resolved if plugin-absence signatures disappear.
- Create new root-cause groups for every remaining failure.
- Refresh `docs/testing/data-core-failure-groups.md` with post-fix state.

**Out of scope:**
- Any change under `packages/data-core/catalyst_data/`.
- Any production runtime behavior change.
- Fixing regression or contract-drift failures (W3-B).
- Touching agents, eval, or app packages.
- Touching frozen DB or Dev DB (read-only access only).
- Provider/API network calls; live ingestion; API key usage.
- Adding `pytest-asyncio` as a hard (non-optional) production dependency.
- Adding manual `markers` entries to `pyproject.toml`.

---

## Verified Current State

### Canonical test result (2026-07-11)

```
.venv/bin/python -m pytest packages/data-core -q
```

- **620 passed**
- **83 failed**
- **1 skipped**
- **1 xfailed**
- **705 collected**

### Environment

| Item | Value |
|---|---|
| Python | 3.13.2 |
| pytest | 9.1.1 |
| pytest-asyncio | NOT INSTALLED |
| `.venv` location | `/Users/yiannischen/Desktop/Catalyst/.venv` |
| editable installs | `catalyst-data`, `catalyst-agents`, `catalyst-app`, `catalyst-eval` all installed |

### Pre-existing declarations (correct; no changes needed)

- `packages/data-core/pyproject.toml`:
  - `[project.optional-dependencies] dev = ["pytest-asyncio>=0.23"]` ✓
  - `[tool.pytest.ini_options] asyncio_mode = "auto"` ✓
- `pytest-asyncio` self-registers its `asyncio` marker on load; no manual
  marker registration is needed.

### Blocking-signature root-cause group (pre-fix)

One group documented in `docs/testing/data-core-failure-groups.md`:

| Group | Cohorts | Count | Blocking signature |
|---|---|---|---|
| G1 | U (undecorated): 23, D (decorated): 60 | **83** | `pytest-asyncio` not installed; all `async def test_*` fail with "async def functions are not natively supported" |

G1 captures the current top-level failure signature only. It does NOT claim
all 83 tests will pass after installation. Underlying outcomes are unknown
until post-install execution.

---

## Exact Files to Inspect

These were inspected during plan authoring; no further inspection needed:

- `packages/data-core/pyproject.toml` — pytest config, dependency declarations
- `conftest.py` — repo-root conftest (editable-install guard + `_FIX_CMD`)
- `README.md` — repo Quick Start setup guidance
- `packages/data-core/README.md` — data-core Installation section
- `packages/data-core/tests/` — all 70 test files enumerated
- `.venv/lib/python3.13/site-packages/` — confirmed `pytest-asyncio` absent

---

## Exact Files Likely to Modify

| File | Change | Reason |
|---|---|---|
| `README.md` | Update Quick Start: replace `pip install -e packages/data-core` with `pip install -e "packages/data-core[dev]"` | Reproducible development setup includes the dev extra |
| `packages/data-core/README.md` | Add `pip install -e "packages/data-core[dev]"` to Installation section alongside existing `[vector]` example | Developers and CI need the dev extra for test execution |
| `conftest.py` | Update `_FIX_CMD` to include `[dev]` extra: `.venv/bin/pip install -e "packages/data-core[dev]" -e packages/agents -e packages/app -e packages/eval` | The stale-editable-install fix command must match the canonical setup |
| `docs/testing/data-core-failure-groups.md` | Update with post-install state: mark G1 resolved or split; add new groups for any remaining failures | The failure manifest is the W3-A deliverable |

Files that should NOT change:

- `packages/data-core/pyproject.toml` — correct as-is; no marker registration needed
- `packages/data-core/tests/**` — no test content changes
- `packages/data-core/catalyst_data/**` — no production runtime changes
- `data/catalyst_dev_ws4b.db` — no DB mutation
- `data/catalyst_eval_frozen_v2.db` — no DB mutation
- `packages/agents/**`, `packages/app/**`, `packages/eval/**` — out of scope

---

## Forbidden Files

- `packages/data-core/catalyst_data/**` — no production runtime changes
- `packages/data-core/tests/**` — no test content changes
- `data/catalyst_dev_ws4b.db` — no DB mutation (read-only)
- `data/catalyst_eval_frozen_v2.db` — no DB mutation (read-only)
- `packages/agents/**`, `packages/app/**`, `packages/eval/**` — out of scope

---

## Network Scope

W3-A makes no provider/API calls, no live ingestion, and uses no API keys.
Package-index access is allowed only for installing the declared dev
dependency if the wheel is not already available in the local pip cache.
Record whether installation used cache or network in the handoff report.
Never print environment variables.

---

## Step-by-Step Tasks

### Task 1 — Capture pre-fix baseline

```
cd /Users/yiannischen/Desktop/Catalyst

# Git status
git status --short

# Installed pytest/plugin versions
.venv/bin/pip show pytest pytest-asyncio 2>&1 || true

# DB SHAs (dynamic — record, don't hardcode)
shasum -a 256 data/catalyst_dev_ws4b.db
shasum -a 256 data/catalyst_eval_frozen_v2.db

# Canonical pre-fix counts
.venv/bin/python -m pytest packages/data-core -q
```

Confirm pytest-asyncio is absent from `pip show` output.

### Task 2 — Install the declared data-core dev extra

```
.venv/bin/pip install -e "packages/data-core[dev]"
```

This installs `pytest-asyncio>=0.23` through the package's declared
`[project.optional-dependencies] dev` specification. It does NOT install an
undeclared standalone package. Record whether installation used cached wheels
or fetched from the package index.

### Task 3 — Confirm plugin loading

```
# Verify package installed via pip
.venv/bin/pip show pytest-asyncio | rg "^Name:|^Version:"

# Prove pytest-asyncio is loaded as a plugin. Capture pytest output without a
# pipeline so a pytest collection/configuration failure cannot be hidden.
if ! .venv/bin/python -m pytest \
  packages/data-core/tests/test_retry.py \
  --trace-config --collect-only -q \
  > /tmp/w3a_plugin_trace.txt 2>&1; then
  cat /tmp/w3a_plugin_trace.txt
  exit 1
fi
if ! rg -q "pytest_asyncio" /tmp/w3a_plugin_trace.txt; then
  cat /tmp/w3a_plugin_trace.txt
  echo "FAIL: pytest_asyncio is installed but not registered as a pytest plugin"
  exit 1
fi

# Prove NO config warnings or unknown-marker warnings exist.
# Scan the already captured full stderr+stdout output and fail explicitly if
# either warning is present.
if rg -q "Unknown config option: asyncio_mode|PytestUnknownMarkWarning" /tmp/w3a_plugin_trace.txt; then
  cat /tmp/w3a_plugin_trace.txt
  echo "FAIL: pytest async configuration or marker warning detected"
  exit 1
fi

# Smoke: one undecorated async test (cohort U)
.venv/bin/python -m pytest packages/data-core/tests/test_update_pipeline.py::TestRunUpdateBatchReal::test_mocked_fetch -q --tb=short

# Smoke: one decorated async test (cohort D)
.venv/bin/python -m pytest packages/data-core/tests/test_retry.py::test_with_retry_429_then_success -q --tb=short
```

Acceptance:
- `pip show` + `rg` confirms `pytest-asyncio` installed with version ≥ 0.23.
- The captured `pytest --trace-config --collect-only` command exits 0 and
  prints a line for `pytest_asyncio` (proves
  the plugin is actually loaded, not just installed).
- The explicit warning scan exits 0 — proving neither "Unknown config option:
  asyncio_mode" nor "PytestUnknownMarkWarning" appears in collected output.
- Smoke tests do NOT fail with "async def functions are not natively supported".
- Decorated smoke test does NOT produce `PytestUnknownMarkWarning` for `asyncio`.

If `PytestUnknownMarkWarning` persists after confirmed plugin loading, stop
and diagnose plugin/version/configuration loading. Do NOT immediately add a
manual `markers` entry to `pyproject.toml`.

### Task 4 — Re-run the focused 83-test cohort

```
.venv/bin/python -m pytest \
  packages/data-core/tests/test_update_pipeline.py \
  packages/data-core/tests/test_3f2_finnhub_live.py \
  packages/data-core/tests/test_3f2_fred_live.py \
  packages/data-core/tests/test_3f2_integration.py \
  packages/data-core/tests/test_3f2_polygon_live.py \
  packages/data-core/tests/test_3f2_sec_live.py \
  packages/data-core/tests/test_retry.py \
  packages/data-core/tests/test_rate_limiter.py \
  packages/data-core/tests/test_sec_connector.py \
  packages/data-core/tests/test_polygon_connector.py \
  packages/data-core/tests/test_finnhub_connector.py \
  packages/data-core/tests/test_fred_connector.py \
  packages/data-core/tests/test_fmp_connector.py \
  packages/data-core/tests/test_orchestrator.py \
  packages/data-core/tests/test_live_runner.py \
  packages/data-core/tests/test_smoke_test.py \
  packages/data-core/tests/test_phase0_remediate.py \
  -q
```

Record the actual pass/fail counts for this 83-test cohort. Do NOT assume
all will pass.

### Task 5 — Re-run full canonical suite

```
.venv/bin/python -m pytest packages/data-core -q
```

Record actual pass/fail/skip/xfail counts. Do NOT predict green.

### Task 6 — Collect actual post-plugin failures

From Task 5 output, extract every `FAILED` line. These are the actual
post-plugin failures.

### Task 7 — Mark G1 resolved or split

- If the "async def functions are not natively supported" signature
  disappears from all 83 tests: mark G1 as **resolved**.
- Any remaining failures must be assigned new root-cause group IDs
  (G2, G3, …) based on their actual post-plugin failure signatures.
- Update `docs/testing/data-core-failure-groups.md`:
  - Record G1 resolution status.
  - Add new groups for every remaining failure.
  - Verify: `sum(new group counts) == actual post-plugin failure count`.

### Task 8 — Update setup guidance

**README.md (repo root):**

In the Quick Start > "1. Clone and install" section, replace:

```
pip install -e packages/data-core
pip install -e packages/eval
pip install -e packages/agents
pip install -e packages/app
```

with:

```
pip install -e "packages/data-core[dev]"
pip install -e packages/eval
pip install -e packages/agents
pip install -e packages/app
```

**packages/data-core/README.md:**

In the Installation section, add after the existing core install line:

```
# Core with development dependencies (pytest, pytest-asyncio)
pip install -e "packages/data-core[dev]"
```

Keep the existing `[vector]` example.

**conftest.py:**

Update `_FIX_CMD` from:

```
_FIX_CMD = (
    ".venv/bin/pip install -e packages/data-core "
    "-e packages/agents -e packages/app -e packages/eval"
)
```

to:

```
_FIX_CMD = (
    ".venv/bin/pip install -e \"packages/data-core[dev]\" "
    "-e packages/agents -e packages/app -e packages/eval"
)
```

### Task 9 — Verify no production runtime files changed

```
# Explicitly fail if any catalyst_data/ file appears in diff
test -z "$(git diff --name-only | rg catalyst_data/)" || { echo "FAIL: catalyst_data/ files changed"; exit 1; }
```

Must exit 0 (no match found).

### Task 10 — Verify no test content changed

```
git diff --name-only packages/data-core/tests/
```

Must produce empty output.

### Task 11 — Verify DB integrity

```
# Record post-run SHAs
shasum -a 256 data/catalyst_dev_ws4b.db
shasum -a 256 data/catalyst_eval_frozen_v2.db
```

Require: Dev DB SHA before == Dev DB SHA after.
Require: frozen DB SHA before == frozen DB SHA after.

Neither DB was opened writable during W3-A.

### Task 12 — Verify no provider artifacts changed

```
git diff --stat data/provider_*
```

Must produce empty output.

### Task 13 — Report

Produce the handoff report (see Handoff Report Format below).

---

## Expected Evidence

1. Pre-fix baseline: git status, pip show output, DB SHAs, canonical counts.
2. `pip show pytest-asyncio` confirming version ≥ 0.23.
3. Smoke test output for one undecorated and one decorated async test.
4. Focused 83-test cohort pass/fail counts.
5. Full canonical run output with exact pass/fail/skip/xfail counts.
6. Updated `docs/testing/data-core-failure-groups.md` reflecting post-fix state.
7. `git diff --stat` showing only `README.md`, `packages/data-core/README.md`,
   `conftest.py`, and `docs/testing/data-core-failure-groups.md` changed.
8. `test -z "$(git diff --name-only | rg catalyst_data/)"` exiting 0 (no match).
9. `git diff --name-only packages/data-core/tests/` producing empty output.
10. Before/after DB SHA comparison showing equality.
11. `pip install` output showing whether cache or network was used.

---

## Failure Handling

| Condition | Action |
|---|---|
| `pip install` fails (network, version conflict) | Record error. W3-A blocked until environment resolution. Report to orchestrator. |
| `PytestUnknownMarkWarning` persists after confirmed plugin load | Stop. Diagnose plugin/version/configuration path. Do NOT add manual marker registration. Report findings. |
| Async-plugin signatures gone; some tests fail with real assertions | Expected. Assign new root-cause group IDs. This is the known-failure manifest for W1. |
| New tests fail that previously passed | Unexpected. Classify as regression; flag for immediate triage. |
| `pyproject.toml` shows a genuine post-plugin compatibility defect | Report separately. Do NOT modify in W3-A without orchestrator approval. |

---

## Landmines

1. **Do not predict green.** The plan must not claim all 83 will pass. The
   only provable fact is the blocking signature. Real outcomes emerge from
   execution.

2. **Install through the declared extra, not standalone.**
   `.venv/bin/pip install -e "packages/data-core[dev]"` — not
   `.venv/bin/pip install pytest-asyncio`. The extra is the canonical
   specification of what data-core needs for development.

3. **`pytest-asyncio` self-registers its `asyncio` marker.** No manual
   `markers = [...]` entry is needed in `pyproject.toml`. The current
   `PytestUnknownMarkWarning` is expected because the plugin is absent.

4. **`asyncio_mode = "auto"` is correct.** It auto-detects `async def
   test_*` functions without requiring `@pytest.mark.asyncio` on every test.
   The 60 decorated tests and 23 undecorated tests both work under `auto`.

5. **Do not create a package-level `conftest.py`.** The root `conftest.py`
   handles editable-install verification. No `packages/data-core/tests/conftest.py`
   is needed.

6. **Do not add `pytest-asyncio` as a hard production dependency.** Keep it
   in `[project.optional-dependencies] dev`. The existing declaration
   is correct.

7. **DB SHAs are dynamic.** Record before values at execution time, not from
   the plan document. Compare before vs after. The plan document's cited
   SHAs are informational only and may be stale.

8. **No network means no provider/API calls.** Package-index access for
   `pip install` of declared dependencies is allowed and must be recorded.

---

## Orchestrator Verification

The orchestrator must independently:

1. **Rerun canonical suite from scratch:**
   ```
   .venv/bin/python -m pytest packages/data-core -q
   ```
   Compare counts against the worker's post-fix report.

2. **Inspect `git diff` for touched-file accuracy:**
   ```
   git diff --stat
   test -z "$(git diff --name-only packages/data-core/tests/)" || { echo "FAIL: test files changed"; exit 1; }
   test -z "$(git diff --name-only | rg catalyst_data/)" || { echo "FAIL: catalyst_data/ files changed"; exit 1; }
   ```
   Confirm only `README.md`, `packages/data-core/README.md`, `conftest.py`,
   and `docs/testing/data-core-failure-groups.md` changed. Confirm zero
   `catalyst_data/` and `tests/` diffs.

3. **Verify no assertion weakening or new skips:**
   Inspect any test-file diffs for weakened assertions, widened tolerances,
   or added skip/xfail markers.

4. **Verify every remaining failure appears in the failure-group manifest:**
   Take each `FAILED` line from the canonical output. Confirm it appears in
   exactly one group in `docs/testing/data-core-failure-groups.md`.
   Confirm `sum(group counts) == actual failure count`.

5. **Choose 3 random groups and reproduce signatures:**
   For each group, reproduce one affected test's failure and confirm the
   signature matches the group description.

6. **Verify DB integrity:**
   ```
   shasum -a 256 data/catalyst_dev_ws4b.db
   shasum -a 256 data/catalyst_eval_frozen_v2.db
   ```
   Compare against the pre-fix baseline recorded in the worker's report.
   Both must match.

7. **Verify no provider artifacts changed:**
   ```
   git diff --stat data/provider_*
   ```
   Must produce empty output.

8. **Verify `pytest-asyncio` is installed from the dev extra:**
   ```
   .venv/bin/pip show pytest-asyncio
   ```
   Version must be ≥ 0.23.

9. **Verify setup guidance is consistent:**
   Read `README.md` Quick Start, `packages/data-core/README.md` Installation,
   and `conftest.py` `_FIX_CMD`. Confirm all three reference
   `packages/data-core[dev]`.

---

## Commit Boundary

**Commit message:** `test(data-core): fix environment test baseline, install dev extra`

**Contains:**
- `README.md` — Quick Start now installs `data-core[dev]`
- `packages/data-core/README.md` — Installation section documents `[dev]` extra
- `conftest.py` — `_FIX_CMD` includes `[dev]`
- `docs/testing/data-core-failure-groups.md` — created with pre-fix G1;
  updated with post-fix groups and resolution status

**Does NOT contain:**
- `packages/data-core/pyproject.toml` (unchanged unless a genuine post-plugin
  defect is demonstrated and separately reported)
- `.venv/` changes (environment side effects, not Git artifacts)

**Staging only; human commits.**

---

## Handoff Report Format

After W3-A completion, report:

1. Pre-fix baseline: git status, pytest/asyncio versions, DB SHAs, canonical
   counts (620/83/1/1).
2. `pip install` output: whether cache or network was used.
3. `pip show pytest-asyncio` version.
4. Smoke test results (one undecorated, one decorated).
5. Focused 83-test cohort pass/fail counts.
6. Post-fix canonical counts: `X passed, Y failed, Z skipped, W xfailed`.
7. G1 resolution status: resolved / partially resolved / not resolved.
8. Any new root-cause groups with IDs, signatures, affected test IDs, and
   counts.
9. Known-failure manifest (list of remaining failing test IDs, if any).
10. `git diff --stat` output.
11. Before/after DB SHA comparison.
12. Any unexpected findings.

> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# Eval Module Audit Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Objectively check whether `packages/eval` is correct, coherent with the system spec, and ready to serve as the Phase 2 evaluation foundation without relying on optimistic assumptions.

**Architecture:** Audit `packages/eval` in layers: package/install baseline, schema contract correctness, metric behavior, harness/report generation, golden set integrity, and spec alignment. Prefer verification commands and targeted negative tests over code reading alone. If a review step exposes a correctness bug, add or update a test first, then make the smallest fix needed.

**Tech Stack:** Python 3.11+, pytest, Pydantic, JSONL golden set, Catalyst `catalyst_eval` package

---

### Task 1: Establish the evaluation baseline

**Files:**
- Reference: `packages/eval/pyproject.toml`
- Reference: `packages/eval/tests/`
- Reference: `packages/eval/catalyst_eval/__init__.py`

**Step 1:** Inspect `packages/eval/pyproject.toml` and confirm package name, dependencies, pytest config, and Python version are internally consistent.

**Step 2:** Run the package test baseline from `packages/eval` with the project’s intended Python environment.

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
python -m pytest tests/ -q --tb=short
```

Expected:

- tests collect successfully
- suite exits `0`

**Step 3:** Confirm the package is importable as `catalyst_eval` from the package directory.

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
python -c "import catalyst_eval; print(catalyst_eval.__file__)"
```

Expected:

- import succeeds
- printed path resolves inside `packages/eval/catalyst_eval`

### Task 2: Audit schema contracts against the system spec

**Files:**
- Reference: `packages/eval/catalyst_eval/schema/golden_event.py`
- Reference: `packages/eval/catalyst_eval/schema/result.py`
- Reference: `packages/eval/tests/test_schema.py`
- Reference: `docs/superpowers/specs/2026-04-02-catalyst-system-design.md`

**Step 1:** Compare the implemented schema objects against the spec’s intended contracts for `GoldenEvent`, `Cause`, `AttributionResult`, and related enums or nested structures.

**Step 2:** Verify that required fields, optional fields, validation rules, and naming conventions do not silently drift from the spec in ways that would break downstream evaluation.

**Step 3:** Add targeted schema tests only if a real contract gap is found, and make the smallest correction necessary.

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
python -m pytest tests/test_schema.py -q --tb=short
```

Expected:

- schema tests pass
- any new tests fail before a fix if a contract mismatch is real

### Task 3: Audit metric correctness, not just existence

**Files:**
- Reference: `packages/eval/catalyst_eval/metrics/base.py`
- Reference: `packages/eval/catalyst_eval/metrics/attribution_f1.py`
- Reference: `packages/eval/catalyst_eval/metrics/category_accuracy.py`
- Reference: `packages/eval/catalyst_eval/metrics/grounding_rate.py`
- Reference: `packages/eval/catalyst_eval/metrics/temporal_precision.py`
- Reference: `packages/eval/catalyst_eval/metrics/confidence_calibration.py`
- Reference: `packages/eval/tests/test_metrics.py`

**Step 1:** Review each metric for three things:

- exact input contract
- scoring behavior on edge cases
- whether the implementation actually matches the spec description

**Step 2:** Check for high-risk mistakes:

- division by zero or empty-list behavior
- silent success on malformed predictions
- confidence bucket logic that produces misleading calibration output
- grounding logic that rewards unsupported summaries
- temporal logic that ignores anchor mismatches

**Step 3:** Run the metric test suite and identify whether current tests prove real behavior or only superficial happy paths.

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
python -m pytest tests/test_metrics.py -q --tb=short
```

Expected:

- metric tests pass
- any discovered correctness gap gets a failing regression test first

### Task 4: Audit harness and experiment behavior

**Files:**
- Reference: `packages/eval/catalyst_eval/harness/runner.py`
- Reference: `packages/eval/catalyst_eval/harness/experiment.py`
- Reference: `packages/eval/catalyst_eval/reports/markdown.py`
- Reference: `packages/eval/tests/test_harness.py`

**Step 1:** Verify the harness can execute a simple deterministic `predict_fn` across the golden set and aggregate metric outputs correctly.

**Step 2:** Verify experiment comparison logic preserves run labels, metric outputs, and ordering without cross-run contamination.

**Step 3:** Verify report generation is deterministic enough for review and does not silently omit failed metric results or empty datasets.

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
python -m pytest tests/test_harness.py -q --tb=short
```

Expected:

- harness tests pass
- deterministic dummy predictions produce stable reports and comparison outputs

### Task 5: Audit the golden set as data, not just a file

**Files:**
- Reference: `packages/eval/golden_set/v1.jsonl`
- Reference: `packages/eval/golden_set/README.md`
- Reference: `packages/eval/catalyst_eval/schema/golden_event.py`

**Step 1:** Load the JSONL golden set and validate every row through the implemented schema.

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
python - <<'PY'
from pathlib import Path
import json
from catalyst_eval.schema.golden_event import GoldenEvent

path = Path("golden_set/v1.jsonl")
rows = [GoldenEvent.model_validate(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
print(len(rows))
PY
```

Expected:

- file loads cleanly
- every line validates
- printed count matches expectations in `golden_set/README.md`

**Step 2:** Check for data-quality risks:

- duplicate event ids
- duplicate `(ticker, trade_date)` pairs without explanation
- invalid cause weights
- impossible category values
- missing evidence references where the metric design expects them

**Step 3:** If data issues are found, document whether the problem is schema-level, dataset-level, or spec-level before changing anything.

### Task 6: Check spec alignment and explicitly list deviations

**Files:**
- Reference: `docs/superpowers/specs/2026-04-02-catalyst-system-design.md`
- Reference: `packages/eval/catalyst_eval/`
- Reference: `packages/eval/golden_set/`

**Step 1:** Compare the implemented module against the spec’s declared evaluation scope:

- agent-agnostic harness
- five core metrics
- golden set support
- experiment comparison
- report generation

**Step 2:** Produce an explicit list of any deviations:

- intentional simplifications
- missing features
- naming mismatches
- riskier-than-specified shortcuts

**Step 3:** Separate “acceptable Phase 2 simplification” from “actual correctness risk” so the owner can decide what blocks evaluation work.

### Task 7: Produce an objective audit outcome

**Files:**
- Reference: `docs/plans/2026-04-06-eval-module-audit.md`
- Reference: `packages/eval/tests/`
- Reference: `packages/eval/catalyst_eval/`

**Step 1:** Summarize what is verified and trustworthy about `packages/eval`.

**Step 2:** List concrete findings by severity if any bugs, spec mismatches, or weak tests are discovered.

**Step 3:** Distinguish between:

- must-fix before relying on `eval`
- acceptable to carry while Phase 2 starts
- optional cleanup

**Step 4:** State a clear verdict:

- ready for Phase 2 usage as-is
- ready with caveats
- not ready until specific blockers are fixed

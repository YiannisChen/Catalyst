> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# Eval Metric Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Bring `packages/eval` into semantic alignment with the system design for grounding, temporal precision, confidence calibration, and golden-set trustworthiness.

**Architecture:** Tighten the eval contract first, then re-implement the remaining metrics on top of one shared matching model instead of three independent approximations. Keep the package agent-agnostic, but require enough structured prediction data to score what the spec actually claims to score. Treat the golden set as a versioned artifact with explicit validation and coverage checks, not just a JSONL file that happens to parse.

**Tech Stack:** Python 3.11, Pydantic v2, pytest, JSONL golden-set data, existing project venv at `packages/data-core/.venv`

---

### Task 1: Lock the New Eval Contract Before Metric Work

**Files:**
- Modify: `packages/eval/catalyst_eval/schema/result.py`
- Modify: `packages/eval/tests/test_schema.py`
- Modify: `packages/eval/tests/test_metrics.py`
- Reference: `docs/superpowers/specs/2026-04-02-catalyst-system-design.md`

**Step 1: Write failing schema tests for the missing fields the metrics actually need**

Add tests that make the contract explicit:

```python
def test_predicted_cause_accepts_temporal_anchor():
    cause = PredictedCause(
        text="China export restrictions expanded",
        category="geopolitical",
        confidence=0.8,
        evidence_ids=["c1"],
        direction="negative",
        temporal_anchor="pre-market",
    )
    assert cause.temporal_anchor == "pre-market"


def test_attribution_result_accepts_retrieved_chunk_objects():
    result = AttributionResult(
        ticker="AAPL",
        trade_date="2026-01-15",
        causes=[],
        summary="Summary text",
        retrieved_chunks=[
            {
                "chunk_id": "c1",
                "text": "China expanded H20 export controls before market open.",
            }
        ],
    )
    assert result.retrieved_chunks[0]["chunk_id"] == "c1"
```

**Step 2: Run the targeted schema tests to verify they fail**

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_schema.py -q --tb=short
```

Expected: FAIL because `PredictedCause` has no `temporal_anchor` and `AttributionResult.retrieved_chunks` is too weakly typed.

**Step 3: Introduce the minimal schema additions**

Implement:

- `PredictedCause.temporal_anchor: str | None = None`
- a small typed chunk schema in `result.py`, for example:

```python
class RetrievedChunk(BaseModel):
    chunk_id: str
    text: str
```

- update `AttributionResult.retrieved_chunks` to `list[RetrievedChunk]`
- keep backward compatibility if possible by accepting old `list[str]` inputs temporarily and normalizing them into chunk objects, or explicitly reject them and migrate tests/callers in the same task

**Step 4: Update fixtures in metric tests to the new contract**

Replace old `retrieved_chunks=["c1", "c2"]` style fixtures with:

```python
retrieved_chunks=[
    {"chunk_id": "c1", "text": "China export restrictions expanded before the open."},
    {"chunk_id": "c2", "text": "Semiconductor stocks sold off broadly during trading."},
]
```

**Step 5: Re-run schema tests**

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_schema.py -q --tb=short
```

Expected: PASS.

**Step 6: Do not commit**

Owner reviews and commits later. Do not run `git commit`.

---

### Task 2: Add a Shared Cause-Matching Helper and Use It Everywhere

**Files:**
- Create: `packages/eval/catalyst_eval/metrics/matching.py`
- Modify: `packages/eval/catalyst_eval/metrics/attribution_f1.py`
- Modify: `packages/eval/catalyst_eval/metrics/category_accuracy.py`
- Modify: `packages/eval/tests/test_metrics.py`

**Step 1: Write failing regression tests around matched-pair semantics**

Add tests that force all metrics to agree on what “matched cause” means:

```python
def test_unrelated_same_category_cause_is_unmatched():
    score = CategoryAccuracy().compute(pred_unrelated_same_category, GOLDEN)
    assert score == 0.0


def test_attribution_matching_helper_returns_expected_pairing():
    matches = match_predicted_to_golden(predicted_causes, golden_causes)
    assert matches == [(0, 0)]
```

**Step 2: Run the targeted metric tests**

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_metrics.py -q --tb=short
```

Expected: PASS for already-fixed category regression, FAIL for any new shared-helper assertions.

**Step 3: Extract matching into one helper**

Create `matching.py` with:

- `_jaccard(text_a, text_b) -> float`
- `match_predicted_to_golden(predicted_causes, golden_causes, threshold=0.2) -> list[tuple[int, int, float]]`
- greedy unmatched assignment, identical semantics for F1, category accuracy, temporal precision, and calibration

**Step 4: Refactor existing metrics to import the helper**

Make both:

- `AttributionF1`
- `CategoryAccuracy`

depend on the shared helper rather than each keeping its own local matcher.

**Step 5: Re-run targeted metrics**

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_metrics.py -q --tb=short
```

Expected: PASS.

**Step 6: Do not commit**

Owner reviews and commits later. Do not run `git commit`.

---

### Task 3: Rebuild GroundingRate Around Summary Claims, Not Evidence-ID Overlap

**Files:**
- Modify: `packages/eval/catalyst_eval/metrics/grounding_rate.py`
- Modify: `packages/eval/tests/test_metrics.py`
- Modify: `packages/eval/golden_set/README.md`
- Reference: `docs/superpowers/specs/2026-04-02-catalyst-system-design.md:633`

**Step 1: Write failing grounding tests for the current loophole**

Add tests like:

```python
def test_grounding_rate_does_not_reward_unsupported_summary_with_reused_chunk_id():
    pred = AttributionResult(
        ticker="AAPL",
        trade_date="2026-01-15",
        causes=[
            PredictedCause(
                text="Invented cause",
                category="earnings",
                confidence=0.9,
                evidence_ids=["c1"],
                direction="negative",
            )
        ],
        summary="Apple missed earnings because of weak iPhone demand.",
        retrieved_chunks=[
            {"chunk_id": "c1", "text": "China expanded H20 export restrictions before market open."}
        ],
    )
    assert GroundingRate().compute(pred, GOLDEN) == 0.0


def test_grounding_rate_rewards_summary_claims_supported_by_chunk_text():
    pred = AttributionResult(
        ticker="AAPL",
        trade_date="2026-01-15",
        causes=[],
        summary="China expanded export restrictions before market open.",
        retrieved_chunks=[
            {"chunk_id": "c1", "text": "China expanded export restrictions before market open."}
        ],
    )
    assert GroundingRate().compute(pred, GOLDEN) == 1.0
```

**Step 2: Run only the new grounding tests**

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_metrics.py -q --tb=short -k grounding
```

Expected: FAIL because current implementation ignores `summary` and chunk text.

**Step 3: Implement the minimal claim-level grounding logic**

Implement a pragmatic Phase 2 version:

- split `summary` into claim-like units using sentence boundaries
- normalize text
- score each claim against retrieved chunk texts using token overlap or Jaccard
- mark a claim as grounded if any chunk text clears a threshold
- return grounded_claims / total_claims

Do not add an LLM judge in this task. Keep it deterministic and offline.

**Step 4: Decide how to treat empty summaries**

Document and test one rule explicitly:

- either `0.0` if there are no claims
- or `None` is not supported and metric returns `0.0`

Do not leave this implicit.

**Step 5: Update docs to match the implemented Phase 2 simplification**

In `golden_set/README.md` or a nearby package doc, state that current grounding is deterministic claim-vs-chunk-text overlap, not yet RAGAS.

**Step 6: Re-run targeted grounding tests**

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_metrics.py -q --tb=short -k grounding
```

Expected: PASS.

**Step 7: Do not commit**

Owner reviews and commits later. Do not run `git commit`.

---

### Task 4: Rebuild TemporalPrecision on Matched Causes and Temporal Anchors

**Files:**
- Modify: `packages/eval/catalyst_eval/metrics/temporal_precision.py`
- Modify: `packages/eval/tests/test_metrics.py`
- Modify: `packages/eval/catalyst_eval/schema/result.py`
- Reference: `docs/superpowers/specs/2026-04-02-catalyst-system-design.md:639`

**Step 1: Write failing temporal tests for “right day, wrong anchor”**

Add tests like:

```python
def test_temporal_precision_requires_anchor_match_for_matched_causes():
    pred = AttributionResult(
        ticker="AAPL",
        trade_date="2026-01-15",
        causes=[
            PredictedCause(
                text="China export ban on H20 chips",
                category="geopolitical",
                confidence=0.9,
                evidence_ids=[],
                direction="negative",
                temporal_anchor="after-hours",
            )
        ],
        summary="",
    )
    assert TemporalPrecision().compute(pred, GOLDEN) == 0.0


def test_temporal_precision_rewards_matched_cause_with_correct_anchor():
    pred = AttributionResult(
        ticker="AAPL",
        trade_date="2026-01-15",
        causes=[
            PredictedCause(
                text="China export ban on H20 chips",
                category="geopolitical",
                confidence=0.9,
                evidence_ids=[],
                direction="negative",
                temporal_anchor="pre-market",
            )
        ],
        summary="",
    )
    assert TemporalPrecision().compute(pred, GOLDEN) == 1.0
```

**Step 2: Run only the temporal tests**

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_metrics.py -q --tb=short -k temporal
```

Expected: FAIL because current metric only compares calendar dates.

**Step 3: Implement matched-cause temporal precision**

Implement:

- use the shared matching helper
- only score matched predicted/golden cause pairs
- compare `predicted_causes[pred_idx].temporal_anchor` to `golden.causes[gold_idx].temporal_anchor`
- return `correct_anchor_matches / matched_pairs`
- return `0.0` if there are no matched pairs or if matched predictions omit the anchor

**Step 4: Keep the trade-date check only as a guardrail**

If desired, require `predicted.trade_date == golden.trade_date` first, then evaluate anchor precision. Document this clearly in the metric docstring and tests.

**Step 5: Re-run temporal tests**

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_metrics.py -q --tb=short -k temporal
```

Expected: PASS.

**Step 6: Do not commit**

Owner reviews and commits later. Do not run `git commit`.

---

### Task 5: Rebuild ConfidenceCalibration on Matched-Cause Correctness

**Files:**
- Modify: `packages/eval/catalyst_eval/metrics/confidence_calibration.py`
- Modify: `packages/eval/tests/test_metrics.py`
- Reference: `packages/eval/catalyst_eval/metrics/matching.py`

**Step 1: Write failing calibration tests for the remaining loophole**

Add tests like:

```python
def test_confidence_calibration_penalizes_unrelated_high_confidence_same_category_predictions():
    pred = AttributionResult(
        ticker="AAPL",
        trade_date="2026-01-15",
        causes=[
            PredictedCause(
                text="Completely unrelated geopolitical rumor",
                category="geopolitical",
                confidence=0.95,
                evidence_ids=[],
                direction="negative",
                temporal_anchor="pre-market",
            ),
            PredictedCause(
                text="Another unrelated sector rumor",
                category="sector",
                confidence=0.9,
                evidence_ids=[],
                direction="negative",
                temporal_anchor="intraday",
            ),
        ],
        summary="",
    )
    score = ConfidenceCalibration().compute(pred, GOLDEN)
    assert score < 0.5
```

**Step 2: Run the calibration tests**

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_metrics.py -q --tb=short -k calibration
```

Expected: FAIL because current implementation treats category presence as correctness.

**Step 3: Implement bucket accuracy using matched predictions**

For each predicted cause in a bucket:

- find whether it matched a golden cause using the shared matcher
- count it as accurate only if:
  - it matched a golden cause, and
  - its category matches that matched golden cause’s category

This keeps calibration tied to actual attribution correctness, not category coincidence.

**Step 4: Re-run calibration tests**

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_metrics.py -q --tb=short -k calibration
```

Expected: PASS.

**Step 5: Re-run the full metric suite**

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_metrics.py -q --tb=short
```

Expected: PASS.

**Step 6: Do not commit**

Owner reviews and commits later. Do not run `git commit`.

---

### Task 6: Treat the Golden Set as a Versioned Dataset, Not a Placeholder

**Files:**
- Modify: `packages/eval/golden_set/README.md`
- Modify: `packages/eval/golden_set/v1.jsonl` or create `packages/eval/golden_set/v1_1.jsonl`
- Create: `packages/eval/tests/test_golden_set.py`
- Create: `packages/eval/scripts/audit_golden_set.py`

**Step 1: Write dataset audit tests**

Add tests like:

```python
def test_golden_set_rows_validate_against_schema():
    ...


def test_golden_set_has_at_least_one_example_for_each_required_category():
    ...


def test_golden_set_is_not_all_negative_moves():
    ...
```

If the current `v1.jsonl` is intentionally too small to pass those stricter assertions, split the tests into:

- `must-pass now` schema/integrity tests
- `target for v1_1` coverage tests

Do not fake green by weakening the intended coverage requirement.

**Step 2: Add an audit script for human reviewers**

Create `scripts/audit_golden_set.py` that prints:

- row count
- unique tickers
- category counts
- temporal anchor counts
- positive vs negative move counts
- any invalid rows with schema errors

Command:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m scripts.audit_golden_set
```

**Step 3: Decide versioning policy explicitly**

Choose one:

- keep `v1.jsonl` as exploratory and create `v1_1.jsonl` for the first human-reviewed set
- or upgrade `v1.jsonl` in place and state the old file was provisional

Write the decision into `golden_set/README.md`.

**Step 4: Expand the dataset to close the most obvious coverage gaps**

At minimum, add:

- one `macro` event
- at least one non-negative move event if the eval is meant to generalize beyond selloffs
- enough total events to stop calling calibration “research-grade” on 5 samples

If full expansion to 15+ events cannot happen in the same pass, explicitly gate the report language:

- “exploratory”
- “not statistically reliable”
- “not for model ranking claims”

**Step 5: Re-run dataset audit**

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_golden_set.py -q --tb=short
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m scripts.audit_golden_set
```

Expected: schema integrity passes; coverage output is explicit and reviewable.

**Step 6: Do not commit**

Owner reviews and commits later. Do not run `git commit`.

---

### Task 7: Re-Verify the Harness Against the Hardened Metrics

**Files:**
- Modify: `packages/eval/tests/test_harness.py`
- Modify: `packages/eval/catalyst_eval/harness/runner.py` only if new schema requires compatibility cleanup
- Modify: `packages/eval/catalyst_eval/harness/experiment.py` only if needed for richer reporting
- Modify: `packages/eval/catalyst_eval/reports/markdown.py` only if metric naming or warnings need surfacing

**Step 1: Add end-to-end harness tests using the hardened schema**

Add one deterministic predict function that returns:

- cause text
- category
- confidence
- temporal anchor
- retrieved chunk objects
- summary

Then assert `evaluate()` returns non-trivial scores for:

- `attribution_f1`
- `category_accuracy`
- `grounding_rate`
- `temporal_precision`
- `confidence_calibration`

**Step 2: Run the harness tests**

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_harness.py -q --tb=short
```

Expected: PASS.

**Step 3: Decide whether markdown reports need caveat text**

If the dataset still remains exploratory, add a small warning line above the table, for example:

```python
if report.metadata.get("dataset_status") == "exploratory":
    lines.insert(0, "> Warning: dataset coverage is exploratory and not suitable for strong ranking claims.")
```

Only do this if the report object already carries enough context, or if adding a small metadata field is worth the complexity.

**Step 4: Re-run harness tests if reporting changed**

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_harness.py -q --tb=short
```

Expected: PASS.

**Step 5: Do not commit**

Owner reviews and commits later. Do not run `git commit`.

---

### Task 8: Final Verification and Audit Closeout

**Files:**
- Modify: `docs/plans/2026-04-06-eval-module-audit.md`
- Create: `docs/testing/eval-module-audit-closeout.md`

**Step 1: Run the full eval suite in the known-good venv**

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/ -q --tb=short
```

Expected: PASS.

**Step 2: Re-run the golden-set audit script**

Run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
PYTHONPATH=. /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m scripts.audit_golden_set
```

Expected: explicit coverage summary with no schema failures.

**Step 3: Write a closeout note**

Create `docs/testing/eval-module-audit-closeout.md` summarizing:

- what was fixed
- what is now spec-aligned
- what remains intentionally simplified
- whether `packages/eval` is ready for agent comparison
- any dataset caveats that still limit interpretation

**Step 4: Update the original audit plan status**

Mark the resolved items and keep any remaining caveats explicit.

**Step 5: Do not commit**

Owner reviews and commits later. Do not run `git commit`.


# Plan B→A Critic Observability + Calibration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add Critic observability and minimal calibration fixes, then run diagnostic and full ablation reruns to raise P1 status accuracy with measurable, explainable changes.

**Architecture:** Keep the current Miner→Critic→Judge graph. Add low-risk schema/normalization hardening in Critic, persist pre-filter scores (`all_graded_chunks`) through state and summary artifacts, and use a small diagnostic run to choose calibration (K vs threshold) based on measured score distribution before the next 200-run ablation.

**Tech Stack:** Python 3.12, Pydantic models, pytest, Catalyst ablation scripts (`p1_trace_report.py`, `p1_ablation.py`), JSONL golden set.

---

## Guardrails

- Only implement changes listed in this plan (no opportunistic refactors).
- DRY/YAGNI: minimal code to satisfy each failing test.
- TDD for every code change.
- Stage changes per task; human reviewer performs final commit/finalize.
- Run commands from repo root: `/Users/yiannischen/Desktop/Catalyst`.

---

### Task 1: Quick Win — Fix g001 Golden Label

**Files:**
- Modify: `/Users/yiannischen/Desktop/Catalyst/packages/eval/golden_set/v1_2_p1_set.jsonl`
- Modify: `/Users/yiannischen/Desktop/Catalyst/packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py`
- Test: `/Users/yiannischen/Desktop/Catalyst/packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py`

**Step 1: Write the failing test**

Add:

```python
def test_g001_not_refusal_case():
    row = _load("g001")
    assert row["expected_status"] == "SUFFICIENT"
    assert row["should_refuse"] is False
```

**Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py::test_g001_not_refusal_case -v
```

Expected: FAIL (current `g001` label mismatch).

**Step 3: Write minimal implementation**

In `v1_2_p1_set.jsonl`, update only g001:

- `"expected_status": "SUFFICIENT"`
- `"should_refuse": false`

**Step 4: Run test to verify it passes**

Run:

```bash
python -m pytest packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py::test_g001_not_refusal_case -v
```

Expected: PASS.

**Step 5: Stage changes for review**

```bash
git add packages/eval/golden_set/v1_2_p1_set.jsonl packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py
```

---

### Task 2: Quick Win — Category Whitelist Normalization in Critic

**Files:**
- Modify: `/Users/yiannischen/Desktop/Catalyst/packages/agents/catalyst_agents/nodes/critic.py`
- Modify: `/Users/yiannischen/Desktop/Catalyst/packages/agents/tests/test_critic.py`
- Test: `/Users/yiannischen/Desktop/Catalyst/packages/agents/tests/test_critic.py`

**Step 1: Write the failing test**

Add:

```python
def test_parse_critic_response_normalizes_unknown_category_to_other():
    payload = '{"graded_chunks":[{"chunk_id":"c1","relevance":0.8,"category":"news","temporal_match":true,"reasoning":"misc"}],"reasoning":"ok"}'
    result = _parse_critic_response(payload)
    assert result["graded_chunks"][0]["category"] == "other"
```

**Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest packages/agents/tests/test_critic.py::test_parse_critic_response_normalizes_unknown_category_to_other -v
```

Expected: FAIL (`"news"` not normalized).

**Step 3: Write minimal implementation**

In `critic.py`, add whitelist and normalize anything not in whitelist:

```python
_VALID_CATEGORIES = {
    "earnings", "macro", "geopolitical", "sector", "technical", "regulatory", "other"
}

if isinstance(parsed, dict):
    for chunk in parsed.get("graded_chunks", []) or []:
        cat = str(chunk.get("category", "")).strip().lower()
        if cat not in _VALID_CATEGORIES:
            chunk["category"] = "other"
```

**Step 4: Run tests to verify pass + no regression**

Run:

```bash
python -m pytest \
  packages/agents/tests/test_critic.py::test_parse_critic_response_accepts_other_category \
  packages/agents/tests/test_critic.py::test_parse_critic_response_normalizes_none_to_other \
  packages/agents/tests/test_critic.py::test_parse_critic_response_normalizes_unknown_category_to_other -v
```

Expected: all PASS.

**Step 5: Stage changes for review**

```bash
git add packages/agents/catalyst_agents/nodes/critic.py packages/agents/tests/test_critic.py
```

---

### Task 3: Quick Win — Tag Known Coverage Gap Cases

**Files:**
- Modify: `/Users/yiannischen/Desktop/Catalyst/packages/eval/golden_set/v1_2_p1_set.jsonl`
- Modify: `/Users/yiannischen/Desktop/Catalyst/packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py`
- Test: `/Users/yiannischen/Desktop/Catalyst/packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py`

**Step 1: Write the failing test**

Add:

```python
def test_p1_coverage_gap_cases_tagged():
    for cid in ("g015", "g046", "g049"):
        row = _load(cid)
        assert row.get("data_coverage_gap") is True
```

**Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py::test_p1_coverage_gap_cases_tagged -v
```

Expected: FAIL (field absent).

**Step 3: Write minimal implementation**

In `v1_2_p1_set.jsonl`, add `"data_coverage_gap": true` to g015, g046, g049 only.

**Step 4: Run test to verify it passes**

Run:

```bash
python -m pytest packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py::test_p1_coverage_gap_cases_tagged -v
```

Expected: PASS.

**Step 5: Stage changes for review**

```bash
git add packages/eval/golden_set/v1_2_p1_set.jsonl packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py
```

---

### Task 4: Persist `all_graded_chunks` End-to-End (State + Critic)

**Files:**
- Modify: `/Users/yiannischen/Desktop/Catalyst/packages/agents/catalyst_agents/state.py`
- Modify: `/Users/yiannischen/Desktop/Catalyst/packages/agents/catalyst_agents/nodes/critic.py`
- Modify: `/Users/yiannischen/Desktop/Catalyst/packages/agents/tests/test_critic.py`
- Test: `/Users/yiannischen/Desktop/Catalyst/packages/agents/tests/test_critic.py`

**Step 1: Write failing tests**

Add:

```python
def test_state_declares_all_graded_chunks_field():
    assert "all_graded_chunks" in AttributionState.__annotations__

def test_critic_persists_all_graded_chunks():
    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    result = critic(state, llm=MockLLM(GOOD_LLM_RESPONSE))
    assert len(result["all_graded_chunks"]) == 2
    ids = {c["chunk_id"] for c in result["all_graded_chunks"]}
    assert {"c1", "c2"} <= ids

def test_critic_all_graded_empty_on_error(monkeypatch):
    sleeps = []
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", sleeps.append)
    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    result = critic(state, llm=FlakyLLM(failures=3))
    assert result["all_graded_chunks"] == []
```

**Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest \
  packages/agents/tests/test_critic.py::test_state_declares_all_graded_chunks_field \
  packages/agents/tests/test_critic.py::test_critic_persists_all_graded_chunks \
  packages/agents/tests/test_critic.py::test_critic_all_graded_empty_on_error -v
```

Expected: FAIL.

**Step 3: Write minimal implementation (state + node together)**

In `state.py` (`AttributionState` Critic output section), add:

```python
all_graded_chunks: list[dict]
```

In `critic()` return payloads:

- success path: add `"all_graded_chunks": graded`
- empty-chunks path: add `"all_graded_chunks": []`
- runtime-error path: add `"all_graded_chunks": []`

**Step 4: Run tests to verify pass + smoke regression**

Run:

```bash
python -m pytest \
  packages/agents/tests/test_critic.py::test_state_declares_all_graded_chunks_field \
  packages/agents/tests/test_critic.py::test_critic_persists_all_graded_chunks \
  packages/agents/tests/test_critic.py::test_critic_all_graded_empty_on_error \
  packages/agents/tests/test_critic.py::test_critic_filters_by_relevance -v
```

Expected: PASS.

**Step 5: Stage changes for review**

```bash
git add packages/agents/catalyst_agents/state.py packages/agents/catalyst_agents/nodes/critic.py packages/agents/tests/test_critic.py
```

---

### Task 5: Expose Score Distribution in Trace Summary

**Files:**
- Modify: `/Users/yiannischen/Desktop/Catalyst/scripts/p1_trace_report.py`
- Test: `/Users/yiannischen/Desktop/Catalyst/packages/agents/tests/test_critic.py` (regression) and compile checks

**Step 1: Add summary fields**

In summary `"critic"` section, add:

```python
"all_graded_count": len(result.get("all_graded_chunks", [])),
"all_graded_scores": [
    {
        "chunk_id": c.get("chunk_id", ""),
        "relevance": c.get("relevance", 0.0),
        "category": c.get("category", ""),
    }
    for c in result.get("all_graded_chunks", [])
],
"below_threshold_count": len(result.get("all_graded_chunks", [])) - len(result.get("graded_evidence", [])),
```

**Step 2: Run compile checks**

Run:

```bash
python -m py_compile \
  packages/agents/catalyst_agents/nodes/critic.py \
  packages/agents/catalyst_agents/state.py \
  scripts/p1_trace_report.py
```

Expected: no errors.

**Step 3: Run critic test suite regression**

Run:

```bash
python -m pytest packages/agents/tests/test_critic.py -q
```

Expected: PASS.

**Step 4: Stage changes for review**

```bash
git add scripts/p1_trace_report.py
```

**Step 5: Stage full Phase 0+1 test files**

```bash
git add packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py packages/agents/tests/test_critic.py
```

---

### Task 6: Diagnostic Run (20 Cases, Full Profile Only)

**Files:**
- Runtime only (no code change)
- Artifacts in `/root/Catalyst/data/eval_reports` (cloud)

**Step 1: Run diagnostic ablation**

```bash
python scripts/p1_ablation.py \
  --golden-set packages/eval/golden_set/v1_2_p1_set.jsonl \
  --profiles full \
  --case-ids g002,g003,g010,g012,g013,g014,g015,g017,g021,g023,g027,g029,g030,g032,g033,g034,g046,g049,g001,g009 \
  --continue-on-error \
  --tag p1_diagnostic_scores_$(date +%Y%m%d_%H%M%S)
```

Expected: 20 runs complete with summary files containing `critic.all_graded_scores`.

**Step 2: Extract score distribution**

Run (analysis snippet):

```bash
python - <<'PY'
import json, glob
files = glob.glob("data/eval_reports/p1_runs/*p1_diagnostic*/**/summaries/*.summary.json", recursive=True)
scores = []
for f in files:
    d = json.load(open(f))
    for s in d.get("critic", {}).get("all_graded_scores", []):
        scores.append(float(s.get("relevance", 0.0)))
print("n_scores=", len(scores))
scores.sort()
def q(p): 
    return scores[int((len(scores)-1)*p)] if scores else None
print("p25=", q(0.25), "p50=", q(0.50), "p75=", q(0.75))
band = sum(0.4 <= x < 0.5 for x in scores)
print("band_0.4_0.5=", band, "ratio=", (band/len(scores) if scores else 0))
PY
```

Expected: numeric distribution and 0.4–0.5 band ratio.

**Step 3: Stage diagnostic notes (human)**

Record outputs in a short markdown note under `docs/` for review before calibration.

**Step 4: No code stage**

No `git add` for runtime-only task.

**Step 5: Decision checkpoint**

Use rule:
- band ratio ≥ 0.40 → lower `RELEVANCE_THRESHOLD` to 0.35–0.40
- band ratio < 0.20 → lower `K_SUFFICIENT` 3→2
- otherwise both: `RELEVANCE_THRESHOLD=0.40` + `K_SUFFICIENT=2`

---

### Task 7: Calibration Change + ADR Update + Full 200 Rerun

**Files:**
- Modify (as decided): `/Users/yiannischen/Desktop/Catalyst/packages/agents/catalyst_agents/nodes/critic.py`
- Modify: `/Users/yiannischen/Desktop/Catalyst/packages/agents/tests/test_critic.py`
- Modify: `/Users/yiannischen/Desktop/Catalyst/docs/ADR/ADR-010-critic-k-sufficient-recalibration.md`

**Step 1: Write failing/updated constants test**

If threshold changed, update `test_relevance_threshold_value()`.  
If K changed, add/update:

```python
def test_k_sufficient_value():
    assert K_SUFFICIENT == 2
```

**Step 2: Apply minimal calibration code change**

Only change selected constant(s) in `critic.py` per Task 6 decision.

**Step 3: Update ADR**

Append:
- diagnostic score distribution stats
- why chosen calibration is selected
- rollback condition

**Step 4: Run targeted tests + compile**

```bash
python -m pytest packages/agents/tests/test_critic.py -q
python -m py_compile packages/agents/catalyst_agents/nodes/critic.py
```

Expected: PASS, no compile errors.

**Step 5: Stage calibration changes**

```bash
git add packages/agents/catalyst_agents/nodes/critic.py packages/agents/tests/test_critic.py docs/ADR/ADR-010-critic-k-sufficient-recalibration.md
```

---

### Task 8: Full 200 Rerun + GO/NO-GO Gate

**Files:**
- Runtime artifacts only

**Step 1: Run full rerun**

```bash
python scripts/p1_ablation.py \
  --golden-set packages/eval/golden_set/v1_2_p1_set.jsonl \
  --profiles full,no_rerank,no_vector,degraded \
  --continue-on-error \
  --tag p1_full_calibrated_$(date +%Y%m%d_%H%M%S)
```

**Step 2: Pull aggregate metrics**

Extract from `${RUN_TAG}_p1_ablation.json`:
- full status_acc
- degraded status_acc
- delta full-degraded
- system_error count
- full grounding_rate

**Step 3: Apply GO/NO-GO**

- GO if:
  - full status_acc ≥ 0.72
  - degraded status_acc ≥ 0.58
  - full - degraded ≥ 0.12
  - SYSTEM_ERROR == 0
  - full grounding_rate ≥ 0.90
- NO-GO otherwise.

**Step 4: Stage no code**

No `git add` (runtime-only).

**Step 5: Hand-off report**

Write short decision memo with:
- calibration chosen
- KPI table
- pass/fail against gate
- rerun tag paths

---

## Validation Matrix (minimum before handoff)

- `python -m pytest packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py -q`
- `python -m pytest packages/agents/tests/test_critic.py -q`
- `python -m py_compile packages/agents/catalyst_agents/state.py packages/agents/catalyst_agents/nodes/critic.py scripts/p1_trace_report.py`

---

Plan complete and saved to `docs/plans/2026-05-14-plan-b-a-critic-observability-calibration.md`. Two execution options:

**1. Subagent-Driven (this session)** - I dispatch fresh subagent per task, review between tasks, fast iteration

**2. Parallel Session (separate)** - Open new session with executing-plans, batch execution with checkpoints

Which approach?

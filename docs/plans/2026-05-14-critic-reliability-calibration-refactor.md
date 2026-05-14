# Critic Reliability & Calibration Refactor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix Critic parsing failures, recalibrate Critic sufficiency gating, correct verified golden-label errors, and produce a rerun-ready P1 pipeline with lower SYSTEM_ERROR and higher status accuracy.

**Architecture:** Keep the existing Miner→Critic→DecisionRouter→Judge pipeline. Apply minimal, test-first changes inside Critic parsing and decision logic; do not add new nodes. Update golden labels only where evidence is verified, and run reruns in two phases (targeted then full) to isolate parsing fixes from threshold calibration effects.

**Tech Stack:** Python 3.12, pytest, Pydantic v2, LangGraph nodes, JSONL golden set, shell-based rerun workflow.

---

### Task 1: Harden Critic JSON Parsing for List-Wrapped Payloads

**Files:**
- Modify: `packages/agents/catalyst_agents/nodes/critic.py`
- Test: `packages/agents/tests/test_critic.py`

**Step 1: Write the failing test**

Add:

```python
def test_parse_critic_response_accepts_single_item_list_wrapper():
    payload = '[{"graded_chunks":[{"chunk_id":"c1","relevance":0.9,"category":"earnings","temporal_match":true,"reasoning":"ok"}],"reasoning":"wrapped"}]'
    result = _parse_critic_response(payload)
    assert result["reasoning"] == "wrapped"
    assert result["graded_chunks"][0]["chunk_id"] == "c1"
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest packages/agents/tests/test_critic.py::test_parse_critic_response_accepts_single_item_list_wrapper -v`  
Expected: FAIL with Pydantic model type error (`input_type=list`).

**Step 3: Write minimal implementation**

In `_parse_critic_response`:

```python
parsed = json.loads(cleaned)
if isinstance(parsed, list):
    if len(parsed) == 1 and isinstance(parsed[0], dict):
        parsed = parsed[0]
    else:
        raise ValueError("critic response list payload is invalid")
validated = CriticResponse.model_validate(parsed)
```

**Step 4: Run test to verify it passes**

Run the same test command.  
Expected: PASS.

**Step 5: Commit**

```bash
git add packages/agents/catalyst_agents/nodes/critic.py packages/agents/tests/test_critic.py
git commit -m "fix(critic): accept single-item list-wrapped JSON responses"
```

### Task 2: Fix Category Handling Without Metric Pollution

**Files:**
- Modify: `packages/agents/catalyst_agents/nodes/critic.py`
- Test: `packages/agents/tests/test_critic.py`

**Step 1: Write failing tests**

Add:

```python
def test_parse_critic_response_accepts_other_category():
    payload = '{"graded_chunks":[{"chunk_id":"c1","relevance":0.8,"category":"other","temporal_match":true,"reasoning":"misc"}],"reasoning":"ok"}'
    result = _parse_critic_response(payload)
    assert result["graded_chunks"][0]["category"] == "other"


def test_parse_critic_response_normalizes_none_to_other():
    payload = '{"graded_chunks":[{"chunk_id":"c1","relevance":0.8,"category":"None","temporal_match":true,"reasoning":"misc"}],"reasoning":"ok"}'
    result = _parse_critic_response(payload)
    assert result["graded_chunks"][0]["category"] == "other"
```

**Step 2: Run tests to verify they fail**

Run:  
`python -m pytest packages/agents/tests/test_critic.py::test_parse_critic_response_accepts_other_category packages/agents/tests/test_critic.py::test_parse_critic_response_normalizes_none_to_other -v`  
Expected: FAIL with category literal validation errors.

**Step 3: Write minimal implementation**

1) Expand schema literal:

```python
category: Literal["earnings", "macro", "geopolitical", "sector", "technical", "regulatory", "other"]
```

2) Normalize only missing/none-like values to `other` before `model_validate`:

```python
if isinstance(parsed, dict):
    for chunk in parsed.get("graded_chunks", []) or []:
        cat = str(chunk.get("category", "")).strip().lower()
        if cat in {"", "none", "null"}:
            chunk["category"] = "other"
```

**Step 4: Run tests to verify they pass**

Run the same two-test command.  
Expected: PASS.

**Step 5: Commit**

```bash
git add packages/agents/catalyst_agents/nodes/critic.py packages/agents/tests/test_critic.py
git commit -m "fix(critic): support category other and normalize None-like category values"
```

### Task 3: Recalibrate Critic Decision Gate (K=3) and Branch Semantics

**Files:**
- Modify: `packages/agents/catalyst_agents/nodes/critic.py`
- Test: `packages/agents/tests/test_critic.py`
- Docs: `docs/ADR/ADR-00X-critic-k-sufficient-recalibration.md` (new)

**Step 1: Write failing tests**

Add:

```python
def test_critic_k3_marks_sufficient_with_three_high_relevance_chunks():
    payload = json.dumps({
        "graded_chunks": [
            {"chunk_id": "c1", "relevance": 0.9, "category": "earnings", "temporal_match": True, "reasoning": "r1"},
            {"chunk_id": "c2", "relevance": 0.8, "category": "earnings", "temporal_match": True, "reasoning": "r2"},
            {"chunk_id": "c3", "relevance": 0.7, "category": "sector", "temporal_match": True, "reasoning": "r3"},
        ],
        "reasoning": "enough"
    })
    state = {**BASE_STATE, "reranked_chunks": [
        {"asset_id":"c1","source_type":"polygon_news","reference_date":"2026-01-15","content_md":"a"},
        {"asset_id":"c2","source_type":"polygon_news","reference_date":"2026-01-15","content_md":"b"},
        {"asset_id":"c3","source_type":"polygon_news","reference_date":"2026-01-15","content_md":"c"},
    ]}
    result = critic(state, llm=MockLLM(payload))
    assert result["critic_decision"].sufficiency == "sufficient"


def test_critic_one_chunk_relevant_is_partial_not_insufficient():
    payload = json.dumps({
        "graded_chunks": [
            {"chunk_id":"c1","relevance":0.9,"category":"earnings","temporal_match":True,"reasoning":"single"}
        ],
        "reasoning": "single"
    })
    state = {**BASE_STATE, "reranked_chunks":[{"asset_id":"c1","source_type":"polygon_news","reference_date":"2026-01-15","content_md":"a"}]}
    result = critic(state, llm=MockLLM(payload))
    assert result["critic_decision"].sufficiency == "partial"
```

**Step 2: Run tests to verify they fail**

Run:  
`python -m pytest packages/agents/tests/test_critic.py::test_critic_k3_marks_sufficient_with_three_high_relevance_chunks packages/agents/tests/test_critic.py::test_critic_one_chunk_relevant_is_partial_not_insufficient -v`  
Expected: FAIL under current K=4 and old branch behavior.

**Step 3: Write minimal implementation**

In `critic.py`:
- Change `K_SUFFICIENT = 3`.
- Refactor `_build_critic_decision` to:

```python
if evidence_count >= K_SUFFICIENT and magnitude_coverage >= M_THRESHOLD:
    sufficiency = "sufficient"
    next_action = "proceed"
elif evidence_count == 0:
    sufficiency = "insufficient"
    next_action = "refuse"
else:
    sufficiency = "partial"
    next_action = "proceed"
```

- Add a one-line comment near `_compute_magnitude_coverage` that `K_PARTIAL` is retained for magnitude scaling only.

**Step 4: Run tests to verify they pass**

Run same two-test command.  
Expected: PASS.

**Step 5: Add ADR**

Create `docs/ADR/ADR-00X-critic-k-sufficient-recalibration.md` with:
- old/new values (`4 -> 3`)
- rationale from P1 run
- expected impact
- rollback strategy.

**Step 6: Commit**

```bash
git add packages/agents/catalyst_agents/nodes/critic.py packages/agents/tests/test_critic.py docs/ADR/ADR-00X-critic-k-sufficient-recalibration.md
git commit -m "refactor(critic): recalibrate sufficiency threshold and document ADR"
```

### Task 4: Add Recovery Regression for Combined Malformed Payload

**Files:**
- Modify: `packages/agents/tests/test_critic.py`

**Step 1: Write failing test**

Add:

```python
def test_critic_recoverable_payload_does_not_set_system_error():
    payload = '[{"graded_chunks":[{"chunk_id":"c1","relevance":0.9,"category":"other","temporal_match":true,"reasoning":"ok"}],"reasoning":"wrapped"}]'
    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    result = critic(state, llm=MockLLM(payload))
    assert result["critic_decision"] is not None
    assert result.get("error_type") is None
```

**Step 2: Run test to verify it fails (before Tasks 1-2) / passes (after Tasks 1-2)**

Run: `python -m pytest packages/agents/tests/test_critic.py::test_critic_recoverable_payload_does_not_set_system_error -v`  
Expected after Tasks 1-2: PASS.

**Step 3: Re-run full Critic suite**

Run: `python -m pytest packages/agents/tests/test_critic.py -q`  
Expected: PASS.

**Step 4: Commit**

```bash
git add packages/agents/tests/test_critic.py
git commit -m "test(critic): add regression for recoverable malformed payloads"
```

### Task 5: Golden Labels — Split Verified vs Pending Change

**Files:**
- Modify: `packages/eval/golden_set/v1_2_p1_set.jsonl`
- Create: `packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py`

**Step 1: Write failing test for verified case only (g009)**

Create test file asserting only verified change first:

```python
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
GOLDEN = PROJECT_ROOT / "packages" / "eval" / "golden_set" / "v1_2_p1_set.jsonl"

def _load(case_id: str):
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["id"] == case_id:
            return row
    raise AssertionError(case_id)

def test_g009_not_refusal_case():
    g009 = _load("g009")
    assert g009["should_refuse"] is False
    assert g009["expected_status"] == "SUFFICIENT"
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py::test_g009_not_refusal_case -v`  
Expected: FAIL before label edit.

**Step 3: Write minimal implementation (g009 only now)**

Edit `g009` in `v1_2_p1_set.jsonl`:
- `expected_status` -> `SUFFICIENT`
- `should_refuse` -> `false`

Do **not** edit g001 yet.

**Step 4: Run test to verify it passes**

Run same command.  
Expected: PASS.

**Step 5: Commit**

```bash
git add packages/eval/golden_set/v1_2_p1_set.jsonl packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py
git commit -m "fix(eval): correct g009 refusal label in p1 golden set"
```

### Task 6: Pipeline Integrity Test Pass (Existing Files Only)

**Files:**
- Test only

**Step 1: Run targeted suites**

Run:

```bash
python -m pytest \
  packages/agents/tests/test_critic.py \
  packages/eval/tests/test_p1_ablation_resume.py \
  packages/eval/tests/test_trace_loader_v3_2.py \
  packages/eval/tests/test_p1_ablation_resilience.py \
  packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py -q
```

Expected: PASS.

**Step 2: Run compile guard**

Run:

```bash
python -m py_compile \
  packages/agents/catalyst_agents/nodes/critic.py \
  scripts/p1_ablation.py \
  scripts/p1_trace_report.py
```

Expected: exit 0.

**Step 3: Run dry-run contract**

Run:

```bash
python scripts/p1_ablation.py \
  --golden-set packages/eval/golden_set/v1_2_p1_set.jsonl \
  --profiles full,no_rerank,no_vector,degraded \
  --case-ids g001,g009,g013 \
  --dry-run
```

Expected: selected profiles/cases printed, exit 0.

**Step 4: Commit**

```bash
git add -A
git commit -m "chore: validate critic refactor and p1 ablation test contract"
```

### Task 7: Phase-1 Targeted Rerun (Resume-Aware)

**Files:**
- Output artifacts only: `data/eval_reports/*`

**Step 1: Verify resume behavior on SYSTEM_ERROR entries**

Run:

```bash
python - <<'PY'
import json, pathlib
p = pathlib.Path("data/eval_reports/p1_runs/p1_full_20260513_055625/p1_full_20260513_055625_p1_ablation.json")
d = json.loads(p.read_text())
print("run_status=", d.get("run_status"))
print("system_error_rows=", sum(1 for r in d["per_case"] if r["output_status"] == "SYSTEM_ERROR"))
PY
```

Expected: prints baseline system_error count.

**Step 2: Execute resume-based targeted rerun**

Run:

```bash
python scripts/p1_ablation.py \
  --golden-set packages/eval/golden_set/v1_2_p1_set.jsonl \
  --profiles full,no_rerank,no_vector,degraded \
  --resume-from data/eval_reports/p1_runs/p1_full_20260513_055625/p1_full_20260513_055625_p1_ablation.json \
  --continue-on-error \
  --tag p1_patch_validation_$(date +%Y%m%d_%H%M%S)
```

Expected: only unresolved/failing combinations rerun.

**Step 3: If resume skipped failed rows, clear only failed summaries and rerun**

If Step 2 reruns zero failing combos, remove known SYSTEM_ERROR summaries from the new run tag and rerun the same command.

Example cleanup command pattern:

```bash
rm -f data/eval_reports/<RUN_TAG>_*_g001_*summary.json data/eval_reports/<RUN_TAG>_*_g006_*summary.json
```

**Step 4: Commit run manifest pointer (optional)**

```bash
git add data/eval_reports/LATEST_P1_RUN data/eval_reports/p1_runs/*/README.md
git commit -m "docs(eval): register phase-1 critic patch validation run"
```

### Task 8: Phase-2 Full Rerun + Post-Run g001 Label Decision

**Files:**
- Modify (conditional): `packages/eval/golden_set/v1_2_p1_set.jsonl`
- Output artifacts: `data/eval_reports/*`

**Step 1: Run full 200-cell rerun**

Run:

```bash
python scripts/p1_ablation.py \
  --golden-set packages/eval/golden_set/v1_2_p1_set.jsonl \
  --profiles full,no_rerank,no_vector,degraded \
  --continue-on-error \
  --tag p1_full_refactor_$(date +%Y%m%d_%H%M%S)
```

Expected: completed run with strongly reduced SYSTEM_ERROR.

**Step 2: Inspect g001 after parsing fixes**

Run:

```bash
python - <<'PY'
import json,glob
files = sorted(glob.glob("data/eval_reports/p1_full_refactor_*_l1l2_full_g001_g001_p1_trace.summary.json"))
assert files, "missing g001 summary"
d = json.load(open(files[-1]))
print("status=", d["judge"]["output_status"])
print("graded_total=", d["retrieval"]["graded_counts"]["total"])
print("grounding=", d["metrics"]["grounding_rate"])
PY
```

Expected: g001 quality evidence visible for label decision.

**Step 3: Conditionally update g001 golden label**

If g001 is clearly supported (non-zero graded + grounded causes), update:
- `g001.expected_status` -> `SUFFICIENT`
- `g001.should_refuse` -> `false`

Then extend `test_p1_golden_refusal_labels_v3_2.py` with:

```python
def test_g001_not_refusal_case_after_validation():
    g001 = _load("g001")
    assert g001["should_refuse"] is False
    assert g001["expected_status"] == "SUFFICIENT"
```

**Step 4: Recompute and validate KPI gates**

Check in final ablation JSON:
- `full.status_accuracy >= 0.55`
- `full.status_accuracy - degraded.status_accuracy >= 0.15`
- `SYSTEM_ERROR` count near zero.

**Step 5: Commit final evaluation updates**

```bash
git add packages/eval/golden_set/v1_2_p1_set.jsonl packages/eval/tests/test_p1_golden_refusal_labels_v3_2.py data/eval_reports/LATEST_P1_RUN data/eval_reports/p1_runs/*/README.md
git commit -m "fix(eval): finalize golden labels and publish post-refactor p1 rerun results"
```


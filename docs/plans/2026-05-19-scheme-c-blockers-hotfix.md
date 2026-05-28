# Scheme C Blockers Hotfix Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the 4 blocking correctness gaps in Scheme C so Tier1/Tier2/attribution results are real, reproducible, and runnable end-to-end.

**Architecture:** Keep existing Scheme C structure and patch only the broken data contracts. Tier2 must read real reranked evidence from summary/trace artifacts, attribution-v2 must compute from real predicted/golden causes, Tier1 must use deterministic retrieval from real corpus (not synthetic string candidates), and the batch script must execute all required steps without placeholders.

**Tech Stack:** Python 3.12, pytest, JSON/JSONL, existing `scripts/reports/*`, existing `packages/eval/tests/*`, Bash runbook.

---

### Task 0: Worktree Preflight

**Files:**
- Modify: `docs/plans/2026-05-19-scheme-c-blockers-hotfix.md` (execution checklist updates only)

**Step 1: Create dedicated worktree**

```bash
cd /Users/yiannischen/Desktop/Catalyst
git worktree add /Users/yiannischen/Desktop/Catalyst-scheme-c-hotfix codex/scheme-c-blockers-hotfix
```

**Step 2: Verify branch and runtime**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c-hotfix
git branch --show-current
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest packages/eval/tests/test_run_direct_llm_frozen_baseline_v3_modes.py -q
```
Expected: branch is `codex/scheme-c-blockers-hotfix`; pytest baseline passes.

**Step 3: Commit checkpoint**

```bash
git add -A
git commit -m "chore(plan): start scheme-c blockers hotfix in isolated worktree"
```

---

### Task 1: Fix Tier2 Input Builder to Use Real Summary/Trace Evidence

**Files:**
- Modify: `scripts/reports/build_same_evidence_inputs.py`
- Modify: `packages/eval/tests/test_build_same_evidence_inputs.py`
- Test: `packages/eval/tests/test_build_same_evidence_inputs.py`

**Step 1: Write failing tests for real artifact extraction**

```python
def test_build_same_evidence_reads_summary_paths_and_extracts_reranked_chunks(tmp_path):
    # manifest has summary_json path only (no evidence_chunks field)
    rows, meta = build_same_evidence_inputs(manifest_path=manifest, repo_root=tmp_path)
    assert rows[0]["evidence_chunks_count"] > 0

def test_build_same_evidence_excludes_cases_with_no_reranked_chunks(tmp_path):
    rows, meta = build_same_evidence_inputs(manifest_path=manifest, repo_root=tmp_path)
    assert meta["tier2_excluded_n"] == 1
    assert meta["tier2_exclusion_reason_counts"]["no_reranked_chunks"] == 1
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c-hotfix
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest packages/eval/tests/test_build_same_evidence_inputs.py -v
```
Expected: FAIL because current implementation reads nonexistent `frozen_units[].evidence_chunks`.

**Step 3: Write minimal implementation**

```python
def _resolve_summary_path(summary_json: str, repo_root: Path) -> Path:
    # map /root/Catalyst/... to local repo_root

def _extract_reranked_chunks(summary_payload: dict[str, Any]) -> list[dict[str, Any]]:
    # read summary_payload["retrieval"]["reranked_chunks"] if present
    # fallback: summary_payload["retrieval"]["reranked_assets"] mapping to text fields

def build_same_evidence_inputs(...):
    # iterate manifest frozen units + summary paths, extract real chunks
```

**Step 4: Run test to verify it passes**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest packages/eval/tests/test_build_same_evidence_inputs.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/reports/build_same_evidence_inputs.py packages/eval/tests/test_build_same_evidence_inputs.py
git commit -m "fix(tier2): build same-evidence inputs from real summary/trace reranked chunks"
```

---

### Task 2: Enforce Tier2 Subset Evaluation in Comparator

**Files:**
- Modify: `scripts/reports/compare_catalyst_vs_direct_llm.py`
- Modify: `packages/eval/tests/test_compare_catalyst_vs_direct_llm_v3_tiers.py`
- Test: `packages/eval/tests/test_compare_catalyst_vs_direct_llm_v3_tiers.py`

**Step 1: Write failing tests for eligible subset alignment**

```python
def test_tier2_uses_eligible_subset_only():
    out = compute_comparison(..., baseline_tier="tier2_same_evidence", tier2_meta={"tier2_eligible_n": 60})
    assert out["subset_eval"] is True
    assert out["cohort_n_eval"] == 60
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c-hotfix
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest packages/eval/tests/test_compare_catalyst_vs_direct_llm_v3_tiers.py -v
```
Expected: FAIL because current comparator still computes metrics on full frozen set.

**Step 3: Write minimal implementation**

```python
if baseline_tier == "tier2_same_evidence":
    eligible_keys = set((r["case_id"], r["profile"]) for r in direct_rows_aligned if r.get("evidence_chunks_count", 0) > 0)
    keys = [k for k in keys if k in eligible_keys]
    result["subset_eval"] = True
    result["cohort_n_eval"] = len(keys)
```

**Step 4: Run test to verify it passes**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest packages/eval/tests/test_compare_catalyst_vs_direct_llm_v3_tiers.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/reports/compare_catalyst_vs_direct_llm.py packages/eval/tests/test_compare_catalyst_vs_direct_llm_v3_tiers.py
git commit -m "fix(eval): enforce tier2 eligible-subset metric computation"
```

---

### Task 3: Fix Attribution-v2 Input Contract (Real Per-Case Mapping)

**Files:**
- Modify: `scripts/reports/compute_attribution_metrics_v2.py`
- Create: `packages/eval/tests/test_compute_attribution_metrics_v2_contract_io.py`
- Test: `packages/eval/tests/test_compute_attribution_metrics_v2.py`
- Test: `packages/eval/tests/test_compute_attribution_metrics_v2_contract_io.py`

**Step 1: Write failing tests for real input schema**

```python
def test_attribution_v2_maps_from_real_case_schema(tmp_path):
    # catalyst row has causes/category from summary-like shape
    # direct row has parsed answer with cause text list
    out = compute_metrics(...)
    assert out["catalyst"]["category_f1"] >= 0.0
    assert out["direct_llm"]["cause_semantic_sim"] >= 0.0

def test_attribution_v2_fails_fast_when_required_fields_missing(tmp_path):
    with pytest.raises(ValueError, match="missing required attribution fields"):
        compute_metrics(...)
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c-hotfix
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest packages/eval/tests/test_compute_attribution_metrics_v2.py packages/eval/tests/test_compute_attribution_metrics_v2_contract_io.py -v
```
Expected: FAIL because current script expects `pred_*`/`gold_*` fields that are absent in real outputs.

**Step 3: Write minimal implementation**

```python
def _adapt_catalyst_row(row: dict[str, Any]) -> tuple[set[str], list[str], set[str], list[str]]:
    # parse predicted categories/causes from catalyst per-case or summary-linked payload
    # parse golden categories/causes from provided golden map

def _adapt_direct_row(row: dict[str, Any], golden: dict[str, Any]) -> ...:
    # parse direct answer text/categories with deterministic parser

def _compute(...):
    # use adapters; raise ValueError if adapters cannot build required fields
```

**Step 4: Run tests to verify they pass**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest packages/eval/tests/test_compute_attribution_metrics_v2.py packages/eval/tests/test_compute_attribution_metrics_v2_contract_io.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/reports/compute_attribution_metrics_v2.py packages/eval/tests/test_compute_attribution_metrics_v2_contract_io.py packages/eval/tests/test_compute_attribution_metrics_v2.py
git commit -m "fix(metrics): align attribution-v2 with real catalyst/direct per-case schemas"
```

---

### Task 4: Replace Tier1 Synthetic Corpus with Deterministic Real Retrieval

**Files:**
- Modify: `scripts/reports/build_tier1_search_corpus.py`
- Modify: `packages/eval/tests/test_build_tier1_search_corpus.py`
- Test: `packages/eval/tests/test_build_tier1_search_corpus.py`

**Step 1: Write failing tests for real retrieval semantics**

```python
def test_tier1_corpus_uses_real_sources_not_synthetic_placeholder(tmp_path):
    rows = build_tier1_search_corpus(...)
    assert rows[0]["search_candidates"][0]["source"] in {"polygon_news", "fmp_fundamentals", "lancedb"}
    assert "cand0" not in rows[0]["search_candidates"][0]["chunk_id"]

def test_tier1_candidates_stable_order(tmp_path):
    r1 = build_tier1_search_corpus(...)
    r2 = build_tier1_search_corpus(...)
    assert r1 == r2
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c-hotfix
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest packages/eval/tests/test_build_tier1_search_corpus.py -v
```
Expected: FAIL because current script emits synthetic `cand0` content.

**Step 3: Write minimal implementation**

```python
def build_tier1_search_corpus(...):
    # query local frozen corpus (deterministic lexical scoring)
    # emit top_k candidates with source, chunk_id, content_md, score
    # stable sort by (-score, source_rank, chunk_id)
```

**Step 4: Run test to verify it passes**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest packages/eval/tests/test_build_tier1_search_corpus.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/reports/build_tier1_search_corpus.py packages/eval/tests/test_build_tier1_search_corpus.py
git commit -m "fix(tier1): generate deterministic real retrieval corpus for search-augmented baseline"
```

---

### Task 5: Make `run_scheme_c_batch.sh` Truly Executable (No Placeholders)

**Files:**
- Modify: `scripts/reports/run_scheme_c_batch.sh`
- Modify: `docs/plans/2026-05-18-scheme-c-cloud-runbook.md`
- Create: `packages/eval/tests/test_run_scheme_c_batch_contract.py`
- Test: `packages/eval/tests/test_run_scheme_c_batch_contract.py`

**Step 1: Write failing tests for placeholder-free runnable chain**

```python
def test_batch_script_has_no_placeholder_echo_commands():
    text = Path("scripts/reports/run_scheme_c_batch.sh").read_text()
    assert "run catalyst sweep" not in text
    assert "command here" not in text

def test_batch_script_generates_stability_before_bundle():
    text = Path("scripts/reports/run_scheme_c_batch.sh").read_text()
    assert "run_direct_llm_stability_replay.py" in text
    assert "build_industrial_audit_bundle.py" in text
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c-hotfix
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest packages/eval/tests/test_run_scheme_c_batch_contract.py -v
```
Expected: FAIL due to sweep placeholders and missing stability generation.

**Step 3: Write minimal implementation**

```bash
# replace placeholders with real catalyst invocation commands
# generate:
#   catalyst_mt04_per_unit.jsonl (or mt045 output + alias)
#   direct_llm_stability_k3.json via run_direct_llm_stability_replay.py
# run three comparator outputs:
#   tier0 / tier1 / tier2 metrics json+csv+md
```

**Step 4: Run test to verify it passes**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest packages/eval/tests/test_run_scheme_c_batch_contract.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/reports/run_scheme_c_batch.sh docs/plans/2026-05-18-scheme-c-cloud-runbook.md packages/eval/tests/test_run_scheme_c_batch_contract.py
git commit -m "fix(ops): make scheme-c batch fully executable with real sweep and stability steps"
```

---

### Task 6: Full Regression Verification + Artifact Contract Check

**Files:**
- Modify: `docs/reports/2026-05-18-scheme-c-verification-checklist.md`

**Step 1: Run blocker-targeted tests**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c-hotfix
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest \
  packages/eval/tests/test_build_same_evidence_inputs.py \
  packages/eval/tests/test_compare_catalyst_vs_direct_llm_v3_tiers.py \
  packages/eval/tests/test_compute_attribution_metrics_v2.py \
  packages/eval/tests/test_compute_attribution_metrics_v2_contract_io.py \
  packages/eval/tests/test_build_tier1_search_corpus.py \
  packages/eval/tests/test_run_scheme_c_batch_contract.py -v
```
Expected: all PASS.

**Step 2: Run non-regression pack**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest \
  packages/eval/tests/test_run_direct_llm_frozen_baseline_v2_contract.py \
  packages/eval/tests/test_run_direct_llm_frozen_baseline_v3_modes.py \
  packages/eval/tests/test_run_direct_llm_stability_replay.py \
  packages/eval/tests/test_build_industrial_audit_bundle.py \
  packages/agents/tests/test_validator_magnitude_threshold_config.py -v
```
Expected: PASS.

**Step 3: Dry-run full batch script**

Run:
```bash
bash scripts/reports/run_scheme_c_batch.sh --dry-run
```
Expected: no placeholders; includes tier0/tier1/tier2 + sweep + stability + bundle.

**Step 4: Update verification checklist**

Add exact commands, outputs, and any residual risks to:
- `docs/reports/2026-05-18-scheme-c-verification-checklist.md`

**Step 5: Commit**

```bash
git add docs/reports/2026-05-18-scheme-c-verification-checklist.md
git commit -m "docs(eval): verify blocker hotfix pack and refresh scheme-c checklist"
```

---

## Done Criteria (Blocker Hotfix)

1. Tier2 same-evidence is built from real reranked evidence artifacts (not manifest placeholder fields).
2. Tier2 metrics are computed on eligible subset only, with explicit subset accounting fields.
3. Attribution-v2 consumes real per-case schemas and no longer relies on nonexistent `pred_*`/`gold_*` fields.
4. Tier1 corpus is derived from deterministic real retrieval, not synthetic `cand0`.
5. `run_scheme_c_batch.sh` has no placeholders and can execute complete chain (at least dry-run validated).


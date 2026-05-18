# Scheme C Eval Industrial Closure Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deliver a thesis-grade, industry-auditable evaluation stack that fairly compares EventUS pipeline vs Direct-LLM under three baselines (closed-book, search-augmented, same-evidence), fixes attribution KPI usability, calibrates magnitude threshold, and emits a fully reproducible freeze bundle.

**Architecture:** Extend the current frozen-eval scripts in `scripts/reports/` with mode-aware Direct runner, attribution-v2 metric computation, and fairness-aware comparator outputs. Keep Catalyst pipeline unchanged except for magnitude threshold calibration and observability aggregation from existing summary artifacts. All changes are TDD-first, minimal, and reversible.

**Tech Stack:** Python 3.12, pytest, JSON/JSONL/CSV/Markdown artifacts, OpenAI-compatible API client, existing Catalyst summary/trace outputs, frozen manifest pipeline.

---

### Task 0: Preflight Worktree + Guardrails

**Files:**
- Modify: `docs/plans/2026-05-18-scheme-c-eval-industrial-closure.md` (checklist updates during execution)

**Step 1: Create isolated worktree**

```bash
cd /Users/yiannischen/Desktop/Catalyst
git worktree add /Users/yiannischen/Desktop/Catalyst-scheme-c codex/scheme-c-eval-industrial-closure
```

**Step 2: Enter worktree and verify branch**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c
git branch --show-current
```
Expected: `codex/scheme-c-eval-industrial-closure`

**Step 3: Record required skills for execution**

Use:
- `@superpowers:executing-plans`
- `@superpowers:test-driven-development`
- `@superpowers:verification-before-completion`

**Step 4: Install/verify local test runtime**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_run_direct_llm_frozen_baseline_v2_contract.py -q
```
Expected: current baseline test passes.

**Step 5: Commit (plan execution checkpoint)**

```bash
git add -A
git commit -m "chore(plan): start scheme-c execution in isolated worktree"
```

---

### Task 1: Add Direct Runner v3 Contract Tests (3 baseline tiers)

**Files:**
- Create: `packages/eval/tests/test_run_direct_llm_frozen_baseline_v3_modes.py`
- Modify: `scripts/reports/run_direct_llm_frozen_baseline.py`

**Step 1: Write failing tests for new CLI and row schema**

```python
def test_cli_accepts_baseline_mode_and_search_fields():
    args = _parse_args([...,"--baseline-mode","closed_book"])
    assert args.baseline_mode == "closed_book"

def test_tier0_model_must_match_catalyst():
    row = _run_one_unit(
        ...,
        baseline_mode="closed_book",
        model="gemini-2.5-flash",
        catalyst_model="gemini-2.5-flash",
    )
    assert row["model"] == "gemini-2.5-flash"

def test_per_unit_row_includes_baseline_and_evidence_meta():
    row = _run_one_unit(...)
    assert row["baseline_mode"] in {"closed_book","search_augmented","same_evidence"}
    assert "model" in row
    assert "search_hits_count" in row
    assert "evidence_chunks_count" in row
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_run_direct_llm_frozen_baseline_v3_modes.py -v
```
Expected: FAIL (`--baseline-mode` and row fields missing).

**Step 3: Implement minimal CLI + output field scaffolding**

```python
parser.add_argument("--baseline-mode", choices=["closed_book","search_augmented","same_evidence"], default="closed_book")
parser.add_argument("--catalyst-model", required=True)
parser.add_argument("--search-corpus-jsonl")
parser.add_argument("--same-evidence-jsonl")
...
if args.baseline_mode == "closed_book" and args.model != args.catalyst_model:
    raise ValueError("Tier0 fairness violation: direct model must equal catalyst model")
record["baseline_mode"] = baseline_mode
record["model"] = model
record["search_hits_count"] = 0
record["evidence_chunks_count"] = 0
```

**Step 4: Run test to verify it passes**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_run_direct_llm_frozen_baseline_v3_modes.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add packages/eval/tests/test_run_direct_llm_frozen_baseline_v3_modes.py scripts/reports/run_direct_llm_frozen_baseline.py
git commit -m "test+feat(eval): add direct baseline v3 mode contracts"
```

---

### Task 1.5: Build Tier 1 Search Corpus (Deterministic, Data-Source Aligned)

**Files:**
- Create: `scripts/reports/build_tier1_search_corpus.py`
- Create: `packages/eval/tests/test_build_tier1_search_corpus.py`

**Step 1: Write failing tests for corpus schema + coverage**

```python
def test_tier1_corpus_has_required_fields(tmp_path):
    rows = build_corpus(...)
    assert {"case_id","profile","ticker","trade_date","query","search_candidates"} <= set(rows[0].keys())

def test_tier1_corpus_covers_all_frozen_units(tmp_path):
    rows = build_corpus(...)
    assert len(rows) == effective_n
```

**Step 2: Run tests to verify fail**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_build_tier1_search_corpus.py -v
```
Expected: FAIL (script missing).

**Step 3: Implement deterministic corpus builder**

```python
def build_tier1_search_corpus(...):
    # freeze-manifest aligned rows, stable sort by (case_id, profile, source_rank, chunk_id)
    return rows
```

**Step 4: Run tests to verify pass**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_build_tier1_search_corpus.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/reports/build_tier1_search_corpus.py packages/eval/tests/test_build_tier1_search_corpus.py
git commit -m "feat(eval): add deterministic tier1 search corpus builder"
```

---

### Task 2: Implement Search-Augmented Direct Baseline (Tier 1)

**Files:**
- Modify: `scripts/reports/run_direct_llm_frozen_baseline.py`
- Create: `packages/eval/tests/test_direct_baseline_search_augmented.py`

**Step 1: Write failing tests for search context injection**

```python
def test_search_mode_injects_topk_context():
    prompt, meta = _build_prompt_with_context(unit, baseline_mode="search_augmented", search_rows=[...])
    assert "Search Evidence" in prompt
    assert meta["search_hits_count"] > 0
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_direct_baseline_search_augmented.py -v
```
Expected: FAIL (`_build_prompt_with_context` missing).

**Step 3: Implement minimal deterministic search adapter**

```python
def _search_local_corpus(unit, corpus_rows, top_k=5):
    # date/ticker constrained lexical match; deterministic sort
    return matched[:top_k]

def _build_prompt_with_context(...):
    if baseline_mode == "search_augmented":
        hits = _search_local_corpus(...)
        prompt += "\nSearch Evidence:\n" + format_hits(hits)
```

Tier1 input source is mandatory:
- `--search-corpus-jsonl` must point to output of `build_tier1_search_corpus.py`
- if missing in `search_augmented` mode, fail-fast.

**Step 4: Run test to verify it passes**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_direct_baseline_search_augmented.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add packages/eval/tests/test_direct_baseline_search_augmented.py scripts/reports/run_direct_llm_frozen_baseline.py
git commit -m "feat(eval): add deterministic search-augmented direct baseline mode"
```

---

### Task 3: Implement Same-Evidence Direct Baseline (Tier 2)

**Files:**
- Create: `scripts/reports/build_same_evidence_inputs.py`
- Create: `packages/eval/tests/test_build_same_evidence_inputs.py`
- Modify: `scripts/reports/run_direct_llm_frozen_baseline.py`

**Step 1: Write failing test for same-evidence builder**

```python
def test_build_same_evidence_inputs_emits_case_profile_aligned_rows(tmp_path):
    out = build_same_evidence_inputs(...)
    assert out[0]["case_id"] == "g001"
    assert out[0]["profile"] == "full"
    assert len(out[0]["evidence_chunks"]) > 0

def test_same_evidence_only_keeps_evidence_nonempty_cases():
    rows = build_same_evidence_inputs(...)
    assert all(r["evidence_chunks_count"] > 0 for r in rows)
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_build_same_evidence_inputs.py -v
```
Expected: FAIL (script missing).

**Step 3: Implement minimal builder + runner hook**

```python
# build_same_evidence_inputs.py
row = {"case_id":..., "profile":..., "evidence_chunks":[{"chunk_id":..., "content_md":...}]}
# exclude cases with zero usable evidence; emit exclusion counters in metadata

# run_direct_llm_frozen_baseline.py
if baseline_mode == "same_evidence":
    prompt += "\nEvidence Chunks:\n" + format_chunks(evidence_chunks)
    record["evidence_chunks_count"] = len(evidence_chunks)
```

**Step 4: Run tests to verify pass**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  packages/eval/tests/test_build_same_evidence_inputs.py \
  packages/eval/tests/test_run_direct_llm_frozen_baseline_v3_modes.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/reports/build_same_evidence_inputs.py packages/eval/tests/test_build_same_evidence_inputs.py scripts/reports/run_direct_llm_frozen_baseline.py
git commit -m "feat(eval): add same-evidence direct baseline inputs and runner mode"
```

---

### Task 4: Add Attribution v2 Metric Script (category_f1 + semantic similarity)

**Files:**
- Create: `scripts/reports/compute_attribution_metrics_v2.py`
- Create: `packages/eval/tests/test_compute_attribution_metrics_v2.py`

**Step 1: Write failing tests for metric behavior**

```python
def test_category_f1_handles_set_overlap():
    assert _category_f1({"macro","earnings"},{"macro"}) == 2/3

def test_semantic_similarity_returns_nonzero_for_paraphrase():
    sim = _cause_semantic_sim(["tariff pause boosted risk appetite"], ["risk-on rally after tariff pause"])
    assert sim > 0.2
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_compute_attribution_metrics_v2.py -v
```
Expected: FAIL (script missing).

**Step 3: Implement minimal v2 metrics with deterministic defaults**

```python
def _category_f1(pred: set[str], gold: set[str]) -> float: ...
def _cause_semantic_sim(pred_texts: list[str], gold_texts: list[str]) -> float: ...
# default embedder: sentence-transformers if available; fallback lexical cosine
```

**Step 4: Run test to verify it passes**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_compute_attribution_metrics_v2.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/reports/compute_attribution_metrics_v2.py packages/eval/tests/test_compute_attribution_metrics_v2.py
git commit -m "feat(eval): add attribution-v2 metrics (category_f1 + cause_semantic_sim)"
```

---

### Task 5: Extend Comparator with Attribution + Fairness Tier Outputs

**Files:**
- Modify: `scripts/reports/compare_catalyst_vs_direct_llm.py`
- Create: `packages/eval/tests/test_compare_catalyst_vs_direct_llm_v3_tiers.py`

**Step 1: Write failing tests for tier-aware comparison**

```python
def test_comparator_outputs_tier_name_and_attribution_columns():
    out = compare(...)
    assert out["baseline_tier"] in {"tier0_closed_book","tier1_search_augmented","tier2_same_evidence"}
    assert "category_f1" in out["catalyst"]
    assert "cause_semantic_sim" in out["catalyst"]
    assert "tier2_eligible_n" in out
    assert "tier2_excluded_n" in out
    assert "tier2_exclusion_reason_counts" in out
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_compare_catalyst_vs_direct_llm_v3_tiers.py -v
```
Expected: FAIL (fields missing).

**Step 3: Implement minimal comparator v3 fields**

```python
p.add_argument("--baseline-tier", required=True)
p.add_argument("--attribution-v2-json")
...
result["baseline_tier"] = args.baseline_tier
result["catalyst"]["category_f1"] = attr["catalyst"]["category_f1"]
result["catalyst"]["cause_semantic_sim"] = attr["catalyst"]["cause_semantic_sim"]
result["tier2_eligible_n"] = ...
result["tier2_excluded_n"] = ...
result["tier2_exclusion_reason_counts"] = ...
# for tier2 output, set subset_eval=true
```

**Step 4: Run test to verify it passes**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_compare_catalyst_vs_direct_llm_v3_tiers.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/reports/compare_catalyst_vs_direct_llm.py packages/eval/tests/test_compare_catalyst_vs_direct_llm_v3_tiers.py
git commit -m "feat(eval): extend comparator with tier-aware attribution-v2 outputs"
```

---

### Task 6: Add Catalyst Cost/Latency Aggregator from Summaries

**Files:**
- Create: `scripts/reports/aggregate_catalyst_runtime_stats.py`
- Create: `packages/eval/tests/test_aggregate_catalyst_runtime_stats.py`
- Modify: `scripts/reports/compare_catalyst_vs_direct_llm.py`

**Step 1: Write failing test for summary aggregation**

```python
def test_aggregate_runtime_stats_from_summaries(tmp_path):
    stats = aggregate_runtime(...)
    assert stats["avg_latency_ms"] is not None
    assert stats["avg_total_tokens"] is not None
    assert stats["avg_total_cost_usd"] is not None
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_aggregate_catalyst_runtime_stats.py -v
```
Expected: FAIL (script missing).

**Step 3: Implement minimal aggregator + comparator merge**

```python
# aggregate_catalyst_runtime_stats.py
out = {"avg_latency_ms": ..., "avg_total_tokens": ..., "avg_total_cost_usd": ...}

# compare_catalyst_vs_direct_llm.py
p.add_argument("--catalyst-runtime-json")
```

**Step 4: Run tests to verify pass**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  packages/eval/tests/test_aggregate_catalyst_runtime_stats.py \
  packages/eval/tests/test_compare_catalyst_vs_direct_llm_v3_tiers.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/reports/aggregate_catalyst_runtime_stats.py packages/eval/tests/test_aggregate_catalyst_runtime_stats.py scripts/reports/compare_catalyst_vs_direct_llm.py
git commit -m "feat(eval): add catalyst runtime aggregation and wire into comparator"
```

---

### Task 7: Calibrate Magnitude Threshold with Configurable Validator Gate

**Files:**
- Modify: `packages/agents/catalyst_agents/nodes/validator.py`
- Modify: `packages/agents/catalyst_agents/nodes/critic.py`
- Create: `packages/agents/tests/test_validator_magnitude_threshold_config.py`

**Step 1: Write failing tests for configurable threshold**

```python
def test_validator_uses_threshold_override(monkeypatch):
    monkeypatch.setenv("CATALYST_M_THRESHOLD", "0.40")
    assert _effective_m_threshold() == 0.40
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/agents/tests/test_validator_magnitude_threshold_config.py -v
```
Expected: FAIL (no override hook).

**Step 3: Implement minimal threshold config hook**

```python
def _effective_m_threshold(default: float = M_THRESHOLD) -> float:
    raw = os.getenv("CATALYST_M_THRESHOLD")
    return float(raw) if raw else default
...
if decision.magnitude_coverage < _effective_m_threshold():
```

Then run calibration sweep on frozen cohort:
- `CATALYST_M_THRESHOLD=0.40`
- `CATALYST_M_THRESHOLD=0.45`
- default (`0.60`)

If no refusal/safety regression, freeze code default to calibrated conservative value:
- set `M_THRESHOLD=0.45`

Add lock test:

```python
def test_default_m_threshold_is_calibrated():
    from catalyst_agents.nodes.critic import M_THRESHOLD
    assert M_THRESHOLD == 0.45
```

**Step 4: Run tests to verify pass**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/agents/tests/test_validator_magnitude_threshold_config.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add packages/agents/tests/test_validator_magnitude_threshold_config.py packages/agents/catalyst_agents/nodes/validator.py packages/agents/catalyst_agents/nodes/critic.py
git commit -m "feat(validator): support configurable magnitude threshold for calibration runs"
```

---

### Task 8: Upgrade Audit Bundle Gates for Scheme C

**Files:**
- Modify: `scripts/reports/build_industrial_audit_bundle.py`
- Modify: `packages/eval/tests/test_build_industrial_audit_bundle.py`

**Step 1: Write failing tests for new quality gates**

```python
def test_bundle_checks_attribution_and_fair_baseline_gates():
    bundle = build_bundle(...)
    assert "attribution_v2_nonzero" in bundle["quality_gates"]
    assert "tier2_same_evidence_available" in bundle["quality_gates"]
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_build_industrial_audit_bundle.py -v
```
Expected: FAIL (new gates missing).

**Step 3: Implement minimal gate computation**

```python
quality_gates["attribution_v2_nonzero"] = (attr.get("catalyst",{}).get("cause_semantic_sim",0.0) > 0.0)
quality_gates["tier2_same_evidence_available"] = bool(metrics.get("baseline_tier") == "tier2_same_evidence")
```

**Step 4: Run test to verify it passes**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_build_industrial_audit_bundle.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/reports/build_industrial_audit_bundle.py packages/eval/tests/test_build_industrial_audit_bundle.py
git commit -m "feat(audit): add scheme-c quality gates to industrial bundle"
```

---

### Task 9: Add End-to-End Scheme C Smoke Test Contracts

**Files:**
- Create: `packages/eval/tests/test_scheme_c_pipeline_contract.py`
- Modify: `scripts/reports/build_thesis_freeze_manifest.py` (if additional fields required)

**Step 1: Write failing contract test**

```python
def test_scheme_c_artifact_set_and_schema(tmp_path):
    # assert expected files and key fields
    assert metrics["baseline_tier"] == "tier2_same_evidence"
    assert metrics["catalyst"]["avg_total_cost_usd"] is not None
    assert metrics["catalyst"]["cause_semantic_sim"] >= 0.0
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_scheme_c_pipeline_contract.py -v
```
Expected: FAIL.

**Step 3: Implement minimal schema harmonization**

```python
# ensure compare output, attribution output, and bundle output have stable keys/types
```

**Step 4: Run test to verify pass**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_scheme_c_pipeline_contract.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add packages/eval/tests/test_scheme_c_pipeline_contract.py scripts/reports/build_thesis_freeze_manifest.py scripts/reports/compare_catalyst_vs_direct_llm.py
git commit -m "test(eval): add scheme-c end-to-end artifact contract"
```

---

### Task 10: Cloud Execution Pack (Tier0/Tier1/Tier2 + Calibration Sweep)

**Files:**
- Create: `docs/plans/2026-05-18-scheme-c-cloud-runbook.md`
- Create: `scripts/reports/run_scheme_c_batch.sh`

**Step 1: Write failing runbook test (command integrity)**

```python
def test_runbook_references_existing_scripts_and_outputs():
    assert Path("scripts/reports/run_scheme_c_batch.sh").exists()
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest packages/eval/tests/test_scheme_c_pipeline_contract.py::test_runbook_references_existing_scripts_and_outputs -v
```
Expected: FAIL.

**Step 3: Implement run scripts (idempotent, resumable)**

```bash
# run_scheme_c_batch.sh includes:
# 1) build freeze manifest
# 2) run direct tier0 closed_book gemini
# 3) build tier1 corpus, then run direct tier1 search_augmented gemini
# 4) build same-evidence inputs + run tier2
# 5) rerun catalyst with CATALYST_M_THRESHOLD=0.40
# 6) compute attribution v2
# 7) aggregate runtime + compare + bundle
```

**Step 4: Run smoke execution locally (dry run)**

Run:
```bash
bash scripts/reports/run_scheme_c_batch.sh --dry-run
```
Expected: prints full command sequence with resolved paths.

**Step 5: Commit**

```bash
git add docs/plans/2026-05-18-scheme-c-cloud-runbook.md scripts/reports/run_scheme_c_batch.sh
git commit -m "docs+ops(eval): add scheme-c cloud execution runbook and batch runner"
```

---

### Task 11: Full Verification Before Handoff

**Files:**
- Modify: `docs/reports/2026-05-18-catalyst-vs-direct-llm-main-comparison.md` (append Scheme C section)
- Create: `docs/reports/2026-05-18-scheme-c-verification-checklist.md`

**Step 1: Run targeted tests by feature**

Run:
```bash
cd /Users/yiannischen/Desktop/Catalyst-scheme-c
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  packages/eval/tests/test_run_direct_llm_frozen_baseline_v3_modes.py \
  packages/eval/tests/test_direct_baseline_search_augmented.py \
  packages/eval/tests/test_build_same_evidence_inputs.py \
  packages/eval/tests/test_compute_attribution_metrics_v2.py \
  packages/eval/tests/test_compare_catalyst_vs_direct_llm_v3_tiers.py \
  packages/eval/tests/test_aggregate_catalyst_runtime_stats.py \
  packages/agents/tests/test_validator_magnitude_threshold_config.py \
  packages/eval/tests/test_build_industrial_audit_bundle.py \
  packages/eval/tests/test_scheme_c_pipeline_contract.py -v
```
Expected: all PASS.

**Step 2: Run pre-existing pre-cloud contracts (non-regression)**

Run:
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  packages/eval/tests/test_thesis_freeze_manifest_v2_contract.py \
  packages/eval/tests/test_direct_llm_pricing_lock.py \
  packages/eval/tests/test_run_direct_llm_frozen_baseline_v2_contract.py \
  packages/eval/tests/test_compare_catalyst_vs_direct_llm_v2_contract.py \
  packages/eval/tests/test_run_direct_llm_stability_replay.py \
  packages/eval/tests/test_extract_catalyst_node_observability.py \
  packages/eval/tests/test_build_industrial_audit_bundle.py -v
```
Expected: PASS.

**Step 3: Generate final artifact manifest**

Run:
```bash
python scripts/reports/build_industrial_audit_bundle.py \
  --freeze-id thesis-freeze-<final> \
  --metrics-json data/eval_reports/.../core/catalyst_vs_direct_*.json \
  --per-case-jsonl data/eval_reports/.../core/direct_*_per_unit*.jsonl \
  --stability-json data/eval_reports/.../core/direct_*_stability*.json \
  --adjudication-md docs/reports/2026-05-18-catalyst-vs-direct-llm-main-comparison.md \
  --out-json data/eval_reports/.../core/industrial_audit_bundle_v3.json
```
Expected: `[ok] wrote audit bundle`

**Step 4: Write verification checklist summary**

Include in `docs/reports/2026-05-18-scheme-c-verification-checklist.md`:
- exact commands
- pass/fail
- artifact paths
- known residual risks
- threshold calibration decision:
  - `m_threshold_default`
  - `m_threshold_sweep_results`
  - `selected_threshold_reason`

**Step 5: Commit**

```bash
git add docs/reports/2026-05-18-catalyst-vs-direct-llm-main-comparison.md docs/reports/2026-05-18-scheme-c-verification-checklist.md
git commit -m "docs(eval): finalize scheme-c verification and artifact checklist"
```

---

## Final Cloud Experiment Deliverables (Must Exist)

- `core/thesis_freeze_manifest.json`
- `core/catalyst_mt04_per_unit.jsonl` (or equivalent calibrated Catalyst outputs)
- `core/direct_closed_book_gemini_per_unit.jsonl`
- `core/direct_search_augmented_gemini_per_unit.jsonl`
- `core/direct_same_evidence_gemini_per_unit.jsonl`
- `core/attribution_metrics_v2.json`
- `core/catalyst_runtime_stats.json`
- `core/catalyst_vs_direct_tier0_metrics.json`
- `core/catalyst_vs_direct_tier1_metrics.json`
- `core/catalyst_vs_direct_tier2_metrics.json`
- `core/direct_llm_stability_k3.json`
- `core/industrial_audit_bundle_v3.json`
- `reports/2026-05-18-catalyst-vs-direct-llm-main-comparison.md`

## Acceptance Gates (Scheme C)

1. `status_accuracy` (Catalyst calibrated run) `>= 0.85`
2. `h_refusal` remains `8/8 INSUFFICIENT`, `0 SYSTEM_ERROR`
3. `category_f1 > 0` and `cause_semantic_sim > 0` (no all-zero attribution collapse)
4. Tier fairness ladder exists (Tier0/1/2 all generated)
5. Bundle gates:
   - `system_error_zero = true`
   - `stability_status_consistency_ge_0_95 = true`
   - `attribution_v2_nonzero = true`
   - `tier2_same_evidence_available = true`

## Notes

- Keep DRY/YAGNI: avoid changing legacy eval modules unless required by new tests.
- Any new CLI argument must be covered by a contract test.
- Do not rewrite historical artifacts in-place; emit versioned outputs and compare.

---

## Optional Hardening Tasks (P2, recommended if time allows)

### Task 12 (P2): Wire `magnitude_plausible` guard for h006-style fabricated magnitude

**Files:**
- Modify: `packages/agents/catalyst_agents/nodes/miner.py`
- Modify: `packages/agents/catalyst_agents/nodes/decision_router.py`
- Modify: `packages/agents/catalyst_agents/nodes/validator.py`
- Add tests:
  - `packages/agents/tests/test_magnitude_plausible_guard.py`

**Goal:**
- Ensure extreme fabricated moves (e.g., claimed 34% vs actual ~1-2%) are rejected deterministically.

### Task 13 (P2): Replace regex ticker extraction with whitelist-backed extraction

**Files:**
- Modify: `packages/agents/catalyst_agents/nodes/miner.py`
- Add tests:
  - `packages/agents/tests/test_ticker_extraction_whitelist.py`

**Goal:**
- Eliminate acronym false positives (CEO/FDA/SEC/IPO/EPS).
- Keep fixture-injected `query_ticker_raw` precedence for adversarial tests.

# S4: Regression Gate + Recalibration — Implementation Plan (AMENDED v3)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a pytest CI gate computing S1 metrics from committed eval evidence pack (zero live LLM), comparing against committed baseline with 0.05 absolute margin per metric. Rewrite check_p0_gate.py threshold table. Build recalibration procedure measuring config flips including A1_expand_macro_live (max_expansions: 0→2), producing ADR-012 with measured deltas.

**Architecture:** Gate reads from tracked packages/eval/eval_cache/ (judge_cache.json + per-case deterministic scores + s1_v1_baseline.json). Cache-or-fail: cache miss = test failure with dev instruction. Gate consumes answerable ∪ unanswerable cases. Judge cache key is (case_id, arm, metric, rubric_version, prompt_sha256). Recalibration runs three-arm experiment across config flips, writes human-reviewed ADR-012.

**Tech Stack:** Python 3.12+, pytest, json, sqlite3 (read-only).

**Design Refs (normative):** S4, §0.4, Decision Sheet items 10, #4.

**Process rule:** CLAUDE.md for writing-plans: strictly exclude all code blocks.

---

## Phase 0: Real-Run Verification Evidence

### 0.1: .gitignore blocks cache paths
.gitignore:11 — .local/ gitignored. .gitignore:18-19 — *.db/*.sqlite gitignored.
Both plan's .local/judge_cache/ and any .db/.sqlite cache are dead on fresh checkout. ✓

### 0.2: packages/eval layout
packages/eval/catalyst_eval/, golden_set/, scripts/, tests/ — no baselines/ or eval_cache/ yet. ✓

### 0.3: ADR-010 M_THRESHOLD
ADR-010-critic-k-sufficient-recalibration.md:18 — "M_THRESHOLD remains 0.6". The plan's "0.45 from ADR-010" was wrong. 0.45 is scheme-C env value — needs human sign-off (Decision #4). ✓

### 0.4: Current check_p0_gate.py
evidence_validity (>=0.95), schema_validity (==1.0), trace_completeness (==1.0), should_refuse_hit_rate (>=2/3), cost_latency_reported (==True). ✓

---

## Dependencies & CORE-vs-STRONG Boundary

- **Dependencies:** S1 (metrics + judge cache), S2 (PolicyConfig), S3 (corpus_items). S4 gates S1–S3.
- **CORE:** Phases 1–3.
- **STRONG:** Phase 4 (automated pipeline, no bootstrap-CI/paper stats).

---

## Phase 1: Evidentiary Pack + CI Gate (AMENDED — fix #12 blocking)

### Cache Relocation
All judge artifacts live at packages/eval/eval_cache/ (tracked directory, dodges .gitignore *.sqlite rule).

### Evidentiary Pack (committed to git)
- judge_cache.json: all LLM judge verdicts keyed by SHA-256(case_id || "::" || arm || "::" || metric || "::" || rubric_version || "::" || prompt)
- refusal_scores.json: per-case deterministic scores for refusal_correctness + direction_accuracy (no LLM needed; the gate replays from cache only)
- s1_v1_baseline.json: committed baseline with version, golden_set, rubric_version, metrics dict, per_case array

### Baseline JSON Schema
| Field | Type |
|---|---|
| version | str |
| golden_set | str |
| rubric_version | str |
| created_at | str (ISO-8601) |
| metrics | dict[str→float] |
| per_case | list[dict] |

### CI Gate Contract
Input: answerable_path (Path), unanswerable_path (Path), cache_dir (Path), baseline_path (Path), rubric_version (str), margin (float=0.05)
Output: GateResult(passed, metrics, baseline, deltas, failures)

Gate consumes BOTH answerable AND unanswerable cases. refusal_correctness is vacuous over answerable-only (all null scores).

Cache miss → AssertionError with dev instruction to run cache refresh. ZERO live LLM calls.

### METRIC_NAMES
cause_match_precision, cause_match_recall, cause_match_f1, citation_faithfulness, refusal_correctness, direction_accuracy

TEST INTENT:
1. Corrupted baseline → gate fails
2. Cache miss → fails with dev instruction
3. Cache-complete rerun → byte-stable
4. rubric_version bump → cache miss
5. All within 0.05 margin → gate passes
6. **Fresh checkout: eval_cache/ committed, gate runs without LLM calls**

**Files:**
- Create: packages/eval/eval_cache/ (with .gitkeep, judge_cache.json placeholder, s1_v1_baseline.json placeholder)
- Create: catalyst_eval/harness/s4_gate.py
- Create: catalyst_eval/tests/test_s4_regression_gate.py

---

## Phase 2: Rewrite check_p0_gate.py

### GATE_THRESHOLDS

| Gate | Operator | Value | Type |
|---|---|---|---|
| evidence_validity | >= | 0.95 | structural |
| schema_validity | == | 1.0 | structural |
| trace_completeness | == | 1.0 | structural |
| cause_match_precision | >= | 0.50 | behavioral |
| cause_match_recall | >= | 0.50 | behavioral |
| cause_match_f1 | >= | 0.50 | behavioral |
| citation_faithfulness | >= | 0.50 | behavioral |
| refusal_correctness | >= | 0.50 | behavioral |
| direction_accuracy | >= | 0.50 | behavioral |
| cost_latency_reported | == | True | structural |

### Deleted
temporal_precision, grounding_rate, attribution_f1, should_refuse_hit_rate

TEST INTENT: new names in, old names out, structural gates preserved.

**Files:**
- Modify: catalyst_eval/scripts/check_p0_gate.py
- Create: catalyst_eval/tests/test_s4_check_p0_gate.py

---

## Phase 3: Recalibration Procedure + ADR-012

**Design refs:** S4, Decision Sheet items 1, 2, #4.

### Config Flips

| Flip | Mechanism | v1 | v2 candidate |
|---|---|---|---|
| A1_expand_macro_live | max_expansions | 0 | 2 |
| A5_hard_temporal_gate | temporal_mode | "soft" | "hard" |
| tier_weight_balanced | tier_weights | all 1.0 | weighted (human-provided) |
| K_recalibrated | K_sufficient, K_partial, M_threshold | 1, 1, 0.3 | human-provided |

### M_threshold Note (AMENDED)
ADR-010 keeps M_threshold=0.6. The plan's "0.45" was scheme-C env value. v2 candidate values are NOT hardcoded here — they come from human sign-off (Decision #4). The recalibration script ACCEPTS candidate configs as input and measures deltas.

### recalibrate.py Contract
Input: config flips (list of PolicyConfig variants), golden paths, arms
Output: docs/ADR/ADR-012-policy-v2-recalibration.md
Process: baseline run (v1) → per-flip run → delta computation → ADR-012 generation

### ADR-012 Schema
- Baseline Metrics table (v1_3, three-arm, frozen plane)
- Per-flip delta tables: metric, baseline, candidate, delta
- Decision section with human-approved PolicyConfig.v2() values

### Deleted
Bootstrap-CI/significance-test step (design excludes paper-grade stats per original review).

### Path Reconciliation (AMENDED)
Baselines live at packages/eval/eval_cache/s1_v1_baseline.json (consistent with judge cache path). Packages are at packages/eval/, NOT catalyst_eval/baselines/.

### Golden Set Naming (AMENDED — fix #13 should-fix)
The 50-case file renamed to v1_3_answerable.jsonl (not v1_3_p0_set.jsonl). The unanswerable file is v1_3_unanswerable.jsonl. Gate consumes both.

TEST INTENT:
1. recalibrate.py runs without error, produces ADR-012
2. ADR-012 contains all four config flips with measured deltas
3. A1_expand_macro_live flip: max_expansions 0→2
4. M_threshold candidate NOT hardcoded (human-provided)
5. Baseline path is packages/eval/eval_cache/s1_v1_baseline.json

**Files:**
- Create: catalyst_eval/scripts/recalibrate.py
- Create: docs/ADR/ADR-012-policy-v2-recalibration.md (generated)
- Modify: catalyst_agents/policy_config.py (PolicyConfig.v2() after human sign-off)

---

## Phase 4 (STRONG): Automated Recalibration Pipeline

Wrap recalibrate.py. No bootstrap-CI. No paper-grade stats.

---

## Post-Implementation Verification

1. pytest packages/eval/tests/test_s4_regression_gate.py -v
2. pytest packages/eval/tests/test_s4_check_p0_gate.py -v
3. Fresh checkout: eval_cache/ committed, gate runs zero LLM
4. Cache key includes rubric_version
5. Corrupted baseline → CI red
6. ADR-012 exists with measured deltas
7. A1_expand_macro_live: max_expansions 0→2
8. s1_v1_baseline.json at packages/eval/eval_cache/

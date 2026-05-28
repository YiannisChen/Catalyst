# Chapter 4 (Thesis) Rewrite Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rewrite Chapter 4 so the primary claims are anchored on manual-review attribution quality (`n=99` common set), with P1-scale reliability/ablation as supporting evidence, and every reported number/figure is traceable to frozen experiment artifacts.

**Architecture:** Track A first (tier1→tier2→EventUS on `n=99` + manual review), then Track B (P1 200-case reliability + ablations + taxonomy + H-refusal). Keep denominators explicit and stable; avoid mixing cohorts in the same table. Figures are few, readable, and each has an explicit data source path.

**Tech Stack:** Markdown draft file for paste (`docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md`), frozen artifacts under `docs/thesis/Deliverables` and `docs/reports/*`, optional Python scripts for generating Chapter 4 charts (matplotlib) and deterministic CSV extracts.

---

## Canonical Inputs (Do Not Edit; Treat As Source of Truth)

Track A (manual review, common set `n=99`):
- `/Users/yiannischen/Desktop/Catalyst/docs/thesis/Deliverables/2026-05-20-ch4_manual_metrics_frozen_v1.json`
- `/Users/yiannischen/Desktop/Catalyst/docs/thesis/Deliverables/2026-05-20-ch4_attribution_manual_scoring_report.md`
- `/Users/yiannischen/Desktop/Catalyst/docs/thesis/Deliverables/2026-05-20-ch4_attribution_review_sheet.csv`
- `/Users/yiannischen/Desktop/Catalyst/docs/thesis/Deliverables/2026-05-20-ch4_casewise_side_by_side.csv`

Track B (P1/R1 runs):
- `/Users/yiannischen/Desktop/Catalyst/docs/reports/2026-05-15-catalyst-metrics-summary.csv`
- `/Users/yiannischen/Desktop/Catalyst/docs/reports/2026-05-15-catalyst-error-taxonomy.csv`
- `/Users/yiannischen/Desktop/Catalyst/docs/reports/2026-05-15-r0-baseline-addendum.md`
- `/Users/yiannischen/Desktop/Catalyst/docs/reports/2026-05-16-h-refusal-results.md`
- `/Users/yiannischen/Desktop/Catalyst/docs/reports/2026-05-17-r1-external-baseline-results-recheck.md`

Chapter 4 package (paths + intersection rule + quick checks):
- `/Users/yiannischen/Desktop/Catalyst/docs/reports/ch4_experiment_package_2026-05-20/direct_vs_catalyst_canonical_notes.md`
- `/Users/yiannischen/Desktop/Catalyst/docs/reports/ch4_experiment_package_2026-05-20/key_metrics_snapshot.json`
- `/Users/yiannischen/Desktop/Catalyst/docs/reports/ch4_experiment_package_2026-05-20/source_paths.txt`

## Output Targets

Primary chapter draft to rewrite:
- Modify: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md`

Optional chart assets (if the thesis template expects image files):
- Create: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/figures/ch4/` (PNG/PDF)
- Create: `/Users/yiannischen/Desktop/Catalyst/scripts/thesis/ch4_extract_tables.py`
- Create: `/Users/yiannischen/Desktop/Catalyst/scripts/thesis/ch4_make_figures.py`

## Task 1: Freeze The Chapter-4 Contract (Denominators + Naming)

**Files:**
- Modify: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md`

**Step 1: Insert an explicit "口径说明" paragraph at the start of 4.1**
- Must define:
  - G-set universe: `50 cases × {full,no_vector} = 100 units`
  - Common set: `intersection on (case_id, profile) across EventUS + tier0/1/2 => n=99`
  - Manual-review denominators: `total_n=99`, `eligible_attr_n=93`
- Must include tier definitions in 1 sentence each:
  - tier0: closed-book
  - tier1: search-augmented
  - tier2: same-evidence
  - EventUS: full workflow

**Step 2: Add a hard sentence that StatusAcc is secondary**
- Example requirement: “状态判定正确率不等价于归因质量；本章主结论以人工复判归因指标为准。”

**Step 3: Verify the numbers appear exactly once and remain consistent**
Run: `rg -n \"n=99|eligible_attr_n|93|100 单元|50 个\" docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md`
Expected: a small number of hits, each consistent.

**Step 4: Commit**
```bash
git add docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md
git commit -m "thesis: lock ch4 cohort contracts and tier definitions"
```

## Task 2: Rewrite 4.2 Metrics To Be A-First (Manual Metrics First)

**Files:**
- Modify: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md`
- Reference: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/04_rules/62_metrics_definitions_and_formulas.md`

**Step 1: Move manual metrics to the front and make them the “主指标”**
- Keep only:
  - AttrCorrectRate (denominator = eligible_attr_n)
  - EffectiveAttrRate (denominator = total_n)

**Step 2: Demote StatusAcc + refusal + cost/latency to “辅助指标”**
- Keep short definitions, defer detailed discussion to Track B.

**Step 3: Ensure formulas are written as text (not images)**
- Replace any image placeholders with Word-equation friendly LaTeX-like or plain math expressions (final Word step later).

**Step 4: Commit**
```bash
git add docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md
git commit -m "thesis: reorganize ch4 metrics to be manual-first"
```

## Task 3: Build Table 4-2 (Track A Main Table) From Frozen Manual Metrics

**Files:**
- Modify: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md`
- Reference: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/Deliverables/2026-05-20-ch4_attribution_manual_scoring_report.md`
- Reference: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/Deliverables/2026-05-20-ch4_manual_metrics_frozen_v1.json`

**Step 1: Replace the existing main table with a three-line Table 4-2**
- Methods: tier1 / tier2 / EventUS only
- Columns order:
  1) 归因正确率（7/93, 34/93, 51/93）
  2) 有效归因率（10/99, 47/99, 87/99）
  3) 状态判定正确率（可小列或脚注）
- Caption above table must include: `共同样本 n=99（eligible=93）` and the frozen artifact name.

**Step 2: Add one paragraph of interpretation in the fixed order**
- tier1→tier2 = evidence supply effect
- tier2→EventUS = governance effect under evidence parity

**Step 3: Add a one-sentence tier0 mention in 4.3.2**
- Not in the table; include its StatusAcc as a closed-book lower bound if needed.

**Step 4: Commit**
```bash
git add docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md
git commit -m "thesis: add ch4 Table 4-2 from frozen manual metrics"
```

## Task 4: Add Manual Error-Type Distribution Table (Explains “Why Better”)

**Files:**
- Modify: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md`
- Reference: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/Deliverables/2026-05-20-ch4_attribution_manual_scoring_report.md`

**Step 1: Create Table 4-3 (or 4-X) with error-type counts**
- Include: OK / WRONG_EVENT / MISSING_EVENT / OVER_REFUSAL / UNDER_REFUSAL / VAGUE / FORMAT_FAIL
- One short paragraph: interpret the dominant failure modes per method (e.g., tier1 mostly OVER_REFUSAL; tier2 VAGUE+WRONG_EVENT; EventUS reduces OVER_REFUSAL but still has WRONG_EVENT).

**Step 2: Commit**
```bash
git add docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md
git commit -m "thesis: add ch4 manual error-type distribution table"
```

## Task 5: Case Analysis Package (Overview Table + 3 Short Cases)

**Files:**
- Modify: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md`
- Reference: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/Deliverables/2026-05-20-ch4_casewise_side_by_side.csv`
- Reference: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/Deliverables/2026-05-20-ch4_attribution_review_sheet.csv`

**Step 1: Add an “overview comparison table” before the case paragraphs**
- 3 cases × 3 methods (tier1/tier2/EventUS), with:
  - 股票/日期
  - 标准答案状态
  - Golden 主因（短语）
  - 三方法输出状态
  - 一句话差异说明

**Step 2: Rewrite 3 case paragraphs to be short and structured**
- Each case must include: expected, tier1 failure type, tier2 failure type (if any), EventUS behavior, and why it is more auditable.

**Step 3: Commit**
```bash
git add docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md
git commit -m "thesis: rewrite ch4 case analysis with overview table"
```

## Task 6: Track B Part 1 (P1 200-case Reliability Summary)

**Files:**
- Modify: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md`
- Reference: `/Users/yiannischen/Desktop/Catalyst/docs/reports/2026-05-15-catalyst-metrics-summary.csv`
- Reference: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/02_materials/40_ch4_materials.md` (run tags)

**Step 1: Add a compact table for P1 profile summary**
- Keep it small: full / no_vector / no_rerank / degraded
- Explicitly label denominator: `P1: n=200` and “旁证（非主指标）”.

**Step 2: One paragraph: what this supports**
- Supports: stability under ablations and degraded settings; does not replace manual quality results.

**Step 3: Commit**
```bash
git add docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md
git commit -m "thesis: add ch4 Track B P1 reliability summary"
```

## Task 7: Track B Part 2 (Ablations + Threshold Notes)

**Files:**
- Modify: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md`
- Reference: `/Users/yiannischen/Desktop/Catalyst/docs/reports/ch4_experiment_package_2026-05-20/key_metrics_snapshot.json`

**Step 1: Vector ablation paragraph (full vs no_vector)**
- Keep claim scoped: contributes to routing/status stability; attribution-quality uplift still comes from governance (already proven in Track A).

**Step 2: Rerank ablation paragraph (no_rerank)**
- Use P1 scale numbers only; avoid mixing with `n=99`.

**Step 3: Threshold (`M_THRESHOLD`) sensitivity paragraph**
- Explicitly label this as a policy parameter; cite the run/tag where the comparison comes from.

**Step 4: Commit**
```bash
git add docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md
git commit -m "thesis: add ch4 Track B ablations and threshold notes"
```

## Task 8: Track B Part 3 (Failure Taxonomy: Diagnosability Evidence)

**Files:**
- Modify: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md`
- Reference: `/Users/yiannischen/Desktop/Catalyst/docs/reports/2026-05-15-catalyst-error-taxonomy.csv`

**Step 1: Add one small table or bullet summary of first-failure distribution**
- Goal: show failures concentrate in a few actionable buckets (Critic gate, coverage gap, retrieval/rerank, etc.).

**Step 2: Commit**
```bash
git add docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md
git commit -m "thesis: add ch4 failure taxonomy evidence for diagnosability"
```

## Task 9: Robustness (H-refusal Evolution) + Negative Finding (External Consistency)

**Files:**
- Modify: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md`
- Reference: `/Users/yiannischen/Desktop/Catalyst/docs/reports/2026-05-16-h-refusal-results.md`
- Reference: `/Users/yiannischen/Desktop/Catalyst/docs/reports/2026-05-17-r1-external-baseline-results-recheck.md`

**Step 1: Add the H-refusal evolution table (small)**
- Interpret it as “可诊断/可修复”，not as the main quality result.

**Step 2: Add external consistency as “保留的负向发现” in limitations**
- Numbers: aligned_pairs / Pearson / Spearman / bucket agreement.

**Step 3: Commit**
```bash
git add docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md
git commit -m "thesis: add ch4 robustness and retained negative finding"
```

## Task 10: Figures (Chart Plan + Generation + Captions)

**Files:**
- Create: `/Users/yiannischen/Desktop/Catalyst/scripts/thesis/ch4_extract_tables.py`
- Create: `/Users/yiannischen/Desktop/Catalyst/scripts/thesis/ch4_make_figures.py`
- Create: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/figures/ch4/fig4-1_effective_attr.png`
- Create: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/figures/ch4/fig4-2_p1_profiles.png`
- Create: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/figures/ch4/fig4-3_taxonomy.png`
- Modify: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md`

**Step 1: Implement `ch4_extract_tables.py` to output deterministic CSV for plots**
- Inputs: frozen deliverables + reports paths listed above.
- Outputs: `docs/thesis/figures/ch4/*.csv` (one per figure).

**Step 2: Implement `ch4_make_figures.py` using matplotlib**
- Produce 3 simple bar charts (large fonts, minimal legends, A4-friendly).

**Step 3: Run generation and sanity-check**
Run:
```bash
python scripts/thesis/ch4_extract_tables.py
python scripts/thesis/ch4_make_figures.py
ls -la docs/thesis/figures/ch4
```
Expected: 3 PNG files and their input CSVs.

**Step 4: Insert figure references + captions in Chapter 4**
- Every figure caption must include the exact frozen input file path (or the derived CSV path generated in Step 1).

**Step 5: Commit**
```bash
git add scripts/thesis/ch4_extract_tables.py scripts/thesis/ch4_make_figures.py docs/thesis/figures/ch4 docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md
git commit -m "thesis: add ch4 figures with frozen-data extraction"
```

## Task 11: Final Consistency Pass (导师批注 Checklist)

**Files:**
- Modify: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md`

**Step 1: Enforce “no orphan figure/table” rule**
Run:
```bash
rg -n \"图4-|表4-\" docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md
```
Expected: every table/figure is referenced in surrounding paragraphs and has a caption.

**Step 2: Enforce denominator labeling**
- Every rate must mention its denominator at least once in the section (e.g., `n=99 (eligible=93)`).

**Step 3: Commit**
```bash
git add docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md
git commit -m "thesis: ch4 consistency pass for captions, denominators, and references"
```

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-05-21-ch4-rewrite-implementation-plan.md`. Two execution options:

1. Subagent-Driven (this session) - I dispatch fresh subagent per task, review between tasks, fast iteration
2. Parallel Session (separate) - Open new session with executing-plans, batch execution with checkpoints

Which approach?


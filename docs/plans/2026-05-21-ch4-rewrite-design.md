# 2026-05-21 Chapter 4 Rewrite Design (A2, A-first then B)

## Scope

Rewrite thesis Chapter 4 "实验设计与结果分析" with a dual-track narrative:

- Track A (primary, written first): attribution quality on the **3-method common set** (`n=99`) with **manual review** as the main evidence.
- Track B (secondary, written after A): reliability + ablation + robustness as supporting evidence, primarily from **P1 200-case** runs, with the **G-set 100 units** used only as a bridge to explain how `n=99` is constructed.

Non-goals:

- Do not re-argue Chapter 3 system design; Chapter 4 should only cite mechanisms when tied to measurable results.
- Do not treat `StatusAcc` as the primary quality metric; it is an auxiliary metric for routing reliability.

## Decisions Locked In

- Main methods in Chapter 4 are named as: `tier1 / tier2 / EventUS` (not the long engineering identifiers).
- Main table (Table 4-2) includes **tier1, tier2, EventUS only**; `tier0` is mentioned briefly in text (as the closed-book lower bound).
- Track A narrative order is: `tier1 → tier2 → EventUS` (evidence supply first, then governance gain under evidence parity).
- Tables must be three-line style; captions above tables/figures; formulas use Word equation editor (no screenshots).

## Canonical Artifacts (Source of Truth)

Use these frozen artifacts as the *only* authoritative sources for Chapter 4 numbers.

- Track A (manual review on common set `n=99`):
  - Manual scoring summary: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/Deliverables/2026-05-20-ch4_attribution_manual_scoring_report.md`
  - Manual metrics freeze (for Table 4-2 values): `/Users/yiannischen/Desktop/Catalyst/docs/thesis/Deliverables/2026-05-20-ch4_manual_metrics_frozen_v1.json`
  - Review sheet (row-level traceability): `/Users/yiannischen/Desktop/Catalyst/docs/thesis/Deliverables/2026-05-20-ch4_attribution_review_sheet.csv`
  - Side-by-side per (case_id, profile) comparison: `/Users/yiannischen/Desktop/Catalyst/docs/thesis/Deliverables/2026-05-20-ch4_casewise_side_by_side.csv`

- Track B (P1/R1 reliability, ablation, robustness):
  - P1 summary metrics: `/Users/yiannischen/Desktop/Catalyst/docs/reports/2026-05-15-catalyst-metrics-summary.csv`
  - Failure taxonomy: `/Users/yiannischen/Desktop/Catalyst/docs/reports/2026-05-15-catalyst-error-taxonomy.csv`
  - R0 addendum (refusal metrics definitions): `/Users/yiannischen/Desktop/Catalyst/docs/reports/2026-05-15-r0-baseline-addendum.md`
  - H-refusal: `/Users/yiannischen/Desktop/Catalyst/docs/reports/2026-05-16-h-refusal-results.md`
  - External consistency (negative finding): `/Users/yiannischen/Desktop/Catalyst/docs/reports/2026-05-17-r1-external-baseline-results-recheck.md`

- Chapter 4 "canonical package" (paths, intersection rules, repricing, quick highlights):
  - `/Users/yiannischen/Desktop/Catalyst/docs/reports/ch4_experiment_package_2026-05-20/direct_vs_catalyst_canonical_notes.md`
  - `/Users/yiannischen/Desktop/Catalyst/docs/reports/ch4_experiment_package_2026-05-20/key_metrics_snapshot.json`
  - `/Users/yiannischen/Desktop/Catalyst/docs/reports/ch4_experiment_package_2026-05-20/source_paths.txt`

## Frozen Numbers To Use (Do Not Recompute In-Text)

These values are already frozen in the deliverables and should be copied consistently.

- Common set size: `common_n = 99` (intersection on `(case_id, profile)` across EventUS + tier0/1/2).
- Manual review denominators:
  - `total_n = 99`
  - `eligible_attr_n = 93` (gold expected status != INSUFFICIENT; per manual report)
- Manual metrics (Table 4-2, Track A main evidence):
  - tier1: AttrCorrect `0.0753 (7/93)`, Effective `0.1010 (10/99)`
  - tier2: AttrCorrect `0.3656 (34/93)`, Effective `0.4747 (47/99)`
  - EventUS: AttrCorrect `0.5484 (51/93)`, Effective `0.8788 (87/99)`
- Status accuracy on the same common set (auxiliary, not primary):
  - tier1 `0.1818`, tier2 `0.8081`, EventUS `0.8384`

## Definitions (What Must Be Explained In Text)

### Tier Definitions (one-sentence, evidence difference only)

- `tier0` (closed-book direct): single-call LLM answer with **no evidence context injected**.
- `tier1` (search-augmented direct): single-call LLM answer with **retrieval/search snippets appended** as context.
- `tier2` (same-evidence direct): single-call LLM answer with **the same evidence chunks as EventUS** injected (evidence parity baseline).
- `EventUS` (full workflow): retrieval + Critic sufficiency gating + routing + generation + post-validation + final status normalization.

### Why `StatusAcc` Is Secondary

Chapter 4 must explicitly state:

- `StatusAcc` measures routing correctness, not attribution quality.
- Main conclusions about "解释质量" must be anchored on manual review metrics (AttrCorrect, Effective).

## Chapter Outline (Rewrite Target)

Target file to rewrite later:

- `/Users/yiannischen/Desktop/Catalyst/docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md`

### 4.1 Environment and Datasets (minimal but precise)

- Provide hardware + OS + key software versions.
- Define two cohorts and their roles:
  - G-set: 50 cases × {full,no_vector} = 100 units; used to build the 3-method common set (be explicit this is the upstream universe for `n=99`).
  - P1: 200 cases × multiple profiles; used for stability/ablation/taxonomy (Track B).
- Explain the relationship: `100 units → intersection → n=99`.

### 4.2 Metrics (A-first)

- Define manual metrics first:
  - AttrCorrectRate: denominator = `eligible_attr_n`.
  - EffectiveAttrRate: denominator = `total_n`.
- Provide `StatusAcc` and refusal metrics as auxiliary, deferred to Track B discussion.

### 4.3 Track A: Attribution Quality (primary)

Mandatory deliverables:

- Table 4-2: tier1 vs tier2 vs EventUS (common set `n=99`), with manual metrics as the first columns.
- A narrative with the fixed order:
  - tier1 → tier2: evidence supply effect.
  - tier2 → EventUS: governance effect under evidence parity.
- Add a compact "manual error type distribution" table (use the error counts from manual scoring report).
- Case analysis:
  - Start with a concise overview table (3 cases × 3 methods).
  - Then 3 short case paragraphs (no long quotes).
- Mention tier0 in one sentence only (closed-book lower bound; conservative refusal tendency).

### 4.4 Track B: Reliability, Ablation, Robustness (secondary)

Minimum set to cover:

- P1 200-case: profile-level summary (full/no_vector/no_rerank/degraded).
- Component ablations:
  - vector on/off, rerank on/off, threshold (`M_THRESHOLD`) sensitivity.
- Failure taxonomy:
  - "first failure point" distribution, and how this supports diagnosability/iterability.
- H-refusal evolution:
  - include the 3-round table, and interpret it as "stability/repairability", not as the primary quality result.
- External consistency:
  - keep as negative finding in limitations.

### 4.5 Threats and Limitations (short, hard)

- Manual review subjectivity and controls (rules, traceability to the sheet).
- `n=99` intersection bias and limited generalization.
- External consistency misalignment as a retained negative result.

### 4.6 Summary (<= 4 bullets)

- Must restate the Track A conclusion first (manual metrics).
- Track B is supporting evidence only.

## Figures and Tables Plan (To Avoid Redundancy)

All figures/tables must have:

- Caption above; three-line tables; numbered references in正文 (导师要求).
- A "Data source" note either in caption tail or in the paragraph immediately before the figure/table, pointing to the canonical artifact path.

Planned items:

- Table 4-1: environment + key hyperparameters (short, stable).
- Table 4-2 (main): manual metrics on `n=99` (tier1/tier2/EventUS).
- Table 4-3: manual error type counts (OK/WRONG_EVENT/VAGUE/OVER_REFUSAL/...).
- Table 4-4: P1 profile summary (200-case; keep compact).
- Figure 4-1: bar chart of EffectiveAttrRate (tier1 vs tier2 vs EventUS) on `n=99`.
- Figure 4-2: ablation profile breakdown (full/no_vector/no_rerank/degraded) on P1.
- Figure 4-3: failure taxonomy distribution (stacked bar or simple bar).
- Table/Figure 4-X: H-refusal evolution (v1/v2/guardrail) as robustness appendix-level in main text (keep small).

## Data Verification Checklist (Before Writing)

- Confirm `common_n=99` and intersection rule from:
  - `/Users/yiannischen/Desktop/Catalyst/docs/reports/ch4_experiment_package_2026-05-20/direct_vs_catalyst_canonical_notes.md`
- Confirm Table 4-2 numbers match:
  - `/Users/yiannischen/Desktop/Catalyst/docs/thesis/Deliverables/2026-05-20-ch4_attribution_manual_scoring_report.md`
- Ensure `n=99 / eligible=93` appears consistently in:
  - Table 4-2 caption, manual metric definitions, and Chapter 4 summary.
- Ensure any additional denominators (`n=100`, `n=200`) are clearly labeled as Track B only.


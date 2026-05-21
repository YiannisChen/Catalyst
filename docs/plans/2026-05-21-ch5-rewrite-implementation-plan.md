# Chapter 5 Rewrite (Results-First) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rewrite thesis Chapter 5 (Conclusion & Outlook) to be results-first, anchored by Chapter 4 frozen metrics and figure references, with Chinese-first wording and minimal engineering jargon.

**Architecture:** Replace the existing 5.1/5.2 narrative with a 3-paragraph results-first conclusion and a 3-item actionable outlook; ensure all claims are backed by Chapter 4 tables/figures (Table 4-2/4-3, Fig 4-1~4-4) and remove component name lists (Miner/Critic/Judge...) in favor of functional descriptions.

**Tech Stack:** Markdown thesis drafts under `docs/thesis/01_final_for_paste/`.

---

### Task 1: Snapshot Current Chapter 5 & Chapter 4 Anchors

**Files:**
- Read: `docs/thesis/01_final_for_paste/50_ch5_conclusion_and_outlook_draft_v1.md`
- Read: `docs/thesis/01_final_for_paste/40_ch4_experiment_design_and_results_draft_v4.md`

**Step 1: Extract the exact “hard anchors” to quote in Chapter 5**
- Common set: `n=99`, `N_eligible=93`
- Main results (Table 4-2 / Fig 4-1): tier1/tier2/EventUS metrics + deltas vs tier2
- Error-type structure (Table 4-3): over-refusal drop and wrong-event boundary
- Critic tradeoff (Fig 4-3): -11.0pp status + cost deltas
- Refusal comparison (Fig 4-4): precision/hit-rate deltas

**Step 2: Decide Chapter 5 reference style**
- Use “见表4-2、图4-1” inline citations (no filenames/paths/metric keys).

---

### Task 2: Rewrite 5.1 Conclusion into 3 Results-First Paragraphs

**Files:**
- Modify: `docs/thesis/01_final_for_paste/50_ch5_conclusion_and_outlook_draft_v1.md`

**Step 1: Replace the current 5.1 opening with a metric-anchored paragraph**
- Must include: `n=99` / `N_eligible=93`
- Must include: EventUS vs tier2 delta: `+18.3pp` attribution correctness, `+40.4pp` effective attribution
- Must include: one sentence stating status accuracy is auxiliary
- Must end with: “见表4-2、图4-1”

**Step 2: Replace the current “component listing” paragraph**
- Remove explicit list: Miner/Critic/Judge/Validator/Finalizer
- Replace with functional phrasing:
  - evidence mining/retrieval
  - sufficiency judgment
  - state routing / refusal as explicit decision
  - generation under evidence constraints
  - post-hoc validation
  - state convergence + trace/summary assets
- Tie back to why tier2→EventUS (same-evidence) improves usability (Table 4-3 framing)

**Step 3: Add a boundary + tradeoff paragraph**
- Boundary: main residual failure is “主因错配”（Table 4-3)
- Robustness: refusal metrics better than tier2 (Fig 4-4)
- Tradeoff: Critic has method value but current toggle shows cost/benefit not yet optimal (Fig 4-3)
- Include: “后续需补齐 EventUS 按样本粒度的令牌/时延/成本采集”

---

### Task 3: Rewrite 5.2 Outlook into 3 Actionable Items

**Files:**
- Modify: `docs/thesis/01_final_for_paste/50_ch5_conclusion_and_outlook_draft_v1.md`

**Step 1: Merge “主因对齐” + “证据覆盖/时间对齐” into one item**
- Provide 2 sub-directions in a single paragraph:
  - event-level reranking + cross-evidence consistency
  - source whitelist expansion + time-window refinement + cross-source alignment

**Step 2: Add “engineering cost + stability” item**
- per-unit cost instrumentation
- caching, parallelism, trigger policy tightening, recovery

**Step 3: Add “long-term evaluation” item**
- periodic regression set
- drift monitoring + alerts

---

### Task 4: Chapter 5 Sanity Checks (Style & Traceability)

**Files:**
- Check: `docs/thesis/01_final_for_paste/50_ch5_conclusion_and_outlook_draft_v1.md`

**Step 1: Ensure Chapter 5 contains no file names, paths, or metric key strings**
Run:
```bash
rg -n "\\.json\\b|\\.csv\\b|\\.md\\b|refusal_precision|should_refuse_hit_rate|attribution_f1|\\btoken\\b|\\bDirect\\b|\\bdirect\\b" docs/thesis/01_final_for_paste/50_ch5_conclusion_and_outlook_draft_v1.md
```
Expected: no matches.

**Step 2: Ensure all quantitative claims point to Chapter 4 tables/figures**
- Look for “见表4-2/表4-3/图4-1/图4-3/图4-4”.

---

### Task 5: Commit (Optional but Recommended)

**Files:**
- Stage: `docs/thesis/01_final_for_paste/50_ch5_conclusion_and_outlook_draft_v1.md`

**Step 1: Stage and commit**
```bash
git add docs/thesis/01_final_for_paste/50_ch5_conclusion_and_outlook_draft_v1.md
git commit -m "thesis: rewrite ch5 conclusion/outlook (results-first)"
```


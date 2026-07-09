# S1: Rebuild the Eval Ruler — Implementation Plan (AMENDED v3)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Delete theater metrics (temporal_precision, grounding_rate, attribution_f1 — DELETE the file, not facade). Replace them with rubric-based LLM judges: cause_match pairings (precision/recall/F1/direction_accuracy), citation_faithfulness (per-cause, three-level), refusal_correctness (deterministic). Build judge cache at packages/eval/eval_cache/judge_cache.json. Build v1_3 golden set (50 answerable + N unanswerable per §0.6). Three-arm experiment: Arm A = closed-book, Arm B = consumes persisted judge_evidence artifact (byte-identical by construction), Arm C = full MCJ with runtime instrumentation.

**Architecture:** Mirror deepeval BaseMetric/GEval for LLM judges. cause_match outputs PAIRINGS → derived precision/recall/F1. citation_faithfulness is per-cause unit, three-level verdict (supported=1/partial=0.5/unsupported=0), LLM-judge (not NLI). refusal_correctness is deterministic. Judge cache at tracked path packages/eval/eval_cache/judge_cache.json (JSON dodges .gitignore *.sqlite rule). Arm-B fidelity: Arm C instrumented at runtime to persist a node_artifact "judge_evidence" (per-asset_id content_md, Critic fields stripped) + "judge_evidence_sha256"; Arm B consumes the artifact verbatim.

**Tech Stack:** Python 3.12+, httpx, gpt-4o temp=0, sqlite3, JSON, pytest.

**Design Refs (normative):** S1, §0.4, §0.5, §0.6, Decision Sheet items 1, 2, 4.

**Process rule:** CLAUDE.md for writing-plans: strictly exclude all code blocks.

---

## Phase 0: Real-Run Verification Evidence

### 0.1: Frozen DB SHA
shasum -a 256 data/catalyst_eval_frozen_v2.db → 0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf ✓

### 0.2: L1/L2 chunk format (index_builder.py verified anchors)
Line 98: L1 chunk_id = "{article_id}::l1", content_text = f"{title}\n{body}" where body=description or ""
Line 131: L2 chunk_id = "{article_id}::l2s{idx:04d}", content_text = single sentence
Line 136: L2 parent_article_id = article_id
**L2 asset_ids like "poly:abc123::l2s0000" will NOT resolve in corpus_items (which only has L1). Arm B re-resolve path is unfaithful.**

### 0.3: Judge._format_evidence source text
judge.py:48-66: Builds content_lookup from asset_id → content_md from in-memory reranked_chunks. Renders chunk_id, relevance, category, temporal_match, critic reasoning, then FULL content_md.

### 0.4: node_artifacts persistence gap
projection.py:78-88: _project_chunk extracts headline + snippet (max 200 chars), drops content_md. The persisted reranked_chunks artifact has NO full content_md.

### 0.5: expand_macro wiring + last-miner rule
graph.py:261-290: expand_macro node + edge to miner. 0 expand_macro events in frozen DB. Last miner by max(event_seq) WHERE node='miner' is correct.

### 0.6: .gitignore cache paths
.gitignore:11 — `.local/` is gitignored. .gitignore:18-19 — `*.db` and `*.sqlite` are gitignored.
Both plan paths (.local/judge_cache/) and design paths (.db/.sqlite cache) are dead on fresh checkout.

### 0.7: packages/eval layout
packages/eval/catalyst_eval/, golden_set/, scripts/, tests/ — no baselines/ or eval_cache/ yet.

---

## Dependencies & CORE-vs-STRONG Boundary

- **Dependencies:** None.
- **CORE:** Phases 1–7.
- **STRONG:** Phase 8.

---

## Phase 1: Delete Theater Metrics + Relocate Cache

**Files:**
- DELETE: temporal_precision.py, grounding_rate.py, attribution_f1.py (full delete — attribution_f1 is a theater-metric name)
- CREATE: packages/eval/eval_cache/ (tracked directory)
- RELOCATE: judge cache → packages/eval/eval_cache/judge_cache.json
- MODIFY: metrics/__init__.py exports

TEST INTENT: temporal_precision, grounding_rate, attribution_f1 all raise ImportError. New metric classes exported. eval_cache/ exists and is tracked.

---

## Phase 2: Build CauseMatch Judge

**Design refs:** §0.5, §0.4.

### Pairing Contract
Outputs list of (pred_idx, golden_idx, verdict) where verdict ∈ {same_event_same_direction, same_event_wrong_direction, unrelated}.

### Derived Metrics
precision = |matched pairings| / |predicted causes|
recall = |matched pairings| / |golden causes|   where "matched" = verdict ∈ {same_event_same_direction, same_event_wrong_direction}
F1 = harmonic mean
direction_accuracy = |same_event_same_direction| / |matched pairings| (null if denominator 0)

### Skip Rules (AMENDED — fix #2 blocking)
SKIP ONLY: golden.should_refuse == True OR golden.causes == [].
Do NOT skip: empty predicted causes on an answerable case.
Empty predicted on answerable case ⇒ precision excluded (undefined), recall=0, F1=0. This closes the gaming path where an agent that abstains on hard answerable cases inflates F1 via skip.

### Evidence-Support Separation (AMENDED — fix #3 should-fix)
cause_match judges SEMANTIC EVENT MATCH + DIRECTION only. Evidence support lives ONLY in citation_faithfulness. The "unresolvable cited evidence_id → unrelated" rule is REMOVED from cause_match (it double-counts citation quality). Unresolvable-id rejection belongs ONLY to citation_faithfulness (auto-unsupported).

### Edge Cases
- Both predicted and golden empty on answerable case → skip (nothing to match)
- Both predicted and golden empty on should_refuse case → skip (handled by refusal_correctness)

### JudgeBase Contract
name (str), criteria (str), evaluation_steps (list[str]), rubric (dict[str→float])
Cache key: SHA-256(case_id || "::" || arm || "::" || metric || "::" || rubric_version || "::" || prompt)
Cache path: packages/eval/eval_cache/judge_cache.json (single JSON per metric, not per-judge)
LLM: gpt-4o, temp=0

TEST INTENT:
1. Fabricated cause citing real-but-irrelevant asset → verdict "unrelated"
2. Same event, opposite direction → verdict "same_event_wrong_direction"
3. Perfect match → verdict "same_event_same_direction", precision=1.0, recall=1.0
4. Refusal case → skipped by cause_match
5. Empty golden causes → skipped
6. **Empty predicted on answerable case → recall=0, F1=0 (NOT skipped)**

---

## Phase 3: Build CitationFaithfulness Judge (AMENDED — fix #4 should-fix)

**Design refs:** §0.4, §0.5.

### Contract: Per-Cause Unit (not summary-level decomposition)
Input: one predicted cause (text, cited evidence_ids), cited evidence content blocks
Output: verdict ∈ {supported (1.0), partial (0.5), unsupported (0.0)}, reason (str)
Process: LLM judge verdict per cause (NOT NLI model — design bans importing NLI).
Score = mean verdict across all causes.

RAGAS-style statement decomposition is DEFERRABLE.

### Skip Rules
Refusal cases and empty-output cases are SKIPPED by citation_faithfulness (handled by refusal_correctness). NOT scored 1.0 (vacuous-truth inflation).

TEST INTENT:
1. All causes fully supported → score 1.0
2. One partial among three → score 0.83 (avg of [1.0, 1.0, 0.5])
3. One unsupported → score 0.67 (avg of [1.0, 1.0, 0.0])
4. Empty causes → skipped (not scored 1.0)
5. Refusal case → skipped

**Files:**
- Create: catalyst_eval/metrics/citation_faithfulness.py

---

## Phase 4: Build RefusalCorrectness (Deterministic)

### Decision Table
| golden.should_refuse | predicted.causes | predicted.summary | score |
|---|---|---|---|
| True | [] | null/empty | 1.0 |
| True | non-empty | any | 0.0 |
| True | [] | non-empty | 0.0 |
| False | any | any | null (N/A) |

**Files:**
- Create: catalyst_eval/metrics/refusal_correctness.py

---

## Phase 5: Build DirectionAccuracy (Derived)

From cause_match pairings: correct_direction / total_matched (null if zero matched).

**Files:**
- Create: catalyst_eval/metrics/direction_accuracy.py

---

## Phase 6: Judge Cache

### Path
packages/eval/eval_cache/judge_cache.json (single JSON, tracked, dodges *.sqlite gitignore).

Cache key: SHA-256(case_id || "::" || arm || "::" || metric || "::" || rubric_version || "::" || prompt)

### Deterministic metrics (refusal_correctness, direction_accuracy)
Also stored in eval_cache/ as per-case JSON (no LLM needed, but the S4 gate replays cache-only).

TEST INTENT:
1. Cache hit → byte-identical, cache_hit=True
2. Rubric_version change → cache miss, re-evaluate, new entry
3. Corrupted cache → treated as empty, re-populated

**Files:**
- Modify: judge_base.py (cache path to packages/eval/eval_cache/)
- Create: packages/eval/eval_cache/ (add to git)

---

## Phase 7: v1_3 Golden Set + Three-Arm Experiment

### 7.1: Delete v1_2_p1_set.jsonl

### 7.2: v1_3_answerable.jsonl — 50 provenance-upgraded cases (AMENDED — fix #5 should-fix)

Per-cause provenance schema (per §0.6):
- annotator (str): who created the cause
- annotated_at (str): ISO-8601
- supporting_asset_ids (list[str]): evidence asset_ids backing this cause
- notes (str|null)

Programmatic unanswerable rule (§0.6):
- |price_move_pct| < 0.3% AND < 3 articles within ±1 trading day
- +5 out-of-window cases: trade_date > 2026-05-01 (beyond current source ceilings)
- Include h006 explicitly

Floor gate: "filter to resolvable" must report exact count. If <40 of 50 survive, FAIL (no silent shrinkage).

### 7.3: v1_3_unanswerable.jsonl — per §0.6 rules

### 7.4: Three-arm experiment

**Arm A — Closed-book:**
Input: ticker + trade_date + price_move_pct ONLY. Zero evidence.
Purpose: A→B delta = retrieval value.

**Arm B — Same-evidence single call (AMENDED — fix #1 blocking):**
Does NOT re-resolve from corpus_items (L2 asset_ids won't resolve; content provenance unverified on frozen plane).

Correct, provenance-independent fix:
1. Instrument Arm C at RUNTIME: after the Judge runs, persist a NEW node_artifact "judge_evidence"
2. judge_evidence payload: per asset_id → content_md string that _format_evidence used (the FULL content)
3. Critic-only fields (relevance, category, temporal_match, reasoning) STRIPPED per §0.5
4. Also persist "judge_evidence_sha256" = SHA-256 of the assembled evidence block (the exact string the Judge's prompt template receives)
5. Arm B reads "judge_evidence" from the last-miner run's node_artifacts, assembles the evidence block, feeds it to a fresh Judge call
6. Bite test: SHA-256(Arm B assembled block) == persisted judge_evidence_sha256 (non-tautological — Arm B rebuilds from per-asset strings, the hash proves byte-identity without sharing the assembled block)

This works on frozen AND live planes — provenance is guaranteed by construction.

**Arm C — Full MCJ (instrumented):**
Standard Catalyst run + writes judge_evidence + judge_evidence_sha256 to node_artifacts.

**DELETED:** Arm C oracle (golden-causes-as-context).

### 7.5: Rewrite orphan test
test_build_same_evidence_inputs.py → test arm-B consumes judge_evidence artifact and hashes to judge_evidence_sha256.

---

## Phase 8 (STRONG): Raw External Baseline

---

## Post-Implementation Verification

1. pytest packages/eval/tests/ -v — all pass
2. Frozen DB SHA unchanged
3. attribution_f1.py DELETED (not facade)
4. Judge cache at packages/eval/eval_cache/judge_cache.json (tracked)
5. Arm B hash == persisted judge_evidence_sha256
6. cause_match: empty predicted on answerable ⇒ recall=0, F1=0 (not skipped)
7. cause_match: unresolvable-id rule REMOVED
8. citation_faithfulness: per-cause unit, three-level, LLM-judge, skipped on empty/refusal
9. v1_3_answerable.jsonl: ≥40 cases survive filter, provenance schema present, h006 included

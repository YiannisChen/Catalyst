# M8 dscodex Prompt Guide

You (human) paste **one prompt at a time**. Wait for a stop report before the
next. Reviewer is Grok; dscodex is the executor.

**Docs to point at (already in repo after P0 merge):**

- `docs/plans/2026-09-18-m8-executor-lock.md` ← binding (amended 2026-09-19)
- `docs/plans/2026-09-19-m8-b-fts-ceiling-and-dense-cutover.md` ← binding amendment
- `docs/plans/2026-09-18-catalyst-v1.1-m8-causal-quality-recovery-design.md`
- `docs/plans/2026-09-18-catalyst-v1.1-m8-causal-quality-recovery.md`

**Do not paste the whole implementation plan.** The lock overrides TDD.

**State 2026-09-19:** P0 → P1 → P2 → P3 are done. **P3 is CLOSED FAIL** (M8-B
FTS sealed at 9/29 recall). **Do not send P4** on the current FTS packs: M8-C is
deferred until M8-D hybrid retrieval exists. **P5 (GPU) is the next prompt and
must contain `GPU_OK=yes`.** No cleanup prompt in this campaign.

If you want the integration branch on GitHub, add a last line to P0:
`PUSH_INTEGRATION=yes`

---

## P0 — git lineage only

```text
You are dscodex, executor. Reviewer/decision-maker is Grok.

Read and obey: docs/plans/2026-09-18-m8-executor-lock.md
Also: docs/plans/2026-09-18-catalyst-v1.1-m8-causal-quality-recovery-design.md

Do M8-0 only.
- Merge v1.1/m7-stage1-eval then v1.1/m8-causal-quality-plan into v1.1/integration with --no-ff.
- Create branch v1.1/m8-causal-quality-recovery and worktree /Users/yiannischen/Desktop/Catalyst-v1.1-m8.
- Do not implement code. Do not touch DBs. Do not commit on the M7 worktree or the plan worktree.
- Untracked scratch/, scripts/arclaude are allowed; do not add them. Do not stash/reset.
- Do not git push unless this prompt contains PUSH_INTEGRATION=yes.

Stop report: worktree path, HEAD, merge-base ancestor check for v1.1-m7-stage1, whether origin was pushed.
```

---

## P1 — code seams, fast

```text
You are dscodex. Work only in /Users/yiannischen/Desktop/Catalyst-v1.1-m8 on v1.1/m8-causal-quality-recovery.

Obey docs/plans/2026-09-18-m8-executor-lock.md (test policy: max six short contract tests, no TDD theater, no full-package pytest).

Implement M8-A/B/C code only, reuse M3:
1. source-selection manifest (reject gold keys; policy general-public-fulltext-v1)
2. generation coverage audit CLI
3. prepare binds source-selection; still uses DATA-01/Q-005; rejects Stage-1 cases.jsonl; never flips is_current
4. pointer-free candidate FTS retrieval runner using retrieve_lexical(..., inactive_build_id=...)
5. eval-only --retrieval-mode candidate-fts adapter
6. recovery_gates.py (M8-C/D thresholds) without changing STAGE1_RETRIEVAL_GATES

Do not run prepare on the 9GB DB. Do not promote. Do not call providers. Do not rent GPU.
Commit with explicit git add. Push origin v1.1/m8-causal-quality-recovery.

Stop report: files changed, HEAD, contract tests run, anything blocked.
```

---

## P2 — real M8-A rebuild (CPU, long)

```text
You are dscodex in /Users/yiannischen/Desktop/Catalyst-v1.1-m8.

Obey docs/plans/2026-09-18-m8-executor-lock.md §4–§5.

M8-A only:
- Copy Q-001 to data/snapshots/catalyst_m8a_derivative_7a004acc.db. Never write Q-001. Confirm source SHA eb3a37b6… unchanged.
- Seal general-public-fulltext-v1 source-selection BEFORE any 19-ID audit. No gold fields.
- prepare only (no promote). Filing bodies must enter the inactive candidate. is_current stays 0.
- If 19/19 fails, one class-based supplement from local public archives (Q-011 scratch as byte archive OK; do not copy that derivative as the candidate). Then stop if still failing.
- Write sanitized coverage report. Do not commit the DB.

No provider. No GPU. No M8-B yet if 19/19 failed.

Stop report: derivative path, build_id, corpus_manifest_id, FULL_TEXT counts, filing chunk count, 19/19 result, is_current, Q-001 SHA still eb3a37b6.
```

---

## P3 — M8-B retrieval gate — CLOSED FAIL 2026-09-19 — do not resend

Result on candidate `bec845b4…` (`candidate_depth=100`, HEAD `c9528f8`):
Recall@8 9/29 (value 0.294), primary-source hit 5/9 (0.556), dup-adjusted P@8
6/26 (0.231), ticker/cutoff violations 0, required-primary non-empty 8/9
(missing `c01` only). Three lexical policies measured (2/29, 10/29, 9/29).
The FTS ceiling is sealed in
`docs/plans/2026-09-19-m8-b-fts-ceiling-and-dense-cutover.md`; no further
lexical ranking change is authorized. Kept below only as the historical prompt.

```text
You are dscodex in /Users/yiannischen/Desktop/Catalyst-v1.1-m8.

Obey executor-lock §6 M8-B. Run the candidate FTS retrieval runner on the M8-A inactive build. Gold IDs only after ranked results are frozen.

Hard gates: Recall@8≥0.75, primary-source hit≥0.80, dup-adjusted P@8≥0.60, 0 ticker/cutoff violations, 9/9 expected-primary cases with non-empty included_evidence_ids and citable body.

If fail: do not call the model, do not GPU. Fix query/ranking/data once, or stop with per-case misses.

Commit only a sanitized JSON report. Stop report: metrics with numerators/denominators, not just percentages.
```

---

## P4 — M8-C probe — DEFERRED, do not send yet

M8-C now runs **after** M8-D hybrid retrieval produces packs, because its first
gate (`c01 non-ABSTAIN`) fails on FTS-only packs for retrieval reasons. Do not
send this prompt on the current FTS candidate, with or without keys. Kept below
as the historical prompt; the replacement will be authored once hybrid retrieval
exists.

```text
You are dscodex in /Users/yiannischen/Desktop/Catalyst-v1.1-m8.

M8-B has passed. Obey executor-lock §6 M8-C.

Run Stage-1 once with --retrieval-mode candidate-fts against the inactive candidate. New eval_id and output dir. Never write v1_1_stage1_327b3eed or eval_id 2792d333….

Provider: DeepSeek, credentials from the environment already on this machine. Ceiling: 12 cases, normal 2 logical calls each, one technical retry. Stop on repeated identical failures.

Gates: Analyst called when citable evidence exists; ≥6/11 answerable non-ABSTAIN; c01 non-ABSTAIN; c04 ABSTAIN; citation 100%; unsupported primary claims 0; false SUFFICIENT ≤1/12.

No GPU. No promote. If fail, inspect retrieval → ContextPack → Analyst, in that order. Do not loosen citation.

Stop report: per-case status vs gold, Analyst call counts, citation/unsupported, new eval_id.
```

---

## P5 — M8-D GPU (next prompt; requires the literal line `GPU_OK=yes`)

This is now the next prompt in the campaign. It **must** contain the literal line
`GPU_OK=yes`; without it dscodex must refuse GPU work. M8-B FTS FAIL and the
deferred M8-C are not blockers for M8-D.

```text
You are dscodex. GPU_OK=yes. Obey executor-lock §6 M8-D and the 2026-09-19
amendment. M8-B FTS is sealed FAIL; M8-C is deferred until hybrid packs exist.

Rent/use GPU only for BGE-M3 5617a9f6… dim 1024 on the NEW candidate to_embed set (374,179 FULL_TEXT bodies of bec845b4…). Do not reuse 162434 title vectors or Q-011's LanceDB / 374179 vectors.

Stage a new LanceDB table for the inactive tuple, four-arm retrieval on the INACTIVE tuple only, keep Q-001 untouched as rollback. Do NOT promote in this prompt; promotion is a separate authorization after the inactive hybrid passes the unchanged M8-B formulas.

Gates: unchanged M8-B formulas still pass on the inactive hybrid tuple; then (after a separate promote authorization) a new-eval_id Stage-1 run: ≥7/9 gold SUFFICIENT non-ABSTAIN; status match ≥8/12; c04 ABSTAIN; citation 100%; unsupported primary 0.

Do not start M8-E cleanup.
```

---

## Do not send

- Any prompt that starts M8-E cleanup/RC.
- Any prompt to promote Q-011 or flip Q-001 `is_current` before M8-D.
- P2 and P3 in the same message (rebuild must finish first).
- P3 again: M8-B FTS is sealed FAIL; another lexical ranking knife is refused.
- P4 on the current FTS packs; M8-C waits for hybrid retrieval.
- P5 without the literal line `GPU_OK=yes`.
- Any P5 variant that also promotes in the same prompt.

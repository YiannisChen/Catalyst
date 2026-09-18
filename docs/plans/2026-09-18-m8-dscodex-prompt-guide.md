# M8 dscodex Prompt Guide

You (human) paste **one prompt at a time**. Wait for a stop report before the
next. Reviewer is Grok; dscodex is the executor.

**Docs to point at (already in repo after P0 merge):**

- `docs/plans/2026-09-18-m8-executor-lock.md` ← binding
- `docs/plans/2026-09-18-catalyst-v1.1-m8-causal-quality-recovery-design.md`
- `docs/plans/2026-09-18-catalyst-v1.1-m8-causal-quality-recovery.md`

**Do not paste the whole implementation plan.** The lock overrides TDD.

Send P0 → P1 → P2 → P3. Send P4 only with provider keys. Do not send P5
(GPU) or cleanup until Grok says so.

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

## P3 — M8-B retrieval gate

```text
You are dscodex in /Users/yiannischen/Desktop/Catalyst-v1.1-m8.

Obey executor-lock §6 M8-B. Run the candidate FTS retrieval runner on the M8-A inactive build. Gold IDs only after ranked results are frozen.

Hard gates: Recall@8≥0.75, primary-source hit≥0.80, dup-adjusted P@8≥0.60, 0 ticker/cutoff violations, 9/9 expected-primary cases with non-empty included_evidence_ids and citable body.

If fail: do not call the model, do not GPU. Fix query/ranking/data once, or stop with per-case misses.

Commit only a sanitized JSON report. Stop report: metrics with numerators/denominators, not just percentages.
```

---

## P4 — M8-C probe (only with keys)

Replace the budget line. Do not send this prompt without keys.

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

## P5 — M8-D GPU (later; do not send now)

```text
You are dscodex. M8-B and M8-C passed. Obey executor-lock §6 M8-D.

Rent/use GPU only for BGE-M3 5617a9f6… dim 1024 on the NEW candidate to_embed set. Do not reuse 162434 title vectors or Q-011 374179.

Stage new LanceDB, four-arm, atomic promote of corpus+FTS+dense, keep Q-001 rollback. New Stage-1 eval_id.

Gates: retrieval gates still pass; ≥7/9 gold SUFFICIENT non-ABSTAIN; status match ≥8/12; c04 ABSTAIN; citation 100%; unsupported primary 0.

Do not start M8-E cleanup.
```

---

## Do not send

- Any prompt that starts M8-E cleanup/RC.
- Any prompt to promote Q-011 or flip Q-001 `is_current` before M8-D.
- P2 and P3 in the same message (rebuild must finish first).
- P4 without M8-B numbers.
- P5 without M8-C numbers and GPU authorization from Grok.

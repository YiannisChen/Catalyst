# Catalyst Post-GPU Session Handoff

> **For the next agent:** REQUIRED SUB-SKILL: use
> `superpowers:executing-plans` and `superpowers:test-driven-development`.
> Read this handoff and `2026-08-08-post-import-completion-plan.md` before
> editing code. This document records the verified state on 2026-08-09 and
> supersedes stale execution-host notes in older prompts.

## 1. Manager verdict

Current status is **INDEX_READY_WAVE1_COMPLETE_B6_INCOMPLETE**.

The production corpus was embedded on an RTX 4090, copied back to the Mac,
imported into LanceDB, and activated. Runtime table resolution is wired and
fails closed. The project has **not** yet demonstrated production four-arm
retrieval, attribution quality, API user flow, or an accuracy improvement.

Do not re-embed or re-import. Do not claim `FOUR_ARM_E2E_OK`, `USER_SMOKE_OK`,
or B6 completion until their evidence contracts are satisfied.

## 2. User and product requirements

Catalyst is an evidence-bounded market-move attribution system for 40 tickers.
For a ticker, market session, and cutoff, it should retrieve only evidence that
was available by the cutoff, compare lexical/dense/hybrid/reranked retrieval,
run the attribution graph, cite evidence, and abstain when evidence is
insufficient. Every result must be reproducible through snapshot, corpus,
index, model, code, case-pack, and trace identities.

The project also needs portfolio-grade evidence for graduate applications and
open-source work:

- production-like data and retrieval, not fixture-only demos;
- explicit failure, retry, resume, provenance, cutoff, and refusal contracts;
- measurable retrieval and attribution quality;
- a 50-event evaluation set after smoke convergence;
- ablations for retrieval arms and for Critic/refusal behavior;
- honest limitations, reproducible reports, API tests, and later UI work.

## 3. Verified repository state

- Repository: `/Users/yiannischen/Projects/Catalyst`
  (`/Users/yiannischen/Desktop/Catalyst` resolves to the same checkout).
- Branch: `recovery/b2o-data-readiness`.
- HEAD: `3154dac6e0634d5d61614b1370120f4b229429a1`.
- Remote: local branch equals `origin/recovery/b2o-data-readiness`.
- Worktree: clean; Git index empty; `git diff --check` clean.
- Worktrees: one active worktree only.
- Other branch: `b2/update-provenance` is an ancestor of the current branch;
  it is no longer an independent line of work and may be deleted after the
  current B6 branch is safely integrated.
- Free disk at review: about 52 GiB.

Latest code change (`3154dac`) makes `RuntimeDependencyLoader` resolve the
table from `active_generation.json` instead of silently opening `chunks`.
Missing or malformed pointers fail closed. Wave 1 evidence token is
`INDEX_WIRING_OK`.

## 4. Frozen production identities

These values are fixed inputs. Do not edit, regenerate, or silently substitute
them.

| Identity | Value |
| --- | --- |
| embedding/index code revision | `bb43ebe20f29a13ef426e0a1a7c3aefc6d15ffd8` |
| runtime Git HEAD | `3154dac6e0634d5d61614b1370120f4b229429a1` |
| model | `BAAI/bge-m3` |
| model/tokenizer revision | `5617a9f61b028005a4858fdac845db406aefb181` |
| snapshot | `7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49` |
| DB SHA-256 | `bb37b213091e256033fa00272cb7a85617dbcddf69d6fe9b515840cd9f1ebe40` |
| corpus manifest | `3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc` |
| source bundle | `8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2` |
| probe report | `25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23` |
| postbuild readiness | `9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b` |
| index manifest | `c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083` |
| active table | `chunks__staging__b3761f4b943542a8` |
| vector contract | `295506 x 1024`, float32, L2-normalized |

Verified on 2026-08-09:

- DB `user_version=13`, foreign-key violations `0`;
- current corpus manifest matches the table above;
- `corpus_served_chunks=295506`;
- active LanceDB table row count `295506`;
- clean-import and full embedding checksum files pass;
- `gpu_run_report.json` is `completed` on an RTX 4090;
- `import_report.json` is `committed`, `ok=true`;
- focused Wave 1/runtime tests: `24 passed`.

## 5. Local assets that must be preserved

```text
data/snapshots/catalyst_b2o_7a004acc...db
data/source_bundles/source_8ffae891...
data/embeddings/b6g_8ffae891_bb43ebe/   # full GPU output + shards
data/embeddings/b6g_import_bb43ebe/     # clean import artifact
data/lancedb_gold/b6g_8ffae891b4e1/     # active gold index
data/run_reports/post_import/wave1_index_wiring_20260808/
data/run_reports/post_import/wave2_blocked_gpu_20260808/
```

The old GPU host `cpod-1trze1ad4jgy.podtcp.compshare.cn:27832` is no longer an
execution surface. It now refuses SSH connections. Its failure was a CUDA
passthrough/cuInit issue, not index corruption. All critical artifacts are on
the Mac.

## 6. Completed versus incomplete

### Completed

- B2/B2-O ingestion, provenance, resume, readiness, and 40-ticker data work.
- B3 corpus normalization/chunking/dedup/publication.
- B4 cutoff-safe FTS and retrieval contracts.
- B5 attribution graph/runtime assurance implementation and package tests.
- Pre-B6 SEC evidence: 688/688 mandatory filings.
- Frozen snapshot, corpus, FTS, 40/40 corpus and lexical probes.
- GPU dense embedding and Mac-side LanceDB import/activation.
- Wave 1 active-table runtime wiring (`INDEX_WIRING_OK`).

### Incomplete and blocking product claims

- T4 production smoke case pack and N/N served-corpus existence probe.
- `packages/eval/scripts/run_post_import_four_arm.py` does not exist.
- Production-pinned fts5/dense/hybrid/reranked artifacts do not exist.
- Cutoff/ticker regression evidence on the production dense index is absent.
- Union judgment pools have not been generated for production cases.
- Attribution graph and HTTP/API smoke have not used this active index.
- Human judgments and effect report are absent.
- No defensible attribution-accuracy improvement has been measured.
- No 50-event benchmark or Critic/refusal ablation has been run.
- UI work remains later; do not start it before retrieval/evaluation converges.

FMP remains optional/degraded for some cells. It does not invalidate the
certified mandatory snapshot, but reports must not imply complete FMP coverage.

## 7. Correct next execution order

The old plan's Wave 1 recommendation is stale. Wave 1 is complete. The most
cost-efficient order is:

### Stage A: local Wave 2 preparation, no rented GPU

1. Execute T4 locally: freeze 3-12 smoke cases and prove N/N ticker+cutoff
   existence against `corpus_served_chunks`.
2. Implement `run_post_import_four_arm.py` with TDD. Use injected mock query
   embedding/reranker only in unit tests and mark them `mock_unit_test`.
3. Add offline contract tests for T5-T8: four-arm order, identity binding,
   cutoff/ticker checks, persisted artifacts, and union pool generation.
4. Run package regressions, commit, and push. Preserve separate fields for
   index build revision (`bb43ebe...`) and runtime `git_head`.

This stage must not emit `FOUR_ARM_E2E_OK`; production-pinned query embedding
has not run yet.

### Stage B: fresh GPU, short production Wave 2 run

1. Rent a fresh CUDA-capable instance. Before transferring large files, prove
   `torch.cuda.is_available() is True`, allocate a tiny CUDA tensor, and record
   the device.
2. Checkout the manager-approved runtime commit and verify a clean tree.
3. Transfer the gold LanceDB directory, frozen DB, active snapshot pointer,
   and only the identity artifacts the runner validates. Rewrite only the
   host-specific `db_path` in the copied pointer; do not change identities.
4. Verify active table row count `295506`.
5. Run one production-pinned case first, then the complete smoke pack.
6. Run T6-T8 and emit `FOUR_ARM_E2E_OK` only when every required artifact and
   pool exists and all identities match.

Do not re-embed and do not re-import.

### Stage C: attribution and API user smoke

Execute T9-T12 using the production retrieval path. Cover normal,
ABSTAIN/refusal, malformed cutoff, wrong manifest, and missing index paths.
Persist traces and assurance records. Emit `USER_SMOKE_OK` only after in-process
and HTTP paths pass.

### Stage D: quality evidence before UI

1. Complete the 12-case minimum human judgment protocol required by the
   current B6 plan.
2. Expand to the intended 50-event evaluation set before making portfolio or
   resume accuracy claims.
3. Retrieval ablations: fts5, dense, hybrid/RRF, reranked.
4. Agent ablations: full graph, Critic disabled, refusal/abstain gate disabled.
5. Report retrieval Recall@k/MRR/nDCG, evidence coverage/precision, attribution
   correctness, unsupported-claim rate, abstain precision/recall, latency, and
   cost. State confidence intervals or sample-size limitations.
6. Freeze Catalyst results before manually comparing the same cases with web
   GPT/Claude so external answers cannot influence internal labels.

Only after this stage should B7 API hardening and frontend building become the
main workstream.

## 8. Evidence and stop rules

Every run writes under:

```text
data/run_reports/post_import/<run_id>/
  meta.json
  WAVE_TOKEN.txt
  arms/<case_id>.json
  pool/<case_id>.json
  traces/*
```

Rules:

- No mock/hash query vector may appear in `production_pinned` evidence.
- No CUDA means stop before production dense/reranked execution.
- No case may contain evidence newer than its cutoff.
- No silent fallback to table `chunks`, CPU, another manifest, or another
  model revision.
- Do not print or commit secrets. Provider checks report only configured state.
- Do not write success tokens from chat text; tokens require persisted evidence.
- Do not modify the frozen DB, gold LanceDB table, vectors, or identity files.

## 9. Git management

- Continue on `recovery/b2o-data-readiness` until Wave 2 and its tests are
  coherent. Use small Conventional Commits for local runner/test work.
- Do not create another worktree unless parallel work has a disjoint write set.
- Do not merge to `main` before at least `FOUR_ARM_E2E_OK` and manager review.
- After integration, delete local/remote `b2/update-provenance`; it is already
  fully contained in the current branch.
- Large runtime artifacts and reports remain untracked/ignored. Never use
  `git add -A` around `data/` or `.env`.

## 10. Immediate execution prompt

Give the next implementation agent the prompt below. It intentionally stops
before renting a GPU.

```text
Continue Catalyst from the verified post-GPU handoff.

Repository:
/Users/yiannischen/Projects/Catalyst

Branch/starting HEAD:
recovery/b2o-data-readiness
3154dac6e0634d5d61614b1370120f4b229429a1

Required skills:
- superpowers:executing-plans
- superpowers:test-driven-development
- superpowers:verification-before-completion

Read first:
1. docs/plans/2026-08-09-catalyst-post-gpu-handoff.md
2. docs/plans/2026-08-08-post-import-completion-plan.md, especially T4-T8
3. docs/plans/2026-07-22-b6-dense-reranker.md

Objective:
Complete all local/offline preparation for Wave 2 in one coherent pass. Do
not rent/connect to GPU and do not run production embedding. Finish T4 and
implement/test the T5-T8 runner contracts so the next paid GPU session only
needs production-pinned execution.

Tasks:

1. Preflight
- Confirm exact branch/HEAD and clean worktree.
- Re-verify active_generation identity and LanceDB count_rows=295506 read-only.
- Re-verify frozen DB user_version=13, FK=0, current corpus manifest, and
  corpus_served_chunks=295506.
- Do not modify data identities.

2. T4 case pack and probe
- Select 3-12 representative cases from the existing frozen/golden sets.
- Include answerable, low-evidence/ABSTAIN, multiple tickers/sectors, and
  date/cutoff diversity where existing cases permit.
- Persist a deterministic case-pack identity.
- Run the read-only served-corpus ticker+cutoff probe.
- Require N/N cases with at least one eligible pre-cutoff chunk.
- Persist T4_PROBE_OK evidence; fail closed otherwise.

3. Implement the unique four-arm runner
- Create packages/eval/scripts/run_post_import_four_arm.py.
- Add focused tests before production code.
- Arms are exactly fts5, dense, hybrid, reranked in that order.
- Reuse ProductionHybridRetriever and existing artifact/pool contracts.
- Bind snapshot, corpus, source bundle, probe, postbuild, index manifest,
  index build revision, runtime git_head, model revision, case pack, cutoff,
  ticker, and embedding_mode.
- Unit tests may inject mock embeddings/reranker but must mark
  embedding_mode=mock_unit_test.
- production_pinned must reject mock/hash vectors and revision mismatch.
- Do not use run_frozen_eval.py as production evidence.

4. T6-T8 contracts
- Test and implement cutoff/ticker/no-look-ahead validation.
- Persist arm artifacts and complete meta.json atomically.
- Generate one UnionJudgmentPool per case.
- Reject missing arms, duplicate/empty chunk IDs, malformed identity, and
  source-artifact mismatch.
- Expected union order in tests must be a literal independent oracle.

5. Verification
- Run all new focused tests.
- Run packages/data-core, packages/agents, packages/eval, and packages/app.
- Run compileall and git diff --check.
- Confirm frozen DB, vectors, LanceDB, and data identities are unchanged.
- Scan staged candidates for secrets and data artifacts.

6. Git
- Stage only coherent source/tests/docs required for T4-T8 offline prep.
- Create small Conventional Commits; do not push unless explicitly authorized
  by the manager.
- Never stage data/, .env, model cache, vectors, LanceDB, or run reports.

Stop conditions:
- No GPU/CUDA/model download/provider calls.
- No re-embedding or re-import.
- Do not write FOUR_ARM_E2E_OK; only real production_pinned GPU execution can
  produce it.
- Do not start T9/API/UI.

Final report:
- files and commits
- T4 case list and N/N probe evidence
- runner CLI/schema and mock-vs-production boundary
- focused/full test counts
- git status/index
- frozen identity/hash evidence
- remaining exact GPU commands and expected runtime
- status must be READY_FOR_WAVE2_GPU_MANAGER_REVIEW or BLOCKED with evidence
```

## 11. Completion language

Accurate current statement:

> Catalyst has a certified 40-ticker snapshot, a 295,506-chunk corpus, a
> production BGE-M3 dense index, atomic LanceDB activation, and fail-closed
> runtime index wiring. Production four-arm retrieval and attribution-quality
> evaluation are the next uncompleted gates.

Do not describe the project as fully complete or accuracy-proven yet.

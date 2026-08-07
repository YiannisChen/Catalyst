# Catalyst Pre-B6 Session Handoff

> **For the next agent:** REQUIRED SUB-SKILLS: use
> `superpowers:using-superpowers`, `superpowers:executing-plans`,
> `superpowers:test-driven-development`, `superpowers:systematic-debugging`,
> `superpowers:requesting-code-review`, and
> `superpowers:verification-before-completion`.

**Handoff date:** 2026-08-01 (Asia/Shanghai)

**Goal:** finish the bounded/resumable Pre-B6 corpus publication implementation,
independently review it, then produce a promoted v13 snapshot, 40/40 corpus and
lexical probes, a frozen lexical baseline, and an identity-bound vector-free
source bundle. Stop for manager review before any GPU upload or B6 execution.

## 1. Repository And Git State

- Canonical local repository: `/Users/yiannischen/Projects/Catalyst`.
- `/Users/yiannischen/Desktop/Catalyst` is an alias/symlink to the same repository.
  Do not create a second clone or worktree for this handoff.
- Branch: `recovery/b2o-data-readiness`.
- HEAD at handoff: `7009a24d0c44883a03c99633d2975bfdd07519aa`.
- The worktree is intentionally dirty and contains the complete accumulated
  B2-O/Pre-B6 implementation. The index is empty.
- Do not reset, checkout, stash, clean, or overwrite existing changes. Do not
  split the work into another worktree because the uncommitted state is required.
- Do not stage or commit until the streaming amendment has passed independent
  review and the manager has approved the commit scope.
- `git diff --check` was clean at handoff.

Important dirty/new areas include:

- `packages/data-core/catalyst_data/b2o.py`
- `packages/data-core/catalyst_data/coverage_audit.py`
- `packages/data-core/catalyst_data/index_builder.py`
- `packages/data-core/catalyst_data/corpus/news_v2.py`
- `packages/data-core/catalyst_data/corpus/filing_v3.py`
- `packages/data-core/catalyst_data/corpus/persisted_id.py`
- `packages/data-core/catalyst_data/corpus/streaming_publication.py`
- `packages/data-core/catalyst_data/candidate_recovery.py`
- `packages/data-core/catalyst_data/sec/`
- `packages/data-core/catalyst_data/pre_b6_probes.py`
- `packages/data-core/catalyst_data/retrieval/source_bundle.py`
- corresponding data-core and eval tests listed by `git status --short`.

## 2. Binding Documents

Read these in order before editing:

1. `docs/plans/2026-07-29-pre-b6-evidence-convergence-design.md`
2. `docs/plans/2026-07-29-pre-b6-evidence-convergence.md`
3. `docs/plans/2026-07-30-pre-b6-execution.md`
4. `docs/plans/2026-08-01-pre-b6-streaming-publication-design.md`
5. `docs/plans/2026-08-01-pre-b6-streaming-publication.md`
6. this handoff.

The 2026-08-01 streaming design is the binding amendment for corpus/FTS
publication. When older documents describe a whole-corpus in-memory build, the
streaming design wins.

## 3. Product Objective And Current Roadmap Position

Catalyst is an evidence-bounded market-attribution system. It must answer causal
market questions for a ratified 40-ticker universe using cutoff-safe news,
filings, market data, macro data, provenance, traceable retrieval, and explicit
abstention/error states. The immediate Pre-B6 goal is to certify the complete
source/corpus/lexical layer before dense embeddings are built on a GPU server.

Completed before this handoff:

- B2 provenance/update infrastructure, B3 deterministic corpus contracts, B4
  cutoff-safe lexical retrieval, and B5 attribution/runtime assurance were
  implemented in earlier commits/dirty changes.
- The 40-ticker B2-O backfill was executed. Polygon OHLCV and news, Finnhub news,
  SEC submissions/index/documents, FRED, and available FMP data are present in
  the candidate lineage. FMP paid-tier HTTP 402 gaps are explicitly optional and
  degraded, not silently treated as mandatory success.
- SEC S1/S2/S4 convergence succeeded. The frozen mandatory inventory has 688
  filing documents with fetched/extracted/provenance-valid evidence.
- Candidate v13 source recovery completed after an interrupted corpus build.
- Prebuild readiness passes with `prebuild_source_ready=true` and
  `sec_source_ready=true`.
- A v13 source snapshot manifest was produced with snapshot ID
  `e38ab4294b43830fa601f0b5f70c937cd7bb6a49dff9e483b169ce7c3053b2d4`.
- The original whole-corpus publisher was rejected by the 6 GiB memory gate.
  This was a real architecture issue, not a threshold-tuning issue. A persistent,
  resumable streaming publisher has been implemented but still needs final
  convergence described below.

Not completed:

- streaming implementation final GREEN and independent approval;
- production corpus and FTS publication;
- `postbuild_evidence_ready=true` / `sec_evidence_ready=true`;
- 40/40 corpus coverage probes and 40/40 lexical smoke probes;
- promotion of the v13 candidate;
- 12-case lexical baseline freeze;
- production source bundle export;
- GPU embedding, B6 retrieval arms, or attribution-effect experiment.

## 4. Candidate Database And Artifact State

Candidate DB:

`data/candidates/catalyst_b2e_final_v13.db`

Fresh read-only checks at handoff:

- `PRAGMA integrity_check`: `ok`.
- `PRAGMA foreign_key_check`: zero rows.
- `PRAGMA user_version`: `13`.
- `raw_assets`: 23,262.
- `normalized_provenance`: 263,300.
- `articles`: 164,481.
- `article_tickers`: 259,710.
- `filings`: 5,229.
- `filing_documents`: 1,659.

Source-recovery evidence:

`data/run_reports/pre_b6_candidate_reset_20260801.json`

Prebuild readiness evidence:

`data/run_reports/pre_b6_audit_20260801_final_prebuild/pre_b6_sec_readiness_audit.json`

Snapshot evidence:

`data/run_reports/pre_b6_snapshot_20260801_final.json`

Active pointer remains deliberately unchanged and still selects the old certified
baseline:

- pointer: `data/manifests/active_data_snapshot.json`
- snapshot ID:
  `d5e5f7fef11581c1f516bc484f64e31c1776a4202eb0c68aeada0bd0b98817c0`
- SHA256:
  `7cc49ba1fd5245a6b8700ba7c26638d54f1dbb644f0aeb7cbaff684aa349f97e`

Do not alter the active pointer until `pre-b6-promote` passes all postbuild and
40/40 probe gates.

The old protected root artifacts are absent after the local/iCloud recovery:

- `data/catalyst_dev_ws4b.db` is missing.
- `data/catalyst_eval_frozen_v2.db` is missing.

This causes approximately 16 artifact-only full-suite failures. Do not fabricate,
symlink, regenerate, skip, or change expected hashes for these files. Report this
as an environment test gap. It does not authorize weakening production gates.

## 5. Frozen Identity Inputs

- B2-O terminal run: `b2-e44bb9625f594d8f970150f884201a95`
- SEC S1 terminal run: `b2-82f0919d8c8043eab2bbb929125ed821`
- SEC S2 terminal run: `b2-e22e8b8560ab42a092b7e2c7da1980aa`
- SEC S4 terminal run: `b2-080ccdcd2842438f800f9f3777086334`
- Filing inventory ID:
  `89aec3341bd1a95a32e45eaec17a46d3ba1b692744d828bfa59710c2895aee38`
- Universe runtime manifest ID:
  `37c5da06c2defd8f4d18b68b3499bc4e05ecbb4c97bc0ac3be62a7d7aaea596a`
- Ratified inventory:
  `data/manifests/sec_filing_inventory_89aec3341bd1a95a32e45eaec17a46d3ba1b692744d828bfa59710c2895aee38.json`
- Universe manifest: `data/manifests/b2o_universe_20260723.json`
- B2-O plan: `data/manifests/b2o_plan_20260723.json`
- S2 cell IDs: `data/run_reports/b2e_s2_cell_ids.json`
- Convergence evidence: use the latest identity-complete artifact validated by
  the existing CLI/tests; inspect `data/run_reports/b2e_convergence_final.json`
  and the snapshot report rather than guessing a replacement.

## 6. Immediate Blocking Work: Three Streaming Findings

The previous amend agent was stopped for this handoff. No production process is
running. Its exact final status follows.

### Finding A: bounded corrupt-FTS suffix cleanup

Partially implemented in:

- `packages/data-core/catalyst_data/corpus/streaming_publication.py`
- `packages/data-core/tests/test_streaming_publication.py`

The implementation now persists `lexical_repair_checkpoint` and
`lexical_repair_cursor`, deletes invalid FTS suffix rows in reverse transactions
of at most 500 rows, batches FTS metadata cleanup, and exposes an
`after_fts_cleanup_batch` interruption hook.

The focused test reached expected deletion sizes `[500, 500]` and persisted
cursor `repair:1000`, but the final combined GREEN run was interrupted. Treat the
implementation as unverified.

First command:

```bash
.venv/bin/python -m pytest \
  packages/data-core/tests/test_streaming_publication.py::test_fts_resume_repairs_corrupt_batch_suffix_without_touching_selection \
  packages/data-core/tests/test_streaming_publication.py::test_corrupt_fts_suffix_cleanup_is_bounded_persisted_and_resumable \
  -q
```

### Finding B: checkpoint validation still materializes corpus-sized tuples

Not implemented. `_validated_fts_checkpoint()` currently builds both
`persisted_rows = tuple(...)` and `source_rows = tuple(...)` for an entire
committed range around lines 1250-1307 of
`streaming_publication.py`. This violates the bounded-memory contract even though
each logical FTS batch is capped at 500 today; validation must itself enforce and
prove bounded reads rather than relying on metadata.

Required TDD behavior:

- validate persisted and source rows in lockstep pages of at most 500;
- incrementally compare count, first/last IDs, UTF-8 bytes, and digest;
- never call `fetchall()` or construct a full-range tuple/list;
- reject count/digest/order/text mismatch without modifying the selected old
  corpus/lexical generation;
- add a regression that instruments cursor reads and proves no read exceeds 500
  rows for a large synthetic checkpoint.

### Finding C: final document staging batch omits the interruption hook

Not implemented. `_stage_document()` calls `after_staging_batch` after
non-final batches, but the final `_write_chunk_batch(..., complete=True)` returns
without calling it. A crash after the final commit therefore cannot be injected
or tested at the same boundary.

Required TDD behavior:

- call `failure_injector("after_staging_batch")` after every committed staging
  batch, including the final batch that marks the document complete;
- interruption after that hook must resume idempotently, not duplicate chunks,
  and not restage a completed document;
- cover both a one-batch document and a multi-batch document.

## 7. Required Verification Before Production

After Findings A-C are GREEN, run at least:

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_streaming_publication.py -q
.venv/bin/python -m pytest \
  packages/data-core/tests/test_candidate_recovery.py \
  packages/data-core/tests/test_chunk_overflow_regression.py \
  packages/data-core/tests/test_chunk_profile_streaming.py \
  packages/data-core/tests/test_index_builder.py \
  packages/data-core/tests/test_pre_b6_phase_gates.py \
  packages/data-core/tests/test_pre_b6_final_readiness.py \
  packages/data-core/tests/test_pre_b6_certification_gates.py \
  packages/data-core/tests/test_pre_b6_identity_gates.py \
  packages/data-core/tests/test_pre_b6_promotion_gates.py \
  packages/data-core/tests/test_source_bundle.py \
  -q
HF_HUB_OFFLINE=1 .venv/bin/python -m pytest packages/data-core -q
.venv/bin/python -m pytest packages/eval -q
git diff --check
git diff --cached --name-status
```

For the full data-core command, separate genuine failures from the known missing
protected-DB artifact failures. Any other failure blocks production.

Then perform a fresh independent read-only code review. It must specifically
reproduce Findings A-C, verify same-manifest rebuild failure preserves the old
selected FTS generation, verify persisted FTS corruption is detected at cutover,
verify all five reconciliation reasons, and verify the legacy publisher API is
unchanged. Do not let the implementation agent approve its own work.

## 8. Production Execution Order After Approval

Only after independent approval:

1. Record branch/HEAD/status, disk, candidate source-table logical hashes,
   integrity/FK/user_version, active pointer, and absence of duplicate executors.
2. Run the official `pre-b6-audit` command and require
   `prebuild_source_ready=true` and `sec_source_ready=true`.
3. Verify or regenerate the official `pre-b6-snapshot` manifest from the same
   candidate and frozen identities. Never hand-edit it.
4. Run exactly one official `pre-b6-publish-corpus` process. It must call
   `publish_streaming_corpus_with_resource_gate`; do not call internal helpers or
   the legacy publisher. Monitor progress about every 15 minutes. Resume the same
   build identity after a typed interruption; do not launch a second executor.
5. Re-run the postbuild audit. Require `postbuild_evidence_ready=true`,
   `sec_evidence_ready=true`, 688/688 mandatory filing evidence, and matching
   snapshot/corpus/lexical identities.
6. Run official `pre-b6-promote` with its probe cutoff and output artifact. Require
   40/40 corpus coverage probes and 40/40 lexical smoke probes before promotion.
7. Verify the new active pointer, promoted DB SHA, integrity/FK, and unchanged old
   baseline snapshot.
8. Freeze the 12-case lexical baseline through the production evaluation path.
9. Export the production source bundle through
   `catalyst_data.retrieval.source_bundle`. It must be bound to the active
   snapshot, corpus manifest, lexical state, universe, inventory, convergence
   evidence, postbuild report, and probe report. It must contain no vectors.
10. Verify bundle checksums and idempotent re-export, then stop for manager review.

Do not run provider backfill during this phase. The required source data has
already been fetched. Do not run GPU/model imports, embeddings, dense indexing,
reranking, B6 retrieval arms, or attribution-effect experiments.

## 9. Safety And Failure Rules

- Never print `.env` values. Only report credential names as `SET`/`MISSING` if a
  command actually needs them. This phase should be offline.
- Never modify source tables to make a gate pass. Corpus, lexical, staging, and
  publication state are derived; source identity must remain stable.
- Never bypass the 6 GiB gate, reduce the 40-ticker universe, reduce the 688-file
  mandatory inventory, weaken 40/40 probes, or classify mandatory failures as
  optional.
- Never use `git reset --hard`, `git checkout --`, `git clean`, destructive DB
  replacement, or ad hoc SQL repair against the active snapshot.
- If corpus publication fails, preserve the previous selected corpus/lexical pair
  and report the exact build ID, phase, checkpoint, exception, source hashes, and
  DB integrity. Fix code test-first before resuming.
- A report saying `PIPELINE IN PROGRESS` is not completion. Wait for the one
  executor to finish or stop at an explicit, evidence-backed blocker.
- Do not stage, commit, push, create a PR, or upload to GPU without manager
  approval.

## 10. Required Final Report

Return:

- exact branch, HEAD, and Git status;
- files changed during this session;
- Findings A-C RED/GREEN evidence and independent-review verdict;
- all test commands with pass/fail counts and known artifact-only exceptions;
- candidate source hashes before/after, integrity/FK/user_version;
- build ID, snapshot ID, corpus manifest ID, lexical identity and row counts;
- `prebuild_source_ready`, `postbuild_evidence_ready`, `sec_source_ready`, and
  `sec_evidence_ready` values;
- 40/40 corpus and 40/40 lexical probe evidence;
- promoted DB path/SHA and active pointer identity;
- lexical baseline path/hash;
- source bundle path, bundle ID, SHA256, chunk/document counts, and explicit
  confirmation that it contains no vectors;
- protected baseline status and missing protected-root artifact caveat;
- explicit confirmation: no network/provider calls, no secrets, no GPU/B6, no
  stage/commit/push/PR unless separately authorized;
- one final decision: `READY_FOR_GPU_MANAGER_REVIEW` or `BLOCKED`, with concrete
  reasons.

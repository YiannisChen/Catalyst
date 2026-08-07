# Pre-B6 Streaming Review Report

**Review date:** 2026-08-01 (Asia/Shanghai)

**Repository:** `/Users/yiannischen/Projects/Catalyst`

**Branch / HEAD:** `recovery/b2o-data-readiness` /
`7009a24d0c44883a03c99633d2975bfdd07519aa`

## Decision

`APPROVED_STREAMING_SCOPE_NOT_READY_FOR_GPU`

The three bounded/resumable streaming-publication findings are implemented and
independently verified in this review. No additional amend is required for those
three findings before production execution.

The project is not ready for GPU embedding because the production v13 corpus,
FTS state, postbuild readiness, 40/40 probes, promotion, lexical baseline, and
identity-bound source bundle have not been produced.

## Independent Verification

Fresh commands and results:

```text
.venv/bin/python -m pytest packages/data-core/tests/test_streaming_publication.py -q
40 passed in 4.00s

Pre-B6 focused matrix
152 passed in 4.68s

.venv/bin/python -m pytest packages/eval -q
133 passed in 0.42s

HF_HUB_OFFLINE=1 .venv/bin/python -m pytest packages/data-core -q
1454 passed, 3 skipped, 16 failed in 99.22s
```

All 16 full-suite failures are caused by the absent protected artifacts:

- `data/catalyst_dev_ws4b.db`
- `data/catalyst_eval_frozen_v2.db`

The failures are limited to `test_s3_corpus_items.py`,
`test_s3_frozen_db_readonly.py`, `test_s3_timestamp_canonical.py`, and
`test_s3_watermark.py`, each failing while opening or copying one of those
missing files. No production or streaming test failed. These artifacts must not
be fabricated, re-pinned, or replaced to make the suite green.

`git diff --check` passed. The Git index is empty. No production executor, GPU
builder, or streaming pytest process was running at review time.

## Findings A-C

### A. FTS corrupt-suffix cleanup

Approved. Invalid generation rows are deleted in transactions of at most 500,
the repair checkpoint/cursor is persisted, interrupted cleanup resumes, and the
old selected corpus/lexical generation remains unchanged. The regression test
verifies two 500-row committed deletions and cursor `repair:1000` before resume.

### B. Checkpoint validation memory bound

Approved. `_validated_fts_checkpoint()` now consumes persisted and source rows
in lockstep through `fetchmany(500)`, incrementally validates order, count,
UTF-8 bytes, first/last IDs, and digest, and does not materialize a full range.
The instrumented large-range test rejects unbounded iteration and any read above
500 rows.

### C. Final staging-batch hook

Approved. `_stage_document()` calls `after_staging_batch` after the final
transaction that marks the document complete. Single-batch and multi-batch tests
inject interruption at that boundary and prove resume does not delete, restage,
or duplicate completed chunks.

## Current Data And Publication State

Candidate:

`data/candidates/catalyst_b2e_final_v13.db`

Fresh database checks:

- integrity: `ok`
- foreign-key violations: `0`
- user version: `13`
- `corpus_chunks`: `0`
- `corpus_manifest`: `0`
- `lexical_index_state`: `0`

Latest certified prebuild state:

- new source snapshot ID:
  `e38ab4294b43830fa601f0b5f70c937cd7bb6a49dff9e483b169ce7c3053b2d4`
- `prebuild_source_ready=true`
- B2-O overall readiness: `complete`
- required provenance: `complete`
- comparable news gate: `complete`
- `sec_source_ready=true`
- mandatory SEC evidence fetched/extracted/provenance-valid: `688/688/688`
- `chunked_count=0`
- `postbuild_evidence_ready=false`
- `sec_evidence_ready=false`

The active pointer still selects the old certified snapshot
`d5e5f7fef11581c1f516bc484f64e31c1776a4202eb0c68aeada0bd0b98817c0`
with SHA256
`7cc49ba1fd5245a6b8700ba7c26638d54f1dbb644f0aeb7cbaff684aa349f97e`.
This is correct until promotion succeeds.

Two older source bundles exist, but they bind snapshot IDs `8163448c...` and
`9297a51c...`, not the current v13 snapshot. They are not valid GPU inputs for
this execution.

## Remaining Pre-Embedding Work

1. Run official prebuild audit and verify/regenerate the current v13 snapshot.
2. Run exactly one official streaming corpus/FTS publication to completion.
3. Require postbuild and SEC evidence readiness, including 688/688 chunked SEC
   evidence.
4. Run 40/40 corpus coverage and 40/40 lexical smoke probes.
5. Promote the candidate and verify the active pointer and promoted DB identity.
6. Freeze the 12-case lexical baseline for the promoted corpus.
7. Export and checksum an identity-bound, vector-free source bundle.
8. Independently review those artifacts. Only then may the bundle be uploaded to
   the GPU environment.

## Final Gate

Current decision: `CONTINUE_PRE_B6_PRODUCTION_EXECUTION`.

Do not start embedding yet.

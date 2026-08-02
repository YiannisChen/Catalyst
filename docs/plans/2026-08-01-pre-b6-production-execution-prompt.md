# Prompt: Complete Pre-B6 Production Publication

Work in the existing repository at `/Users/yiannischen/Projects/Catalyst`.

Use:

- `superpowers:using-superpowers`
- `superpowers:executing-plans`
- `superpowers:systematic-debugging`
- `superpowers:test-driven-development` if any defect is found
- `superpowers:verification-before-completion`

Read completely:

1. `docs/plans/2026-08-01-catalyst-pre-b6-session-handoff.md`
2. `docs/plans/2026-08-01-pre-b6-streaming-review-report.md`
3. `docs/plans/2026-07-30-pre-b6-execution.md`
4. `docs/plans/2026-08-01-pre-b6-streaming-publication-design.md`

Manager decision: the three streaming findings A-C are independently approved.
Do not perform another speculative amend round. Proceed to the remaining
official Pre-B6 production pipeline. If a real failure appears, stop the pipeline,
reproduce it with a RED test, apply the smallest fix, run the complete focused
matrix, and report it for review before resuming.

## Safety Rules

- Preserve the current dirty worktree. Do not reset, checkout, stash, clean, or
  create a different worktree.
- Do not stage, commit, push, create a PR, or modify `.env`.
- Do not call providers or the network; all source backfill is already complete.
- Do not modify source rows or weaken readiness/resource/probe gates.
- Do not fabricate the missing protected root DB artifacts.
- Do not start GPU embedding, dense indexing, RRF, reranking, B6, or attribution
  experiments.
- Run exactly one corpus publisher. Never start a second executor.
- A final response of `PIPELINE IN PROGRESS` is not acceptable. Monitor and wait
  until completion or an explicit evidence-backed blocker.

## Frozen Inputs

- candidate DB: `data/candidates/catalyst_b2e_final_v13.db`
- source snapshot manifest: `data/run_reports/pre_b6_snapshot_20260801_final.json`
- expected snapshot ID:
  `e38ab4294b43830fa601f0b5f70c937cd7bb6a49dff9e483b169ce7c3053b2d4`
- universe: `data/manifests/b2o_universe_20260723.json`
- B2-O plan: `data/manifests/b2o_plan_20260723.json`
- filing inventory:
  `data/manifests/sec_filing_inventory_89aec3341bd1a95a32e45eaec17a46d3ba1b692744d828bfa59710c2895aee38.json`
- inventory ID:
  `89aec3341bd1a95a32e45eaec17a46d3ba1b692744d828bfa59710c2895aee38`
- B2-O terminal run: `b2-e44bb9625f594d8f970150f884201a95`
- S1 terminal run: `b2-82f0919d8c8043eab2bbb929125ed821`
- S2 terminal run: `b2-e22e8b8560ab42a092b7e2c7da1980aa`
- S4 terminal run: `b2-080ccdcd2842438f800f9f3777086334`
- S2 cell IDs: `data/run_reports/b2e_s2_cell_ids.json`
- baseline snapshot ID:
  `d5e5f7fef11581c1f516bc484f64e31c1776a4202eb0c68aeada0bd0b98817c0`
- baseline SHA256:
  `7cc49ba1fd5245a6b8700ba7c26638d54f1dbb644f0aeb7cbaff684aa349f97e`

Resolve the convergence evidence path from the snapshot report and current
production validation code. Do not guess or hand-edit an evidence artifact.

## Execution

### Phase 1: preflight

Record branch, HEAD, dirty status, empty index, free disk, candidate integrity,
FK, user version, active pointer, source-table logical hashes, and running
executor count. Require no existing publisher. Confirm current derived counts are
zero or explain any resumable streaming state before proceeding.

Run the focused streaming/Pre-B6 verification matrix from the review report. Do
not rerun the known protected-artifact failures as a fake blocker.

### Phase 2: source and snapshot gates

Use the official `python -m catalyst_data.b2o pre-b6-audit` entrypoint with the
frozen inputs. Require:

- `prebuild_source_ready=true`
- `sec_source_ready=true`
- B2-O overall readiness `complete`
- required provenance `complete`
- comparable gate `complete`
- 688 fetched, extracted, and provenance-valid mandatory SEC documents.

Verify the existing snapshot manifest against the candidate through the official
production path. If it is stale, regenerate it only through
`pre-b6-snapshot`. Record the final snapshot ID and source hashes.

### Phase 3: corpus and FTS publication

Run exactly one official
`python -m catalyst_data.b2o pre-b6-publish-corpus` command. It must route through
`publish_streaming_corpus_with_resource_gate`. Do not invoke internal helpers or
the legacy publisher.

Monitor about every 15 minutes and report bounded progress: build ID, phase,
documents, chunks, source/chunk bytes, RSS, DB size, disk, and errors. Do not
interrupt a healthy process. On a typed resource stop or external interruption,
verify DB integrity and resume the same build identity.

When it exits, require a successful selector cutover with matching snapshot,
corpus, and lexical identities. Record build ID, manifest ID, document/chunk/FTS
counts, peak bounded buffers, and source hashes before/after.

### Phase 4: postbuild and promotion

Run the official postbuild readiness path. Require:

- `postbuild_evidence_ready=true`
- `sec_evidence_ready=true`
- `chunked_count=688`
- no mandatory missing evidence
- corpus and lexical state bound to the final snapshot.

Run official `pre-b6-promote` with a production cutoff and probe output. Require:

- corpus coverage probes `40/40`
- lexical smoke probes `40/40`
- no future/cutoff violations
- promotion succeeds only after the probes pass.

Verify promoted DB integrity/FK/SHA and that the active pointer now selects the
new snapshot. Verify the old baseline snapshot and SHA remain unchanged.

### Phase 5: lexical baseline and source bundle

Freeze the 12-case lexical baseline through the existing production evaluation
path. Record artifact path, identity, and SHA256.

Export the source bundle through
`catalyst_data.retrieval.source_bundle`. It must bind the promoted snapshot,
corpus manifest, lexical state, universe, filing inventory, convergence evidence,
postbuild report, and 40/40 probe report. Verify:

- checksum file passes;
- re-export is idempotent;
- chunk/document counts match the promoted corpus;
- no vectors or model outputs are present;
- it is not either of the two older July bundles.

Stop here. Do not upload the bundle to a GPU.

## Final Report

Return all commands and results plus:

- branch/HEAD/Git status;
- candidate source hashes before/after and integrity/FK/user version;
- prebuild and postbuild readiness fields;
- snapshot, build, corpus, lexical, inventory, universe, convergence and probe
  identities;
- document/chunk/FTS counts and bounded-memory evidence;
- 40/40 corpus and 40/40 lexical probe results;
- promoted DB path/SHA and active pointer;
- lexical baseline path/SHA;
- source bundle path/ID/SHA/counts and explicit `contains_vectors=false`;
- protected-artifact caveat;
- explicit confirmation of no network/provider/GPU/B6/stage/commit/push/PR.

End with exactly one verdict:

- `READY_FOR_GPU_MANAGER_REVIEW`, or
- `BLOCKED: <specific verified blocker>`.

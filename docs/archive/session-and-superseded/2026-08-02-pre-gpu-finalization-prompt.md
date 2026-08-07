# Prompt: Finish Catalyst Pre-GPU Artifacts

Work in place at `/Users/yiannischen/Projects/Catalyst`.

Use:

- `superpowers:using-superpowers`
- `superpowers:executing-plans`
- `superpowers:test-driven-development`
- `superpowers:systematic-debugging`
- `superpowers:requesting-code-review`
- `superpowers:verification-before-completion`

Read:

1. `docs/plans/2026-08-02-pre-gpu-readiness-review.md`
2. `docs/plans/2026-07-29-pre-b6-evidence-convergence-design.md`
3. `docs/plans/2026-07-29-pre-b6-evidence-convergence.md`, Tasks 17-19
4. `docs/plans/2026-07-30-pre-b6-execution.md`, Tasks 11-13

Promotion is complete. Do not rebuild corpus/FTS, rerun providers, or create a
new snapshot. Complete only the final lexical-baseline and source-bundle gates.

Preserve the dirty worktree. Do not reset, checkout, stash, clean, stage, commit,
push, or create a PR. Do not print secrets. No provider/network calls, model
downloads, GPU, embeddings, dense index, B6, reranker, or attribution run.

## Frozen Production Identities

- DB:
  `data/snapshots/catalyst_b2o_e38ab4294b43830fa601f0b5f70c937cd7bb6a49dff9e483b169ce7c3053b2d4.db`
- DB SHA256:
  `b73056310d59c46298a4439bb91764e7d0eb95d98089d8c5ec2bbd9c83f5332d`
- snapshot:
  `e38ab4294b43830fa601f0b5f70c937cd7bb6a49dff9e483b169ce7c3053b2d4`
- corpus:
  `50d68fc76208840cb55c809a95c3f74a9e74e125dd291633212a70731559f2a4`
- universe:
  `37c5da06c2defd8f4d18b68b3499bc4e05ecbb4c97bc0ac3be62a7d7aaea596a`
- postbuild report:
  `data/run_reports/pre_b6_audit_20260801_production_postbuild/pre_b6_sec_readiness_audit.json`
- probe report:
  `data/run_reports/pre_b6_probe_20260801_production.json`
- probe report ID:
  `64772d1aa746a57d69130e59f9e12be19a27c4ced7f976a16189b9b8f09a0fdc`
- postbuild readiness ID:
  `0ba3cbcdc42cea5fe9aea7e3b340defd25ab31e05bcf6b71d36c0143171bbd9f`
- probe cutoff: `2026-07-31T23:59:59Z`
- 12 cases:
  `packages/eval/golden_set/pre_b6_attribution_cases_v1.jsonl`

## Task 1: Bounded Source-Bundle Export, RED -> GREEN

Current `export_source_bundle()` uses `fetchall()` and corpus-sized containers.
Do not run it against production yet.

Add regression tests first, then minimally amend
`packages/data-core/catalyst_data/retrieval/source_bundle.py`:

1. Read served chunks in deterministic `chunk_id` order with `fetchmany(500)` or
   smaller. No `fetchall`, whole-corpus `list`/`tuple`, `records`, `hashes`, or
   `seen_ids` collection.
2. Preserve the exact schema `1.1.0` source-bundle ID formula. Stream the canonical
   JSON identity containing `ordered_chunk_record_hashes` so synthetic fixtures
   produce byte-identical IDs to the existing implementation.
3. Use two bounded passes if necessary: pass 1 validates rows and computes the
   identity; pass 2 writes `chunks.jsonl` atomically. Memory must not scale with
   chunk count.
4. Detect duplicates/order drift using only the previous chunk ID.
5. Stream SHA256 file hashing; do not use `Path.read_bytes()` for large artifacts.
6. Verify an existing bundle by streaming its JSONL in lockstep with a bounded DB
   cursor. Do not use `read_text().splitlines()` or expected-record collections.
7. Preserve all current gates: current corpus, snapshot binding, FTS binding,
   postbuild report, certified 40/40 probe report, filing_v3 presence, row count,
   content/metadata hashes, atomic temp-directory rename, corruption refusal,
   idempotent re-export, and no vectors.
8. Add a static test rejecting `fetchall`, large-file `read_bytes`, corpus-sized
   containers, and unbounded JSONL reads in the production exporter.
9. Add a large synthetic test instrumenting cursor reads and proving every read is
   <=500 and peak retained records/hashes do not grow with corpus size.

Run at minimum:

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_source_bundle.py -q
.venv/bin/python -m pytest \
  packages/data-core/tests/test_corpus_embedder_grain.py \
  packages/data-core/tests/test_pre_b6_final_review.py \
  packages/data-core/tests/test_pre_b6_certification_gates.py \
  packages/data-core/tests/test_pre_b6_promotion_gates.py -q
```

## Task 2: Deterministic 12-Case Lexical Baseline, RED -> GREEN

No production lexical-baseline freezer currently exists. Implement one in the
eval package without adding an eval import to data-core.

Suggested ownership:

- production generator:
  `packages/eval/catalyst_eval/probes/lexical_baseline.py`
- CLI/script:
  `packages/eval/scripts/freeze_pre_b6_lexical_baseline.py`
- tests:
  `packages/eval/tests/test_pre_b6_lexical_baseline.py`

Contract:

1. Load exactly the 12 ordered tracked cases (`c01` through `c12`) and reject
   duplicates, missing cases, unknown slots, or ticker/order drift.
2. Load and cryptographically verify the certified production probe report using
   existing production validators.
3. For each case ticker, use that ticker's frozen lexical smoke anchor,
   `query_terms`, and cutoff from the certified 40/40 probe report. Do not invent
   a question or cutoff.
4. Re-run cutoff-safe lexical retrieval against the promoted DB with
   `candidate_depth=20` and `top_k=20`, exact ticker filter, exact corpus manifest,
   and the frozen cutoff.
5. Persist ordered top-20 result identities and retrieval fields needed for later
   B6 comparison. Include case ID, slot, ticker, query terms, cutoff, anchor IDs,
   result chunk/document IDs, ranks/raw lexical scores, source class,
   available_at, and corpus/index identities.
6. Compute and persist `look_ahead_count=0`; reject any result whose
   `available_at` is after cutoff.
7. Bind schema version, snapshot, corpus, universe, probe report ID,
   postbuild-readiness ID, tokenizer/profile revisions, and exact ordered case
   pack hash.
8. Exclude generation timestamp/path from the deterministic baseline ID.
9. Write atomically to:
   `data/eval_reports/lexical_baseline_50d68fc76208840cb55c809a95c3f74a9e74e125dd291633212a70731559f2a4.json`.
10. Prove identical reruns yield the same baseline ID and content; identity or
    cutoff drift must fail closed.

Run all eval tests after the focused RED/GREEN tests.

## Task 3: Independent Review Gate

Request a fresh read-only reviewer. It must verify:

- source-bundle identity parity and bounded memory;
- corruption and idempotent-reuse behavior;
- no weakening of readiness/probe/current-manifest gates;
- exact 12-case order and identity binding;
- cutoff safety and `look_ahead_count=0`;
- no data-core -> eval dependency;
- no vectors, network, provider, model, or GPU work.

Amend and re-review until APPROVED. Do not self-approve.

## Task 4: Generate Final Artifacts

After approval:

1. Re-verify active pointer, DB SHA, integrity/FK/user version, current corpus and
   lexical identities, postbuild readiness, and 40/40 probe report.
2. Freeze the 12-case lexical baseline twice and prove deterministic identity.
3. Export the production source bundle to `data/source_bundles/` using the bounded
   exporter. Monitor RSS and disk. Do not run a second exporter.
4. Re-export and require idempotent reuse with full streaming verification.
5. Verify `checksums.sha256`, manifest identity, exact 295,506 chunk count,
   filing_v3 presence, non-empty JSONL, sorted unique chunk IDs, content hashes,
   and `contains_vectors=false`.
6. Confirm the new bundle binds the frozen snapshot/corpus/probe/postbuild IDs and
   is not either older July bundle.
7. Recheck active DB SHA and pointer unchanged.

## Final Report

Report exact test commands/counts, reviewer verdict, files changed, active DB
identity/SHA/integrity/FK, lexical baseline path/ID/SHA/case count/look-ahead,
source bundle path/ID/SHA/chunk count/checksum verification/RSS, and Git status.

Explicitly confirm no network/provider/model/GPU/B6/stage/commit/push/PR.

End with exactly one verdict:

- `READY_FOR_GPU_MANAGER_REVIEW`, or
- `BLOCKED: <specific verified blocker>`.

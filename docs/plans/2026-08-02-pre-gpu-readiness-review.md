# Pre-GPU Readiness Review

**Date:** 2026-08-02 (Asia/Shanghai)

**Decision:** `BLOCKED_SOURCE_CLASS_PROVENANCE`

The production artifacts pass structural, identity, coverage, and temporal
gates, but a final distribution review found that source provenance is not fit
for a production attribution index. Do not embed the frozen bundle below. It is
retained as audit evidence while source classification is corrected and the
derived corpus artifacts are rebuilt.

## Frozen Identity Chain

- Snapshot ID:
  `e38ab4294b43830fa601f0b5f70c937cd7bb6a49dff9e483b169ce7c3053b2d4`
- Snapshot DB SHA256:
  `b73056310d59c46298a4439bb91764e7d0eb95d98089d8c5ec2bbd9c83f5332d`
- Schema version: `13`
- Universe manifest ID:
  `37c5da06c2defd8f4d18b68b3499bc4e05ecbb4c97bc0ac3be62a7d7aaea596a`
- Corpus manifest ID:
  `50d68fc76208840cb55c809a95c3f74a9e74e125dd291633212a70731559f2a4`
- Probe report ID:
  `64772d1aa746a57d69130e59f9e12be19a27c4ced7f976a16189b9b8f09a0fdc`
- Postbuild readiness ID:
  `0ba3cbcdc42cea5fe9aea7e3b340defd25ab31e05bcf6b71d36c0143171bbd9f`

## Database And Retrieval State

- Active pointer selects the promoted snapshot above.
- Full-file DB SHA matches the active pointer.
- `PRAGMA integrity_check`: `ok`
- Foreign-key violations: `0`
- Documents: `165,809`
- Served chunks: `295,506`
- FTS rows: `295,506`, mode `fts5`
- Corpus and FTS bind the same corpus manifest.
- Prebuild, postbuild, SEC source, and SEC evidence readiness are all true.
- Mandatory SEC evidence is `688/688` through fetch, extraction, provenance,
  and chunking.
- Corpus coverage and lexical probes are both `40/40`.

## Frozen Lexical Baseline

- Baseline ID:
  `a7012502e6ca5dc5cafd3aac6ae787120e19722494186a79a5deb7426012b9dc`
- File SHA256:
  `21900cdeecd581d2f7ac1a1be660f74ee83234af82b8816e256dea4a38d171d1`
- Cases: `12`
- Look-ahead violations: `0`
- Snapshot, corpus, universe, probe, and readiness identities match the
  promoted production chain.

## Frozen Source Bundle

- Source bundle ID:
  `f656b9cb8af98dfaecf6d431fc621aef37abc524b2f669feaea5afd6684aab06`
- Chunk rows: `295,506`
- `checksums.sha256` file SHA256:
  `bc28a616d94ee60a29a1798dbc301cfc1a7fa739c784b3c01eeae43928e9b817`
- Both listed artifact checksums pass.
- The bundle contains only `chunks.jsonl`, `source_bundle_manifest.json`, and
  `checksums.sha256`; no vector artifact is present.
- Bundle identities match the promoted snapshot, corpus, universe, probe, and
  postbuild readiness reports.

## Blocking Quality Finding

The promoted corpus contains 164,150 eligible news documents. Their source
class distribution is:

- `aggregated_unknown`: 146,794 (`89.43%`)
- `analysis_opinion`: 13,800 (`8.41%`)
- `corporate_press_release`: 3,535 (`2.15%`)
- `reported_news`: 21 (`0.01%`)

All 164,481 rows in `articles.source_class` are null, so corpus publication
reclassifies them. The classifier checks the `finnhub.io` aggregator host before
the explicit publisher. Consequently, Finnhub rows carrying publishers such as
CNBC, MarketWatch, and SeekingAlpha are collapsed to `aggregated_unknown`.
Agents treat that class as unknown-origin support and record source-support
degradation, so this distribution can materially weaken attribution output.

Fix the deterministic precedence and publisher taxonomy with explicit tests,
rebuild from the promoted source snapshot into a new candidate corpus, rerun
40/40 probes, freeze a replacement lexical baseline, and export a replacement
source bundle. The replacement must receive a new corpus and bundle identity.

## Verification

- Pre-GPU focused data-core matrix: `61 passed`
- Eval suite: `138 passed`
- Full data-core: `1461 passed, 16 failed, 3 skipped`
- The 16 failures all require the absent protected legacy fixtures
  `data/catalyst_dev_ws4b.db` or `data/catalyst_eval_frozen_v2.db`; they do not
  exercise the promoted v13 database or the Pre-GPU implementation.
- `git diff --check`: clean before final staging.

## GPU Boundary

The current `f656b9cb...` source bundle is forbidden for production embedding.
After source-class remediation and artifact regeneration,
`packages/data-core/scripts/build_embeddings_gpu.py` is still legacy
frozen-eval only: it reads `clean_assets` and is forbidden for the replacement
bundle. B6 must implement and test `build_corpus_embeddings_gpu.py` against the
source-bundle contract, including a fixture-scale mock round trip and strict
output import.

After that review gate, upload the source-bundle directory as an immutable unit
and verify its `checksums.sha256` on the GPU host before model loading. The
embedding output must preserve the source bundle ID and every `chunk_id`; local
import must reject missing, duplicate, extra, or reordered identities and must
not mutate the promoted source snapshot.

# Catalyst Pre-B6 System Review Prompt

You are an independent principal architect and adversarial reviewer. Perform a
read-only, evidence-based review of the Catalyst repository before any GPU
embedding work starts.

Repository:

- canonical path: `/Users/yiannischen/Projects/Catalyst`
- `/Users/yiannischen/Desktop/Catalyst` is a symlink to the same repository
- review the current branch and HEAD; report both explicitly

## Product Requirements

Catalyst must:

1. Answer evidence-bounded attribution questions for a ratified 40-ticker
   universe using market data, company news, SEC filings, macro data, and
   fundamentals.
2. Enforce point-in-time cutoffs and prevent look-ahead leakage. It must cite
   retrievable evidence and abstain when evidence is insufficient.
3. Improve attribution accuracy and answer usefulness over the original
   10-ticker prototype through broader coverage, deterministic data identity,
   reproducible retrieval, and explicit assurance rather than by claiming that
   more tickers alone improve quality.
4. Be credible systems-engineering work for open-source participation and
   study-application evidence: clear contracts, tests, operational safety,
   observability, reproducibility, and maintainable boundaries matter.
5. Keep ingestion, normalization, reconciliation, chunking, deduplication, and
   lexical indexing runnable on an 8 GB Mac. Only embedding and model-heavy
   reranking may require the GPU server.
6. Survive interruption and provider failure through immutable identities,
   lineage-bound resume, bounded retries, rate-limit handling, provenance,
   readiness gates, and atomic snapshot/promotion.

## Current Claimed State

Do not trust these claims without verifying code, tests, Git history, manifests,
and the promoted database:

- B2-B5 implementation exists; B6 GPU execution has not started.
- B2-O uses schema v12 and full checkpoint identity.
- Ratified coverage plan has 5,771 source cells.
- Mandatory cells are complete: Polygon OHLCV 280/280, Polygon news 3280/3280,
  Finnhub news 2040/2040, SEC 40/40, FRED 11/11.
- FMP is optional/degraded: 75/120 successful and 45/120 terminal HTTP 402.
- terminal ingestion lineage ends at
  `b2-e44bb9625f594d8f970150f884201a95`.
- certified data snapshot ID is
  `d5e5f7fef11581c1f516bc484f64e31c1776a4202eb0c68aeada0bd0b98817c0`.
- promoted DB is
  `data/snapshots/catalyst_b2o_d5e5f7fef11581c1f516bc484f64e31c1776a4202eb0c68aeada0bd0b98817c0.db`
  with SHA-256
  `7cc49ba1fd5245a6b8700ba7c26638d54f1dbb644f0aeb7cbaff684aa349f97e`.
- corpus/lexical manifest ID is
  `29010eef2bcf00258293463ab3d6cb77ef07a07dc2a48bf04b7b4ecdfd1e8a33`.
- corpus and FTS each contain 164,353 active rows.
- `active_data_snapshot.json`, generated snapshot manifests, databases, and run
  reports are local operational artifacts and must not be committed.
- the two historical protected DB fixtures are unavailable after a local iCloud
  incident. Sixteen artifact-bound tests therefore cannot run; this must not be
  misreported as a product-code pass.

## Required Inspection

Inspect, at minimum:

- `docs/plans/2026-07-21-b2-b7-technical-contracts.md`
- all B2-B7 implementation plans
- `docs/plans/2026-07-23-b2o-data-readiness-design.md`
- `docs/plans/2026-07-23-b2o-data-readiness.md`
- B2 ingestion, migrations, checkpoints, retry, provenance, readiness, snapshot,
  publish, and promote code
- B3 tokenizer, chunk profiles, reconciliation, deduplication, and corpus
  manifest code
- B4 cutoff, FTS, retrieval contracts, and evaluation foundation
- B5 attribution graph, gates, ranking, trace, status, and assurance
- B6 design for dense retrieval, RRF, reranking, GPU export/import, and fallback
- B7 API, trace, attribution, metrics, and evaluation plans
- relevant production tests and the current Git diff/commit

Run safe offline tests and read-only SQLite queries where useful. Do not call
providers, expose `.env` values, mutate any DB, run GPU/model downloads, stage,
commit, push, or create a PR.

## Adversarial Questions

1. Does v12 preserve every v11 column, default, index, trigger, rollback
   guarantee, legacy checkpoint behavior, and full `(run_id, cell_id)` identity?
2. Is `checkpoint_id` deterministic and collision risk acceptable? Can endpoint
   siblings overwrite each other anywhere in execution, audit, resume, snapshot,
   publish, or promotion?
3. Are readiness and FMP HTTP 402 degradation exact, lineage-bound, and limited
   to optional FMP cells?
4. Is `DataSnapshotManifest` deterministic and complete? Are source tables
   immutable during corpus publication, and are derived tables correctly
   excluded?
5. Is creating `idx_index_state_chunk_id` during corpus publication preferable
   to a schema migration, and is its transaction/failure behavior correct?
6. Do B3 chunking, source classification, deduplication, tombstones, tokenizer
   pinning, and manifest identity match the contracts without hidden fallbacks?
7. Do B4 lexical retrieval and future dense retrieval use the same universe,
   cutoff, filters, candidate identity, and comparison protocol?
8. Is the B6 handoff checksummed, resumable, model/revision-bound, batch- and
   memory-bounded, and safe for float precision, partial uploads, and retries?
9. Is reranker enable/disable decided by predeclared case-level metrics rather
   than subjective examples? Are no-relevant-evidence and unjudged cases sound?
10. Does B5 attribution produce better grounded answers, or only more
    infrastructure? Identify missing evaluation evidence needed to prove answer
    quality across the 40 tickers.
11. Are secrets, provider terms, rate limits, local paths, generated artifacts,
    and protected data handled safely?
12. Is the repository understandable and credible to an external maintainer, or
    does it contain overengineering, duplicate ownership, stale plans, weak
    tests, or unverifiable claims?

## Required Output

Return:

1. Executive verdict: `GO_TO_B6_G`, `GO_WITH_PRECONDITIONS`, or `NO_GO`.
2. A requirement traceability matrix with `verified`, `partial`, `missing`, or
   `contradicted`.
3. Findings ordered P0-P3, each with exact file:line evidence, impact, and the
   smallest defensible fix.
4. Plan-versus-implementation drift matrix for B2-O and B2-B7.
5. Data quality and 40-ticker coverage assessment, separating coverage breadth
   from demonstrated attribution accuracy.
6. Pre-GPU handoff checklist, including artifacts, hashes, models, revisions,
   resource formulas, resume semantics, and rollback.
7. Minimal amendment list; explicitly say when no amendment is required.
8. Evaluation plan that can demonstrate whether answers improved over the
   original 10-ticker system and whether reranking should remain enabled.
9. Portfolio/open-source assessment: what is strong evidence, what is weak or
   inflated, and what should be shown publicly.

Label every important statement as verified fact, inference, or unknown. Do not
accept completion reports as evidence when the repository contradicts them.

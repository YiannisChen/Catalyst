# WS4B Step 4a: Embed Readiness Plan

## Architecture Summary

This phase makes data-core ready for GPU vectorization by fixing the canonicality
model (per-association instead of per-article), building the index_state work queue,
cleaning Plane-2 residue from the clean layer, and surfacing SEC scope. All work is
local CPU/SQL. No GPU, no cloud, no embedding-model load, no LanceDB writes.

After this phase, the downstream GPU step reads index_state rows with status=pending,
embeds them, writes to LanceDB, and marks them embedded. It trusts index_state and
does not recompute eligibility.

## Dependency Graph

S1 (canonicality) → S2 (index_state) → S3 (clean residue) → S4 (SEC report) → S5 (gate)

S1 must complete before S2 because index_state requires the per-association canonicality
contract. S3 and S4 are independent of each other. S5 gates everything.

A prose-readiness sub-gate runs after S2 (independently reportable, same pattern as
Phase 0's sub-gate before the FRED network phase).

## Mutation vs Code Classification

S1: Mutation (additive ALTER TABLE migration + recompute dedup over dev DB).
S2: Mutation (index_state INSERT — the first population of the embed work queue).
S3: Mutation (DELETE clean_assets rows for fmp_fundamentals + polygon_ohlcv).
S4: Pure code (read-only reporting, no DB writes).
S5: Pure code (read-only gate function).

Pre-flight backup before S1. Frozen SHA immutable throughout
(0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf).

## S1 — Per-Association Canonicality

Root cause: index_builder.build_article_records fetches ALL articles (SELECT * FROM articles,
no WHERE clause), and _assert_guards enforces l1_count == COUNT(*) FROM articles. In a
deduped corpus, duplicate-losers must NOT be embedded — they add noise and waste GPU budget.
The canonicality model must move from article grain to association grain.

Design contract:

Add is_canonical INTEGER DEFAULT 1 to article_tickers via additive ALTER TABLE — safe, no
column drop, no existing data loss. The migration is gated on PRAGMA table_info check so
re-runs are idempotent.

Recompute cross-source dedup so that within each dedup_group_id exactly one article_tickers
association is canonical. For a singleton group (one article with one ticker association),
that sole association is canonical. For a multi-article group, the select_canonical winner's
association for each ticker is canonical; losing associations for that ticker are non-canonical.
For a multi-ticker article, the same article_id can have one canonical association for AAPL
and one canonical for TSLA — this is correct OR-semantics at the association grain.

Define EMBED ELIGIBILITY in a shared function in index_builder or a new eligibility module:
an article is index-eligible iff is_rag_eligible = 1 AND exists (SELECT 1 FROM article_tickers
WHERE article_id = ? AND is_canonical = 1). This drops pure duplicate-losers (articles with
no canonical association) but keeps singleton-winners. An article with is_rag_eligible=0
(quality-filtered) is never eligible regardless of canonicality.

Keep articles.is_canonical as a derived convenience column (recomputed as: 1 if any
association is canonical, else 0), but the source of truth for embed eligibility becomes
article_tickers.is_canonical.

Bite-tests:

a) A singleton article with one ticker association and dedup_group_id set — after recompute,
its article_tickers association is canonical=1, and it IS embed-eligible.

b) A pure duplicate-loser article — three articles in one dedup_group_id, select_canonical
picks one winner. The two losers have zero canonical associations and are NOT embed-eligible.
The winner has canonical=1 for its ticker and IS eligible.

c) Every dedup_group_id has exactly one canonical association per ticker — zero-canonical
and multi-canonical both fail.

d) A multi-ticker article that wins canonical for AAPL but loses for MSFT: embed-eligible
for AAPL, not for MSFT.

e) Existing Gate P0 tests still pass after recompute (zero blank refs, zero NULL dedup, zero
dup-canon violations, zero phantom prose checkpoints).

File targets:

Create: packages/data-core/catalyst_data/eligibility.py
Modify: packages/data-core/catalyst_data/storage/sqlite.py (ALTER TABLE migration)
Modify: packages/data-core/catalyst_data/dedup/cross_source.py (per-association canonical)
Modify: packages/data-core/catalyst_data/phase0_remediate.py (recompute entrypoint)
Modify: packages/data-core/tests/test_phase0_remediate.py (bite-tests a-e)

## S2 — Build and Persist index_state

Design contract:

Update build_article_records to select by S1 eligibility: WHERE is_rag_eligible = 1 AND
EXISTS canonical association. The query joins articles with article_tickers WHERE
is_canonical=1, GROUP BY article_id. Update _assert_guards: l1_count == eligible article
count (not total articles), and SUM of ticker refs == COUNT of canonical article_tickers
associations (ticker-lossless over eligible set). L2 eligibility unchanged (body >= 800 chars,
only for articles that pass the eligibility filter). All source tiers included —
tier/opinion suppression is retrieval-time, not index-time.

Persist the generated L1/L2 records into index_state. Each row has:
corpus_item_id = article_id (or filing_id for filings), source_kind = 'article' or 'filing',
chunk_id (article_id::l1, article_id::l2s0001, etc.), chunk_level (l1/l2), content_hash
(SHA-256 of NFC-normalized content_text), content_text (the string to embed), status =
'pending', plus metadata columns (provider, source_type, source_tier, tickers_json,
reference_date, published_utc). Key uniqueness on (chunk_id, content_hash) enables
idempotent re-runs: INSERT OR IGNORE so re-running with unchanged content inserts zero new
rows; a changed article gets a new content_hash so its old rows stay (stale) and new rows
with new hash are inserted as pending.

Provide a local dry-run manifest (index_summary, already exists but update to use eligibility):
total L1/L2 counts, per-source counts, per-tier counts, L2-eligible percentage, eligible vs
total articles count, a "what will be embedded" review artifact. This manifest is read-only
and can run before or after index_state population.

Gate invariants for the prose-readiness sub-gate after S2:

index_state pending count for articles equals eligible L1 + eligible L2. One L1 per eligible
article. Re-run is idempotent — zero new rows if content unchanged. Ticker-lossless: SUM of
ticker refs in index_state L1 records equals COUNT of canonical article_tickers associations.

Filing index_state: EXACTLY ONE L1 per rag-eligible filing (existing behavior preserved —
build_filing_records already gates on is_rag_eligible=1; no change needed). L2 only when
body >= 800 chars.

SEC filing counts are small (7 filings, 9 docs, 6 rag_eligible). See S4 for scope decision.

File targets:

Modify: packages/data-core/catalyst_data/index_builder.py (eligibility filter, guards, persist)
Modify: packages/data-core/tests/test_index_builder.py (eligibility tests, idempotency tests)
Potentially create: packages/data-core/scripts/build_index_state.py (CLI entrypoint for dry-run
and persist modes)

## S3 — Clean Plane-2 Residue

Design contract:

DELETE FROM clean_assets WHERE source_type IN ('fmp_fundamentals', 'polygon_ohlcv').
Structured/fundamental plane must not sit in the prose clean layer. Total rows affected: 6,849
(3,500 fmp_fundamentals + 3,349 polygon_ohlcv). Note that index_builder reads articles and
filings directly — it NEVER reads clean_assets. So this deletion has zero effect on
index_state or embedding eligibility. The clean_assets table previously held 3,500
fred_macro rows which P0-4 already deleted; this completes the cleanup.

Optional (stated, not required for gate): flag dead fmp_fundamentals Bronze (raw_assets) for
later pruning. Do not delete Bronze in this phase — raw_assets have FK dependencies.

Gate: clean_assets contains only prose source_types (polygon_news, finnhub_company_news).
Total before: 31,777, after: 24,928. Zero fmp_fundamentals, zero polygon_ohlcv, zero
fred_macro.

File targets:

Modify: packages/data-core/catalyst_data/phase0_remediate.py (add clean-prose-residue command)
Modify: packages/data-core/tests/test_phase0_remediate.py (S3 clean test)

## S4 — SEC Scope Surfaced

Design contract:

SEC coverage is thin: 7 filings, 9 documents, 6 rag_eligible. This is not enough for
meaningful SEC-aware retrieval. Surface these counts explicitly in the index_summary
manifest and in the pre-GPU gate report with a clear marker: SEC_SCOPE_THIN=7. The default
decision is embed-as-is (the six rag-eligible filings enter index_state as pending L1/L2
records) and defer full SEC backfill to a later Step 4b or Step 5. Do not silently ship
thin coverage — the manifest must show the count so a human can decide whether to proceed.

If the orchestrator wants to gate on SEC coverage, a threshold can be added (e.g.,
require >= 100 filings before GPU spend). Default: no threshold, embed-as-is with the count
surfaced.

File targets:

Modify: packages/data-core/catalyst_data/index_builder.py (index_summary to include SEC counts)
No structural changes — reporting only.

## S5 — Pre-GPU Gate

Design contract:

Extend coverage_audit.py with an audit_embed_readiness function (or extend audit_gate_p0
with additional checks). This gate asserts:

S1 invariant: For every dedup_group_id in article_tickers, per-ticker canonical count is
exactly 1: SUM(is_canonical) GROUP BY dedup_group_id, ticker yields no zeros and no >1s.

Eligibility contract: COUNT of eligible articles (S1 definition: is_rag_eligible=1 AND has
canonical association) == index_state pending L1 count for articles.

Clean layer: zero clean_assets rows for fmp_fundamentals, polygon_ohlcv, fred_macro.

One L1 per eligible article: no duplicate article_id in index_state L1 records.

Ticker-lossless: SUM of ticker references in index_state L1 records equals COUNT of canonical
article_tickers associations.

SEC scope surfaced: manifest includes SEC filing count.

Zero FRED macro in index_state (Plane-2 — already enforced by P0-4, re-assert).

Gate returns pass/fail with per-check granularity, same pattern as audit_gate_p0.

Bite-tests: (a) gate fails when index_state L1 count != eligible article count; (b) gate
fails when a dedup group has zero canonical associations; (c) gate fails when clean_assets has
fmp rows; (d) gate passes on a fully compliant fixture.

File targets:

Modify: packages/data-core/catalyst_data/coverage_audit.py (add audit_embed_readiness)
Modify: packages/data-core/tests/test_coverage_audit.py (S5 bite-tests)

## Cross-Cutting Requirements

This phase is 100% local CPU/SQL. No GPU, no cloud, no embedding-model load, no LanceDB
writes. index_state is populated as a PENDING queue only; vectorization is a separate phase.

Pre-flight backup before S1 (the first mutation). Frozen SHA immutable:
0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf.

TDD-first per step: write the bite-test, prove it fails (red), implement the minimal fix,
prove it passes (green), run the targeted test suite. Fail-stop gates after each step.

Data-core only. Never touch packages/app or packages/agents. No new dependencies — all work
uses sqlite3, hashlib, unicodedata (stdlib). No network calls.

Secrets: none in this phase — no provider keys needed.

## Handoff Contract to GPU Phase

The GPU phase (Step 4b) receives index_state as its sole input. Its contract:

Input: SELECT * FROM index_state WHERE status = 'pending' ORDER BY source_kind, chunk_id.
Output: vectors written to LanceDB (table/index TBD by Step 4b) with chunk_id as key.
On success: UPDATE index_state SET status = 'embedded' WHERE chunk_id IN (embedded set).
The GPU phase does NOT recompute eligibility, canonicality, or content hashing — it trusts
index_state exactly as populated by Step 4a. If index_state is empty (no pending rows), the
GPU phase is a no-op and should report that.

## Open Questions

Q1: Should articles.is_canonical be dropped after S1, or kept as a derived convenience?
Default: keep as derived (recomputed on each dedup run), not dropped.

Q2: Should fmp_fundamentals Bronze (raw_assets) be pruned now or deferred?
Default: deferred — flag as tech-debt, prune in a later cleanup phase. The raw_assets have
FK references that make deletion non-trivial, and the rows are 3,500 — negligible storage.

Q3: Should SEC backfill be gated (require N filings before GPU)?
Default: no threshold — embed the 6 eligible filings as-is, surface the thin count prominently,
let the orchestrator decide.

Q4: Should filing index_state use the same pending/embedded status pattern as articles?
Default: yes — same table, same status column, same handoff contract. Filings already have
a corpus_item_id and source_kind='filing' distinct from source_kind='article'.

## File Manifest

Created:
packages/data-core/catalyst_data/eligibility.py
packages/data-core/scripts/build_index_state.py (optional CLI)

Modified:
packages/data-core/catalyst_data/storage/sqlite.py (ALTER TABLE migration)
packages/data-core/catalyst_data/dedup/cross_source.py (per-association canonical)
packages/data-core/catalyst_data/index_builder.py (eligibility filter, guards, persist)
packages/data-core/catalyst_data/phase0_remediate.py (S1 recompute + S3 clean)
packages/data-core/catalyst_data/coverage_audit.py (audit_embed_readiness)
packages/data-core/tests/test_phase0_remediate.py (S1 + S3 bite-tests)
packages/data-core/tests/test_index_builder.py (S2 eligibility + idempotency tests)
packages/data-core/tests/test_coverage_audit.py (S5 bite-tests)
packages/data-core/tests/test_eligibility.py (S1 eligibility tests)

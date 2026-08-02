# Pre-B6 Streaming Corpus Publication Implementation Plan

> **For maintainers:** execute every behavior test-first and preserve the shared dirty worktree.

**Goal:** implement bounded, resumable corpus and FTS publication without changing
the certified v13 source identity or bypassing the 6 GiB gate.

**Architecture:** Add build-scoped derived publication tables and a served-row
compatibility view. Stream source documents, inventory identity, reconciliation,
and FTS batches; perform a selector-only final transaction.

**Tech Stack:** Python 3.12, SQLite/FTS5, pytest, pinned BGE-M3 tokenizer.

---

### Task 1: Identity and inventory RED/GREEN

- Add shuffled, Unicode, null, `news_v2`, and `filing_v3` identity equivalence tests.
- Add a two-pass binary-order inventory iterator and streaming legacy exporter.
- Verify exact chunk/content/metadata hashes and legacy `manifest_id`.

### Task 2: Persistent staging RED/GREEN

- Add build/document/chunk/inventory tables outside migrations and source identity.
- Add deterministic article and filing keyset iterators.
- Enforce 100-document, source-byte, 500-chunk, and chunk-text-byte limits.
- Commit each bounded batch and checkpoint completion only with the final chunk batch.
- Verify restart regenerates only incomplete documents and creates no duplicates.

### Task 3: SQL reconciliation RED/GREEN

- Add build-scoped reconciliation delta rows computed by SQL joins and anti-joins.
- Cover batch-size and resume independence plus deterministic replacement IDs.
- Return summaries and paged iterators only.

### Task 4: Batched lexical build RED/GREEN

- Append unpublished manifest FTS rows in transactions of at most 500 rows.
- Persist count/digest readiness and verify parity with staged chunks.
- Preserve selected lexical state after injected batch failures.

### Task 5: Atomic cutover RED/GREEN

- Add final readiness validation and selector-only `BEGIN IMMEDIATE` cutover.
- Inject failures after staging, manifest, reconciliation, FTS, and before cutover.
- Verify the old selected corpus/lexical pair remains intact at every point.

### Task 6: Estimator and CLI RED/GREEN

- Correct eligibility and UTF-8 accounting.
- Use instantaneous RSS and a phase-max bounded headroom formula.
- Raise a typed resumable stop for an oversized document.
- Route `pre-b6-publish-corpus` exclusively through streaming publication.

### Task 7: Static and bounded-memory verification

- Reject `fetchall` and corpus-sized result containers in the publish path.
- Compare fixed-size and 10x synthetic corpora with deterministic retention metrics
  and a subprocess RSS check.
- Run focused data-core tests, expanded data-core excluding only absent protected-DB
  artifacts, eval probe tests, `git diff --check`, and staged-file inspection.
- Do not run production builds, probes, promotion, bundles, network, or GPU work.

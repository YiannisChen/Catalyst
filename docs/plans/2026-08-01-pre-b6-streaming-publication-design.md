# Pre-B6 Streaming Corpus Publication Design

**Status:** binding amendment for Pre-B6 corpus and lexical publication.

**Goal:** publish the v13 article and filing corpus plus matching FTS state with
bounded memory, resumability, deterministic legacy identity, and selector-only
atomic cutover under the existing 6 GiB resource gate.

## Storage choice

Use schema-compatible internal derived tables in the candidate SQLite database,
keyed by `build_id`. Do not add source columns, alter source rows, or increment
`PRAGMA user_version`. `DataSnapshotManifest` already excludes corpus, lexical,
and other derived publication state, so these internal tables do not invalidate
the certified v13 source identity. Keeping staging in the candidate database also
gives batch commits and final selector updates one SQLite durability boundary;
a sidecar would require a non-atomic cross-database publish protocol.

The streaming store contains build metadata, document checkpoints, immutable
build chunks, normalized manifest inventory, reconciliation deltas, and lexical
readiness metadata. A served compatibility view selects rows for the current
manifest from the generation store and falls back to legacy `corpus_chunks` for
older fixture-built databases. Existing source tables and the legacy small-test
API remain unchanged.

## Streaming and resume contract

Eligible articles and filing documents are read in deterministic keyset order.
The producer retains no more than 100 source documents and a configured UTF-8
source-byte budget. Chunk writes retain no more than 500 chunks and a configured
chunk-text UTF-8 byte budget. Every bounded chunk batch commits. A document is
marked complete in the same transaction as its final chunk batch. Resume deletes
and regenerates only the rows for an incomplete document; completed documents are
never duplicated.

An individual source document that cannot fit the configured source budget, or
whose generated chunk batch cannot fit the enforced chunk limits, raises a typed
resumable resource stop. Progress is emitted at least every 30 seconds or 100
completed documents and reports documents, chunks, source/chunk bytes, and current
instantaneous RSS.

## Identity and inventory

Manifest inventory remains normalized in SQLite. Pass one validates counts and
computes an inventory digest over `chunk_id COLLATE BINARY`; pass two streams the
same rows into the canonical manifest identity byte sequence. The resulting
`manifest_id` is byte-for-byte equal to the legacy formula:

`SHA256(canonical_json(identity_with_sorted_active_chunk_inventory))`.

The persisted manifest JSON is a bounded header with count, digest, and inventory
storage metadata. A streaming legacy exporter reconstructs the historical JSON
shape without `json.loads` or a corpus-sized Python list.

## Reconciliation and lexical state

Reconciliation is expressed as SQL anti-joins and build-scoped delta rows. New,
content-changed, metadata-only, eligibility-lost, removed, profile-replaced, and
disappeared-child cases are computed in SQL. Replacement identity is the binary
`MIN(chunk_id)` for the replacement profile. APIs return counts and page/iterator
access rather than corpus-sized collections.

FTS rows for the unpublished manifest are appended in transactions of at most 500
rows. Each committed batch records its row count and digest contribution. The old
manifest's FTS rows and singleton lexical selector remain untouched during build.

## Atomic cutover and failure

The final `BEGIN IMMEDIATE` validates source identity, staged document/chunk
counts, inventory and lexical digests, reconciliation readiness, and matching
corpus/lexical identities. It then updates only the current corpus selector,
lexical selector, and build status. No corpus-sized copy occurs in cutover.

Failures after staging, manifest construction, reconciliation, any FTS batch, or
before final cutover leave the previously selected corpus and lexical pair intact.
The same `build_id` resumes idempotently from committed checkpoints.

## Resource model

The estimator applies exactly the production eligibility filters and measures
UTF-8 bytes. Required headroom is the maximum of bounded phases, including the
tokenizer/cache allowance, SQLite page cache, largest eligible source document,
source buffer, chunk buffer, and fixed interpreter overhead. The official gate
remains strict: `instantaneous_rss + required_headroom < 6 GiB`.

## Rejected alternatives

1. Raising or bypassing the resource gate is forbidden and leaves the unbounded
   implementation unchanged.
2. A sidecar staging database bounds memory but cannot provide a short atomic
   selector cutover across two SQLite databases.
3. Replacing or bumping canonical source schema v13 conflates derived publishing
   state with certified source identity and is unnecessary.

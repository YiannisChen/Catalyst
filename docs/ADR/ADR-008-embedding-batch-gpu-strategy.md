# ADR-008: Server Batch Embedding and Index Artifacts

- Status: Accepted for B6
- Owner: `catalyst-data`

## Context

Full-corpus BGE-M3 embedding is not a reliable workload for the architect's 8 GB Mac. The lexical baseline, corpus reconciliation, and zero-network tests must remain usable locally while dense artifacts are built on an authorized server/GPU.

## Decision

- The primary dense model is `BAAI/bge-m3`, 1024 dimensions.
- The first authorized server build pins the exact Hugging Face model and tokenizer revisions; the model name alone is insufficient.
- Embedding consumes active chunks from one published `CorpusManifest`, never legacy whole raw assets or a mutable DB query without a manifest.
- The batch job emits vectors, a chunk-ID row mapping, and an `IndexManifest` containing corpus manifest ID, model/tokenizer revisions, normalization mode, dtype, dimension, row count, and artifact SHA-256 hashes.
- Vector generation and index assembly are separate steps so artifacts can be verified before LanceDB publication.
- Incremental builds reuse vectors by `content_hash`, update metadata by `metadata_hash`, and tombstone removed chunks. A candidate full rebuild remains available at the current corpus size.
- Mac and CI run lexical retrieval and fixture-sized dense adapter tests without requiring the full model or GPU.
- No automatic fallback to a different embedding model is allowed. A different model creates a distinct manifest and requires an explicit design amendment.

## Consequences

- Local development does not depend on a GPU.
- Dense retrieval remains reproducible and tied to a precise corpus identity.
- Model or chunk-profile changes cannot silently reuse incompatible vectors.

## Implementation anchors

- `packages/data-core/catalyst_data/storage/lancedb_store.py`
- future B6 embedding/index commands under `packages/data-core/scripts/`
- `docs/ADR/ADR-002-hybrid-rag-retrieval.md`
- `docs/plans/2026-07-19-catalyst-provider-provenance-and-chunking-design.md`
- `docs/plans/2026-07-21-b2-b7-technical-contracts.md`

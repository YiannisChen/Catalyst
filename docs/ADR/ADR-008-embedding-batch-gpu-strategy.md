> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# ADR-008: Embedding Batch and GPU Strategy for bge-m3 (P1)

**Status:** Proposed  
**Date:** 2026-05-04  
**Decision:** Separate **cloud-GPU batch embedding generation** from **local CPU index build**, with explicit artifact layout, pinning, and coverage linkage to frozen eval Silver rows.

## Context

P0 shipped with SQL-only retrieval after [W-15](docs/decisions/W-15-t07-deferred-to-p1.md) deferred LanceDB-backed hybrid retrieval: bge-m3 embedding of the full `clean_assets` corpus on available Mac CPU/MPS hardware was outside the sprint budget. P1 restores vector retrieval but must not bind day-to-day development to an on-machine GPU.

The immutable eval frozen corpus (`data/catalyst_eval_frozen.db`) remains the lineage anchor. For P1 execution through at least T04, the operative consumption artifact is `data/catalyst_eval_frozen_v2.db`, and its `clean_assets` table is the authoritative textual row set for the T01 embedding batch.

## Decision

- **Primary path:** Run a dedicated **batch embedding job** on a **cloud GPU** (e.g., Colab, Lambda, RunPod) that embeds **every** `clean_assets` row required for frozen eval, using **`BAAI/bge-m3`** with **1024-dimensional** output vectors.
- **Separation of concerns:** The batch job emits **immutable artifacts on disk**; local P1-T02 consumes those artifacts to build `data/lancedb_gold/eval_frozen/` without performing full-corpus embedding on the laptop.
- **Artifact layout (normative for P1-T01 acceptance):**
  - `data/embeddings/bge_m3_eval_frozen_v2.npy` — row-aligned matrix or agreed serialized layout documented in the build script README.
  - `data/embeddings/asset_id_index.json` — stable ordering mapping embedding row index ↔ `asset_id` (and any minimal metadata needed for joins).
  - `data/embeddings/manifest.json` — DB fingerprint, model pin, row count, dtype/dim, and output hashes for reproducibility.
- **Reproducibility:** Record **SHA256** hashes for `*.npy` and `*.json` (and any sidecar manifests) in preflight/report headers alongside existing DB fingerprint fields.
- **Model pinning:** Pin **exact** model revision (HF commit hash or tarball SHA256) in the embedding script documentation and freeze metadata — not only the string `bge-m3`.
- **Coverage invariant:** T01 embeds **all qualifying L1 `clean_assets` rows** in `data/catalyst_eval_frozen_v2.db` with a **1:1 embedding row per qualifying L1 row**; gaps are failures, not silent skips. L2 sentence vectors are produced later in P1-T03 after T03-pre locks chunking parameters.
- **Privacy / secrets:** Batch jobs MUST NOT commit API keys or private URLs into the repo; use environment-injected secrets only. Embedding inputs are Silver text already present in the frozen DB artifact — treat the job as **data-processed-internal**, not public redistribution.
- **Fallback (documented, non-default):** If cloud GPU is unavailable, a smaller **CPU-suitable** embedding model (e.g., MiniLM-class, **384 dims**) remains a contingency described in `docs/plans/2026-05-04-p1-p2-architecture-plan.md` §3 Risk Register — it implies a **separate manifest and dimension change** across the Lance schema and ADR-002/ADR-009 assumptions; invoke only with an explicit waiver update.

## Alternatives considered

- **Inline embedding on laptop for full corpus:** Rejected for P1 predictability — W-15 already established economic infeasibility on default hardware.
- **Single combined script that embeds + builds Lance on GPU:** Rejected — couples infra to LanceDB versioning and complicates repeatable local audits; separation keeps “generate vectors” and “assemble index” independently verifiable.
- **Skipping SHA256 pinning:** Rejected — breaks Tier-A reproducibility narrative and frozen-sample integrity for committee defense.

## Consequences

**Positive**

- Retrieval quality work unblocks without mandating developer GPUs.
- Artifacts can be regenerated with a documented manifest when the frozen DB revision changes.

**Negative**

- Operational overhead: secure transfer of embedding artifacts between cloud and repo-adjacent storage.
- Model supply-chain risk — mitigated via revision pinning.

**Follow-up tasks**

- Implement `scripts/build_embeddings_gpu.py` (per architecture plan P1-T01) and document runbook + env vars.
- Wire P1-T02 index build to consume precomputed embeddings (no duplicate inference path in CI by default).

## References

- `docs/full-version-execution-spec.md` — retrieval / eval reproducibility framing (§§5, 12, 13).
- `docs/full-version-design-delta.md` — tiered retrieval context (§2); note dimensional assumptions must stay consistent with Gold schema.
- `docs/decisions/W-15-t07-deferred-to-p1.md` — deferral rationale and P1 re-entry criteria.
- `docs/plans/2026-05-04-p1-p2-architecture-plan.md` — P1-T01, ADR numbering, risk register (`2026-05-04-p1-p2-architecture-plan.md` §0–1).

## Implementation notes

- **Consumption path:** Embedding artifacts feed `packages/data-core` Lance index build tooling (extended `scripts/build_index.py` per plan — described here only).
- **`clean_assets` linkage:** SQLite `asset_id` is the join key between `asset_id_index.json` and Silver rows loaded into `gold_chunks`.
- **Dimensional constant:** Lance `vector` column and any numpy outputs must assert **1024** for canonical bge-m3 runs.

### Related ADRs

- **ADR-002** — Hybrid BM25 + vector + rerank direction (embedding model complements hybrid search).
- **ADR-009** — Chunking ladder and rerank contract downstream of embeddings.
- **ADR-004** — When Lance is live, Layers 1–2 activation policy consumes `hybrid_search` results.
- **ADR-010** — Golden-set and gate protocol; embeddings must never be tuned to cheat frozen gates.

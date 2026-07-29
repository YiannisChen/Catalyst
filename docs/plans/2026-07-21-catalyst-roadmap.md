# Catalyst Delivery Roadmap

- Status: binding
- Scope: a non-toy local RAG/agent workbench, not a SaaS product or research paper
- Execution rule: one package at a time, focused tests before canonical suites, stage only after review, no commit or push without architect authorization

## Stage 1 — Backend + API

### B1. Repository and contract baseline

Remove superseded plans and experiments, repair package boundaries, make active READMEs honest, and restore trustworthy package test baselines. Agents may depend on data-core but never eval. Eval consumes plain runtime artifacts through its own adapter.

### B2. Data update and provenance

Finish the two-stage OHLCV-first update service. Add request/page attempt records, append-only raw responses, normalized provenance, pagination, partial-result semantics, plan drift detection, status, cancellation, and checkpoint-based reruns.

### B3. Corpus and chunking

Normalize each provider item as an independent domain record. Implement `news_v2` and `filing_v2`, exclude raw JSON and numeric arrays, reconcile corpus changes, emit tombstones, and publish versioned corpus manifests atomically.

### B4. Retrieval baseline

Create the minimal Eval Foundation schemas before any comparison pool is persisted. Build cutoff-safe FTS5/BM25 retrieval with ticker, time, and evidence-type filters. Produce a retrieval trace and use the same cutoff contract later for dense and reranked paths.

### B5. Attribution workflow

Add deterministic market, sector, and peer context. Add a new deterministic Context Builder ahead of the existing Miner → Critic → Router → Judge → Validator → Finalizer workflow, and strengthen that workflow with typed outputs, hypothesis prerequisites, counter-evidence, explicit abstention, and one RunAssuranceRecord per run.

### B2-E. Pre-B6 evidence convergence (blocking)

Binding: `2026-07-29-pre-b6-evidence-convergence-design.md` and
`2026-07-29-pre-b6-evidence-convergence.md`.

B2-O produced a v12 promoted snapshot with complete news/OHLCV/FRED cells but
**without SEC filing body text** and with FMP lineage gaps. Before any GPU embedding:

1. Migration **v13** table rebuild + persisted `filing_documents.document_id` +
   **filing_v3** (max 384 / target 320 / overlap 48).
2. SEC **three independent root plan families**: S1 submissions, S2 `sec_filing_index`,
   S4 `sec_document` (each first run `parent_run_id=NULL`); SEC plan-cell record
   v2 carries full endpoint identity; S3 freezes `inventory_id` + requiredness;
   **sec_source_ready** then DataSnapshotManifest, then corpus, then
   **sec_evidence_ready**.
3. `plan_checkpoint_reconciliation` / `apply` (request_count only) on candidate.
4. Composite `convergence_plan_hash` binds all three SEC plans, inventory and
   reconciliation evidence; gates on candidate → **PROMOTE auth** → single
   `promote_candidate()`; baseline SHA unchanged.
5. 40 corpus coverage invariants + 40 lexical smoke probes; 12-case lexical baseline.
6. B6-L **source_bundle_id** + explicit dense/RRF/rerank fixtures; B6-G only after GO.

Do not describe Stage 1 as complete while SEC body readiness or evaluation
baselines are missing.

### B6. Dense retrieval and reranker

GPU consumes a checksummed **source_bundle** of post-B2-E `corpus_chunks` (not
legacy frozen `clean_assets`). Combine lexical and dense candidates with RRF,
run a reranker without changing the candidate universe, persist retrieval-arm
outputs, and generate the union judgment pool. Final keep/kill waits for B7
under predeclared bootstrap gates (pilot n=12).

### B7. API, compact evaluation, and backend release

Expose stable Data, News, Chart, Attribution, Trace, and Run Status APIs. Grade and freeze 12 human-reviewed BenchmarkCase records, make named-case component decisions, generate a reproducible ResultPack and Markdown scorecard, and provide key-free offline data, agent, and eval quickstarts.

Exact formulas, identities, state machines, migration ownership, benchmark counts, and cross-package sequencing for B2–B7 are binding in `2026-07-21-b2-b7-technical-contracts.md`.

Stage 1 is complete when all backend APIs, offline quickstarts, package suites, cutoff checks, provenance checks, and runtime assurance checks pass. At that point Catalyst may be described as a non-toy backend workbench.

## Stage 2 — Frontend

### F1. Data and exploration

Implement update preview/confirmation/status, K-line charts, a synchronized news timeline, publisher/image metadata, original-source links, and corpus/index status.

### F2. Attribution and trace

Display ranked hypotheses, supporting and counter-evidence, abstention reasons, run integrity, retrieval stages, agent stages, model/index identities, latency, token usage, and cost. Finish the five-minute reviewer journey.

Stage 2 is complete when the local workbench can demonstrate one full update-to-attribution flow without hidden manual steps. This is the Portfolio Complete milestone.

## Explicit non-goals

- SaaS, accounts, billing, multi-tenancy, public deployment, or enterprise RBAC
- GraphRAG, graph databases, reinforcement learning, fine-tuning, or artificial multi-agent personas
- paper-style arm experiments, generalized financial-accuracy claims, or large benchmark sets
- generic framework extraction, PyPI publication, or additional providers before measured need

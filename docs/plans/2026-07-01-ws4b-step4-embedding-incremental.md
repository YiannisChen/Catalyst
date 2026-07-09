# WS4B Step 4 — First Full Embed + Daily Incremental (Implementation Plan)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> **Status:** PLAN ONLY — for Claude review. Do not implement, edit code, run embeddings, or commit.
> **The GPU run executes only after Claude reviews this plan AND the cloud environment is confirmed ready.**
> **PROVISIONAL — depends on Step 1 outcomes (ticker-scoping mechanism, index_builder API, L2 threshold, content_hash definition) and Step 3 outcomes (SEC filings table existence); re-validate after Steps 1–3 are executed and reviewed.**
> **Prerequisites:** Steps 1–3 reviewed/merged. `index_builder.py`, `index_state`, `index_manifests` exist (Step 1). SEC `filings` table may exist (Step 3). `articles` = 11,772+ canonical.

**Goal:** Embed the improved corpus (Polygon-tiered + SEC) once on cloud/GPU using bge-m3, then run daily incrementals forever. One vector per canonical `article_id` — never per-ticker. Full rebuild only for model/schema/architecture changes.

**Architecture:** The existing `lancedb_store.py` is refactored to operate on the article-level record builder output (from Step 1's `index_builder.py`) instead of the per-ticker `clean_assets` table. `index_state` tracks which article_ids are embedded and is populated ONLY after a real embed completes — never by dry-run. `index_manifests` records each build with `status='live'` (full) or `status='incremental'`. Incremental mode diffs `articles` ∪ `filings` vs `index_state` and only embeds the delta. A cloud handoff script packages the SQLite snapshot, LanceDB path, and model metadata for the GPU run; the GPU run produces a build manifest and retrieval smoke report. **All embedding imports (FlagEmbedding, lancedb) are Mac-guarded — the full+incremental embed paths are cloud-only.**

**Tech Stack:** Python 3.11+, BAAI/bge-m3 (1024-dim), BAAI/bge-reranker-v2-m3, LanceDB, FlagEmbedding, nltk PunktTokenizer. Cloud GPU (A10G/L4 or better). No new dependencies beyond existing `[vector]` extras.

---

## Task 1 — Refactor lancedb_store.py for Article-Level Architecture

### 1.1 Purpose

The current `lancedb_store.py` reads from `clean_assets` (per-ticker Silver table), producing one vector per (asset_id, ticker) with scalar `ticker` column and 384-dim vectors in the `chunks` table. The article-level architecture (Step 1) changes the source of truth: records come from `index_builder.build_index_records()` — one L1 record per canonical `article_id` with `tickers[]` (JSON array), `source_tier`, full provenance, and `content_hash`. No per-ticker duplication.

### 1.2 New LanceDB schema (prose)

The new `chunks_v2` table schema stores per-article records with these fields: `article_id` (canonical, e.g. "poly:abc123"), `parent_article_id` (same as article_id for L1; owning article_id for L2), `chunk_level` ("l1" or "l2"), `chunk_id` ("{article_id}::l1" or "{article_id}::l2sNNNN"), `source_type`, `source_tier` (1–6 integer), `publisher_name`, `article_url`, `published_utc` (ISO), `reference_date` (trading day), `title`, `tickers` (JSON array string for LanceDB WHERE compatibility, e.g. '["AAPL","MSFT"]'), `content_md` (full text for BM25), and `vector` (bge-m3 1024-dim float list).

### 1.3 Refactored functions (prose contracts)

- `_build_chunk_records_from_articles(articles: list[dict]) -> list[dict]` — pass-through adapter. Takes records from index_builder.build_index_records() (already have L1+L2 with chunk_ids, parent_article_id, tickers[], source_tier, provenance) and serializes `tickers` list to JSON string for LanceDB storage. Returns LanceDB-ready dicts. No SQL queries — just field serialization.
- `build_index_from_records(records: list[dict], lancedb_path: str, *, embedding_model, embedding_revision, use_fp16, batch_size) -> tuple[int, int]` — new public entry point. Embeds `content_md` via bge-m3, writes to `chunks_v2` table. Returns (n_chunks, n_unique_article_ids).
- Old `build_index(db_path, lancedb_path)` — preserved with deprecation warning for backward compatibility. Delegates to the old per-ticker `_SILVER_QUERY` → `_build_chunk_records` path. The old `chunks` table (3 rows, 384-dim) is preserved but unused after this step.
- `hybrid_search(table, query, *, ticker, date_range, top_k, embedding_fn)` — updated to use new schema fields. Ticker filter uses the `ticker_scope.filter_by_ticker()` post-filter from Step 1 (SQLite-based, Mac-safe). Returns new schema fields: `article_id`, `tickers`, `source_tier`, `publisher_name`, `title`, etc.

### 1.4 Constants update

`L2_ELIGIBLE_SOURCE_TYPES` expanded to `("polygon_news", "sec_filings")` — SEC filings (10-K, 10-Q) are long-form and benefit from L2 sentence-level retrieval. The L2 threshold default is 800 characters (aligned with Step 1 amendment D).

**Files:**
- Modify: `packages/data-core/catalyst_data/storage/lancedb_store.py`
- Test: `packages/data-core/tests/storage/test_lancedb_store_v2.py`

**Test assertions (prose, all mock — no real embedding):**
1. `_build_chunk_records_from_articles` produces exactly one L1 record per article (not per-ticker).
2. An article with 3 tickers produces one record with `tickers` containing all three as a JSON string.
3. Provenance fields (publisher_name, article_url, source_tier, title) are preserved in output.
4. L2 record has correct `parent_article_id` pointing to the owning L1 article_id.
5. Chunk_id format matches `{article_id}::l1` / `{article_id}::l2sNNNN` pattern.
6. `_build_chunk_records_from_articles` does not import lancedb or FlagEmbedding.

---

## Task 2 — Full Embed Pipeline (rebuild-index --mode full)

### 2.1 New module: `catalyst_data/embed_pipeline.py`

**Purpose:** The orchestration layer that ties together record building (Step 1) + embedding + LanceDB write + index_state/index_manifests population. **This module raises ImportError on Mac** — the full embed path requires FlagEmbedding and LanceDB, which are cloud/GPU only.

**Function contract:** `build_full_index(db_path, lancedb_path, *, embedding_model="BAAI/bge-m3", embedding_revision=None, use_fp16=True, batch_size=32, min_l2_chars=800, include_filings=True) -> dict`

**Pipeline steps (prose):**
1. Guard: assert FlagEmbedding and lancedb are importable. If not, raise ImportError with message "GPU/cloud only — full embed requires FlagEmbedding + LanceDB."
2. Call `index_builder.build_index_records(conn, min_l2_chars=...)` — builds L1+L2 records for all canonical articles. If `include_filings=True` and `filings` table exists, also builds records for each filing (one L1 per filing, L2 if description ≥ threshold).
3. Call `lancedb_store.build_index_from_records(records, lancedb_path, model)` — loads bge-m3 at the pinned `embedding_revision`, encodes all `content_md` texts in batches of `batch_size` (default 32), writes `chunks_v2` table (overwrites if exists).
4. Populate `index_state`: for each article_id in the build, INSERT OR REPLACE with `content_hash` (from index_builder), `indexed_build_id = build_id`, `indexed_at = now()`. **This is the ONLY place index_state is written — never by dry-run.**
5. Write `index_manifests` row with `status='live'`, `model_hash = embedding_revision`, `l1_count`, `l2_count`, `article_count`, `indexed_through_date = MAX(reference_date/filed_at)`, `corpus_hash = SHA-256 of sorted article_ids + filing_ids`.
6. Return manifest dict: `{build_id, l1_count, l2_count, n_vectors, article_count, filing_count, model_revision, embedding_dim: 1024, indexed_through_date, corpus_hash, status: "live", per_tier_distribution, elapsed_sec}`.

**Model pinning:** `--model-revision` records exact HF commit hash in `index_manifests.model_hash`. If not provided, latest available is used and recorded.

**Embed-once invariant:** 11,772 articles → 11,772 L1 vectors. Never per-ticker. Assert: `l1_count == article_count + filing_count` (if filings included). The `n_vectors = l1_count + l2_count` sum.

**Files:**
- Create: `packages/data-core/catalyst_data/embed_pipeline.py`
- Test: `packages/data-core/tests/test_embed_pipeline.py` (mock embedding — deterministic 1024-dim vectors, no GPU)

**Test assertions (prose, all mock):**
1. Full build manifest has all required keys: build_id, l1_count, l2_count, n_vectors, article_count, model_revision, embedding_dim=1024, indexed_through_date, status="live".
2. `l1_count == article_count` — one vector per canonical article_id, zero per-ticker duplication.
3. Index_state populated: `SELECT COUNT(*) FROM index_state WHERE indexed_build_id = ?` equals article_count.
4. Index_manifests row written with non-null model_hash and created_at.
5. Mac ImportError: when FlagEmbedding is unavailable, raises ImportError with message "GPU/cloud only".
6. Model revision is recorded in manifest exactly as provided.
7. Per-tier distribution is populated in manifest.
8. Corpus hash is non-null and deterministic for same input.

---

## Task 3 — Incremental Embed Pipeline (rebuild-index --mode incremental)

### 3.1 Purpose

The normal daily flow. After the update pipeline fetches new articles, this step diffs `articles` ∪ `filings` vs `index_state`, embeds only the delta, upserts into LanceDB, and updates `index_state`. This does NOT require a full rebuild — it is the normal daily path.

**Function contract:** `build_incremental_index(db_path, lancedb_path, *, embedding_model, embedding_revision, use_fp16, batch_size, min_l2_chars=800) -> dict`

**Pipeline steps (prose):**
1. Guard: same ImportError as full build.
2. Diff `articles` ∪ `filings` vs `index_state`: find article_ids where `index_state.article_id IS NULL` (new) OR `index_state.content_hash != articles.content_hash` (changed — title/description edited). Only these article_ids need re-embedding.
3. Call `index_builder.build_incremental_records(conn, stale_ids=...)` — builds L1+L2 records only for the delta article_ids.
4. If delta is empty, return `{new_count: 0, changed_count: 0, total_embedded: 0}` — zero-op, idempotent.
5. Embed only the delta records via bge-m3 → LanceDB upsert (INSERT OR REPLACE by `article_id` — LanceDB handles idempotent upsert natively).
6. Update `index_state`: INSERT OR REPLACE for each embedded article_id with `indexed_build_id = build_id`, `indexed_at = now()`. **index_state is written ONLY here — after successful embed.**
7. Write `index_manifests` row with `status='incremental'`, delta counts.
8. Return `{build_id, new_count, changed_count, total_embedded, elapsed_sec}`.

**Idempotency:** Second run with no new/changed articles returns `total_embedded=0`. Safe to call every day.

**Partial failure re-pick:** If embedding fails mid-batch, the failed article_id is NOT written to `index_state`. Next run, it reappears in the diff and gets re-embedded. LanceDB upsert is atomic per row — partial failures don't corrupt existing vectors.

**Files:**
- Modify: `packages/data-core/catalyst_data/embed_pipeline.py` (add `build_incremental_index`)
- Test: extend `packages/data-core/tests/test_embed_pipeline.py`

**Test assertions (prose):**
1. When index_state is empty, ALL articles appear in delta (full-embed equivalent).
2. When index_state covers all articles with matching content_hash, delta is empty → total_embedded=0.
3. One new article → only that article_id is embedded.
4. Metadata-only change (source_tier edit, same content_hash) → article NOT in delta (content_hash unchanged).
5. Index_state updated after incremental: new article has indexed_build_id set.
6. Index_manifests incremental row written with status='incremental'.
7. Second incremental run with no changes → total_embedded=0 (idempotent).

---

## Task 4 — CLI: rebuild-index --mode full|incremental|dry-run

### 4.1 Modify: `catalyst_data/cli_index.py` (Step 1+2 artifact)

**Three modes:**

| Mode | Description | Requires GPU | Writes LanceDB | Writes index_state |
|------|-------------|-------------|----------------|-------------------|
| `dry-run` | Build records only, print counts | No | No | No |
| `full` | Embed ALL articles+filings, create new LanceDB table | Yes | Yes (chunks_v2) | Yes (all) |
| `incremental` | Embed only new/changed since last index_state | Yes | Yes (upsert) | Yes (delta only) |

**New flags:** `--lancedb-path` (default: `data/lancedb_gold_v2/`), `--model-revision` (pin HF commit), `--batch-size` (default: 32), `--no-fp16`, `--no-filings` (exclude SEC), `--min-l2-chars` (default: 800).

**CLI guard:** `--mode full` prints a confirmation prompt listing approximate article count and time estimate. Requires explicit 'y' to proceed.

**Files:**
- Modify: `packages/data-core/catalyst_data/cli_index.py`
- Test: extend existing CLI tests

**Test assertions (prose):**
1. `--mode dry-run` exits 0 on Mac, prints record counts, no embedding imports.
2. `--mode full` exits with ImportError on Mac (no GPU deps).
3. `--mode incremental` exits with ImportError on Mac.
4. `--mode full` with mock embedding exits 0 (CLI wiring correct).

---

## Task 5 — Cloud Handoff Artifacts

### 5.1 New script: `scripts/cloud_handoff.py`

**Purpose:** Package and unpack artifacts for the cloud GPU run. Self-contained — the cloud operator only needs Python 3.11+ and the `[vector]` extras.

**Handoff IN (Mac → Cloud, prose):** Directory `handoff/` containing: (a) `catalyst_dev_ws4b.db` — SQLite snapshot copied from `data/`; (b) `handoff_manifest.json` — metadata: db_sha256, article_count, filing_count, embedding_model, embedding_revision, embedding_dim=1024, reranker_model, lancedb_output_path, created_at_utc; (c) `README.md` — cloud operator instructions.

**Handoff OUT (Cloud → Mac, prose):** Directory `handoff_out/` containing: (a) `lancedb_gold_v2/` — the LanceDB directory with `chunks_v2` table; (b) `build_manifest.json` — build_id, l1_count, l2_count, n_vectors, article_count, model_revision, embedding_dim, indexed_through_date, corpus_hash, per_tier_distribution, elapsed_sec, gpu_type, operator; (c) `retrieval_smoke.json` — smoke test results (queries, tier_balance_ok, t5_pct_of_top12, multi_ticker_coverage_ok); (d) `index_state.db` — SQLite with populated index_state + index_manifests rows for import into dev DB.

**Files:**
- Create: `packages/data-core/scripts/cloud_handoff.py`
- Create: `packages/data-core/scripts/cloud_handoff_README.md`

---

## Task 6 — Retrieval Smoke Tests (on GPU)

### 6.1 New script: `scripts/retrieval_smoke.py`

**Purpose:** Run after embed on cloud GPU to validate the produced LanceDB index before shipping it back. Uses a set of predefined queries and assertions.

**Smoke test assertions (prose):**
1. **Chunk count parity:** Total chunks in LanceDB == unique canonical article_ids + filing_ids (no duplicate vectors).
2. **Provenance completeness:** Every chunk has non-null article_id, source_type, tickers, published_utc, source_tier.
3. **Multi-ticker coverage:** Query with `ticker='AAPL'` → results include shared articles. Same article queried with `ticker='MSFT'` appears under both tickers.
4. **Tier balance:** Neutral query ("earnings report") → top-12 results have ≤30% T5 (opinion) articles.
5. **Index_state parity:** `SELECT COUNT(*) FROM index_state WHERE indexed_build_id = ?` equals article_count from manifest.
6. **Incremental idempotent:** Run `--mode incremental` twice → second run returns `total_embedded=0`.
7. **Partial failure re-pick:** Manually delete one index_state row, re-run incremental → that article_id is re-embedded.

**Files:**
- Create: `packages/data-core/scripts/retrieval_smoke.py`
- Test: `packages/data-core/tests/test_retrieval_smoke.py` (mock LanceDB table, no GPU needed)

**Test assertions for mock smoke (prose):**
1. Chunk count parity check passes on mock data.
2. Provenance check catches missing fields.
3. Multi-ticker coverage check passes with mock multi-ticker articles.
4. Tier balance check passes/fails correctly based on mock composition.
5. Index_state parity check matches expected counts.
6. Incremental idempotent check returns zero on second run.
7. Partial failure re-pick correctly identifies the deleted row.

---

## Task 7 — Tests (Full Suite, Mac-Safe Mocks)

### 7.1 Test files and key assertions

| File | Count | Key Assertions |
|------|-------|----------------|
| `tests/storage/test_lancedb_store_v2.py` | 6 | Article-level schema, no per-ticker duplication, tickers[] serialized, provenance preserved, L2 parent correct, chunk_id format |
| `tests/test_embed_pipeline.py` | 10 | Full manifest shape, l1==article_count, index_state populated, index_manifests written, incremental zero-op, incremental delta-only, Mac ImportError, model revision recorded, metadata-only change skipped, per-tier distribution |
| `tests/test_retrieval_smoke.py` | 7 | Chunk count parity, provenance completeness, multi-ticker coverage, tier balance, index_state parity, incremental idempotent, partial failure re-pick |
| `tests/test_cli_index.py` (extend) | 4 | dry-run exits 0 on Mac, full exits ImportError on Mac, incremental exits ImportError on Mac, full with mock exits 0 |

### 7.2 Test runner

```bash
cd packages/data-core && python -m pytest \
  tests/storage/test_lancedb_store_v2.py \
  tests/test_embed_pipeline.py \
  tests/test_retrieval_smoke.py \
  tests/test_cli_index.py \
  -v
```

### 7.3 All tests use mock embedding

Deterministic 1024-dim float vectors. No GPU, no FlagEmbedding download, no LanceDB required on Mac (only for lancedb_store_v2 tests which mock LanceDB). The real embedding path is tested only on the cloud GPU.

---

## Task 8 — Validation Manifest (Post GPU Run)

| Metric | Expected | Verification |
|--------|----------|--------------|
| L1 count == article_count + filing_count | Exact parity | build_manifest.json |
| N vectors = L1 + L2 | Exact sum | build_manifest.json |
| Embedding dim | 1024 | build_manifest.json |
| Model revision recorded | Exact HF commit hash | build_manifest.json |
| index_state parity | Every article has index_state row | SQL query vs manifest |
| Per-tier distribution | T5~59%, T4~27%, T3~14% (pre-SEC) | build_manifest.json |
| T5 ≤30% of top-12 | Tier balance | retrieval_smoke.json |
| Multi-ticker coverage | Shared article under all tickers | retrieval_smoke.json |
| Incremental idempotent | Second run → 0 embedded | smoke test assertion 6 |
| Partial failure re-pick | Deleted row → re-embedded | smoke test assertion 7 |
| Frozen DB unchanged | SHA-256 unchanged | shasum |

---

## Files Summary

### Created
| File | Purpose |
|------|---------|
| `catalyst_data/embed_pipeline.py` | Full + incremental embed orchestration (cloud-only, Mac-guarded) |
| `scripts/cloud_handoff.py` | Package/unpack handoff artifacts |
| `scripts/cloud_handoff_README.md` | Cloud operator instructions |
| `scripts/retrieval_smoke.py` | Post-embed validation smoke tests (cloud) |

### Modified
| File | Change |
|------|--------|
| `catalyst_data/storage/lancedb_store.py` | Refactor to article-level schema; `_build_chunk_records_from_articles()`; new `chunks_v2` table; update `hybrid_search` to use ticker_scope post-filter |
| `catalyst_data/cli_index.py` | `rebuild-index --mode full|incremental|dry-run` with new flags |

### Test files
| File | Tests |
|------|-------|
| `tests/storage/test_lancedb_store_v2.py` | 6 |
| `tests/test_embed_pipeline.py` | 10 |
| `tests/test_retrieval_smoke.py` | 7 |
| `tests/test_cli_index.py` (extended) | 4 |

---

## Task Order

```
1. lancedb_store.py refactor (article-level schema)     — foundation, Mac-safe mock tests
2. embed_pipeline.py: build_full_index()                — depends on 1, Mac-guarded
3. embed_pipeline.py: build_incremental_index()          — depends on 2, Mac-guarded
4. CLI: rebuild-index --mode full|incremental|dry-run    — depends on 2, 3
5. cloud_handoff.py + README                            — depends on 4
6. retrieval_smoke.py                                   — depends on 1, 2
7. Full test suite (mock, Mac-safe)                     — depends on 1-4
8. Cloud GPU run — PRODUCE artifacts                    — AFTER Claude review + env ready
9. Post GPU: validation manifest                        — AFTER cloud run
```

Tasks 1-7 are Mac-safe (mock embeddings, no GPU). Tasks 8-9 are cloud-only and gated on Claude review.

---

## Cloud GPU Run Checklist (AFTER plan review)

1. [ ] Claude reviewed and approved this plan
2. [ ] Steps 1-3 merged and tests pass
3. [ ] Cloud env: Python 3.11+, FlagEmbedding, lancedb, nltk, sqlite3
4. [ ] GPU available (A10G/L4/V100 — bge-m3 fits in 4GB VRAM with fp16)
5. [ ] No API keys needed (embedding is local, model from HuggingFace)
6. [ ] `scripts/cloud_handoff.py --mode pack` on Mac → `handoff/`
7. [ ] Transfer `handoff/` to cloud
8. [ ] Cloud: `python -m catalyst_data.cli_index rebuild-index --mode full --lancedb-path /data/lancedb_gold_v2`
9. [ ] Cloud: `python scripts/retrieval_smoke.py --lancedb-path /data/lancedb_gold_v2 --db handoff/catalyst_dev_ws4b.db`
10. [ ] Cloud: `python scripts/cloud_handoff.py --mode unpack --input /data/lancedb_gold_v2` → `handoff_out/`
11. [ ] Transfer `handoff_out/` back to Mac
12. [ ] Mac: import index_state + index_manifests into dev DB
13. [ ] Mac: `python -m catalyst_data.cli_index status --freshness` → index FRESH

---

## Guardrails

1. **data-core only** — no `packages/app/`, no `packages/agents/`.
2. **Frozen DB immutable** — read-only, no writes.
3. **One vector per article_id** — 11,772 articles → 11,772 L1 vectors (not 20,867). Guard: `l1_count == article_count + filing_count`.
4. **No commit** — all changes stay in working tree for review; the human commits. The GPU run commits nothing (LanceDB is external artifact).
5. **NEVER full-embed on 8GB Mac** — `embed_pipeline.py` raises ImportError. Dry-run is Mac-safe.
6. **New LanceDB table name** — `chunks_v2`. Old `chunks` table (3 rows, 384-dim, per-ticker) preserved as legacy.
7. **index_state written ONLY after real embed** — never by dry-run or incremental indexer (Step 2). Only `build_full_index()` and `build_incremental_index()` in this module write index_state.
8. **Model revision pinned** — `--model-revision` records exact HF commit hash.
9. **Additive DDL only** — `index_state` and `index_manifests` already exist (Step 1). No schema changes.
10. **No new dependencies** — FlagEmbedding, lancedb, nltk already in `[vector]` extras.
11. **Cloud GPU run gated** — executes only after Claude review AND confirmed cloud environment.

---

## Out of Scope

- **WS4C / UI** — any admin button or web interface.
- **New connectors** — Step 3 scope.
- **Schema changes** — all tables already defined.
- **Tier/dedup changes** — metadata-only refreshes don't change content_hash → skip.
- **Real-time/streaming embedding** — batch only.
- **Multi-model experiments** — bge-m3 only.
- **LanceDB cloud/remote** — local LanceDB directory only.

---

## Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| bge-m3 revision changes upstream | Medium | `--model-revision` pins exact commit; recorded in manifest |
| LanceDB upsert slow with many small batches | Low | Daily delta <100 articles → negligible |
| L2 chunks for SEC too large | Low | 800-char threshold gates L2; per-filing L2 cap of 30 sentences already in code |
| Old hybrid_search callers break with new schema | Medium | Backward-compat wrapper; agents update in separate PR |
| Cloud GPU not available | Medium | Handoff artifacts self-contained; any GPU instance works; dry-run Mac-safe |
| Ticker array filter in LanceDB unsupported | Medium | Default to Step 1 SQLite post-filter via ticker_scope.filter_by_ticker() — already Mac-safe and tested |

---

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Tasks 1-7 are Mac-safe. Tasks 8-9 are cloud-only and MUST NOT execute until Claude reviews this plan AND confirms the cloud environment is ready. All work on dev DB only; no commit.

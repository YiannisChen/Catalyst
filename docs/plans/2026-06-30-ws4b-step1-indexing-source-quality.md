# WS4B Step 1 — Indexing & Source-Quality Foundation (Implementation Plan)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> **Status:** PLAN ONLY — for Claude review. Do not implement, edit code, run embeddings, or commit.
> **Prerequisite:** WS4B B0–B2 complete at commit `75974e2` on branch `ws4b/article-level-data`.
> **Archive:** articles = 11,772 canonical Polygon articles; article_tickers = 20,867 lossless associations; clean_assets polygon_news = 20,867 per-(article, ticker); no-merge guard active; frozen DB untouched.

**Goal:** Make the corpus tier-aware, stand up the article-level index record builder (dry-run), define the retrieval policy, and create the index_manifests/index_state schema — all Mac-safe, zero embeddings, zero LanceDB imports.

**Operating constraints:** data-core only; `data/catalyst_dev_ws4b.db` only; frozen DB `data/catalyst_eval_frozen_v2.db` READ-ONLY; Mac-only dry-run (no GPU, no model loads, no embeddings, no LanceDB imports); additive DDL only; no new dependencies beyond existing `vector` extras; no commit.

---

## Task 1 — Source Tier Classifier

### 1.1 New module: `catalyst_data/source_tier.py`

**Purpose:** Map publisher name → tier (INTEGER 1–6), populate the existing `articles.source_tier` column. The column already exists (created in B0–B2 DDL, currently NULL).

**Tier mapping:**

| Tier | Label | Publishers |
|------|-------|------------|
| T1 | Primary Source | (reserved: SEC filings — Step 3) |
| T2 | Premium Financial Press | MarketWatch |
| T3 | Wire Service | GlobeNewswire |
| T4 | Aggregator / Specialist | Benzinga, Investing.com |
| T5 | Opinion / Retail Media | Motley Fool, Zacks |
| T6 | Macro / Data | (reserved: FRED) |

**Unknown publisher fallback:** T4 with a `logger.warning(f"Unknown publisher: {name} — assigning T4")`. This is logged once per unique publisher name per run (deduplicated via set).

**Functions:**
- `tier_for_publisher(publisher_name: str) -> int` — pure lookup.
- `classify_articles(conn: sqlite3.Connection) -> int` — UPDATE all articles WHERE source_tier IS NULL, returning count of rows updated. Idempotent (only touches NULLs).
- `tier_distribution(conn: sqlite3.Connection) -> dict[int, int]` — return `{tier: count}` for validation report.

**Files:**
- Create: `packages/data-core/catalyst_data/source_tier.py`
- Test: `packages/data-core/tests/test_source_tier.py`

**Acceptance:**
- All 11,772 articles receive a non-NULL source_tier.
- Listed publishers map to declared tiers exactly.
- No tier 6 articles (FRED not yet ingested into articles).
- Unknown publisher → T4, warning logged (deduplicated).
- Idempotent: re-run produces same count (0 updates on second pass).
- Tier distribution report matches expected profile (Motley Fool ~6,946 → T5; Investing.com ~1,587 → T4; Benzinga ~1,660 → T4; GlobeNewswire ~1,581 → T3).

---

## Task 2 — Article-Level Index Record Builder (Dry-Run)

### 2.1 New module: `catalyst_data/index_builder.py`

**Purpose:** Construct L1 + L2 chunk records for future LanceDB ingestion, sourced from the `articles` table (not clean_assets). One L1 record per canonical article_id. L2 sentence chunks only when content length ≥ threshold. No embedding, no model loads, no LanceDB imports.

**Design decisions:**
- **Source:** `articles` table (11,772 rows), not `clean_assets` (20,867 per-ticker rows). This guarantees zero per-ticker duplication in the index.
- **Content for embedding:** `title` + `description` concatenated. If description is NULL/empty, title alone. This is the embedding input — the same text that will be encoded by bge-m3 in Step 4.
- **L2 threshold: ~800 characters** (configurable, `min_l2_chars`). At 400 chars ~27% of Polygon articles would produce L2; at 600 chars ~1%; at 800 chars effectively zero for Polygon (description averages 319 chars, max 758). **L2 is reserved for long-form sources (SEC 10-K, 10-Q filings in Step 3) where sentence-level retrieval matters.** The exact threshold is tunable; the default is 800, which gates L2 to filings only. The dry-run reports the exact count of articles that would produce L2 at the chosen threshold.
- **Chunk IDs:** `{article_id}::l1` for L1; `{article_id}::l2s{NNNN}` for L2 (zero-padded 4-digit, e.g., `poly:abc123::l2s0001`).
- **`parent_article_id`:** Always `article_id` — used to recombine L2 sentence chunks back to their parent article for retrieval deduplication.
- **Content hash:** `content_hash = SHA-256(title + "|" + description)`. This is the embedding-input hash. Changes to `source_tier`, `dedup_group_id`, `is_rag_eligible`, or `quality_score` are metadata-only refreshes — they do NOT change `content_hash` and do NOT trigger re-embedding. Only title/description edits trigger re-embedding.
- **Metadata per record:** `provider`, `source_type`, `publisher_name`, `publisher_logo_url`, `article_url`, `image_url`, `author`, `published_utc`, `reference_date`, `source_tier`, `dedup_group_id`, and `tickers` (JSON array — the full ticker association list joined from `article_tickers`). ONE record per article_id — never per ticker.
- **No LanceDB write, no LanceDB import:** Dry-run only. Builds the list of dicts, asserts counts, returns records. This module never imports `lancedb`. A `--mode dry-run` is the only mode on Mac.

**Functions:**
- `build_index_records(conn: sqlite3.Connection, *, min_l2_chars: int = 800) -> list[dict[str, Any]]` — queries articles JOIN article_tickers (group concat of tickers), builds L1 + L2 records. Computes `content_hash` from title+description. Returns full record list.
- `compute_content_hash(title: str, description: str) -> str` — SHA-256 of `f"{title}|{description}"`, hex first 16 chars. Pure function, reusable across modules.
- `index_summary(records: list[dict[str, Any]]) -> dict` — returns `{l1_count, l2_count, l2_eligible_count, l2_eligible_pct, would_embed_count, per_tier: {tier: {l1, l2}}}`.

**Has two important guards:**
1. `sum(len(article["tickers"]) for all records) == article_tickers row count` (lossless ticker coverage).
2. `len({r["article_id"] for r in records if r["chunk_level"] == "l1"}) == article count` (dedupe = canonical count, one vector per article_id).

**Files:**
- Create: `packages/data-core/catalyst_data/index_builder.py`
- Test: `packages/data-core/tests/test_index_builder.py`

**Acceptance:**
- L1 count == 11,772 (one per canonical article, zero per-ticker duplication).
- L2 count is reported as a measured number (expected 0 for Polygon at 800-char threshold; non-zero only when SEC filings are added in Step 3).
- Every record has non-null `tickers` (JSON array), `source_tier`, `article_id`, `parent_article_id`, `content_hash`.
- Chunk IDs follow format `{article_id}::l1` / `{article_id}::l2sNNNN`.
- `parent_article_id` == `article_id` on L1; `parent_article_id` references the owning article_id on L2.
- Tickers array on each record contains exactly the tickers from `article_tickers` for that article_id (lossless join).
- No embedding call, no model import, no LanceDB import, no GPU usage.
- `would_embed_count` = L1 + L2 (the number of vectors Step 4 would produce).

---

## Task 3 — Ticker-Scoping Post-Filter (Mac-Safe, No LanceDB)

### 3.1 New module: `catalyst_data/storage/ticker_scope.py`

**Problem:** In the article-level index (Step 4), each chunk has `tickers` (JSON array of all associated tickers). LanceDB's current `hybrid_search` uses a scalar `ticker = 'AAPL'` WHERE prefilter. The article-level schema uses `tickers[]`, not a scalar column — ticker scoping must handle array membership.

**Decision: SQLite post-filter is the DEFAULT mechanism (Mac-safe).** LanceDB list-filter verification is deferred to Step 4 (server/GPU environment where LanceDB is actually available). Step 1 does NOT import LanceDB.

**Post-filter mechanism:**
- LanceDB search runs WITHOUT ticker prefilter (broader recall, returns all matching chunks).
- Results are post-filtered: for each result, extract `article_id`, batch-query `SELECT ticker FROM article_tickers WHERE article_id IN (?,?,...) AND ticker = ?` from the dev SQLite DB.
- Only chunks whose article is associated with the requested ticker survive.
- The post-filter is wrapped so `ticker_scope.filter_by_ticker(results, ticker, db_path)` returns the filtered list — the same call signature `hybrid_search` consumers expect.

**Functions:**
- `filter_by_ticker(results: list[dict], ticker: str, db_path: str) -> list[dict]` — batch SQLite query for all article_ids in results, returns only those associated with the given ticker.
- `filter_by_tickers(results: list[dict], tickers: list[str], db_path: str) -> list[dict]` — multi-ticker variant for future use.

**Design notes:**
- Uses a single SQL query with `WHERE article_id IN (...)` and `WHERE ticker = ?` — not N individual queries.
- The `article_tickers` table is indexed on `(ticker, reference_date)` and `(article_id)` — queries are sub-millisecond.
- Tested on synthetic data (no LanceDB needed — input is a list of dicts with `article_id` keys).
- The post-filter does NOT modify `lancedb_store.py` or `hybrid_search` in any way. It is a standalone module that callers compose into their retrieval pipeline. The actual `hybrid_search` switchover happens in Step 4.

**LanceDB list-filter note (deferred to Step 4):**
If LanceDB supports `WHERE tickers LIKE '%"AAPL"%'` or an array-contains function with `prefilter=True`, the post-filter can be replaced at the LanceDB query level for lower latency. Verification of this requires an actual LanceDB environment (server/GPU), so it is deferred to the Step 4 retrieval smoke tests. The post-filter works correctly in all cases and is the production fallback.

**Files:**
- Create: `packages/data-core/catalyst_data/storage/ticker_scope.py`
- Create: `docs/spikes/ticker-scoping.md` (documents the decision, SQLite post-filter design, and LanceDB list-filter deferred note)
- Test: `packages/data-core/tests/test_ticker_scoping.py`

**Acceptance:**
- `filter_by_ticker(results, 'AAPL', db_path)` returns only chunks whose article is associated with AAPL via article_tickers.
- Batch query is one SQL call per filter operation, not N individual queries.
- No LanceDB import anywhere in the module or its tests.
- Correct on synthetic data: multi-ticker articles appear under all their tickers.
- Empty results → empty list (no crash).
- Same call signature as what `hybrid_search` consumers will use.

---

## Task 4 — Additive Schema: Index Manifests & State

### 4.1 New DDL in `sqlite.py` init_db

**Two new tables (CREATE IF NOT EXISTS):**

```sql
-- Index build manifest: one row per build
CREATE TABLE IF NOT EXISTS index_manifests (
    build_id              TEXT PRIMARY KEY,
    created_at            TEXT NOT NULL,
    model                 TEXT NOT NULL,
    model_hash            TEXT NOT NULL,
    lancedb_path          TEXT NOT NULL,
    l1_count              INTEGER NOT NULL,
    l2_count              INTEGER NOT NULL,
    article_count         INTEGER NOT NULL,
    indexed_through_date  TEXT NOT NULL,
    corpus_hash           TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending'
);

-- Per-article index state: tracks what was indexed in which build
CREATE TABLE IF NOT EXISTS index_state (
    article_id            TEXT PRIMARY KEY,
    content_hash          TEXT NOT NULL,
    source_tier           INTEGER,
    dedup_group_id        TEXT,
    indexed_build_id      TEXT,
    indexed_at            TEXT,
    FOREIGN KEY (article_id) REFERENCES articles(article_id)
);
CREATE INDEX IF NOT EXISTS idx_index_state_build ON index_state(indexed_build_id);
```

**Modify:** `packages/data-core/catalyst_data/storage/sqlite.py` — add DDL strings and wire into `init_db()` with `conn.executescript(…)`. Additive only.

**Purpose:**
- `index_manifests` tracks every LanceDB build (model, counts, date range, status) for audit and reproducibility. Populated ONLY by real embedding runs (Step 4), never by dry-run.
- `index_state` tracks per-article embedding staleness. `content_hash` = SHA-256(title + description) — computed from the embedding input only. Changes to `source_tier`, `dedup_group_id`, `is_rag_eligible`, or `quality_score` are metadata-only refreshes that do NOT change `content_hash` and do NOT trigger re-embedding. Only title/description edits trigger re-embedding. Populated ONLY by real embedding runs (Step 4).

**Files:**
- Modify: `packages/data-core/catalyst_data/storage/sqlite.py`
- Test: `packages/data-core/tests/test_index_schema.py`

**Acceptance:**
- Both tables exist after `init_db()`.
- `CREATE IF NOT EXISTS` — idempotent (re-runnable without error).
- index_state PK is article_id, matching articles table cardinality.
- No DROP or destructive ALTER.

---

## Task 5 — Retrieval Policy: Tier Boost + T5 Cap

### 5.1 New module: `catalyst_data/retrieval_policy.py`

**Purpose:** Pure functions for post-retrieval tier-aware re-ranking. Operates on already-retrieved result lists. No side effects, no DB access — just list[dict] → list[dict].

**Functions:**

- `boost_by_tier(results: list[dict], *, tiers: dict[int, float] | None = None) -> list[dict]`:
  - Default tier multipliers: T1=1.5, T2=1.3, T3=1.1, T4=1.0, T5=0.9, T6=1.0.
  - Multiplies each result's `rrf_score` by its `source_tier` multiplier.
  - Re-sorts by adjusted score descending.
  - Preserves original `rrf_score` as `_raw_rrf_score` for audit.

- `cap_t5_opinion(results: list[dict], *, max_pct: float = 0.30) -> list[dict]`:
  - After tier boost, ensures T5 (opinion) articles ≤ max_pct of final top-k.
  - **Caps, does not exclude.** If T5 exceeds 30%, trim T5 entries from the bottom (lowest scores) until ≤30%.
  - Preserves ordering within tiers.
  - If fewer than 3 total results, cap is skipped (no point capping with tiny k).

- `apply_retrieval_policy(results: list[dict], top_k: int = 12) -> list[dict]`:
  - Composition: tier_boost → cap_t5_opinion → slice top_k.
  - Returns the final list.

**Design notes:**
- This is a data-core module — agents can import and compose it. The current `packages/agents/catalyst_agents/retrieval/policy.py` will eventually be refactored to use this (out of scope for Step 1, but architected to be importable).
- The cap is a **DIVERSITY mechanism**, not a quality gate. T5 content (Motley Fool, Zacks) is useful but should not dominate results. 30% was chosen as initial value; it can be tuned later.

**Files:**
- Create: `packages/data-core/catalyst_data/retrieval_policy.py`
- Test: `packages/data-core/tests/test_retrieval_policy_data_core.py` (note: distinct from existing `test_retrieval_policy.py` in agents)

**Acceptance:**
- Tier boost increases T1/T2 scores relative to T4/T5.
- T5 cap: with 10 results all T5, only 3 survive (30% of 10 = 3).
- T5 cap: with 2 T5 out of 10, both survive (20% ≤ 30%).
- T5 cap: with 4 T5 out of 10, only 3 survive (trim lowest-scored T5).
- Ordering within tiers is preserved.
- < 3 results → cap skipped.
- All functions are pure (no DB, no I/O).

---

## Task 6 — CLI: Status & Dry-Run Rebuild

### 6.1 New module: `catalyst_data/cli_index.py`

**Purpose:** CLI entry point for index status reporting and dry-run record building. Invoked as `python -m catalyst_data.cli_index status` or `python -m catalyst_data.cli_index rebuild-index --mode dry-run`.

**Commands:**

- `status`:
  - Opens dev DB, reads `index_manifests` (latest build_id, created_at, status, l1_count, l2_count, article_count).
  - Reads `index_state` (count of indexed articles, count with NULL indexed_build_id = stale). If `index_state` is empty (no real embed has run yet), reports "No builds — run rebuild-index on cloud/GPU (Step 4)."
  - Reads `articles` (per-tier distribution via source_tier).
  - Prints a formatted summary to stdout.

- `rebuild-index --mode dry-run`:
  - Runs `classify_articles()` (populates source_tier — idempotent).
  - Runs `build_index_records()` (constructs L1+L2 records, no embedding, no LanceDB import).
  - Prints summary: L1 count, L2 count, L2 eligible count + %, would-embed total, per-tier distribution.
  - Asserts: L1 == 11,772 (deduplication guard), total ticker associations == article_tickers count (lossless guard).
  - Logs per-tier breakdown.
  - NO model imports, NO embedding calls, NO GPU, NO LanceDB imports, NO LanceDB writes.
  - Does NOT write `index_state` or `index_manifests` — those are populated only by real embedding (Step 4).

**Files:**
- Create: `packages/data-core/catalyst_data/cli_index.py`
- Test: `packages/data-core/tests/test_cli_index.py`

**Acceptance:**
- `status` prints index freshness summary without errors; reports "No builds" if no real embed has run.
- `rebuild-index --mode dry-run` completes on Mac in < 30 seconds.
- Dry-run asserts pass (L1 count, ticker coverage).
- No embedding/model imports triggered by CLI.
- No LanceDB imports anywhere in CLI or its dependencies.
- Exit code 0 on success, non-zero on assertion failure.

---

## Task 7 — Tests (Scoped Suite)

### 7.1 New test files

| File | Tests | Key assertions |
|------|-------|----------------|
| `tests/test_source_tier.py` | 6–8 | Tier mapping exact, unknown→T4+warning, idempotent, distribution, all non-NULL after classify |
| `tests/test_index_builder.py` | 6–8 | L1 count == 11,772, L2 gating (800-char threshold → zero for Polygon), content_hash computed from title+description, metadata completeness, tickers[] lossless, chunk ID format, no model import, no LanceDB import |
| `tests/test_ticker_scoping.py` | 5–7 | SQLite post-filter correctness, batch query efficiency (one query not N), multi-ticker article appears under all its tickers, empty results → empty list, no LanceDB import anywhere |
| `tests/test_index_schema.py` | 4–5 | Tables exist after init_db, idempotent CREATE, index_state PK, FK to articles |
| `tests/test_retrieval_policy_data_core.py` | 6–8 | Tier boost ordering, T5 cap ≤30%, T5 cap preserves ordering, <3 results skip, pure function |
| `tests/test_cli_index.py` | 4–5 | status exits 0, rebuild-index --mode dry-run exits 0, dry-run assertions, no embedding imports, no LanceDB imports |

### 7.2 Test runner command

```bash
cd packages/data-core && python -m pytest \
  tests/test_source_tier.py \
  tests/test_index_builder.py \
  tests/test_ticker_scoping.py \
  tests/test_index_schema.py \
  tests/test_retrieval_policy_data_core.py \
  tests/test_cli_index.py \
  tests/test_articles_schema.py \
  tests/test_rederive.py \
  tests/test_regenerate_clean.py \
  tests/test_no_merge_regression.py \
  tests/test_pipeline.py \
  tests/test_orchestrator.py \
  tests/test_storage.py \
  tests/test_align.py \
  tests/test_dedup.py \
  -v
```

**Explicitly deferred to server/Step 4:** `test_lancedb.py`, `test_build_embeddings_gpu.py`, `test_build_index_from_artifacts.py` (require LanceDB/FlagEmbedding/GPU context). `test_lancedb_store.py` is NOT modified in Step 1.

### 7.3 No new test dependencies

Uses existing `pytest`, `sqlite3`, and standard library only. No new pip packages. No LanceDB imports.

---

## Validation Report (to be produced after implementation)

| Metric | Expected Value | Verification Method |
|--------|---------------|---------------------|
| L1 count | == 11,772 | `SELECT COUNT(*) FROM articles` vs count of `chunk_level='l1'` records |
| L2 count + % | measured (expected 0 for Polygon at 800-char threshold) | `count(chunk_level='l2') / (L1+L2)` |
| Would-embed total | L1 + L2 | Sum assertion in dry-run |
| Per-tier distribution | T5 ~59%, T4 ~27%, T3 ~14%, T2 <1% | `SELECT source_tier, COUNT(*) FROM articles GROUP BY 1` |
| Tickers lossless | sum(len(tickers)) == 20,867 | Assert in index_builder |
| Ticker-scoping post-filter | Correct on synthetic data | test_ticker_scoping.py |
| Content hash computed | SHA-256(title + description) for every record | test_index_builder.py |
| T5 cap ≤30% | Unit test | test_retrieval_policy_data_core.py |
| Frozen DB SHA-256 | `0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf` | `shasum -a 256 data/catalyst_eval_frozen_v2.db` |
| Additive DDL only | No DROP/destructive ALTER | Diff of sqlite.py before/after |
| No embedding on Mac | grep -r "FlagEmbedding\|BGEM3FlagModel\|model.encode\|import lancedb" new modules → empty | Code audit |

---

## Files Summary

### Created (7 new modules)
| File | Purpose |
|------|---------|
| `catalyst_data/source_tier.py` | Publisher→tier classifier, populate articles.source_tier |
| `catalyst_data/index_builder.py` | L1+L2 record builder (dry-run, no embedding, no LanceDB import) |
| `catalyst_data/storage/ticker_scope.py` | SQLite post-filter for ticker-scoping (Mac-safe, no LanceDB) |
| `catalyst_data/retrieval_policy.py` | Tier boost + T5 opinion cap (pure functions) |
| `catalyst_data/cli_index.py` | CLI: `status` + `rebuild-index --mode dry-run` |
| `docs/spikes/ticker-scoping.md` | Ticker-scoping decision: SQLite post-filter default; LanceDB list-filter deferred to Step 4 |

### Modified (1 file)
| File | Change |
|------|--------|
| `catalyst_data/storage/sqlite.py` | Add `index_manifests` + `index_state` DDL to `init_db()` |

### NOT modified (preserved as-is)
| File | Reason |
|------|--------|
| `catalyst_data/storage/lancedb_store.py` | Deferred to Step 4 — hybrid_search, build_index, and LanceDB schema changes happen there |
| `packages/agents/` | Out of scope for data-core Step 1 |

### Test files (6 new)
- `tests/test_source_tier.py`
- `tests/test_index_builder.py`
- `tests/test_ticker_scoping.py`
- `tests/test_index_schema.py`
- `tests/test_retrieval_policy_data_core.py`
- `tests/test_cli_index.py`

---

## Task Order (Sequential)

```
1. source_tier.py + test                (no dependencies)
2. index_builder.py + test              (depends on articles table existing)
3. ticker_scope.py + test               (independent — synthetic data only, no LanceDB)
4. sqlite.py DDL + test                 (independent — additive schema)
5. retrieval_policy.py + test           (pure functions, independent)
6. cli_index.py + test                  (depends on 1, 2, 4)
7. Run scoped test suite                (all of above)
8. Validation report                    (counts, SHA-256, tier distribution)
```

Tasks 3, 4, 5 can run in parallel (no shared state). Task 6 integrates 1+2+4. Task 7 gates on all.

---

## Guardrails

1. **data-core only** — no `packages/app/`, no `packages/agents/`.
2. **Dev DB only** — `data/catalyst_dev_ws4b.db`. Frozen DB read-only.
3. **No embeddings on Mac** — dry-run only. No `FlagEmbedding`, no `BGEM3FlagModel`, no `model.encode()`, no GPU, no LanceDB imports anywhere in new or modified modules.
4. **Additive DDL only** — `CREATE TABLE IF NOT EXISTS`; no `DROP`, no destructive `ALTER`.
5. **No new dependencies** — all new imports from stdlib or existing `catalyst_data` modules. nltk already in `[vector]` extras (for PunktTokenizer in index_builder).
6. **No commit** — all changes stay in working tree for review; the human commits.
7. **No network calls** — all data from local dev DB; tests use synthetic data.
8. **8GB Mac safe** — records are built in memory (11,772 L1 + 0 L2 at 800-char threshold = ~12k dicts, < 50 MB). No large model loads.
9. **Do NOT modify lancedb_store.py or hybrid_search** — retrieval switchover deferred to Step 4.

---

## Out of Scope (Steps 2–4, NOT here)

- **Step 2:** Update/backfill pipeline, freshness reporting, checkpoint/resume.
- **Step 3:** New connectors (SEC, Finnhub, FMP news), cross-source dedup.
- **Step 4:** Real GPU embedding, LanceDB rebuild, incremental embedding, retrieval switchover.
- **Any change to packages/agents or packages/app.**
- **Any LanceDB write or import.**
- **Search API integration.**

---

## Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| L2 threshold needs tuning for SEC filings | Low | Parameterized (`min_l2_chars`); adjustable without schema change. Dry-run reports exact count. |
| Tier mapping incomplete (new publishers from Step 3) | Low | Unknown → T4 with logging; tier map is data, not code — easy to update when new sources added |
| Dry-run asserts fail (count mismatch vs 11,772/20,867) | Low | Guards validate against known counts; if fail → investigate before proceeding |
| SQLite post-filter too slow at scale | Low | Batch query with `WHERE article_id IN (...)` is O(results); 12 results × 1 query = sub-ms. Benchmark in test. |

---

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task in section order. All work on dev DB only; no commit.

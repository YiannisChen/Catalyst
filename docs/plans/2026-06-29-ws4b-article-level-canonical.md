# WS4B Article-Level Evidence — Implementation Plan (B0 + B1 + B2)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Unbundle Polygon news digests into canonical per-article rows, recover all dropped provenance fields, and refactor clean_assets to 1-row-per-article with provenance — all offline, additive, and non-destructive to the frozen eval DB.

**Architecture:** Three ordered, non-overlapping sections. B0 audits raw_assets encoding and designs the `articles` table schema (DDL-only, additive). B1 reads existing polygon_news raw_assets from a dev/copy DB, parses each zlib-compressed JSON payload, and emits one `articles` row per Polygon `results[]` entry — idempotent via upsert. B2 refactors `clean_assets` for polygon_news: adds a nullable `raw_asset_id` provenance column, creates a per-article Markdown transform (no merge), and regenerates all polygon_news clean_assets from the articles table (~20,867 rows replacing 3,125 digest rows). Non-news rows (fundamentals, macro, ohlcv) are completely untouched.

**Tech Stack:** Python 3.11+, SQLite (WAL, zlib), existing `catalyst_data` pipeline primitives (align.py, sqlite.py, dedup/hard.py).

---

## Section B0 — Audit & Schema Design

### Files Touched
- **Create:** `packages/data-core/catalyst_data/articles.py` (new module: schema DDL, upsert, query helpers)
- **Modify:** `packages/data-core/catalyst_data/storage/sqlite.py` (wire articles table into init_db alongside existing tables — additive)
- **Test:** `packages/data-core/tests/test_articles_schema.py` (new)

### Verified Facts (from frozen DB audit)

1. **raw_assets.content_raw encoding**: zlib-compressed JSON bytes. Decompressed structure for polygon_news is `{"news": {"results": [...]}}` where each element has:
   ```
   id, publisher{name, logo_url, favicon_url, homepage_url}, title, author,
   published_utc, article_url, image_url, description, keywords, insights, tickers
   ```
   Confirmed by scanning all 3,125 polygon_news raw_assets — all decompress successfully, all have this exact structure.

2. **Article count**: 3,125 polygon_news raw_assets → 20,867 articles (min 1, max 37, mean 6.7 per row).

3. **Field fill rates** (query: `SELECT COUNT(*) WHERE col IS NOT NULL AND col != ''` per field; for keywords/insights: `WHERE json_array_length(col) > 0`):

   | Field | Non-null | Non-empty | Rate |
   |---|---|---|---|
   | article_url | 20,867 | 20,867 | 100.0% |
   | image_url | 20,867 | 20,867 | 100.0% |
   | author | 20,867 | 20,867 | 100.0% |
   | publisher_logo_url | 20,867 | 20,867 | 100.0% |
   | published_utc | 20,867 | 20,867 | 100.0% |
   | keywords | 20,867 | 20,867 | 100.0% |
   | insights | 20,867 | 20,867 | 100.0% |
   | title | 20,867 | 20,867 | 100.0% |
   | description | 20,867 | 20,867 | 100.0% |

   Polygon consistently provides all fields in every response. The existing `_transform_news` merge drops image_url, author, publisher.logo_url, favicon_url, keywords, insights, and multi-ticker context. **All are fully recoverable from raw_assets.**

4. **Publisher distribution:** Motley Fool 59.0% (12,317), Investing.com 16.3% (3,403), Benzinga 13.0% (2,713), GlobeNewswire 11.6% (2,415), MarketWatch 0.1% (17), Zacks <0.1% (2).

5. **article_id design**: Polygon `results[].id` is already a provider-native SHA-256 hash (e.g., `c2e71b7c84aea80aefcd6ee3bb62fba2daa62caa7cf98ea6b4b57d91f8398ea8`). Namespace as `poly:{id}` to leave room for other providers (e.g., `benzinga:{id}`). For future filings/events, the same table can extend via `provider` + `source_type` columns.

### Schema DDL

```sql
CREATE TABLE IF NOT EXISTS articles (
    article_id        TEXT PRIMARY KEY,              -- "poly:{results[].id}"
    raw_asset_id      TEXT NOT NULL,                 -- FK → raw_assets.asset_id
    provider          TEXT NOT NULL DEFAULT 'polygon',
    source_type       TEXT NOT NULL DEFAULT 'polygon_news',
    ticker            TEXT NOT NULL,
    reference_date    TEXT NOT NULL,                 -- aligned via map_to_trade_date
    published_utc     TEXT NOT NULL,                 -- raw published_utc from payload
    title             TEXT NOT NULL,
    description       TEXT,                          -- article body / description text
    article_url       TEXT,
    image_url         TEXT,
    author            TEXT,
    publisher_name    TEXT,
    publisher_homepage_url TEXT,
    publisher_logo_url    TEXT,
    publisher_favicon_url TEXT,
    keywords_json     TEXT,                          -- JSON array
    insights_json     TEXT,                          -- JSON array of {ticker, sentiment, reasoning}
    tickers_json      TEXT,                          -- JSON array of ticker strings
    source_tier       INTEGER,                       -- nullable now, populated in B4
    dedup_group_id    TEXT,                          -- nullable now, populated later
    is_canonical      INTEGER DEFAULT 1,
    is_rag_eligible   INTEGER DEFAULT 1,
    quality_score     REAL DEFAULT 1.0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (raw_asset_id) REFERENCES raw_assets(asset_id)
);

CREATE INDEX IF NOT EXISTS idx_articles_raw_asset ON articles(raw_asset_id);
CREATE INDEX IF NOT EXISTS idx_articles_ticker_date ON articles(ticker, reference_date);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_utc);
CREATE INDEX IF NOT EXISTS idx_articles_ticker_published ON articles(ticker, published_utc);
CREATE INDEX IF NOT EXISTS idx_articles_provider ON articles(provider);
```

### Task Order

#### Task B0.1: Write schema migration test

Write `tests/test_articles_schema.py` with these test cases:

1. **test_articles_table_created**: `init_db` or explicit DDL creates the table + indexes.
2. **test_articles_table_additive**: Running DDL twice does not error (IF NOT EXISTS). Existing raw_assets/clean_assets tables are untouched.
3. **test_articles_fk_enforcement**: Insert with invalid raw_asset_id fails if FK enabled.
4. **test_articles_upsert**: INSERT OR REPLACE by article_id works, idempotent.
5. **test_articles_schema_columns**: All named columns present with correct types.

#### Task B0.2: Create articles.py module

Create `packages/data-core/catalyst_data/articles.py` containing:
- `_ARTICLES_DDL`: The CREATE TABLE + CREATE INDEX statements
- `ensure_articles_table(conn)`: Execute DDL, commit
- `compute_article_id(provider, native_id) -> str`: Namespace as `"{provider}:{native_id}"`
- `upsert_article(conn, *, article: dict)`: INSERT OR REPLACE a single row
- `get_articles_by_raw_asset(conn, raw_asset_id) -> list[dict]`: Fetch all articles for a raw_asset

#### Task B0.3: Wire articles DDL into init_db (additive)

In `packages/data-core/catalyst_data/storage/sqlite.py`, add a call to `ensure_articles_table` inside `init_db` after the existing `conn.executescript(_DDL)` line. This ensures every new database gets the articles table transparently.

#### Task B0.4: Run scoped tests

```bash
cd packages/data-core && python -m pytest tests/test_articles_schema.py tests/test_storage.py tests/test_pipeline.py tests/test_orchestrator.py tests/test_align.py tests/test_dedup.py -v
```

Deferred to server: `test_lancedb.py`, `test_build_embeddings_gpu.py`, `test_build_index_from_artifacts.py` (require lancedb/FlagEmbedding/nltk; not part of B0–B2 scope).

#### Acceptance Criteria (B0)

- [ ] `articles` table exists with all specified columns and indexes
- [ ] FK constraint links `articles.raw_asset_id` → `raw_assets.asset_id`
- [ ] Running DDL on an existing DB is idempotent (no errors, no data loss)
- [ ] No existing table (raw_assets, clean_assets, ohlcv, etc.) is altered
- [ ] Scoped test suite passes: test_articles_schema + test_storage + test_pipeline + test_orchestrator + test_align + test_dedup
- [ ] `init_db` creates articles table in new databases

---

## Section B1 — Offline Polygon Re-derive → Articles

### Files Touched
- **Create:** `packages/data-core/catalyst_data/rederive.py` (new module: re-derive logic)
- **Test:** `packages/data-core/tests/test_rederive.py` (new)

### Dev Database Preparation

Before running B1, create the working database:

```bash
cp data/catalyst_eval_frozen_v2.db data/catalyst_dev_ws4b.db
```

This one-time copy ensures:
- All 3,125 polygon_news raw_assets are present (zlib-compressed, immutable)
- The `ohlcv` table contains the trading calendar needed by `map_to_trade_date`
- The frozen eval DB (`catalyst_eval_frozen_v2.db`) remains untouched

All B1 operations target `data/catalyst_dev_ws4b.db`. Never read or write the frozen DB.

### How It Works

The re-derive script:
1. Connects to `data/catalyst_dev_ws4b.db`.
2. Fetches all `polygon_news` rows from `raw_assets`.
3. For each row: zlib-decompresses `content_raw`, parses the JSON, extracts `news.results[]`.
4. For each result: maps all 16 provenance fields, computes `reference_date` via `align.map_to_trade_date`, namespaces `article_id` as `poly:{id}`.
5. Upserts each article via `upsert_article` (INSERT OR REPLACE — idempotent).
6. Batches commits (every 100 rows) for 8GB-Mac safety.

### Reference Date Alignment

Reuse `catalyst_data.pipeline.align.map_to_trade_date` — do not reinvent. This function:
- Converts `published_utc` to US/Eastern
- Maps after-16:00 ET articles to the next calendar day
- Finds the first trading day on or after that candidate via binary search over the trading calendar

The trading calendar is obtained from `ohlcv` table dates. **If `ohlcv` is empty or missing, the re-derive must raise a hard error** — no silent fallback to `published_utc` date, no misalignment. The dev DB copy from the frozen DB guarantees the calendar is present.

### Task Order

#### Task B1.1: Write re-derive tests

Write `tests/test_rederive.py`:

1. **test_rederive_idempotent**: Run re-derive twice on the same raw_asset → same article count, no duplicates.
2. **test_rederive_n_in_n_out**: For a raw_asset with N articles in payload, N rows appear in articles table.
3. **test_rederive_provenance_completeness**: Every article row has a non-null raw_asset_id that joins back to raw_assets.
4. **test_rederive_field_mapping**: A synthetic payload with all fields → all fields mapped correctly (author, image_url, keywords, insights, publisher fields).
5. **test_rederive_reference_date_alignment**: Known published_utc → expected reference_date (test boundary cases: before/after 16:00 ET).
6. **test_rederive_payload_decoding**: Handles zlib-compressed BLOBs, handles edge cases (empty results, malformed JSON, missing fields).
7. **test_rederive_batch_commit**: Processing many raw_assets does not OOM (batched).
8. **test_rederive_missing_calendar_is_error**: Raises hard error when ohlcv table is empty/missing (no silent fallback).

#### Task B1.2: Create rederive.py module

Create `packages/data-core/catalyst_data/rederive.py` containing:
- `_load_trading_calendar(conn) -> list[str]`: Extract sorted distinct dates from ohlcv table. Raises `RuntimeError` if empty.
- `_parse_article(result, raw_asset_id, ticker, trading_days) -> dict | None`: Map one Polygon `results[]` entry to an articles row dict, with reference_date alignment.
- `rederive_polygon_news(db_path) -> dict`: Main entry point — iterates raw_assets, decompresses, parses, upserts in batches.

Key properties:
- `BATCH_COMMIT_SIZE = 100` for memory safety
- Uses `INSERT OR REPLACE` for idempotency
- Logs warnings for malformed payloads but continues
- Returns `{raw_rows_processed, articles_upserted, articles_skipped}`

#### Task B1.3: Run scoped tests

```bash
cd packages/data-core && python -m pytest tests/test_rederive.py tests/test_articles_schema.py tests/test_storage.py tests/test_align.py tests/test_dedup.py -v
```

#### Task B1.4: Run re-derive on dev DB

```bash
cd packages/data-core && python -c "
from catalyst_data.rederive import rederive_polygon_news
result = rederive_polygon_news('../data/catalyst_dev_ws4b.db')
print(result)
"
```

Expected: `articles_upserted == 20867`.

#### Acceptance Criteria (B1)

- [ ] All 20,867 articles parsed without error
- [ ] Every article has a valid `raw_asset_id` FK back to `raw_assets`
- [ ] Re-running is idempotent — same article count, no duplicates
- [ ] Actual per-field fill rates match audit table above (run the fill-rate query and report)
- [ ] Publisher distribution matches audit (run the GROUP BY query and report)
- [ ] Missing ohlcv calendar raises hard error (not silent fallback)
- [ ] No network calls, no API keys used
- [ ] Batching keeps memory under 500MB peak

---

## Section B2 — clean_assets Compatibility Refactor (No-Merge, With Provenance)

### Files Touched
- **Modify:** `packages/data-core/catalyst_data/storage/sqlite.py` (add `raw_asset_id` column to clean_assets DDL + index)
- **Create:** `packages/data-core/catalyst_data/pipeline/transform_v2.py` (new module: per-article transform, no merge)
- **Modify:** `packages/data-core/catalyst_data/orchestrator.py` (change polygon_news path only: per-article clean_assets with raw_asset_id)
- **Create:** `packages/data-core/catalyst_data/regenerate_clean.py` (new module: historical clean_assets regeneration from articles)
- **Test:** `packages/data-core/tests/test_no_merge_regression.py` (new)
- **Test:** `packages/data-core/tests/test_regenerate_clean.py` (new)
- **Modify:** `packages/data-core/tests/test_pipeline.py` (update news merge tests)
- **Modify:** `packages/data-core/tests/test_orchestrator.py` (update for per-article path)

### clean_assets Schema Change (Additive, Non-Destructive)

The existing clean_assets table has this DDL:

```sql
CREATE TABLE IF NOT EXISTS clean_assets (
    asset_id        TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    reference_date  TEXT NOT NULL,
    cleaned_at      TEXT NOT NULL,
    content_md      TEXT NOT NULL,
    title_hash      TEXT,
    is_duplicate    INTEGER DEFAULT 0,
    FOREIGN KEY (asset_id) REFERENCES raw_assets(asset_id)
);
```

**Additive change only** — do not DROP or ALTER existing columns:

```sql
-- Add provenance column (additive ALTER TABLE)
ALTER TABLE clean_assets ADD COLUMN raw_asset_id TEXT;

-- Index for provenance lookups
CREATE INDEX IF NOT EXISTS idx_clean_raw_asset ON clean_assets(raw_asset_id);
```

**Legacy FK handling**: The existing `FOREIGN KEY (asset_id) REFERENCES raw_assets(asset_id)` constraint was designed when `asset_id` was a SHA-256 hash matching `raw_assets.asset_id`. After B2, polygon_news clean_assets will have `asset_id = poly:{article_id}` which does NOT match any raw_assets row. This is intentional and non-destructive:

- The legacy FK remains in the schema (not dropped — additive only).
- `PRAGMA foreign_keys=OFF` is used during the clean_assets regeneration to allow inserts with non-matching asset_id values.
- **The real provenance link is `clean_assets.raw_asset_id → raw_assets.asset_id`**, which is always populated for regenerated rows.
- Non-news clean_assets rows (fundamentals, macro) are untouched and retain their original `asset_id` values which still match raw_assets — their FK integrity is preserved.
- This is documented as: *"Legacy FK on clean_assets.asset_id is unenforced for polygon_news rows post-B2. Use clean_assets.raw_asset_id for provenance."*

### What Changes (polygon_news path only)

Currently `process_request` in orchestrator.py:
1. Fetches polygon_news → raw JSON
2. Cleans → list of article dicts (dedup'd)
3. Transforms → single merged Markdown string via `_transform_news`
4. Upserts ONE clean_asset row with the merged digest, `asset_id` = SHA-256 hash

**After B2**, for polygon_news **only**:
1. Fetches → raw JSON (unchanged)
2. Cleans → list of article dicts (unchanged)
3. Transform each article individually via `run_transform_v2` → one StageResult per article
4. Upserts ONE clean_asset row **per article**:
   - `asset_id` = `poly:{article_id}`
   - `raw_asset_id` = the raw_assets.asset_id this article came from
   - `content_md` = single-article Markdown (no merge)

All non-news source types (fmp_fundamentals, fred_macro, polygon_ohlcv) are **completely untouched** — their fetch, clean, transform, and storage paths are identical to current code. Concurrency (asyncio.gather + thread pool) is preserved.

### Compatibility Shape

Each article produces a clean_asset row with:
- `asset_id`: `poly:{article_id}` (consistent with articles table)
- `raw_asset_id`: the raw_assets.asset_id (provenance link)
- `ticker`: from raw_assets ticker
- `source_type`: `polygon_news`
- `reference_date`: from alignment
- `content_md`: single-article Markdown (no merge):
  ```
  ## {ticker}: {title}
  *Source: {publisher_name} | {published_utc} | Category: news*

  {description}

  ## References
  [1] {article_url}: {title}
  ```
- `title_hash`: SHA-256 of normalized title (reuse dedup.hard logic)
- `is_duplicate`: 0 (dedup handled upstream at clean stage)
- `cleaned_at`: now()

### Stale LanceDB Note

B2 invalidates any existing LanceDB index built from the dev DB (`data/lancedb_gold/`). The LanceDB `chunks` table keys on `clean_assets.asset_id` — after B2, polygon_news asset_ids change from SHA-256 hash format to `poly:{article_id}` format, and row count changes from 3,125 to ~20,867. LanceDB rebuild is scoped to B3/B8 — **do not touch LanceDB in this plan**.

The frozen eval DB (`data/catalyst_eval_frozen_v2.db`) and its associated LanceDB embeddings (`data/embeddings/`) remain the untouched eval baseline. The frozen DB is never read or written by B0–B2 code paths.

### The No-Merge Regression Guard

The plan must guarantee the bundle-digest form can never be produced again for polygon_news:

1. **Code path**: `transform_v2.py` has no `_transform_news` — it only has `_transform_article` which takes a single article dict, never a list. If a list is ever passed, it raises `TypeError`.
2. **Module-level guard comment**: Top of `transform_v2.py` states: *"This module MUST NOT merge multiple articles into a single Markdown digest. The bundle-digest form is permanently retired."*
3. **Test guard**: `test_no_merge_regression.py` tests that passing a list raises TypeError and that N articles produce N clean_asset rows.

### Task Order

#### Task B2.1: Add raw_asset_id column to clean_assets DDL

In `packages/data-core/catalyst_data/storage/sqlite.py`:
- After the existing clean_assets CREATE TABLE, add `ALTER TABLE clean_assets ADD COLUMN raw_asset_id TEXT;` wrapped in a try/except (safe if column already exists)
- Add `CREATE INDEX IF NOT EXISTS idx_clean_raw_asset ON clean_assets(raw_asset_id);`
- Update `upsert_clean_asset` to accept and store `raw_asset_id` (nullable, default None for backward compat)
- Update `get_clean_asset` to return `raw_asset_id` in the result dict

#### Task B2.2: Write no-merge regression tests

Write `tests/test_no_merge_regression.py`:

1. **test_n_articles_in_n_rows_out**: Given N article dicts, `run_transform_v2` produces N `StageResult` entries.
2. **test_no_digest_produced**: Given 2+ articles via `run_transform_v2`, verify no single `content_md` contains multiple `## {ticker}:` headers.
3. **test_single_article_shape**: Output for one article matches the expected Markdown format exactly.
4. **test_list_input_rejected**: Passing a list to `_transform_article` raises `TypeError` with message containing "merge guard".
5. **test_clean_assets_schema_preserved**: Output rows use columns: asset_id, ticker, source_type, reference_date, cleaned_at, content_md, title_hash, is_duplicate, raw_asset_id.
6. **test_bundle_digest_guard_comment**: Top of `transform_v2.py` contains "MUST NOT merge" and "permanently retired".
7. **test_article_to_clean_asset_join**: After regeneration, every polygon_news article_id can be joined to exactly one clean_assets row via `clean_assets.asset_id = articles.article_id`.
8. **test_raw_asset_id_populated**: Every regenerated clean_asset has non-null `raw_asset_id`.

#### Task B2.3: Create transform_v2.py

Create `packages/data-core/catalyst_data/pipeline/transform_v2.py` containing:
- `_compute_title_hash(title) -> str`: Stable SHA-256 of normalized title (same algorithm as existing `dedup.hard.compute_dedup_fingerprint` but title-only)
- `_transform_article(article, ticker) -> dict`: Convert one article dict to `{asset_id, content_md, title_hash}`. Raises `TypeError` if article is a list (merge guard).
- `run_transform_v2(articles, source_type, ticker) -> list[StageResult]`: Iterate articles, return one `StageResult` each.

#### Task B2.4: Create regenerate_clean.py

Create `packages/data-core/catalyst_data/regenerate_clean.py`:

```python
"""Historical regeneration: rebuild clean_assets for polygon_news from articles table.

This is required so B3 has article-level clean_assets. Operates on polygon_news
scope only; non-news rows are untouched. Idempotent — safe to re-run.
"""

def regenerate_polygon_clean_assets(db_path: str | Path) -> dict[str, int]:
    """Rebuild clean_assets for polygon_news from the articles table.

    1. DELETE all clean_assets rows where source_type = 'polygon_news'
    2. For each article in articles WHERE provider = 'polygon':
       - Build single-article Markdown via _transform_article
       - INSERT one clean_asset row with asset_id = article_id, raw_asset_id populated
    3. Non-news rows (fundamentals, macro, ohlcv) are never touched

    Uses PRAGMA foreign_keys=OFF temporarily so the legacy FK on asset_id
    (which expects raw_assets-compatible ids) doesn't block poly:{id} values.

    Returns {deleted_digest_rows, inserted_article_rows}.
    """
```

Key properties:
- `PRAGMA foreign_keys=OFF` during the transaction so `poly:{id}` asset_ids don't violate the legacy FK
- BATCH_COMMIT_SIZE = 100 for memory safety
- `INSERT OR REPLACE` for idempotency
- Non-news rows filtered by `WHERE source_type = 'polygon_news'` only
- Returns counts for verification

#### Task B2.5: Write regeneration tests

Write `tests/test_regenerate_clean.py`:

1. **test_regenerate_idempotent**: Run twice → same clean_assets count for polygon_news.
2. **test_regenerate_n_in_n_out**: 20,867 articles → 20,867 clean_assets rows.
3. **test_non_news_untouched**: After regeneration, non-news clean_assets count unchanged.
4. **test_provenance_intact**: Every regenerated clean_asset has non-null raw_asset_id joining to raw_assets.
5. **test_legacy_digests_gone**: No clean_assets row with source_type='polygon_news' has content_md containing multiple articles.

#### Task B2.6: Modify orchestrator (polygon_news path only)

In `packages/data-core/catalyst_data/orchestrator.py`, locate `process_request`. For the polygon_news path **only**:
- Import `run_transform_v2` from `catalyst_data.pipeline.transform_v2`
- Replace the single `run_transform` + single `upsert_clean_asset` with a loop over `run_transform_v2` results, upserting one clean_asset per article with `raw_asset_id` set to the raw_assets.asset_id
- Use `row["asset_id"]` (i.e., `poly:{article_id}`) as the clean_asset primary key
- All non-news source type paths remain completely unchanged
- Concurrency model (asyncio.gather + thread pool) is preserved

#### Task B2.7: Update existing tests

In `tests/test_pipeline.py`: if any test exercises `_transform_news` with multi-article lists, update to use `run_transform_v2` for the news path. Preserve tests for non-news transform paths.

In `tests/test_orchestrator.py`: update any test that asserts on polygon_news clean_asset count or asset_id format to reflect the new per-article model.

#### Task B2.8: Run scoped test suite

```bash
cd packages/data-core && python -m pytest \
  tests/test_no_merge_regression.py \
  tests/test_regenerate_clean.py \
  tests/test_pipeline.py \
  tests/test_orchestrator.py \
  tests/test_articles_schema.py \
  tests/test_rederive.py \
  tests/test_storage.py \
  tests/test_align.py \
  tests/test_dedup.py \
  -v
```

Deferred to server: `test_lancedb.py`, `test_build_embeddings_gpu.py`, `test_build_index_from_artifacts.py`.

#### Task B2.9: Run regeneration on dev DB

```bash
cd packages/data-core && python -c "
from catalyst_data.regenerate_clean import regenerate_polygon_clean_assets
result = regenerate_polygon_clean_assets('../data/catalyst_dev_ws4b.db')
print(result)
# Expected: {deleted_digest_rows: 3125, inserted_article_rows: 20867}
"
```

#### Acceptance Criteria (B2)

- [ ] clean_assets has new `raw_asset_id TEXT` column + `idx_clean_raw_asset` index
- [ ] Legacy FK on asset_id preserved (not dropped); documented as unenforced for polygon_news
- [ ] Non-news clean_assets rows completely untouched (count unchanged before/after)
- [ ] N articles in → N clean_assets out for polygon_news (20,867 rows)
- [ ] No digest can be produced (TypeError guard + test_gone_digests)
- [ ] Every regenerated clean_asset has non-null raw_asset_id
- [ ] Polygon provenance survives: `articles.raw_asset_id → raw_assets.asset_id` AND `clean_assets.raw_asset_id → raw_assets.asset_id`
- [ ] Scoped test suite passes (9 test files)
- [ ] Frozen DB + frozen LanceDB + frozen embeddings untouched
- [ ] Stale LanceDB note documented: dev LanceDB invalid, rebuild deferred to B3/B8

---

## Validation Report (Post-Execution)

After B0+B1+B2 are implemented, the following validation must pass:

| Check | Method | Expected |
|---|---|---|
| Article count correctness | `SELECT COUNT(*) FROM articles WHERE provider='polygon'` | 20,867 |
| N-in-N-out invariant | Compare article count to sum of `len(news.results[])` across all raw_assets | Equal |
| No-merge regression | `test_no_merge_regression.py` | 8/8 pass |
| Regeneration correctness | `test_regenerate_clean.py` | 5/5 pass |
| Per-field fill rates | Run fill-rate query from B0 audit | Report actual rates (expected 100% based on frozen DB audit) |
| Publisher distribution | `SELECT publisher_name, COUNT(*) FROM articles GROUP BY publisher_name ORDER BY COUNT(*) DESC` | Motley Fool ~59%, Investing.com ~16%, Benzinga ~13%, GlobeNewswire ~12% |
| Frozen DB immutability | SHA-256 of frozen DB before/after | Identical |
| Provenance completeness (articles) | `SELECT COUNT(*) FROM articles WHERE raw_asset_id NOT IN (SELECT asset_id FROM raw_assets)` | 0 |
| Provenance completeness (clean_assets) | `SELECT COUNT(*) FROM clean_assets WHERE source_type='polygon_news' AND raw_asset_id IS NULL` | 0 |
| Non-news untouched | `SELECT COUNT(*) FROM clean_assets WHERE source_type != 'polygon_news'` | Same before/after |
| Re-derive idempotency | Run re-derive twice, compare counts | Same |
| Payload decoding | All 3,125 raw_assets decompress + parse without error | 0 errors |
| Scoped tests | 9 test files (no lancedb/embeddings) | All pass |

---

## Guardrails

- **data-core only**: All changes in `packages/data-core/`. No modifications to `packages/app/` or `packages/agents/`.
- **No LanceDB writes**: B3/B8 will handle LanceDB rebuild. This plan only touches SQLite. Existing dev LanceDB is documented as stale.
- **No embeddings**: No model loading, no vector operations.
- **No network/API calls**: Purely offline — reads existing raw_assets data.
- **No destructive migration**: All DDL is `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD COLUMN` (wrapped in try/except), `CREATE INDEX IF NOT EXISTS`. No DROP, no ALTER of existing columns.
- **Frozen DB immutable**: `data/catalyst_eval_frozen_v2.db` is never read or written. Work on `data/catalyst_dev_ws4b.db` (one-time copy from frozen DB).
- **8GB-Mac safe**: Batching (BATCH_COMMIT_SIZE=100), no heavy deps (no lancedb, no FlagEmbedding, no nltk).
- **No keys printed**: API keys are never read from env.
- **No new dependencies**: All new code uses existing deps (stdlib, sqlite3, zlib, json, existing catalyst_data modules).
- **No commit**: Plan only — do not commit, do not create worktrees.
- **Local test scope only**: Run only new tests + core pipeline/storage/align/dedup tests. Defer lancedb/embedding tests to server.

---

## Files Changed Summary

| File | Action | Section |
|---|---|---|
| `packages/data-core/catalyst_data/articles.py` | Create | B0 |
| `packages/data-core/catalyst_data/storage/sqlite.py` | Modify (wire articles into init_db; add raw_asset_id to clean_assets DDL + upsert_clean_asset) | B0, B2 |
| `packages/data-core/tests/test_articles_schema.py` | Create | B0 |
| `packages/data-core/catalyst_data/rederive.py` | Create | B1 |
| `packages/data-core/tests/test_rederive.py` | Create | B1 |
| `packages/data-core/catalyst_data/pipeline/transform_v2.py` | Create | B2 |
| `packages/data-core/catalyst_data/regenerate_clean.py` | Create | B2 |
| `packages/data-core/catalyst_data/orchestrator.py` | Modify (polygon_news path only: per-article clean_assets with raw_asset_id) | B2 |
| `packages/data-core/tests/test_no_merge_regression.py` | Create | B2 |
| `packages/data-core/tests/test_regenerate_clean.py` | Create | B2 |
| `packages/data-core/tests/test_pipeline.py` | Modify (update news merge tests) | B2 |
| `packages/data-core/tests/test_orchestrator.py` | Modify (update for per-article polygon_news path) | B2 |

---

## Risks

1. **Legacy FK on clean_assets**: The existing `FOREIGN KEY (asset_id) REFERENCES raw_assets(asset_id)` will have non-matching values for polygon_news rows (`poly:{id}` format). Mitigation: `PRAGMA foreign_keys=OFF` during regeneration and process_request for polygon_news; documented as unenforced for that scope. Non-news rows keep original asset_ids and FK integrity.
2. **LanceDB staleness**: Post-B2, any existing dev LanceDB index is invalid (asset_ids changed, row count changed). Mitigation: explicitly documented; LanceDB rebuild is B3/B8. Frozen eval LanceDB + embeddings are untouched.
3. **orchestrator concurrency**: `process_request` uses `asyncio.gather` + thread pool for SQLite writes. Per-article upserts increase write volume 6.7× for polygon_news. Mitigation: each thread-bound worker opens its own connection (existing pattern); batch commits keep contention low; test_orchestrator.py updated to verify.
4. **ohlcv calendar dependency**: If the dev DB copy is missing ohlcv data, re-derive fails with hard error. Mitigation: the one-time copy from frozen DB guarantees the calendar is present; test_rederive_missing_calendar_is_error verifies the hard-error behavior.
5. **dev DB disk usage**: 20,867 clean_assets rows (~50–100 MB) plus articles table (~30–50 MB). Mitigation: well within local disk capacity; no impact on frozen DB.

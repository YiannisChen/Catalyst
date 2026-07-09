# S3: Data Belt — Implementation Plan (AMENDED v3)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Create a corpus_items VIEW with articles (INNER JOIN article_tickers) plus filings (ROW_NUMBER() window function for index_builder-compatible document selection), content_md byte-identical to index_builder L1. Rewire SQL fallback. Never write frozen DB (os.path.realpath guard). Canonicalize timestamps (D2), derive watermarks from source_checkpoints (D3), wire pending index_state (D4).

**Architecture:** SQLite VIEW with window functions (≥3.25). Frozen DB compat is READ-side only via sqlite_master check. Frozen guard uses os.path.realpath against declared constant, not endswith suffix.

**Tech Stack:** SQLite ≥3.25 VIEW, Python 3.12+, pytest.

**Design Refs (normative):** §0.1, D1, D2, D3, D4, §0.7.

**Process rule:** CLAUDE.md for writing-plans: strictly exclude all code blocks.

---

## Phase 0: Real-Run Verification Evidence

### 0.1: Frozen DB SHA
0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf ✓

### 0.2: Frozen DB — no new tables
Only clean_assets, raw_assets, ohlcv, agent_runs, trace_events, node_artifacts, source_checkpoints, etc. No articles, article_tickers, filings, filing_documents, index_state. ✓

### 0.3: Dev DB — has all tables
articles, article_tickers, filings, filing_documents, index_state, index_manifests, etc. ✓

### 0.4: article_tickers schema
article_id TEXT PK1, ticker TEXT PK2, raw_asset_id TEXT NOT NULL, reference_date TEXT NOT NULL, dedup_group_id TEXT, is_canonical INTEGER DEFAULT 1 ✓

### 0.5: L1 content_text derivation (index_builder.py anchors)
Article L1 (line 98): content_text = f"{title}\n{body}" where body = description or "". chunk_id = "{article_id}::l1".
Filing L1 (line ~310): title = f"{form_type} filed {filed_at}", body = doc_text or "", content_text = f"{title}\n{body}" if body else title. Document preference: exhibit_99_1 over primary_doc (Python logic: ORDER BY document_type ASC, first row with text wins). ✓

### 0.6: SQLite version supports window functions
SQLite ≥3.25 (2018) supports ROW_NUMBER() OVER (...) in views. Modern macOS sqlite3 is ≥3.39. ✓

### 0.7: Source tier distribution
Tiers 2–5 have live articles (tier 5: 7,971). is_rag_eligible present. ✓

### 0.8: No existing views in dev DB ✓

---

## Dependencies & CORE-vs-STRONG Boundary

- **Dependencies:** None. Independent.
- **CORE:** Phases 1–5.
- **STRONG:** Phases 6–7.

---

## Phase 1: Build corpus_items VIEW

**Design refs:** §0.1.

### 1.1: Grain
One row per (article_id × ticker). INNER JOIN article_tickets (LEFT JOIN yields NULL-ticker rows; §0.1 grain is per-association).

### 1.2: content_md — Articles Branch
SQLite: title || char(10) || COALESCE(description, '')
Byte-identical to index_builder L1 content_text. ✓

### 1.3: content_md — Filings Branch (AMENDED — fix #10 should-fix)
SQLite window function reproduces index_builder's document preference:
ROW_NUMBER() OVER (PARTITION BY f.filing_id ORDER BY CASE fd.document_type WHEN 'exhibit_99_1' THEN 0 ELSE 1 END, fd.document_type) AS doc_rank
WHERE doc_rank = 1
content_md = (f.form_type || ' filed ' || f.filed_at) || char(10) || COALESCE(fd.text, '')
When fd.text is NULL/empty, fallback to title only without trailing newline.
Byte-identical to index_builder filing L1. ✓

### 1.4: source_kind
'article' | 'filing' only. No 'clean_asset'.

### 1.5: Column List
articles branch: corpus_item_id, source_kind, ticker, provider, source_type, reference_date (from article_tickers), published_utc, content_md, title, article_url, publisher_name, source_tier, dedup_group_id, is_canonical, is_rag_eligible
filings branch: corpus_item_id, source_kind, ticker, provider='sec', source_type='sec_filing', reference_date=filed_at, published_utc=filed_at, content_md, title=form_type, article_url=url, publisher_name='SEC', source_tier, dedup_group_id, is_canonical, is_rag_eligible

### 1.6: Frozen DB Compat — READ-SIDE ONLY (AMENDED — fix #11 should-fix)
FROZEN_PATHS constant: set of os.path.realpath() values.
Guard: if os.path.realpath(db_path) in FROZEN_PATHS → raise FrozenDBWriteError for any CREATE VIEW / CREATE TABLE / INSERT / UPDATE / DELETE.
NOT endswith('frozen_v2.db') — misses copies, renames, future v3.

_sql_fallback_query READ-side compat:
1. Check sqlite_master for corpus_items VIEW
2. VIEW exists → SELECT FROM corpus_items
3. VIEW absent → SELECT FROM clean_assets (legacy path), fallback_surface='clean_assets'

Biting test: full retrieval pass over frozen DB → SHA before == SHA after == 0dfc81... ✓

TEST INTENT:
1. Finnhub-only day → hits through corpus_items (zero through clean_assets alone)
2. Article L1: content_md byte-identical to index_state.content_text
3. **Filing L1: content_md byte-identical to index_state.content_text (window function reproduces document preference)**
4. Multi-ticker article → one row per ticker via INNER JOIN (no NULL-ticker rows)
5. is_rag_eligible present in output
6. Frozen DB retrieval → SHA unchanged
7. init_db against frozen realpath → raises FrozenDBWriteError
8. Renamed copy of frozen DB (os.path.realpath in FROZEN_PATHS) → also raises

**Files:**
- Modify: catalyst_data/storage/sqlite.py (VIEW DDL, FROZEN_PATHS, guard)
- Modify: catalyst_agents/retrieval/policy.py (_sql_fallback_query READ-side fallback)
- Create: catalyst_data/tests/test_s3_corpus_items.py
- Create: catalyst_data/tests/test_s3_frozen_db_readonly.py

---

## Phase 2: Rewire SQL Fallback

FROM clean_assets → FROM corpus_items. Column aliases match existing contract.

**Files:**
- Modify: catalyst_agents/retrieval/policy.py

---

## Phase 3: D2 Timestamp Canonicalization

Finnhub +00:00 → 'Z'. Idempotent. Frozen DB guard. Post-condition: zero +00:00.

**Files:**
- Modify: catalyst_data/migrations.py
- Create: catalyst_data/tests/test_s3_timestamp_canonical.py

---

## Phase 4: D3 Watermark Derivation

From source_checkpoints. No new table. RunReport: embedded_count, queued_count, pending_after, skipped_ineligible, watermarks dict, dedup dict (nullable at CORE — references STRONG Phase 6).

**Files:**
- Modify: catalyst_data/run_report.py
- Create: catalyst_data/tests/test_s3_watermark.py

---

## Phase 5: D4 Pending index_state Writes

persist_index_state(): one row per article, status='pending'. Embed step: no backend → no-op log.

**Files:**
- Modify: catalyst_data/index_builder.py, catalyst_data/orchestrator.py

---

## Phase 6 (STRONG): D5 Near-Dup Detection

## Phase 7 (STRONG): D7 Freshness Gate

---

## Post-Implementation Verification

1. Frozen DB SHA unchanged: 0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf
2. Full retrieval pass over frozen DB → SHA unchanged
3. corpus_items content_md byte-identical to index_state L1 for articles AND filings
4. Filing document selection matches index_builder (ROW_NUMBER with exhibit preference)
5. Multi-ticker → one row per ticker, zero NULL tickers
6. source_kind ∈ {'article','filing'}
7. is_rag_eligible column present
8. Frozen guard uses os.path.realpath, not endswith
9. Zero +00:00 timestamps

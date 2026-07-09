# WS4B Step 3 — Connectors & Source Expansion (Implementation Plan)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> **Status:** PLAN ONLY — for Claude review. Do not implement, edit code, run network calls, or commit.
> **PROVISIONAL — depends on Step 1 outcomes (ticker-scoping mechanism, index_builder API, L2 threshold) and Step 2 outcomes (update pipeline API, freshness model); re-validate after Steps 1–2 are executed and reviewed.**
> **Prerequisites:** Steps 1–2 reviewed/merged. Branch `ws4b/article-level-data`.
> **Archive:** articles = 11,772 all polygon_news; article_tickers = 20,867; clean_assets polygon_news = 20,867; source_tier all NULL (Step 1 pending); no SEC/Finnhub/FMP-news data ingested.

**Goal:** Add SEC official filings (T1 primary source) as a new connector with its own `filings` table; audit Finnhub free-tier `company-news` and FMP news/article endpoints to determine availability; plug confirmed sources into the existing tier/index/pipeline. No broad crawling — 1–2 tickers × 3–5 days small-sample live validation only.

**Architecture:** SEC is a fundamentally different source (structured filings, not news articles) → gets its own `filings` table parallel to `articles`. Finnhub and FMP news follow the existing Polygon pattern: connector → raw_assets → articles → article_tickers → clean_assets → tier → index. Cross-source dedup via `dedup_group_id` populated in `articles` table. All new connectors follow the `create_<name>_fetcher` closure pattern from `base.py`. Orchestrator and articles edits are strictly additive and surgical — polygon_news + non-news paths remain byte-identical.

**Tech Stack:** Python 3.11+, httpx, sqlite3, existing connector/retry/limiter infrastructure. No new dependencies. SEC EDGAR submissions API (no API key required).

---

## Task 1 — Sanitized Provider Audit (RUNS FIRST)

### 1.1 Purpose

Before building any connector, audit Finnhub and FMP news endpoints to confirm availability on the current API keys. The existing `provider_audit.py` only knows about `polygon_*`, `fmp_fundamentals`, `fred_*`, and `yfinance_*`. Extend it to probe:
- FMP news endpoints: `stock-news`, `press-releases`, `fmp-articles`
- Finnhub `company-news`
- SEC EDGAR submissions (always available — verify form types)

**Sanitization rules (mandatory):**
1. NEVER print or log any API key value. Use `hashlib.sha256(key)[:12]` if key identity is needed.
2. NEVER print full response payloads. Only print: HTTP status code, count of top-level JSON keys, presence (True/False) of expected field names, and for one sample article: its top-level field NAMES only — no values.
3. Print one-line summary per endpoint: status, field_count, expected_fields_present.
4. If status is 402/403, print "UNAVAILABLE on current plan" — no further analysis.

**Audit target (per endpoint, 1 ticker × 1 date):**

| Provider | Endpoint | Expected Fields | Key Question |
|----------|----------|-----------------|--------------|
| FMP | stock-news | symbol, publishedDate, title, text, site | Free tier? |
| FMP | press-releases | symbol, date, title, text | Free tier? |
| FMP | fmp-articles | title, date, content, tickers | Free tier? |
| Finnhub | company-news | category, datetime, headline, summary, source, url | Free tier? |
| SEC | EDGAR submissions | cik, entityName, filings.recent (form, filingDate, primaryDocument) | Always available |

**Output:** JSON report at `data/eval_reports/sanitized_audit_<timestamp>.json`. Each endpoint entry includes: endpoint, status, available (bool), top_level_keys list, expected_fields dict (key→present bool), sample_article_field_names list.

**Implementation:** Modify `scripts/provider_audit.py` — add a `--audit-mode sanitized` flag that skips the full ingest/clean/transform pipeline (no DB writes), makes ONE request per endpoint, and writes the sanitized JSON report.

**Contingency:** This task MUST run FIRST. The results determine whether Tasks 4 (Finnhub) and 5 (FMP news) are built at all. Tasks 2 (SEC) and 3 (filings table) are always built regardless of audit results.

**Files:**
- Modify: `packages/data-core/scripts/provider_audit.py`
- Test: `packages/data-core/tests/test_sanitized_audit.py`

**Test assertions (prose):**
1. Sanitized output contains zero occurrences of "apiKey" or "apikey" (case-insensitive).
2. Sanitized output contains field names only, never field values (e.g., no "AAPL announces" in output).
3. HTTP 402 → available=false.
4. HTTP 403 → available=false.
5. SEC EDGAR → always available=true (no API key required).

---

## Task 2 — SEC EDGAR Connector

### 2.1 New module: `catalyst_data/connectors/sec.py`

**Purpose:** Fetch SEC filings (8-K, 10-Q, 10-K) from the EDGAR submissions API. SEC filings are structured documents (form type, period, items, primary document link) — they get their own storage table, not the `articles` table.

**API:** `https://data.sec.gov/submissions/CIK{cik_padded}.json`. No API key required. Must include descriptive `User-Agent` header (SEC requirement). Rate limit: ≤10 req/s (conservative: 5 req/s). CIK zero-padded to 10 digits.

**Connector contract (follows the `create_polygon_fetcher` closure pattern):**
`create_sec_fetcher(user_agent, limiter=None, client=None) -> async fetch(ticker, endpoint, date) -> FetchResult`. Endpoint is one of '8-K', '10-Q', '10-K'. Ticker is resolved to CIK via the CIK map. Date filters `filings.recent` to only entries where `form` matches the requested type AND `filingDate` equals the requested date.

**CIK map asset:** Static JSON file `catalyst_data/connectors/sec_cik_map.json` mapping all 10 universe tickers to their CIK and company name. CIKs are zero-padded to 10 digits. Example entries: AAPL→0000320193, MSFT→0000789019, NVDA→0001045810.

**Form type filter logic:** After fetching the full submissions JSON for a CIK, iterate `filings.recent[]` entries. Keep only entries where `form ∈ {'8-K', '10-Q', '10-K'}` AND `filingDate == requested_date`. Return the filtered list in `FetchResult.data` under key `"filings"`.

**Files:**
- Create: `packages/data-core/catalyst_data/connectors/sec.py`
- Create: `packages/data-core/catalyst_data/connectors/sec_cik_map.json`
- Test: `packages/data-core/tests/connectors/test_sec.py` (uses saved fixture — no network)
- Fixture: `packages/data-core/tests/fixtures/sec_aapl_submissions.json` (real SEC response, API keys stripped)

**Test assertions (prose):**
1. SEC fetcher returns only filings matching the requested form type.
2. SEC fetcher filters by filingDate exactly.
3. Unknown ticker (not in CIK map) returns status=0 with error containing "CIK".
4. User-Agent header is included in the HTTP request.
5. Empty filings (no matching form+date) returns empty list, not error.
6. All 10 universe tickers resolve to valid CIKs in the map.

---

## Task 3 — SEC `filings` Table (Schema + Storage)

### 3.1 New DDL in `catalyst_data/articles.py`

**Purpose:** SEC filings are structurally different from news articles (form types, periods, items, accession numbers). A dedicated `filings` table avoids polluting the `articles` table with SEC-specific columns.

**DDL (schema contract — the only code block retained):**

```sql
CREATE TABLE IF NOT EXISTS filings (
    filing_id       TEXT PRIMARY KEY,        -- "sec:{cik}:{accession_number}"
    cik             TEXT NOT NULL,            -- zero-padded 10-digit CIK
    ticker          TEXT NOT NULL,            -- canonical ticker symbol
    form_type       TEXT NOT NULL,            -- '8-K', '10-Q', '10-K'
    filed_at        TEXT NOT NULL,            -- ISO date of filing
    period          TEXT,                     -- reporting period end date
    accession_number TEXT NOT NULL,
    primary_document TEXT,                    -- e.g. "aapl-20250930.htm"
    description     TEXT,                     -- items description (from 8-K)
    url             TEXT NOT NULL,            -- full SEC document URL
    items_json      TEXT,                     -- JSON array of 8-K item numbers
    source_tier     INTEGER NOT NULL DEFAULT 1,  -- T1: immutable
    dedup_group_id  TEXT,                     -- cross-source dedup
    is_canonical    INTEGER DEFAULT 1,
    is_rag_eligible INTEGER DEFAULT 1,
    quality_score   REAL DEFAULT 1.0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_filings_cik ON filings(cik);
CREATE INDEX IF NOT EXISTS idx_filings_ticker_date ON filings(ticker, filed_at);
CREATE INDEX IF NOT EXISTS idx_filings_form_type ON filings(form_type);
```

**Upsert function contract:** `upsert_filing(conn, *, filing: dict) -> None` — INSERT OR REPLACE one filing row. `source_tier` is always 1 (hardcoded, not from the classifier).

### 3.2 SEC normalization pipeline

After the SEC connector returns filtered filings, a normalization step converts EDGAR submission entries into `filings` rows. This is handled via a new `sec_normalize.py` module:

**Function contract:** `normalize_sec_filing(entry: dict, cik: str, ticker: str) -> dict` — maps EDGAR `filings.recent[]` entry fields to the `filings` table schema. Computes `filing_id = f"sec:{cik}:{accession_number}"`. Constructs `url = f"https://www.sec.gov/Archives/edgar/data/{cik_no_pad}/{accession_dashes}/{primary_document}"`. Extracts `items_json` from 8-K items field if present.

### 3.3 Modify: `catalyst_data/orchestrator.py` (ADDITIVE, SURGICAL)

Add a branch in `_process_source` for `source == "sec_filings"`. The branch:
- After ingest/clean succeeds, routes to a new `_store_sec_filings()` function (analogous to `_store_bronze_and_silver` but writes to `filings` table).
- Still writes raw_assets (the full SEC JSON response is stored as Bronze).
- Does NOT touch the `articles` or `clean_assets` tables for SEC data.
- **All existing polygon_news and non-news paths remain byte-identical.** The sec_filings branch is an additive else-if clause — no existing code paths are modified.

**Files:**
- Modify: `packages/data-core/catalyst_data/articles.py` (add filings DDL + upsert_filing)
- Modify: `packages/data-core/catalyst_data/orchestrator.py` (add sec_filings branch — additive only)
- Create: `packages/data-core/catalyst_data/pipeline/sec_normalize.py`
- Test: `packages/data-core/tests/test_sec_filings.py`

**Test assertions (prose):**
1. filings table exists after init_db.
2. upsert_filing inserts a row; second call with same filing_id replaces it.
3. source_tier is always 1 after insert (cannot be overridden to non-1).
4. All expected columns are populated from a normalized EDGAR entry.
5. Orchestrator sec_filings branch does not modify articles or clean_assets tables.
6. Existing polygon_news pipeline path is unchanged (byte-identical code paths).

---

## Task 4 — Finnhub Company News Connector (CONTINGENT on Audit)

### 4.1 New module: `catalyst_data/connectors/finnhub.py`

**Contingency:** ONLY build this if the sanitized audit (Task 1) confirms `finnhub:company-news` returns HTTP 200 with expected fields. If audit shows 402/403, SKIP this task entirely and document: "Finnhub unavailable on current plan."

**API:** `https://finnhub.io/api/v1/company-news?symbol={ticker}&from={date}&to={date}&token={api_key}`. Free tier: 60 req/min. Returns list of news articles.

**Connector contract:** `create_finnhub_fetcher(api_key, limiter=None, client=None) -> async fetch(ticker, endpoint, date) -> FetchResult`. Follows the same closure pattern as Polygon.

**Normalization to articles:** Finnhub articles map to `articles` with `provider='finnhub'`, `source_type='finnhub_company_news'`. Field mapping: `headline→title`, `summary→description`, `datetime→published_utc` (Unix→ISO), `source→publisher_name`, `url→article_url`, `category` preserved in metadata.

**Tier mapping:** Finnhub → T3 (Wire Service) via `source_tier.py` classifier.

**Files (all conditional on audit):**
- Create: `packages/data-core/catalyst_data/connectors/finnhub.py`
- Modify: `packages/data-core/catalyst_data/config.py` (add `finnhub` to provider key env map)
- Modify: `packages/data-core/catalyst_data/source_mapping.py` (add `finnhub_company_news→["company-news"]`)
- Modify: `packages/data-core/scripts/smoke_test.py` (add Finnhub routing in _build_fetch_fn)
- Modify: `packages/data-core/catalyst_data/source_tier.py` (add Finnhub publisher→T3)
- Test: `packages/data-core/tests/connectors/test_finnhub.py` (saved fixture)

**Test assertions (prose, conditional):**
1. Finnhub fetcher returns 200 with mocked fixture data.
2. API key is passed as query param `token=...`.
3. Field mapping: headline→title, summary→description, datetime Unix→ISO correct.
4. Rate limiter is honored (acquire called before request).

---

## Task 5 — FMP News Connector (CONTINGENT on Audit)

### 5.1 Extend: `catalyst_data/connectors/fmp.py`

**Contingency:** ONLY build this if the sanitized audit confirms at least one FMP news endpoint (stock-news, press-releases, or fmp-articles) returns HTTP 200. The existing `fmp.py` only handles fundamentals (income_statement, balance_sheet, cash_flow). Add news endpoint routing — whatever endpoint(s) the audit confirms.

**Endpoint routing:** Add the confirmed endpoint(s) to `ENDPOINT_PATH_MAP`. Each maps the logical endpoint name to the FMP API path. Stock-news uses `stock-news` path with `symbol` and `apikey` params. The existing connector infrastructure (retry, limiter, error handling) is reused without modification.

**Normalization to articles:** FMP news articles map to `articles` with `provider='fmp'`, `source_type='fmp_news'`. Field mapping: `title→title`, `text` or `content→description`, `publishedDate→published_utc`, `site→publisher_name`, `url→article_url`, `symbol→ticker` (via article_tickers).

**Tier mapping:** FMP news → T3 (Wire Service) via `source_tier.py`.

**Files (all conditional on audit):**
- Modify: `packages/data-core/catalyst_data/connectors/fmp.py` (add news endpoint routing)
- Modify: `packages/data-core/catalyst_data/source_mapping.py` (add `fmp_news→[confirmed_endpoint]`)
- Modify: `packages/data-core/scripts/smoke_test.py` (add FMP news routing)
- Modify: `packages/data-core/catalyst_data/source_tier.py` (add FMP publisher→T3)
- Test: `packages/data-core/tests/connectors/test_fmp_news.py` (saved fixture)

**Test assertions (prose, conditional):**
1. FMP fetcher routes stock-news endpoint correctly.
2. Field mapping maps title/text/publishedDate/site/url correctly.
3. HTTP 402 returns available=false in fetch result.
4. 429 triggers retry with backoff.

---

## Task 6 — Tier Mapping Updates

### 6.1 Modify: `catalyst_data/source_tier.py` (Step 1 artifact)

Add entries for the new sources (regardless of audit outcomes):

| Source | Tier | Label |
|--------|------|-------|
| SEC (all filings) | T1 | Primary Source — hardcoded in upsert_filing, also defined in tier map for indexing |
| Finnhub (company-news) | T3 | Wire Service |
| FMP (stock-news/press-releases) | T3 | Wire Service |

SEC source_tier is set to 1 at insert time (hardcoded), not via the classifier. But the tier constant is defined in `source_tier.py` for index record building and retrieval policy use.

**Files:**
- Modify: `packages/data-core/catalyst_data/source_tier.py`

---

## Task 7 — Cross-Source Dedup

### 7.1 Modify: `catalyst_data/config.py`

Update `CROSS_SOURCE_PRIORITY` to include SEC at the top:

```
Priority order: sec_filings (T1) > polygon_news (T4) > fmp_news (T3) > finnhub_company_news (T3) > gdelt_news (future)
```

### 7.2 New module: `catalyst_data/dedup/populate_dedup_groups.py`

**Purpose:** After articles from multiple sources are loaded, detect cross-source duplicates and populate `dedup_group_id` on `articles` and `filings` tables. Runs as a post-batch step in the update pipeline.

**Algorithm (prose):**
1. Load all articles + filings with their canonical URLs and normalized titles.
2. Group by `canonical_url` — exact URL match → same `dedup_group_id` (UUID).
3. Within same ticker + same date (±1 day): group by normalized title match → same `dedup_group_id`.
4. The canonical article in each group is the one with highest `CROSS_SOURCE_PRIORITY` rank (SEC beats Polygon beats Finnhub beats FMP).
5. Non-canonical articles get `is_canonical=0`.
6. This is a Phase 2 operation — runs AFTER all sources are loaded, not during individual ingestion.

**Files:**
- Create: `packages/data-core/catalyst_data/dedup/populate_dedup_groups.py`
- Modify: `packages/data-core/catalyst_data/config.py` (update CROSS_SOURCE_PRIORITY)
- Test: `packages/data-core/tests/test_cross_source_dedup.py`

**Test assertions (prose):**
1. Two articles with same canonical URL → same dedup_group_id.
2. Two articles with same title, same ticker, within 4h → same dedup_group_id.
3. SEC beats Polygon in priority (SEC article is canonical when both share same URL).
4. Non-duplicate articles keep dedup_group_id=NULL.
5. Empty input → no errors.

---

## Task 8 — Pipeline Integration

### 8.1 Modify: `catalyst_data/update_pipeline.py` (Step 2 artifact)

Add `sec_filings`, `finnhub_company_news`, and `fmp_news` as recognized sources. The update pipeline already calls `orchestrator.process_request` per source — new sources route through `source_mapping` and the orchestrator's new branches without modifying the pipeline's core loop.

Post-batch step additions (after regenerate_clean_assets):
1. Re-derive new-source articles (Finnhub/FMP news — SEC goes to filings table).
2. Run cross-source dedup: `populate_dedup_groups(conn)`.
3. Classify source tiers (includes Finnhub/FMP publishers).
4. Regenerate clean_assets (re-runs for all news sources).
5. Incremental index dry-run (delta-only, includes new article_ids + filing_ids).

### 8.2 Modify: `catalyst_data/cli_index.py` (Step 2 artifact)

Add `sec_filings`, `finnhub_company_news`, `fmp_news` to the `--sources` choices in `update-news` and `backfill` subcommands.

**Files:**
- Modify: `packages/data-core/catalyst_data/update_pipeline.py`
- Modify: `packages/data-core/catalyst_data/cli_index.py`

---

## Task 9 — Small-Sample Live Validation

### 9.1 Validation script: `scripts/validate_step3.py`

**Purpose:** Run 1–2 tickers × 3–5 days across all newly available sources. NOT a full backfill — tiny smoke test to verify connectors work end-to-end with real API calls. Gated: runs only after all connectors pass mocked tests.

**What it validates:** Runs `update-news --from X --to Y` for each source via Step 2 pipeline. After completion, queries: filings count, per-provider article counts, form type distribution, dedup group count, freshness report. Produces Markdown + JSON report.

**Files:**
- Create: `packages/data-core/scripts/validate_step3.py`

---

## Task 10 — Tests (Full Suite)

### 10.1 Test files and key assertions

| File | Count | Key Assertions |
|------|-------|----------------|
| `tests/test_sanitized_audit.py` | 5 | No API keys, field-names-only, 402/403→unavailable, SEC always available |
| `tests/connectors/test_sec.py` | 6 | Form type filter, date filter, CIK resolution, User-Agent, unknown ticker, empty filings |
| `tests/test_sec_filings.py` | 6 | filings DDL, upsert_filing, source_tier=1 immutable, orchestrator additive (polygon unchanged), normalization correct |
| `tests/connectors/test_finnhub.py` | 4 | Conditional: HTTP 200, field mapping, rate limit honored, API key in params |
| `tests/connectors/test_fmp_news.py` | 4 | Conditional: endpoint routing, field mapping, 402 handling, 429 retry |
| `tests/test_cross_source_dedup.py` | 6 | URL match, title+date match, priority order, is_canonical, non-duplicates, empty input |
| `tests/test_source_tier.py` (extend) | 3 | SEC→T1, Finnhub→T3, FMP news→T3 |

### 10.2 Test runner

```bash
cd packages/data-core && python -m pytest \
  tests/test_sanitized_audit.py \
  tests/connectors/test_sec.py \
  tests/test_sec_filings.py \
  tests/connectors/test_finnhub.py \
  tests/connectors/test_fmp_news.py \
  tests/test_cross_source_dedup.py \
  tests/test_source_tier.py \
  -v
```

### 10.3 Saved fixtures

All fixture JSON files are real API responses (fetched once), stripped of API keys and personally identifiable content. Keys replaced with `"REDACTED"`.

| Fixture | Source | Purpose |
|---------|--------|---------|
| `tests/fixtures/sec_aapl_submissions.json` | Real SEC EDGAR API | SEC connector tests |
| `tests/fixtures/finnhub_aapl_news.json` | Real Finnhub API (if available) | Finnhub connector tests |
| `tests/fixtures/fmp_aapl_news.json` | Real FMP API (if available) | FMP news connector tests |

---

## Task 11 — Validation Report (Post-Implementation)

| Metric | Expected | Verification |
|--------|----------|--------------|
| Sanitized audit complete | JSON report, all 5 endpoints | data/eval_reports/sanitized_audit_*.json |
| SEC connector returns correct form types | 8-K, 10-Q, 10-K only | test_sec.py |
| filings table populated | ≥1 filing per ticker | DB query |
| filings.source_tier always 1 | T1 for all rows | SELECT DISTINCT |
| Finnhub connector (if OK) | articles with provider='finnhub' | DB query |
| FMP news connector (if OK) | articles with provider='fmp', source_type='fmp_news' | DB query |
| Cross-source dedup groups | dedup_group_id non-null | DB query |
| CIK map covers 10 tickers | 10 entries | JSON file length |
| No duplicate article_ids | COUNT = COUNT DISTINCT | DB query |
| Frozen DB unchanged | SHA-256 unchanged | shasum |
| No secrets in any output | grep empty | grep audit reports |
| Orchestrator polygon_news path unchanged | Byte-identical | diff before/after |

---

## Files Summary

### Created
| File | Condition |
|------|-----------|
| `catalyst_data/connectors/sec.py` | Always |
| `catalyst_data/connectors/sec_cik_map.json` | Always |
| `catalyst_data/connectors/finnhub.py` | If audit OK |
| `catalyst_data/pipeline/sec_normalize.py` | Always |
| `catalyst_data/dedup/populate_dedup_groups.py` | Always |
| `scripts/validate_step3.py` | Always |

### Modified
| File | Change | Condition |
|------|--------|-----------|
| `scripts/provider_audit.py` | --audit-mode sanitized | Always |
| `catalyst_data/config.py` | finnhub+sec keys; CROSS_SOURCE_PRIORITY | Always |
| `catalyst_data/source_mapping.py` | sec_filings, finnhub, fmp_news entries | If audit OK |
| `catalyst_data/articles.py` | filings DDL + upsert_filing | Always |
| `catalyst_data/orchestrator.py` | sec_filings branch (additive) | Always |
| `catalyst_data/source_tier.py` | SEC→T1, Finnhub→T3, FMP→T3 | Always |
| `catalyst_data/connectors/fmp.py` | News endpoints | If audit OK |
| `scripts/smoke_test.py` | Finnhub+FMP news routing | If audit OK |
| `catalyst_data/update_pipeline.py` | New sources + post-batch dedup | Always |
| `catalyst_data/cli_index.py` | New --sources choices | Always |

---

## Task Order

```
1. Sanitized audit (provider_audit.py)     — FIRST: determines what to build
   ↓
2. SEC connector + CIK map                 — Always (no key needed)
3. SEC filings table + normalization       — Always
   ↓
4. Finnhub connector (IF audit OK)         — Parallel with 5
5. FMP news connector (IF audit OK)        — Parallel with 4
   ↓
6. Tier mapping updates                    — Depends on 4,5
7. Cross-source dedup                      — Depends on all connectors
8. Pipeline integration                    — Depends on 2,4,5
9. Small-sample live validation            — After all, real API keys
10. Full test suite                        — Depends on 2-7
11. Validation report                      — After 9
```

---

## Guardrails

1. **data-core only** — no `packages/app/`, no `packages/agents/`.
2. **Dev DB only** — `data/catalyst_dev_ws4b.db`. Frozen DB read-only.
3. **Never print API keys** — audit uses hash for key identity. Fixtures strip keys to "REDACTED".
4. **Never print full payloads** — sanitized audit prints field names and counts only, never values.
5. **Additive DDL only** — `filings` table is `CREATE TABLE IF NOT EXISTS`. No DROP, no destructive ALTER.
6. **Orchestrator edits additive + surgical** — polygon_news + non-news paths remain byte-identical.
7. **No new heavy dependencies** — all imports from stdlib or existing httpx/catalyst_data.
8. **No commit** — all changes stay in working tree for review; the human commits.
9. **Audit runs FIRST** — Tasks 4 and 5 are gated on audit results. Tasks 2 and 3 always built.
10. **SEC fair-access** — declarative User-Agent, ≤5 req/s.
11. **Small-sample live validation only** — 1–2 tickers × 3–5 days. Large backfill gated.

---

## Contingency Table

| Audit Result | Action |
|-------------|--------|
| FMP stock-news → 200 | Build FMP news connector |
| FMP press-releases → 200 | Build FMP news connector |
| FMP fmp-articles → 200 | Build FMP news connector |
| FMP all news → 402 | Skip FMP news. Note: "FMP news unavailable." |
| Finnhub company-news → 200 | Build Finnhub connector |
| Finnhub company-news → 402/403 | Skip Finnhub. Note: "Finnhub unavailable." |
| SEC EDGAR → non-200 | Investigate (public API, should not happen) |

---

## Out of Scope

- **GDELT, company IR/PR** — deferred.
- **SEC forms beyond 8-K/10-Q/10-K** — not ingested.
- **Large-scale backfill** of new sources — gated. Only 1–2 tickers × 3–5 days validated.
- **Real GPU embedding** — Step 4.
- **Any UI or packages/app/agents changes.**

---

## Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Finnhub free tier excludes company-news | Medium | Skip connector; Polygon already covers commentary |
| FMP free tier excludes news endpoints | Medium | Skip; FMP fundamentals already work |
| SEC CIK map stale | Low | Static JSON, 10 tickers, CIKs rarely change |
| Cross-source dedup false positives | Medium | Conservative: exact URL + same-ticker title within 4h |
| Filing content needs second API call | Medium | Store primary document URL only; full text download deferred |
| Orchestrator edits break existing paths | Low | Additive branch only; polygon_news paths byte-identical; verified by diff |

---

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Run the sanitized audit (Task 1) FIRST. Tasks 4 and 5 are explicitly conditional on audit output. All work on dev DB only; no commit.

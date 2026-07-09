# WS4B Step 3C — SEC Primary Evidence (Final Plan)

**Status:** PLAN ONLY — not implemented
**Date:** 2026-07-01
**Branch:** `ws4b/article-level-data`
**Baseline:** `a41906b` (Step 2.1 fix)
**Design spike:** `docs/research/ws4b-step3c-sec-design-spike.md`
**Prerequisite:** Steps 1–2 reviewed and merged

**PROVISIONAL — depends on Step 1 outcomes (ticker-scoping mechanism, index_builder API,
L2 threshold); re-validate after Step 1 is executed and reviewed.**

---

## 0. Goal

Add SEC EDGAR as a T1 primary evidence source: 8-K full text with Exhibit 99.1 resolution,
10-Q/10-K metadata. Store filings in dedicated `filings` + `filing_documents` tables
(prose plane). Prepare for Step 4 indexing via the polymorphic `index_state`
(`source_kind='filing'`). Do not force filings into `clean_assets`.

Step 3C is ONE step delivered as TWO PRs reviewed separately and merged in order:
- **3C1:** Connector + storage + normalization + index builder + tests (zero pipeline)
- **3C2:** Update-pipeline integration + freshness + checkpoint semantics + CLI

Read each PR's plan section completely before implementing that PR. Re-validate
against Step 1 as-built APIs at PR start.

---

## 1. Two-Plane Corpus Lock (Architecture Constraint)

Catalyst evidence has TWO planes. This separation is intentional and permanent.

**Plane 1 — Prose evidence (embedded via index_state):**
- `articles` + `article_tickers` (source_kind='article') — Polygon, Finnhub, yfinance
- `filings` + `filing_documents` (source_kind='filing') — SEC 8-K/10-Q/10-K
- Prose text is L1/L2 chunked and dense-embedded in LanceDB (Step 4)

**Plane 2 — Structured signals (NEVER embedded):**
- `ohlcv` — daily price bars
- `calendar_events` — Nasdaq earnings dates (Step 3E)
- `insider_transactions` — SEC Form 4 (Step 3E)
- `macro_observations` — FRED/Fed/BLS (Step 3E)
- `market_signals` — FINRA short interest (Step 3E)
- Plane 2 tables are joined at query time via ticker/date; their rows never appear
  in `index_state`, never produce vectors, never enter `clean_assets`

**Implication for Step 3C:** `filings` is Plane 1 (prose). Every `filings` row with
`is_rag_eligible=1` produces exactly one L1 index record via `build_filing_records()`.
No `clean_assets` row is created for any filing.

---

## 2. Polygon Retention + Shared Dedup (Forward-Compat Note)

Step 3D (Finnhub) will ingest `articles` with `provider='finnhub'`. The `dedup_group_id`
column on `articles` is provider-agnostic — it hashes the canonical article identity
(title+date fingerprint) so a Polygon Motley Fool article and a Finnhub Yahoo article
covering the same event can share a `dedup_group_id`.

For `filings`, dedup is accession-based: `dedup_group_id = SHA256("sec:{accession_number}")[:16]`.
This means SEC filings never collide with articles (different namespace), but two SEC
connector runs ingesting the same accession will produce the same `dedup_group_id`.

Do NOT design anything in Step 3C that assumes `dedup_group_id` is a single-provider key
or that blocks cross-provider dedup between Polygon and Finnhub. The column is
provider-agnostic by design.

---

## 3. PR 3C1 — Connector + Storage + Normalize + Index Builder

### 3.1 Execution Order

| Task | Files | Depends On |
|------|-------|------------|
| T1 — CIK map | `catalyst_data/cik_map.py`, `data/cik_map/cik_ticker_map.csv` | — |
| T2 — Retry + provider limits | `catalyst_data/retry.py`, `catalyst_data/provider_limits.py` | — |
| T3 — SEC connector | `catalyst_data/connectors/sec.py` | T2 |
| T4 — Source mapping | `catalyst_data/source_mapping.py` | T3 |
| T5 — Filings DDL | `catalyst_data/storage/sqlite.py` | — |
| T6 — SEC normalization + Exhibit 99.1 | `catalyst_data/pipeline/sec_normalize.py` | T3, T5 |
| T7 — Index builder (filing records) | `catalyst_data/index_builder.py` | T5 |
| T8 — Tests + CLI `refresh-cik-map` | `tests/`, `catalyst_data/cli_index.py` | T1–T7 |

All tasks are data-core only. Zero pipeline integration in 3C1. Zero network in tests
(fixtures only).

### 3.2 T1 — CIK Map Asset

**New files:** `catalyst_data/cik_map.py`, `data/cik_map/cik_ticker_map.csv`

**Module contract (prose):**
- `SUPPORTED_TICKERS: frozenset[str]` — `{AAPL, AMD, AMZN, GOOGL, JPM, META, MSFT, NVDA, TSLA, UNH}`
- `ticker_to_cik(ticker: str) -> str` — returns zero-padded 10-digit CIK string, raises `KeyError` with clear message if missing.
- `cik_to_ticker(cik: str) -> str` — reverse lookup with same error contract.
- `refresh_cik_map(output_path: str | None = None) -> dict[str, str]` — fetches `https://www.sec.gov/files/company_tickers.json` (no key), validates all 10 `SUPPORTED_TICKERS` are present, writes CSV. CLI: `python -m catalyst_data.cli_index refresh-cik-map`.
- On module import: assert all 10 tickers resolve, raise clear error if missing.

**CSV format:** `ticker,cik` pairs. CIKs stored as zero-padded 10-digit strings (e.g., `0000320193`).

### 3.3 T2 — Retry Policy + Provider Limits

**Modify:** `catalyst_data/retry.py` — add `"sec"` entry to `RETRY_POLICIES` dict with:
- Rate limit rule: base 5.0s, max 30.0s, 3 retries, no jitter
- Server error rule: base 10.0s, max 60.0s, 3 retries, no jitter
- Timeout rule: base 10.0s, max 40.0s, 2 retries, no jitter

**Modify:** `catalyst_data/provider_limits.py` — add `SEC` entry referencing the existing
`_DEV_POLICIES["sec"]` rate policy from `config.py` (already present: `RatePolicy(0.2, 3, None)`).

### 3.4 T3 — SEC Connector

**New file:** `catalyst_data/connectors/sec.py`

**Factory function contract:**
```python
def create_sec_fetcher(
    user_agent: str,
    limiter=None,
    client: httpx.AsyncClient | None = None,
) -> <namespace>
```
The factory returns a namespace (tuple or types.SimpleNamespace) with two callables:

- `await fetcher.fetch(ticker: str, endpoint: str, date: str) -> FetchResult`
- `await fetcher.fetch_document(url: str) -> FetchResult`

Both share the same `user_agent`, `limiter`, and `client` — no duplication. The `fetch`
callable follows the standard connector contract. `fetch_document` is a provider-specific
extension that is NOT part of the base contract; callers check `hasattr(fetcher, 'fetch_document')`.

**Endpoints (argument to `fetch`):**

`"sec_submissions"` — fetches `https://data.sec.gov/submissions/CIK{cik_padded}.json`.
Returns the full submissions JSON. The `ticker` argument is used to resolve the CIK;
the `date` argument is unused (submissions API returns all filings for the CIK).
FetchResult.data shape: dict with top-level keys `cik, name, filings.recent` where
`filings.recent` contains parallel arrays `accessionNumber, filingDate, reportDate,
form, primaryDocument, items, primaryDocDescription, size`.

`fetch_document(url)` — fetches a single SEC document URL. The URL is typically an
8-K primary document or Exhibit 99.x URL. Uses the same `user_agent` and `limiter`.
Returns:
```python
FetchResult(status=200, data={
    "url": str,           # the fetched URL
    "text": str | None,   # extracted plain text, or None if extraction failed
    "content_type": str,  # e.g., "text/html"
    "byte_size": int,     # raw response byte count
    "extraction_status": str,  # "success" | "empty" | "pdf_skipped" | "fetch_failed" | "timeout"
})
```

The `with_retry` wrapper is applied to both callables with `provider="sec"`.

**Design constraints:**
- No API key — SEC EDGAR is public
- `user_agent` parameter is required and must be descriptive (SEC requirement: org name + email)
- Rate limit: ≤5 req/s enforced by limiter (TokenBucketLimiter)
- `Accept-Encoding: gzip, deflate` set on all requests
- 403 → treat as rate limit, honor Retry-After, retry
- 404 → non-retryable (bad accession/CIK)

### 3.5 T4 — Source Mapping

**Modify:** `catalyst_data/source_mapping.py`

Add single mapping:
```python
if source == "sec_filings":
    return ["sec_submissions"]
```

`sec_primary_doc` is NOT a logical source — it is called internally by normalization
via `fetcher.fetch_document(url)`. Only `sec_submissions` is the pipeline entry point.

### 3.6 T5 — Filings Schema DDL

**Modify:** `catalyst_data/storage/sqlite.py`

Add `ensure_filings_tables(conn)` function with additive DDL. Call from `init_db()`.

**filings table:**
```sql
CREATE TABLE IF NOT EXISTS filings (
    filing_id         TEXT PRIMARY KEY,       -- "sec:{cik_padded}:{accession_dashed}"
    cik               TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    form_type         TEXT NOT NULL,          -- '8-K', '10-Q', '10-K'
    filed_at          TEXT NOT NULL,          -- ISO date from SEC filingDate
    period            TEXT,                   -- reportDate (quarter/year end)
    accession_number  TEXT NOT NULL,
    primary_document  TEXT,                   -- filename from submissions
    url               TEXT NOT NULL,          -- filing detail page URL
    items_json        TEXT,                   -- 8-K items array as JSON string
    source_tier       INTEGER NOT NULL DEFAULT 1,
    dedup_group_id    TEXT,                   -- SHA256("sec:{accession}")[:16]
    is_canonical      INTEGER DEFAULT 1,
    is_rag_eligible   INTEGER DEFAULT 1,
    quality_score     REAL DEFAULT 1.0,
    raw_asset_id      TEXT,                   -- FK to raw_assets (submissions JSON)
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_filings_cik ON filings(cik);
CREATE INDEX IF NOT EXISTS idx_filings_ticker_date ON filings(ticker, filed_at);
CREATE INDEX IF NOT EXISTS idx_filings_form ON filings(form_type);
CREATE INDEX IF NOT EXISTS idx_filings_accession ON filings(accession_number);
```

**filing_documents table:**
```sql
CREATE TABLE IF NOT EXISTS filing_documents (
    filing_id         TEXT NOT NULL,
    document_url      TEXT NOT NULL,
    document_type     TEXT NOT NULL DEFAULT 'primary_doc',
    text              TEXT,                   -- extracted plain text
    char_len          INTEGER,
    content_type      TEXT,                   -- 'text/html', 'application/pdf'
    byte_size         INTEGER,
    extraction_status TEXT NOT NULL
        CHECK (extraction_status IN (
            'success', 'empty', 'pdf_skipped', 'fetch_failed', 'timeout'
        )),
    extracted_at      TEXT,
    PRIMARY KEY (filing_id, document_url),
    FOREIGN KEY (filing_id) REFERENCES filings(filing_id)
);
```

**Design invariants:**
- `filing_id = "sec:{cik_padded}:{accession_dashed}"` — e.g., `"sec:0000320193:0000320193-24-000070"`
- `document_type` values: `'primary_doc'` (the primaryDocument filename), `'exhibit_99_1'` (resolved Exhibit 99.1), `'exhibit_99_2'`, etc.
- A `filing_documents` row exists ONLY when a document fetch was attempted. 10-Q/10-K that are metadata-only have NO `filing_documents` row. Non-filtered 8-K items have NO `filing_documents` row. Absence of a row means "text not yet extracted."
- No FK from `filings` to `index_state` — `index_state` is polymorphic and stores `corpus_item_id = filing_id` with `source_kind = 'filing'` independently.

**Upsert helpers (new functions in sqlite.py):**
- `upsert_filing(conn, **kwargs) -> None` — INSERT OR REPLACE into `filings`
- `upsert_filing_document(conn, **kwargs) -> None` — INSERT OR REPLACE into `filing_documents`

### 3.7 T6 — SEC Normalization + Exhibit 99.1 Resolution

**New file:** `catalyst_data/pipeline/sec_normalize.py`

**3.7.1 Submissions normalization**

`normalize_submissions(raw_data: dict, ticker: str, cik: str, from_date: str, to_date: str) -> list[dict]`

- Input: the `FetchResult.data` dict from `sec_submissions`.
- Filters `filings.recent` to forms `'8-K'`, `'10-Q'`, `'10-K'` where `filingDate` is in `[from_date, to_date]`.
- For each matching filing, produces a dict ready for `upsert_filing()`:
  - `filing_id = f"sec:{cik_padded}:{accession_dashed}"`
  - `source_tier = 1`
  - `dedup_group_id = SHA256("sec:{accession}")[:16]`
  - `items_json = json.dumps(items_list)` (8-K items parsed from the submissions `items` field)
  - `is_rag_eligible`: True if the 8-K has any item in the attribution-relevant set (see §3.7.3), True for ALL 10-Q/10-K (metadata is valuable), False for non-relevant 8-K items.
- `url` is constructed as: `https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dash}/{accession_dashed}-index.htm`
- Returns list of filing dicts. Empty list if no filings in range.

**3.7.2 Exhibit 99.1 Resolution (CORRECTNESS-CRITICAL)**

Many 8-K primary documents are pointers to exhibits. For example, an Item 2.02 8-K may
have `primaryDocument = "ea0204059-8k.htm"` which is a boilerplate wrapper pointing to
`ex99-1.htm` containing the actual earnings release.

`resolve_filing_documents(fetcher, filing_dict: dict) -> list[dict]`

- Takes the `fetcher` (returned by `create_sec_fetcher`) and a filing dict from `normalize_submissions`.
- Fetches the filing's index-headers page:
  `{base_url}/{accession_no_dash}/{accession_dashed}-index-headers.htm`
- Parses the SGML-like `<DOCUMENT>...</DOCUMENT>` blocks to find all attached documents.
  Each block contains `<TYPE>`, `<SEQUENCE>`, `<FILENAME>`, `<DESCRIPTION>`.
- For attribution-relevant 8-Ks (has item in the 9-item filter set):
  - Always fetch the `primaryDocument` (document_type='primary_doc')
  - If the filing has Item 2.02 (earnings) and the index contains documents of type
    `EX-99.1` or `EX-99`, fetch the first such exhibit as `document_type='exhibit_99_1'`
  - More generally: if the primaryDocument's text is <200 chars after extraction,
    and the index contains exhibits of type `EX-99.*`, fetch the first exhibit as
    the evidence text — the primary document was a pointer.
- Returns list of document dicts, each ready for `upsert_filing_document()` with:
  `filing_id, document_url, document_type, text, char_len, content_type, byte_size, extraction_status, extracted_at`.
- For 10-Q/10-K: returns empty list (metadata only, no document fetch).

**3.7.3 8-K Item Filtering**

**Known limitation:** exhibit resolution handles EX-99.* only. Item 1.01 8-Ks often attach
EX-10.x (material agreements) which are not resolved by this mechanism. Those filings will
fall back to the primary document wrapper text. Record this limitation — EX-10.x resolution
is deferred to a future Step. EX-99.1/EX-99.2 (earnings releases, investor presentations)
are the primary attribution evidence targets.

Attribution-relevant set (constant, expandable):
```
1.01 — Entry into Material Definitive Agreement    → customer_partner_contracts, orders_demand
1.03 — Bankruptcy or Receivership                   → fundamentals_financials
2.02 — Results of Operations and Financial Condition → earnings_results
2.05 — Costs Associated with Exit or Disposal       → fundamentals_financials
2.06 — Material Impairments                         → fundamentals_financials
5.02 — Departure of Directors/Officers              → management_governance
7.01 — Regulation FD Disclosure                     → guidance_outlook, orders_demand
8.01 — Other Events                                 → corporate_actions, regulatory_legal
9.01 — Financial Statements and Exhibits            → earnings_results
```

**Policy:** ALL 8-K metadata is stored in `filings` regardless of items (provenance).
Only 8-Ks with at least one item in the above set get `is_rag_eligible=1` AND a
`filing_documents` row. Non-relevant 8-Ks get `is_rag_eligible=0` and no document row.
10-Q/10-K always get `is_rag_eligible=1` (metadata value) but no document row (text
extraction deferred to future Step).

**3.7.4 HTML Text Extraction**

`extract_text_from_html(html: str) -> str`

Uses stdlib `html.parser.HTMLParser` — NO new dependencies. The extractor strips
`<script>` and `<style>` elements, converts block-level tags (`p, div, br, li, tr,
h1-h6`) to newlines, and unescapes HTML entities. Collapses runs of 3+ newlines to 2.
Returns the visible text content suitable for embedding.

No `html2text`, no `bs4`, no `lxml`. The 8-K HTML is simple prose with occasional
tables — stdlib is sufficient for embedding-quality text extraction. Can be upgraded
later without API change.

### 3.8 T7 — Index Builder (Filing Records)

**Modify:** `catalyst_data/index_builder.py`

**Refactor existing:** Rename `build_index_records` → `build_article_records` (internal;
existing callers updated). Keep `build_index_records` as a thin wrapper calling both
`build_article_records()` and `build_filing_records()` for backward compatibility (CLI dry-run).

**New function:** `build_filing_records(conn, *, min_l2_chars=800) -> list[dict]`

- Queries `filings LEFT JOIN filing_documents` with `is_rag_eligible=1`. Uses LEFT JOIN
  (NOT inner) so metadata-only 10-Q/10-K and failed-extraction 8-Ks still emit their L1.
  Where a `filing_documents` row with `extraction_status='success'` exists, the document
  `text` is used as the body; otherwise body is empty.
- Produces index records with this per-filing shape:
  - `chunk_id = f"{filing_id}::l1"`
  - `chunk_level = "l1"`
  - `corpus_item_id = filing_id`
  - `source_kind = "filing"`
  - `content_hash = compute_content_hash(title, extracted_text)` where title = `"{form_type} filed {filed_at}"` and extracted_text is from `filing_documents.text`
  - `content_text = f"{title}\n{extracted_text}"`
  - `provider = "sec"`, `source_type = "sec_filing"`
  - `tickers = [ticker]` (single ticker from CIK→ticker map)
  - `source_tier = 1`, `filed_at`, `form_type`, `accession_number`, `filing_url = url`
- L2 records only when a `filing_documents` row with `extraction_status='success'`
  exists AND `len(body) >= min_l2_chars`. Sentence-split from the body (extracted text,
  NOT title) into L2 records with `chunk_id = f"{filing_id}::l2s{idx:04d}"`.
  Title is never included as an L2 sentence. Metadata-only and failed-extraction
  filings produce zero L2 records.
- For filings with no `filing_documents` row or `extraction_status != 'success'`:
  produces L1 record only. Content_text = title only. `content_hash = compute_content_hash(title, "")`
  (empty string, never None). Standardize both call sites.

**Guard assertions (protecting all callers including Step 4):**
- L1 filing count == `SELECT COUNT(*) FROM filings WHERE is_rag_eligible=1`
  (this counts ALL rag-eligible filings including metadata-only 10-Q/10-K and
  failed-extraction 8-Ks, matching the LEFT JOIN emit rule exactly)
- No duplicate `corpus_item_id` across articles AND filings (cross-plane guard)

**New function:** `build_filing_incremental_records(conn, *, min_l2_chars=800) -> dict`
- Diffs `filings` against `index_state` WHERE `source_kind='filing'`.
- Returns delta report: `{new_count, changed_count, total_delta, l1_count, l2_count, ...}`
- Writes NOTHING to `index_state` or `index_manifests` (Step 4 populates).

### 3.9 T8 — Tests + CLI

**New test fixtures** (under `tests/fixtures/`), captured from public SEC as a one-time
fixture-creation step at the start of 3C1 execution (distinct from the gated §9 live-validation):

- `sec_submissions_AAPL.json` — redacted SEC submissions JSON for AAPL with 3 filings
  (1 8-K Item 2.02, 1 8-K non-relevant item, 1 10-Q). Source: public SEC submissions API.
- `sec_8k_index_headers.htm` — index-headers page showing `<DOCUMENT>` blocks for the 8-K.
  Confirms which artifact carries the EX-99.1 document TYPE. Source: SEC EDGAR archive.
- `sec_8k_primary_wrapper.htm` — the short wrapper primary document HTML.
- `sec_8k_exhibit_99_1.htm` — the actual earnings release HTML (EX-99.1).
- `sec_8k_exhibit_99_1.txt` — expected text extraction output from the exhibit.
- `fixtures/sec/README.md` — records source URLs for each captured fixture.

CRITICAL: Do NOT hand-author fixtures that approximate SEC's format. Capture real SEC
content, redact, and pin the parser to what the real capture shows. The document TYPE
(EX-99.1) is confirmed from the index-headers page, NOT the submissions JSON.

**New test files:**

`tests/test_sec_connector.py` (6 tests):
- Submissions 200 → FetchResult with correct `filings.recent` structure
- Submissions 404 → non-retryable error
- fetch_document HTML → extracted text via html.parser
- fetch_document PDF content-type → extraction_status='pdf_skipped'
- Rate limit 429 → Retry-After honored by with_retry wrapper
- User-Agent header passed on both fetch and fetch_document

`tests/test_cik_map.py` (4 tests):
- All 10 SUPPORTED_TICKERS resolve to CIKs
- ticker_to_cik("AAPL") → "0000320193"
- cik_to_ticker("0000320193") → "AAPL"
- Unknown ticker → KeyError with informative message

`tests/test_filings_schema.py` (5 tests):
- ensure_filings_tables creates both tables (+ idempotent)
- upsert_filing INSERT → row exists with correct filing_id format
- upsert_filing UPDATE → row reflects new values
- upsert_filing_document INSERT → row FK to filings
- extraction_status CHECK constraint rejects invalid values

`tests/test_sec_normalize.py` (9 tests):
- normalize_submissions filters to [from_date, to_date] range
- normalize_submissions filters to 8-K/10-Q/10-K only (no Form 4, SC 13G)
- All returned filings have source_tier=1
- Empty window → empty list
- Exhibit 99.1 resolution: 8-K with Item 2.02 and wrapper primary → exhibit text extracted, not wrapper
- Exhibit 99.1 resolution: primary document with body >200 chars → primary text used, exhibit NOT fetched
- Non-relevant 8-K → is_rag_eligible=0, no filing_documents row
- 10-Q/10-K → is_rag_eligible=1, no filing_documents row (metadata only)
- extract_text_from_html on fixture → matches expected output

`tests/test_sec_index_builder.py` (5 tests):
- build_filing_records: 8-K with text → one L1 record, source_kind='filing'
- build_filing_records: long 8-K body → L1 + L2 records (body only, title excluded)
- build_filing_records: 10-Q metadata-only → L1 record, content_hash from title only
- filing_id format: corpus_item_id matches "sec:{cik}:{accession}"
- Cross-plane dedup guard: same corpus_item_id used for article + filing → AssertionError

**Existing test extensions:**

`tests/test_index_builder.py` (2 tests):
- build_filing_records guard: L1 count mismatch → AssertionError
- build_filing_incremental_records: new filing not in index_state → appears in delta

`tests/test_cli_index.py` (1 test):
- refresh-cik-map exits 0, writes CSV to expected path

**CLI extension:**

`catalyst_data/cli_index.py`:
- New subcommand: `refresh-cik-map` — calls `cik_map.refresh_cik_map()`, prints summary of tickers found/missing.

**Frozen DB invariant:** SHA-256 must remain `0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf`. All tests use `:memory:` or the dev DB. No test touches the frozen DB.

---

## 4. PR 3C2 — Pipeline Integration + Freshness

**Gate:** 3C1 must be reviewed and merged before 3C2 implementation begins.
Re-validate against 3C1's as-built APIs at start.

### 4.1 Execution Order

| Task | Files | Depends On |
|------|-------|------------|
| T9 — Checkpoint semantics | `catalyst_data/quality.py` (extend) | 3C1 merged |
| T10 — Pipeline `_fetch_cell` SEC path | `catalyst_data/update_pipeline.py` | T9 |
| T11 — Freshness | `catalyst_data/freshness.py` | — |
| T12 — CLI | `catalyst_data/cli_index.py` | T10, T11 |
| T13 — Pipeline tests | `tests/test_update_pipeline.py`, `tests/test_freshness.py` | T9–T12 |

### 4.2 T9 — Checkpoint Semantics

**Design decision from design spike Q2:** No `success_empty` status. No DDL migration.

For SEC, `_fetch_cell` writes `status='success'` when the submissions API returned 200
and normalization produced 0 filings in range (empty day). The cell is covered — it will
not be re-fetched. `compute_missing_cells` queries `WHERE status = 'success'` and correctly
excludes the covered cell. Freshness reports query `filings` directly for per-ticker
data; the checkpoint only tells us the cell was attempted.

**No changes to the `CHECK` constraint** on `source_checkpoints.status`. The existing
`('pending', 'success', 'failed', 'skipped')` set is sufficient.

### 4.3 T10 — Pipeline `_fetch_cell` SEC Path

**IMPORTANT — fetcher contract note:** `_fetch_cell` for `source="sec_filings"` must receive
the full SEC fetcher namespace (returned by `create_sec_fetcher`), not just a bare `fetch`
callable. The namespace must expose `fetch_document(url)` for exhibit resolution in
`resolve_filing_documents`. The existing `_fetch_cell` signature accepts a generic `fetch_fn`;
for SEC, the caller must pass the full namespace. Resolve this in the 3C2 implementation.

**Modify:** `catalyst_data/update_pipeline.py`

Add a source-specific branch in `_fetch_cell` when `source == "sec_filings"`:

1. Resolve CIK for the ticker via `cik_map.ticker_to_cik(ticker)`.
2. Fetch submissions via `await fetch_fn(ticker, "sec_submissions", date)`.
3. Call `normalize_submissions` with `from_date=date, to_date=date` to get filings in the date range.
4. For each filing where `is_rag_eligible=1` and form_type is 8-K:
   a. Call `resolve_filing_documents(fetcher, filing_dict)`.
   b. For each returned document dict: `upsert_filing_document(conn, ...)`.
   c. `upsert_filing(conn, ...)`.
5. For 10-Q/10-K: `upsert_filing(conn, ...)` only (no document fetch).
6. Store the raw submissions JSON in `raw_assets` (as above).
7. For each fetched document (primary_doc and every exhibit): write a separate `raw_assets`
   row for the raw HTML (as specified in §5 Bronze archive).
8. Set `filings.raw_asset_id` to the submissions JSON's asset_id.
   - `asset_id = compute_asset_id(ticker, fetched_at_date, "sec_submissions")`
   - `reference_date = fetched_at_date` (the date we fetched, NOT the trading date)
   - `metadata = {"cik": cik, "response_hash": SHA256(submissions_json)[:16]}`
7. Write `source_checkpoint` with `status='success'` and `retries` count.
8. Return `{ticker, date, source, status: "success", filings_count: N}`.

**Submissions per-run cache:** The submissions API returns ALL filings for a CIK —
not date-filtered. During `run_update_batch`, the same CIK's submissions may be needed
for multiple trading days. The pipeline module caches the submissions response per
(ticker, run_id) in memory for the duration of the run, avoiding re-fetching the
same 200KB JSON for each trading day.

### 4.4 T11 — Freshness

**Modify:** `catalyst_data/freshness.py`

Add `filings_freshness(conn) -> dict` returning per-ticker:
- `latest_filing_date`: MAX(`filed_at`) from `filings` for this ticker
- `latest_checked_date`: MAX(`date`) from `source_checkpoints` WHERE `source_type='sec_filings'` AND `status='success'`
- `filings_30d_count`: COUNT of filings in last 30 calendar days
- No STALE/FRESH binary judgment — SEC filings are sparse by nature. "No filing today" is correct behavior, not a failure.

Integrate into `freshness_report(conn)` output as a separate "SEC FILINGS" section with
per-ticker rows and a `current / stale_check / never_checked` status based on whether
`latest_checked_date` is within 1 business day of `latest_local_ohlcv_date`.

### 4.5 T12 — CLI

**Modify:** `catalyst_data/cli_index.py`

- `update-news --sources sec_filings` — existing CLI, add sec_filings to help text.
- `status --freshness` — now includes SEC filings section.

### 4.6 T13 — Pipeline Tests

`tests/test_update_pipeline.py` (3 new tests):
- `compute_missing_cells` includes `sec_filings` in source list → returns expected cells
- `_fetch_cell` SEC path with mocked submissions returning 0 filings in range → checkpoint written, counts=0
- `_fetch_cell` SEC path with mocked submissions returning 1 8-K + 1 exhibit → filing + document stored, counts=2

`tests/test_freshness.py` (2 new tests):
- `filings_freshness`: latest filing < watermark → shown correctly
- `filings_freshness`: empty filings table → "no filings" in report

---

## 5. Raw/Bronze Storage Identity

**SEC submissions JSON** stored in `raw_assets`:
- `asset_id = compute_asset_id(ticker, fetched_at_date, "sec_submissions")`
- `source_type = "sec_submissions"`
- `reference_date = fetched_at_date` (the date we fetched, NOT the trading date — the submissions API is not date-specific)
- `content_raw = zlib.compress(submissions_json_bytes)`
- `metadata = {"cik": cik, "response_hash": SHA256(submissions_json)[:16], "endpoints": ["sec_submissions"]}`

**SEC primary document and exhibit HTML** stored in `raw_assets`:
- For EACH fetched document (primary_doc AND every exhibit): create a raw_assets row
- `asset_id = compute_asset_id(ticker, filed_at, f"sec_primary_doc:{filing_id}:{doc_type}")`
  where `doc_type` is `primary_doc` or `exhibit_99_1`
- `source_type = "sec_primary_doc"`
- `content_raw = zlib.compress(html_bytes)`
- `metadata = {"url": url, "filing_id": filing_id, "document_type": doc_type, "content_type": content_type, "byte_size": byte_size}`

`filings.raw_asset_id` is set to the submissions JSON's asset_id (the parent Bronze artifact).

**Provenance chain:** `raw_assets` → `filings` (via `raw_asset_id` FK for submissions) → `filing_documents` (via `filing_id` FK) → `index_state` (via `corpus_item_id = filing_id`, independent polymorphic write by Step 4).

---

## 6. Expected File Summary

### Created (3C1 — 14 files)
| File | Purpose |
|------|---------|
| `catalyst_data/cik_map.py` | CIK↔ticker map module |
| `data/cik_map/cik_ticker_map.csv` | Static 10-ticker CIK map |
| `catalyst_data/connectors/sec.py` | SEC connector (fetch + fetch_document) |
| `catalyst_data/pipeline/sec_normalize.py` | Normalization + exhibit resolution + HTML extraction |
| `tests/test_sec_connector.py` | Connector tests (6) |
| `tests/test_cik_map.py` | CIK map tests (4) |
| `tests/test_filings_schema.py` | Schema tests (5) |
| `tests/test_sec_normalize.py` | Normalization tests (9) |
| `tests/test_sec_index_builder.py` | Index builder tests (5) |
| `tests/fixtures/sec_submissions_AAPL.json` | Redacted submissions fixture |
| `tests/fixtures/sec_8k_index_headers.htm` | Index headers fixture |
| `tests/fixtures/sec_8k_primary_wrapper.htm` | Wrapper 8-K HTML |
| `tests/fixtures/sec_8k_exhibit_99_1.htm` | Exhibit 99.1 HTML |
| `tests/fixtures/sec_8k_exhibit_99_1.txt` | Expected extraction output |

### Modified (3C1 — 5 files)
| File | Change |
|------|--------|
| `catalyst_data/retry.py` | Add `"sec"` retry policy |
| `catalyst_data/provider_limits.py` | Add `SEC` limits entry |
| `catalyst_data/source_mapping.py` | Add `"sec_filings"` mapping |
| `catalyst_data/storage/sqlite.py` | Add filings DDL + upsert helpers |
| `catalyst_data/index_builder.py` | Refactor + build_filing_records + guards |

### Modified (3C2 — 4 files)
| File | Change |
|------|--------|
| `catalyst_data/update_pipeline.py` | Add SEC path in `_fetch_cell` + per-run cache |
| `catalyst_data/freshness.py` | Add `filings_freshness` section |
| `catalyst_data/cli_index.py` | Add `refresh-cik-map` subcommand |
| `tests/test_update_pipeline.py` | Add 3 SEC pipeline tests |
| `tests/test_freshness.py` | Add 2 filing freshness tests |
| `tests/test_index_builder.py` | Add 2 filing guard tests |
| `tests/test_cli_index.py` | Add `refresh-cik-map` test |

---

## 7. Test Count Summary

| PR | New Tests | Extended Tests | Total |
|----|-----------|----------------|-------|
| 3C1 | 6 + 4 + 5 + 9 + 5 = 29 | 3 (existing) | **32** |
| 3C2 | 0 | 3 + 2 = 5 | **5** |
| **Combined** | **29** | **8** | **37** |

All tests fixture-only. Zero network in test suite. Frozen DB SHA unchanged.

---

## 8. Risk Register

| Risk | Severity | Mitigation |
|------|----------|------------|
| Exhibit 99.1 pointer not resolved → empty text indexed | **HIGH** | `resolve_filing_documents` implements <200-char wrapper detection + exhibit fetch. Tested with fixture. |
| SEC rate limit at scale | Low | 10-ticker universe. ~10 submissions calls + ~1-3 document fetches per day. Well within ≤5 req/s. |
| 8-K HTML extraction quality varies | Medium | Some 8-Ks attach PDFs. `extraction_status='pdf_skipped'`. Track skipped rate. Accept for now. |
| CIK↔ticker map drifts | Low | 10 large-cap tickers. CIK changes rare. `refresh-cik-map` regenerates from SEC source. |
| index_state FK confusion | None | `index_state` is explicitly polymorphic — designed for multi-source use. |
| Submissions JSON cached per-run | Low | 10 tickers × ~20KB compressed = 200KB memory. Acceptable. |

---

## 9. Small-Sample Live Validation (GATED)

A separate script `scripts/validate_sec_live.py` runs ONLY after Claude review and
explicit approval. Not part of 3C1 or 3C2 implementation.

**Scope:** 2 tickers (AAPL, NVDA), 3 recent trading days. Dry-run by default
(prints what would be stored, zero writes). `--write` flag required to commit to dev DB.

---

## 10. Guardrails (All Apply)

- ✅ data-core only — no `packages/app` or `packages/agents`
- ✅ Dev DB `data/catalyst_dev_ws4b.db` only
- ✅ Frozen DB `data/catalyst_eval_frozen_v2.db` read-only, SHA `0dfc81b154a9...` unchanged
- ✅ Additive DDL only (`CREATE TABLE IF NOT EXISTS`)
- ✅ No new dependencies — `httpx` + stdlib only
- ✅ No embeddings, no LanceDB, no model loads on Mac
- ✅ No network in tests (fixtures only)
- ✅ No commit — plan only
- ✅ No implementation code beyond DDL snippets

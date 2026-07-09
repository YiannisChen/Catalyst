# WS4B Step 3C-SEC — Design Spike: Architecture Decisions

**Status:** Design spike — NOT implementation
**Date:** 2026-07-01
**Branch:** `ws4b/article-level-data`
**Commit baseline:** `a41906b`
**References plan:** `docs/plans/2026-07-01-ws4b-step3c-sec.md`

---

## Summary Table

| # | Question | Recommendation | Impact |
|---|----------|---------------|--------|
| Q1 | Pipeline integration now vs later | **Split: 3C1 (connector+storage) → 3C2 (pipeline)** | Lower risk, testable independently |
| Q2 | Checkpoint for empty SEC days | **`status='success'` with `row_count=0` in notes; defer `success_empty`** | No DDL migration; backward compatible |
| Q3 | `sec_primary_doc` fetch API shape | **Add `fetch_document(url)` to connector return object** | Clean; no date pollution; minimal contract change |
| Q4 | HTML extraction dependency | **Stdlib `html.parser` + regex fallback; NO new dependency** | Zero risk; sufficient for 8-K |
| Q5 | 8-K item filtering strategy | **Option C: store all metadata, fetch docs only for filtered** | Provenance preserved; cost-minimal; expandable |
| Q6 | `filing_documents` row policy | **Row only when fetch attempted; `extraction_status='not_attempted'` for 10-Q/10-K** | Clean separation; no phantom rows |
| Q7 | Raw/Bronze storage identity | **Per CIK per `fetched_at` date; submissions dedup by response hash** | Avoids 200KB × N per backfill |
| Q8 | Filing ID + accession normalization | **`filing_id = "sec:{cik_padded}:{accession_dashed}"`; URL from int-cik path** | Matches SEC conventions; matches OpenBB pattern |
| Q9 | Index builder design | **`build_article_records()` + `build_filing_records()`; combine at CLI** | Clean; no schema pollution; Step 4-ready |
| Q10 | Freshness semantics for SEC | **Report latest filing date + latest checked date; no STALE/FRESH** | Sparse data semantics honored |

---

## Q1: Pipeline Integration — Now vs Later

### Options

**A. Step 3C monolithic:** CIK map + connector + DDL + normalize + index_builder + update_pipeline all in one.
**B. Split into 3C1 + 3C2:** 3C1 = connector + storage + normalize + index_builder. 3C2 = update_pipeline integration.

### Analysis

| Factor | Option A (Monolithic) | Option B (Split) |
|--------|----------------------|------------------|
| Complexity | High — 9 tasks with interleaved deps | Lower — 3C1 is 6 self-contained tasks |
| Risk | Pipeline changes touch freshness, checkpoints, CLI — tested together | Each half tested independently |
| Testability | Need integration tests mixing SEC + pipeline | 3C1 fully testable with fixtures; 3C2 adds pipeline integration tests |
| Review surface | Large PR, hard to review | Two smaller PRs, separately reviewable |
| Checkpoint semantics (Q2) | Must solve before 3C | Solved in 3C2 only |
| Rollback | Hard if pipeline assumptions break | 3C1 ships stable storage; 3C2 can iterate |

### Recommendation: **Option B — Split into 3C1 + 3C2**

**3C1 scope (connector + storage):**
- CIK map
- SEC connector (`sec_submissions` only — no primary doc in fetch contract)
- `filings` + `filing_documents` DDL
- `sec_normalize.py` (submissions → filing rows; 8-K HTML → text)
- Primary document fetch via helper function (see Q3), NOT through connector
- `build_filing_records()` for index builder
- `refresh-cik-map` CLI
- Full test suite (fixture-only, zero network)

**3C2 scope (pipeline integration, after 3C1 review):**
- `compute_missing_cells` extension for `sec_filings`
- `_fetch_cell` SEC path
- `run_update_batch` `--sources sec_filings`
- Checkpoint semantics (see Q2)
- Submissions per-run cache
- `freshness.py` extension
- CLI `update-news --sources sec_filings`
- Pipeline integration tests

**Rationale:** The connector/storage layer is well-understood and mirrors Polygon's pattern. The pipeline integration has genuine open questions (checkpoint semantics, per-run caching, freshness reporting for sparse data). Separating them avoids blocking the stable storage work on pipeline design decisions.

---

## Q2: Checkpoint Semantics for Empty SEC Days

### Current State

`source_checkpoints` has a hard `CHECK` constraint:
```sql
status TEXT NOT NULL CHECK (status IN ('pending', 'success', 'failed', 'skipped'))
```
in `quality.py:_QUALITY_TABLES_SQL`. This is baked into the DDL via `ensure_ingestion_quality_tables(conn)`.

`compute_missing_cells` queries `WHERE status = 'success'` to find covered cells. A `'success_empty'` status would make those cells show as "missing" (not in the success set) → they'd be re-fetched every run.

`write_source_checkpoint` has no validation of the `status` parameter — it trusts the caller to pass a valid value.

**Adding `'success_empty'` requires a DDL migration:**
1. The `CHECK` constraint is inline in the `CREATE TABLE`. SQLite does not support `ALTER TABLE ... ALTER CONSTRAINT`.
2. Options: (a) drop and recreate the table, (b) create new table + migrate data, (c) don't add the status value.
3. The `_STATUS_VALUES` tuple is used in the DDL string via f-string interpolation at table creation time — it's not validated at write time.

### Options

| Option | Migration Required | Backward Compatible | Empty-Day Semantics |
|--------|-------------------|---------------------|---------------------|
| `'success_empty'` (new status) | **Yes** — DDL migration | Breaks existing CHECK constraint | Explicit, queryable |
| `'success'` with `row_count=0` in notes | **No** | Fully compatible | Requires parsing `notes` JSON |
| `'skipped'` for empty days | **No** | Fully compatible | `skipped` already means "no articles" for news |
| Add `result_count` column | **Yes** — additive DDL | Compatible (new column) | Clean, structured |

### Recommendation: **`status='success'` with `row_count=0` in notes for Step 3C2; add `result_count` column in a later Step 3C2 refinement**

**Rationale:**
1. `'skipped'` is semantically wrong — SEC being empty is success (we checked, there's nothing), not a skip (we didn't bother).
2. `'success_empty'` requires DDL migration, which is disproportionate for Step 3C2.
3. The `notes` field on `ingestion_runs` already carries JSON metadata — the per-cell `source_checkpoints` doesn't have a `notes`/`metadata` column but we can encode it in the `run_id`'s run-level `notes` or simply track via `filings` row counts.
4. **Practical solution:** `compute_missing_cells` already queries `WHERE status = 'success'`. For SEC, `'success'` with 0 filings is indistinguishable from `'success'` with 3 filings at the checkpoint level. But `filings` rows are queryable. If `_fetch_cell` writes `status='success'` when the submissions API returned 200 and we normalized 0 filings in range, the cell won't be re-fetched. Next run: `SELECT COUNT(*) FROM filings WHERE ticker=? AND filed_at=?` tells us we checked and found 0. This is correct behavior — we don't need a distinct status.

**Implementation:** In `_fetch_cell` for SEC: if submissions API returns 200 and `normalize_submissions` returns `[]`, write `status='success'` with `retries=0`. The cell is covered. Freshness reports query `filings` directly for per-ticker data, not checkpoints.

---

## Q3: `sec_primary_doc` Fetch API Shape

### Options

| Option | Pros | Cons |
|--------|------|------|
| A. Pass URL as `date` in `fetch(ticker, endpoint, date)` | Minimal code change | Semantic pollution; `date` means "date" everywhere else |
| B. Add `fetch_document(url)` to returned closure | Clean API; separate concern | Unusual for connector contract (currently `fetch` only) |
| C. Separate `create_sec_document_fetcher()` | Clean separation | Two factories for one provider |
| D. Put document fetch in `sec_normalize.py` directly | Simplest; no connector indirection | Bypasses rate limiter, retry, User-Agent consistency |

### Recommendation: **Option B — Add `fetch_document(url)` to the returned closure**

The connector factory `create_sec_fetcher(user_agent, limiter, client)` returns an object with two callables:

```python
fetcher = create_sec_fetcher(user_agent="Catalyst/1.0 (contact@example.com)")
submissions = await fetcher.fetch("AAPL", "sec_submissions", "2026-01-15")
doc_text = await fetcher.fetch_document("https://www.sec.gov/Archives/edgar/data/320193/...")
```

**Rationale:**
1. `fetch_document` shares the same `user_agent`, `limiter`, `client` — no duplication.
2. The `create_*_fetcher` factory pattern already returns a closure; returning a tuple or namespace object is a natural extension.
3. `fetch_document` is provider-specific — Polygon doesn't need it, yfinance doesn't need it. Not every connector must expose it.
4. Normalization code calls `fetcher.fetch_document(url)` directly, keeping the fetch orchestration in the connector layer where rate limiting and retry live.
5. SEC fetching has two distinct operations: "get all filings for a CIK" (submissions) and "get text for a specific filing" (primary doc). They map naturally to two methods.

**Impact on connector contract:** The current contract is `async fetch(ticker, endpoint, date) -> FetchResult`. Option B extends it per-connector — `fetch_document` is an optional method on the returned object, not a change to the base contract. Callers check `hasattr(fetcher, 'fetch_document')`.

---

## Q4: HTML Extraction Dependency

### Current State

- **Catalyst:** Zero HTML parsing dependencies. No `html2text`, no `bs4`, no `lxml`.
- **OpenBB SEC:** Uses `bs4` (BeautifulSoup) + `lxml` via `html2markdown.py` utility. This is a heavy dependency (bs4 + lxml native binary).
- **8-K primary documents:** SEC 8-K filings are clean HTML — no complex JavaScript, no deep nesting. The structure is typically `<html><body>...text with occasional tables...</body></html>`.

### Options

| Option | Dep Size | Quality | Risk |
|--------|----------|---------|------|
| A. Stdlib `html.parser` + `html.unescape` + regex tag strip | 0 new deps | Adequate for 8-K | Some inline tables may be garbled |
| B. Optional `html2text` (install if available) | ~50KB pure Python | Good | Conditional behavior — tests need both paths |
| C. Required `html2text` | ~50KB pure Python | Good | New dependency — but tiny and pure Python |
| D. Required `bs4` + `lxml` (like OpenBB) | ~10MB | Excellent | Heavy; lxml has native compilation risk; overkill for 8-K |

### Recommendation: **Option A — Stdlib only. NO new dependency.**

**Implementation:**
```python
from html.parser import HTMLParser
from html import unescape
import re

class _TextExtractor(HTMLParser):
    """Extract visible text from HTML, stripping tags and scripts."""
    def __init__(self):
        super().__init__()
        self._text: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self._skip = False
        # Add newline after block-level elements
        if tag in ('p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self._text.append('\n')

    def handle_data(self, data):
        if not self._skip:
            self._text.append(data)

    def get_text(self) -> str:
        raw = ''.join(self._text)
        # Collapse whitespace
        text = re.sub(r'\n{3,}', '\n\n', raw)
        text = re.sub(r'[ \t]+', ' ', text)
        return unescape(text).strip()


def extract_text_from_html(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    return extractor.get_text()
```

**Rationale:**
1. 8-K primary documents are prosaic — Item 1.01 text, Item 2.02 numbers. We need plain text for embedding, not structured extraction.
2. The cost of `bs4` + `lxml` is not justified for our use case. OpenBB needs it for general-purpose SEC HTML parsing (13F tables, XBRL extraction). We don't.
3. `html2text` adds Markdown conversion we don't need (embedding models handle plain text fine). The dependency is small but the functionality is excess.
4. If extraction quality proves insufficient during live validation, `html2text` can be added later as an optional upgrade without changing the API — just swap the extractor.

---

## Q5: 8-K Item Filtering Strategy

### Analysis of Excluded Items

| Item | Name | Attribution Relevance | Risk if Excluded |
|------|------|----------------------|-----------------|
| **1.02** | Termination of Material Definitive Agreement | Medium — customer loss, partnership end | Missed revenue-impacting contract termination |
| **3.01** | Delisting Notice | **HIGH** — stock faces delisting | Rare but catastrophic event |
| **3.02** | Unregistered Equity Sales | Medium — dilution signal | Missed dilution events |
| **4.01** | Auditor Changes | Low | Rarely market-moving alone |
| **4.02** | Non-Reliance / Restatement | **HIGH** — financials unreliable | Rare but devastating; the canonical "accounting fraud" signal |
| **5.01** | Change in Control | **HIGH** — M&A, takeover | Missed M&A signal |
| **5.03** | Charter/Bylaw Changes | Low | Governance only |
| **5.07** | Shareholder Vote Results | Low | Routine |

### Options

| Option | Storage | Fetch Cost | Risk |
|--------|---------|------------|------|
| A. Fetch/store only filtered 8-Ks | Minimal | Low | IRREVERSIBLE data loss for excluded items |
| B. Store all metadata, fetch docs only for filtered | Complete metadata | Low | Excluded items: no text for embedding, but metadata preserved |
| C. Store all metadata, fetch all docs, index only filtered | Complete | Higher | All text available; index is filtered |
| D. Store/fetch/index everything | Complete | Highest | Noisy — many 8-K items are boilerplate/compliance |

### Recommendation: **Option C — Store all metadata, fetch docs only for filtered items**

**Filter set (9 items, same as plan):** `1.01, 1.03, 2.02, 2.05, 2.06, 5.02, 7.01, 8.01, 9.01`

**Behavior:**
- ALL 8-Ks → `filings` row (metadata preserved: form_type, items, filed_at, period, url, accession_number).
- Filtered 8-Ks → `filing_documents` row with extracted text, `extraction_status='success'`.
- Non-filtered 8-Ks → `is_rag_eligible=0`, NO `filing_documents` row, `extraction_status` implied as `'not_attempted'` (tracked in `filings` metadata).
- 10-Q/10-K → `filings` row with `is_rag_eligible=1` (metadata is valuable), NO `filing_documents` row (text extraction deferred).

**Expanding the filter:** Later, adding an item (e.g., `4.02` for restatements) means: update the filter set, re-run normalize on existing filings, fetch docs for newly-included items. No re-ingestion needed — submissions metadata is already stored.

**Why not Option D (fetch everything):** Many 8-K items are boilerplate compliance filings (3.03 rights modification, 5.07 shareholder votes) that add noise to the index without attribution value. The fetch cost is real: 10 tickers × ~5-10 8-Ks/year each with non-relevant items = 50-100 unnecessary document fetches/year.

---

## Q6: `filing_documents` Row Policy

### Options

| Option | 10-Q/10-K in filing_documents | Semantics |
|--------|------------------------------|-----------|
| A. Row for every filing, `pdf_skipped` for non-8-K | Rows with NULL text | Phantom rows — no actual document fetch happened |
| B. Row only when fetch was attempted | No row for 10-Q/10-K | Clean — row existence means "we fetched this" |
| C. No filing_documents table at all; text in filings.text column | N/A | Simpler schema, but loses per-document tracking |

### Recommendation: **Option B — Row only when fetch was attempted**

**Schema (final):**
```sql
CREATE TABLE IF NOT EXISTS filing_documents (
    filing_id         TEXT NOT NULL,
    document_url      TEXT NOT NULL,
    document_type     TEXT NOT NULL DEFAULT 'primary_doc',
    text              TEXT,
    char_len          INTEGER,
    content_type      TEXT,
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

- `document_type` = `'primary_doc'` for the main 8-K HTML. Future: `'exhibit_99_1'` for earnings release exhibits, `'complete_submission_txt'` for the full submission text file.
- `extraction_status` values: `'success'` (text extracted), `'empty'` (HTML parsed but no text content), `'pdf_skipped'` (content-type is PDF, not attempted), `'fetch_failed'` (network error), `'timeout'` (fetch timed out).
- 10-Q/10-K: NO `filing_documents` row. The `filings` row alone captures metadata. The absence of a document row is the signal: "text not yet extracted."
- When 10-Q/10-K text extraction is added later: upsert a `filing_documents` row with the extracted text. No schema change needed.

---

## Q7: Raw/Bronze Storage Identity

### Problem

`compute_asset_id(ticker, date, "sec_submissions")` would store the same 200KB submissions JSON once per trading day. For a 30-day backfill of 10 tickers: 10 × 30 × 200KB = 60MB of duplicate data (compressed to ~20KB each = still ~6MB wasted).

### Options

| Option | asset_id | Dedup | Granularity |
|--------|----------|-------|-------------|
| A. `compute_asset_id(ticker, date, "sec_submissions")` | Per ticker per date | None — re-fetched per day | Daily |
| B. `compute_asset_id(ticker, fetched_at_date, "sec_submissions")` | Per ticker per fetch day | Re-fetched daily but not per-trading-day | Daily fetch |
| C. Per CIK + response content hash | Per CIK + SHA | True dedup | Content-based |

### Recommendation: **Per CIK per `fetched_at` date, with response hash in metadata**

```python
fetch_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
asset_id = compute_asset_id(ticker, fetch_date, "sec_submissions")
```

**Storage:**
```python
upsert_raw_asset(
    conn,
    asset_id=asset_id,
    ticker=ticker,
    source_type="sec_submissions",
    reference_date=fetch_date,  # When we fetched, not the trading date
    content_raw=zlib.compress(submissions_json),
    http_status=200,
    metadata={
        "cik": cik,
        "response_hash": sha256(submissions_json).hexdigest(),
        "endpoints": ["sec_submissions"],
        "trading_date_range": {"from": from_date, "to": to_date},
    },
)
```

**Rationale:**
1. The submissions API returns ALL filings for a CIK — it's not date-scoped. Storing per `reference_date` (trading date) implies it's date-specific data, which is misleading.
2. `reference_date` = `fetched_at` date is the honest representation: "we fetched this on 2026-07-01."
3. `response_hash` in metadata enables the pipeline to check: "did the response change since last fetch?" before re-storing.
4. For backfill: the pipeline fetches submissions once per CIK per run, not once per trading day. The `from_date`/`to_date` in metadata records what window was queried.

---

## Q8: Filing ID + Accession Normalization

### SEC Accession Number Conventions

Accession numbers in the submissions JSON have dashes: `0000320193-24-000070`.
- The 10-digit CIK prefix (`0000320193`) is the padded CIK.
- The 2-digit year (`24`).
- The 6-digit sequential number (`000070`).

For URLs, SEC uses:
```
https://www.sec.gov/Archives/edgar/data/{int_cik}/{accession_no_dash}/{primary_document}
```
where `{int_cik}` = CIK without leading zeros (e.g., `320193`), and `{accession_no_dash}` = accession with dashes removed (e.g., `000032019324000070`).

### Recommendation

**`filing_id` format:** `"sec:{cik_padded}:{accession_dashed}"`
- Example: `"sec:0000320193:0000320193-24-000070"`
- Padded CIK for consistency (no ambiguity about leading zeros).
- Dashed accession for readability and SEC convention matching.

**URL fields in `filings`:**
| Column | Value | Example |
|--------|-------|---------|
| `url` | Filing detail page (`-index.htm`) | `https://www.sec.gov/Archives/edgar/data/320193/000032019324000070/0000320193-24-000070-index.htm` |
| `primary_document` | Filename only (from submissions JSON) | `ea0204059-08k_items101_901.htm` |

**Full primary document URL** is computed when needed: `{base_url}/{accession_no_dash}/{primary_document}`.

This exactly matches OpenBB's URL construction pattern in `company_filings.py` transform_data:
```python
base_url = f"https://www.sec.gov/Archives/edgar/data/{str(int(query.cik))}/"
primaryDocumentUrl = base_url + accessionNumber.replace("-", "") + "/" + primaryDocument
```

---

## Q9: Index Builder Design

### Current State

`build_index_records(conn)` builds records from `articles` table only. Record dicts use `article_id`, `parent_article_id` fields. The `index_state` table is polymorphic (`corpus_item_id`, `source_kind`).

### Options

| Option | Record Schema | Code Duplication | Step 4 Clarity |
|--------|--------------|-----------------|----------------|
| A. `build_index_records(conn, filings=False)` with flag | Mixed schema — `article_id` OR `filing_id` | Low | Confusing for Step 4 embed |
| B. `build_article_records()` + `build_filing_records()` separate | Clean per-type schema | Some shared helpers | Clear — Step 4 calls both |
| C. Unified record schema with `corpus_item_id`, `source_kind` | Unified | Major refactor of article records | Cleanest but risky |

### Recommendation: **Option B — `build_article_records()` + `build_filing_records()`**

**Implementation:**
1. Rename existing `build_index_records` → `build_article_records` (internal refactor, no test changes needed beyond renaming).
2. Add `build_filing_records(conn, *, min_l2_chars=800) -> list[dict]`:
   - Queries `filings JOIN filing_documents` WHERE `is_rag_eligible=1` AND `extraction_status='success'`.
   - Record shape per filing:
     ```python
     {
         "chunk_id": f"{filing_id}::l1",
         "chunk_level": "l1",
         "corpus_item_id": filing_id,
         "source_kind": "filing",
         "content_hash": compute_content_hash(title, extracted_text),
         "content_text": f"{title}\n{extracted_text}",
         "provider": "sec",
         "source_type": "sec_filing",
         "tickers": [ticker],
         "source_tier": 1,
         "filed_at": filed_at,
         "form_type": form_type,
         "accession_number": accession_number,
         "filing_url": url,
     }
     ```
   - L2: if `len(extracted_text) >= min_l2_chars`, sentence-split from extracted text. Title NOT included in L2 bodies.
3. Keep `build_index_records` as a thin wrapper that calls both (backward compatibility for CLI dry-run).
4. `build_incremental_records` extended similarly: `build_article_incremental()` + `build_filing_incremental()`.

**Why not Option C (unified schema):**
- Articles carry `article_id`, `publisher_name`, `article_url`, `image_url` — fields meaningless for filings.
- Filings carry `filing_id`, `form_type`, `filed_at`, `cik` — fields meaningless for articles.
- Forcing a unified schema adds null columns and confusion for Step 4 embed code.
- The polymorphic `index_state` already handles the unification at the storage layer. The build layer should preserve type-specific semantics.

---

## Q10: Freshness Semantics for SEC

### Problem

SEC filings are sparse — most trading days have zero filings. Reporting "STALE" for a ticker that hasn't filed in 3 days is misleading — the SEC pipeline is working correctly, there's just nothing to ingest.

### Options

| Option | Report Shape | Semantics |
|--------|-------------|-----------|
| A. STALE/FRESH/NO_DATA per ticker | Binary judgment | Misleading — "STALE" implies failure |
| B. Latest filing date + latest checked date | Two dates | Accurate — tells you what's available and when you last checked |
| C. Per-ticker filing count + last checked watermark | Count + date | Most informative |

### Recommendation: **Option B — Report latest filing date + latest checked date**

**`status --freshness` SEC section:**
```
SEC FILINGS FRESHNESS
  Ticker  Latest Filing  Last Checked  Filings (30d)  Status
  AAPL    2026-06-28     2026-06-30    3              current
  NVDA    2026-06-27     2026-06-30    2              current
  UNH     2026-04-17     2026-06-30    1              sparse
```

**Implementation:**
- `filings_freshness(conn) -> dict` returns: `{ticker: {latest_filing_date, latest_checked_date, filings_30d_count}}`.
- `latest_checked_date` = MAX date from `source_checkpoints` WHERE `source_type='sec_filings'` AND `status='success'`. If no checkpoint exists, report "never checked."
- No STALE/FRESH judgment for SEC — the semantics don't fit. The "Status" column is `current` if checked within 1 business day of latest OHLCV, `stale_check` if last check is older, `never_checked` if no checkpoints.
- This is fundamentally different from `news_freshness` where STALE means "there could be news we haven't fetched." For SEC, "no filing on this date" is the correct answer, not a failure.

**Impact on `freshness_report(conn)`:** The existing report outputs news freshness. Add a separate `filings` section with its own semantics. Do not shoehorn filings into the news STALE/FRESH framework.

---

## Amendments to the Step 3C Plan

Based on these decisions, the following amendments are needed to `docs/plans/2026-07-01-ws4b-step3c-sec.md`:

1. **Execution order:** Split Task 8 (pipeline integration) into a separate Step 3C2. Tasks 1–7, 9 become Step 3C1.
2. **Task 3 (SEC connector):** The factory `create_sec_fetcher()` returns a namespace with both `fetch()` and `fetch_document()` methods. Document URL is NOT passed through the `date` parameter.
3. **Task 5 (DDL):** `filing_documents` `extraction_status` CHECK constraint uses `('success', 'empty', 'pdf_skipped', 'fetch_failed', 'timeout')`. 10-Q/10-K do NOT get `filing_documents` rows. No `'not_attempted'` status needed — absence of row is the signal.
4. **Task 6 (normalization):** HTML extraction uses stdlib `html.parser`, NOT `html2text`. Add the `_TextExtractor` class described in Q4.
5. **Task 6 (8-K filter):** ALL 8-K metadata stored in `filings`. `filing_documents` row created ONLY for filtered items. Non-filtered items get `is_rag_eligible=0`.
6. **Task 7 (index builder):** Split into `build_article_records()` + `build_filing_records()`. Keep `build_index_records` as wrapper.
7. **Task 8 (pipeline):** Deferred to Step 3C2. Deleted from Step 3C1 plan.
8. **Task 5 (Q2):** No `success_empty` status. Empty days → `status='success'`. No DDL migration.
9. **Task 4 (Q7):** Raw asset identity: `compute_asset_id(ticker, fetched_at_date, "sec_submissions")`. `reference_date` = fetch date, not trading date.
10. **Task 5 (Q8):** `filing_id = "sec:{cik_padded}:{accession_dashed}"`. URL construction follows OpenBB pattern.
11. **Task 7 (Q10):** Freshness section reports latest filing date + latest checked date. No STALE/FRESH for SEC.

---

## Verification

- ✅ **Secret grep:** Document contains zero API keys, tokens, or secrets.
- ✅ **No DB writes:** Research only — zero database interaction.
- ✅ **No packages/app or packages/agents changes:** Read-only inspection.
- ✅ **Git diff:** Only this new research document.

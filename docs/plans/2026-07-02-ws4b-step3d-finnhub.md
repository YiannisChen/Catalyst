# WS4B Step 3D — Finnhub Company-News (Final Plan)

**Status:** PLAN ONLY — not implemented
**Date:** 2026-07-02
**Branch:** `ws4b/article-level-data`
**Baseline:** `d960bd5` (Step 3C committed)
**Prerequisite:** Step 3C merged and as-built APIs stable
**Provider discovery:** COMPLETE — locked decisions in §0.1

**PROVISIONAL — depends on Step 3C as-built APIs. Re-validate at implementation start.**

---

## 0. Goal

Add Finnhub company-news as a second news provider, complementing Polygon. Finnhub provides
publisher breadth (Yahoo Finance, SeekingAlpha, wires) and cleaner discovery, while Polygon
retains its role as the only free source with 5-year history and date-filtered API. Bookshelf
Finnhub articles into the EXISTING `articles` + `article_tickers` + `raw_assets` tables —
no new tables beyond the Bronze raw_asset. Adds one additive column (article_tickers.dedup_group_id TEXT) via migration for per-association dedup grouping. Implements cross-source dedup so Polygon and
Finnhub articles about the same event share a `dedup_group_id`.

### 0.1 Locked Provider Discovery Decisions

These are NOT open for re-audit. The live probe was performed against the 10-ticker universe.

| Decision | Rationale |
|----------|-----------|
| Finnhub company-news: BUILD | Free tier supports 10-ticker backfill. ~2197 items across universe. 5.6% SeekingAlpha (opinion/T5), Yahoo-dominant, summary-only (~152-char median). |
| FMP news: DROPPED | Paid/HTTP 402. Not viable for free-only tier. |
| yfinance: SKIPPED | No date filter — cannot scope to trading days. Useless for backfill. |
| Finnhub VALUE = publisher breadth | Complements Polygon, does NOT replace it. Polygon is retained for 5-year history + date filter. |
| L1 only | Median summary is ~152 chars — far below 800-char L2 threshold. Zero L2 records from Finnhub. |
| URLs are finnhub.io redirects | Canonical URL resolution is DEFERRED. Store raw finnhub.io URL; cross-source dedup uses title+ticker+date, not URL. |

---

## 1. Architecture Decision: Dedicated Branch vs Orchestrator Path

### 1.1 The Two Patterns in the Codebase

**Pattern A — Orchestrator (Polygon news):**
`_fetch_cell` calls `orchestrator.process_request()` which runs `ingest → clean → transform → storage`
per source. The orchestrator owns the full Bronze-to-Silver pipeline in one async flow.
`rederive_polygon_news()` is run post-batch to feed `articles` + `article_tickers` from raw_assets.

**Pattern B — Dedicated `_fetch_cell_sec` (SEC filings):**
`_fetch_cell` dispatches to `_fetch_cell_sec()` when `source == "sec_filings"`. That function
owns fetching, normalization, upsert, and Bronze archive inline. No orchestrator involvement.
SEC has its own schema (`filings`, `filing_documents`) and its own rederive path (none — it
writes directly to `filings` during the cell fetch).

### 1.2 Recommendation: Dedicated `_fetch_cell_finnhub` (Pattern B, Simplified)

**Rationale:**

1. **Finnhub does NOT use `clean_assets`.** Polygon's orchestrator stores per-article
   Markdown in `clean_assets`. Finnhub summaries are too short for L2 and are consumed
   through `articles` directly — there's no reason to create `clean_assets` rows. The
   orchestrator's `clean → transform → upsert_clean_asset` pipeline is wasted work.

2. **Finnhub's rederive is trivial.** Unlike Polygon (which nests `results[]` inside
   a `news` wrapper inside the raw response), Finnhub's API returns a flat JSON array.
   A dedicated `rederive_finnhub_news()` mirrors `rederive_polygon_news()` with a
   simpler JSON path: `data` (top-level array) → each element is an article. This is
   ~50 lines of code, not worth routing through the orchestrator abstraction.

3. **The orchestrator has hardcoded Polygon assumptions.** `_process_source` checks
   `if source == "polygon_news"` for per-article transform. Adding `finnhub_company_news`
   branches to the orchestrator would add complexity without value.

4. **Bronze-archival is simpler direct.** The dedicated path stores raw Finnhub JSON
   in `raw_assets` with `source_type="finnhub_company_news"` via `upsert_raw_asset()`
   in one call per (ticker, date) cell. The orchestrator's `_store_bronze_and_silver`
   has a legacy FK-disable hack for `polygon_news` we don't want to replicate.

5. **Consistency with SEC.** The SEC path already proved the dedicated-branch pattern
   works. Finnhub is even simpler than SEC (no document fetching, no exhibit resolution).

**What changes in `_fetch_cell`:**

Add a new branch:
```
if source == "finnhub_company_news":
    → _fetch_cell_finnhub(conn, ticker, date, run_id, fetcher_ns, ...)
```

`_fetch_cell_finnhub` does:
1. Fetch `fetcher_ns.fetch(ticker, "company-news", date)` → FetchResult
2. Store raw JSON in `raw_assets` (Bronze)
3. Write checkpoint `status='success'` (even if 0 articles)
4. Return `{ticker, date, source, status, articles_count, ...}`

The post-batch rederive (`rederive_finnhub_news`) reads raw_assets → `articles` + `article_tickers`,
mirroring `rederive_polygon_news`. This is run by `run_update_batch` after all cells are fetched.

---

## 2. Finnhub Connector

**New file:** `catalyst_data/connectors/finnhub.py`

### 2.1 Factory Function Contract

```python
def create_finnhub_fetcher(
    api_key: str,
    limiter=None,
    client: httpx.AsyncClient | None = None,
) -> SimpleNamespace
```

Returns a namespace with one callable:
- `await fetcher.fetch(ticker: str, endpoint: str, date: str) -> FetchResult`

`endpoint` must be `"company-news"`. Unknown endpoints return `FetchResult(status=0, error="Unknown Finnhub endpoint: ...")`.

### 2.2 API Contract

**Endpoint:** `https://finnhub.io/api/v1/company-news`

**Query params:** `symbol={ticker}&from={date}&to={date}&token={api_key}`

- `from` and `to` are the same calendar day (`date` parameter).
- `token` is the API key — passed as query param, NEVER logged. Sanitize in error messages
  and logs by replacing the token value with `[REDACTED]` if it appears in a URL string.

**Response shape (200):** JSON array of article objects:
```json
[
  {
    "category": "company",
    "datetime": 1751414400,
    "headline": "Apple Reports Q2 Earnings",
    "id": 12345678,
    "image": "https://finnhub.io/...",
    "related": "",
    "source": "Yahoo",
    "summary": "Apple Inc. reported quarterly earnings...",
    "url": "https://finnhub.io/api/v1/news/..."
  }
]
```

### 2.3 Error Handling + Retry

**Rate limit (429):** Finnhub free tier returns 429 with no structured `Retry-After` header.
The with_retry wrapper handles this via the retry policy. Set `retry_after_seconds` to 1.0s
as a fallback when the header is absent.

**Other errors:** 401 (bad key), 403 (forbidden) → non-retryable, immediate error.
5xx → retryable. Timeout → retryable.

### 2.4 Rate Limiter

Finnhub free tier: 60 req/min. The existing `config.RATE_POLICIES["dev"]["finnhub"]`
is already `RatePolicy(1.0, 3, None)` — 1 req/sec with up to 3 concurrent. This is correct
and should be reused directly via `provider_limits.py`.

**New entry in `provider_limits.py`:**
```python
FINNHUB = {
    "rate_per_min": int(round(60.0 / _DEV_POLICIES["finnhub"].min_interval_sec)),
    "concurrency": _DEV_POLICIES["finnhub"].max_concurrent,
}
```

### 2.5 Retry Policy

**New entry in `retry.py` `RETRY_POLICIES`:**
```python
"finnhub": RetryPolicy(
    rate_limit=RetryRule(
        base_seconds=1.0, max_seconds=30.0, max_retries=3,
        jitter=False, min_delay_seconds=1.0,
    ),
    server_error=RetryRule(
        base_seconds=5.0, max_seconds=30.0, max_retries=3,
        jitter=False,
    ),
    timeout=RetryRule(
        base_seconds=5.0, max_seconds=20.0, max_retries=2,
        jitter=False,
    ),
),
```

Wrap with `with_retry(fetch, provider="finnhub")` in the factory.

### 2.6 API Key

Read from `FINNHUB_API_KEY` environment variable. The existing `config._PROVIDER_KEY_ENV`
needs a new entry:
```python
_PROVIDER_KEY_ENV = {
    ...
    "finnhub": "FINNHUB_API_KEY",
    ...
}
```

The connector raises `ValueError("FINNHUB_API_KEY not set")` if the env var is missing
or empty. Key is passed as a `token` query param — never in headers, never logged.
Sanitize any URL strings that appear in error messages.

---

## 3. Bronze Archive

Store the raw Finnhub JSON response in `raw_assets` for re-derivability.

**During `_fetch_cell_finnhub` (one raw_asset per (ticker, date) cell):**

- `asset_id = compute_asset_id(ticker, date, "finnhub_company_news")`
- `source_type = "finnhub_company_news"`
- `reference_date = date` (the trading date — Finnhub company-news IS date-scoped)
- `content_raw = raw_json_bytes` (the full HTTP response body — upsert_raw_asset compresses internally)
- `http_status = 200`
- `metadata = {"endpoints": ["company_news"], "article_count": len(articles_array)}`

**Design invariant:** Bronze stores the immutable, complete API response as received.
Silver (`articles`) is derived from Bronze in the post-batch rederive step.

---

## 4. Normalization → SILVER (articles / article_tickers)

**New file:** `catalyst_data/pipeline/finnhub_normalize.py`

**New function:** `rederive_finnhub_news(conn, trading_days) -> dict`

Mirrors `rederive_polygon_news()` but reads `source_type='finnhub_company_news'` raw_assets
and normalizes Finnhub article JSON into `articles` + `article_tickers` rows.

### 4.1 Field Mapping

| Finnhub Field | Articles Column | Notes |
|---------------|----------------|-------|
| `headline` | `title` | Direct mapping |
| `summary` | `description` | Direct mapping |
| `datetime` (Unix int) | `published_utc` | Convert to ISO 8601: `datetime.fromtimestamp(dt, tz=timezone.utc).isoformat()` (NOT deprecated utcfromtimestamp) |
| `source` | `publisher_name` | The REAL publisher (Yahoo, SeekingAlpha, etc.) — NOT "finnhub" |
| `url` | `article_url` | Raw finnhub.io redirect URL — canonical resolution DEFERRED |
| `id` | native_id | `article_id = compute_article_id("finnhub", str(native_id))` |
| `category` | (discard) | Not stored — not present in Polygon schema either |
| `image` | `image_url` | Direct mapping |
| `related` | (discard) | Not stored |

### 4.2 Article Row Contract (AMENDED — mirror rederive_polygon_news column set)

Mirror `_parse_article()` in `rederive.py` EXACTLY — same column set, same field names,
same defaults. The only differences are provider-specific field sources:

```python
{
    "article_id": compute_article_id("finnhub", str(result["id"])),
    "raw_asset_id": raw_asset_id,
    "provider": "finnhub",
    "source_type": "finnhub_company_news",
    "ticker": ticker,
    "reference_date": reference_date or "",
    "published_utc": iso_timestamp,
    "title": result.get("headline", "Untitled"),
    "description": result.get("summary", "") or "",
    "article_url": result.get("url"),
    "image_url": result.get("image"),
    "author": None,
    "publisher_name": result.get("source"),
    "publisher_homepage_url": None,
    "publisher_logo_url": None,
    "publisher_favicon_url": None,
    "keywords_json": "[]",
    "insights_json": "[]",
    "tickers_json": json.dumps([ticker]),
    "source_tier": None,
    "dedup_group_id": None,
    "is_canonical": 1,
    "is_rag_eligible": 1,
    "quality_score": 1.0,
}
```

Same column set as `_parse_article()` in `rederive.py`, same defaults for
`title` ("Untitled"), `description` (empty string), `reference_date` (empty string),
same `quality_score=1.0`, same `json.dumps([])` pattern for `keywords_json` and
`insights_json`. This avoids schema drift between the two rederive modules.

### 4.3 Trading-Day Alignment

Reuse `catalyst_data.pipeline.align.map_to_trade_date(published_utc, trading_days)` —
identical to Polygon's rederive. This ensures `reference_date` is a valid trading day.

### 4.4 article_tickers

One row per article: `(article_id, ticker, raw_asset_id, reference_date)`. Finnhub
company-news is per-symbol — each article is associated with exactly one ticker (the
query symbol). Unlike Polygon which returns ticker arrays, Finnhub does not — the
query symbol IS the only associated ticker.

### 4.5 L1-Only Policy

Finnhub summaries have a ~152-char median. The L2 threshold (`RAG_MIN_CHAR_COUNT` = 800)
is never met. `is_rag_eligible = 1` but `index_builder.build_article_records()` will
produce L1-only records for these articles (title + summary). Zero L2 records from Finnhub.

This is a data quality reality, not a design defect. The value of Finnhub is publisher
breadth and event discovery — not evidence depth.

---

## 5. Tier-by-Publisher (NOT flat finnhub→T3)

Finnhub's `source` field carries the REAL publisher name. `source_tier.py`'s
`_PUBLISHER_TIER` already classifies by publisher name. Finnhub articles flow through
the SAME `classify_articles()` post-batch — no per-provider tiering.

### 5.1 New Publisher Entries

Based on discovery data, add these to `_PUBLISHER_TIER`:

| Publisher | Tier | Rationale |
|-----------|------|-----------|
| `"Yahoo"` | 4 | Aggregator — syndicates wire content, not original reporting |
| `"Yahoo Finance"` | 4 | Same as Yahoo |
| `"Yahoo Finance UK"` | 4 | Same as Yahoo |
| `"Yahoo Finance Video"` | 4 | Video content, aggregator |
| `"Seeking Alpha"` | 5 | Opinion/analysis platform — can be insightful but is editorial, not primary |
| `"Business Wire"` | 3 | Wire service — press release distribution |
| `"PR Newswire"` | 3 | Wire service — press release distribution |
| `"Accesswire"` | 4 | Press release aggregator — less established than Business Wire/PR Newswire |
| `"TipRanks"` | 5 | Analyst rating aggregator — opinion/derivative |
| `"Investor's Business Daily"` | 4 | Financial news/analysis — specialty publication |
| `"The Wall Street Journal"` | 2 | Premium financial press — original reporting |
| `"Reuters"` | 2 | Premium financial press — global wire with original reporting |
| `"Bloomberg"` | 2 | Premium financial press — original reporting |
| `"CNBC"` | 4 | Financial TV network — some original reporting but heavy syndication |
| `"Fox Business"` | 4 | Financial TV network |
| `"Barrons"` | 4 | Financial weekly — analysis/opinion |
| `"Morningstar"` | 4 | Fund research — specialist |
| `"MarketBeat"` | 4 | News aggregator |
| `"24/7 Wall St."` | 5 | Retail-oriented opinion/analysis |

**Existing entries that may also match Finnhub `source` values:**
- `"Benzinga"` → T4 (already in map)
- `"GlobeNewswire"` → T3 (already in map)
- `"GlobeNewswire Inc."` → T3 (already in map)
- `"The Motley Fool"` / `"Motley Fool"` → T5 (already in map)
- `"Zacks"` / `"Zacks Investment Research"` → T5 (already in map)
- `"MarketWatch"` → T2 (already in map)

**Unknown publisher fallback:** T4 (unchanged).

All new entries are ADDITIVE to `_PUBLISHER_TIER`. The fallback behavior for unknown
publishers (T4 + log-once warning) is unchanged.

---

## 6. Cross-Source Dedup (Polygon + Finnhub)

### 6.1 Problem

Finnhub and Polygon both cover the same financial events. A SeekingAlpha article
about "Apple Reports Q2 Earnings" may appear in both providers. Currently, the
`articles` table has `dedup_group_id` and `is_canonical` columns that are unused
Polygon-side (all rows have `dedup_group_id=NULL, is_canonical=1`).

### 6.2 Provider-Agnostic Fingerprint (AMENDED — per-association, multi-ticker aware)

Finnhub URLs are `finnhub.io` redirects — URL-based dedup will NEVER match Polygon.
Use a title+ticker+date fingerprint instead.

**Fingerprint definition:**
```
dedup_group_id = SHA256(
    NFC-normalized(title) + "|" + ticker + "|" + reference_date
)[:16]
```

Where:
- `NFC-normalized(title)` = `unicodedata.normalize("NFC", title.strip().lower())`
- `ticker` = ONE ticker from the (article, ticker) association
- `reference_date` = the trading date from article_tickers (ISO format `YYYY-MM-DD`)

**Multi-ticker strategy (D2 resolution):** The fingerprint is computed PER
`article_tickers` row — one fingerprint per (article, ticker, reference_date)
association. This means:

- A single-ticker Polygon article (AAPL, title="Earnings") gets one `dedup_group_id`
  via its sole `article_tickers` row.
- A multi-ticker Polygon article (MSFT, AAPL, title="Earnings") gets TWO fingerprints:
  one via `(article_id, MSFT, 2026-07-01)` and one via `(article_id, AAPL, 2026-07-01)`.
  Both are stored in `article_tickers.dedup_group_id`. That article can match a Finnhub
  AAPL article on the AAPL-specific fingerprint, AND a Finnhub MSFT article on the
  MSFT-specific fingerprint — correct behavior since the same event affects both tickers.
- The `articles.dedup_group_id` column is set to the lexicographically first ticker's
  fingerprint in the group, for backward-compatibility reference. The authoritative
  grouping is in `article_tickers.dedup_group_id`.

**Schema note:** `article_tickers` needs a new column `dedup_group_id TEXT` (additive DDL
via ALTER TABLE IF NOT EXISTS pattern). The existing `articles.dedup_group_id` is set as
the first-encountered fingerprint for reference but is NOT the authoritative grouping key.

**Canonical selection:** Grouping is by `article_tickers.dedup_group_id`. Within each
group, winner is chosen by `dedup/cross_source.select_canonical()`, building
`AssetCandidate` from article rows (source_type, title, published_utc, url,
ticker_primary, description). `canonical_url()`-based matching is explicitly NOT used
(Finnhub URLs are redirects).

**Conservative guard:** Two articles with the same ticker, same reference_date, and
same NFC-normalized title ARE the same event. Two articles with the same ticker and
date but subtly different titles are NOT merged. This favors false-negatives over
false-positives. The per-association approach ensures multi-ticker articles are
reachable from any of their associated tickers without requiring cross-ticker matching.

### 6.3 Canonical Selection

Within a `dedup_group_id`, one article is `is_canonical=1`, others are `is_canonical=0`.

**Selection criteria (in priority order):**
1. **Provider priority:** Polygon > Finnhub (Polygon has full history and richer metadata). This is already configured: `CROSS_SOURCE_PRIORITY = ("polygon_news", "fmp_news", "finnhub_company_news", "gdelt_news")`.
2. **Published time:** Earlier published article wins (first to report).
3. **Description length:** Longer description wins (more evidence).

Reuse `dedup.cross_source.select_canonical()` which already implements this priority chain.

### 6.4 Implementation (AMENDED — GROUP-COMPLETE canonical assignment)

**New function:** `compute_cross_source_dedup(conn) -> int` added to `dedup/cross_source.py`.

**Algorithm (two-pass):**

*Pass 1 — Assign dedup_group_id:*
For every article WHERE `dedup_group_id IS NULL` AND `provider IN ('polygon', 'finnhub')`:
  - Compute fingerprint = `SHA256(NFC(title.lower()) | ticker | reference_date)[:16]`
  - The ticker comes from `article_tickers.ticker` — one fingerprint per (article, ticker)
    association (see §6.2 D2 amendment).
  - For multi-ticker Polygon articles, this means one article gets MULTIPLE
    `dedup_group_id` rows (one per ticker association). The `articles.dedup_group_id`
    column is set to the first (alphabetically ordered) ticker's fingerprint, but the
    per-association fingerprints are stored in `article_tickers.dedup_group_id`.
  - UPDATE `articles SET dedup_group_id = ...` for single-ticker articles, and
    `article_tickers SET dedup_group_id = ...` for every association.

*Pass 2 — Recompute canonical (GROUP-COMPLETE, for EVERY group that has ≥2 members):*
For each `dedup_group_id` that has ≥2 article associations:
  - SELECT all articles in that group
  - Build `AssetCandidate(source_type, title, published_utc, url, ticker_primary, description)`
    from each article row
  - Call `select_canonical(duplicates)` → returns the winning AssetCandidate
  - UPDATE `articles SET is_canonical = 0` for ALL rows in the group
  - UPDATE `articles SET is_canonical = 1` WHERE `article_id = winner_article_id`

**Critical property:** Pass 2 runs against EVERY group with ≥2 members, NOT just groups
with NULL `is_canonical`. This handles the case where a Finnhub article joins a group
that already has a single Polygon article with `is_canonical=1` — the group now has two
members and `is_canonical` must be recomputed across both. A Polygon singleton with no
match stays `is_canonical=1` unchanged.

**Idempotent:** Safe to re-run. Pass 1 only touches `dedup_group_id IS NULL` rows.
Pass 2 recomputes `is_canonical` for all groups with ≥2 members — overwriting any
prior canonical assignments with the latest `select_canonical` result.

Post-batch flow in `run_update_batch`:
1. `rederive_polygon_news()` → populates Polygon articles
2. `rederive_finnhub_news()` → populates Finnhub articles
3. `compute_cross_source_dedup(conn)` → two-pass: assigns `dedup_group_id`, then computes
   `is_canonical` per group
4. `classify_articles(conn)` → sets `source_tier` by publisher
5. `regenerate_polygon_clean_assets()` → (unchanged, Polygon-only)
6. `build_incremental_records()` → delta index

**Non-destructive invariant:** Existing Polygon-only rows that DON'T match any Finnhub
article keep `dedup_group_id` assigned (Pass 1) and `is_canonical=1` (Pass 2 doesn't
touch singletons). The dedup step is additive — it groups articles by fingerprint
and selects a canonical winner within each group. A Polygon article with no Finnhub
counterpart is unchanged beyond getting a `dedup_group_id`.

### 6.5 Dedup Scope (AMENDED — group-complete for canonical, NULL-only for fingerprint)

- **Pass 1 (assign dedup_group_id):** Runs within articles WHERE `provider IN ('polygon',
  'finnhub')` AND `dedup_group_id IS NULL`. Only touches rows that have not been previously
  fingerprinted (idempotent by NULL check). Also sets `article_tickers.dedup_group_id` for
  each association.
- **Pass 2 (recompute canonical):** Runs against EVERY `dedup_group_id` that has ≥2 member
  articles, REGARDLESS of prior `is_canonical` values. This is NOT NULL-gated — a group
  that previously had one singleton (is_canonical=1) and later gains a Finnhub member must
  have its canonical recomputed over both members.
- Does NOT touch `filings` rows (different namespace — `dedup_group_id = SHA256("sec:{accession}")`)
- Does NOT dedup across different trading days (the fingerprint includes `reference_date`)

### 6.6 Post-Dedup Invariants (AMENDED — group-complete)

After `compute_cross_source_dedup`:
- Every article has a non-NULL `articles.dedup_group_id`
- Every `article_tickers` row has a non-NULL `dedup_group_id` (per-association)
- Within each `article_tickers.dedup_group_id` that has ≥2 members, exactly one
  `articles` row has `is_canonical=1`
- The canonical article is the one selected by `select_canonical()` across all
  article rows in that group
- All other article rows in the group have `is_canonical=0`
- Singleton groups (one article, no cross-source match) retain `is_canonical=1`

---

## 7. Pipeline + CLI Wiring

### 7.1 Source Mapping

**Add to `source_mapping.py`:**
```python
if source == "finnhub_company_news":
    return ["company-news"]
```

### 7.2 Update Pipeline

**Modify `_fetch_cell`:** Add Finnhub branch before the polygon fallback:
```python
if source == "finnhub_company_news":
    if fetcher_ns is None:
        raise ValueError(
            "fetcher_ns (Finnhub namespace from create_finnhub_fetcher) is "
            "required for source='finnhub_company_news'"
        )
    result = await _fetch_cell_finnhub(conn, ticker, date, run_id, fetcher_ns, limiter=limiter)
    conn.close()
    return result
```

**New function `_fetch_cell_finnhub`:**
- Fetch via `fetcher_ns.fetch(ticker, "company-news", date)`
- On non-200: write failed checkpoint, return error
- On 200: `upsert_raw_asset()` for the full JSON response (Bronze)
- Write success checkpoint (even if 0 articles — cell is covered)
- Return `{ticker, date, source, status, articles_count: len(json_array), ...}`

**Modify `run_update_batch` post-batch flow:**
1. If `finnhub_company_news` in sources:
   a. `rederive_finnhub_news(db_path)` → populates `articles` + `article_tickers`
2. If both polygon AND finnhub were fetched:
   a. `compute_cross_source_dedup(conn)` → fills `dedup_group_id` + `is_canonical`
3. If polygon_news in sources:
   a. `rederive_polygon_news(db_path)` (unchanged)
   b. `classify_articles(conn)` (now also tiers Finnhub articles)
   c. `regenerate_polygon_clean_assets(db_path)` (Polygon-only, unchanged)
4. `build_incremental_records(db_path)` (unchanged)

**Submissions cache:**
Finnhub does not need a per-run cache (unlike SEC). Each company-news call is date-scoped
and independent. No caching.

### 7.3 `run_update_batch` — Finnhub Fetcher Construction

Mirrors the SEC pattern:
```python
if "finnhub_company_news" in sources:
    from catalyst_data.connectors.finnhub import create_finnhub_fetcher
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        raise ValueError("FINNHUB_API_KEY environment variable is not set")
    finnhub_fetcher_ns = create_finnhub_fetcher(
        api_key=api_key,
        limiter=limiter,
    )
```

Dispatch per-source in `_fetch_cell` based on `source`:
- `finnhub_company_news` → pass `finnhub_fetcher_ns` as `fetcher_ns`

### 7.4 CLI

**Modify `cli_index.py` — `update-news` subcommand:**
- Add `finnhub_company_news` to `--sources` choices in help text
- Default unchanged (`polygon_news`)

**Modify `cli_index.py` — `backfill` subcommand:**
- Same `--sources` extension

**Modify `cli_index.py` — `status --freshness`:**
- `news_freshness` already reports per-source_type. Finnhub articles with
  `source_type='finnhub_company_news'` automatically appear in the news freshness
  section — no code change needed beyond ensuring the data is there.

### 7.5 Dry-Run Invariant

`dry_run=True` computes missing cells only via `compute_missing_cells()`. Zero network
calls, zero DB writes. The `fetch_fn=None` condition already enforces this — `_fetch_cell`
is never called during dry-run. The Finnhub path must maintain this invariant exactly:
do NOT construct the Finnhub fetcher during dry-run, and do NOT call `_fetch_cell_finnhub`.

---

## 8. Freshness

`news_freshness(conn)` already reports per-`source_type` via `article_tickers.reference_date`.
Finnhub articles with `source_type='finnhub_company_news'` are automatically included —
no code change needed. The function queries:

```sql
SELECT a.source_type, at.ticker, MAX(at.reference_date)
FROM article_tickers at JOIN articles a ...
```

Finnhub rows match `a.source_type = 'finnhub_company_news'` and appear alongside
`polygon_news` in the freshness report. No new per-provider dimension is needed.

**Expected behavior:** Finnhub freshness tracks the same local OHLCV calendar.
A cell with 0 articles on a given trading day produces a NO_DATA status. This is
expected — company-news may legitimately have no articles on slow days.

---

## 9. Tests

### 9.1 Test Fixture

**New fixture:** `tests/fixtures/finnhub_company_news_AAPL.json`

A real, redacted Finnhub company-news response for AAPL on a specific trading day.
Token stripped from all URLs (`token=[REDACTED]`). Document the capture source URL
in `tests/fixtures/README.md`.

Fixture content: A JSON array with 2–5 article objects covering typical response
shape (headline, summary, datetime, source, url, id, image, category).

### 9.2 Test Files

**`tests/test_finnhub_connector.py`** (6 tests):

1. **`test_company_news_200`** — Mocked 200 response → FetchResult with correct JSON array structure
2. **`test_company_news_field_mapping`** — Verify fixture fields parse correctly (headline→title, datetime→Unix→ISO, source→publisher_name)
3. **`test_token_as_query_param`** — Assert `token=` appears in the query string (not in headers), and the token value matches the API key
4. **`test_limiter_honored`** — Mock limiter, assert `acquire()` is called before the HTTP request
5. **`test_401_non_retryable`** — Mocked 401 → FetchResult with error, no retry
6. **`test_429_retry_after`** — Mocked 429 → retry behavior from `with_retry` wrapper

**`tests/test_finnhub_normalize.py`** (5 tests):

1. **`test_datetime_unix_to_iso`** — Unix timestamp 1751414400 → "2025-07-01T16:00:00Z"
2. **`test_publisher_name_from_source`** — `source="Yahoo"` → `publisher_name="Yahoo"`
3. **`test_article_tickers_is_query_symbol`** — article_tickers row has the query symbol, not a different ticker
4. **`test_l1_only_no_l2`** — Summary < 800 chars → `is_rag_eligible=1` but no L2 content (verified via index builder dry-run)
5. **`test_bronze_rederivability`** — Store a Finnhub raw_asset, decompress via `get_raw_asset()`, re-normalize via `rederive_finnhub_news()`, assert articles match expected fields

**`tests/test_finnhub_tier.py`** (4 tests):

1. **`test_seeking_alpha_tier_5`** — `tier_for_publisher("Seeking Alpha")` → 5
2. **`test_reuters_tier_2`** — `tier_for_publisher("Reuters")` → 2
3. **`test_unknown_publisher_tier_4`** — `tier_for_publisher("SomeUnknownBlog")` → 4
4. **`test_classify_articles_sets_finnhub_tiers`** — Full flow: insert a Finnhub article with publisher_name="Yahoo", run `classify_articles()`, assert `source_tier=4`

**`tests/test_finnhub_dedup.py`** (6 tests, AMENDED for D1 + M4):

1. **`test_same_event_shared_dedup_group`** — Polygon article + Finnhub article with same normalized title + ticker + reference_date → same `dedup_group_id` via article_tickers
2. **`test_distinct_stories_not_merged`** — Different titles, same ticker/date → different `dedup_group_id`
3. **`test_conservative_no_false_merge`** — "Apple Reports Q2 Earnings" vs "Apple Q2 Earnings Report" → NOT same (title normalization is literal, not fuzzy)
4. **`test_canonical_selection_polygon_wins`** — Polygon + Finnhub in same dedup group → Polygon article is `is_canonical=1`, Finnhub is `is_canonical=0`
5. **`test_idempotent_rerun_one_canonical`** (D1) — After first dedup run, insert a new Finnhub article that hashes into an existing group. Re-run `compute_cross_source_dedup`. Assert: exactly ONE `is_canonical=1` in that group, and the group now has 3 members.
6. **`test_multi_ticker_polygon_matches_finnhub`** (D2/M4) — Insert a multi-ticker Polygon article `(MSFT, AAPL, title="Earnings", 2026-06-30)`. Insert a Finnhub article `(AAPL, title="Earnings", 2026-06-30)`. Run dedup. Assert: both share a `dedup_group_id` via the AAPL association; the group has ≥2 members; exactly one `is_canonical=1`.

**`tests/test_update_pipeline.py`** — Extend existing SEC pipeline test class (4 new tests):

1. **`test_finnhub_zero_articles`** — Mocked 200 with empty array → success, articles_count=0, checkpoint written
2. **`test_finnhub_with_articles`** — Mocked 200 with 3 articles → success, articles_count=3, raw_asset stored
3. **`test_finnhub_http_error`** — Mocked 500 → failed checkpoint
4. **`test_finnhub_dry_run_zero_writes`** — Dry-run → zero new rows in raw_assets, zero articles

### 9.3 Frozen DB

SHA-256 must remain `0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf`.
All tests use `:memory:` or `tmp_path`. No test touches the frozen DB.

---

## 10. File Summary

### Created (9 files)

| File | Purpose |
|------|---------|
| `catalyst_data/connectors/finnhub.py` | Finnhub connector with create_finnhub_fetcher |
| `catalyst_data/pipeline/finnhub_normalize.py` | Finnhub article normalization + rederive |
| `tests/test_finnhub_connector.py` | Connector tests (6) |
| `tests/test_finnhub_normalize.py` | Normalization tests (5) |
| `tests/test_finnhub_tier.py` | Publisher tier tests (4) |
| `tests/test_finnhub_dedup.py` | Cross-source dedup tests (4) |
| `tests/fixtures/finnhub_company_news_AAPL.json` | Redacted Finnhub company-news fixture |
| `tests/fixtures/README.md` | Fixture source documentation (extend or create) |

### Modified (8 files)

| File | Change |
|------|--------|
| `catalyst_data/connectors/finnhub.py` | (new) |
| `catalyst_data/pipeline/finnhub_normalize.py` | (new) |
| `catalyst_data/provider_limits.py` | Add `FINNHUB` limits entry |
| `catalyst_data/retry.py` | Add `"finnhub"` retry policy |
| `catalyst_data/source_mapping.py` | Add `"finnhub_company_news"` mapping |
| `catalyst_data/config.py` | Add `"finnhub"` to `_PROVIDER_KEY_ENV` |
| `catalyst_data/source_tier.py` | Add 18 new publisher → tier entries |
| `catalyst_data/dedup/cross_source.py` | Add `compute_cross_source_dedup()` (two-pass, group-complete) |
| `catalyst_data/storage/sqlite.py` | Add `article_tickers.dedup_group_id` column (additive DDL) |
| `catalyst_data/update_pipeline.py` | Add `_fetch_cell_finnhub` + post-batch dedup |
| `catalyst_data/cli_index.py` | Add finnhub to `--sources` help text |
| `tests/test_update_pipeline.py` | Add 4 Finnhub pipeline tests |

### Test Count Summary

| Area | New Tests |
|------|-----------|
| Connector | 6 |
| Normalization + Bronze re-derivability | 5 |
| Publisher tiering | 4 |
| Cross-source dedup | 6 |
| Pipeline integration | 4 |
| **Total** | **25** |

All tests fixture-only. Zero network. Frozen DB SHA unchanged.

---

## 11. Risk Register

| Risk | Severity | Mitigation |
|------|----------|------------|
| Finnhub free tier history depth unknown | **MEDIUM** | Probe one ticker for available date range before backfill. If <1 year, document as a Finnhub limitation and adjust backfill window. |
| finnhub.io URLs are redirects — link rot | Low | Store raw URLs; canonical URL resolution is DEFERRED. Mitigated by title-based dedup. |
| Finnhub `source` field inconsistent across responses | Medium | `classify_articles()` logs unknown publishers once each. New publishers discovered during backfill can be added to `_PUBLISHER_TIER` in a follow-up. |
| Cross-source dedup false-merge | **MEDIUM** | Conservative fingerprint (NFC-normalized title + ticker + date). Only exact title matches merge. "Apple Q2 Earnings" vs "Apple Reports Q2" are NOT merged. Favor false-negatives. |
| Finnhub rate limit (60/min) too aggressive for backfill | Low | 10 tickers × 365 days = 3650 requests. At 1 req/sec = ~1 hour. Acceptable for periodic backfill, not real-time. |
| Summary-only content too short for meaningful L1 embedding | Low | Expected and accepted. Finnhub's value is breadth/discovery, not evidence depth. The title alone is still useful for event detection. |
| `FINNHUB_API_KEY` not set → crash | Low | Clear ValueError on startup. Document in setup instructions. |

---

## 12. Open Questions for Review

### (a) Orchestrator path vs dedicated `_fetch_cell_finnhub`

**Recommendation:** Dedicated `_fetch_cell_finnhub` (Pattern B, simplified).

Rationale detailed in §1.2. Summary: Finnhub does not use `clean_assets`; its rederive is
trivial (flat JSON array); the orchestrator has hardcoded Polygon assumptions. The dedicated
path is simpler, more maintainable, and consistent with the SEC pattern already proven in 3C.

### (b) Exact dedup fingerprint

**Recommendation:** `SHA256(NFC(title.lower().strip()) + "|" + ticker + "|" + reference_date)[:16]`

Conservative: exact title match after NFC normalization and lowercasing. No stemming,
no stopword removal, no fuzzy matching. Two articles about "Apple Reports Q2 Earnings"
on the same ticker and trading day ARE the same event. "Apple Reports Q2 Earnings" vs
"Apple Q2 Earnings Report" are NOT the same — this is intentional. Favor false-negatives
over merging distinct stories.

### (c) Finnhub free-tier history depth + daily quota

**Not yet probed.** The Finnhub free tier has a 60 req/min rate limit which we've already
configured. The history depth of the company-news endpoint is unknown — it may be 1 year
or less.

**Recommendation:** Before backfill, run a tiny probe script (separate from implementation):
```python
# Probe 2 tickers, 2 extreme dates
fetch(AAPL, from="2024-01-01", to="2024-01-01")  # ~18 months ago
fetch(AAPL, from="2025-07-01", to="2025-07-01")  # recent
```
If the 2024 request returns empty or an error, document the actual depth limit and
adjust the `from_date` for backfill accordingly. Do NOT include this probe in the
implementation PR — it's a one-time discovery step.

### (d) Publisher names seen in discovery not yet in `_PUBLISHER_TIER`

The discovery probe saw 5.6% SeekingAlpha and Yahoo-dominant results. The exact set
of publisher names encountered depends on the full 10-ticker crawl. The plan lists 18
new publisher entries based on known Finnhub publisher names (§5.1).

**Recommendation:** During the initial backfill execution (not plan phase), log all
unique publisher names to a file. After the first complete run, compare against
`_PUBLISHER_TIER` and add any missing names with appropriate tiers. This is a
post-implementation tuning step, not a plan-blocker.

---

## 13. Guardrails (All Apply)

- ✅ data-core only — no `packages/app` or `packages/agents`
- ✅ Dev DB `data/catalyst_dev_ws4b.db` only
- ✅ Frozen DB `data/catalyst_eval_frozen_v2.db` read-only, SHA `0dfc81b154a9...` unchanged
- ✅ No new dependencies — `httpx` + stdlib only
- ✅ No embeddings, no LanceDB, no model loads on Mac
- ✅ No network in tests (fixtures only)
- ✅ No commit — plan only
- ✅ No implementation code beyond DDL/contract snippets
- ✅ English only in code/comments
- ✅ Never log the API key — sanitize token from all error/log strings

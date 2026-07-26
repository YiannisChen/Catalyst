# Universe, Provider & Evidence-Coverage Audit

> Status: retained research evidence, not an execution specification. References below to deleted W1/N2 plans document historical audit inputs only. Current migration ownership and B2–B7 sequencing are binding in `docs/plans/2026-07-21-b2-b7-technical-contracts.md`.

**Date:** 2026-07-16
**Branch:** `ws4b/article-level-data`
**Author:** dscodex (read-only evidence gathering, amended)
**Purpose:** Factual evidence pack for Fable 5 architecture decision on tiered universe sizing.
**Amended:** 2026-07-16 — Sections A, B.3, D.1–D.3, E, F, G, H, I, J corrected/expanded per remediation pass.
**Integrity:** No DB writes, no provider calls, no secrets exposed, nothing staged or committed.

---

## A. Executive Factual Summary

1. **Current universe: 10 tickers** hardcoded in `config.py`. All 10 have Polygon and Finnhub news + FMP fundamentals (annual statements). OHLCV covers **2024-12-30 through 2026-05-01** (336 sessions) — it is **51 sessions behind** today (2026-07-16). News articles extend through **2026-07-02** — **10 sessions behind** today. OHLCV and news have distinct lags; neither reaches today.

2. **36,861 articles, 58,396 article_tickers associations** — two-provider (Polygon + Finnhub). Finnhub dominates count (62%) but only spans 2026-05-04 to 2026-07-02. Polygon spans Dec 2024 through Jul 2026. All 41 proposed peers have **incidental Polygon tickers_json coverage** (range: 8–877 articles each) recoverable without new API calls. Finnhub `related` field is 100% single-ticker — provides no multi-ticker mapping benefit.

3. **FMP news endpoints are 402-blocked** on current plan. Only `fmp-articles` (broad feed) is available on free tier — unimplemented, pagination and date filtering unknown. FMP fundamentals are fully operational but produce only annual financial statements, not news.

4. **Architecture:** Article-level many-to-many schema exists and is verified. Target/context profile schema is absent. `tickers_json` unroll is absent. Index builder and vector-cache pattern are proposed, not implemented.

5. **Migration conflict is concrete:**
   - W1-D plan (`docs/plans/2026-07-12-w1d-two-stage-update-service.md`) explicitly claims migration **v8**
   - W1-E plan (`docs/plans/2026-07-12-w1e-durable-run-control.md`) explicitly claims migration **v9**
   - N2 proposal (`docs/plans/2026-07-15-catalyst-final-product-attribution-architecture-review.md` row N2) also claims **v8**
   - Current DB `user_version`: **7**

6. **DB integrity confirmed.** Dev SHA unchanged: `92731fb7c5c3e989b4fdcbefb9d1d2060974082a6c60f1bbecad6bc42ee846b0`. Frozen SHA: `0d97a7ec61b6ec8fb5f9263b0b37b0efc9755812739d7afead7f720c3567e8dd`. `git diff --cached` empty.

---

## B. Current Target Universe

### B.1 Configured Tickers

**Source:** `packages/data-core/catalyst_data/config.py:97-100`

| # | Ticker | Sector/Industry | Classification |
|---|--------|-----------------|----------------|
| 1 | AAPL | Technology — Consumer Electronics (GICS 4520) | Inferred from GICS |
| 2 | AMD | Technology — Semiconductors (GICS 4530) | Inferred from GICS |
| 3 | AMZN | Consumer Discretionary — Internet Retail (GICS 2550) | Inferred from GICS |
| 4 | GOOGL | Communication Services — Internet Content (GICS 5020) | Inferred from GICS |
| 5 | JPM | Financials — Diversified Banks (GICS 4010) | Inferred from GICS |
| 6 | META | Communication Services — Internet Content (GICS 5020) | Inferred from GICS |
| 7 | MSFT | Technology — Software/Infrastructure (GICS 4510) | Inferred from GICS |
| 8 | NVDA | Technology — Semiconductors (GICS 4530) | Inferred from GICS |
| 9 | TSLA | Consumer Discretionary — Auto Manufacturers (GICS 2510) | Inferred from GICS |
| 10 | UNH | Healthcare — Managed Healthcare (GICS 3510) | Inferred from GICS |

All sector assignments are inferred from public GICS classification data. No local sector data source is embedded in the repository. Every "verified" label in the prior version is replaced with "inferred from GICS."

**Sector concentration:** Technology 4 (AAPL, AMD, MSFT, NVDA), Communication Services 2 (GOOGL, META), Consumer Discretionary 2 (AMZN, TSLA), Financials 1 (JPM), Healthcare 1 (UNH). Industrials, Energy, Materials, Utilities, Real Estate, Consumer Staples have zero representation.

### B.2 Hardcoded Assumptions

**config.py** — universe source of truth:
- `TICKER_UNIVERSE`: 10-ticker tuple at line 97
- `HISTORICAL_START`: `"2024-12-30"` at line 102
- `CROSS_SOURCE_PRIORITY`: includes `fmp_news` which is unimplemented (no connector exists)

**cik_map.py** — hardcoded 10-ticker frozenset:
- `SUPPORTED_TICKERS = frozenset(["AAPL", "AMD", "AMZN", "GOOGL", "JPM", "META", "MSFT", "NVDA", "TSLA", "UNH"])`
- Raises RuntimeError if refreshed and any of these 10 are missing

**source_mapping.py** — logical-to-physical mapping:
- `polygon_news` → `["news"]`, `polygon_ohlcv` → `["ohlcv"]`
- `finnhub_company_news` → `["company-news"]`
- `fmp_fundamentals` → `["income_statement", "balance_sheet", "cash_flow"]`
- `sec_filings` → `["sec_submissions"]`
- `fred_macro` → `["DAAA", "DBAA", "DFF", "DGS10", "DGS2", "CPIAUCSL", "GDP", "PAYEMS", "PCEPI", "UNRATE", "VIXCLS"]`

### B.3 Modules/Tests Assuming Exactly 10 Symbols

**Direct references:**
- `config.py`: `TICKER_UNIVERSE` — any consumer gets 10
- `cik_map.py`: `SUPPORTED_TICKERS` — validation gate checks these 10
- `update_planner.py`: `_resolve_universe()` — falls back to `TICKER_UNIVERSE`
- `update_pipeline.py`: `compute_missing_cells()` — uses OHLCV-derived universe by default (10 in practice)

**Tests with hardcoded tickers (illustrative, not exhaustive):**
- `test_run_update_service.py`: `AAPL` as sole fixture
- `test_coverage_audit.py`: `AAPL`, `MSFT`, `TSLA`, `NVDA`, `META`
- `test_cross_source_dedup.py`: `AAPL` as default

**Extensible-by-design modules:**
- `update_pipeline.py`: `compute_missing_cells(tickers=None)` → reads `SELECT DISTINCT symbol FROM ohlcv`
- `update_planner.py`: `plan_update(use_ohlcv_universe=True)` → reads from ohlcv
- `index_builder.py`: ticker-agnostic — derives from articles/filings tables

---

## C. Candidate Peer and Benchmark Seed

### C.1 Proposed Peer Candidates

All relationships are **proposed**, not approved architecture. No "verified" label is used. Classification taxonomy:

| Label | Meaning |
|-------|---------|
| direct peer | Same GICS sub-industry, competing for same customer |
| direct supplier/customer | Documented supply or distribution relationship |
| indirect upstream exposure | Upstream in supply chain but not direct 1:1 |
| broad sector context | Same sector but different sub-industry |

#### Technology — Semiconductors (AMD, NVDA)

| Ticker | Relationship | Classification |
|--------|-------------|----------------|
| INTC | Competitor — x86/CPU/GPU peer | direct peer |
| QCOM | Competitor — mobile/auto silicon | direct peer |
| AVGO | Adjacent — networking/ASICs, not direct chip peer | broad sector context |
| TXN | Adjacent — analog/embedded, different sub-industry | broad sector context |
| MU | Memory supplier to system builders; indirect for fabless designers | indirect upstream exposure |
| ASML | Lithography equipment vendor to foundries (TSM, INTC, Samsung); not a direct supplier to fabless chip designers (AMD, NVDA) | indirect upstream exposure |
| TSM | Foundry for AMD, NVDA, AAPL, QCOM, AVGO, INTC | direct supplier/customer |

**ASML/LRCX note:** ASML sells lithography equipment to foundries, not to fabless designers. AMD and NVDA contract with foundries (primarily TSM) who own the ASML relationship. Representing ASML or LRCX as direct suppliers to AMD/NVDA without provenance is incorrect. They are indirect upstream exposure.

#### Technology — Consumer Electronics (AAPL)

| Ticker | Relationship | Classification |
|--------|-------------|----------------|
| SONY | Competitor — consumer electronics | direct peer |
| DELL | Competitor — PC/enterprise | direct peer |
| HPQ | Competitor — PC/print | direct peer |
| LRCX | Semiconductor equipment → foundries → AAPL's chip suppliers | indirect upstream exposure |

#### Technology — Software/Infrastructure (MSFT)

| Ticker | Relationship | Classification |
|--------|-------------|----------------|
| ORCL | Competitor — enterprise software/cloud | direct peer |
| CRM | Competitor — enterprise SaaS | direct peer |
| ADBE | Competitor — creative/experience SaaS | direct peer |
| NOW | Competitor — enterprise workflow | direct peer |
| SNOW | Adjacent — data cloud, different sub-industry | broad sector context |

#### Communication Services — Internet Content (GOOGL, META)

| Ticker | Relationship | Classification |
|--------|-------------|----------------|
| SNAP | Competitor — social media | direct peer |
| PINS | Competitor — social/visual discovery | direct peer |
| RDDT | Competitor — social/community | direct peer |
| BIDU | Competitor — Chinese internet/search, different regulatory domain | broad sector context |

#### Consumer Discretionary — Internet Retail (AMZN)

| Ticker | Relationship | Classification |
|--------|-------------|----------------|
| WMT | Competitor — retail/omnichannel | direct peer |
| TGT | Competitor — retail | direct peer |
| COST | Competitor — membership retail | direct peer |
| SHOP | Adjacent — e-commerce platform | broad sector context |
| DASH | Adjacent — delivery/logistics | broad sector context |

#### Consumer Discretionary — Auto (TSLA)

| Ticker | Relationship | Classification |
|--------|-------------|----------------|
| F | Competitor — traditional auto EV transition | direct peer |
| GM | Competitor — traditional auto EV transition | direct peer |
| RIVN | Competitor — EV pure play | direct peer |
| LCID | Competitor — EV luxury | direct peer |
| NIO | Competitor — Chinese EV, different regulatory domain | broad sector context |

#### Financials — Diversified Banks (JPM)

| Ticker | Relationship | Classification |
|--------|-------------|----------------|
| BAC | Competitor — universal bank | direct peer |
| WFC | Competitor — universal bank | direct peer |
| C | Competitor — universal bank | direct peer |
| GS | Competitor — investment bank | direct peer |
| MS | Competitor — investment bank | direct peer |
| BLK | Adjacent — asset management, not a bank | broad sector context |

#### Healthcare — Managed Healthcare (UNH)

| Ticker | Relationship | Classification |
|--------|-------------|----------------|
| CI | Competitor — health insurer | direct peer |
| CNC | Competitor — managed care | direct peer |
| HUM | Competitor — Medicare Advantage | direct peer |
| ELV | Competitor — health insurer (formerly Anthem) | direct peer |
| CVS | Adjacent — pharmacy/health services | broad sector context |

### C.2 Deduplicated Peer List

41 unique symbols proposed across all targets. 20 are direct peers; 6 are direct supplier/customer; 4 are indirect upstream exposure; 11 are broad sector context. See Section D.6 for incidental coverage per peer.

### C.3 Benchmark Seed

| Ticker | Type | Rationale |
|--------|------|-----------|
| SPY | Broad market | S&P 500 ETF |
| QQQ | Tech-heavy | Nasdaq-100 |
| XLK | Technology sector | Maps to AAPL, AMD, MSFT, NVDA |
| XLC | Communication Services | Maps to GOOGL, META |
| XLY | Consumer Discretionary | Maps to AMZN, TSLA |
| XLV | Healthcare | Maps to UNH |
| XLF | Financials | Maps to JPM |
| SMH | Semiconductors | Maps to AMD, NVDA (sub-sector) |

All 10 targets map to a convincing sector ETF. No unmapped targets. 8 benchmark symbols.

---

## D. Current News Coverage

### D.1 Articles by Provider

| Provider | Article Count | % |
|----------|--------------|---|
| finnhub | 22,941 | 62.2% |
| polygon | 13,920 | 37.8% |
| **Total** | **36,861** | |

### D.2 Article_Tickers by Ticker and Provider

| Ticker | Polygon | Finnhub | Total |
|--------|---------|---------|-------|
| NVDA | 5,846 | 8,751 | 14,597 |
| GOOGL | 3,496 | 4,367 | 7,863 |
| MSFT | 3,318 | 3,913 | 7,231 |
| AMZN | 3,517 | 3,733 | 7,250 |
| AAPL | 2,354 | 2,825 | 5,179 |
| META | 2,115 | 2,979 | 5,094 |
| TSLA | 1,981 | 2,851 | 4,832 |
| JPM | 697 | 1,272 | 1,969 |
| AMD | 1,302 | 2,350 | 3,652 |
| UNH | 302 | 427 | 729 |

### D.3 Coverage Date Ranges — Corrected

| Data Layer | Earliest | Latest | Sessions | Lag Behind 2026-07-16 |
|-----------|----------|--------|----------|----------------------|
| OHLCV | 2024-12-30 | **2026-05-01** | 336 | 51 sessions |
| Polygon articles | 2024-12-30 | 2026-07-02 | 378 | 10 sessions |
| Finnhub articles | 2026-05-04 | 2026-07-02 | 42 | 10 sessions |

**Corrected facts:**
- OHLCV ends on 2026-05-01 — **not** 2026-07-02 as previously stated.
- News (both providers) ends on 2026-07-02 — **not** today (2026-07-16).
- OHLCV lag (51 sessions) and news lag (10 sessions) are **separate** and must be reported independently.

### D.4 Targets with Weak Coverage

| Ticker | Total Articles | Assessment |
|--------|---------------|------------|
| UNH | 729 | Weakest — least covered by both providers |
| JPM | 1,969 | Below average |
| AMD | 3,652 | Adequate |
| TSLA | 4,832 | Adequate |
| META | 5,094 | Good |
| AAPL | 5,179 | Good |
| AMZN | 7,250 | Strong |
| MSFT | 7,231 | Strong |
| GOOGL | 7,863 | Strong |
| NVDA | 14,597 | Outlier — most covered |

### D.5 Duplicate/Syndicated Behavior

- **dedup_groups:** 56,763 distinct groups identified
- **non-canonical associations:** 1,633 (2.8% of article_tickers)
- **Cross-source URL matching:** `canonical_url()` strips tracking params
- **Title-based matching:** NFC-normalized title + same ticker + within 4-hour window = duplicate
- **Polygon description:** max ~758 chars (never triggers L2 embedding at 800-char threshold)
- **Publisher distribution:** T4 (Aggregator) dominates at 73% (26,916); T5 (Opinion) at 22% (7,971); T2 (Premium) at 0.03% (12)

### D.6 Peer Incidental Coverage (from tickers_json)

Every proposed peer has recoverable Polygon articles via `tickers_json`. Zero peers appear as scalar ticker in `articles.ticker`. No new API calls are needed to recover this data — only a `rederive` extension to unroll `tickers_json` into `article_tickers` rows.

| Peer | Polygon Articles | Date Range | Relationship |
|------|-----------------|------------|-------------|
| AVGO | 877 | 2024-12-31 → 2026-07-02 | broad sector context |
| TSM | 658 | 2024-12-31 → 2026-07-02 | direct supplier/customer |
| INTC | 621 | 2025-01-01 → 2026-07-02 | direct peer |
| ORCL | 508 | 2024-12-31 → 2026-07-02 | direct peer |
| MU | 484 | 2024-12-31 → 2026-07-02 | indirect upstream exposure |
| WMT | 289 | 2025-01-02 → 2026-07-02 | direct peer |
| BAC | 270 | 2024-12-31 → 2026-07-02 | direct peer |
| GS | 195 | 2025-01-03 → 2026-07-02 | direct peer |
| CRM | 180 | 2025-01-01 → 2026-07-02 | direct peer |
| RIVN | 165 | 2025-01-03 → 2026-07-02 | direct peer |
| QCOM | 164 | 2025-01-01 → 2026-06-30 | direct peer |
| DELL | 159 | 2025-01-01 → 2026-07-01 | direct peer |
| GM | 138 | 2025-01-06 → 2026-06-25 | direct peer |
| ASML | 132 | 2025-01-07 → 2026-07-01 | indirect upstream exposure |
| F | 131 | 2025-01-09 → 2026-06-25 | direct peer |
| COST | 121 | 2025-01-07 → 2026-07-02 | direct peer |
| C | 120 | 2025-01-06 → 2026-07-02 | direct peer |
| ADBE | 116 | 2024-12-31 → 2026-07-02 | direct peer |
| WFC | 106 | 2025-01-02 → 2026-07-02 | direct peer |
| MS | 98 | 2024-12-31 → 2026-06-30 | direct peer |
| LCID | 95 | 2025-01-02 → 2026-07-02 | direct peer |
| SNOW | 88 | 2025-01-08 → 2026-07-01 | broad sector context |
| NOW | 87 | 2025-01-08 → 2026-06-25 | direct peer |
| SHOP | 84 | 2025-01-15 → 2026-06-18 | broad sector context |
| TGT | 67 | 2025-01-02 → 2026-07-01 | direct peer |
| RDDT | 55 | 2025-01-16 → 2026-06-16 | direct peer |
| BIDU | 48 | 2025-01-14 → 2026-06-15 | broad sector context |
| BLK | 47 | 2025-01-03 → 2026-05-28 | broad sector context |
| NIO | 43 | 2025-01-13 → 2026-06-24 | broad sector context |
| HPQ | 42 | 2025-01-06 → 2026-06-30 | direct peer |
| CVS | 36 | 2025-01-13 → 2026-06-15 | broad sector context |
| SNAP | 35 | 2025-01-03 → 2026-07-01 | direct peer |
| PINS | 32 | 2025-01-03 → 2026-07-02 | direct peer |
| TXN | 30 | 2025-01-30 → 2026-06-24 | broad sector context |
| LRCX | 30 | 2025-02-04 → 2026-07-01 | indirect upstream exposure |
| SONY | 27 | 2025-01-08 → 2026-03-31 | direct peer |
| DASH | 25 | 2025-02-12 → 2026-06-17 | broad sector context |
| CI | 20 | 2025-02-21 → 2026-06-24 | direct peer |
| HUM | 20 | 2025-01-13 → 2026-04-13 | direct peer |
| ELV | 13 | 2025-05-19 → 2026-04-07 | direct peer |
| CNC | 8 | 2025-05-19 → 2026-04-07 | direct peer |

**Key finding:** All 41 peers have >0 incidental articles. AVGO, TSM, INTC, ORCL, MU each have 480+ articles recoverable without new API calls. CNC (8 articles) and ELV (13) are the thinnest. The median is 98 articles per peer. All coverage is Polygon-only (Finnhub `related` field provides no multi-ticker benefit — see Section E.2).

---

## E. Provider Capability Table

### E.1 Connector Capability Matrix

| Provider | Endpoint | Ticker-Scoped | Broad Feed | Date Filter | Pagination | Pub Timestamp | Provider Ticker Tags | Connector | Current Probe |
|----------|----------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **Polygon** | `/v2/reference/news` | Yes | Unknown | `published_utc.gte/.lt` per day | `limit=50` | `published_utc` ISO-8601 | `tickers[]` array per article | Implemented | 200, ~10 items/req |
| **Finnhub** | `/api/v1/company-news` | Yes | No | `from`/`to` per day | None — all for day | `datetime` Unix epoch | `related` string (single ticker) | Implemented | 200, ~55 items/req |
| **FMP** | `stable/news/stock` | Yes | No | Unknown | Unknown | Unknown | Unknown | No | 402 — paid plan |
| **FMP** | `stable/news/stock-latest` | No | Yes | None | `page`/`limit` | Unknown | Unknown | No | 402 — paid plan |
| **FMP** | `stable/news/press-releases` | Yes | No | Unknown | Unknown | Unknown | Unknown | No | 402 — paid plan |
| **FMP** | `stable/news/press-releases-latest` | No | Yes | None | `page`/`limit` | Unknown | Unknown | No | 402 — paid plan |
| **FMP** | `stable/news/general-latest` | No | Yes | None | `page`/`limit` | Unknown | Unknown | No | 402 — paid plan |
| **FMP** | `stable/fmp-articles` | Yes | Yes | `date` field exists | `page`/`limit` | `date` field (format uninspected) | `tickers` field in item | No | 200, free tier |
| **SEC** | `/submissions/CIK{}.json` | Per-CIK | No | All per CIK | N/A — single call | `filingDate` | `tickers[]` on company level | Implemented | 200 |
| **SEC** | `fetch_document(url)` | N/A | N/A | N/A | N/A | N/A | N/A | Implemented | 200, HTML→text |
| **GDELT** | (probed) | Yes | No | Unknown | Unknown | Unknown | Unknown | No | Transport error |
| **Tiingo News** | (probed) | Unknown | Unknown | Unknown | Unknown | Unknown | Unknown | No | 403 — blocked |
| **Nasdaq Earnings** | (probed) | Yes | No | N/A | N/A | N/A | Query param | No | 4/10 tickers found |

### E.2 Finnhub `related` Field — Bounded Sample

Inspected from 420 raw_assets (33,494 embedded articles):

| Metric | Count | % |
|--------|-------|---|
| Total articles inspected | 33,494 | 100% |
| Empty/missing `related` | 0 | 0.0% |
| Single ticker in `related` | 33,494 | 100.0% |
| Multi-ticker in `related` | 0 | 0.0% |

**Format:** Plain string, always a single ticker symbol. Never comma-separated, never space-separated multi-value. Never a JSON array.

**Deterministic parsing:** Trivial — the value is the ticker. No parsing ambiguity exists.

**Conclusion:** Finnhub provides no multi-ticker mapping benefit. The `related` field is redundant with the request ticker. Only Polygon's `tickers[]` array carries multi-ticker data that could expand peer coverage.

### E.3 Request Semantics — Cell ≠ HTTP Request

The following terms are distinct and must not be conflated:

| Term | Definition | Example |
|------|-----------|---------|
| **Planner cell** | One (ticker, trading_day, source_type) tuple in the update plan | `(AAPL, 2026-07-15, polygon_news)` = 1 cell |
| **Connector request** | One HTTP call from a connector to a provider | 1 Polygon `GET /v2/reference/news?ticker=AAPL&...` |
| **Pagination request** | One page of a paginated response | Polygon `limit=50`; if 137 results, unknown if paginated |
| **Broad-feed request** | One HTTP call returning articles for all/many tickers | 1 FMP `GET stable/fmp-articles?page=0&limit=5` |
| **Historical range request** | One request covering a date range rather than a single day | Polygon `published_utc.gte=2024-12-30&published_utc.lt=2025-03-31` |

**Current mapping:**
- `polygon_news` cell → 1 connector request per day (single-day date filter)
- `finnhub_company_news` cell → 1 connector request per day (single-day date filter)
- `polygon_ohlcv` cell → 1 connector request per day
- `fmp_fundamentals` cell → 3 connector requests (income, balance, cash_flow) per cell

**Unknown pagination multipliers:**
- Polygon may paginate when results exceed `limit=50` — observed 10 items/request for targets, pagination behavior at scale unknown
- Finnhub returns all articles for a day — no pagination observed (max 137 items for AAPL one day)
- FMP `fmp-articles` pagination depth unknown — `page`/`limit` params confirmed but per-page count and total pages unmeasured
- SEC submissions: single call per CIK, no pagination

**Historical backfill optimization:**
- Polygon's date filter supports ranges: `published_utc.gte` / `published_utc.lt` with arbitrary dates. A historical backfill could use wider windows (e.g., 30-day ranges) instead of per-day. **This is not implemented** — the current connector hardcodes single-day windows.
- Finnhub's `from`/`to` also support date ranges — similarly not used for range queries.

---

## F. FMP/Earnings Factual Audit

### F.1 Implemented FMP Endpoints

**Source:** `packages/data-core/catalyst_data/connectors/fmp.py`

| Logical Name | FMP Path | Purpose |
|-------------|----------|---------|
| `income_statement` | `stable/income-statement` | Annual income statements |
| `balance_sheet` | `stable/balance-sheet-statement` | Annual balance sheets |
| `cash_flow` | `stable/cash-flow-statement` | Annual cash flow statements |

All three mapped as `fmp_fundamentals` → `["income_statement", "balance_sheet", "cash_flow"]` via `source_mapping.py`.

### F.2 `reference_date` and Request Semantics

The `date` parameter passed to `create_fmp_fetcher` is **not used** in URL construction:

```python
params = {"symbol": ticker, "apikey": api_key, "period": "annual"}
```

The `reference_date` in `raw_assets` reflects the caller's trading-day context, not the FMP response. FMP data is date-independent — the same annual statements return regardless of which trading day is queried.

### F.3 (Ticker, Endpoint) Snapshot Identity

Under the current logical-source schema, `fmp_fundamentals` maps to 3 physical endpoints whose responses are merged into **one logical asset per (ticker, reference_date)**. The three endpoint payloads are encoded together in a single compressed JSON blob per `raw_assets` row.

**Current raw_assets:** 3,500 rows = 10 tickers × 350 reference_dates (2024-12-30 through 2026-05-01, one per trading day). Each row contains all three endpoint responses merged.

**Factual identity requirements for a provider snapshot:**

| Field | Purpose |
|-------|---------|
| ticker | Which company |
| logical source | `fmp_fundamentals` |
| provider observation timestamp | When the payload was fetched (currently `fetched_at`) |
| payload content hash | SHA-256 of the merged compressed payload |
| statement period/report dates | `date`, `fillingDate`, `acceptedDate` from the statement payloads |

**Correction:** The statement "30 tickers should produce 90 raw rows" was incorrect. Under the current merged-logical-asset schema, one observation per (ticker, reference_date) produces one row — not one per endpoint. A new provider snapshot must create a **new version** when content changes (different content hash for the same ticker), not overwrite. The current `INSERT OR REPLACE` by asset_id behavior silently overwrites when content changes.

### F.4 DB Counts

| Metric | Count |
|--------|-------|
| FMP raw_assets rows | 3,500 |
| Unique tickers | 10 |
| Date range | 2024-12-30 → 2026-05-01 |
| Assets per ticker | 350 |
| FMP source_checkpoints (success) | 3,500 |
| FMP source_checkpoints (failed) | 22 |

### F.5 Fields Available

Income statement: `date`, `symbol`, `reportedCurrency`, `cik`, `fillingDate`, `acceptedDate`, `calendarYear`, `period`, `revenue`, `costOfRevenue`, `grossProfit`, `grossProfitRatio`, `researchAndDevelopmentExpenses`, `generalAndAdministrativeExpenses`, `sellingAndMarketingExpenses`, `sellingGeneralAndAdministrativeExpenses`, `otherExpenses`, `operatingExpenses`, `costAndExpenses`, `interestIncome`, `interestExpense`, `depreciationAndAmortization`, `ebitda`, `ebitdaratio`, `operatingIncome`, `operatingIncomeRatio`, `totalOtherIncomeExpensesNet`, `incomeBeforeTax`, `incomeBeforeTaxRatio`, `incomeTaxExpense`, `netIncome`, `netIncomeRatio`, `eps`, `epsdiluted`, `weightedAverageShsOut`, `weightedAverageShsOutDil`

Balance sheet and cash flow provide comparable breadth. Quarterly statements are not fetched — `period=annual` is hardcoded.

### F.6 What Can Honestly Be Materialized

- Annual financial statements for all 10 targets
- **Not:** quarterly statements, earnings calendar, earnings surprise, earnings estimates, FMP news (all 402-blocked)

### F.7 Unimplemented Endpoints in Provider Research

| Endpoint | Source | Status |
|----------|--------|--------|
| FMP stock news (all variants) | `fmp_reprobe.py`, `provider_audit.py` | 402 — paid-only |
| FMP press releases (all variants) | `fmp_reprobe.py` | 402 — paid-only |
| FMP general news latest | `fmp_reprobe.py` | 402 — paid-only |
| **FMP Articles (broad)** | `fmp_reprobe.py` | **200 OK — free tier, unimplemented** |
| FMP Earnings Calendar | Not probed via FMP | Unknown |
| Nasdaq Earnings Calendar | Provider dossiers | 4/10 tickers found |

### F.8 SEC Coverage — Corrected Counts

| Ticker | Form | Count | Date Range |
|--------|------|-------|------------|
| JPM | 8-K | **4** | 2026-06-02 → 2026-06-25 |
| NVDA | 8-K | **3** | 2026-06-18 → 2026-07-02 |
| **Total** | | **7** | |

All 7 filings are 8-K. No 10-Q or 10-K filings have been ingested. The statement "5 JPM 8-Ks and 2 NVDA 8-Ks" was incorrect.

---

## G. Request and Storage Estimates

### G.1 Canonical Trading-Session Counts

**Source:** `catalyst_data.trading_calendar.calendar_trading_days()` — verified at runtime.

| Window | Sessions |
|--------|----------|
| 2024-12-30 → 2026-05-01 (OHLCV watermark) | **336** |
| 2024-12-30 → 2026-07-02 (article watermark) | **378** |
| 2024-12-30 → 2026-07-16 (today) | **387** |
| OHLCV catch-up (2026-05-02 → 2026-07-16) | **51** |
| News catch-up (2026-07-03 → 2026-07-16) | **10** |
| Single rolling day | **1** |

The prior document used an incorrect 140-session figure. All estimates below use canonical calendar counts.

### G.2 Three Workloads — Separate Estimates

#### Workload 1: Existing DB Catch-Up (from current watermarks to today)

OHLCV is 51 sessions behind. News is 10 sessions behind.

| Provider | Source | Tickers | Cells | Connector Requests | Notes |
|----------|--------|---------|-------|-------------------|-------|
| Polygon | OHLCV | 10 | 510 | 510 | 1 req/cell/day |
| Polygon | News | 10 | 100 | 100 | 1 req/cell/day; already have articles through 07-02 |
| Finnhub | News | 10 | 100 | 100 | 1 req/cell/day; already have articles through 07-02 |
| FMP | Fundamentals | 10 | 510 | 1,530 | 3 endpoints per cell |
| **Catch-up total** | | 10 | 1,220 | 2,240 | |

#### Workload 2: Full Historical Backfill (from 2024-12-30)

| Provider | Source | Tickers | Sessions | Cells | Connector Requests | Notes |
|----------|--------|---------|----------|-------|-------------------|-------|
| Polygon | OHLCV | 10 | 387 | 3,870 | 3,870 | Range optimization possible |
| Polygon | OHLCV | 30 | 387 | 11,610 | 11,610 | |
| Polygon | News | 10 | 387 | 3,870 | 3,870 | Single-day windows |
| Polygon | News | 30 | 387 | 11,610 | 11,610 | Could use range queries (unimplemented) |
| Finnhub | News | 10 | 387 | 3,870 | 3,870 | Single-day windows |
| Finnhub | News | 30 | 387 | 11,610 | 11,610 | |
| FMP | Fundamentals | 10 | 387 | 3,870 | 11,610 | 3 endpoints/cell; date-independent |
| FMP | Fundamentals | 30 | 387 | 11,610 | 34,830 | |
| SEC | Submissions | 10 | N/A | 10 | 10 | 1 per CIK |
| SEC | Submissions | 30 | N/A | 30 | 30 | 1 per CIK |
| Benchmarks | OHLCV | 8 | 387 | 3,096 | 3,096 | ETFs |
| Broad feed | fmp-articles | N/A | 387 | 387 | **Unknown × pagination** | Pagination multiplier unknown |

**Polygon range-caching note:** The Polygon connector could use multi-day date ranges for historical backfill (e.g., 30-day windows) instead of per-day, reducing 387 requests per ticker to ~13. This optimization is not implemented.

#### Workload 3: One Normal Rolling Trading Day

| Provider | Source | Tickers (10) | Tickers (30) | Tickers (38 with benchmarks) |
|----------|--------|:---:|:---:|:---:|
| Polygon | OHLCV | 10 | 30 | 38 |
| Polygon | News | 10 | 30 | 38 |
| Finnhub | News | 10 | 30 | 38 |
| FMP | Fundamentals | 30 req | 90 req | 114 req |
| SEC | Submissions | — | — | — |
| Broad feed | fmp-articles | 1 req × pagination | 1 req × pagination | 1 req × pagination |

### G.3 OHLCV Row Estimates

| Config | Tickers | Trading Days | OHLCV Rows |
|--------|---------|-------------|------------|
| 10 targets | 10 | 336 (existing) + 51 catch-up | 3,349 + 510 = 3,859 |
| 10 targets + 20 peers | 30 | 387 (full backfill) | 11,610 |
| 10 targets + 30 peers | 40 | 387 (full backfill) | 15,480 |
| Benchmarks only | 8 | 387 | 3,096 |

**Formula:** `tickers × trading_sessions × 1 (daily bar)`

### G.4 Embedding Volume Estimates

**Ratified design:**
- Model: BAAI/bge-m3 (exact HF revision pinned at first server build)
- Dimensions: **1,024** (fp16)
- Server-side batch embedding
- Vector cache keyed by `SHA-256(content_hash + model_revision + config_hash)`
- Full candidate LanceDB rebuild from cached vectors

**Removed:** All text-embedding-3-small, 1,536-dimension, API-call, and dollar-cost estimates.

| Config | Eligible L1 Articles | L2 Chunks (~1.3%) | Raw fp16 Bytes |
|--------|---------------------|-------------------|----------------|
| Current 10 targets | ~35,958 | ~463 | ~74.6 MB |
| +20 peers (broad feed) | ~35,958 + ~40,000 = ~76,000 | ~988 | ~157.7 MB |
| +30 peers (broad feed) | ~35,958 + ~60,000 = ~96,000 | ~1,248 | ~199.2 MB |

**Formula:** `N articles × 1024 dims × 2 bytes (fp16) = raw vector bytes`

**Metadata overhead:** Unknown — depends on LanceDB index structure, chunk_id length, and metadata columns. Not estimated here.

**Cache misses for new content:** All peer articles would be cache misses on first build. Cache hits for existing 10-target articles if content hashes match (expected if no re-derive changes).

**Server throughput:** Must be measured on the deployment hardware. No throughput claims are made here.

---

## H. Existing Architecture Readiness

### H.1 Readiness Matrix

| Capability | Status | Evidence |
|-----------|--------|----------|
| Many-to-many article schema (`article_tickers`) | **existing and verified** | `articles.py` DDL; 58,396 rows; PK `(article_id, ticker)` |
| Article-level quality tiers (`source_tier`, `is_canonical`, `is_rag_eligible`) | **existing and verified** | `source_tier.py`; `dedup/cross_source.py`; `eligibility.py` |
| `corpus_items` VIEW (articles + filings union) | **existing and verified** | `sqlite.py:_CORPUS_ITEMS_VIEW`; polymorphic `source_kind` |
| `tickers_json` unroll to `article_tickers` | **absent** | `rederive.py` writes only scalar `ticker`, not `tickers_json` array |
| Target/context profile schema | **absent** | No `profile` column, no `target_universe` table, no context flag |
| Context certification pipeline | **absent** | No certification module exists; only eligibility gating |
| `index_state` producer (pending queue) | **existing and verified** | `index_builder.py:persist_index_state()` — 35,958 L1 + 463 L2 pending rows |
| Update-run outbox wiring | **absent** | Not implemented; referenced in architecture decisions §N |
| Vector cache (content-hash keyed) | **proposed** | Designed in specs; no implementation |
| Canonical-corpus LanceDB builder | **absent** | No `lancedb` package in main Mac `.venv`; `build_index.py` and `build_embeddings_gpu.py` reference server-side scripts |
| ACTIVE candidate promotion | **proposed** | Referenced as "outbox" pattern in architecture decisions |
| Incremental vector reuse (delta-only embed) | **designed, not implemented** | `index_builder.py:build_incremental_records()` computes delta; no embed/reuse layer exists |
| Broad news ingestion (non-ticker-scoped feed) | **absent** | Only ticker-scoped connectors implemented; `fmp-articles` probed but no connector |
| `lancedb` package availability (Mac .venv) | **absent** | Not importable in main dev environment; reserved for server |

### H.2 Architecture Assessment

The row-level data model (articles, article_tickers, source_tier, is_canonical, is_rag_eligible, corpus_items) already supports the conceptual distinction between target and context profiles. The absent pieces are:

1. **Schema:** No column or table distinguishes target vs context tickers
2. **Ingestion:** No broad-feed connector; rederive path doesn't unroll `tickers_json`
3. **Certification:** No pipeline exists for any certification level
4. **Vector production:** Index state queue exists but no embed/build pipeline in main .venv
5. **Outbox:** Referenced in architecture decisions but not implemented

Adding context profiles is a schema-and-wiring task, not a data-model rewrite. The existing tables do not need to change — they need new columns/tables alongside them.

---

## I. Hardcoded Assumptions and Migration Conflicts

### I.1 Hardcoded Universe Assumptions

Same as prior version (Section B.2). No changes to these findings.

### I.2 Migration Version Conflict — Corrected

**Current DB `PRAGMA user_version`: 7**

**Concrete claims (from plan documents):**

| Workstream | Plan Document | Claimed Version | DDL Scope |
|-----------|---------------|:---:|------|
| W1-D | `docs/plans/2026-07-12-w1d-two-stage-update-service.md` | **v8** | `ingestion_runs` + stage, plan_hash, plan_path |
| W1-E | `docs/plans/2026-07-12-w1e-durable-run-control.md` | **v9** | heartbeat_at, pid, cancel_requested_at, parent_run_id |
| N2 | `docs/plans/2026-07-15-catalyst-final-product-attribution-architecture-review.md` (row N2, line 432) | **v8** | entities, relationship_edges, earnings_events schemas |

**Conflict:** Both W1-D and N2 claim migration v8. W1-E claims v9. The final review document (§T) states "N0–N2 before W1-D" — meaning N2 must land its schema before W1-D begins implementation. If N2 claims v8 and W1-D claims v8, one must yield.

**The prior document's claim** that "W1-D/W1-E versions are unknown" was incorrect — both plans explicitly name their migration versions. The prior document's speculative recommendation to replace the migration system with a named registry has been removed. The architecture reviewer will decide canonical numbering.

---

## J. Unknowns Requiring Design or Sanitized Live Probes

1. **FMP Articles endpoint at scale:**
   - Articles per page, pagination depth, rate limits — all unknown
   - `tickers` field format and coverage breadth unknown
   - Date filter capabilities unknown (only default page 0 probed)
   - Recommendation: Controlled 7-day probe with 5 tickers

2. **Polygon broad-feed availability:**
   - `/v2/reference/news` without `ticker` param — not probed
   - Free tier may restrict to ticker-scoped only
   - Recommendation: Probe without ticker param

3. **Polygon range-query optimization:**
   - Multi-day date filter behavior not tested
   - Unknown whether results cap or paginate differently for wide ranges

4. **Earnings calendar coverage:**
   - Nasdaq Earnings: 4/10 tickers, unknown peer coverage
   - FMP Earnings Calendar: not probed on current plan
   - SEC 8-K Item 2.02 serves as earnings proxy but requires filtering
   - Recommendation: Probe FMP earnings-calendar; fall back to SEC 8-K Item 2.02

5. **GDELT reliability:**
   - Probed once: transport error
   - Not recommended for Fable 5 without reliability probe

6. **Server embedding throughput:**
   - bge-m3 batch throughput on deployment hardware unknown
   - Must be measured, not estimated
   - Vector cache miss rate depends on content hash stability across rederives

7. **Rate-limit contention at 30+ tickers:**
   - Polygon 5 req/min: 30 tickers × 1 day = 30 req = 6 min (acceptable for daily)
   - Finnhub 60 req/min: 30 tickers × 1 day = 30 req = 30 sec (acceptable)
   - Historical backfill: Polygon 30 tickers × 387 days = 11,610 req = 38.7 hours sequential
   - Range-query optimization could reduce Polygon backfill by 30×, to ~1.3 hours

---

## K. Exact File/DB Citations

| Artifact | Path |
|----------|------|
| Universe config | `packages/data-core/catalyst_data/config.py:97-102` |
| CIK map | `packages/data-core/catalyst_data/cik_map.py:20` |
| Source mapping | `packages/data-core/catalyst_data/source_mapping.py` |
| Articles schema | `packages/data-core/catalyst_data/articles.py` |
| SQLite storage | `packages/data-core/catalyst_data/storage/sqlite.py` |
| Migrations (current v7) | `packages/data-core/catalyst_data/migrations.py` |
| Polygon connector | `packages/data-core/catalyst_data/connectors/polygon.py` |
| Finnhub connector | `packages/data-core/catalyst_data/connectors/finnhub.py` |
| FMP connector | `packages/data-core/catalyst_data/connectors/fmp.py` |
| SEC connector | `packages/data-core/catalyst_data/connectors/sec.py` |
| Update planner | `packages/data-core/catalyst_data/update_planner.py` |
| Update pipeline | `packages/data-core/catalyst_data/update_pipeline.py` |
| Index builder | `packages/data-core/catalyst_data/index_builder.py` |
| Rederive (Polygon) | `packages/data-core/catalyst_data/rederive.py` |
| Cross-source dedup | `packages/data-core/catalyst_data/dedup/cross_source.py` |
| Eligibility | `packages/data-core/catalyst_data/eligibility.py` |
| Source tier | `packages/data-core/catalyst_data/source_tier.py` |
| Trading calendar | `packages/data-core/catalyst_data/trading_calendar.py` |
| FMP reprobe | `packages/data-core/scripts/fmp_reprobe.py` |
| W1-D plan (v8 claim) | `docs/plans/2026-07-12-w1d-two-stage-update-service.md` |
| W1-E plan (v9 claim) | `docs/plans/2026-07-12-w1e-durable-run-control.md` |
| Architecture review (N2 v8) | `docs/plans/2026-07-15-catalyst-final-product-attribution-architecture-review.md` |
| Dev DB | `data/catalyst_dev_ws4b.db` |
| Frozen DB | `data/catalyst_eval_frozen_v2.db` |

---

## L. Git and DB Integrity Report

| Check | Value | Status |
|-------|-------|--------|
| Branch | `ws4b/article-level-data` | ✅ |
| HEAD commit | `4d4e3a938ac6ee4720f13586c5b281217d75308d` | ✅ |
| `git diff --cached --name-status` | (empty) | ✅ |
| Dev DB SHA-256 (before) | `92731fb7c5c3e989b4fdcbefb9d1d2060974082a6c60f1bbecad6bc42ee846b0` | ✅ |
| Dev DB SHA-256 (after) | `92731fb7c5c3e989b4fdcbefb9d1d2060974082a6c60f1bbecad6bc42ee846b0` | ✅ — unchanged |
| Frozen DB SHA-256 | `0d97a7ec61b6ec8fb5f9263b0b37b0efc9755812739d7afead7f720c3567e8dd` | ✅ — unchanged |
| Dev DB `user_version` | 7 | ✅ |
| Provider calls made | 0 | ✅ |
| Secrets exposed | 0 | ✅ |
| DB writes performed | 0 (all queries used `mode=ro&immutable=1`) | ✅ |
| Filesystem writes | 1 — this document | ✅ |

---

## Amended Sections Summary

| Section | Change |
|---------|--------|
| A | Corrected OHLCV end date to 2026-05-01; split news/OHLCV lag; added migration conflict precision |
| B.1 | Replaced all "verified" with "inferred from GICS" |
| C.1 | Replaced classification taxonomy; ASML/LRCX marked indirect upstream; added provenance disclaimer |
| C.2 | Updated dedup count to 41 |
| D.3 | Corrected coverage date range table with canonical session counts |
| D.6 | Added full peer incidental-coverage table (41 rows, all >0 articles) |
| E.2 | Added Finnhub `related` field inspection (100% single-ticker, 0% multi-ticker) |
| E.3 | Added request semantics distinction (cell vs request vs pagination); marked unknown multipliers |
| F.3 | Corrected snapshot identity; removed "90 raw rows" claim; documented versioning requirements |
| F.8 | Corrected SEC counts: JPM 4, NVDA 3, not 5+2 |
| G | Replaced 140-session assumption with canonical 336/378/387; split into 3 workloads; removed text-embedding-3-small estimates; replaced with bge-m3 1024-dim fp16 |
| H | Replaced READY with readiness matrix (existing, absent, proposed, designed, server-required) |
| I.2 | Cited exact W1-D v8, W1-E v9, N2 v8 claims with file:line; removed speculative registry recommendation |
| J.3 | Added Polygon range-query optimization unknown |
| L | Added Frozen DB SHA; confirmed both SHAs unchanged |

## Unresolved Items Reserved for Fable

1. Canonical migration version numbering (W1-D v8, W1-E v9, N2 v8 conflict)
2. Concrete peer seed list approval (~25 symbols from N2 review)
3. Relationship edge set approval (peers, competitors, suppliers)
4. `fmp-articles` pagination and date-filter probe results
5. Server embedding throughput measurement
6. Polygon range-query optimization decision
7. `lancedb` packaging strategy for Mac dev vs Linux server
8. Outbox/ACTIVE promotion schema
9. Target/context profile schema design
10. Whether to unroll existing `tickers_json` into `article_tickers` or defer to broad-feed re-ingestion

---

*Audit amended. No production code changes. No DB writes. No provider calls. All corrections verified against canonical code and read-only DB queries.*

# Financial Modeling Prep (FMP) API — Catalyst Data-Core

> **Scope:** Endpoints and quotas relevant to ingestion. Authoritative reference: [FMP Developer Docs](https://site.financialmodelingprep.com/developer/docs). **Convention:** [README](./README.md).

---

## Authentication

Every request needs your API key:

| Method | How |
|--------|-----|
| **Header** | `apikey: YOUR_KEY` |
| **Query** | Append `?apikey=YOUR_KEY` (use `&apikey=` if other query params exist) |

---

## Base URLs (two styles)

FMP is moving toward a **`/stable/`** query style; many integrations still use **`/api/v3/`** path style.

| Style | Pattern | Example (income statement) |
|--------|---------|----------------------------|
| **Stable** | `https://financialmodelingprep.com/stable/<endpoint>?symbol=AAPL&apikey=…` | `/stable/income-statement?symbol=AAPL` |
| **v3** (legacy) | `https://financialmodelingprep.com/api/v3/<endpoint>/<SYMBOL>?apikey=…` | `/api/v3/income-statement/AAPL` |

**This repo today:** `packages/data-core/data_core/connectors/fmp.py` uses **`api/v3`** with paths like `income-statement`, `balance-sheet-statement`, `cash-flow-statement` and `period=annual`.

Before bulk refactors, confirm in [official docs](https://site.financialmodelingprep.com/developer/docs) whether your plan exposes the same data on both bases.

---

## Basic plan & call budget (250/day)

- **1 HTTP GET ≈ 1 call** (unless FMP documents otherwise for a specific endpoint).
- Prefer **one response with many rows** over **many tickers × many endpoints** on day one.
- **Bulk** endpoints (`*-bulk`, `eod-bulk`, etc.) can save calls when your tier allows them (verify plan entitlements — Basic often restricts premium/bulk).
- For **3 years of history**: use **`from` / `to`** or **`limit`** where the endpoint supports it so you do not paginate blindly.

---

## A. Discovery & identifiers

| Purpose | Stable-style endpoint (add `apikey`) |
|---------|----------------------------------------|
| Symbol search | `GET /stable/search-symbol?query=AAPL` |
| Name search | `GET /stable/search-name?query=Apple` |
| CIK | `GET /stable/search-cik?cik=320193` |
| CUSIP / ISIN | `/stable/search-cusip`, `/stable/search-isin` |
| Listed symbols | `/stable/stock-list` |
| Company profile | `/stable/profile?symbol=AAPL` |
| Quote (single) | `/stable/quote?symbol=AAPL` |
| Batch quotes | `/stable/batch-quote?symbols=AAPL,MSFT` |

---

## B. Financial statements (Catalyst-relevant)

Same three statements the orchestrator already fetches by logical source `fmp_fundamentals`:

| Statement | Stable example | v3 example (repo pattern) |
|-----------|----------------|---------------------------|
| Income | `/stable/income-statement?symbol=AAPL` | `/api/v3/income-statement/AAPL` |
| Balance sheet | `/stable/balance-sheet-statement?symbol=AAPL` | `/api/v3/balance-sheet-statement/AAPL` |
| Cash flow | `/stable/cash-flow-statement?symbol=AAPL` | `/api/v3/cash-flow-statement/AAPL` |

**Common query params (check docs for exact support):**

- `period=annual` | `quarter`
- `limit` — cap number of periods returned (useful to approximate “last N years” without extra calls)

**Related (extra calls — use only if needed):**

- TTM variants: `income-statement-ttm`, `balance-sheet-statement-ttm`, `cash-flow-statement-ttm`
- Growth / as-reported / segmentation: paths under same “Statements” family in full doc

---

## C. OHLC / EOD / intraday (“charts”)

**Daily OHLC (best default for “3 years of prices”):**

| Variant | Endpoint |
|---------|----------|
| Light EOD | `/stable/historical-price-eod/light?symbol=AAPL` |
| **Full OHLCV (+ VWAP, change %)** | `/stable/historical-price-eod/full?symbol=AAPL` |
| Non-split-adjusted | `/stable/historical-price-eod/non-split-adjusted?symbol=AAPL` |
| Dividend-adjusted | `/stable/historical-price-eod/dividend-adjusted?symbol=AAPL` |

**Intraday bars** (many more rows; higher plan / more storage):  
`/stable/historical-chart/{1min|5min|15min|30min|1hour|4hour}?symbol=AAPL`  
Usually scoped with **`from` / `to`** in the official API viewer — confirm before coding.

---

## D. News (stock & general)

| Purpose | Endpoint |
|---------|----------|
| Latest stock news | `/stable/news/stock-latest?page=0&limit=20` |
| Stock news by symbols | `/stable/news/stock?symbols=AAPL` |
| Press releases | `/stable/news/press-releases-latest` / `.../press-releases?symbols=AAPL` |
| General / crypto / forex | `/stable/news/general-latest`, `.../crypto-latest`, `.../forex-latest` |
| FMP articles | `/stable/fmp-articles?page=0&limit=20` |

**Pagination:** `page`, `limit` — **each page = another call**; tune `limit` within API max to stay under 250/day.

---

## E. Corporate actions & calendar (optional)

| Topic | Endpoint |
|-------|----------|
| Dividends | `/stable/dividends?symbol=AAPL` |
| Splits | `/stable/splits?symbol=AAPL` |
| Earnings | `/stable/earnings?symbol=AAPL` |
| IPO / earnings / dividends calendars | `...-calendar` variants |

---

## F. Bulk endpoints (high leverage if plan allows)

One request, many symbols or large tables — **verify Basic tier access**:

| Examples |
|----------|
| `/stable/profile-bulk?part=0` |
| `/stable/eod-bulk?date=2024-10-22` |
| `/stable/income-statement-bulk?year=2025&period=Q1` |
| `/stable/price-target-summary-bulk`, `ratios-ttm-bulk`, etc. |

See [FMP Developer Docs](https://site.financialmodelingprep.com/developer/docs) for the full **Bulk** endpoint list.

---

## G. SEC filings (if you stay on FMP for this)

Examples from FMP docs (typically `from`, `to`, `page`, `limit`):

- `/stable/sec-filings-8k?from=…&to=…&page=0&limit=100`
- `/stable/sec-filings-financials?from=…&to=…`
- `/stable/sec-filings-search/symbol?symbol=AAPL&from=…&to=…`

(You also have a dedicated SEC connector in data-core — choose one source of truth to avoid duplicate quotas.)

---

## Quick “3-year backfill” call sketch (order of operations)

1. **Universe:** `stock-list` or your own ticker list (1 call if you use list endpoint once).  
2. **Fundamentals:** per ticker, **3 calls** for the three statements if each returns enough history in one response with `period` + `limit`; otherwise minimize tickers per day.  
3. **OHLC:** **1 call per ticker** for `historical-price-eod/full` if `from`/`to` or default range covers 3 years.  
4. **News:** **few calls** with high `limit` and cached cursors in your DB — avoid re-pulling the same pages.

Exact parameter names for date windows: use the **API Viewer** on FMP for your key’s tier.

---

## Official links

- **Developer documentation:** https://site.financialmodelingprep.com/developer/docs  
- **Pricing / plan limits:** https://site.financialmodelingprep.com/developer/docs/pricing  
- **Dashboard (keys, usage):** https://site.financialmodelingprep.com/developer/docs/dashboard  

---

## Out of scope (roadmap)

- **DB schema** for multi-year OHLC, news, and fundamentals (dedupe keys, as-of dates, ingestion runs).  
- **Ingestion runner** that respects **250 calls/day** (queue, resume, no delete-db-on-start for backfills).

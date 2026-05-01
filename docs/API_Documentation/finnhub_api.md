> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# Finnhub REST API — Catalyst Data-Core

> **Scope:** REST v1 reference and free-tier mapping. Authoritative detail: [Finnhub API docs](https://finnhub.io/docs/api), [Pricing](https://finnhub.io/pricing). **Convention:** [README](./README.md).

---

## Base URL & docs

| Item | Value |
|------|--------|
| **REST base** | `https://finnhub.io/api/v1` |
| **Official docs** | https://finnhub.io/docs/api |
| **OpenAPI / Swagger** | Download or try-from-docs on [finnhub.io/docs/api](https://finnhub.io/docs/api) |

All example paths below are **relative** to `/api/v1` (e.g. full URL = `https://finnhub.io/api/v1/company-news`).

---

## Authentication

- **Query:** `token=YOUR_API_KEY`  
- **Header:** `X-Finnhub-Token: YOUR_API_KEY`  
- Key: **Dashboard** on finnhub.io  

Do **not** commit real keys; treat any pasted examples as **placeholders**.

---

## Rate limits

- **Free tier:** **60 API calls / minute** (per pricing page).  
- **Paid tiers:** higher per-minute caps — confirm on [Pricing](https://finnhub.io/pricing).  
- **All plans:** **30 requests / second** global cap; **HTTP 429** when exceeded.  
- **WebSocket:** 1 API key → **1 concurrent** WebSocket connection. On **Free**, pricing typically allows streaming **up to 50 symbols** (trades feed); confirm on [Pricing](https://finnhub.io/pricing).

---

## Free tier (Pricing checkmarks) → REST paths

Below matches the **feature rows you ticked on the Free column** against the usual **Finnhub REST v1** paths (verified against the public PHP client path list). **If the API returns 403 or an upgrade message, your key’s entitlements changed — trust the live response and the Pricing page, not this table.**

| Pricing / product (Free ✓) | Typical endpoint | Notes |
|----------------------------|------------------|--------|
| **Company Profile v2** | `GET /stock/profile2` | Query by `symbol`, `isin`, or `cusip`. |
| **Financials As Reported** | `GET /stock/financials-reported` | As-reported statements from filings. |
| **SEC filings** | `GET /stock/filings` | Company SEC filings (per-doc limits in doc). |
| **Company News** (1 year + updates) | `GET /company-news` | `symbol`, `from`, `to` (YYYY-MM-DD); North America. |
| **Key metrics** | `GET /stock/metric` | e.g. `metric=all` — “basic financials” / ratios style payload. |
| **WebSocket** (50 symbols) | `wss://ws.finnhub.io?token=…` | Trades stream; subscribe per symbol JSON messages. |
| **Recommendation trends** | `GET /stock/recommendation` | Analyst buy/hold/sell trend series. |
| **EPS surprises** (e.g. last 4 quarters) | `GET /stock/earnings` | Use `limit=4` (or omit for more history per doc). |
| **Earnings calendar** (1 month + US real-time per matrix) | `GET /calendar/earnings` | `from` / `to` to bound the window (keep within free window). |
| **Country’s metadata** | `GET /country` | Economic “country list + metadata” (no symbol). |

---

## What Catalyst uses today

| Endpoint | Method | Role in `finnhub.py` |
|----------|--------|----------------------|
| **`/company-news`** | GET | `symbol`, `from`, `to` (YYYY-MM-DD); returns article list; connector uses ~7-day window and caps articles. |

**Free tier note (doc):** company news includes **~1 year** of history + updates (North American companies).

---

## Highest-value endpoints (by use case)

### Market data (US)

| Endpoint | Notes |
|----------|--------|
| **`/quote`** | Real-time US quote; **avoid tight polling** — use websocket if you need streaming. |
| **`/stock/candle`** | OHLCV; **`from` / `to` as UNIX seconds**; `resolution` = `1,5,15,30,60,D,W,M`. **Doc labels this as Premium** — confirm your plan before relying on it for 3-year OHLC; otherwise use **FMP** (or another source) for candles. |

### News & sentiment

| Endpoint | Notes |
|----------|--------|
| **`/company-news`** | Per-symbol, date range (**required**). |
| `/market-news` | General market news (see doc for params). |
| `/news-sentiment`, `/press-releases` | **Premium** in pricing matrix. |

### Discovery & status

| Endpoint | Notes |
|----------|--------|
| `/stock/symbol` | Search / resolve symbols. |
| `/market-status` | Exchanges open/closed. |
| `/calendar/earnings` | Earnings calendar — **often included on Free** (narrow date window per pricing). |
| `/calendar/ipo`, others | Many calendars are **Premium** — check docs + your plan. |

### Fundamentals

Beyond the **Free-tier table** above, other items under **Stock Fundamentals** / **Alternative Data** may still be **Premium**. Always verify in [Pricing](https://finnhub.io/pricing) and with a live `GET`.

---

## WebSocket (optional)

- **URL:** `wss://ws.finnhub.io?token=YOUR_KEY`  
- **Trades:** subscribe `{"type":"subscribe","symbol":"AAPL"}` — Free tier typically **up to 50 symbols** (see [Pricing](https://finnhub.io/pricing)).  
- **Dedicated news WebSocket stream:** **Premium** (US/Canada) in the doc matrix — do not confuse with REST `/company-news`.

Does not replace REST backfills; useful for live dashboards only.

---

## Official links

- **API documentation:** https://finnhub.io/docs/api  
- **Pricing:** https://finnhub.io/pricing  
- **Dashboard (API key):** https://finnhub.io/dashboard  

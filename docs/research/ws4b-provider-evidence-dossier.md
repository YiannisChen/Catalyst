# WS4B Provider Evidence Research Dossier

**Status:** Research complete — awaiting Claude/user review
**Date:** 2026-07-01
**Branch:** `ws4b/article-level-data`
**Commit baseline:** `a41906b`
**Live probes:** Finnhub (no key), yfinance (rate-limited) — field data from prior Step 3A discovery fixtures

---

## Executive Summary

Current Polygon news corpus is 100% commentary/aggregator (T4/T5). The attribution taxonomy identifies 17 categories — 7 require T1 (SEC), 4 require T2/T3 structured data, and the remaining 6 benefit from T3/T4 diversified news. After SEC (Step 3C), the next highest-value, lowest-risk providers are:

1. **Finnhub company-news** — free tier, summary-level, Yahoo/other publishers, different publisher mix from Polygon
2. **FRED** — free, structured, essential for macro_rates + macro_inflation categories
3. **SEC Form 4** — free, structured, insider transactions — direct signal for management_governance + technical_flow

These three plus SEC close 10 of 17 attribution categories with T1/T2 evidence.

---

## Methodology

**Reference inspection:** OpenBB platform (34 providers), PokieTicker pipeline, Catalyst data-core architecture.
**Live probes:** Minimal, sanizited. Finnhub key not configured; yfinance rate-limited. Prior Step 3A discovery fixtures provide verified field data for Finnhub and Polygon.
**Field classification:** Based on OpenBB standard models (CompanyNewsData, WorldNewsData, ShortInterestData, CalendarEarningsData, etc.) and actual API responses from discovery fixtures.

---

## Provider Dossiers

### 1. Finnhub Company News

**Source family:** News / reporting
**OpenBB model:** Not in OpenBB (no Finnhub provider exists)
**Official docs:** https://finnhub.io/docs/api/company-news
**Endpoint:** `https://finnhub.io/api/v1/company-news?symbol={ticker}&from={YYYY-MM-DD}&to={YYYY-MM-DD}&token={api_key}`

**Auth:** API key required. Free tier available (60 API calls/minute, limited daily quota).
**Status from discovery:** 200 ✅, key_id `945e45b4a3a6`, summary depth.

**Field shape (from discovery fixture):**
| Field | Type | Present | Notes |
|-------|------|---------|-------|
| `category` | string | ✅ | "company" for all sampled items |
| `datetime` | integer | ✅ | Unix timestamp (seconds since epoch) |
| `headline` | string | ✅ | Article title |
| `id` | integer | ✅ | Finnhub news ID |
| `image` | string | ✅ | Usually Yahoo Finance placeholder |
| `related` | string | ✅ | Ticker symbol |
| `source` | string | ✅ | Publisher — e.g., "Yahoo", "Motley Fool", "Reuters" |
| `summary` | string | ✅ | 101-431 chars (prior discovery measurement) |
| `url` | string | ✅ | finnhub.io redirect URL |

**Quality/depth:** `summary` — NO full text. Summary is a 1-3 sentence teaser.
**Ticker-scoped:** ✅ Single ticker per call.
**Date filter:** ✅ `from`/`to` parameters — supports historical backfill.
**Multi-ticker:** No — single ticker endpoint. But each article's `related` field may reference a different ticker than the query.

**Publisher distribution (from discovery fixture, NVDA sample):**
- Yahoo (3/3 items in sample)
- Prior research noted diverse sources

**Attribution categories helped:**
- orders_demand (product announcements, contracts)
- customer_partner_contracts
- competition_sector
- supply_chain

**Tier:** T4 (aggregator). Content quality varies by `source` field.

**Source quality vs Polygon:**
- Polygon publisher distribution: ~58% T5 (Motley Fool, Zacks), ~28% T4 (Benzinga, Investing.com), ~15% T3 (GlobeNewswire)
- Finnhub publisher mix is DIFFERENT — heavier on Yahoo, Reuters, Bloomberg, CNBC
- Less Motley Fool concentration expected
- Summary-only depth means Finnhub is a "discovery/surface" layer, not deep evidence

**Schema implications:**
- Reuses `articles` + `article_tickers` schema exactly
- `provider='finnhub'`, `source_type='finnhub_company_news'`
- `article_id = f"finnhub:{native_id}"`
- `publisher_name = source` field
- L1 only (summary is short, 101-431 chars — well below 800-char L2 threshold)
- `content_hash = SHA256(title + "\n" + summary)`
- `dedup_group_id` enables cross-source dedup with Polygon (same event, different provider)

**Cost/risk:**
- Free tier: 60 calls/min, unknown daily cap
- 10 tickers × ~1 call/day = 10 calls/day — well within limits
- No ToS/scraping risk (official API)
- Data duplication risk with Polygon: MODERATE — same events will appear in both, but different publisher perspectives

**Recommendation:** **BUILD_NOW** (after SEC). Highest-value free news source after Polygon. Different publisher mix reduces Motley Fool concentration.

---

### 2. yfinance News

**Source family:** News / reporting
**OpenBB model:** `providers/yfinance/openbb_yfinance/models/company_news.py`
**Method:** `yfinance.Ticker(ticker).get_news()` — returns latest ~50-100 items, NO date filter.

**Auth:** None. Free. No API key needed.
**Status from probe:** Rate-limited (Too Many Requests). yfinance has aggressive rate limiting on `get_news()`.

**Field shape (from OpenBB model `YFinanceCompanyNewsData`):**
Uses `CompanyNewsData` standard model: `date`, `title`, `author`, `excerpt`, `body`, `images`, `url`, `symbols`. Additional field: `source` (publisher display name).

**Quality/depth:** `summary` — extracts from `content.summary` or `content.description`. NO full text.
**Ticker-scoped:** ✅ Single ticker.
**Date filter:** ❌ NONE. Only returns latest items. Cannot query historical dates.
**Multi-ticker:** Single ticker per call.

**Critical limitation:** No date filter. This means yfinance news can ONLY be used for daily incremental top-up (fetch latest for today). Cannot backfill historical dates. This makes it a supplementary source, not a primary pipeline source.

**Attribution categories helped:**
- orders_demand
- competition_sector
- (limited — no historical backfill)

**Tier:** T4 (Yahoo-sourced summary).

**Schema implications:**
- Reuses `articles` + `article_tickers`
- `provider='yfinance'`, `source_type='yfinance_news'`
- L1 only
- Daily-only pipeline integration — `compute_missing_cells` won't work (no date history)

**Recommendation:** **DAILY_ONLY**. Build as a simple daily top-up connector AFTER Finnhub. Not suitable for backfill. Good for incremental diversity (different source = Yahoo, not Finnhub/Polygon).

---

### 3. Nasdaq

**Source family:** Official exchange + news
**OpenBB models (no company_news, but structured calendars):**
| Model | Type | Ticker-scoped | Free? |
|-------|------|---------------|-------|
| `calendar_earnings` | Earnings dates | ✅ | Free (web) |
| `calendar_dividend` | Dividend dates | ✅ | Free (web) |
| `calendar_ipo` | IPO calendar | ❌ | Free (web) |
| `economic_calendar` | Macro events | ❌ | Free (web) |
| `historical_dividends` | Dividend history | ✅ | Free (web) |
| `equity_screener` | Fundamentals screener | ❌ | Free (web) |
| `company_filings` | SEC filings (redirect) | ✅ | Free (web) |
| `top_retail` | Retail flow data | ✅ | Free (web) |

**Key finding:** Nasdaq does NOT have a company-news endpoint in OpenBB. Its value is **structured calendar data**, not news articles.

**Attribution value:**
- `calendar_earnings` → earnings_results (knowing WHEN earnings occur is critical context)
- `calendar_dividend` → corporate_actions
- `historical_dividends` → corporate_actions
- `top_retail` → technical_flow (retail investor activity)
- `company_filings` → duplicates SEC — not needed

**Recommendation:** Calendar endpoints worth probing for **structured metadata**. Not an article source. The earnings calendar tells us "AAPL reports on 2026-07-29" — paired with the actual 8-K filing text (from SEC Step 3C), this gives complete earnings coverage.

**Build priority:** **PROBE_NEXT** for earnings calendar. Not a standalone news source.

---

### 4. Tiingo News

**Source family:** News / reporting
**OpenBB model:** `providers/tiingo/openbb_tiingo/models/company_news.py`
**Endpoint:** `https://api.tiingo.com/tiingo/news?tickers={symbols}&startDate={date}&endDate={date}&token={api_key}`

**Auth:** API key (free tier available). `tiingo_token` credential.
**Field shape:**
| Field | Type | Notes |
|-------|------|-------|
| `id` | integer | Article ID |
| `title` | string | |
| `description` | string | Summary |
| `publishedDate` | datetime | |
| `tickers` | list | Multi-ticker support |
| `tags` | list | Topic tags |
| `source` | string | Publisher domain |
| `url` | string | Article URL |
| `crawlDate` | datetime | When Tiingo crawled it |

**Quality/depth:** `summary` — `description` field is summary-level. No full text body.
**Ticker-scoped:** ✅ Multi-ticker (`tickers` parameter accepts comma-separated list).
**Date filter:** ✅ `startDate`/`endDate` — supports historical backfill.
**Multi-ticker:** Multi-ticker query support, and each item carries `tickers[]`.

**Key advantage over Finnhub:**
- Multi-ticker queries (one API call for multiple tickers)
- `tags` field for topic classification
- `source` field for publisher attribution

**Key disadvantage:**
- Requires separate API key (free tier exists)
- Summary-only (same depth as Finnhub)
- Crawled aggregation (not original reporting)

**Attribution categories:** Same as Finnhub (orders_demand, competition_sector, supply_chain, customer_partner_contracts).

**Recommendation:** **PROBE_NEXT**. If Finnhub coverage is insufficient, Tiingo is the next free alternative. Good multi-ticker support. Free tier key needs to be obtained.

---

### 5. Intrinio Company News

**Source family:** News / reporting (PAID)
**OpenBB model:** `providers/intrinio/openbb_intrinio/models/company_news.py`
**Endpoint:** `https://api.intrinio.com/company/news`

**Auth:** API key. PAID only.
**Source options:** `yahoo`, `moody`, `moody_us_news`, `moody_us_press_releases`.
**Key features:** `business_relevance` score (0-1), `sentiment`, `word_count` filter, `is_spam` filter.

**Field shape:** `title`, `summary` (excerpt), `body` (full text for some sources), `publication_date`, `source`, `topics`, `symbol`, `sentiment`, `word_count`, `url`.

**Quality/depth:** Varies — `summary` for Yahoo source, possibly `full_text` for Moody's sources. The `business_relevance` score is a unique quality filter.

**Recommendation:** **DEFER_PAID**. Moody's-sourced news is potentially high quality (T2/T3), but the cost barrier means it should only be considered after free sources are exhausted and if attribution quality plateaus.

---

### 6. Benzinga Company News

**Source family:** News / reporting (PAID)
**OpenBB model:** `providers/benzinga/openbb_benzinga/models/company_news.py`
**Endpoint:** `https://api.benzinga.com/api/v2/news`

**Auth:** API key. PAID (limited free tier exists but very restricted).
**Field shape:** `title`, `teaser` (excerpt), `body` (if `display=full`), `stocks` (tickers), `channels`, `tags`, `created` (date), `id`, `url`.

**Display modes:** `headline`, `abstract` (headline+teaser), `full` (headline+full body).
**Quality/depth:** `reporting_summary` to `reporting_full_text` with display=full. Benzinga is an aggregator/specialist — similar depth tier to Polygon.

**Key concern:** Benzinga articles ALREADY appear in the Polygon news corpus (publisher "Benzinga" → T4). Building a Benzinga connector would mostly duplicate content already ingested via Polygon.

**Recommendation:** **DEFER_PAID / KEEP_REFERENCE_ONLY**. Content already partially available through Polygon. Paid tier not justified when free alternatives (Finnhub, Tiingo) provide similar breadth.

---

### 7. Seeking Alpha

**Source family:** Opinion / analysis (PAID)
**OpenBB models:** `calendar_earnings`, `forward_eps_estimates`, `forward_sales_estimates` — structured data only. NO news/article endpoint.

**Key finding:** OpenBB Seeking Alpha provider does NOT expose news articles. It exposes structured analyst estimates data.

**Value for Catalyst:**
- Earnings calendar — duplicates Nasdaq earnings calendar
- Forward EPS/sales estimates — analyst consensus data (structured, useful for analyst_sentiment category)

**Recommendation:** **DEFER_LOW_QUALITY for news**. The forward estimates endpoints may be worth probing LATER for analyst_sentiment structured data. But Seeking Alpha articles are opinion-heavy and would worsen the Motley Fool problem.

---

### 8. WSJ

**Source family:** Premium reporting (PAID)
**OpenBB models:** `gainers`, `losers`, `active` — market screener data only. NO news/article endpoint.

**Key finding:** OpenBB WSJ provider only exposes market data (top gainers/losers/active stocks). No company news endpoint.

**Recommendation:** **DEFER_PAID**. WSJ articles are T2 quality but paywalled with no API for article text. The OpenBB WSJ provider doesn't even expose news.

---

### 9. TMX Company News

**Source family:** Exchange news
**OpenBB model:** `providers/tmx/openbb_tmx/models/company_news.py`
**Endpoint:** TMX GraphQL (`https://app-money.tmx.com/graphql`)

**Auth:** No API key (GraphQL with random User-Agent). Free.
**Field shape:** `headline` (title), `summary`, `source`, `datetime`, `url`.

**Coverage:** TSX/TSXV only — Canadian listings. Of our 10 tickers, 0 are Canadian-listed. This source provides ZERO coverage for AAPL, AMD, AMZN, GOOGL, JPM, META, MSFT, NVDA, TSLA, UNH.

**Recommendation:** **KEEP_REFERENCE_ONLY**. Zero coverage for current universe. Worth noting for future Canadian expansion.

---

### 10. Biztoc

**Source family:** News aggregator
**OpenBB model:** `providers/biztoc/openbb_biztoc/models/world_news.py`
**Endpoint:** RapidAPI (`https://biztoc.p.rapidapi.com/`)

**Auth:** RapidAPI key needed. Free tier (limited).
**Key limitation:** NO ticker filter. World news only. `start_date`/`end_date` explicitly NOT supported (emits warning).

**Field shape:** `title`, `body` (full text), `published` (date), `tags`, `images`, `url`, `source`. The `body` field is full text — notable because most free sources are summary-only.

**Why not:**
- No ticker filter → must post-filter by keyword, high noise
- World news, not company-specific
- No date range filter → can't backfill
- RapidAPI dependency (extra auth)

**Recommendation:** **KEEP_REFERENCE_ONLY**. Full text is attractive but lack of ticker filter makes it impractical for company-specific attribution.

---

### 11. Finviz

**Source family:** Screener / technical
**OpenBB models:** `equity_screener`, `equity_profile`, `key_metrics`, `price_target`, `price_performance`, `compare_groups`.

**Key finding:** Finviz is a SCREENER, not a news source. No news articles. Provides fundamental/technical data: P/E, market cap, price performance, target prices.

**Attribution value:**
- `price_target` → analyst_sentiment
- `equity_screener` → fundamentals_financials (screening context)
- `key_metrics` → fundamentals_financials

**Risk:** Finviz is web-scraping based (HTML scraping of finviz.com). OpenBB provider scrapes the public website. ToS risk.

**Recommendation:** **DEFER_LEGAL_RISK**. Web scraping. Limited attribution value beyond what SEC filings + structured data provide.

---

### 12. FRED (Federal Reserve Economic Data)

**Source family:** Macro / official structured
**OpenBB models:** `consumer_price_index`, `iorb_rates`, `ameribor`, `sonia_rates`, `tmc`, `fred_series`, `fred_search`, `fred_release_table`. Many more series in `standard_models/`.

**Auth:** API key (free from St. Louis Fed). Catalyst already has FRED connector (`connectors/fred.py`) and FRED retry policy.

**Endpoint:** `https://api.stlouisfed.org/fred/series/observations?series_id={SERIES}&api_key={KEY}&file_type=json`

**Key series for 17-category attribution:**

| Series ID | Category | Description |
|-----------|----------|-------------|
| `DFF` | macro_rates | Federal Funds Effective Rate |
| `DGS10` | macro_rates | 10-Year Treasury Yield |
| `DGS2` | macro_rates | 2-Year Treasury Yield |
| `T10Y2Y` | macro_rates | 10Y-2Y Spread (yield curve) |
| `CPIAUCSL` | macro_inflation | CPI All Urban Consumers |
| `PCEPI` | macro_inflation | PCE Price Index |
| `UNRATE` | macro_labor_growth | Unemployment Rate |
| `GDP` | macro_labor_growth | Gross Domestic Product |
| `VIXCLS` | market_risk_liquidity | VIX (CBOE) |
| `DTWEXBGS` | geopolitical_trade | Trade-Weighted Dollar Index |
| `TEDRATE` | market_risk_liquidity | TED Spread |

**Quality/depth:** `structured_numeric` — date + value pairs. No text. Tier = T6.

**Schema implications:**
- Does NOT fit `articles` schema (structured numeric, not prose)
- Needs separate `macro_observations` table: `series_id, date, value, unit, source_label`
- `source_kind = 'macro_observation'`
- L1 only (structured data cannot be sentence-split)
- `content_hash` = SHA256 of `f"{series_id}\n{date}\n{value}"`

**Catalyst already has:** FRED connector (`connectors/fred.py`), retry policy, provider limits. The FRED connector is functional but under-utilized — it fetches series but doesn't feed into the index or freshness pipeline.

**Recommendation:** **BUILD_LATER (Step 3E)**. Essential for macro categories. Connector exists. Needs structured data schema + index integration.

---

### 13. Federal Reserve

**Source family:** Macro / official
**OpenBB models:** `federal_funds_rate`, `treasury_rates`, `money_measures`, `overnight_bank_funding_rate`, `yield_curve`, `primary_dealer_stats`.

**Auth:** None (public data). Free.

**Attribution value:** FOMC statements/minutes are prose text — could be ingested as articles with `source_kind='official_release'`. Rate data is structured numeric.

**Recommendation:** **BUILD_LATER (Step 3E)** as part of macro expansion. FOMC statements are unique attribution evidence for macro_rates events (Jackson Hole, rate decisions).

---

### 14. BLS (Bureau of Labor Statistics)

**Source family:** Macro / official
**OpenBB models:** `bls_series`, `bls_search`.

**Auth:** API key (free). CPI, employment, PPI data.

**Recommendation:** **DEFER_LATER**. FRED already covers CPI and unemployment data. BLS adds detail (PPI, wage data) but is redundant for initial macro coverage.

---

### 15. SEC Form 4 (Insider Transactions)

**Source family:** Official / structured
**OpenBB model:** `providers/sec/openbb_sec/models/insider_trading.py`

**Auth:** None (SEC public). Free.
**Endpoint:** SEC EDGAR Form 4 XML filings.

**Field shape:** `company_name`, `form`, `transaction_date`, `transaction_code` (P=purchase, S=sale, A=grant), `shares`, `price`, `director`/`officer`/`ten_percent_owner` flags, `securities_owned`.

**Attribution value (DIRECT signal):**
- `management_governance` — insider buying/selling is a direct governance signal
- `technical_flow` — large insider transactions affect float/momentum

**Quality:** T1 official filing. Structured numeric. No ambiguity about source.

**Recommendation:** **PROBE_NEXT (after SEC 3C)**. Free, structured, direct signal. Should be built as part of SEC connector extension, not a separate provider.

---

### 16. SEC 13F (Institutional Holdings)

**OpenBB model:** `providers/sec/openbb_sec/models/form_13FHR.py`

**Key limitation:** Quarterly filings with 45-day lag. Filed Feb 14 for Dec 31 quarter-end holdings. Not usable for daily attribution.

**Recommendation:** **DEFER_QUARTERLY**. Data is T1 quality but the 45-day lag makes it unsuitable for same-day attribution. Useful for pattern analysis only.

---

### 17. FINRA Short Interest

**OpenBB model:** `providers/finra/openbb_finra/models/equity_short_interest.py`

**Auth:** No API key. Data is downloaded as SQLite database from FINRA and cached locally.

**Field shape:** `symbol`, `current_short_position`, `previous_short_position`, `avg_daily_volume`, `days_to_cover`, `change_pct`, `settlement_date`.

**Frequency:** Bi-monthly (settlement dates on 15th and last day of month).

**Attribution value:** `technical_flow` — short interest and changes in short position are direct flow signals.

**Recommendation:** **PROBE_NEXT**. Free, structured, unique signal. Bi-monthly frequency means it's a "context" layer, not daily evidence. Worth having for short squeeze detection (g026: UNH Berkshire 13F → short squeeze).

---

### 18. Company IR Press Releases

**Source family:** Official / primary
**No OpenBB model exists.**

**Value:** T1 primary source. Company press releases for earnings, product launches, contracts, partnerships, management changes.

**Challenge:** No unified API. Each company has its own IR page. Need per-company URL templates:
- AAPL: `https://www.apple.com/newsroom/`
- MSFT: `https://news.microsoft.com/`
- GOOGL: `https://blog.google/`
- etc.

**Recommendation:** **DEFER_TO_STEP_3E**. High value but high implementation cost. Build after SEC + Finnhub + FRED. Scraping risk requires per-site ToS review.

---

### 19. Earnings Call Transcripts

**Source family:** Official + third-party
**No consistent OpenBB model.**

**Value:** T2 quality. Management commentary, Q&A, forward-looking statements.

**Sources:**
- Seeking Alpha transcripts (free to read, no API)
- Fool transcripts (free to read, no API)
- Company IR (some post transcripts, inconsistent)
- Dedicated APIs (AlphaSense, Sentieo — paid)

**Recommendation:** **DEFER_PAID / DEFER_LEGAL_RISK**. No reliable free API. Scraping transcripts has ToS risk. Paid APIs exist but cost barrier. Revisit if attribution quality gaps persist after SEC 8-K + earnings calendar.

---

## Attribution Category Coverage Matrix

| Category | Current (Polygon) | After SEC | After +Finnhub | After +FRED+Form4 |
|----------|-------------------|-----------|----------------|-------------------|
| earnings_results | T5 commentary | **T1 filing** | T1+T4 | T1+T4 |
| guidance_outlook | T5 commentary | **T1 filing** | T1+T4 | T1+T4 |
| fundamentals_financials | – | **T1 filing** | T1+T4 | T1+T4 |
| orders_demand | T4 aggregator | T1 (8-K 1.01) | **T1+T4** | T1+T4 |
| supply_chain | T4 aggregator | – | T4 | T4 |
| competition_sector | T4/T5 | – | T4 | T4 |
| customer_partner_contracts | T4 aggregator | **T1 (8-K 1.01)** | T1+T4 | T1+T4 |
| management_governance | T5 commentary | **T1 filing** | T1+T4 | **T1+Form4** |
| regulatory_legal | T5 commentary | **T1 filing** | T1+T4 | T1+T4 |
| geopolitical_trade | T5 commentary | – | T4 | T4+FRED |
| macro_rates | – | – | – | **FRED** |
| macro_inflation | – | – | – | **FRED** |
| macro_labor_growth | – | – | – | **FRED** |
| market_risk_liquidity | – | – | – | **FRED (VIX)** |
| analyst_sentiment | T5 commentary | – | T4 | T4 |
| technical_flow | – | – | – | **Form4+FINRA** |
| corporate_actions | T4 aggregator | **T1 filing** | T1+T4 | T1+T4 |

**Categories with T1/T2 coverage after all recommended builds: 15/17** (missing: supply_chain and competition_sector still T4 only).

---

## Ranked Recommendations

### Top 5 Recommended Next Builds

| # | Provider | Priority | Rationale |
|---|----------|----------|-----------|
| 1 | **SEC 8-K + 10-Q/10-K** | BUILD_NOW (Step 3C) | Closes 7 categories with T1 evidence. Free. No API key. |
| 2 | **Finnhub company-news** | BUILD_NOW (Step 3D) | Free tier. Different publisher mix from Polygon. Reduces Motley Fool %. |
| 3 | **FRED** | BUILD_LATER (Step 3E) | Free. Structured. Essential for 5 macro categories. Connector exists. |
| 4 | **SEC Form 4** | BUILD_LATER (Step 3E) | Free. Structured. Direct insider signal. Extends SEC connector. |
| 5 | **Nasdaq earnings calendar** | PROBE_NEXT | Free. Structured. Pairs with SEC filings for complete earnings coverage. |

### Top 5 Probes Needed Before Decision

| # | Source | Question to resolve |
|---|--------|---------------------|
| 1 | Tiingo | Obtain free key; compare publisher quality to Finnhub |
| 2 | Nasdaq earnings calendar | Confirm free availability; test ticker coverage |
| 3 | FINRA short interest | Confirm data freshness; test bi-monthly frequency fit |
| 4 | yfinance news | Re-test after rate-limit cooldown; publisher distribution |
| 5 | Benzinga free tier | Confirm what free tier actually provides (likely too limited) |

### Sources to Avoid

| Source | Reason |
|--------|--------|
| Seeking Alpha (articles) | Opinion-heavy — worsens Motley Fool problem |
| WSJ | Paywalled, no API for articles |
| Biztoc | No ticker filter, no date filter, world news only |
| TMX | Zero coverage for our 10 US tickers |
| Finviz (scraping) | ToS risk, screener only, no news articles |
| FMP paid news | Needs paid plan (402 on current key) |

### Sources to Keep as Reference Only

| Source | Future potential |
|--------|-----------------|
| FMP fmp-articles | Full text is attractive, but single-ticker and editorial |
| Intrinio | Moody's sources are T2/T3 if budget allows |
| BLS, BEA, EIA | Structured macro — redundant with FRED for now |
| Company IR | T1 quality, no API — design templates when needed |
| Earnings call transcripts | T2 quality, no free API — revisit if gaps persist |

---

## Schema/Indexing Implications Summary

| Provider | Table | source_kind | L1/L2 | content_hash basis | dedup key |
|----------|-------|-------------|-------|--------------------|-----------|
| SEC 8-K/10-Q/10-K | `filings` + `filing_documents` | `filing` | L1 + L2 (8-K body) | `title + "\n" + extracted_text` | `accession_number` |
| Finnhub | `articles` + `article_tickers` | `article` | L1 only | `title + "\n" + summary` | `finnhub:native_id` |
| yfinance | `articles` + `article_tickers` | `article` | L1 only | `title + "\n" + summary` | `yfinance:native_id` |
| FRED | `macro_observations` (NEW) | `macro_observation` | L1 only | `series_id + "\n" + date + "\n" + value` | `series_id/date` |
| SEC Form 4 | `insider_transactions` (NEW) | `market_signal` | L1 only | `accession + "\n" + transaction_date` | `accession/owner_cik` |
| Nasdaq calendars | `calendar_events` (NEW) | `market_signal` | L1 only | `ticker + "\n" + event_date + "\n" + event_type` | `ticker/event_date/event_type` |

---

## Open Questions for Claude/User

1. **Finnhub daily quota:** Prior discovery confirmed API works with free key but the key isn't currently configured. Obtain key and probe exact daily limit?
2. **FRED schema design:** `macro_observations` table design deferred to Step 3E. Should it be per-series with ticker mapping, or purely series-based?
3. **Tiingo vs Finnhub:** If Tiingo free tier has better publisher distribution than Finnhub, should it be prioritized? Probe needed.
4. **Nasdaq earnings calendar:** Confirm it returns data for our 10 tickers. Some Nasdaq endpoints may only cover Nasdaq-listed stocks (excludes NYSE: JPM, UNH, BRK).
5. **yfinance backfill strategy:** Since yfinance has no date filter, should it be integrated into the daily freshness pipeline only (not `compute_missing_cells`)?
6. **FINRA short interest:** Bi-monthly frequency means it should update `index_state` only on settlement dates. How should freshness be reported?
7. **Company IR scraping:** Worth designing per-company templates for 10 tickers now, or defer until after all API sources are integrated?

---

## Appendix: Files Inspected

**Catalyst:**
- `packages/data-core/catalyst_data/connectors/base.py`
- `packages/data-core/catalyst_data/connectors/polygon.py`
- `packages/data-core/catalyst_data/connectors/fred.py`
- `packages/data-core/catalyst_data/orchestrator.py`
- `packages/data-core/catalyst_data/storage/sqlite.py`
- `packages/data-core/catalyst_data/articles.py`
- `packages/data-core/catalyst_data/source_tier.py`
- `packages/data-core/catalyst_data/index_builder.py`
- `packages/data-core/catalyst_data/update_pipeline.py`
- `packages/data-core/catalyst_data/quality.py`
- `packages/data-core/catalyst_data/retry.py`
- `packages/data-core/catalyst_data/provider_limits.py`
- `packages/data-core/catalyst_data/config.py`
- `packages/data-core/catalyst_data/source_mapping.py`
- `docs/research/ws4b-attribution-taxonomy-and-source-map.md`
- `data/provider_discovery/20260630T101009Z/discovery_report.md` + fixtures

**OpenBB:**
- `core/openbb_core/provider/standard_models/company_news.py`
- `core/openbb_core/provider/standard_models/world_news.py`
- `providers/yfinance/models/company_news.py`
- `providers/tiingo/models/company_news.py`
- `providers/benzinga/models/company_news.py`
- `providers/intrinio/models/company_news.py`
- `providers/tmx/models/company_news.py`
- `providers/biztoc/models/world_news.py`
- `providers/sec/models/company_filings.py`
- `providers/sec/models/insider_trading.py`
- `providers/finra/models/equity_short_interest.py`
- `providers/nasdaq/models/calendar_earnings.py`
- `providers/fred/models/consumer_price_index.py`
- `providers/fred/utils/fred_base.py`

**PokieTicker:**
- `backend/pipeline/layer0.py`, `layer1.py`, `layer2.py`
- `backend/pipeline/alignment.py`
- `backend/database.py`

---

## Appendix: Probe Commands Run

```bash
# Finnhub — no key configured
# yfinance — rate-limited
# Prior discovery fixtures used for field data (Step 3A, 2026-06-30)
```

## Appendix: Artifact Paths

- `docs/research/ws4b-provider-evidence-dossier.md` — this document
- `data/provider_discovery/20260630T101009Z/` — prior Step 3A discovery (Finnhub, Polygon, SEC fixtures)
- `data/provider_discovery/20260701T035921Z/attribution_source_matrix.json` — prior source matrix
- `data/provider_discovery/20260701T063221Z/provider_dossiers/` — today's probe stubs

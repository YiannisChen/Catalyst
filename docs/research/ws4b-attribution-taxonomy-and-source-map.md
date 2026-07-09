# WS4B Step 3R — Attribution Taxonomy + Evidence Source Map

**Status:** Draft for Claude/user review — NOT implemented
**Date:** 2026-07-01
**Branch:** `ws4b/article-level-data`
**Commit baseline:** `a41906b` (Step 2.1 fix)

---

## 0. Executive Summary

Catalyst's current corpus is 100% Polygon news — Motley Fool, Zacks, Benzinga, GlobeNewswire. The golden set evaluation reveals that the **Critic rejects 6–8 out of 8 chunks as below-threshold for 3/10 sampled events** (`g015`, `g046`, `g049`). The root cause is not the retrieval pipeline — it's that **the evidence corpus itself lacks primary and structured sources for the attribution categories the golden set tests.**

This document defines a detailed 17-category attribution taxonomy, maps each to the best available evidence sources (avoiding commentary/aggregator pollution), and recommends a prioritized build sequence. The first 3 builds are: **SEC 8-K full-text → Finnhub company-news → company IR/earnings structured data**. Together these close the 3 biggest coverage gaps: regulatory/legal, earnings/fundamentals, and orders/partnerships.

---

## Part A — Detailed Attribution Taxonomy

### A.1 Current State (6 Categories)

From `packages/eval/golden_set/README.md` and `annotation_template.md`:

| Category | Golden Set Events | Dominant Evidence Type | Current Source Quality |
|----------|-------------------|----------------------|----------------------|
| `earnings` | 16/50 (32%) | Earnings results, guidance, margin compression | Polygon commentary — NOT primary filings |
| `macro` | 8/50 (16%) | Rate decisions, tariff pauses, consumer health | Polygon commentary + macro roundups |
| `geopolitical` | 8/50 (16%) | Tariff shocks, trade war, China exposure | Polygon commentary on tariff headlines |
| `sector` | 13/50 (26%) | AI capex, EV competition, DeepSeek, credit cycle | Polygon commentary + aggregator |
| `regulatory` | 7/50 (14%) | DOJ probe, antitrust ruling, export controls | Polygon commentary — NOT legal filings |
| `technical` | 3/50 (6%) | Short squeeze (13F), momentum ($4T), insider buy | Polygon commentary |

**Key finding:** 5 of 6 categories rely entirely on Polygon commentary for evidence. The Critic correctly rejects this commentary as low-relevance when the event requires primary evidence (e.g., earnings miss needs the actual filing/transcript; DOJ probe needs the actual complaint/settlement).

### A.2 Proposed Attribution Taxonomy (17 Categories)

Each category defined with: description, golden set examples, strong vs weak evidence criteria, preferred source tier, and evidence type classification.

---

#### 1. `earnings_results`
**Description:** Quarterly/annual financial results — revenue, EPS, margins, segment performance, beats/misses vs consensus.
**Golden set examples:** `g001` (TSLA Q4 delivery miss), `g004` (TSLA Q1 miss), `g021` (UNH MLR miss), `g034` (META Q2 beat), `g040` (AMZN AWS miss).
**Strong evidence:** 8-K earnings release (primary), 10-Q section, earnings call transcript (management commentary, guidance numbers).
**Weak evidence:** Aggregator articles rewording the press release, commentary that "investors were disappointed."
**Preferred tier:** T1 (SEC filings) for the numbers; T2 (earnings call transcript, premium press) for management commentary.
**Evidence type:** Structured data (EPS/revenue/margin numbers) + official filing + transcript.

---

#### 2. `guidance_outlook`
**Description:** Forward guidance — raise, cut, suspension, management outlook, capex guidance.
**Golden set examples:** `g023` (UNH guidance cut from $30 to $26-26.50), `g024` (UNH suspended 2025 outlook), `g035` (META raised capex to $70B).
**Strong evidence:** 8-K item 2.02/7.01 (guidance filed with SEC), earnings call transcript (forward-looking statements section).
**Weak evidence:** Analyst speculation about guidance, second-hand interpretation.
**Preferred tier:** T1 (SEC 8-K).
**Evidence type:** Official filing + structured forward estimates.

---

#### 3. `fundamentals_financials`
**Description:** Balance sheet, cash flow, debt, buyback authorization, dividend changes, capital allocation, segment restructuring.
**Golden set examples:** `g035` (META $15.93B tax charge), `g040` (AMZN FCF drop from $53B to $18.2B).
**Strong evidence:** 10-Q/10-K balance sheet and cash flow statements, 8-K item 8.01 for buyback/dividend, Form 4 for insider transactions.
**Weak evidence:** Commentary articles without citing specific numbers.
**Preferred tier:** T1 (SEC) for filings; T2 for analysis.
**Evidence type:** Structured financial data + official filing.

---

#### 4. `orders_demand`
**Description:** Product demand, preorders, deliveries, backlog, order wins, large contracts, customer announcements.
**Golden set examples:** `g001` (TSLA Q4 deliveries 495,570), `g004` (TSLA Q1 deliveries 358,023), `g013` (NVDA DOE supercomputer contract), `g019` (AMD OpenAI partnership).
**Strong evidence:** Company IR press release, 8-K item 1.01 (material definitive agreement), SEC contract filing, earnings call transcript (management demand commentary).
**Weak evidence:** Rumor articles, aggregator speculation about "expected" deals.
**Preferred tier:** T1 (company IR, SEC 8-K); T2 (premium press reporting the contract).
**Evidence type:** Official press release + filing + structured order/delivery data.

---

#### 5. `supply_chain`
**Description:** Suppliers, production bottlenecks, inventory, input costs, semiconductor fab updates, logistics.
**Golden set examples:** `g003` (auto tariff softening → localized supply chain benefit), `g010` (NVDA tariff → hardware input cost risk).
**Strong evidence:** Company IR (supply chain press releases), earnings call (management discussion of supply constraints), industry reports.
**Weak evidence:** General "supply chain crisis" articles without company-specific detail.
**Preferred tier:** T2 (company IR, premium industry press).
**Evidence type:** Press release + management commentary.

---

#### 6. `competition_sector`
**Description:** Peer moves, pricing pressure, product launches, market share shifts, industry dynamics.
**Golden set examples:** `g009` (NVDA / DeepSeek), `g012` (AI infrastructure ceiling), `g016` (AMD Meta win vs NVDA), `g047` (JPM AI disruption to software loans).
**Strong evidence:** Peer earnings calls (competitive mentions), industry reports, premium financial press (FT, Bloomberg).
**Weak evidence:** Motley Fool "X vs Y" comparison articles, aggregator listicles.
**Preferred tier:** T2 (premium press, earnings call analysis).
**Evidence type:** Article/reporting + earnings call analysis.

---

#### 7. `customer_partner_contracts`
**Description:** Large customer wins, partnerships, cloud/AI/data-center contracts, government contracts, supplier agreements.
**Golden set examples:** `g013` (NVDA DOE 7-supercomputer contract), `g016` (AMD Meta AI partnership), `g019` (AMD OpenAI deal with warrants).
**Strong evidence:** 8-K item 1.01 (material contract), company IR press release, joint press release from both parties.
**Weak evidence:** Rumor articles, "sources say" reporting without named parties.
**Preferred tier:** T1 (SEC 8-K, company IR).
**Evidence type:** Official filing + press release.

---

#### 8. `management_governance`
**Description:** CEO/CFO changes, insider buying/selling, board decisions, governance risk, activist investors.
**Golden set examples:** `g008` (TSLA Musk insider purchase), `g024` (UNH CEO replacement), `g026` (UNH Berkshire 13F stake → short squeeze).
**Strong evidence:** 8-K item 5.02 (officer departure/election), Form 4 (insider transactions), 13F (institutional holdings), Schedule 13D (activist).
**Weak evidence:** Speculation about "possible" management changes, rumor articles.
**Preferred tier:** T1 (SEC filings).
**Evidence type:** Official filing.

---

#### 9. `regulatory_legal`
**Description:** DOJ/FTC/SEC investigations, antitrust, lawsuits, settlements, approvals, healthcare/tech/finance regulation.
**Golden set examples:** `g017` (AMD export license relief), `g018` (AMD export tax), `g022` (UNH DOJ Medicare probe), `g028` (GOOGL antitrust penalty), `g029` (GOOGL no-Chrome-divestiture relief).
**Strong evidence:** SEC 8-K item 1.03/8.01 (legal proceedings), DOJ/FTC press release, court docket (PACER), settlement agreement.
**Weak evidence:** Commentary about investigations without primary source, opinion about "what the ruling means."
**Preferred tier:** T1 (SEC filing, government press release).
**Evidence type:** Official filing + legal document + government press release.

---

#### 10. `geopolitical_trade`
**Description:** Tariffs, export controls, sanctions, China exposure, trade war, supply chain relocation.
**Golden set examples:** `g003` (auto tariff adjustment), `g010` (NVDA tariff risk), `g033` (META tariff shock hardware costs), `g042` (AAPL China supply chain tariff), `g044` (AAPL 90-day tariff pause rally).
**Strong evidence:** USTR/Federal Register notices, executive orders, trade data (Census Bureau), premium financial press.
**Weak evidence:** Aggregator "trade war fears" roundups without specific policy details.
**Preferred tier:** T2 (government source + premium press).
**Evidence type:** Government document + structured trade data + reporting.

---

#### 11. `macro_rates`
**Description:** Fed decisions, rate-cut/hike expectations, Treasury yield moves, yield curve, monetary policy.
**Golden set examples:** `g007` (TSLA Jackson Hole → September cut expectation), `g011` (NVDA broad risk-on after tariff pause).
**Strong evidence:** FOMC statement/minutes, Fed funds futures, Treasury yield data (FRED), Fed speeches.
**Weak evidence:** Opinion about "what the Fed might do next."
**Preferred tier:** T2 (FRED/Fed data) — T6 in source_tier if structured; T2 if premium analysis.
**Evidence type:** Structured numeric data + official statement + market pricing.

---

#### 12. `macro_inflation`
**Description:** CPI, PCE, inflation expectations, input inflation, wage inflation.
**Golden set examples:** None directly — inflation is usually a contributing factor in macro events.
**Strong evidence:** BLS CPI report, BEA PCE data, TIPS breakevens, Michigan survey inflation expectations.
**Weak evidence:** "Inflation fears" articles without specific data.
**Preferred tier:** T2 (structured data).
**Evidence type:** Structured numeric data.

---

#### 13. `macro_labor_growth`
**Description:** Jobs, unemployment, GDP, recession/growth risk, PMI, consumer sentiment.
**Golden set examples:** `g046` (JPM Walmart consumer health warning → credit quality fears).
**Strong evidence:** BLS employment report, BEA GDP, ISM PMI, Conference Board consumer confidence.
**Weak evidence:** "Recession fears" roundups without data.
**Preferred tier:** T2 (structured data).
**Evidence type:** Structured numeric data.

---

#### 14. `market_risk_liquidity`
**Description:** VIX, dollar, broad risk-on/off, liquidity conditions, sector rotation, credit spreads.
**Golden set examples:** `g003` (broad auto rally), `g011` (broad risk-on rebound), `g048` (JPM tariff pause → recession odds down).
**Strong evidence:** VIX, credit spreads (FRED), dollar index, market breadth data, Fed financial conditions index.
**Weak evidence:** "Markets are jittery" commentary without quantification.
**Preferred tier:** T3 (structured market data + premium market commentary).
**Evidence type:** Structured market data + reporting.

---

#### 15. `analyst_sentiment`
**Description:** Analyst upgrades/downgrades, price target changes, estimate revisions, initiations.
**Golden set examples:** `g012` (analyst reports on AI infrastructure ceiling) — analyst sentiment is often a contributing factor, not the primary cause in golden set.
**Strong evidence:** Analyst reports (primary, not second-hand), price target data (structured), consensus estimate changes.
**Weak evidence:** "Analysts are bullish" aggregator roundups.
**Preferred tier:** T3 (structured analyst data); T4 for news articles about analyst moves.
**Evidence type:** Structured data + primary analyst report.

---

#### 16. `technical_flow`
**Description:** Momentum, short interest, options flow, institutional ownership changes, ETF/sector flow, unusual volume.
**Golden set examples:** `g008` (TSLA Musk insider buy → momentum), `g026` (UNH Berkshire 13F → short squeeze), `g045` (AAPL $4T milestone → momentum).
**Strong evidence:** FINRA short interest, 13F filings, options flow data, insider transaction filings (Form 4).
**Weak evidence:** "Stock is moving on technicals" commentary without data.
**Preferred tier:** T3 (structured flow data).
**Evidence type:** Structured market data + SEC filing (13F, Form 4).

---

#### 17. `corporate_actions`
**Description:** M&A, spin-offs, splits, dividends, buybacks, capital raises, IPO/secondary.
**Golden set examples:** No direct M&A events in the golden set, but buybacks/dividends appear in fundamentals.
**Strong evidence:** 8-K item 1.01 (M&A agreement), 8-K item 8.01 (dividend/buyback), SEC filings (S-1, S-3, S-4).
**Weak evidence:** Rumor articles about "possible" deals.
**Preferred tier:** T1 (SEC filings).
**Evidence type:** Official filing.

---

### A.3 Category-to-Tier Mapping Summary

| Category | Primary Tier | Evidence Nature |
|----------|-------------|-----------------|
| earnings_results | T1 | Structured + Filing + Transcript |
| guidance_outlook | T1 | Filing + Transcript |
| fundamentals_financials | T1 | Structured + Filing |
| orders_demand | T1/T2 | Press Release + Filing |
| supply_chain | T2 | Press Release + Commentary |
| competition_sector | T2 | Reporting + Analysis |
| customer_partner_contracts | T1 | Filing + Press Release |
| management_governance | T1 | SEC Filing |
| regulatory_legal | T1 | Filing + Legal Doc + Gov PR |
| geopolitical_trade | T2 | Gov Doc + Structured + Reporting |
| macro_rates | T2 | Structured + Official Statement |
| macro_inflation | T2 | Structured Data |
| macro_labor_growth | T2 | Structured Data |
| market_risk_liquidity | T3 | Structured + Reporting |
| analyst_sentiment | T3 | Structured + Primary Report |
| technical_flow | T3 | Structured + Filing |
| corporate_actions | T1 | SEC Filing |

---

## Part B — Evidence Source Mapping

### B.1 Source Families

#### Official / Primary

| Source | Docs URL / OpenBB Ref | Auth | Free/Paid | Rate Limit | Ticker-Scoped | Date Filter | Field Shape | Depth | Tier | Coverage (10 tickers) | Freshness | Risk |
|--------|----------------------|------|-----------|-----------|---------------|-------------|-------------|-------|------|----------------------|-----------|------|
| **SEC EDGAR submissions** | `openbb_platform/providers/sec/models/company_filings.py` | None (User-Agent required) | Free | ≤10 req/s | Via CIK lookup | Yes (`start_date`, `end_date`) | `filingDate`, `form`, `primaryDocument`, `items`, `accessionNumber` | official_metadata | T1 | 10/10 — all US-listed | Daily (same-day 8-K) | CIK↔ticker map needed; 10 req/s is generous |
| **SEC 8-K/10-Q/10-K full text** | `openbb_platform/providers/sec/models/sec_filing.py` | None | Free | ≤10 req/s | Via CIK + accession | N/A (single doc) | `document_urls[]`, HTML → text | primary_full_text | T1 | 10/10 | Same-day for 8-K | HTML extraction quality varies; iXBRL for 10-Q/10-K needs parser |
| **SEC Form 4 (insider)** | `openbb_platform/providers/sec/models/insider_trading.py` | None | Free | ≤10 req/s | Via CIK | Yes | `transaction_date`, `transaction_code`, `shares`, `price`, `director`/`officer` flags | structured_numeric | T1 | 10/10 | Daily (Form 4 within 2 business days) | Form 4 parsing is straightforward; OpenBB has reference impl |
| **SEC 13F (institutional)** | `openbb_platform/providers/sec/models/form_13FHR.py` | None | Free | ≤10 req/s | Via CIK | Quarterly | `filing_date`, `issuer`, `value`, `shares` | structured_numeric | T1 | 10/10 (as holders) but 13F filers only for flow | Quarterly (45-day lag) | Lag makes it NOT suitable for same-day attribution; pattern data only |
| **Company IR press releases** | Per-company websites | None | Free | Per-site | Per-ticker (manual) | Varies | title, date, body HTML, URL | primary_full_text / reporting_summary | T1 | 8–10/10 (varies by company) | Same-day | No unified API; scraping varies per site; ToS risk |
| **Earnings call transcripts** | Seeking Alpha / Fool / company IR | Varies | Mostly free (delayed) | N/A | Per-ticker | Per-quarter | speaker, text, Q&A | primary_full_text | T2 | 10/10 | Quarterly + 1-2 day delay | Seeking Alpha transcripts are free but not API-accessible; scraping risk |

#### Macro / Government (Structured)

| Source | Docs URL / OpenBB Ref | Auth | Free/Paid | Rate Limit | Ticker-Scoped | Date Filter | Field Shape | Depth | Tier | Coverage | Freshness | Risk |
|--------|----------------------|------|-----------|-----------|---------------|-------------|-------------|-------|------|----------|-----------|------|
| **FRED** | `openbb_platform/providers/fred/` | API key (free) | Free | 120 req/min | No (macro series) | Yes | `date`, `value`, series metadata | structured_numeric | T6 | Universal | Daily → real-time for some series | Series discovery needs curation |
| **Federal Reserve** | `openbb_platform/providers/federal_reserve/` | None | Free | Unknown | No | Varies | FOMC statement, minutes, projections | official_metadata + structured_numeric | T6 | Universal | Per-meeting (6-week cycle) | Not daily-tradable freshness |
| **BLS** | `openbb_platform/providers/bls/` | API key (free) | Free | Unknown | No | Monthly/quarterly | CPI, employment, PPI series | structured_numeric | T6 | Universal | Monthly (1-2 week lag) | Release schedule is predictable; useful for event-day |
| **BEA** | Not in OpenBB currently | None (web) | Free | N/A | No | Monthly/quarterly | GDP, PCE, trade balance | structured_numeric | T6 | Universal | Monthly/quarterly | |
| **EIA** | `openbb_platform/providers/eia/` | API key (free) | Free | Unknown | No | Varies | Petroleum, nat gas, electricity data | structured_numeric | T6 | Universal | Weekly/monthly | Energy-sector-specific |
| **TradingEconomics** | `openbb_platform/providers/tradingeconomics/` | API key | Paid (limited free) | Unknown | No | Yes | Economic indicators, forecasts | structured_numeric | T3 | Universal | Daily | Paid beyond basic tier |

#### News / Reporting

| Source | Docs URL / OpenBB Ref | Auth | Free/Paid | Rate Limit | Ticker-Scoped | Date Filter | Field Shape | Depth | Tier | Coverage (10 tickers) | Freshness | Risk |
|--------|----------------------|------|-----------|-----------|---------------|-------------|-------------|-------|------|----------------------|-----------|------|
| **Finnhub company-news** | (prior discovery) | API key (free tier) | Free (limited) | 60 req/min | Per-ticker | Yes (`from`, `to`) | `headline`, `summary` (101-431 chars), `source`, `url`, `datetime` | summary | T4 (aggregator of publishers) | 10/10 | Daily (near real-time) | Summary only — no full text; `source` is the real publisher; useful for discovery/surface, not deep evidence |
| **yfinance news** | `openbb_platform/providers/yfinance/models/company_news.py` | None | Free | Moderate | Per-ticker | No (only latest ~50-100 items) | `title`, `summary`, `url`, `source`, `pubDate` | summary | T4 | 10/10 | Near real-time | No date filter → hard to use for historical backfill; good for daily incremental top-up |
| **Nasdaq news / events** | `openbb_platform/providers/nasdaq/` | Varies | Free (limited) | Unknown | Per-ticker (earnings calendar, dividends) | Yes | `date`, event type, description | structured_numeric (calendar) + summary | T2 (official exchange) | 10/10 (US-listed) | Daily | Nasdaq provider in OpenBB focuses on earnings/dividend calendars, not news articles |
| **TMX company news** | `openbb_platform/providers/tmx/models/company_news.py` | None (GraphQL) | Free | Unknown | Per-ticker | Page-based | `headline`, `summary`, `source`, `datetime` | summary | T4 | 2–3/10 (Canadian listings only) | Daily | TSX/TSXV only — limited US coverage |
| **Biztoc world news** | `openbb_platform/providers/biztoc/models/world_news.py` | None | Free | Unknown | No (world, not ticker) | Yes | `title`, `excerpt`, `url`, `date` | aggregator_summary | T5 | Universal but unfiltered | Daily | No ticker filter → high noise for ticker-specific attribution |
| **Tiingo news** | `openbb_platform/providers/tiingo/models/company_news.py` | API key (free tier) | Free (limited) | Unknown | Per-ticker (multi) | Yes | `title`, `description`, `tags`, `source`, `publishedDate`, `crawlDate`, `url`, `symbols` | summary | T4 | 10/10 | Daily (crawled) | Crawled aggregation — quality depends on `source`; `tags` + `symbols` useful for filtering |
| **Intrinio company news** | `openbb_platform/providers/intrinio/models/company_news.py` | API key | Paid (limited trial) | Unknown | Per-ticker | Yes | `title`, `summary`, `publication_date`, `source`, `topics`, `sentiment`, `word_count`, `url` | summary to full_text (varies by source) | T3 | 10/10 | Daily | Paid; sources are yahoo/moody; `business_relevance` score useful; sentiment included |
| **WSJ** | `openbb_platform/providers/wsj/` | API key | Paid | Unknown | Per-ticker likely | Unknown | Unknown | reporting_full_text (expected) | T2 | 10/10 | Daily | Paywalled; premium quality but expensive |
| **Seeking Alpha** | `openbb_platform/providers/seeking_alpha/` | API key | Paid (limited free) | Unknown | Per-ticker | Unknown | `title`, `summary`, `url`, `published_date` | opinion_full_text / summary | T5 (opinion) | 10/10 | Daily | Opinion-heavy; worsens the Motley Fool problem — only useful for earnings call transcript data |
| **Benzinga** | `openbb_platform/providers/benzinga/models/company_news.py` | API key | Paid (limited free) | Unknown | Per-ticker (multi) | Yes | `title`, `teaser`, `body` (if display=full), `stocks` (+ channels, tags, authors) | reporting_summary → reporting_full_text with display=full | T4 | 10/10 | Real-time | Free tier very limited; full text needs paid; available via OpenBB but requires key |
| **Polygon /news** | (baseline, already in Catalyst) | API key (configured) | Free (5 years) / Paid | ~12s interval | Per-ticker | Yes | `title`, `description`, `published_utc`, `publisher.name`, `article_url`, `tickers[]`, `keywords[]` | summary (description is card-level, NOT full text) | T4/T5 (by publisher) | 10/10 | Daily (near real-time) | Already integrated; depth is summary only; ~58% T5 |

### B.2 Source Recommendations Table

| Source | Recommendation | Reasoning |
|--------|---------------|-----------|
| SEC 8-K full text | **BUILD_NOW** | Closes regulatory_legal + earnings_results + guidance_outlook + management_governance + corporate_actions gaps. Free, no key. Official primary source. |
| SEC 10-Q/10-K metadata | **BUILD_NOW** (metadata first, full text defer) | T1 structured financials. Full text extraction from iXBRL is complex — defer full parsing, ship metadata + 8-K full text first. |
| Finnhub company-news | **BUILD_NOW** | Free tier works (confirmed Step 3A). Summary-level but `source` field identifies real publisher. Complements Polygon with different publisher set. |
| yfinance news | **PROBE_NEXT** | Free, no key. Summary-level. Yahoo-sourced. Good for daily incremental diversity. But no date filter — limited historical backfill. |
| SEC Form 4 | **PROBE_NEXT** | Free. Structured. Insider transactions are direct signals. OpenBB has reference impl. |
| SEC 13F | **DEFER_QUARTERLY** | Quarterly 45-day lag makes it useless for daily attribution. Good for pattern analysis only. |
| Tiingo news | **PROBE_NEXT** | Free tier. Multi-ticker. `tags` + `symbols` fields useful. Crawled aggregation with `source` attribution. |
| Intrinio company news | **DEFER_PAID** | Paid. Moody's sources are premium but cost is a barrier. business_relevance score is interesting. |
| FMP stock-news / press-releases | **DEFER_PAID** | 402 on current key (Step 3A-2). Needs paid plan. |
| FMP fmp-articles | **KEEP_REFERENCE_ONLY** | Free tier works but single-ticker-per-article (no multi-ticker mapping). Current Polygon already covers this niche. |
| Benzinga | **DEFER_PAID** | Free tier very limited. Full text needs paid plan. Aggregator quality similar to Polygon. |
| Seeking Alpha | **DEFER_LOW_QUALITY** | Opinion-heavy, worsens Motley Fool problem. Only useful for earnings call transcript data — which is better sourced from company IR. |
| WSJ | **DEFER_PAID** | Premium quality but paywalled and expensive. |
| TMX | **KEEP_REFERENCE_ONLY** | Canadian listings only — <3 of our 10 tickers. |
| Biztoc | **KEEP_REFERENCE_ONLY** | No ticker filter — high noise. World news, not ticker-specific. |
| FRED | **BUILD_LATER** (Step 3E) | Free, structured. Essential for macro_rates + macro_inflation. But macro is a complementary layer, not the current bottleneck. |
| BLS / BEA / EIA | **BUILD_LATER** (Step 3E) | Free, structured. Macro data. Same priority as FRED. |
| Company IR press releases | **BUILD_LATER** (Step 3D) | T1 quality but no unified API. Per-company scraping. High value, high implementation cost. |
| Earnings call transcripts | **BUILD_LATER** (Step 3D) | T2 quality. Seeking Alpha has free access but no API. Scraping or third-party API needed. |
| Nasdaq events calendar | **PROBE_NEXT** | Free. Structured earnings/dividend calendar data. Official exchange source. |

---

## Part C — Prioritized Build Plan

### C.1 Recommended Sequence

```
Step 3C: SEC 8-K Full Text + 10-Q/10-K Metadata  [HIGHEST PRIORITY]
  └─ Closes: regulatory_legal, earnings_results, guidance_outlook, management_governance, corporate_actions

Step 3D: Finnhub Company-News + yfinance News      [HIGH PRIORITY]
  └─ Closes: orders_demand, customer_partner_contracts (via diverse publisher set), competition_sector

Step 3E: Company IR + Macro Structured Data         [MEDIUM PRIORITY]
  └─ Closes: macro_rates, macro_inflation, macro_labor_growth, supply_chain
  └─ Includes: FRED, SEC Form 4, Nasdaq events calendar
```

### C.2 Why SEC First

1. **5 of 17 categories are SEC-gated:** Without SEC filings, Catalyst cannot produce T1 evidence for regulatory_legal, earnings_results, guidance_outlook, management_governance, or corporate_actions.
2. **Golden set directly tests this:** `g022` (UNH DOJ probe), `g023` (UNH guidance cut), `g028` (GOOGL antitrust) — all require the actual 8-K or DOJ filing.
3. **The error taxonomy confirms:** DATA_COVERAGE_GAP is the #1 failure mode (g015, g046, g049). Adding Polygon-only sources doesn't fix this.
4. **SEC is free and well-documented:** No API key, no rate limit issues at our scale, OpenBB has reference implementations.
5. **8-K full text extraction is feasible:** The SEC primary document URL resolves to HTML. The Step 3A discovery confirmed content-type=text/html with cheap extractability. iXBRL for 10-Q/10-K can be deferred.

### C.3 Why Finnhub + yfinance Next

1. **Diverse publisher set:** Finnhub's `source` field identifies the real publisher — different from Polygon's publisher mix. Less Motley Fool, more financial wires.
2. **Free tier works:** Step 3A confirmed Finnhub company-news returns 200 with free key. yfinance needs no key.
3. **Low implementation cost:** Both have well-defined APIs. Finnhub maps cleanly to the `articles` schema. yfinance fits the connector contract.
4. **Daily incremental compatible:** Both support date ranges (Finnhub) or are near-real-time (yfinance).

### C.4 What Each Step Closes

| Build Step | Categories Improved | Motley Fool Reduction | Schema Impact | New Tables? |
|-----------|-------------------|-----------------------|---------------|-------------|
| **3C: SEC** | earnings_results, guidance_outlook, fundamentals_financials, management_governance, regulatory_legal, corporate_actions, customer_partner_contracts | Adds T1 alternative to T5 commentary for all earnings/regulatory events | Add `filings` table (filing_id, cik, ticker, form_type, filed_at, period, url, items_json, source_tier=T1). Add CIK↔ticker map asset. `source_kind='filing'` in index_state (already polymorphic). | Yes — `filings` table |
| **3D: Finnhub/yfinance** | orders_demand, customer_partner_contracts, competition_sector, supply_chain | Adds T3/T4 reporting/summary from different publishers — less Motley Fool, more wires | Reuses `articles` + `article_tickers`. `provider='finnhub'` or `'yfinance'`. source_tier by publisher. dedup via `dedup_group_id`. | No new tables |
| **3E: Macro + IR** | macro_rates, macro_inflation, macro_labor_growth, market_risk_liquidity, analyst_sentiment, technical_flow | Adds structured numeric evidence (T2/T6) — NOT news articles at all | Structured data may need new tables or dedicated `macro_observations` table. FRED/Fed/BLS are series-based, not article-based. | Yes — `macro_observations` likely |

### C.5 L1 vs L2 for New Sources

| Source | L1 | L2 | Rationale |
|--------|----|----|-----------|
| SEC 8-K full text | ✅ (title: form_type + items) | ✅ (body ≥800 chars, sentence-split from filing text) | 8-K bodies are often long — ideal for L2 sentence-level retrieval |
| SEC 10-Q/10-K metadata | ✅ (structured fields) | ❌ (no body text initially — metadata only) | Metadata-first approach |
| Finnhub articles | ✅ (title + summary) | ❌ (summary too short, 101-431 chars) | Below 800-char threshold |
| yfinance articles | ✅ (title + summary) | ❌ (summary only) | Below 800-char threshold |
| FRED observations | ✅ (series metadata + values) | ❌ (structured, not prose) | Structured data — not sentence-splittable |

---

## Part D — Explicit Non-Goals

### Sources explicitly NOT built now:

| Source | Reason |
|--------|--------|
| **FMP paid news** (stock-news, press-releases) | 402/403 on current key. Needs paid plan. Not worth the cost until SEC + Finnhub + yfinance are operational. |
| **FMP fmp-articles** | Free tier works but single-ticker-per-article (no multi-ticker mapping). Polygon already covers this depth at larger scale. |
| **Seeking Alpha** | Opinion-heavy. Worsens the Motley Fool problem. Only useful for earnings call transcript data, which is better sourced from company IR or dedicated transcript APIs. |
| **WSJ** | Paywalled, expensive, ToS constraints on scraping. |
| **Benzinga (paid tier)** | Aggregator similar to Polygon. Free tier too limited. Paid tier not justified before SEC is integrated. |
| **Intrinio** | Paid. Moody's sources are premium but cost barrier. Revisit if attribution quality plateaus after SEC + Finnhub. |
| **Biztoc** | No ticker filter — world news only. High noise. |
| **TMX** | Canadian listings only (<3/10 tickers). |
| **GDELT** | Deferred to WS4D. Web-scale crawling needs separate architecture. |
| **Company IR scraping** | High per-company implementation cost, no unified API, ToS risk. Defer to Step 3E when the ROI of SEC + Finnhub is known. |
| **Earnings call transcripts (scraped)** | Seeking Alpha scraping has ToS risk. Dedicated transcript APIs exist but are paid. Defer. |

---

## Part E — Deliverables

### E.1 This Document

`docs/research/ws4b-attribution-taxonomy-and-source-map.md`

### E.2 Machine-Readable Artifact

`data/provider_discovery/20260701T035921Z/attribution_source_matrix.json`

### E.3 Files Inspected

**Catalyst:**
- `packages/eval/golden_set/README.md`
- `packages/eval/golden_set/v1_2.jsonl` (50 golden events)
- `packages/eval/golden_set/annotation_template.md`
- `docs/ADR/ADR-003-miner-critic-judge-workflow.md`
- `docs/ADR/ADR-009-two-level-chunking-and-reranker.md`
- `docs/reports/2026-05-15-catalyst-error-taxonomy.csv`
- `packages/data-core/catalyst_data/source_tier.py`
- `packages/data-core/catalyst_data/connectors/base.py`
- `packages/agents/catalyst_agents/retrieval/policy.py`

**PokieTicker:**
- `backend/pipeline/layer0.py` — rule-based news filter
- `backend/pipeline/layer1.py` — Claude Haiku batch relevance grading (50 articles/call)
- `backend/pipeline/layer2.py` — Sonnet deep analysis on click
- `backend/pipeline/alignment.py` — news-to-trading-day alignment with forward returns
- `backend/database.py` — schema (news_raw, news_ticker, layer0/1/2_results, news_aligned)

**OpenBB:**
- `openbb_platform/core/openbb_core/provider/standard_models/company_news.py`
- `openbb_platform/core/openbb_core/provider/standard_models/world_news.py`
- `openbb_platform/providers/yfinance/models/company_news.py`
- `openbb_platform/providers/benzinga/models/company_news.py`
- `openbb_platform/providers/intrinio/models/company_news.py`
- `openbb_platform/providers/tiingo/models/company_news.py`
- `openbb_platform/providers/tmx/models/company_news.py`
- `openbb_platform/providers/sec/models/company_filings.py`
- `openbb_platform/providers/sec/models/sec_filing.py`
- `openbb_platform/providers/sec/models/insider_trading.py`
- Provider listing: 34 providers in OpenBB platform

### E.4 Verification

- ✅ **Secret grep:** Document contains zero API keys, tokens, or secrets
- ✅ **No DB writes:** This is a research document — zero database interaction
- ✅ **No packages/app or packages/agents changes:** Read-only inspection
- ✅ **No implementation code:** Taxonomy + source map only

---

## Part F — Open Questions for Claude/User Decision

1. **SEC 8-K item filtering:** Should we ingest ALL 8-K items or filter to the attribution-relevant subset (1.01 contracts, 1.03 legal, 2.02 results, 5.02 management, 7.01 regulation FD, 8.01 other)? The full set includes items like 3.03 (stockholder rights plans) that are rarely attribution-relevant. **Recommendation:** Start with filter list above; add items on demand.

2. **SEC HTML → text extraction quality:** The Step 3A discovery confirmed text/html and "cheaply extractable." But some 8-Ks attach PDFs or images rather than inline HTML. **Recommendation:** Handle HTML inline first; document PDF/image attachments as "text_unavailable" in the filing record. Do not build a PDF parser in Step 3C.

3. **CIK↔ticker map maintenance:** The SEC CIK↔ticker mapping changes over time (ticker changes, delistings). **Recommendation:** Ship a pinned CIK map for our 10-ticker universe. Add a `--refresh-cik-map` CLI command. Do not auto-refresh.

4. **Finnhub daily limit on free tier:** The free tier has a daily request cap (typically 60 calls/minute but limited total per day). **Recommendation:** Probe the exact limit during Step 3D discovery before committing to daily backfill volume.

5. **yfinance no-date-filter limitation:** yfinance `get_news()` returns only the latest ~50-100 items with no date range filter. This means it can only be used for daily incremental top-up, not historical backfill. **Recommendation:** Accept this limitation. Use yfinance for daily freshness, not backfill. Document the gap.

6. **Macro structured data schema:** FRED/Fed/BLS data is series-based (series_id → {date, value}) — fundamentally different from article-based evidence. **Recommendation:** Defer the macro schema decision to Step 3E planning. The current article-centric schema (articles, article_tickers, clean_assets) does not fit structured macro observations well.

7. **Company IR scraping feasibility:** Most large-cap companies have IR press release pages with predictable URL patterns (e.g., `investor.apple.com/news/`). But there's no unified API. **Recommendation:** Build a generic IR connector with per-company URL templates in Step 3E. Do not build in Step 3C/3D.

8. **Should `filings` data produce `clean_assets` rows?** The current clean_assets model is (article_id, ticker)-scoped. Filings are filing-scoped with multiple tickers implied by CIK→ticker. **Recommendation:** Filings produce their own retrieval pathway (via index_state with source_kind='filing'). Do not force filings into clean_assets. The retrieval policy already accounts for `sec_filing` as a DIRECT source type.

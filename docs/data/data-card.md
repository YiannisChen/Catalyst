# Data Card

> Historical documents mentioned below that are no longer in the workspace remain in the main text in git history, under tag `research-archive-20261106`.

> This card records frozen backend versions and evaluations. The `m9` Python source paths in the body are names used by those historical versions. The current data package uses `catalyst_data.ingestion`; see §3 for executable commands.


- **Version**: v2.0.2, 2026-10-24 (clarifies the SEC index-time description and the undeployed boundary of data/main; v2.0.1: 2026-10-18)
- **Applies to**: Three frozen backend versions (`backend-freeze-v1`, `-v2`, `-v2.1`) and their evaluations
- **Principle**: Raw data is **not committed to git** (`.gitignore` covers `data/m9/*.sqlite3`, `data/research/`, `data/prospective/stores/`, etc.). The repository provides only collection scripts, data manifests, and the hashes below.
- **Hashes**: sha256 values show only the first 12 characters and were calculated locally on 2026-10-16. SQLite snapshots must be opened read-only (`mode=ro&immutable=1`); otherwise their hashes will change.
- **Licensing**: Follow the terms set by each data provider. The summaries below are not legal advice.

The current daily service covers 102 large-cap U.S. stocks. The historical prospective evaluation used an S&P 100 constituent file with 101 tickers; these are separate universes.

## 1. Data sources

### 1.1 Daily price data (OHLCV)

| Item | Details |
|---|---|
| Source | **Server daily updates and prospective evaluations**: yfinance is the primary source; when data is unavailable, Polygon grouped daily fills in that day (`packages/data-core/catalyst_data/m9/market.py`, with filled rows marked `source='polygon'`). **Research snapshot** (E series): Polygon grouped daily (`scripts/research/backfill_grouped_daily.py`) |
| License | Polygon free tier: personal, non-commercial use. Yahoo / yfinance: follow Yahoo terms; personal research only |
| Coverage | Server daily updates: supported stock universe plus reference ETFs (SPY, sector ETFs, SMH, USO, UNG, GLD, SLV, CPER). Research snapshot: all U.S. stocks and ETFs [R: data.grouped_daily] |
| Time semantics | Daily data for trading day D is not complete until the morning of D+1 (the free tier does not provide it that evening). Both sources adjust for splits, **but not dividends**; dividends are handled by the corrections in §1.2. Closing-price consistency between the sources is covered in [R: data.price_agreement] |
| Known limitations | The free tier does not provide intraday data. Some symbols may have missing prices: in the 2026-10-15 smoke test, BNY had no computable z-score and was skipped for that day. Yahoo intermittently blocks some server IPs, so Polygon backfill is needed |
| Storage | `data/m9/market_context.sqlite3` (active), `data/research/grouped_daily.sqlite3` (research) |
| Snapshots | `market_context.v6snap_20261013.sqlite3`: `c045aa645e81…` (used to run the test set); `grouped_daily.sqlite3`: `b1df4904df0a…` |

### 1.2 Dividends and stock splits

| Item | Details |
|---|---|
| Source | `packages/data-core/catalyst_data/m9/corp_actions.py`; results are stored in `data/m9/corp_actions.sqlite3` (environment variable `CATALYST_CORP_ACTIONS_DB`) |
| Time semantics | Returns are adjusted by ex-dividend date (calculations such as `groups._market_z` read these adjustments) |
| Known limitations | Added before the freeze; not evaluated separately on the test set |

### 1.3 SEC 8-K filings and exhibits

| Item | Details |
|---|---|
| Source | SEC EDGAR submissions interface and exhibit pages (`m9/sec.py`, `m9/sec_earnings.py`; T6 backfill: `scripts/research/t6_sec_intake.py`) |
| License | Public U.S. government data. Follow the SEC fair-access policy: include `SEC_USER_AGENT` in requests (name and contact email; stored only in `.env`, **not in git**) and limit request frequency |
| Coverage | Server daily updates: 8-K exhibits for each stock in the universe. Research calendar: [R: data.sec_calendar] (`data/research/events.sqlite3`) |
| Time semantics | `m9/sec_filings.py` enumerates accessions and Items from the submissions JSON, then uses the `Accepted` time on each filing's index page, stored as UTC; `acceptanceDateTime` is retained only as a candidate original value and checked against `sec.acceptance()`. **Do not use `filingDate`**: filings accepted after 17:30 are assigned to the next business day. The index-page time change in `sec_filings.py` is currently only in `data/main`; it has not been deployed to the evaluation or daily-update environments |
| Known limitations | The offline snapshot originally did not include 8-Ks outside the baseline date. As a result, the first T6 pass had many cases without evidence [R: t6.first_pass_no_evidence]; it was rerun after backfill |
| Auxiliary files | `data/m9/sec_company_tickers.json` (`a1d4b030a746…`). Company-name matching depends on this file. It **must be next to the body database**; otherwise, a headline must include the stock ticker to match |
| Snapshots | `events.sqlite3`: `162ab0be90e6…`; T6 body database copy `news_bodies_v2.t6.sqlite3`: `2254e1c4db45…` |

### 1.3a SEC 8-K index and exhibit units (second edition and supplement)

| Item | Details |
|---|---|
| 8-K index | `catalyst_data/m9/sec_filings.py`: reads SEC submissions JSON for each issuer and retrieves historical paginated files through its `filings.files[]`. The JSON provides accessions and Items; the SEC index page for each filing provides the final acceptance time in `Accepted`. Results are cached by CIK/accession in `CATALYST_SEC_SUBMISSIONS_DIR` (default `data/m9/sec_submissions/`) as `CIK{cik}-{accession}-index.json`. Offline mode fails closed if the index cache is missing; online mode requires `SEC_USER_AGENT` and must follow SEC request intervals. `company(ticker)` returns the company name |
| Online and offline modes | Online, a cache older than 6 hours is refreshed (requires `SEC_USER_AGENT`); if fetching fails, the cache is used. `CATALYST_SEC_OFFLINE=1` reads only the cache, making replays deterministic. Development set, T6, and new earnings-date sample runs are all offline; the second prospective evaluation round is online, with the cache stored in that round's data directory |
| Use | Second edition: earnings anchoring (the company's EARNINGS event uses the acceptance time of Item 2.02), and conditions for direct earnings handling (Item 2.02 within the window, and whether other material 8-Ks exist). Supplement: fields and verification of filing facts |
| Known limitations | The `recent` section of submissions JSON lists only about the latest 1,000 filings; older filings require separate historical files. Historical files are not currently used, which has caused missed answers: in the new earnings-date sample, WFC's earnings 8-K for 2025-10-14 predates the earliest filing in `recent` (2025-10-16). The anchor, direct earnings handling, and exhibit ingestion all miss it, so this case was declined (gap RG-05). URLs for co-filers' exhibits may contain another CIK (for example, URI), so verification uses accession, not the CIK in the URL |
| Exhibit units | `catalyst_data/m9/sec_structure.py`: splits Ex-99 by HTML block (the innermost p/div/li/td, as in `sec.exhibit_text`) into titles, bullets, and paragraphs. Stored in the evidence database table `sec_exhibit_units` (`url` primary key, `accession`, `units_json`, `parsed_at`). **The body, chunks, and title tables are unchanged**; only the `filing` rule reads these units |
| Writing units | Production ingestion: `sec.rows_for_filing` includes `structure` in the row, and `store.write_assets` writes the table. Existing database: `scripts/v2/sec_units_backfill.py <database>` refetches HTML from EDGAR and writes the units (exhibits do not change); `sec_structure.reclassify` reclassifies stored units using the current title rules without refetching |
| Title rules | `is_title`: one line, 20–240 characters; no period; does not end with a conjunction or punctuation; contains announcement verbs such as reports / announces / delivers / posts / declares |
| Known limitations | Multiline titles are not recognized, such as CoStar splitting a title across three lines; unusual block layouts may produce incorrect units. Title detection is heuristic and was tuned twice on T6 (backend design §5.3, v2.1), so T6 counts only as development data |
| Snapshots (with units) | Development-set database copy `news_bodies_v2.v6snap_20261013u.sqlite3`: `6c34df7fbf4e…` (1,206 exhibits have units); T6 database copy `news_bodies_v2.t6u.sqlite3`: `50cbe263a9e7…` (1,440); new earnings-date sample database `news_bodies_v2.e100.sqlite3`: `05756a485d49…` (1,317; based on the development-set database, with 131 8-K exhibits from 100 sessions added by `scripts/v2/sec_intake.py`). All three are under `data/m9/` in the v2.1 branch working directory |

### 1.3b Archived: earnings calendar and press-release RSS (collection only since 2026-10-20; not read by the attribution pipeline)

| Item | Details |
|---|---|
| Code | `data/collectors` branch: `catalyst_data/m9/earnings_calendar.py`, `catalyst_data/m9/press_rss.py`; separate server checkout directory `/opt/catalyst/collectors` and timer (`deploy/deploy_collectors.sh`), which does not affect the daily-update task |
| Earnings calendar | Finnhub `/calendar/earnings`, queried daily at 12:15 and 22:15 UTC for “today −7 to +28 days.” Every fetch is appended with `fetched_at`, allowing queries such as “what did the calendar say at a given time” (`as_of`). A single request returns at most 1,500 records, so requests are split into 3-day ranges and subdivided in half when a range reaches the limit |
| Earnings calendar limitations | The free tier has **no history**: all dates before 2026-07 are empty, and only the schedule for about the most recent month and a half and the future is available. Archiving started on 2026-10-20, so T6 and the new earnings-date sample cannot be checked against it. The `hour` field (bmo pre-market / amc after-hours / dmh during market hours) was populated for 69 of 85 S&P 100 earnings events (81%) |
| Press-release RSS | PR Newswire (all press releases and earnings, two feeds) and Business Wire (all news), polled every 5 minutes. Only feed metadata is stored: title, link, publication time, summary, and any stock tickers identified from it, along with the time first seen (`first_seen_at`); **press-release body pages are never fetched**. The PR Newswire all-press-releases feed returns only 20 items at a time, covering about 20 minutes, so it must be polled every 5 minutes |
| Press-release terms | Access is limited to personal, non-commercial research use. PR Newswire's terms restrict automated access and storing its content in a database; Business Wire permits reading RSS but not storing or summarizing its information. Therefore, only metadata is stored, feeds are polled infrequently, and data is **not distributed**; archived data is excluded from open source releases. Possible consequences include an IP block or a cease-and-desist notice. GlobeNewswire's robots.txt disallows automated access, so it is **not included** |
| Time semantics | Press releases: `published_at` is the newswire publication time (used to assign a trading day); `first_seen_at` is when we saw it (used to determine “what was available at the time”). RSS lists only recent items, so it **cannot be backfilled**; the archive starts on the first polling day |
| Logging | One `intake_log` row per request (OK / EMPTY / ERROR / DISABLED); gaps in the archive can be identified |

### 1.3c Archived: free news sources (collection only since 2026-10-21; not read by the attribution pipeline)

| Item | Details |
|---|---|
| Code | `data/collectors` branch `catalyst_data/m9/news_archive.py`; server database `/opt/catalyst/archive/news_archive.sqlite3`, four timer jobs (`deploy/collect/catalyst-collect-news-*`) |
| Principle | Include sources without historical data and accumulate data daily from the first polling day. Store only metadata for each item (title, link, time, provider summary, stock ticker) and record when it was first seen (`first_seen_at`); log one row per request (OK / EMPTY / ERROR / DISABLED) |
| Every 15 minutes | Finnhub market news (general, merger); 4 Investing.com RSS feeds (stocks, companies, economy, earnings) |
| Every hour | GDELT (one query per company, most recent 2 hours, one request every 10 seconds; assign a ticker only when the headline mentions the company); Seeking Alpha ticker RSS; Alpha Vantage news and sentiment (requires `ALPHAVANTAGE_API_KEY`, free tier allows 25 requests per day); NewsAPI (requires `NEWSAPI_KEY`) |
| Every 2 hours | Marketaux (requires `MARKETAUX_API_KEY`, 20 stocks per request) |
| 4 times daily | Finnhub company news, all 101 stocks in the universe (00:30, 06:30, 18:30, 22:30 UTC, avoiding daily-update and evaluation periods) |
| Checked but excluded | Yahoo Finance, Google News, CNBC RSS (robots.txt disallows access); MarketWatch feeds and SEC full-text search (robots.txt returns 403); Nasdaq (denies automated access); GlobeNewswire (robots.txt disallows access) |
| Terms | Personal, non-commercial research use; Seeking Alpha and Investing.com robots.txt allow reading these feeds. Store metadata only and do not distribute |
| Time note | Investing.com RSS publication times have no timezone and are stored as UTC; verification is needed |

### 1.3d Archived: market data and event calendar (collection only since 2026-10-21; not read by the attribution pipeline)

- Database `/opt/catalyst/archive/market_archive.sqlite3`, code `catalyst_data/m9/market_archive.py`.
- Four tables: `minute_bars` (1-minute bars, including pre-market and after-hours), `option_snapshots` (options chains before earnings), `rating_changes` (rating and price-target changes), `events` (FOMC, BLS, BEA, S&P constituent changes, options expiration dates).
- Every row includes its fetch time; ratings and events also have `first_seen_at`. When reading, filter to items seen before the cutoff time.
- Use only as [data] facts or candidate context, not as causes; adding them to attribution is Category B. See `provider_survey.md` for sources, quotas, and terms.
- Minute bars (same-day revisions): Alpaca SIP is primary; yfinance and Polygon fill gaps. One file per month, stored at `/opt/catalyst/archive/minute/minute_YYYY-MM.sqlite3`; keep 90 days locally, back up monthly files once, and fetch once from Hong Kong (runbook §6). Logging remains in `market_archive.sqlite3`.

### 1.3e Archived: Alpaca news (Benzinga; collection only since 2026-10-21; not read by the attribution pipeline)

- Database `/opt/catalyst/archive/alpaca_news.sqlite3`, code `catalyst_data/m9/alpaca.py`; fetches the most recent 90 minutes every hour.
- Backfill: `alpaca news-backfill --dates …`, fetching full New York calendar days; history is available back to 2015.
- For each article, record `created_at` (publication time, to the second, UTC), `first_seen_at`, and every observed `updated_at` (`news_versions`, RG-17 step one; record changes without excluding articles).
- The body comes from the API's `content`. Compared with Benzinga bodies already in the database, median similarity is 0.95 (`provider_survey.md` §15.1).

### 1.4 News headlines and article bodies

| Item | Details |
|---|---|
| Source | Finnhub company-news headline archive (`m9/headline_archive.py`), Polygon news, free article-body fetching (`m9/fetch.py`, follows robots.txt, one request per host per second), Benzinga / Kitco discovery (`m9/benzinga.py`, `m9/kitco.py`), CNBC closing summaries (`m9/cnbc_roundups.py`); GDELT for research (E9) |
| License | Finnhub free tier: personal use. Article bodies are fetched only from sites whose terms permit automated access: StockStory, PR Newswire, AOL, and GuruFocus prohibit automated scraping and are not included (attribution-quality analysis). Server access to Benzinga uses a local relay |
| Coverage | Server daily updates: for each stock with an absolute daily move of at least 2% (|change| ≥ 2%), same-day company headlines and articles, peer, proxy ETF, and market news |
| Archive schedule | `catalyst-headline-archive.timer` archives the complete UTC date each day at 10:30 UTC, through yesterday; the headline archive and daily updates share `CATALYST_HEADLINE_ARCHIVE_DB` (default `headline_archive.sqlite3` in the daily-update run directory). Daily updates run afterward at 11:00 UTC. |
| Time semantics | Publication time. Headlines are clues only; article bodies are used as evidence |
| Known limitations | The corpus is mostly headlines, with limited article-body coverage. Some commentary sites (Motley Fool, 24/7 Wall St., etc.) may enter publisher evidence (see backend design §8) |
| Storage and snapshots | `news_bodies_v2.v6snap_20261013.sqlite3`: `5b47c4e793e9…`; `headline_archive.v6snap_20261013.sqlite3`: `4fcec7a8cbdc…` (used to run the test set) |

### 1.5 Tavily web search

| Item | Details |
|---|---|
| Source | Tavily Search API (current code: `packages/data/catalyst_data/ingestion/collectors/web_discovery_v2.py`, `packages/data/catalyst_data/ingestion/collectors/web_search.py`). Discovery cache and one-off web search are separate entry points; the event-discovery agent's old web mode has been retired |
| License | Under the Tavily API terms of service. Result cache is for local research only |
| Coverage | Called only when building the publisher's web cache in phase two (prospective runner and CLI, up to 4 credits per stock; server daily limit 50); the publisher reads only the cache while running and never searches (operations S10). Domain allowlist is `web_search.FINANCE_DOMAINS` plus `web_discovery_v2._EXTRA_DOMAINS` |
| Time semantics | Provider metadata `published_at`. Date-only values are set to `23:59:59Z` on that date |
| Known limitations | **Date-only timestamps**: 5.0% of publisher evidence, all from Tavily, enter the publisher without a “date-only” marker; 19 records from D−1 fall before market open on D [R: t1.date_only_share] [R: t1.preopen_dminus1]. **Midnight timestamps**: 4.6% of results are exactly `00:00:00 GMT`; they represent dates only but are parsed to minute precision, affecting the event-discovery agent and inferred-association channel [R: t1.tavily_midnight]. Timestamps with a specific time agree with page times within 1 hour for URLs with matching page metadata [R: t1.clock_agreement]; it has not been verified whether differences within 1 hour cross trading-window boundaries |
| Snapshots | `web_search_v6_d2.frozen.sqlite3`: `b83aa5cf432e…`; `web_search_v5_d3.frozen.sqlite3`: `571b58b23cab…` (used to run the test set) |

### 1.6 Macroeconomic and policy data (not used by the publisher)

| Item | Details |
|---|---|
| Source | FRED / ALFRED (DGS2, DGS10, VIX, dollar, crude oil, and historical versions, `scripts/research/fetch_factor_data.py`), Federal Register API, EIA; FOMC decision dates are in `m9/structured_facts.py` |
| License | FRED: follow the St. Louis Fed terms of use; Federal Register and EIA are public U.S. government data |
| Use | E7 / E8 research and structured facts for the event-discovery agent |
| Time semantics | Publication date and the version available at the time (ALFRED vintage); do not use revised values |
| Known limitations | The FRED release IDs in `structured_facts.py` are marked “verify before use” |
| Snapshot | `data/research/fred.sqlite3`: `8d03273fef39…` |

## 2. Manifests and stock universe

| File | Purpose | sha256 / source |
|---|---|---|
| `data/prospective/sp100_universe_v1.json` | Prospective evaluation universe: S&P 100 constituents, 101 tickers | `be12e4e05f0e…d9d22` (full value in `prospective.py::UNIVERSE_SHA256`). Source: Wikipedia constituent table revid 1378987012 (2026-10-07). The iShares OEF holdings CSV could not be downloaded by script, so this table was used instead; the HTML comment in the BRK-B entry was removed [R: pro.universe] |
| `data/manifests/peer_map_v1.json` | Peer and sector mapping | `079e20ebc557…` |
| `data/manifests/issuer_domains_v1.json` | Company-owned domains (S-level sources for the event-discovery agent) | `c1b5d51bedfb…` |
| `data/manifests/exposure_map_v1.json` | Exposure map for the inferred-association channel (frozen) | `f81034b88e0b…`, freeze commit `318a2d1` |
| `data/manifests/heldout_candidates_v5.json`, `v6.json` | Case sources for the development and test sets | Tracked by git |
| `data/research/e100_sample.json` | New earnings-date sample (100 cases, sampled with seed 20261018 after E11 removed T6) | `04e82d039bdc…`; tracked by git |
| `data/research/v3_placebo.json` | Placebo dates | `7ebe6b0106466d9c…` (full value in event-discovery agent design §5.2) |

## 3. Collection scripts

| Data | Script / command |
|---|---|
| Server daily updates (prices, 8-K, moves, news, classification, images) | `python -m catalyst_data.ingestion.ops.daily scheduled` (see operations manual) |
| On-demand refresh for one trading day | `catalyst_data.ingestion.ops.refresh.refresh_session` |
| Research prices | `scripts/research/backfill_grouped_daily.py` |
| Research 8-K calendar | `scripts/research/build_earnings_calendar.py` |
| Macroeconomic factors | `scripts/research/fetch_factor_data.py` |
| Web evidence | `python -m catalyst_data.ingestion.collectors.web_discovery_v2 run …` |
| Prospective evaluation | `scripts/research/prospective.py day D`: copies active databases to `data/prospective/stores/` and also copies `sec_company_tickers.json` |
| Exhibit-unit backfill | `scripts/v2/sec_units_backfill.py <database>` (v2.1 branch) |
| Ingest 8-Ks and index cache for the new sample | `scripts/v2/sec_intake.py <cases.json> <base-db> <new-db> <index-dir>…` (v2.1 branch) |
| Second prospective evaluation round | `scripts/research/prospective2.py day D` (`~/Catalyst-round2/runner`): data directory `data/prospective2/`; before running the supplement arm, add units to this round's body database |
| Prospective evaluation gold labels | `scripts/v2/prospective_gold.py run` (`~/Catalyst-eval`, launchd `com.catalyst.gold`): reads only the two rounds' `candidates.json` files and writes `data/gold/<round>/gold.jsonl` and the hash ledger |

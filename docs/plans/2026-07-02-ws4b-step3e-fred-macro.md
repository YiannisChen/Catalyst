# WS4B Step 3E — FRED Macro Structured Signals (Final Plan)

**Status:** PLAN ONLY — not implemented
**Date:** 2026-07-02
**Branch:** `ws4b/article-level-data`
**Baseline:** `dbb7f67` (Step 3D committed)
**Prerequisite:** Step 3C/3D merged; two-plane architecture locked
**Provider discovery:** COMPLETE — locked decisions in §0.1

**PROVISIONAL — re-validate connector contract and source_mapping at implementation start.**

---

## 0. Goal

Add FRED macro economic indicators as the FIRST Plane-2 structured-signals table
(`macro_observations`). FRED provides core macroeconomic data (rates, inflation,
labor, market risk) that is joined to tickers by date at query/attribution time —
NEVER embedded, NEVER in `index_state`, NEVER in `clean_assets`. This step establishes
the Plane-2 ingestion pattern: Bronze archive → Silver numeric table → freshness
reporting, with point-in-time (no-look-ahead) integrity required for evaluation.

### 0.1 Step-0 Findings (What Exists vs What's New)

| Finding | Detail |
|---------|--------|
| **FRED connector EXISTS** | `catalyst_data/connectors/fred.py` — `create_fred_fetcher(api_key, limiter, client)` returns an async `fetch(series_id, endpoint, date)` function. Fetches 30-day observations, filters `"."` missing markers. Returns raw JSON. **AMENDED: needs small extension** — add optional `start_date`, `output_type`, `realtime_start`, `realtime_end` params for per-observation first-release dates (output_type=4). Default behavior unchanged. |
| **Rety/provider_limits/config ALL exist** | `retry.py` has `"fred"` policy (3 retries, 10s base), `provider_limits.py` has `FRED` entry (120/min), `config.py` has `FRED_API_KEY` + `RatePolicy(0.5, 3, None)`. No additions needed. |
| **source_mapping EXISTS** | `"fred_macro"` → `["DFF", "DGS10", "VIXCLS", "UNRATE", "CPIAUCSL"]`. This 5-series set needs expansion to the full curated set. |
| **FRED is NOT ingested anywhere** | No `macro_observations` table, no `_fetch_cell` branch, no FRED writes in the pipeline, no CLI FRED commands. The connector exists but is unused. |
| **No freshness reporting for FRED** | `freshness.py` has no macro section. |
| **TEDRATE is STALE** | Probe confirmed last value ~2022. Must be replaced. |
| **No point-in-time protection** | Current connector fetches latest-revised values — a look-ahead leak for backtesting. |
| **Two-plane architecture already defined** | SEC plan §1 lists `macro_observations` as Plane-2. This plan formalizes it. |

**Bottom line:** 3E is about building the `macro_observations` table, the ingestion pipeline,
point-in-time integrity, and freshness — NOT about building a new connector. The connector is
ready; the ingestion layer is not.

---

## 1. Two-Plane Architecture (Re-stated for 3E)

This is the architecture constraint from the SEC plan §1. 3E is the FIRST Plane-2 table.

**Plane 1 — Prose evidence (embedded via index_state):**
- `articles` + `article_tickers` — Polygon, Finnhub
- `filings` + `filing_documents` — SEC 8-K/10-Q/10-K
- Prose text is L1/L2 chunked and dense-embedded in LanceDB (Step 4)

**Plane 2 — Structured signals (NEVER embedded):**
- `ohlcv` — daily price bars (already exists)
- `macro_observations` — FRED economic indicators **(THIS STEP)**
- `calendar_events` — DEFERRED (Nasdaq earnings)
- `insider_transactions` — DEFERRED (SEC Form 4)
- `market_signals` — DEFERRED (FINRA short interest)
- Plane 2 tables are JOINED at query time via date; their rows NEVER appear in
  `index_state`, NEVER produce vectors, NEVER enter `clean_assets`

**Consumption boundary (explicit):** `macro_observations` is consumed by JOINING on
`observation_date` at query/attribution time. Any agent/query-side wiring is out of
scope for 3E — the packages/agents directory is untouched.

---

## 2. FRED Connector (Minor Extension of Existing)

**File:** `catalyst_data/connectors/fred.py` (extend, don't rewrite)

### 2.1 Current Contract (Unchanged)

```python
def create_fred_fetcher(api_key, limiter=None, client=None) -> callable
    # Returns: await fetch(series_id, endpoint, date) -> FetchResult
```

- `endpoint` = `series_id` (e.g., `"DFF"`)
- `date` = observation end date
- Fetches 30-day lookback: `observation_start = date - 30d`, `observation_end = date`
- Filters `"."` (FRED missing-data marker) from observations
- Returns the FRED JSON response body with filtered observations

**This contract is adequate for 3E. No changes needed.**

### 2.2 Connector Changes (AMENDED — output_type=4 params needed)

The current `fetch` uses default params (latest-revised values, top-level `realtime_start`).
For 3E, add these params to the fetch call:

- `output_type=4` — initial release only (one row per date with first-release value + date)
- `realtime_start=1776-07-04` — capture all historical vintages
- `realtime_end=9999-12-31` — capture all historical vintages

**Backward compatibility:** The `output_type=4` and `realtime_start/realtime_end` params
are added as optional keyword arguments with safe defaults (output_type=None → latest-revised
behavior; realtime_start/end only sent when output_type is set). Existing callers that
don't pass `output_type=4` get the original behavior unchanged.

**Additional date-range extension:** The current `fetch` hardcodes a 30-day lookback.
Extend with an optional `start_date` parameter:
```python
async def fetch(ticker, endpoint, date, start_date=None, output_type=None,
                realtime_start=None, realtime_end=None):
```
Default behavior unchanged; 3E passes `output_type=4, realtime_start=..., realtime_end=...`
for per-observation first-release dates.

### 2.3 API Key + Sanitization

`FRED_API_KEY` is already in `config._PROVIDER_KEY_ENV`. The connector passes it as
`api_key` query param. Sanitize from all error/log strings (replace with `[REDACTED]`).

### 2.4 No New retry/provider_limits/config Needed

`"fred"` entries in all three modules exist and are correct for 120 req/min.

---

## 3. Series Curation

### 3.1 Series Manifest

Replace the hardcoded list in `source_mapping.py` with a curated manifest constant.

**File:** `catalyst_data/pipeline/fred_manifest.py` (new)

| Series ID | Name | Attribution Category | Cadence | Notes |
|-----------|------|---------------------|---------|-------|
| `DFF` | Federal Funds Rate | macro_rates | Daily | Policy rate benchmark |
| `DGS10` | 10-Year Treasury | macro_rates | Daily | Long-end yield |
| `DGS2` | 2-Year Treasury | macro_rates | Daily | Short-end yield (NEW — see §3.2) |
| `T10Y2Y` | 10Y-2Y Spread | macro_rates | Daily | Derived: DGS10 − DGS2. Not a FRED-native series — computed post-fetch from DGS10 and DGS2. |
| `VIXCLS` | VIX Close | market_risk_liquidity | Daily | Fear index |
| `UNRATE` | Unemployment Rate | macro_labor_growth | Monthly | U.S. unemployment |
| `CPIAUCSL` | CPI All-Urban | macro_inflation | Monthly | Consumer price index |
| `PCEPI` | PCE Price Index | macro_inflation | Monthly | Fed's preferred inflation gauge (NEW) |
| `PAYEMS` | Nonfarm Payrolls | macro_labor_growth | Monthly | Employment level (NEW) |
| `GDP` | Gross Domestic Product | macro_labor_growth | Quarterly | U.S. GDP (NEW) |
| `TEDRATE` | ~~TED Spread~~ | ~~market_risk_liquidity~~ | — | **REMOVED** — stale since 2022. Replaced by T10Y2Y. |
| `DAAA` | Moody's Aaa Yield | macro_rates | Daily | Corporate bond benchmark (NEW) |
| `DBAA` | Moody's Baa Yield | macro_rates | Daily | Credit spread reference (NEW) |

**Final set: 12 series** (11 from FRED + 1 derived T10Y2Y).

**Values stored:** First-release (initial print) values via `output_type=4`, NOT latest-revised. This ensures no-look-ahead integrity for historical evaluation.

### 3.2 TEDRATE Replacement

TEDRATE (3-month LIBOR-Treasury spread) was discontinued when LIBOR was retired (~2022).
Replace with the **10Y-2Y Treasury spread** (T10Y2Y), a widely-followed recession/risk
signal.

Implementation: fetch both `DGS10` and `DGS2`, compute `T10Y2Y = DGS10 - DGS2` for
dates where both have non-null values, and store as a derived `macro_observations` row
with `series_id='T10Y2Y'`. This is computed post-fetch in the normalization step, not
requested from FRED (T10Y2Y is not a FRED-native series).

### 3.3 Category Grouping

| Attribution Category | Series |
|---------------------|--------|
| macro_rates | DFF, DGS10, DGS2, DAAA, DBAA, T10Y2Y (derived) |
| macro_inflation | CPIAUCSL, PCEPI |
| macro_labor_growth | UNRATE, PAYEMS, GDP |
| market_risk_liquidity | VIXCLS |

### 3.4 Release Cadence

| Cadence | Series | Staleness Check |
|---------|--------|----------------|
| Daily | DFF, DGS10, DGS2, DAAA, DBAA, VIXCLS | Stale if >1 trading day behind OHLCV watermark |
| Monthly | UNRATE, CPIAUCSL, PCEPI, PAYEMS | Stale if >35 calendar days since last observation |
| Quarterly | GDP | Stale if >100 calendar days since last observation |

---

## 4. Point-in-Time / No-Look-Ahead (REQUIRED)

### 4.1 The Problem

FRED's default `/series/observations` endpoint returns the LATEST-REVISED value for every
date. For example, GDP for 2024-Q1 was initially reported as 1.6%, later revised to 1.4%.
If you attribute a stock move on 2024-04-25 using the REVISED 1.4% value, the model saw
data that didn't exist on 2024-04-25 — this is a look-ahead leak. The prior fundamentals
point-in-time leak already burned this project.

### 4.2 Approach: FRED output_type=4 (Initial Release Only — AMENDED)

**Recommendation:** Store a `released_at` column on each `macro_observations` row,
populated from the PER-OBSERVATION `realtime_start` field returned by FRED's
`output_type=4` ("initial release only"). Define the query-time rule:
"only use observations whose `released_at <= attribution_as_of_date`."

**How output_type=4 works:**
- Request: `/series/observations?series_id=DFF&output_type=4&realtime_start=1776-07-04&realtime_end=9999-12-31&...`
- Returns ONE row per `observation_date` with:
  - `value` = the FIRST-RELEASED value (initial print, not latest-revised)
  - `realtime_start` = the date that initial value was first published (per-observation, NOT top-level)
- This is NOT ALFRED vintage explosion — it is one row per date with the true first-release date embedded per observation.
- The wide realtime window (1776–9999) ensures every observation's initial release is captured regardless of how far back the series goes.

**Why not ALFRED vintages:**
- ALFRED without `output_type=4` returns ALL revisions of every observation — tens of thousands of rows.
- With `output_type=4`, ALFRED returns exactly one row per date with the FIRST release — no explosion.
- This is the sweet spot: per-observation precision without the storage cost.

**What this fixes vs the old approach:**
- OLD: `realtime_start` from the top-level response metadata = the fetch date → useless for historical eval.
- NEW: `realtime_start` from each observation row = the TRUE first-release date → `WHERE released_at <= as_of` correctly excludes not-yet-released data for historical attribution events.

### 4.3 Schema Columns

```sql
observation_date TEXT NOT NULL,    -- FRED 'date' field (YYYY-MM-DD)
value           REAL,              -- numeric value (NULL if '.' missing)
released_at     TEXT,              -- FRED 'realtime_start' — first-release date (ISO)
fetched_at      TEXT NOT NULL,     -- when Catalyst fetched this observation
```

**Query-time rule (for attribution consumers):**
```sql
WHERE released_at <= :as_of_date
```

### 4.4 Implementation (AMENDED — output_type=4)

The connector fetches with the following params for no-look-ahead integrity:

```
series_id=SERIES
output_type=4
realtime_start=1776-07-04
realtime_end=9999-12-31
observation_start=<backfill_start>
observation_end=<backfill_end>
api_key=<key>
file_type=json
```

**Response shape (per observation):**
```json
{
  "realtime_start": "2026-03-15",
  "realtime_end": "9999-12-31",
  "date": "2026-02-01",
  "value": "4.1"
}
```

- `date` → `macro_observations.observation_date`
- `value` → `macro_observations.value` (first-release value, not latest-revised)
- `realtime_start` → `macro_observations.released_at` (true first-release date)

The normalization step extracts `realtime_start` from EACH observation object and
populates `released_at` — it NEVER uses a top-level field or the fetch date.

**No "observation_date + release_lag" approximation.** The `output_type=4` response
directly provides the true first-release date per observation. The query-time rule
`WHERE released_at <= as_of_date` works correctly for historical evaluation.

---

## 5. macro_observations Schema (Plane-2)

**File:** `catalyst_data/storage/sqlite.py` (additive DDL)

### 5.1 DDL

```sql
CREATE TABLE IF NOT EXISTS macro_observations (
    series_id        TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    value            REAL,              -- NULL for missing (FRED ".")
    released_at      TEXT,              -- FRED realtime_start proxy
    fetched_at       TEXT NOT NULL DEFAULT (datetime('now')),
    raw_asset_id     TEXT,              -- FK to raw_assets
    PRIMARY KEY (series_id, observation_date),
    FOREIGN KEY (raw_asset_id) REFERENCES raw_assets(asset_id)
);
CREATE INDEX IF NOT EXISTS idx_macro_obs_date
    ON macro_observations(observation_date);
CREATE INDEX IF NOT EXISTS idx_macro_obs_series_date
    ON macro_observations(series_id, observation_date);
```

### 5.2 Design Invariants

- **No ticker column.** Macro is universal — joined to any ticker by `observation_date`
  at query time.
- **No index_state rows.** `macro_observations` is Plane-2 — its rows never appear in
  `index_state`, never produce vectors, never enter `clean_assets`.
- **No `index_builder` integration.** `build_index_records()` and
  `build_incremental_records()` must NOT query `macro_observations`.
- **`raw_asset_id` FK** links each observation back to its Bronze parent for provenance.
- **`value` is REAL, nullable.** FRED uses `"."` for missing observations — these are
  stored as NULL, not 0.0.
- **T10Y2Y (derived):** Not fetched from FRED — computed as `DGS10.value - DGS2.value`
  where both are non-NULL on the same date, stored as `series_id='T10Y2Y'` with
  `released_at = max(DGS10.released_at, DGS2.released_at)`.

### 5.3 Upsert Helper

```python
def upsert_macro_observation(
    conn, *, series_id, observation_date, value, released_at, raw_asset_id
) -> None:
    """INSERT OR REPLACE into macro_observations."""
```

### 5.4 Migration

Add `ensure_macro_tables(conn)` called from `init_db()`. The function also calls
`ensure_filings_tables()` pattern — CREATE TABLE IF NOT EXISTS, additive-only.

---

## 6. Bronze Archive

Store each raw FRED series-observations JSON response in `raw_assets`.

### 6.1 Asset Identity

- `asset_id = compute_asset_id(series_id, fetch_date, "fred_macro")`
  where `series_id` is the series ID (e.g., `"DFF"`) and `fetch_date` is the
  `observation_end` date used in the request.
- `source_type = "fred_macro"`
- `reference_date = fetch_date` (the observation_end of the fetch window)
- `content_raw = raw_json_bytes` (upsert_raw_asset compresses internally)
- `metadata = {"series_id": series_id, "observation_count": len(observations), "observation_start": start_date, "observation_end": end_date}`

### 6.2 Re-derivability

`macro_observations` (Silver) is derived from Bronze. A `rederive_fred_macro()`
function reads raw_assets WHERE `source_type='fred_macro'`, decompresses, and
re-populates `macro_observations`. This is tested via a Bronze re-derivability
test: decompress → re-normalize → rows match.

### 6.3 Provenance Chain

`raw_assets` → `macro_observations` (via `raw_asset_id` FK) → query-time JOIN by date.

---

## 7. Pipeline Integration (Dedicated CLI Command)

### 7.1 Why NOT a `_fetch_cell_*` Branch

FRED is series-scoped, not (ticker, date)-cell-scoped like Polygon/Finnhub/SEC.
Adding FRED to `run_update_batch`'s (ticker, date) cell model would require awkward
parameterization (series_id in place of ticker? all series every day?) and breaks the
`compute_missing_cells` model which assumes (ticker, date, source) triplets.

**Recommendation: Dedicated CLI command `update-macro`**, separate from
`run_update_batch`. FRED is fetched independently and stored in its own Plane-2
table — it should not be shoehorned into the news/filings cell model.

### 7.2 CLI Command

```
python -m catalyst_data.cli_index update-macro
    [--series DFF,DGS10,...]   # default: all 12 curated series
    [--from YYYY-MM-DD]        # observation start (default: latest observation + 1 day)
    [--to YYYY-MM-DD]          # observation end (default: today)
    [--dry-run]                # compute-only, zero network, zero DB writes
    [--db PATH]
```

### 7.3 Implementation Flow

1. Resolve series list (from `--series` or the curated manifest constant)
2. For each series, fetch observations via `fred_fetcher.fetch(series_id, series_id, date, start_date=from_date)`
   with date-range spanning `[from_date, to_date]` (FRED allows multi-year windows in
   one request — no per-day requests needed)
3. Store raw JSON response in `raw_assets` (Bronze)
4. Normalize observations → upsert into `macro_observations` (Silver)
5. Compute T10Y2Y derived series
6. Report: series_id, observations fetched, observations upserted

### 7.4 Dry-Run Invariant

`--dry-run` prints the fetch plan (series list, date range, expected observation count
from metadata) without making network calls or DB writes. The FRED fetcher is never
constructed.

### 7.5 Source Mapping Update

Update `source_mapping.py` to return the full 12-series list:
```python
if source == "fred_macro":
    return ["DFF", "DGS10", "DGS2", "VIXCLS", "UNRATE", "CPIAUCSL",
            "PCEPI", "PAYEMS", "GDP", "DAAA", "DBAA"]
```
(DGS2 is included for T10Y2Y derivation; T10Y2Y itself is derived, not fetched.)

---

## 8. Freshness

**Modify:** `catalyst_data/freshness.py` — add `macro_freshness(conn) -> dict`.

### 8.1 Per-Series Reporting

| Series | Cadence | Staleness Threshold |
|--------|---------|-------------------|
| DFF, DGS10, DGS2, DAAA, DBAA, VIXCLS | Daily | >1 trading day behind OHLCV watermark |
| UNRATE, CPIAUCSL, PCEPI, PAYEMS | Monthly | >35 calendar days |
| GDP | Quarterly | >100 calendar days |
| T10Y2Y (derived) | Daily | Same as DGS10/DGS2 |

### 8.2 Report Shape

```
=== MACRO Freshness ===
  Series    Cadence   Latest Obs   Status    Days Behind
  DFF       daily     2026-07-01   FRESH     0
  DGS10     daily     2026-07-01   FRESH     0
  CPIAUCSL  monthly   2026-06-15   FRESH     17
  GDP       quarterly 2026-03-31   FRESH     93
  TEDRATE   —         —            REMOVED   —
```

### 8.3 Integration

Add `macro_freshness()` section to `freshness_report(conn)`. A series is "stale" only
when its latest observation exceeds its cadence-specific threshold — not between
scheduled releases. Monthly series are NOT stale 20 days after the last release if
the next release isn't expected for 30 days.

---

## 9. Consumption Boundary (State Explicitly, Build Nothing)

`macro_observations` is consumed by JOINING on `observation_date` at query/attribution
time. Example query for an agent:

```sql
SELECT m.series_id, m.value, m.observation_date
FROM macro_observations m
WHERE m.observation_date BETWEEN :event_date - 5 AND :event_date
  AND m.released_at <= :event_date
ORDER BY m.observation_date
```

**Out of scope for 3E:**
- Agent-side query wiring (packages/agents untouched)
- Integration with the Critic or attribution engine
- Macro-data-aware retrieval policies
- LanceDB embedding of macro data (it's Plane-2, never embedded)

---

## 10. Tests

### 10.1 Test Fixture

**New fixture:** `tests/fixtures/fred_observations_DFF.json`

A real, redacted FRED response for DFF with ~30 daily observations. API key stripped.
Document source URL in `tests/fixtures/README.md`.

**Fixture properties:**
- 30 daily observations for DFF from a recent 30-day window
- Includes `realtime_start`/`realtime_end` metadata
- At least one observation has a non-trivial value (not all same)
- No `"."` missing values in the fixture (filtering is tested separately)

### 10.2 Test Files

**`tests/test_fred_connector.py`** (6 tests — extend existing if there is one):

1. **`test_series_observations_200`** — Mocked 200 response → FetchResult with correct structure, observations filtered for `"."`.
2. **`test_missing_dot_filtered`** — Response includes `{"value": "."}` → filtered out of observations list.
3. **`test_api_key_as_query_param`** — Assert `api_key` is in query params, not headers.
4. **`test_limiter_honored`** — Mock limiter, assert `acquire()` called.
5. **`test_extended_date_range`** — Verify `start_date` parameter produces correct `observation_start` in URL.
6. **`test_http_error`** — Mocked 500 → error in FetchResult.

**`tests/test_fred_normalize.py`** (6 tests):

1. **`test_observations_parsed_to_rows`** — FRED JSON → list of (series_id, date, value, released_at) tuples.
2. **`test_null_value_for_missing`** — "." value → stored as NULL in macro_observations.
3. **`test_numeric_value_conversion`** — "4.33" → 4.33 REAL.
4. **`test_t10y2y_derived`** — DGS10=4.5, DGS2=4.0, both on 2026-07-01 → T10Y2Y = 0.5.
5. **`test_point_in_time_excludes_future`** — Query with `released_at <= '2026-06-01'` excludes an observation released on 2026-06-15.
6. **`test_bronze_rederivability`** — Store raw_asset → decompress → re-normalize → macro_observations rows match.

**`tests/test_fred_freshness.py`** (4 tests):

1. **`test_daily_series_stale_after_2_days`** — DFF latest obs 2 trading days behind → STALE.
2. **`test_monthly_series_fresh_within_35_days`** — CPIAUCSL latest obs 20 days ago → FRESH (within 35-day threshold).
3. **`test_quarterly_series_fresh_within_100_days`** — GDP latest obs 90 days ago → FRESH.
4. **`test_removed_series_not_reported`** — TEDRATE not in curated manifest → not in freshness report.

**`tests/test_fred_pipeline.py`** (4 tests):

1. **`test_update_macro_single_series`** — Mocked fetch for DFF → raw_asset stored, macro_observations populated.
2. **`test_update_macro_derived_t10y2y`** — Fetch DGS10 + DGS2 → T10Y2Y computed and stored.
3. **`test_update_macro_idempotent`** — Run twice → no duplicate rows, same observation count.
4. **`test_dry_run_zero_writes`** — dry-run → zero new rows in raw_assets, zero macro_observations.

### 10.3 Frozen DB

SHA-256 must remain `0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf`.
All tests use `:memory:` or `tmp_path`.

---

## 11. File Summary

### Created (4 files)

| File | Purpose |
|------|---------|
| `catalyst_data/pipeline/fred_manifest.py` | Curated series manifest constant + category mapping |
| `catalyst_data/pipeline/fred_normalize.py` | FRED JSON → macro_observations normalization + T10Y2Y derivation |
| `tests/test_fred_normalize.py` | Normalization + Bronze re-derivability tests (6) |
| `tests/test_fred_freshness.py` | Macro freshness tests (4) |
| `tests/fixtures/fred_observations_DFF.json` | Redacted FRED DFF fixture |

### Modified (6 files)

| File | Change |
|------|--------|
| `catalyst_data/connectors/fred.py` | Extend `fetch` with optional `start_date` parameter |
| `catalyst_data/storage/sqlite.py` | Add `macro_observations` DDL + `ensure_macro_tables()` + `upsert_macro_observation()` |
| `catalyst_data/source_mapping.py` | Expand FRED series list to 12 |
| `catalyst_data/freshness.py` | Add `macro_freshness()` section |
| `catalyst_data/cli_index.py` | Add `update-macro` subcommand |
| `tests/test_fred_connector.py` | Add extended date-range test |

### Extended (1 file)

| File | Change |
|------|--------|
| `tests/test_fred_connector.py` | (if it exists — extend; if not, create with 6 tests) |

### Test Count Summary

| Area | Tests |
|------|-------|
| Connector (existing + new) | ~6 |
| Normalization + point-in-time + Bronze | 6 |
| Freshness (cadence-aware) | 4 |
| Pipeline integration | 4 |
| **Total (new)** | **20** |

All tests fixture-only. Zero network. Frozen DB SHA unchanged.

---

## 12. DEFERRED (Document-Only — Do NOT Build in 3E)

These are Plane-2 structured-signals add-ons, gated behind FRED. Each follows the SAME
Plane-2 pattern: its own numeric table, NEVER embedded, joined at query time by date
(and ticker where applicable). They are NOT separate steps — they are add-ons to 3E
that can be picked up individually when needed.

### 12.1 SEC Form 4 (Insider Transactions) — DEFERRED

**Table:** `insider_transactions`
**Key columns:** `filing_date, ticker, insider_name, relationship, transaction_code, shares, price, value`
**Why deferred:** SEC Form 4 needs a namespace-aware XML parser (the SEC Edgar XBRL/XML
format is not simple HTML like 8-K filings). Only owner/issuer transactions are
proven reliable; non-owner insider signals are noisy. Parser complexity is the gating
factor — not the data source.
**Proposed when built:** Add as `_fetch_cell` branch similar to SEC filings, with a
dedicated `insider_transactions` Plane-2 table.

### 12.2 FINRA Short Interest — DEFERRED

**Table:** `market_signals` (with `signal_type='short_interest'`)
**Key columns:** `settlement_date, ticker, short_interest, avg_daily_volume, days_to_cover`
**Why deferred:** FINRA releases short interest bi-monthly as batch files. The data
is useful for `technical_flow` attribution but the low cadence (bi-monthly) limits
its daily-trading utility. Lower urgency than daily-sourced signals.
**Proposed when built:** Standalone CLI command `update-market-signals --signal short_interest`,
downloading batch files from FINRA's public FTP.

### 12.3 Tiingo EOD Audit — DEFERRED

**Table:** `market_data_audit` (or extend `ohlcv`)
**Key columns:** `date, ticker, adj_close, adj_open, adj_high, adj_low, adj_volume, dividend, split_factor`
**Why deferred:** Tiingo EOD provides adjusted OHLCV with dividends and splits — useful
for cross-validating Polygon's OHLCV data, but our OHLCV table is already populated
and reliable. This is an audit tool, not a bottleneck.
**Proposed when built:** Standalone CLI command `audit-ohlcv`, fetching Tiingo EOD
for [from_date, to_date] and diffing against existing `ohlcv` rows.

### 12.4 Nasdaq Earnings Calendar — DEFERRED

**Table:** `calendar_events` (with `event_type='earnings'`)
**Key columns:** `ticker, event_date, event_type, reported_eps, estimated_eps, surprise_pct`
**Why deferred:** Only 4/10 universe tickers are covered by the Nasdaq API (AAPL, AMZN,
GOOGL, JPM — all NYSE-listed; AMD/MSFT/NVDA are NASDAQ-listed but returned empty).
For earnings event dates, we can derive them from `filings` table: 8-K Item 2.02
filings (already ingested in 3C). This is PREFERRED over the Nasdaq API — it's more
reliable (10/10 coverage) and uses data we already have.
**Proposed when built:** A `derive_earnings_from_filings()` function that queries
`filings WHERE form_type='8-K' AND items_json LIKE '%2.02%'` — no new API calls needed.

---

## 13. Risk Register

| Risk | Severity | Mitigation |
|------|----------|------------|
| Point-in-time leak from latest-revised values | **HIGH** | Store `released_at` column; query-time rule `WHERE released_at <= as_of_date`. Documented limitation: per-observation release precision requires ALFRED vintages (deferred). |
| FRED API key exposed in logs | Medium | Sanitize `api_key` from all error/log strings — replace with `[REDACTED]`. Already pattern in Finnhub connector. |
| Series coverage incomplete for attribution categories | Low | 12-series curated set covers macro_rates, macro_inflation, macro_labor_growth, market_risk_liquidity. Expandable via manifest constant. |
| FRED rate limit (120/min) insufficient for backfill | Low | 12 series × 1 request per series = 12 requests per backfill run. At 0.5s/req = 6 seconds total. Extremely low risk. |
| T10Y2Y derivation depends on DGS10 + DGS2 both being non-null | Low | Both are daily series; near-zero chance of one being missing on a trading day. If one is null, T10Y2Y is null for that date. |
| Freshness cadence model too simplistic | Low | Cadence thresholds (daily=2d, monthly=35d, quarterly=100d) are reasonable defaults. FRED series have predictable release schedules — can be refined with a per-series calendar later. |

---

## 14. Open Questions for Review

### (a) Point-in-Time Approach — Release-Date Column vs ALFRED Vintages

**Recommendation:** Release-date column (`released_at` via `realtime_start` proxy).
ALFRED vintages are more precise but explode storage and add complexity without
proportional value for daily stock attribution where macro is a context layer.
The release-date approach prevents the worst look-ahead leaks (future observations)
while accepting minor revision imprecision.

### (b) TEDRATE Replacement

**Recommendation:** 10Y-2Y Treasury spread (T10Y2Y), derived from DGS10 and DGS2.
This is the standard recession/risk signal that replaced TED spread after LIBOR
retirement. FRED-native alternatives (T10YIE, BAMLC0A0CM) are equity/credit focused
but the 10Y-2Y is more universally referenced.

### (c) Fetch/CLI Integration Model

**Recommendation:** Dedicated CLI command `update-macro`, separate from
`run_update_batch`. FRED is series-scoped (not ticker-scoped) and all 12 series
can be fetched in 12 requests (one per series) for a multi-year window — the
(ticker, date)-cell model of `run_update_batch` would require 12 × N_days requests
which is wasteful and doesn't fit the compute_missing_cells model.

### (d) Any Existing FRED Data to Migrate?

**Recommendation:** Build fresh. No `macro_observations` table exists, no FRED
data is ingested anywhere in the current schema. The connector is ready but
unused. 3E starts from scratch — no migration needed. The source_mapping list
(5 series) is superseded by the new 12-series manifest.

---

## 15. Guardrails (All Apply)

- ✅ data-core only — no `packages/app` or `packages/agents`
- ✅ Dev DB `data/catalyst_dev_ws4b.db` only
- ✅ Frozen DB `data/catalyst_eval_frozen_v2.db` read-only, SHA `0dfc81b154a9...` unchanged
- ✅ No new dependencies — `httpx` + stdlib only
- ✅ No embeddings, no LanceDB, no model loads on Mac
- ✅ No network in tests (fixtures only)
- ✅ No commit — plan only
- ✅ No implementation code beyond DDL/contract snippets
- ✅ English only in code/comments
- ✅ Never log the FRED API key — sanitize from all error/log strings
- ✅ `macro_observations` is Plane-2 — NEVER embedded, NEVER in `index_state`, NEVER in `clean_assets`

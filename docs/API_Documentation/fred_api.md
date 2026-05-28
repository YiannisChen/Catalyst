> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# FRED® API — Catalyst Data-Core

> **Scope:** REST usage for macro series. Authoritative reference: [FRED API documentation](https://fred.stlouisfed.org/docs/api/fred/). **Connector:** `fred.py` calls **`series/observations`** for the IDs in `FRED_SERIES_MAP`. **Convention:** [README](./README.md).

---

## What FRED provides

Economic time series (rates, CPI, unemployment, VIX, etc.) from the St. Louis Fed. Two API versions exist; most client code uses **v1-style REST** under `api.stlouisfed.org/fred`.

---

## API key

- Request a key: [FRED API Keys](https://fred.stlouisfed.org/docs/api/api_key.html)  
- Pass on every request as **`api_key=YOUR_KEY`** (query parameter).

---

## Base URL

```
https://api.stlouisfed.org/fred/
```

All paths below are relative to this base.

---

## Endpoints you actually need first

| Endpoint | Purpose |
|----------|---------|
| **`series/observations`** | **Values** for one series (date + value). Main backfill target. |
| `series` | Metadata for a series (title, frequency, units, seasonal adjustment). |
| `series/search` | Find `series_id` by text query. |
| `series/categories` | Category tree for a series. |

### `series/observations` — common parameters

| Parameter | Meaning |
|-----------|---------|
| `series_id` | **Required.** e.g. `GDP`, `UNRATE`, `DGS10`. |
| `observation_start` | First date `YYYY-MM-DD` (inclusive). |
| `observation_end` | Last date (inclusive). |
| `limit` | Max rows (use with `sort_order` for “latest N”). |
| `sort_order` | `asc` or `desc`. |
| `units` | `lin`, `chg`, `ch1`, `pch`, `pc1`, `pca`, `cch`, `cca`, `log` (see official docs). |
| `frequency` | Aggregation frequency if supported. |
| `aggregation_method` | `avg`, `sum`, `eop`, etc. |

Missing or invalid values often appear as `"."` in JSON — **filter those out** (already done in `fred.py`).

---

## Discovery workflow (new series)

1. **`series/search`** → candidate `series_id` values.  
2. **`series`** → confirm frequency, units, title.  
3. **`series/observations`** → pull history with `observation_start` / `observation_end` for your 3-year (or full) window.

---

## Archived path index (original outline scrape)

Use the [official FRED API docs](https://fred.stlouisfed.org/docs/api/fred/) for parameters; this list is only a **table of contents**.

**Categories:** `fred/category`, `fred/category/children`, `fred/category/related`, `fred/category/series`, `fred/category/tags`, `fred/category/related_tags`

**Releases:** `fred/releases`, `fred/releases/dates`, `fred/release`, `fred/release/dates`, `fred/release/series`, `fred/release/sources`, `fred/release/tags`, `fred/release/related_tags`, `fred/release/tables`

**Series:** `fred/series`, `fred/series/categories`, `fred/series/observations`, `fred/series/release`, `fred/series/search`, `fred/series/search/tags`, `fred/series/search/related_tags`, `fred/series/tags`, `fred/series/updates`, `fred/series/vintagedates`

**Sources:** `fred/sources`, `fred/source`, `fred/source/releases`

**Tags:** `fred/tags`, `fred/related_tags`, `fred/tags/series`

**Maps API:** shape files, series group meta, series regional data, regional data (see FRED site).

---

## Official links

- **FRED API overview:** https://fred.stlouisfed.org/docs/api/fred/  
- **API key:** https://fred.stlouisfed.org/docs/api/api_key.html  
- **Series in Catalyst today:** `DFF`, `DGS2`, `DGS10`, `T10Y2Y`, `CPIAUCSL`, `UNRATE`, `VIXCLS`, `BAMLH0A0HYM2` — see `FRED_SERIES_MAP` in `fred.py`.

---

## Rate limits

FRED documents fair-use and throttling; batching many `series_id` in one “macro bundle” (as in `fred.py`) reduces round-trips. Add small delays between calls if you expand to dozens of series.

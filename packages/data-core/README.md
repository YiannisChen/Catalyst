# Catalyst Data-Core: V1 Context Factory

> High-concurrency ingestion, physical denoising, and SQLite-first caching for multi-agent US stock attribution.

## V1 Scope (Frozen)

- Build a robust SQLite pipeline only (`WAL` mode).
- Do not implement LanceDB in V1.
- Follow `SCHEMA_DESIGN.md` as the only schema source of truth.
- No LLM summarization/compression in Data-Core.

## Core Rules

1. **Schema truth:** `SCHEMA_DESIGN.md` is authoritative for `CatalystDataRequest` and `DataAsset`.
2. **Timezone discipline:** Store physical time in UTC and expose ET-formatted display strings.
3. **No UI logic:** Data-Core only outputs validated `DataAsset` records.
4. **Dedup:** Physical dedup only via `asset_id = SHA256(ticker + date + source_type + data_version)`.

## V1 Pipeline

1. Validate request with `CatalystDataRequest`.
2. Compute `asset_id` and apply cache check (with source-specific TTL rules).
3. If miss/expired/`force_refresh=True`, fetch from network using async connectors.
4. Apply retries (max 3) and backoff (2s, 4s, 8s) for retryable failures.
5. Apply source fallback policy (FMP -> yfinance) only for configured failure classes.
6. Run physical transmuter:
   - HTML: remove `nav/footer/script/style/aside`, extract links to numbered references.
   - Financial JSON: produce Markdown table output.
7. Compress `content_raw` using `zlib` and persist to SQLite.
8. Return normalized `DataAsset`.

## Source Priority and Fallback

- Primary: FMP; fallback: yfinance.
- Missing API key or 401/403 from FMP -> immediate fallback to yfinance.
- 429/5xx/timeout from FMP -> retry 3 times, then fallback to yfinance.
- Missing fields/data conflict -> keep FMP result, log warning, do not fallback.

## Cache and Refresh Rules

- `gdelt_news`: infinite TTL.
- `fmp_fundamentals`:
  - `is_final=True`: infinite TTL.
  - `is_final=False`: TTL 24h.
- `force_refresh=True`: bypass cache read, fetch live, overwrite existing SQLite record.

## Concurrency, Retry, and Logging

- Per-source concurrency limit: `asyncio.Semaphore(5)`.
- Retry policy: max retries = 3, exponential backoff = `2s -> 4s -> 8s`.
- On terminal failure after retries: return a structured error object.
- Minimum JSON log fields per fetch:
  - `timestamp`, `asset_id`, `source`, `latency_ms`, `http_status`, `cache_hit`, `retry_count`.

## Logical Source Mapping

- `sources=["fmp_fundamentals"]` maps to:
  - income statement
  - balance sheet
  - cash flow statement
- These are merged into one `DataAsset` response payload for that logical source.

## Tech Stack (V1)

- Python 3.10+
- `asyncio`, `httpx` (async-first)
- `pydantic` v2
- `sqlite3` with WAL pragmas
- `zlib`
- `beautifulsoup4` (or equivalent deterministic HTML cleaner)

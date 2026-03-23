# Data-Core Schema and Interfaces

This document defines the strict input/output contracts for Data-Core V1.

## Authority

- This file is the single source of truth for Data-Core schema.
- Do not add fields not defined here.
- Do not implement LanceDB in V1.

## 1) Input Interface: `CatalystDataRequest`

All inbound requests to the Data-Core orchestrator must validate against this model.

```python
from pydantic import BaseModel, Field
from typing import List


class CatalystDataRequest(BaseModel):
    ticker: str = Field(..., description="Stock ticker symbol, e.g. 'NVDA'")
    date: str = Field(
        ...,
        description="Business date in ET, format YYYY-MM-DD"
    )
    sources: List[str] = Field(
        ...,
        description="Requested logical sources, e.g. ['fmp_fundamentals', 'gdelt_news']"
    )
    force_refresh: bool = Field(
        default=False,
        description="If True, bypass cache read, fetch live data, and overwrite existing SQLite record"
    )
```

### Logical Source Mapping (V1)

- `fmp_fundamentals` must map to:
  - income statement
  - balance sheet
  - cash flow statement
- The mapped outputs are merged into one logical `DataAsset`.

## 2) Output and Persistence Model: `DataAsset`

This schema is used both for API output payloads and for SQLite table mapping.

```python
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class DataAsset(BaseModel):
    # Core identity and dedup
    asset_id: str = Field(
        ...,
        description="Primary key: SHA256(ticker + date + source_type + data_version)"
    )
    ticker: str = Field(..., description="Ticker symbol, e.g. 'NVDA'")
    source_type: str = Field(
        ...,
        description="Source descriptor, e.g. 'fmp_fundamentals', 'gdelt_news', 'sec_filings'"
    )

    # Time semantics
    reference_date_utc: datetime = Field(
        ...,
        description="Physical UTC timestamp for event/fact time"
    )
    reference_date_et: str = Field(
        ...,
        description="Human-readable ET timestamp, e.g. '2026-01-15 16:00 ET'"
    )
    last_updated: datetime = Field(
        ...,
        description="UTC timestamp when this record is fetched/inserted"
    )

    # Versioning/state
    data_version: str = Field(
        default="v1",
        description="Physical transmuter version used for this asset"
    )
    is_final: bool = Field(
        default=True,
        description="False for restatable/preliminary data; True for stable/final data"
    )

    # Payload
    content_raw: Optional[bytes] = Field(
        default=None,
        description="Raw JSON/HTML bytes, stored compressed (zlib) in SQLite"
    )
    content_clean: str = Field(
        ...,
        description="Deterministically cleaned Markdown content"
    )

    # Observability metadata
    metadata: dict = Field(
        default_factory=dict,
        description="Flexible key-value map for URL, latency, status, and fetch details"
    )
```

## 3) V1 Time Semantics Rules

- For news/filings: `reference_date_utc` must come from source published/filing timestamp.
- For daily market/fundamentals: use `16:00:00 US/Eastern` of the request date, then convert to UTC.

## 4) Cache TTL Rules

- `gdelt_news`: infinite TTL.
- `fmp_fundamentals`:
  - `is_final=True`: infinite TTL.
  - `is_final=False`: TTL 24 hours.

## 5) Fallback and Retry Rules

- Primary source for fundamentals: FMP. Fallback: yfinance.
- Missing API key or FMP 401/403: immediate fallback to yfinance.
- FMP 429/5xx/timeout: retry 3 times with backoff `2s, 4s, 8s`, then fallback.
- Missing fields or data conflict: do not fallback; keep FMP data and log warning.

## 6) Deterministic Cleaning Acceptance Criteria

- HTML cleaning must remove these tags completely:
  - `<nav>`, `<footer>`, `<script>`, `<style>`, `<aside>`
- Extract links and append numbered references at the bottom:
  - Example format: `[3](https://example.com)`
- Financial/JSON output must include Markdown table structure.
  - Minimum assertion: output contains `|---|---|`.

## 7) SQLite Requirements

On every SQLite connection, execute:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
```

## 8) Minimum Fetch Log Schema (JSON)

Each fetch attempt must log at least:

```json
{
  "timestamp": "ISO-8601",
  "asset_id": "sha256...",
  "source": "fmp_fundamentals",
  "latency_ms": 123,
  "http_status": 200,
  "cache_hit": false,
  "retry_count": 1
}
```
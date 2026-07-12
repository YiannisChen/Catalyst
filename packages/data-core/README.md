# catalyst-data

Financial data ingestion, cleaning, and storage for the Catalyst agent system.

Implements a **medallion pipeline** — raw provider responses (Bronze) are cleaned and deduplicated into structured Markdown (Silver), then embedded into a vector index (Gold) — with five data source connectors, cross-source deduplication, and provider-level rate limiting.

---

## Installation

```bash
# Core (no vector dependencies)
pip install -e packages/data-core

# With dev dependencies (pytest, pytest-asyncio)
pip install -e "packages/data-core[dev]"

# With LanceDB vector support
pip install -e "packages/data-core[vector]"
```

**Requirements:** Python ≥ 3.11

---

## Configuration

All settings are read from environment variables with sensible defaults.

```bash
cp packages/data-core/.env.template .env
```

| Variable | Required | Description |
|----------|----------|-------------|
| `POLYGON_API_KEY` | Yes | Polygon.io OHLCV + news |
| `FMP_API_KEY` | Yes | Financial Modeling Prep fundamentals |
| `FRED_API_KEY` | Yes | FRED macro indicators |
| `FINNHUB_API_KEY` | No | Optional supplemental news |
| `SEC_USER_AGENT` | No | SEC EDGAR user-agent string |
| `CATALYST_DB_PATH` | No | SQLite path (default: `data/catalyst_dev.db`) |
| `CATALYST_RAG_MIN_CHAR_COUNT` | No | Minimum chunk size for RAG (default: 200) |

---

## Package Structure

```
catalyst_data/
├── connectors/         # Provider API clients
│   ├── polygon.py      # OHLCV and news events
│   ├── fmp.py          # Income statement, balance sheet, cash flow
│   ├── fred.py         # FRED macro time series
│   └── gdelt.py        # Global news events (free, no key)
├── storage/
│   ├── sqlite.py       # WAL-mode SQLite with medallion schema
│   └── lancedb_store.py  # LanceDB vector index (Gold layer)
├── pipeline/           # Orchestrated ingestion runs
├── dedup/              # Cross-source deduplication
├── transmuter.py       # HTML → Markdown cleaning
├── quality.py          # Asset quality flags and spam detection
├── rate_limiter.py     # Per-provider token bucket + key pool
└── config.py           # Environment-based configuration
```

---

## Storage Schema

**SQLite** (Bronze/Silver layers):

| Table | Description |
|-------|-------------|
| `raw_assets` | Raw provider responses (Bronze) |
| `clean_assets` | Cleaned Markdown (Silver) |
| `ohlcv` | Daily OHLCV prices |
| `news_alignment` | News↔price event alignment |
| `attributions` | Attribution outputs |
| `golden_events` | Frozen evaluation cases |
| `ingestion_runs` | Ingestion run history |
| `agent_runs` | Agent execution records |
| `trace_events` | Node-level trace events |

**LanceDB** (Gold layer): BGE-M3 embeddings for hybrid retrieval.

---

## Running Tests

```bash
cd packages/data-core
python -m pytest tests/ -q
```

Most connector tests use mock responses and do not require live API keys. Tests that hit real APIs are marked `@pytest.mark.live`.

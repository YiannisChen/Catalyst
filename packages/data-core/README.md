# catalyst-data

Financial data ingestion, cleaning, and storage for the Catalyst agent system.

Implements the **medallion pipeline** (Bronze → Silver → Gold) with multi-source
connectors (Polygon, FMP, FRED, GDELT, yfinance), cross-source deduplication,
quality flags, and dual storage backends (SQLite + LanceDB).

## Local Dev Setup

```bash
cd /path/to/Catalyst
pip install -e packages/data-core
cp packages/data-core/.env.template .env
```

## Key Modules

| Module | Purpose |
|--------|---------|
| `connectors/` | Provider-specific API clients with rate limiting and key pooling |
| `storage/sqlite.py` | WAL-mode SQLite for structured data (Bronze/Silver) |
| `storage/lancedb_store.py` | LanceDB vector index for embedding-based retrieval (Gold) |
| `quality.py` | Asset quality flags and template-spam detection |
| `config.py` | Environment-based configuration with sensible defaults |

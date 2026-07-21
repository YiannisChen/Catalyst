# catalyst-data

Data ingestion and retrieval foundations for Catalyst.

## Responsibilities

- provider connectors for Polygon, Finnhub, FMP, SEC, FRED, and yfinance fallback;
- deterministic update planning and plan hashes;
- SQLite-backed Raw Source Archive and Canonical Domain Store;
- article, filing, OHLCV, macro, checkpoint, and run-state schemas;
- deduplication, corpus eligibility, chunking, and retrieval-index artifacts;
- rate limits, retries, fallback policy, quality checks, and migrations.

Functional terminology is used throughout new APIs:

| Layer | Meaning |
|---|---|
| Raw Source Archive | Provider responses and request provenance |
| Canonical Domain Store | Normalized articles, ticker links, OHLCV, filings, and macro observations |
| Retrieval Corpus | Versioned searchable document chunks |
| Retrieval Index | Lexical and vector indexes derived from a corpus manifest |

## Install and test

```bash
pip install -e "packages/data-core[dev]"
.venv/bin/python -m pytest packages/data-core -q
```

Vector dependencies are optional:

```bash
pip install -e "packages/data-core[vector]"
```

Tests use recorded or synthetic fixtures. Live provider calls require explicit operator authorization.

# catalyst-data

Local data storage, point-in-time reads, retrieval, and the daily update used by Catalyst.

## Responsibilities

- read and maintain local market, filing, and news stores;
- expose time-bounded evidence and market context to the application;
- build and query the supported retrieval indexes;
- report daily update and data readiness status.

## Source layout

| Directory | Responsibility |
|---|---|
| `catalyst_data/ingestion/` | Supported collectors and daily update operations |
| `catalyst_data/read/` | Read-side market, filing, and readiness access |
| `catalyst_data/retrieval/` | Search and evidence retrieval |
| `catalyst_data/storage/` | SQLite connections and schema helpers |

Runtime data is stored in the ignored `.local/data/ingestion/` directory for a
clean checkout. When an existing `data/m9/` contains a recognized runtime
store, the full store family remains there. `CATALYST_RUNTIME_DATA_DIR` selects
an explicit root; store-specific variables take precedence.

## Install

```bash
pip install -e "packages/data[dev]"
```

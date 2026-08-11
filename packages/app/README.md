# catalyst-app

FastAPI boundary for the local Catalyst attribution workbench.

It exposes local corpus data, attribution-run lifecycle operations, trace artifacts,
runtime health, and sanitized model configuration. It is not a hosted SaaS service.

---

## Installation

```bash
pip install -e packages/data-core
pip install -e packages/agents
pip install -e packages/app
```

**Requirements:** Python ≥ 3.11

---

## Running

```bash
uvicorn catalyst_app.main:app --reload --port 8000
```

Interactive API docs available at `http://localhost:8000/docs`.

---

## API Endpoints

**Workbench data** (`/api/`):

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/tickers` | Available ticker symbols |
| `GET` | `/api/range-local` | Local OHLCV date range |
| `GET` | `/api/ohlcv/{ticker}` | Candlestick data for a ticker |
| `GET` | `/api/news/{ticker}` | News available for a ticker and date window |
| `GET` | `/api/fundamentals/{ticker}` | Stored fundamental snapshots |
| `GET` | `/api/session/{ticker}` | Combined local workbench context |

**Live runs** (`/api/live-runs/`):

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/live-runs` | Create a new attribution run |
| `GET` | `/api/live-runs/{run_id}` | Run summary and status |
| `GET` | `/api/live-runs/{run_id}/events` | Node-level trace events |
| `GET` | `/api/live-runs/{run_id}/artifacts` | Run artifacts (evidence, scores) |
| `GET` | `/api/live-runs/{run_id}/workspace` | Projected analyst workspace state |
| `POST` | `/api/live-runs/{run_id}/retry` | Retry a failed run |
| `POST` | `/api/live-runs/{run_id}/cancel` | Request cancellation |
| `GET` | `/api/health/runtime` | Runtime health check |
| `GET` | `/api/models/catalog` | Sanitized provider/model catalog |
| `POST` | `/api/models/validate` | Validate a BYOK model configuration |

---

## Package Structure

```
catalyst_app/
├── main.py             # FastAPI app factory
├── dependencies.py     # Dependency injection (service, store)
├── llm_factory.py      # Model routing + provider abstraction
├── schemas.py          # Pydantic request/response models
├── workbench_store.py  # Workbench data access layer
└── routers/
    ├── live_runs.py    # Attribution run lifecycle endpoints
    └── workbench.py    # OHLCV + context data endpoints
```

---

## Configuration

| Variable | Description |
|----------|-------------|
| `CATALYST_DB_PATH` | SQLite database path |
| `CATALYST_LANCEDB_DIR` | Gold LanceDB directory (identity-bound runtime) |
| `CATALYST_INDEX_MANIFEST_PATH` | Explicit clean-import `index_manifest.json` path; authoritative for identity binding. When unset, the loader falls back to `<lancedb_dir>/index_manifest.json`, which is NOT the production clean-import manifest. |
| `CATALYST_CORPUS_MANIFEST_ID` / `CATALYST_INDEX_MANIFEST_ID` / `CATALYST_SOURCE_BUNDLE_ID` / `CATALYST_SNAPSHOT_ID` / `CATALYST_PROBE_REPORT_ID` / `CATALYST_POSTBUILD_READINESS_ID` | Manager-approved retrieval identity bindings required by `require_identity_bound_runtime` |
| Provider API keys | Optional server-environment credentials; values never appear in API responses |

Browser-supplied BYOK credentials are held in process memory for the run and are
not persisted in SQLite. Provider identifiers and model metadata may be persisted.

---

## Running Tests

```bash
.venv/bin/python -m pytest packages/app -q
```

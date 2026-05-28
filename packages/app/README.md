# catalyst-app

FastAPI backend for the Catalyst live attribution workbench.

Serves the runtime API for agent execution, trace streaming, workbench data queries,
and artifact projection. Designed to be run alongside the `apps/workbench` frontend.

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

**Live runs** (`/api/live-runs/`):

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/live-runs` | Create a new attribution run |
| `GET` | `/api/live-runs/{run_id}` | Run summary and status |
| `GET` | `/api/live-runs/{run_id}/events` | Node-level trace events |
| `GET` | `/api/live-runs/{run_id}/artifacts` | Run artifacts (evidence, scores) |
| `POST` | `/api/live-runs/{run_id}/retry` | Retry a failed run |
| `GET` | `/api/health/runtime` | Runtime health check |

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

## Environment Variables

| Variable | Description |
|----------|-------------|
| `CATALYST_DB_PATH` | SQLite database path |
| `AIHUBMIX_API_KEY` | LLM proxy key (or use provider keys directly) |
| `OPENAI_API_KEY` | OpenAI-compatible provider key |
| `ANTHROPIC_API_KEY` | Anthropic Claude key |

---

## Running Tests

```bash
cd packages/app
python -m pytest tests/ -q
```

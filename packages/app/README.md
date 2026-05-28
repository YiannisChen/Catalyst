# catalyst-app

FastAPI runtime application for Catalyst.

Serves the live attribution workbench API, including real-time agent execution,
trace streaming, and artifact projection endpoints. Integrates with the
`catalyst-agents` pipeline and `catalyst-data` storage layer.

## Key Modules

| Module | Purpose |
|--------|---------|
| `routers/live_runs.py` | WebSocket + REST endpoints for live agent execution |
| `llm_factory.py` | Model routing with provider abstraction (aihubmix proxy) |
| `schemas.py` | Pydantic request/response contracts |

# catalyst-app

FastAPI application for local Catalyst data access and attribution runs. The
production runtime accepts `desk_cascade_v1` runs and exposes run lifecycle,
events, artifacts, health, and workbench data through the existing API
contracts.

## Run locally

```bash
pip install -e packages/data -e packages/agents -e packages/api
uvicorn catalyst_app.main:app --reload --port 8000
```

Interactive API documentation is available at `http://localhost:8000/docs`.
Use the generated contract in [`contracts/`](../../contracts/README.md) for
public request, response, and event fields.

## Runtime configuration

Database paths and runtime identity bindings are configured through the
application's `CATALYST_*` environment variables. Provider credentials remain
server-side or request-scoped and are not returned by API endpoints. Never put
secret values in this document or in responses.

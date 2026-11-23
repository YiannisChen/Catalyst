# Contributing

Catalyst is in maintenance status. Issues are welcome; feature work has stopped; bug, security and documentation fixes may be reviewed slowly.

## Reporting Issues

Please open a GitHub issue with:

- A clear description of the problem
- Steps to reproduce
- Expected vs. actual behavior
- Python/Node version and OS

## Development Setup

```bash
git clone https://github.com/YiannisChen/Catalyst.git
cd Catalyst

python -m venv .venv && source .venv/bin/activate
make install
(cd apps/web && npm ci)

cp .env.example .env
# Edit .env with your store paths and model key
```

Keep local credentials only in the repository-root `.env` (ignored by Git).
The API loads it explicitly; data command-line tools read process environment
variables, so export the required values before running them locally. Deployed
services use `/opt/catalyst/.env` through systemd.

## Running Tests

```bash
# All suites (one pytest process per package, as in CI)
make test

# Single package
PYTHONPATH=packages/data:packages/agents:packages/api python -m pytest packages/agents/tests -q
```

The API contract exporter uses the FastAPI and Pydantic versions pinned by the
API package's `dev` extra. After an intentional API schema change, regenerate
the OpenAPI artifact with:

```bash
python scripts/export_api_contract.py
```

## Commit Style

This project uses [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(agents): add magnitude plausibility guardrail
fix(critic): handle None category values
docs(ADR): add retrieval ordering decision record
chore: update dependencies
```

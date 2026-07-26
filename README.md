# Catalyst

Catalyst is a local, evidence-bounded RAG and attribution workbench. It uses a difficult financial-event use case to demonstrate reliable data ingestion, temporal retrieval, reranking, structured agent workflows, runtime assurance, and reproducible engineering evaluation.

Catalyst is not an investment product, trading system, SaaS service, multi-agent framework, or replacement for general web-search assistants. Current capabilities and limitations are described honestly; planned work is tracked in [`docs/plans/2026-07-21-catalyst-roadmap.md`](docs/plans/2026-07-21-catalyst-roadmap.md).

## Architecture

```text
Providers
  → Raw Source Archive
  → Canonical Domain Store
  → Retrieval Corpus
  → FTS5 / Dense / RRF / Reranker
  → Miner → Critic → DecisionRouter → Judge → Validator → Finalizer
  → Trace + Runtime Assurance
  → Local API / Workbench
```

The current graph is a multi-stage LLM workflow with two normal model calls, not a multi-agent system.

## Packages

| Package | Responsibility |
|---|---|
| `packages/data-core` | Provider ingestion, SQLite storage, update planning, provenance, corpus, and retrieval primitives |
| `packages/agents` | Attribution workflow, deterministic routing, runtime service, trace, and assurance |
| `packages/eval` | Agent-agnostic benchmark schemas, metrics, result packs, and replay gates |
| `packages/app` | Local FastAPI contracts, BYOK model wiring, run lifecycle, and workbench queries |

Dependency direction is `data-core → agents → app`. Eval consumes persisted artifacts and plain state through eval-owned adapters; agents never imports eval.

## Current Providers

| Provider | Role |
|---|---|
| Polygon | News and OHLCV |
| Finnhub | Supplemental company news |
| FMP | Fundamentals snapshots |
| SEC EDGAR | Filing metadata and documents |
| FRED | Macro observations |
| yfinance | OHLCV fallback |

## Development

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e "packages/data-core[dev]"
pip install -e packages/agents
pip install -e packages/eval
pip install -e packages/app
```

Run canonical tests from the repository root:

```bash
.venv/bin/python -m pytest packages/data-core -q
.venv/bin/python -m pytest packages/agents -q
.venv/bin/python -m pytest packages/eval -q
.venv/bin/python -m pytest packages/app -q
```

Tests and offline fixtures do not require live provider calls. Never commit provider credentials, raw licensed payloads, databases, embeddings, or local run artifacts.

## Design

- [Final workbench design](docs/plans/2026-07-19-catalyst-open-source-workbench-design.md)
- [Provider provenance and chunking](docs/plans/2026-07-19-catalyst-provider-provenance-and-chunking-design.md)
- [Package architecture](docs/plans/2026-07-19-catalyst-final-package-architecture.md)
- [Evaluation architecture](docs/plans/2026-07-19-catalyst-evaluation-architecture-review.md)
- [B2–B7 technical contracts](docs/plans/2026-07-21-b2-b7-technical-contracts.md)
- [Delivery roadmap](docs/plans/2026-07-21-catalyst-roadmap.md)

## License

MIT — see [LICENSE](LICENSE).

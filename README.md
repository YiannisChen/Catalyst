# Catalyst

> **Why did this stock move?** — AI-powered causal attribution for US equity price movements.

Catalyst retrieves multi-source financial evidence (SEC filings, news, macro indicators, OHLCV prices), filters and grades it through a Miner-Critic-Judge agent pipeline, and produces structured attribution reports with confidence scores and source citations.

**Midterm status note (April 2026):** the current frozen midterm scope is a package-level, script-driven prototype centered on `packages/data-core`, `packages/eval`, and `packages/agents`. This repo does **not** currently claim full API, frontend, or MCP completion for the midterm version.

## Architecture

```
Bronze (raw JSON/HTML) → Silver (cleaned Markdown + dedup) → Gold (LanceDB embeddings)
                                                                      ↓
                                                              Miner → Critic → Judge
                                                                      ↓
                                                            Attribution Report
```

**Four pillars:**

| Package | Purpose |
|---------|---------|
| `catalyst-data` | Multi-source ingestion, medallion pipeline, SQLite + LanceDB storage |
| `catalyst-eval` | Agent-agnostic evaluation framework — 5 metrics, experiment harness |
| `catalyst-mcp` | MCP server exposing attribution as tools for AI assistants |
| `catalyst` | Integrated system — LangGraph agent workflow, CLI, API |

## Key Design Decisions

- **SQLite (WAL mode)** over PostgreSQL — zero-config, portable as single file ([ADR-001](docs/ADR/ADR-001-sqlite-over-postgresql.md))
- **Hybrid RAG** (BM25 + vector + cross-encoder reranking) — neither keyword nor semantic search alone is sufficient for financial text ([ADR-002](docs/ADR/ADR-002-hybrid-rag-retrieval.md))
- **Miner-Critic-Judge** over single-agent or debate — bounded token cost, Critic prevents evidence pollution ([ADR-003](docs/ADR/ADR-003-miner-critic-judge-workflow.md))

## Data Sources

| Provider | Data Type | Auth |
|----------|-----------|------|
| Polygon.io | OHLCV, news | API key |
| FMP | Fundamentals (income, balance, cash flow) | API key |
| FRED | Macro indicators (rates, CPI, unemployment) | API key |
| GDELT | Global news events | Free |
| yfinance | OHLCV fallback | Free |

## Quick Start

```bash
git clone https://github.com/YianniChen/Catalyst.git
cd Catalyst
python -m venv .venv && source .venv/bin/activate
pip install -e packages/data-core

# Configure API keys
cp packages/data-core/.env.template packages/data-core/.env
# Edit .env with your keys

# One-time git hook setup
git config core.hooksPath .githooks
```

## Documentation

- [System Design Spec](docs/superpowers/specs/2026-04-02-catalyst-system-design.md)
- [Midterm Freeze Boundary](docs/superpowers/specs/2026-04-07-catalyst-midterm-freeze-boundary.md)
- [Architecture Decision Records](docs/ADR/)
- [API Provider Documentation](docs/API_Documentation/)
- [Project Whitepaper](docs/catalyst_whitepaper.md)

## Status

Under active development. The April 10, 2026 midterm freeze is limited to the package-level attribution prototype and canonical demo path documented in the freeze addendum, not the full end-state platform.

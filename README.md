# Catalyst

> **From prompt-driven attribution demo to evidence-bounded agent system.**
>
> Catalyst manages uncertainty under evidence constraints, demonstrated on financial event explanation as a high-noise, low-ground-truth testbed.

Catalyst is an agent engineering showcase focused on evidence validity, refusal quality, replayability, and failure governance. It does not optimize for one-shot answer fluency and does not claim to prove true economic causality.

**Current scope (Defense P0):**
- Evidence-bounded outputs with validator-enforced evidence references.
- Explicit output states: `SUFFICIENT / PARTIAL / INSUFFICIENT / SYSTEM_ERROR`.
- Traceable runs with reproducible eval artifacts and baseline comparison.

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
- **P1-facing ADRs (Proposed drafts):** [ADR-004](docs/ADR/ADR-004-adaptive-three-layer-retrieval-ordering.md) retrieval ordering • [ADR-005](docs/ADR/ADR-005-output-status-contract-and-downgrade-policy.md) statuses • [ADR-006](docs/ADR/ADR-006-trace-schema-and-langsmith-alignment.md) trace + LangSmith • [ADR-007](docs/ADR/ADR-007-budget-breaker-and-model-routing-policy.md) budget/routing stub • [ADR-008](docs/ADR/ADR-008-embedding-batch-gpu-strategy.md) embeddings • [ADR-009](docs/ADR/ADR-009-two-level-chunking-and-reranker.md) chunking • [ADR-010](docs/ADR/ADR-010-golden-set-expansion-and-statistical-power.md) golden-set protocol • [ADR-011](docs/ADR/ADR-011-g5-eval-package-extraction-disposition.md) G5 extraction

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

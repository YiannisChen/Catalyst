# Catalyst

> **From prompt-driven attribution demo to evidence-bounded agent system.**
>
> Catalyst manages uncertainty under evidence constraints, demonstrated on financial event explanation as a high-noise, low-ground-truth testbed.

Catalyst is an agent engineering showcase focused on evidence validity, refusal quality, replayability, and failure governance. It does not optimize for one-shot answer fluency and does not claim to prove true economic causality.

## Architecture

```
Bronze (raw JSON/HTML) → Silver (cleaned Markdown + dedup) → Gold (LanceDB embeddings)
                                                                      ↓
                                                              Miner → Critic → Judge
                                                                      ↓
                                                            Attribution Report
```

**Key capabilities:**
- Evidence-bounded outputs with validator-enforced evidence references.
- Explicit output states: `SUFFICIENT / PARTIAL / INSUFFICIENT / SYSTEM_ERROR`.
- Traceable runs with reproducible eval artifacts and baseline comparison.
- Live workbench UI for interactive attribution analysis.

**Package structure:**

| Package | Purpose |
|---------|---------|
| [`packages/data-core`](packages/data-core/) | Multi-source ingestion, medallion pipeline, SQLite + LanceDB storage |
| [`packages/agents`](packages/agents/) | LangGraph agent workflow — Miner, Critic, Judge, DecisionRouter |
| [`packages/eval`](packages/eval/) | Agent-agnostic evaluation framework — 5 metrics, experiment harness |
| [`packages/app`](packages/app/) | FastAPI backend — live runtime API, workbench endpoints |
| [`apps/workbench`](apps/workbench/) | React frontend — interactive attribution workbench |

## Key Design Decisions

All major technical choices are documented as Architecture Decision Records:

- **SQLite over PostgreSQL** — zero-config, portable as single file ([ADR-001](docs/ADR/ADR-001-sqlite-over-postgresql.md))
- **Hybrid RAG** (BM25 + vector + cross-encoder reranking) — neither keyword nor semantic search alone is sufficient for financial text ([ADR-002](docs/ADR/ADR-002-hybrid-rag-retrieval.md))
- **Miner-Critic-Judge** over single-agent or debate — bounded token cost, Critic prevents evidence pollution ([ADR-003](docs/ADR/ADR-003-miner-critic-judge-workflow.md))
- [Full ADR index →](docs/ADR/)

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
pip install -e packages/data-core -e packages/agents -e packages/eval -e packages/app

# Configure API keys
cp packages/data-core/.env.template .env
# Edit .env with your Polygon, FMP, FRED keys
```

## Documentation

- [Architecture Decision Records](docs/ADR/)
- [API Provider Documentation](docs/API_Documentation/)
- [Project Whitepaper](docs/catalyst_whitepaper.md)
- [P1 Architecture Plan](docs/plans/2026-05-04-p1-p2-architecture-plan.md)

## License

MIT — see [LICENSE](LICENSE).

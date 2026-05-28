# Catalyst

**An evidence-bounded financial event attribution system.**

Catalyst answers the question *"why did this stock move?"* by retrieving relevant evidence, scoring it under explicit quality constraints, and refusing to answer when evidence is insufficient — rather than hallucinating a confident-sounding explanation.

Built as a graduation project demonstrating agent engineering principles: evidence validity, refusal quality, replayability, and failure governance over one-shot LLM fluency.

---

## How It Works

```
User query: "Why did AAPL move -3.2% on 2025-11-14?"
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│                    Catalyst Pipeline                     │
│                                                         │
│  Parser → RetrievalPolicy → Miner → Critic              │
│                                    │                    │
│                              DecisionRouter             │
│                              ┌─────┴─────┐             │
│                           Judge       Refuse            │
│                              │                          │
│                          Validator → Finalizer          │
└─────────────────────────────────────────────────────────┘
        │
        ▼
Output status: SUFFICIENT | PARTIAL | INSUFFICIENT | SYSTEM_ERROR
+ Evidence-referenced attribution report
```

Every output carries an explicit status. `INSUFFICIENT` and `SYSTEM_ERROR` are first-class responses — the system refuses rather than fabricates when evidence does not support a conclusion.

---

## Repository Structure

```
Catalyst/
├── packages/
│   ├── data-core/      # Multi-source financial data ingestion + storage
│   ├── agents/         # LangGraph MCJ attribution pipeline
│   ├── eval/           # Evaluation framework (5 metrics + experiment harness)
│   └── app/            # FastAPI runtime backend
├── apps/
│   └── workbench/      # React attribution workbench UI
├── scripts/            # Evaluation, ablation, and diagnostic scripts
├── docs/
│   ├── ADR/            # Architecture Decision Records (12 decisions)
│   ├── API_Documentation/  # Data provider API references
│   └── plans/          # Implementation plans and design docs
└── data/               # Evaluation database (gitignored at runtime)
```

---

## Quick Start

**Prerequisites:** Python ≥ 3.11, Node.js ≥ 18

### 1. Clone and install

```bash
git clone https://github.com/YianniChen/Catalyst.git
cd Catalyst

python -m venv .venv && source .venv/bin/activate

# Install packages in dependency order
pip install -e packages/data-core
pip install -e packages/eval
pip install -e packages/agents
pip install -e packages/app
```

### 2. Configure API keys

```bash
cp packages/data-core/.env.template .env
```

Edit `.env`:

```env
POLYGON_API_KEY=your_polygon_key
FMP_API_KEY=your_fmp_key
FRED_API_KEY=your_fred_key
AIHUBMIX_API_KEY=your_llm_proxy_key   # or set OPENAI_API_KEY / ANTHROPIC_API_KEY directly
```

Data providers: [Polygon.io](https://polygon.io) (free tier available), [FMP](https://financialmodelingprep.com), [FRED](https://fred.stlouisfed.org/docs/api/fred/) (free).

### 3. Run the end-to-end drill

```bash
# Deterministic smoke test — no API keys needed
python scripts/e2e_drill.py

# With real LLM
python scripts/e2e_real.py
```

### 4. Start the workbench

```bash
# Terminal 1 — backend
uvicorn catalyst_app.main:app --reload --port 8000

# Terminal 2 — frontend
cd apps/workbench
npm install && npm run dev
# → http://localhost:5173
```

---

## Evaluation

Catalyst is evaluated against a frozen golden set of 50 financial events with ground-truth attribution labels.

```bash
# Run frozen eval (requires data/)
python packages/eval/scripts/run_frozen_eval.py

# Ablation: critic on vs. off
python scripts/p1_ablation.py

# Statistical report
python scripts/p1_stats.py
```

**Five metrics:** Attribution F1 · Grounding Rate · Temporal Precision · Category Accuracy · Confidence Calibration

See [docs/ADR/ADR-010-golden-set-expansion-and-statistical-power.md](docs/ADR/ADR-010-golden-set-expansion-and-statistical-power.md) for evaluation design.

---

## Key Design Decisions

All major technical choices are documented as Architecture Decision Records:

| ADR | Decision |
|-----|----------|
| [ADR-001](docs/ADR/ADR-001-sqlite-over-postgresql.md) | SQLite (WAL) over PostgreSQL — zero-config, portable |
| [ADR-002](docs/ADR/ADR-002-hybrid-rag-retrieval.md) | Hybrid RAG (BM25 + vector + cross-encoder rerank) |
| [ADR-003](docs/ADR/ADR-003-miner-critic-judge-workflow.md) | Miner-Critic-Judge over single-agent or debate |
| [ADR-005](docs/ADR/ADR-005-output-status-contract-and-downgrade-policy.md) | 4-state output status contract |
| [ADR-009](docs/ADR/ADR-009-two-level-chunking-and-reranker.md) | Two-level chunking with cross-encoder reranking |
| [ADR-010](docs/ADR/ADR-010-critic-k-sufficient-recalibration.md) | Critic sufficiency threshold calibration |

→ [Full ADR index](docs/ADR/)

---

## Data Sources

| Provider | Data | Tier |
|----------|------|------|
| [Polygon.io](https://polygon.io) | OHLCV, news | Free / paid |
| [FMP](https://financialmodelingprep.com) | Fundamentals (income, balance sheet, cash flow) | Free tier |
| [FRED](https://fred.stlouisfed.org) | Macro indicators (rates, CPI, unemployment) | Free |
| [GDELT](https://www.gdeltproject.org) | Global news events | Free |
| [yfinance](https://github.com/ranaroussi/yfinance) | OHLCV fallback | Free |

---

## Documentation

- [Project Whitepaper](docs/catalyst_whitepaper.md)
- [Architecture Decision Records](docs/ADR/)
- [P1 Architecture Plan](docs/plans/2026-05-04-p1-p2-architecture-plan.md)
- [API Provider Documentation](docs/API_Documentation/)

---

## License

MIT — see [LICENSE](LICENSE).

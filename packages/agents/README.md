# catalyst-agents

LangGraph-based Miner-Critic-Judge attribution pipeline for Catalyst.

Implements a policy-driven graph that enforces evidence validity, explicit refusal
states, single-trace replayability, and failure-taxonomy governance — as an
alternative to one-shot LLM attribution.

---

## Installation

```bash
pip install -e packages/data-core
pip install -e packages/eval
pip install -e packages/agents
```

**Requirements:** Python ≥ 3.11

---

## Pipeline

```
AttributionState
      │
      ▼
   Parser          ← extracts ticker, trade_date, price_move_pct
      │
      ▼
RetrievalPolicy    ← selects Layer 1 (BM25+vector) or Layer 2 (macro expansion)
      │
      ▼
   Miner           ← retrieves + reranks evidence chunks
      │             ← applies guardrails: ticker_consistent, market_session_valid,
      │                                   magnitude_plausible
      ▼
   Critic          ← scores evidence chunks (relevance, category, temporal match)
      │             ← decides: proceed / expand_macro / refuse
      ▼
DecisionRouter     ← routes based on Critic decision
   ┌───┴───┐
Judge   Refuse     ← Judge generates attribution; Refuse emits INSUFFICIENT
   │
   ▼
Validator          ← enforces evidence reference contract
   │
   ▼
Finalizer          ← assembles output with status + cost + trace metadata
```

**Output statuses:** `SUFFICIENT` · `PARTIAL` · `INSUFFICIENT` · `SYSTEM_ERROR`

---

## Quick Usage

```python
from catalyst_agents.graph import build_attribution_graph
from catalyst_agents.adapter import CatalystAdapter

# Build graph (requires LLM client via env vars)
graph = build_attribution_graph(use_critic=True)

# Run attribution
adapter = CatalystAdapter()
result = adapter.run(
    ticker="AAPL",
    trade_date="2025-11-14",
    price_move_pct=-3.2,
)

print(result.status)          # "SUFFICIENT"
print(result.attribution)     # evidence-referenced explanation
print(result.trace_id)        # for replay / audit
```

---

## Package Structure

```
catalyst_agents/
├── graph.py            # LangGraph graph assembly
├── state.py            # AttributionState TypedDict + enums
├── adapter.py          # High-level run interface
├── cost_tracker.py     # Per-model token cost accounting
├── nodes/
│   ├── miner.py        # Evidence retrieval + guardrails
│   ├── critic.py       # Evidence scoring + sufficiency decision
│   ├── decision_router.py
│   ├── judge.py        # Attribution generation
│   ├── validator.py    # Output contract enforcement
│   └── finalizer.py
├── retrieval/
│   └── policy.py       # Adaptive 3-layer retrieval ordering
├── runtime/
│   ├── runner.py       # Trace-instrumented graph execution
│   └── service.py      # Live run management for API
├── trace/
│   ├── schema.py       # Trace event schema
│   ├── writer.py       # Trace persistence
│   ├── artifacts.py    # Per-node artifact capture
│   └── projection.py   # Trace summary projection
└── prompts/            # Prompt templates (Critic, Judge)
```

---

## Configuration

| Environment Variable | Description |
|----------------------|-------------|
| `OPENAI_API_KEY` | OpenAI-compatible key (or use `AIHUBMIX_API_KEY` via proxy) |
| `ANTHROPIC_API_KEY` | Anthropic key for Claude models |
| `AIHUBMIX_API_KEY` | Unified proxy key (routes to multiple providers) |
| `CATALYST_DB_PATH` | SQLite path for trace persistence |

---

## Running Tests

```bash
cd packages/agents
python -m pytest tests/ -q --tb=short
```

---

## Key Design Decisions

- [ADR-003](../../docs/ADR/ADR-003-miner-critic-judge-workflow.md) — Why MCJ over single-agent
- [ADR-004](../../docs/ADR/ADR-004-adaptive-three-layer-retrieval-ordering.md) — Retrieval layer ordering
- [ADR-005](../../docs/ADR/ADR-005-output-status-contract-and-downgrade-policy.md) — 4-state output contract
- [ADR-010](../../docs/ADR/ADR-010-critic-k-sufficient-recalibration.md) — Critic threshold calibration

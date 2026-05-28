# catalyst-agents

The agent control plane for Catalyst — an **evidence-bounded** explanation system.

Implements the policy-driven graph (Parser → RetrievalPolicy → Miner →
Critic → DecisionRouter → Validator → Finalizer) that enforces evidence validity,
explicit refusal states (SUFFICIENT / PARTIAL / INSUFFICIENT / SYSTEM_ERROR),
single-trace replayability, and failure-taxonomy governance over one-shot LLM answers.

## Local Dev Setup

This package depends on the sibling monorepo packages (`data-core`, `eval`).

```bash
cd Catalyst
pip install -e packages/data-core -e packages/eval -e packages/agents
```

## Running Tests

```bash
cd packages/agents
python -m pytest tests/ -q --tb=short
```

## Key Modules

| Module | Purpose |
|--------|---------|
| `nodes/miner.py` | Evidence mining with guardrails (ticker consistency, market session) |
| `nodes/critic.py` | Evidence quality scoring with calibrated sufficiency thresholds |
| `nodes/validator.py` | 4-state output validation with evidence reference enforcement |
| `retrieval/policy.py` | Adaptive 3-layer retrieval ordering (BM25 → vector → rerank) |
| `runtime/runner.py` | Trace-instrumented graph execution with checkpoint support |
| `trace/schema.py` | Trace event schema aligned with LangSmith conventions |

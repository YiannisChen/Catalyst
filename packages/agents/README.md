# catalyst-agents

The typed attribution workflow and runtime for Catalyst.

## Workflow

[`run_v1_graph`](catalyst_agents/graphs/graph.py) builds the production graph.
The attribution package assembles the evidence context, checks candidate claims
and quotes, applies publication gates, and records the answer and decision trace.
The production runtime resolves runs to `desk_cascade_v1`; the API supplies
model clients through the runtime boundary.

`catalyst_agents/attribution/` owns typed evidence and assessment contracts,
their builders, publication policy, and final answer projections. The graph
in `catalyst_agents/graphs/` coordinates those pieces; `runtime/` connects
storage and model clients. Contract tests live in `tests/attribution/`, and
retrieval contract tests in `tests/retrieval/`.

## Dependency rule

`catalyst-agents` depends on `catalyst-data` only. Evaluation adapters belong to `catalyst-eval`; this package must import no eval module.

## Install and test

```bash
pip install -e packages/data
pip install -e packages/agents
.venv/bin/python -m pytest packages/agents -q
```

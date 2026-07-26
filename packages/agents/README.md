# catalyst-agents

The typed attribution workflow and runtime for Catalyst.

## Workflow

```text
Miner → Critic → DecisionRouter → Judge → Validator → Finalizer
```

- Miner retrieves candidate evidence.
- Critic grades relevance and sufficiency.
- DecisionRouter proceeds, expands retrieval, or refuses.
- Judge produces structured competing hypotheses.
- Validator enforces evidence and output contracts.
- Finalizer emits a terminal result.

The workflow is deliberately not described as multi-agent. Runtime orchestration, trace persistence, failure classification, and future per-run assurance live in this package. Concrete model-provider clients and BYOK credentials live in `catalyst-app` and are injected through runtime boundaries.

## Dependency rule

`catalyst-agents` depends on `catalyst-data` only. Evaluation adapters belong to `catalyst-eval`; this package must import no eval module.

## Install and test

```bash
pip install -e packages/data-core
pip install -e packages/agents
.venv/bin/python -m pytest packages/agents -q
```

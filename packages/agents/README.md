# catalyst-agents

Miner-Critic-Judge attribution workflow for Catalyst.
# catalyst-agents

The agent control plane for Catalyst — an **evidence-bounded** explanation system.
This package implements the policy-driven graph (Parser -> RetrievalPolicy -> Miner ->
Critic -> DecisionRouter -> Validator -> Finalizer) that enforces evidence validity,
explicit refusal states (SUFFICIENT / PARTIAL / INSUFFICIENT / SYSTEM_ERROR),
single-trace replayability, and failure-taxonomy governance over one-shot LLM answers.

## Local Dev Setup

This package depends on the sibling monorepo packages:

- `../data-core`
- `../eval`

Use the shared project virtualenv, then install the local packages in editable mode:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/agents
/Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pip install -e ../data-core -e ../eval -e '.[dev]'
```

## No-PYTHONPATH Verification

For a normal monorepo checkout, the test suite bootstraps sibling package paths via `tests/conftest.py`, so you can verify from `packages/agents` without setting `PYTHONPATH`.

After the editable installs above, or directly inside the repo checkout, run:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/agents
./scripts/verify_local_pytest.sh
```

Equivalent direct command:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/agents
/Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/ -q --tb=short
```

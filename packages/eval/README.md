# catalyst-eval

Agent-agnostic engineering evaluation for Catalyst artifacts.

The package separates:

- per-run deterministic assurance produced by the runtime;
- compact offline benchmark metrics for component decisions;
- reproducible ResultPack and Markdown scorecard generation.

The current S1 schemas and tests remain as compatibility foundations while B7 introduces the compact 12-case `BenchmarkCase` dataset. Benchmark aggregates are developer evidence, not user-facing accuracy claims.

Evaluation adapters live under `catalyst_eval.adapters` and convert plain runtime state into eval schemas. Agents never depends on this package.

## Install and test

```bash
pip install -e packages/eval
.venv/bin/python -m pytest packages/eval -q
```

Canonical tests are offline and must not call model or provider APIs.

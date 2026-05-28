# catalyst-eval

Agent-agnostic evaluation framework for financial attribution quality.

Provides domain-specific metrics (F1, grounding rate, refusal precision, temporal
accuracy, cost efficiency), frozen golden-set management, experiment harness with
ablation support, and baseline comparison tooling (Catalyst vs. direct-LLM).

## Key Modules

| Module | Purpose |
|--------|---------|
| `metrics/` | Attribution quality metrics with statistical confidence intervals |
| `golden_set/` | Frozen evaluation cases with SHA-verified manifests |
| `experiments/` | Experiment runner with ablation matrix support |
| `scripts/run_frozen_eval.py` | Entry point for reproducible evaluation runs |

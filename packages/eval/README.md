# catalyst-eval

Agent-agnostic evaluation framework for financial attribution quality.

Provides domain-specific metrics, a frozen golden-set protocol, an experiment harness
with ablation support, and baseline comparison tooling (Catalyst vs. direct-LLM).

---

## Installation

```bash
pip install -e packages/eval
```

**Requirements:** Python ≥ 3.11

---

## Metrics

| Metric | Description |
|--------|-------------|
| `AttributionF1` | Precision/recall over attributed event categories |
| `GroundingRate` | Fraction of claims with valid evidence references |
| `TemporalPrecision` | Accuracy of date/period alignment in attributions |
| `CategoryAccuracy` | Correctness of event category classification |
| `ConfidenceCalibration` | Calibration of model confidence scores |

All metrics produce point estimates with 95% confidence intervals via bootstrap resampling.

---

## Golden Set

The frozen evaluation set contains **50 financial events** with ground-truth attribution labels, SHA-verified for reproducibility.

```bash
# Run full frozen eval
python packages/eval/scripts/run_frozen_eval.py

# Eval with critic disabled (ablation baseline)
python packages/eval/scripts/run_frozen_eval.py --no-critic
```

See [ADR-010: Golden Set Expansion and Statistical Power](../../docs/ADR/ADR-010-golden-set-expansion-and-statistical-power.md) for the evaluation design and sample-size rationale.

---

## Ablation Experiments

```bash
# Critic on vs. off across routing profiles
python scripts/p1_ablation.py

# Statistical significance and effect size report
python scripts/p1_stats.py

# Trace-level visualization
python scripts/p1_trace_viz.py
```

---

## Baselines

`catalyst_eval/harness/baselines/` contains:

- **`direct_llm.py`** — single-shot LLM baseline (no retrieval, no critic)

Used to quantify the value added by the Catalyst pipeline vs. a naive LLM call.

---

## Package Structure

```
catalyst_eval/
├── metrics/
│   ├── attribution_f1.py
│   ├── grounding_rate.py
│   ├── temporal_precision.py
│   ├── category_accuracy.py
│   └── confidence_calibration.py
├── harness/
│   ├── frozen_eval.py      # Golden-set evaluation runner
│   ├── experiment.py       # Ablation experiment harness
│   ├── runner.py           # Per-case trace runner
│   └── baselines/
│       └── direct_llm.py   # Direct LLM comparison baseline
├── schema/                 # Eval result schemas
├── reports/                # Report generation utilities
└── scripts/
    └── run_frozen_eval.py  # CLI entry point
```

---

## Running Tests

```bash
cd packages/eval
python -m pytest tests/ -q
```

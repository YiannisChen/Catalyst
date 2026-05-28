# 2026-05-15 R1 External Baseline Results

## Status

Pending cloud pullback artifacts. This placeholder exists so local plan tasks can complete without running any local experiments or model calls.

## Inputs

- Catalyst input: `/Users/yiannischen/Desktop/Catalyst/data/eval_reports/r1_external/r1_input_chunks.jsonl`
- External input: `/Users/yiannischen/Desktop/Catalyst/data/eval_reports/r1_external/r1_sonnet_grades.jsonl` (expected from cloud run)

## Metrics (to fill after cloud pullback)

- Pearson: `TBD`
- Spearman: `TBD`
- Bucket agreement: `TBD`
- Aligned pairs: `TBD`

## Notes

- Do NOT run experiments on local machine.
- Generate final metrics by running `scripts/reports/compute_r1_correlation.py` after cloud external grades are available.

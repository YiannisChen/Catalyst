# 2026-05-17 Regression Check

- h_refusal RUN_TAG: `r1_h_refusal_guardrail_20260517_080355`
- G1 (SYSTEM_ERROR == 0 and INSUFFICIENT >= 6): `PASS`
- G2 (200-case full status_accuracy >= 0.84, grounding_rate >= 0.90, new SYSTEM_ERROR = 0): `PENDING (not rerun in this batch)`
- G3 (external baseline bucket >= 0.70, pearson >= 0.55): `PENDING (depends on Exp1 v2 rerun)`

## h_refusal Distribution
- INSUFFICIENT: 7
- SUFFICIENT: 1


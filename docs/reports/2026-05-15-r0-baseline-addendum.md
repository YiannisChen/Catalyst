# 2026-05-15 R0 Baseline Addendum

## Trivial Baseline

- Trivial policy: always predict majority expected status `SUFFICIENT`.
- Trivial baseline accuracy: `0.9600`.

## Dual-Track Metrics

This addendum reports both raw performance and coverage-adjusted performance to separate model behavior from known data coverage limits.

## Raw vs Coverage-Adjusted

- Raw status accuracy (all 200 rows): `0.7700`.
- Coverage-adjusted status accuracy (exclude `data_coverage_gap=true` rows, n=188): `0.8191`.

## Refusal and Grounding Framing

- Output refusal proxy (`output_status=INSUFFICIENT`) rate: `0.0650`.
- Mean grounding_rate_field (where available): `0.9802`.
- Interpretation: raw metrics capture end-to-end behavior; coverage-adjusted metrics better isolate model decision quality when known data gaps exist.

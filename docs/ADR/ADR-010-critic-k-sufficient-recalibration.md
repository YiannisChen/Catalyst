# ADR-00X: Critic K-Sufficient Recalibration

- Date: 2026-05-14
- Status: Accepted

## Context

The P1 evaluation run showed Critic parsing and gating behavior produced too many non-actionable outcomes before Judge synthesis. In particular, requiring 4 high-relevance evidence chunks for `sufficient` caused borderline high-quality cases to be labeled `partial`, reducing status accuracy and amplifying downstream refusals when evidence quality was otherwise strong.

## Decision

Recalibrate Critic sufficiency threshold:

- `K_SUFFICIENT`: `4 -> 3`

Retain current magnitude gate and partial semantics:

- `M_THRESHOLD` remains `0.6`
- `evidence_count == 0` -> `insufficient`
- all other non-sufficient cases -> `partial`

`K_PARTIAL` is retained only for magnitude coverage scaling and no longer drives branch eligibility.

## Rationale

- P1 traces indicate many valid cases had 3 grounded, high-relevance chunks but missed the previous 4-chunk gate.
- Lowering to 3 keeps a multi-evidence requirement while improving recall for legitimate sufficient cases.
- Keeping `M_THRESHOLD` unchanged preserves quality control and avoids turning weak 3-chunk sets into `sufficient`.

## Expected Impact

- Increase `full.status_accuracy` in P1 reruns.
- Reduce false `partial` assignments for well-supported cases.
- Improve refusal calibration by reducing unnecessary upstream conservatism.

## Rollback Strategy

If rerun KPI gates regress or refusal precision degrades:

1. Revert `K_SUFFICIENT` from `3` back to `4`.
2. Re-run targeted and full P1 ablation.
3. Compare `status_accuracy`, full-vs-degraded gap, and SYSTEM_ERROR/label quality diffs before re-adopting.

## Diagnostic v2 Follow-up (2026-05-14)

Diagnostic run summary (20 cases, full profile only):

- files: `20`
- scores: `159`
- below-threshold total: `103`
- relevance quantiles: `p25=0.0`, `p50=0.2`, `p75=0.7`
- `0.4 <= score < 0.5` band: `6` (`ratio=0.0377`)

Decision update:

- Keep `RELEVANCE_THRESHOLD = 0.5`
- Lower `K_SUFFICIENT: 3 -> 2`

Why:

- The 0.4–0.5 band ratio is very low (`0.0377 < 0.20`), so lowering threshold is not supported by observed score distribution.
- Lowering `K_SUFFICIENT` is the targeted calibration expected to improve sufficiency recall without broadening low-confidence evidence acceptance.

Rollback condition:

- If full rerun fails KPI gate or causes refusal-quality regression, revert `K_SUFFICIENT` to `3` and rerun.

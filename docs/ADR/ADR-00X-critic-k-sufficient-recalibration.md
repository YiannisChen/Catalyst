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


## Scheme C Verification Snapshot (2026-05-18)

- Scope: local implementation + contract verification + cloud runbook dry-run.
- Tier0 fairness guard: enforced (`--model == --catalyst-model` in `closed_book`).
- Tier1 prerequisite: deterministic corpus builder added (`build_tier1_search_corpus.py`).
- Tier2 accounting fields surfaced: `tier2_eligible_n`, `tier2_excluded_n`, `tier2_exclusion_reason_counts`.
- Magnitude threshold default frozen to `0.45`; env override via `CATALYST_M_THRESHOLD`.

### Local test status
- New Scheme C tests: PASS
- Pre-cloud non-regression contracts: PASS

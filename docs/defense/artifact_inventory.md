# P0 defense artifact inventory

Generated from plan §I.6 checklist paths. JSON rows validate parse. Vector index row: directory presence; empty table is expected (W-15).

| Artifact | Expected path | OK | Detail | Notes |
|---|---|---|---|---|
| Frozen DB | `data/catalyst_eval_frozen.db` | ✅ | 90193920 B |  |
| Frozen vector index (W-15) | `data/lancedb_gold/eval_frozen/` | ✅ | exists, empty (W-15) | W-15: Lance deferred; dir may be empty |
| direct_llm report (JSON) | `data/eval_reports/20260503_154053_direct_llm.json` | ✅ | 6986 B |  |
| direct_llm report (MD) | `data/eval_reports/20260503_154053_direct_llm.md` | ✅ | 1131 B |  |
| mcj_full report (JSON) | `data/eval_reports/20260503_154053_mcj_full.json` | ✅ | 9656 B |  |
| mcj_full report (MD) | `data/eval_reports/20260503_154053_mcj_full.md` | ✅ | 1129 B |  |
| Comparison (JSON) | `data/eval_reports/20260503_154053_comparison.json` | ✅ | 19135 B |  |
| Comparison (MD) | `data/eval_reports/20260503_154053_comparison.md` | ✅ | 1386 B |  |
| Trace files (original, ≥20) | `data/traces/*.json` (excl. rerun) | ✅ | 20 files |  |
| Tier-A rerun (JSON) | `data/eval_reports/rerun_20260504_072205_mcj_full.json` | ✅ | 10228 B |  |
| Tier-A rerun (MD) | `data/eval_reports/rerun_20260504_072205_mcj_full.md` | ✅ | 1144 B |  |
| Rerun traces (≥10) | `data/traces/rerun_*.json` | ✅ | 10 files |  |
| Audit log | `docs/testing/audit_2026-05-04.md` | ✅ | 7512 B |  |
| Demo notebook | `notebooks/demo.ipynb` | ✅ | 4092 B |  |
| Demo executed | `notebooks/demo_executed.ipynb` | ✅ | 7190 B |  |
| Defense deck | `docs/defense/deck.md` | ✅ | 4676 B |  |
| Preflight | `data/eval_reports/preflight_20260503_154053.json` | ✅ | 932 B |  |
| Freeze header | `data/eval_reports/freeze_header_20260503_154053.json` | ✅ | 1128 B |  |
| Golden set | `packages/eval/golden_set/v1_2_p0_set.jsonl` | ✅ | 5033 B |  |
| Threshold calibration | `data/eval_reports/20260503_154053_threshold_calibration.json` | ✅ | 438 B |  |
| Failure taxonomy | `docs/testing/failure-taxonomy.md` | ✅ | 2575 B |  |

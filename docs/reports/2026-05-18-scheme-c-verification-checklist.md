# Scheme C Verification Checklist (Blockers Hotfix Local)

## Step 1: Blocker-targeted tests
Command:
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest packages/eval/tests/test_build_same_evidence_inputs.py packages/eval/tests/test_compare_catalyst_vs_direct_llm_v3_tiers.py packages/eval/tests/test_compute_attribution_metrics_v2.py packages/eval/tests/test_compute_attribution_metrics_v2_contract_io.py packages/eval/tests/test_build_tier1_search_corpus.py packages/eval/tests/test_run_scheme_c_batch_contract.py -v`

Result: PASS (`14 passed`)

## Step 2: Non-regression pack
Commands:
- `cd /Users/yiannischen/Desktop/Catalyst-scheme-c-hotfix/packages/eval && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_run_direct_llm_frozen_baseline_v2_contract.py tests/test_run_direct_llm_frozen_baseline_v3_modes.py tests/test_run_direct_llm_stability_replay.py tests/test_build_industrial_audit_bundle.py -v`
- `cd /Users/yiannischen/Desktop/Catalyst-scheme-c-hotfix/packages/agents && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_validator_magnitude_threshold_config.py -v`

Result: PASS (`9 passed` + `2 passed`)

## Step 3: Batch dry-run
Command:
`bash scripts/reports/run_scheme_c_batch.sh --dry-run`

Result: PASS
- No placeholder sweep commands remain.
- Includes tier0/tier1/tier2 compare outputs, stability replay, and bundle generation commands.

## Step 4: Real-input smoke check (attribution-v2)
Commands:
1. Convert real Catalyst ablation `per_case` to JSONL:
   - `.../final_rerun_20260517_131820_g2_risky_rerun_p1_ablation.json -> .../catalyst_mt04_per_unit.jsonl`
2. Run:
   - `python scripts/reports/compute_attribution_metrics_v2.py --catalyst-json .../catalyst_mt04_per_unit.jsonl --direct-json .../direct_llm_dsv4_frozen_per_unit.run1.jsonl --output-json .../attribution_metrics_v2_smoke.json`

Observed output:
- `[warn] attribution-v2 fallback mode activated: missing required attribution fields`
- `[ok] compute_mode=legacy_fallback`

Hotfix applied:
- `compute_attribution_metrics_v2.py` now emits explicit `compute_mode` and warning when fallback is used, so fallback is no longer silent.

## Residual Risks
- Real artifacts still miss attribution gold fields for full `real_schema` mode; current smoke shows `legacy_fallback` (explicitly surfaced).
- Cloud run should supply/derive gold attribution fields or pass curated attribution-ready rows to avoid fallback metrics inflation.

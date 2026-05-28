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

## Step 4: Real-input smoke check (attribution-v2 strict)
Commands:
1. Build strict smoke inputs from real artifacts:
   - `catalyst_attribution_smoke.jsonl` (row points to real `summary_json`)
   - `direct_attribution_smoke.jsonl` (row copied from real direct run JSONL)
2. Run strict mode (no fallback flag):
   - `python scripts/reports/compute_attribution_metrics_v2.py --catalyst-json .../catalyst_attribution_smoke.jsonl --direct-json .../direct_attribution_smoke.jsonl --output-json .../attribution_metrics_v2_smoke_strict.json`

Observed output:
- `[ok] compute_mode=real_schema`

Hotfix applied:
- `compute_attribution_metrics_v2.py` defaults to strict real-schema mode.
- Legacy fallback now requires explicit `--allow-legacy-fallback`; otherwise command fails non-zero.

## Residual Risks
- Full-cohort real-schema attribution requires all rows to carry parseable attribution spans/fields.
- Batch is intentionally strict and will fail fast when those fields are absent.

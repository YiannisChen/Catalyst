# Scheme C Verification Checklist

## Commands Run
1. `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ... pytest tests/test_run_direct_llm_frozen_baseline_v3_modes.py ... tests/test_scheme_c_pipeline_contract.py -v`
- Result: PASS (13 passed)

2. `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ... pytest packages/eval/tests/test_thesis_freeze_manifest_v2_contract.py ... packages/eval/tests/test_build_industrial_audit_bundle.py -q`
- Result: PASS (11 passed)

3. `bash scripts/reports/run_scheme_c_batch.sh --dry-run`
- Result: PASS (resolved command chain printed)

4. `python scripts/reports/build_industrial_audit_bundle.py ... --out-json .../industrial_audit_bundle_v3.json`
- Result: PASS (`[ok] wrote audit bundle`)

## Artifact Paths
- `/Users/yiannischen/Desktop/Catalyst-scheme-c/scripts/reports/run_scheme_c_batch.sh`
- `/Users/yiannischen/Desktop/Catalyst-scheme-c/docs/plans/2026-05-18-scheme-c-cloud-runbook.md`
- `/Users/yiannischen/Desktop/Catalyst-scheme-c/data/eval_reports/r1_runs/final_rerun_20260517_131820/core/industrial_audit_bundle_v3.json`

## Threshold Calibration Decision
- `m_threshold_default`: `0.45`
- `m_threshold_sweep_results`: command pack prepared for `0.40 / 0.45 / 0.60`.
- `selected_threshold_reason`: balanced conservatism per scheme-c plan default freeze.

## Residual Risks
- Full cloud batch execution not run locally (by design); runbook generated for cloud execution.

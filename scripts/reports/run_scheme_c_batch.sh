#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

PY=${PY:-/usr/local/miniconda3/envs/py312/bin/python}
RUN_ROOT=${RUN_ROOT:-data/eval_reports/r1_runs/final_rerun_20260517_131820}
CORE_DIR="${RUN_ROOT}/core"
FREEZE_MANIFEST="${CORE_DIR}/thesis_freeze_manifest.json"
TIER1_CORPUS="${CORE_DIR}/tier1_search_corpus.jsonl"
TIER2_INPUTS="${CORE_DIR}/same_evidence_inputs.jsonl"
TIER2_META="${CORE_DIR}/same_evidence_meta.json"
ATTR_V2="${CORE_DIR}/attribution_metrics_v2.json"
RUNTIME_JSON="${CORE_DIR}/catalyst_runtime_stats.json"

run_cmd() {
  echo "$*"
  if [[ "$DRY_RUN" == "false" ]]; then
    eval "$*"
  fi
}

run_cmd "$PY scripts/reports/build_thesis_freeze_manifest.py --main-ablation-json data/eval_reports/final_rerun_20260517_131820_g2_risky_rerun_p1_ablation.json --golden-set packages/eval/golden_set/v1_2_p1_set.jsonl --db-path data/catalyst_eval_frozen_v2.db --lancedb-dir data/lancedb_gold/eval_frozen --model-lock-path configs/model_profiles.lock.json --output ${FREEZE_MANIFEST}"
run_cmd "$PY scripts/reports/run_direct_llm_frozen_baseline.py --freeze-manifest ${FREEZE_MANIFEST} --model gemini-2.5-flash --catalyst-model gemini-2.5-flash --baseline-mode closed_book --provider aihubmix --base-url https://aihubmix.com/v1 --pricing-lock configs/eval_pricing.lock.json --out-dir ${CORE_DIR}"
run_cmd "mv ${CORE_DIR}/direct_llm_dsv4_frozen_per_unit.jsonl ${CORE_DIR}/direct_closed_book_gemini_per_unit.jsonl"
run_cmd "$PY scripts/reports/build_tier1_search_corpus.py --freeze-manifest ${FREEZE_MANIFEST} --output ${TIER1_CORPUS}"
run_cmd "$PY scripts/reports/run_direct_llm_frozen_baseline.py --freeze-manifest ${FREEZE_MANIFEST} --model gemini-2.5-flash --catalyst-model gemini-2.5-flash --baseline-mode search_augmented --search-corpus-jsonl ${TIER1_CORPUS} --provider aihubmix --base-url https://aihubmix.com/v1 --pricing-lock configs/eval_pricing.lock.json --out-dir ${CORE_DIR}"
run_cmd "mv ${CORE_DIR}/direct_llm_dsv4_frozen_per_unit.jsonl ${CORE_DIR}/direct_search_augmented_gemini_per_unit.jsonl"
run_cmd "$PY scripts/reports/build_same_evidence_inputs.py --freeze-manifest ${FREEZE_MANIFEST} --output ${TIER2_INPUTS} --meta-output ${TIER2_META}"
run_cmd "$PY scripts/reports/run_direct_llm_frozen_baseline.py --freeze-manifest ${FREEZE_MANIFEST} --model gemini-2.5-flash --catalyst-model gemini-2.5-flash --baseline-mode same_evidence --same-evidence-jsonl ${TIER2_INPUTS} --provider aihubmix --base-url https://aihubmix.com/v1 --pricing-lock configs/eval_pricing.lock.json --out-dir ${CORE_DIR}"
run_cmd "mv ${CORE_DIR}/direct_llm_dsv4_frozen_per_unit.jsonl ${CORE_DIR}/direct_same_evidence_gemini_per_unit.jsonl"
run_cmd "CATALYST_M_THRESHOLD=0.40 echo 'run catalyst sweep 0.40 command here'"
run_cmd "CATALYST_M_THRESHOLD=0.45 echo 'run catalyst sweep 0.45 command here'"
run_cmd "CATALYST_M_THRESHOLD=0.60 echo 'run catalyst sweep 0.60 command here'"
run_cmd "$PY scripts/reports/compute_attribution_metrics_v2.py --catalyst-json ${CORE_DIR}/catalyst_mt04_per_unit.jsonl --direct-json ${CORE_DIR}/direct_same_evidence_gemini_per_unit.jsonl --output-json ${ATTR_V2}"
run_cmd "$PY scripts/reports/aggregate_catalyst_runtime_stats.py --summaries-glob 'data/eval_reports/*_p1_trace.summary.json' --output-json ${RUNTIME_JSON}"
run_cmd "$PY scripts/reports/compare_catalyst_vs_direct_llm.py --freeze-manifest ${FREEZE_MANIFEST} --catalyst-ablation-json data/eval_reports/final_rerun_20260517_131820_g2_risky_rerun_p1_ablation.json --direct-per-unit-jsonl ${CORE_DIR}/direct_closed_book_gemini_per_unit.jsonl --baseline-tier tier0_closed_book --attribution-v2-json ${ATTR_V2} --tier2-meta-json ${TIER2_META} --catalyst-runtime-json ${RUNTIME_JSON} --output-json ${CORE_DIR}/catalyst_vs_direct_tier0_metrics.json --output-csv ${CORE_DIR}/catalyst_vs_direct_tier0_main_table.csv --output-md docs/reports/2026-05-18-catalyst-vs-direct-llm-main-comparison.md"
run_cmd "$PY scripts/reports/build_industrial_audit_bundle.py --freeze-id thesis-freeze-scheme-c --metrics-json ${CORE_DIR}/catalyst_vs_direct_tier0_metrics.json --per-case-jsonl ${CORE_DIR}/direct_closed_book_gemini_per_unit.jsonl --stability-json ${CORE_DIR}/direct_llm_dsv4_stability_k3.json --adjudication-md docs/reports/2026-05-18-catalyst-vs-direct-llm-main-comparison.md --out-json ${CORE_DIR}/industrial_audit_bundle_v3.json"

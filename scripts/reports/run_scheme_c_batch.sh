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
DIRECT_RUN1="${CORE_DIR}/direct_closed_book_gemini_per_unit.jsonl"
DIRECT_RUN2="${CORE_DIR}/direct_search_augmented_gemini_per_unit.jsonl"
DIRECT_RUN3="${CORE_DIR}/direct_same_evidence_gemini_per_unit.jsonl"
CATALYST_TAG_040="catalyst_mt040"
CATALYST_TAG_045="catalyst_mt045"
CATALYST_TAG_060="catalyst_mt060"

run_cmd() {
  echo "$*"
  if [[ "$DRY_RUN" == "false" ]]; then
    eval "$*"
  fi
}

run_cmd "$PY scripts/reports/build_thesis_freeze_manifest.py --main-ablation-json data/eval_reports/final_rerun_20260517_131820_g2_risky_rerun_p1_ablation.json --golden-set packages/eval/golden_set/v1_2_p1_set.jsonl --db-path data/catalyst_eval_frozen_v2.db --lancedb-dir data/lancedb_gold/eval_frozen --model-lock-path configs/model_profiles.lock.json --output ${FREEZE_MANIFEST}"
run_cmd "$PY scripts/reports/run_direct_llm_frozen_baseline.py --freeze-manifest ${FREEZE_MANIFEST} --model gemini-2.5-flash --catalyst-model gemini-2.5-flash --baseline-mode closed_book --provider aihubmix --base-url https://aihubmix.com/v1 --pricing-lock configs/eval_pricing.lock.json --out-dir ${CORE_DIR}"
run_cmd "mv ${CORE_DIR}/direct_llm_dsv4_frozen_per_unit.jsonl ${DIRECT_RUN1}"
run_cmd "$PY scripts/reports/build_tier1_search_corpus.py --freeze-manifest ${FREEZE_MANIFEST} --output ${TIER1_CORPUS}"
run_cmd "$PY scripts/reports/run_direct_llm_frozen_baseline.py --freeze-manifest ${FREEZE_MANIFEST} --model gemini-2.5-flash --catalyst-model gemini-2.5-flash --baseline-mode search_augmented --search-corpus-jsonl ${TIER1_CORPUS} --provider aihubmix --base-url https://aihubmix.com/v1 --pricing-lock configs/eval_pricing.lock.json --out-dir ${CORE_DIR}"
run_cmd "mv ${CORE_DIR}/direct_llm_dsv4_frozen_per_unit.jsonl ${DIRECT_RUN2}"
run_cmd "$PY scripts/reports/build_same_evidence_inputs.py --freeze-manifest ${FREEZE_MANIFEST} --output ${TIER2_INPUTS} --meta-output ${TIER2_META}"
run_cmd "$PY scripts/reports/run_direct_llm_frozen_baseline.py --freeze-manifest ${FREEZE_MANIFEST} --model gemini-2.5-flash --catalyst-model gemini-2.5-flash --baseline-mode same_evidence --same-evidence-jsonl ${TIER2_INPUTS} --provider aihubmix --base-url https://aihubmix.com/v1 --pricing-lock configs/eval_pricing.lock.json --out-dir ${CORE_DIR}"
run_cmd "mv ${CORE_DIR}/direct_llm_dsv4_frozen_per_unit.jsonl ${DIRECT_RUN3}"

# Real sweep runs (0.40 / 0.45 / 0.60)
run_cmd "CATALYST_M_THRESHOLD=0.40 ${PY} scripts/p1_ablation.py --golden-set packages/eval/golden_set/v1_2_p1_set.jsonl --profiles full,no_vector --db data/catalyst_eval_frozen_v2.db --lancedb-dir data/lancedb_gold/eval_frozen --provider aihubmix --base-url https://aihubmix.com/v1 --continue-on-error --tag ${CATALYST_TAG_040} --out-dir ${CORE_DIR}"
run_cmd "CATALYST_M_THRESHOLD=0.45 ${PY} scripts/p1_ablation.py --golden-set packages/eval/golden_set/v1_2_p1_set.jsonl --profiles full,no_vector --db data/catalyst_eval_frozen_v2.db --lancedb-dir data/lancedb_gold/eval_frozen --provider aihubmix --base-url https://aihubmix.com/v1 --continue-on-error --tag ${CATALYST_TAG_045} --out-dir ${CORE_DIR}"
run_cmd "CATALYST_M_THRESHOLD=0.60 ${PY} scripts/p1_ablation.py --golden-set packages/eval/golden_set/v1_2_p1_set.jsonl --profiles full,no_vector --db data/catalyst_eval_frozen_v2.db --lancedb-dir data/lancedb_gold/eval_frozen --provider aihubmix --base-url https://aihubmix.com/v1 --continue-on-error --tag ${CATALYST_TAG_060} --out-dir ${CORE_DIR}"

# Select calibrated 0.45 as comparison default and derive per-case jsonl for attribution script.
run_cmd "$PY -c \"import json,pathlib; abl=json.loads(pathlib.Path('${CORE_DIR}/${CATALYST_TAG_045}_p1_ablation.json').read_text(encoding='utf-8')); out=pathlib.Path('${CORE_DIR}/catalyst_mt04_per_unit.jsonl'); out.parent.mkdir(parents=True, exist_ok=True); f=out.open('w', encoding='utf-8'); [f.write(json.dumps(r, ensure_ascii=False)+'\\\\n') for r in abl.get('per_case', [])]; f.close(); print(out)\""

run_cmd "$PY scripts/reports/compute_attribution_metrics_v2.py --catalyst-json ${CORE_DIR}/catalyst_mt04_per_unit.jsonl --direct-json ${DIRECT_RUN3} --output-json ${ATTR_V2}"
run_cmd "$PY scripts/reports/aggregate_catalyst_runtime_stats.py --summaries-glob 'data/eval_reports/*_p1_trace.summary.json' --output-json ${RUNTIME_JSON}"
run_cmd "$PY scripts/reports/compare_catalyst_vs_direct_llm.py --freeze-manifest ${FREEZE_MANIFEST} --catalyst-ablation-json ${CORE_DIR}/${CATALYST_TAG_045}_p1_ablation.json --direct-per-unit-jsonl ${DIRECT_RUN1} --baseline-tier tier0_closed_book --attribution-v2-json ${ATTR_V2} --tier2-meta-json ${TIER2_META} --catalyst-runtime-json ${RUNTIME_JSON} --output-json ${CORE_DIR}/catalyst_vs_direct_tier0_metrics.json --output-csv ${CORE_DIR}/catalyst_vs_direct_tier0_main_table.csv --output-md docs/reports/2026-05-18-catalyst-vs-direct-llm-main-comparison.md"
run_cmd "$PY scripts/reports/compare_catalyst_vs_direct_llm.py --freeze-manifest ${FREEZE_MANIFEST} --catalyst-ablation-json ${CORE_DIR}/${CATALYST_TAG_045}_p1_ablation.json --direct-per-unit-jsonl ${DIRECT_RUN2} --baseline-tier tier1_search_augmented --attribution-v2-json ${ATTR_V2} --tier2-meta-json ${TIER2_META} --catalyst-runtime-json ${RUNTIME_JSON} --output-json ${CORE_DIR}/catalyst_vs_direct_tier1_metrics.json --output-csv ${CORE_DIR}/catalyst_vs_direct_tier1_main_table.csv --output-md docs/reports/2026-05-18-catalyst-vs-direct-llm-main-comparison.md"
run_cmd "$PY scripts/reports/compare_catalyst_vs_direct_llm.py --freeze-manifest ${FREEZE_MANIFEST} --catalyst-ablation-json ${CORE_DIR}/${CATALYST_TAG_045}_p1_ablation.json --direct-per-unit-jsonl ${DIRECT_RUN3} --baseline-tier tier2_same_evidence --attribution-v2-json ${ATTR_V2} --tier2-meta-json ${TIER2_META} --catalyst-runtime-json ${RUNTIME_JSON} --output-json ${CORE_DIR}/catalyst_vs_direct_tier2_metrics.json --output-csv ${CORE_DIR}/catalyst_vs_direct_tier2_main_table.csv --output-md docs/reports/2026-05-18-catalyst-vs-direct-llm-main-comparison.md"
run_cmd "$PY scripts/reports/run_direct_llm_stability_replay.py --freeze-manifest ${FREEZE_MANIFEST} --run-artifacts-dir ${CORE_DIR} --k-runs 3 --out-json ${CORE_DIR}/direct_llm_dsv4_stability_k3.json"
run_cmd "$PY scripts/reports/build_industrial_audit_bundle.py --freeze-id thesis-freeze-scheme-c --metrics-json ${CORE_DIR}/catalyst_vs_direct_tier2_metrics.json --per-case-jsonl ${DIRECT_RUN3} --stability-json ${CORE_DIR}/direct_llm_dsv4_stability_k3.json --adjudication-md docs/reports/2026-05-18-catalyst-vs-direct-llm-main-comparison.md --out-json ${CORE_DIR}/industrial_audit_bundle_v3.json"

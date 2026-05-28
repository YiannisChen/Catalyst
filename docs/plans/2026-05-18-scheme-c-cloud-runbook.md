# Scheme C Cloud Runbook

## Preconditions
- branch synced to `exp/p1-1-cloud-run-20260511`
- `AIHUBMIX_API_KEY` exported
- run in `/root/Catalyst`

## One-command batch
```bash
bash scripts/reports/run_scheme_c_batch.sh
```

## Dry run
```bash
bash scripts/reports/run_scheme_c_batch.sh --dry-run
```

## Expected outputs
- `core/thesis_freeze_manifest.json`
- `core/direct_closed_book_gemini_per_unit.jsonl`
- `core/direct_search_augmented_gemini_per_unit.jsonl`
- `core/direct_same_evidence_gemini_per_unit.jsonl`
- `core/catalyst_mt04_per_unit.jsonl`
- `core/catalyst_mt040_ablation.json`
- `core/catalyst_mt045_ablation.json`
- `core/catalyst_mt060_ablation.json`
- `core/attribution_metrics_v2.json`
- `core/catalyst_runtime_stats.json`
- `core/catalyst_vs_direct_tier0_metrics.json`
- `core/catalyst_vs_direct_tier1_metrics.json`
- `core/catalyst_vs_direct_tier2_metrics.json`
- `core/direct_llm_dsv4_stability_k3.json`
- `core/industrial_audit_bundle_v3.json`

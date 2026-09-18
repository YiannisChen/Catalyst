# v1_1_stage1_report_v1

- eval_id: `2792d333e7c01eb528fce77b5a54531343e6acdc2633212d2d3315fc97bb32cc`
- dataset_id: `v1_1_stage1_q011`
- execution_head: `327b3eed`
- handoff_manifest_sha256: `b745902af95e158d33ea68226b4991d933a3e3ed30887429571a3da39f5d6baf`
- handoff_head: `327b3eed6929b53b1c1b5ea7fd2c57fa1c7bec8e`
- handoff_eval_id: `2792d333e7c01eb528fce77b5a54531343e6acdc2633212d2d3315fc97bb32cc`
- comparability: `NON-COMPARABLE`
- gates_passed: `False`

## Hard gates
- `abstain_producing_sufficient`: True
- `citation_correctness`: None
- `duplicate_adjusted_precision`: False
- `false_sufficient`: True
- `no_material_producing_sufficient`: True
- `no_material_without_sanity`: True
- `no_ticker_or_cutoff_violations`: True
- `primary_source_hit`: False
- `recall_at_8`: False
- `unsupported_material_claims`: None
- `unsupported_primary_claims`: True

## Counts
- coverage_limited_count: 11
- model_limited_count: 0
- case_count: 12
- provider_calls: 12
- cost_usd: 0.001634
- latency_ms: 128420
- tokens: 2630

## Integrity evidence
- leakage_scan: PASS (original=2, derived=0, model_visible=0, original_sha256=b855e9351592eba7c324eb0a9de35827ca41db5e8f892d137885c6dbe41c9698, derived_sha256=9f9e19033f6f9bf2f3e53532758279f1e689a07cd1458bb0875e040302c94282)
- secret_scan: PASS (count=0, sha256=0c54b7e283b24ef7991b013aeebef8a4d81bb93b6f4be6115a601c74ba708123)
- original_false_positive_disposition: original terminal fields retained as false positives and exempted only by verified post-Writer run_diagnostics proof
- handoff_verified_files: report_inputs/leakage_scan.json=b855e9351592eba7c324eb0a9de35827ca41db5e8f892d137885c6dbe41c9698; report_inputs/secret_scan.json=0c54b7e283b24ef7991b013aeebef8a4d81bb93b6f4be6115a601c74ba708123; runtime.sqlite3=c6d8d2745adf403046e72712daf53ab789afdae548b893bbf2b3b820e32541b2

_Deterministic rendering of the canonical report JSON._

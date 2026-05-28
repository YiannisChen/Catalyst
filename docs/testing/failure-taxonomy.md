> Supporting reference. Canonical source: `docs/plans/2026-04-30-p0-construction-plan-v3.md`

# Failure Taxonomy

### retrieval_failure
Definition: Retrieval returned no evidence or only weak evidence for the requested move.
Detection signal: Critic cannot assemble sufficiently relevant chunks or the retrieval policy exhausts its allowed expansions.
Deterministic action: Refuse with `INSUFFICIENT` rather than fabricating a causal answer.
Regression-test reference: Covered indirectly by the retrieval-policy and graph refusal-path tests.

### model_failure
Definition: The model call itself fails, times out, or returns content that cannot be parsed into the required schema.
Detection signal: LLM invocation raises, retry budget exhausts, or JSON/schema parsing fails.
Deterministic action: Route to `SYSTEM_ERROR` and persist the failure in trace/error fields.
Regression-test reference: `packages/agents/tests/test_failure_taxonomy.py::test_model_failure_routes_to_system_error_and_traces_error_type`

### consistency_failure
Definition: The output shape is syntactically valid but grounding invariants are broken, such as non-existent citations.
Detection signal: Validator detects missing `evidence_id`, schema mismatch, time-window failure, or similar post-judge inconsistency.
Deterministic action: Allow one correction attempt; on repeat failure downgrade to `PARTIAL` unless the failure is a system error.
Regression-test reference: `packages/agents/tests/test_failure_taxonomy.py::test_consistency_failure_downgrades_to_partial_and_is_traced`

### budget_failure
Definition: The run exceeds its configured cost, token, or latency budget guard.
Detection signal: Projected or observed spend/latency crosses the configured cap, including OD-6 direct-baseline substitution thresholds.
Deterministic action: Apply the deterministic budget policy, typically downgrade model tier or stop expansion instead of continuing at the same cost level.
Regression-test reference: Covered by the OD-6 guard test in `packages/eval/tests/test_direct_llm_baseline.py`.

### provider_failure
Definition: Upstream providers fail repeatedly even before the model can produce a usable answer.
Detection signal: Repeated 429s, 5xxs, transport failures, or provider outages from required external services.
Deterministic action: Surface `SYSTEM_ERROR` and record the provider-facing failure without silently retrying forever.
Regression-test reference: Covered by upstream connector and orchestrator failure-path tests; no dedicated T-12 regression added beyond the model-failure path.

# M7 Stage-1 source-to-metric closure matrix

**Status:** Phase A corrective reference (2026-09-14). Offline only: no provider
call, no publication, no commit, no database mutation.
**Binding plan:** `docs/plans/2026-08-19-catalyst-v1.1-m7-stage1-eval.md`
**Checkpoint guide:** `docs/plans/2026-09-13-m7-completion-operator-guide.md`

## 1. Classification legend

| Class | Meaning |
| --- | --- |
| **OBS** | production-observed fact: the V1.1 runtime observes it during the run and persists it through the M6 contract. |
| **BENCH** | benchmark/human truth: predeclared by the human-adjudicated Stage-1 dataset or its stratification. |
| **DERIVED** | derived eval metric: computed by eval from OBS + BENCH; never reported by the runtime. |
| **N/A** | unavailable/non-scorable: the runtime does not persist a comparable value, so the metric is explicitly non-scorable rather than a scored zero. |

Rule enforced in code: no field is defaulted or inferred from absence unless the
contract proves that meaning. Every proof is stated in the third column.

## 2. Retrieval metrics (`retrieval_metrics.py`)

| Field | Class | Source / proof |
| --- | --- | --- |
| `case_id` | BENCH | eval ordered case ids from the dataset manifest. |
| `ranked_evidence_ids` | OBS | `run_diagnostics.retrieval.ordered_final_ranked_evidence_ids`: the ordered final ranking the runtime actually served (union of per-task final rankings in research-task order). |
| `reranker_contributed` | OBS | `run_diagnostics.retrieval.arms` contains a `reranked` stage for the run. Presence of the stage proves the reranker was served; absence cannot be read as "reranker failed". |
| `latency_ms` | OBS | sum of the per-call measured retrieval latencies recorded by `AgentRetrieverAdapter` (`RetrievalCallObservation.measured_latency_ms`, wall-clock around the retriever call). |
| `degraded` | OBS | non-empty `run_diagnostics.retrieval.degradation_reasons`, recorded from the runtime's own degradation records (`ResearchExecution.degradations`). An empty list is an observed "no degradation recorded", not a default. |
| `ticker_violations` | OBS (DERIVED check) | computed in `_observe_call` over the served hits' `ticker_scope` versus the run ticker. Nonzero would indicate a source-side filter invariant break; both retrieval arms filter by ticker (`fts5.py` `EXISTS ... json_each(c.ticker_associations)`, `dense.py` `array_contains(ticker_associations, ...)`). |
| `cutoff_violations` | OBS (DERIVED check) | computed in `_observe_call` over the served hits' `eligible_at` versus the run cutoff. The V1 converter already drops post-cutoff hits (`hybrid.py` `_to_v1_result_set`), so nonzero indicates an invariant break. |
| `pool.schema_version`, `pool.arms`, `pool.chunk_inventory` | OBS + BENCH | arms are the retrieval stages the runtime actually executed/served (name + the run's observed `top_k`); stage presence is based on control-flow completion, so a successful empty stage remains truthfully recorded while its inventory is empty. A degraded fallback is not a served reranked arm and does not fabricate final IDs. The benchmark declares the arm version (`1.0.0`). `chunk_inventory` is the sorted unique observed candidate evidence set. |
| `pool.corpus_manifest_id`, `pool.index_manifest_id` | OBS | the run's `DataRuntimeIdentity` (persisted in the diagnostics and the RunManifest). |
| `pool.source_artifact_id` | OBS | the persisted `run_diagnostics` artifact `payload_hash`; binds the pool identity to the exact persisted diagnostics bytes. |
| `pool.pool_id` | DERIVED | deterministic sha256 over the fields above (`PoolManifest.compute_pool_id`); validated by the model. |
| `pool.created_at` | OBS | `run_diagnostics.recorded_at` (UTC-aware). |
| `pool.case_id` | BENCH | eval case id; `compute_retrieval_metrics` fails closed if the pool case identity does not match. |
| `pool` as the *frozen M3 four-arm union judgment pool* | N/A | **not used.** The frozen pool identity is not bound to the final Q-001 runtime, so it is never presented as this run's pool. The pool passed to the metric is the *observed* pool identity described above and is documented as such. |

Removed in this pass: the `"p" * 64` synthetic pool id, the empty
`ranked_evidence_ids` list presented as a real result, and the eval-side
`retrieval_authority_for_case` callback.

## 3. Attribution metrics (`attribution_metrics.py`)

| Field | Class | Source / proof |
| --- | --- | --- |
| `case_id` | BENCH | eval ordered case ids. |
| `output_status` | OBS | persisted terminal `run.completed` payload `result_status` (must equal `run_diagnostics.terminal.result_status`). |
| `attribution_type` | OBS | persisted `attribution_result` artifact (terminal sequence); must equal `run_diagnostics.terminal.attribution_type`. |
| `refusal_reason` | N/A | the runtime has no machine-comparable refusal taxonomy; the Stage-1 human refusal reasons are free prose. |
| `refusal_reason_available` | OBS | `run_diagnostics.terminal.refusal_reason_available` (currently always `False`). ABSTAIN cases without a comparable reason are excluded from `refusal_correctness` and counted in `refusal_unavailable_count`; the metric reports `value=None`, `exercised=False`. |
| `claims[].claim_id`, `.role`, `.citation_ids`, `.statement` | OBS | persisted `claim_detail` artifacts (terminal/claim-validation publication). `claim_id`/`role` are required; a missing value fails closed instead of being invented. |
| `claims[].material` | DERIVED | `role in {PRIMARY, SECONDARY}` — the agents' material claim roles; derived from the observed persisted role, never persisted as a runtime judgement. |
| `sanity_tasks_completed` | OBS | `run_diagnostics.trajectory.initial_task_ids`: the initial research tasks the runtime actually dispatched. |
| `latency_ms` | OBS | terminal `run.completed` `total_latency_ms`. |
| `tokens` | OBS (nullable) | sum of every dispatched Analyst/Writer attempt's input/output usage only when every attempt reports complete verifiable values; `None` for any missing/partial/invalid attempt (never a claimed zero or Writer-only total). The same top-level run fact is copied to the case outcome, report input, and output-audit reconstruction. |
| `cost_usd` | OBS (nullable) | `run_diagnostics.provider.cost_usd` with `cost_method`; `unavailable` when no cost bound was computable. |
| `coverage_limited` | BENCH | stratification per-case coverage state (eval-side). |
| `model_limited` | OBS (contract-proven `False`) | a run excluded by a model/runtime limitation terminalizes FAILED, so a COMPLETED row can never be a model-limited exclusion. |

## 4. Trajectory metrics (`trajectory_metrics.py`)

| Field | Class | Source / proof |
| --- | --- | --- |
| `case_id` | BENCH | eval ordered case ids. |
| `corrective_triggered` | OBS | `run_diagnostics.trajectory.corrective_rounds_executed > 0`. |
| `rounds_executed` | OBS | `run_diagnostics.trajectory.corrective_rounds_executed`. |
| `gap_ids` (gap reason codes) | OBS | `run_diagnostics.trajectory.rounds[].gap_reason_codes`: the code-owned `GapReasonCode` values of the gaps the executed round addressed (mapped from the validated gaps; the opaque `gap_id` is not comparable). |
| `corrective_actions` | OBS | `…rounds[].action_ids` (code-owned action identities). |
| `research_fingerprints` | OBS | `…rounds[].research_fingerprints`. |
| `evidence_delta_ids` | OBS | `…rounds[].evidence_delta.added_evidence_ids`; `removed_evidence_ids` is empty by the cumulative-union contract of `EvidenceState`. |
| `produced_structure` | OBS | terminal `result_status != ABSTAIN`. |
| `stop_correct` | DERIVED | eval computes it against `ExpectedResearchBehavior`: a required corrective case that did not trigger is an incorrect stop; an unnecessary corrective is captured separately by `unnecessary_corrective_rate`. |
| `corrected` (recovery) | DERIVED | eval computes it: the round executed and its observed gap reason codes cover at least one predeclared `expected_gap_reason_code`. |

Removed in this pass: the runtime-reported `stop_correct=True` /
`corrected=False` / `new_structure_created=False` defaults.

## 5. Execution ledger (`execution_ledger.py`)

| Field | Class | Source / proof |
| --- | --- | --- |
| `eval_id` | DERIVED | `EvalManifest.evaluation_identity.eval_id`. |
| `case_id` | BENCH | eval ordered case ids. |
| `run_manifest_id`, `run_manifest_hash` | OBS | persisted `run_manifest` artifact `artifact_id` + recomputed canonical hash; must equal the pre-submit capture and `runs.run_manifest_id/manifest_hash`. |
| `result_artifact_id`, `result_artifact_hash` | OBS | persisted `attribution_result` artifact at the terminal sequence + recomputed canonical hash. |
| `terminal_status` | OBS | persisted terminal event type, required to agree with the persisted lifecycle and to be preceded by `assurance.completed`. |
| `attempts` | OBS (eval) | eval attempts counter for the case (never reruns an identity-valid COMPLETED row). |
| `provider_calls` | OBS | `run_diagnostics.provider` analyst + writer provider attempts. |
| `cost_usd` | OBS (nullable) | `run_diagnostics.provider.cost_usd`. |
| `context_pack_ref`, `claim_plan_ref`, `assurance_ref` | OBS | persisted `context_pack` / deterministic claim-plan identity over the persisted `claim_detail` set / persisted `assurance` artifact. No manifest or result hash is substituted for a missing artifact. |
| `run_facts` | DERIVED | built by the adapter purely from persisted rows + persisted diagnostics, then accepted only by the strict recursive run-facts model; no reconstruction coercion or defaulting. |

## 6. Report (`report.py`)

| Field | Class | Source / proof |
| --- | --- | --- |
| `schema_version`, `eval_id`, `dataset_id`, ordered ids | DERIVED | sealed EvalManifest. |
| per-case metrics inputs | — | joined from the ledger `run_facts` (all sourced above). |
| `gates_passed` | DERIVED | all hard-gate `MetricResult`s true; trajectory useful/unnecessary rates stay development signals. |
| comparability marker | DERIVED | mandatory `NON-COMPARABLE` (Q-002). |

A case whose `retrieval.observed` is `False` is rejected by `report.py`: a run
that never reached retrieval must not be reported as a measured zero.

## 7. Provider budget (new in this pass)

| Fact | Class | Source / proof |
| --- | --- | --- |
| per-case provider attempts | OBS | `ProviderBudgetGuard` reservation counter, reconciled against the persisted diagnostics after every case. |
| per-case cost | OBS (bounded) | settled with genuinely reported tokens; the conservative pre-call upper bound is charged when the role reports no usage. `cost_method` records which applied. |
| model prices / role token limits | BENCH (operator) | non-secret `--pricing-json`; identity-bound `(provider, model_id)` prices. Missing price or role limit with a positive USD ceiling fails closed before dispatch. |

## 8. Remaining non-scorable fields

| Field | Why non-scorable | Effect |
| --- | --- | --- |
| `refusal_reason` matching | no machine-comparable runtime refusal taxonomy; human truth is free prose | `refusal_correctness` denominator 0, `value=None`, `exercised=False`, `refusal_unavailable_count` reports the excluded ABSTAIN cases. |
| retrieval dedup drops | the V1.1 `RetrievalResultSet` exposes no post-dedup drop list, and RRF fusion has no drop concept at all: `fusion.fuse` merges candidates by `chunk_id` into one record per id (`packages/data-core/catalyst_data/retrieval/fusion.py:40-61`), so a duplicate is unioned, never dropped-and-reported | `duplicate_drops` is typed `None` in the diagnostics contract and is never reported as "no drops". |
| frozen M3 union-pool identity | not bound to the final Q-001 runtime | retrieval metrics use the observed pool identity; no frozen-pool claim is made. |
| analyst token usage | the Analyst role does not report token usage | diagnostics `total_tokens_in/out` are `None` in that case; the cost guard charges the bounded pre-call upper bound and labels it. |

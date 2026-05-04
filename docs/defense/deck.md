## Slide 1: Positioning

- Catalyst is framed as a traceable attribution control plane for market-move explanations, not a generic chat wrapper.
- Defense claim: `mcj_full` adds deterministic retrieval, critic routing, validation, and trace persistence on top of a single-call `direct_llm` baseline.
- Evaluation is frozen on the 10-case P0 set with Tier-A pins for model ids, DB SHA, code SHA, random seed, and W-15 sentinel handling.

## Slide 2: Architecture

- Data plane: frozen SQLite corpus, SQL time-window retrieval, and replayable trace artifacts.
- Control plane: Miner → Critic → DecisionRouter → Judge → Validator → Finalizer.
- Deterministic components are explicit: retrieval policy, router edges, validator checks, and gate script thresholds.

## Slide 3: Retrieval And Evidence Flow

- Layer 1 (`DIRECT`) applies ticker filter plus ±3 day window.
- Layer 2 (`MACRO`) broadens retrieval without strict ticker filter and loops back through Miner when Critic requests expansion.
- Layer 3 is declared only; `expand_related` short-circuits to refusal in P0.

## Slide 4: Baseline Comparison

- Frozen configs: `direct_llm` and `mcj_full`, 10 cases each, 20 runs total.
- Gate results (`20260503_154053_comparison.json`): all five gates at target — `evidence_validity=1.0`, `schema_validity=1.0`, `trace_completeness=1.0`, `should_refuse_hit_rate=1.0`, `cost_latency_reported=true`; `check_p0_gate.py` exit 0 (GREEN).
- Cost / latency (reported; Tier B variance accepted for exact ms): `direct_llm` avg cost USD ~0.00113, avg latency ~4.3 ms; `mcj_full` avg cost USD ~0.00265, avg latency ~4.7 ms (mocked stack; frozen artifact is authoritative per strategy §4.1 Tier B).
- Case distribution: 5 SUFFICIENT, 2 PARTIAL, 3 should-refuse (all three refusals hit `INSUFFICIENT`).

## Slide 5: Failure Taxonomy

- Retrieval failure: sparse or irrelevant evidence returned from the frozen corpus.
- Model failure: provider or parsing failure in Critic, Judge, Validator, or `direct_llm`.
- Consistency failure: evidence ids, time window, schema, or magnitude sanity checks fail validator policy.
- Budget failure and provider failure remain documented even though P0 does not ship the P2 budget breaker.

## Slide 6: Gates And Reproducibility

- Gate script evaluates `evidence_validity`, `schema_validity`, `trace_completeness`, `should_refuse_hit_rate`, and `cost_latency_reported`.
- Frozen report headers carry `model_id_per_role`, `provider_version`, `db_sha256`, `code_git_sha`, `random_seed`, `case_distribution`, `geo_corpus_tier`, and `lancedb_dir_sha256`.
- Artifacts: `data/eval_reports/<frozen_ts>_comparison.{json,md}` and `data/traces/<run_id>.json`; Tier-A `mcj_full` rerun for T-15 at `data/eval_reports/rerun_20260504_072205_mcj_full.{json,md}` with per-case status parity (empty `status_mismatches`) and traces `data/traces/rerun_<run_id>.json`.
- Offline rehearsal notebook: `notebooks/demo.ipynb` (committed executed copy `notebooks/demo_executed.ipynb`).

## Slide 7: Done Deferred Waivers

- **Done in P0 (defense):** 4-state output enum, validator (4 checks), deterministic router, critic contract, retrieval L1+L2, trace persistence, frozen 10-case set, direct_llm vs mcj comparison + 5 gates, Tier-A pins (`model/db/code sha` + W-15 Lance sentinel), frozen corpus, demo notebook (`notebooks/demo_executed.ipynb`).
- **Deferred to P1:** `rag_only` baseline (W-01), LangSmith (W-06), cross-source semantic dedup (W-11), RAG precision upgrades (W-14).
- **Deferred to P2/Roadmap:** Layer3 retrieval (W-02), Critic LLM grading (W-03), budget breaker (W-04), model routing (W-05), full eval matrix (W-07), expanded refusal precision/recall (W-08), GDELT integration (W-09), ADR-004~007.
- **Defense-window waivers:** W-01..W-09 and W-11..W-14 map one-to-one to strategy spec §11 and this plan §A.4.

## Slide 8: Roadmap And W-15

- W-15 disclosure: vector similarity search (`LanceDB` / `bge-m3`) is deferred to P1, so P0 frozen eval uses SQL-only retrieval.
- Gate script must treat `lancedb_dir_sha256="DEFERRED_P1"` as a warning, not a hard fail, while still evaluating all core gates normally.
- Roadmap re-entry starts with T-07 vector index build, retrieval-quality recalibration, and a rerun of threshold calibration against the vector path.

## Appendix A9: Audit Hooks

- Latest frozen comparison artifact is the source for Day 10 gate and later T-15 audit rows.
- `check_p0_gate.py` returns `0` for GREEN, `1` for RED, and `2` for script error / malformed artifact.
- If a gate fails under W-15, the deck and audit must attribute sparse-retrieval degradation explicitly instead of treating it as an unclassified code defect.

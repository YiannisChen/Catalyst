# Pre-B6 Evidence Convergence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement Pre-B6 evidence convergence so all 40 tickers have per-document SEC body readiness, honest FMP lineage, v13/filing_v3 corpus, lexical baselines, and a production source_bundle before any GPU embedding.

**Architecture:** Offline TDD first (B2-E-I); authorized live SEC S1–S2 then new-root S4 (B2-E-X/D); candidate-only FMP reconciliation; ordered re-snapshot/corpus/FTS/promote (B2-E-S); freeze lexical baseline (B4-L-B); B6-L source bundle without vectors; B6-G/B6-E only after GO.

**Tech Stack:** Python 3.12+, SQLite, existing B2 planner/executor, SEC connectors, B3 corpus, B4 FTS, pytest.

**Binding design:** `docs/plans/2026-07-29-pre-b6-evidence-convergence-design.md`
**No unresolved architect TBD.** Every decision is binding in the design.

---

## 0. Execution rules

1. Default offline. Live / promote / GPU each need **separate** architect authorization.
2. No production code changes in this documentation wave (manager already limited scope). When executing later: never mutate baseline snapshot file.
3. TDD: RED command → expected fail → implement → GREEN command → focused regression.
4. No “implement everything”; no inventing test counts.
5. No provider/network/GPU/stage/commit/push without task-level authorization text.

### Phase boundaries

| Phase | Network | DB writes | GPU |
|---|---|---|---|
| B2-E-I Tasks 0–12b | no | temp fixtures only | no |
| B2-E-X Task 13a–d | yes SEC S1+S2 (+S3 offline freeze) | candidate; **three root families** | no |
| B2-E-D Task 14 | yes SEC S4 | candidate, **S4 root** | no |
| B2-E-R Task 15 | no | candidate only | no |
| B2-E-S Task 16 | no | candidate → **PROMOTE then promote_candidate** | no |
| B4-L-B Task 17 | no | eval artifacts | no |
| B6-L Tasks 18–19 | no | source_bundles dir | no |
| B6-G | GPU auth | embedding side dir | yes |
| B6-E Task 20 | LLM/web auth | eval reports | no |

---

## 1. Task 0 — Freeze baseline identity

**Files:** none production; write `data/run_reports/b2e_baseline_freeze.json` only when executing (gitignored).

**Commands (execute later):**

```bash
git rev-parse HEAD
git branch --show-current
shasum -a 256 data/snapshots/catalyst_b2o_d5e5f7fef11581c1f516bc484f64e31c1776a4202eb0c68aeada0bd0b98817c0.db
```

**Required recorded values:**

| Key | Value |
|---|---|
| branch | `recovery/b2o-data-readiness` |
| HEAD | execution-start HEAD containing the manager-approved Pre-B6 plan commit |
| baseline sha256 | `7cc49ba1fd5245a6b8700ba7c26638d54f1dbb644f0aeb7cbaff684aa349f97e` |

**Precondition:** `git status --short` is empty at execution start, both
2026-07-29 plan files are tracked at HEAD, and the baseline SHA matches exactly.
The old documentation freeze HEAD `9733cf…` is historical evidence only and is
not the expected implementation HEAD. Freeze the actual approved execution HEAD;
any later drift during the run → STOP.

---

## 2. Offline implementation tasks (B2-E-I)

### Task 1 — `.gitignore` inventory + source_bundles

**Files:**
- Modify: `.gitignore`

**Add exact lines:**

```gitignore
/data/manifests/sec_filing_inventory_*.json
/data/source_bundles/
```

**RED:** none (doc+config).
**GREEN:** `git check-ignore -v data/manifests/sec_filing_inventory_deadbeef.json data/source_bundles/x` shows ignore rules.

**Regression:** existing `active_data_snapshot.json` ignore still works.

---

### Task 2 — Migration v13 filing_v3 surface

**Files:**
- Modify: `packages/data-core/catalyst_data/migrations.py`
- Test: `packages/data-core/tests/test_migration_v13_filing_v3.py`

**RED commands:**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_migration_v13_filing_v3.py -q
```

**Expected failure:** collection/import errors or assert `CURRENT_SCHEMA_VERSION == 13` fails (still 12).

**RED test names:**
- `test_current_schema_version_is_13`
- `test_v13_always_table_rebuild_not_alter_check`
- `test_fresh_db_accepts_filing_v3_insert`
- `test_upgrade_v12_to_v13_preserves_filing_v2_rows`
- `test_v13_recreates_indexes_idx_corpus_chunks_document_available_source_class_manifest`
- `test_v13_recreates_triggers_insert_and_update_guard`
- `test_v13_adds_nullable_filing_document_id_and_unique_partial_index`
- `test_v13_document_id_guards_shape_and_immutability`
- `test_v13_legacy_filing_document_null_id_preserved`
- `test_v13_savepoint_rollback_restores_v12`
- `test_trigger_rejects_unknown_profile`
- `test_trigger_allows_news_v2_filing_v2_filing_v3`
- `test_foreign_key_check_empty_after_v13`
- `test_v13_full_row_logical_hashes_match_before_after_rebuild`

**GREEN:** same command, all pass; `PRAGMA user_version=13` on fixture.

**Focused regression:**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_checkpoint_identity_v12.py packages/data-core/tests/test_migrations.py -q
```

---

### Task 3 — FilingInventoryManifest pure builder (S3 identity)

**Files:**
- Create: `packages/data-core/catalyst_data/sec/__init__.py`
- Create: `packages/data-core/catalyst_data/sec/inventory.py`
- Create: `packages/data-core/tests/fixtures/sec/submissions_aapl_min.json`
- Create: `packages/data-core/tests/fixtures/sec/index_canonical_aapl_8k.html`
- Create: `packages/data-core/tests/fixtures/sec/index_headers_aapl_8k.html`
- Create: `packages/data-core/tests/fixtures/sec/index_headers_legacy.htm`
- Create: `packages/data-core/tests/fixtures/sec/index_malformed_no_primary.html`
- Create: `packages/data-core/tests/fixtures/sec/index_duplicate_sequence.html`
- Test: `packages/data-core/tests/test_sec_inventory.py`
- Test: `packages/data-core/tests/test_sec_index_parser.py`

**RED:**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_sec_inventory.py packages/data-core/tests/test_sec_index_parser.py -q
```

**RED names (inventory):**
- `test_inventory_id_excludes_document_plan_hash`
- `test_inventory_id_stable_for_fixed_entries`
- `test_document_sort_primary_then_sequence`
- `test_ex99_roles_are_exhibit_99_n_not_all_1`
- `test_carry_in_periodic_only`
- `test_missing_carry_in_slot_recorded`
- `test_requiredness_and_reason_enter_inventory_id`
- `test_response_cannot_downgrade_mandatory_to_optional`
- `test_unknown_ex99_extension_is_mandatory`
- `test_pdf_ex99_descriptor_is_optional_degraded`
- `test_primary_pdf_always_blocks_source_ready`

**RED names (index parser / transport contract):**
- `test_canonical_index_html_table_parser`
- `test_index_headers_html_fallback_when_canonical_404`
- `test_legacy_index_headers_htm_second_fallback`
- `test_malformed_or_no_primary_is_failed`
- `test_duplicate_sequence_deterministic_sort`

**GREEN:** same pytest green; oracle `inventory_id` hard-coded hex from fixture.

---

### Task 4 — Index cell identity (S2)

**Files:**
- Create: `packages/data-core/catalyst_data/sec/index_cells.py`
- Create: `packages/data-core/catalyst_data/sec/cell_record.py`
- Modify: `packages/data-core/catalyst_data/update_planner.py` (validate SEC plan-cell record v2)
- Test: `packages/data-core/tests/test_sec_index_cells.py`

**RED names:**
- `test_index_cell_id_includes_accession_cik_form_policy`
- `test_index_cell_distinct_from_submissions_cell`
- `test_no_document_discovery_in_index_module` (module has no fetch side effects)
- `test_sec_cell_v2_rejects_missing_and_unknown_extension_keys`
- `test_sec_cell_v2_full_record_enters_plan_hash`
- `test_legacy_source_cell_ids_and_serialization_unchanged`

**Commands:**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_sec_index_cells.py -q
```

---

### Task 5 — Document cell identity + plan_hash (S4)

**Files:**
- Create: `packages/data-core/catalyst_data/sec/document_cells.py`
- Create: `packages/data-core/catalyst_data/sec/convergence_identity.py`
- Test: `packages/data-core/tests/test_sec_document_cells.py`
- Test: `packages/data-core/tests/test_b2e_convergence_identity.py`

**RED names:**
- `test_document_cell_id_includes_inventory_id_role_file_url`
- `test_document_id_formula`
- `test_document_plan_hash_includes_inventory_and_ordered_cells_and_policy_versions`
- `test_document_plan_differs_from_baseline_b2o_plan_hash`
- `test_document_cell_record_contains_persisted_document_id_and_requiredness`
- `test_convergence_plan_hash_binds_s1_s2_inventory_s4_reconciliation_and_v13`
- `test_convergence_plan_hash_excludes_timestamps_and_paths`
- `test_snapshot_plan_hash_is_composite_not_any_stage_plan_hash`

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_sec_document_cells.py -q
```

---

### Task 6 — Extract + PDF policy

**Files:**
- Create: `packages/data-core/catalyst_data/sec/extract.py`
- Test: `packages/data-core/tests/test_sec_extract.py`
- Fixtures: HTML snippet, whitespace-only, PDF magic bytes

**RED names:**
- `test_html_success_meets_rag_min_char`
- `test_whitespace_not_success`
- `test_pdf_primary_mandatory_failed`
- `test_pdf_exhibit_optional_degraded`
- `test_no_ocr_empty_string_success`

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_sec_extract.py -q
```

---

### Task 7 — Materializer offline with injected transport

**Files:**
- Modify: `packages/data-core/catalyst_data/connectors/sec.py`
- Modify: `packages/data-core/catalyst_data/b2o.py`
- Modify: `packages/data-core/catalyst_data/update_pipeline.py`
- Modify: `packages/data-core/catalyst_data/update_planner.py`
- Modify: `packages/data-core/catalyst_data/pipeline/sec_normalize.py` (remove all-`exhibit_99_1` path from new code path; leave legacy function only if tests still need it gated)
- Test: `packages/data-core/tests/test_sec_document_materialize.py`
- Test: `packages/data-core/tests/test_sec_index_materialize.py`

**RED names:**
- `test_success_writes_raw_attempt_filing_document_provenance`
- `test_entity_type_filing_entity_id_document_id`
- `test_filing_documents_persists_same_document_id_as_provenance_and_corpus_input`
- `test_request_count_equals_attempts`
- `test_placeholder_empty_not_text_ready`
- `test_resume_same_document_plan_skips_complete_cells`
- `test_new_root_parent_run_id_null_for_document_plan`
- `test_sec_submissions_endpoint_never_fetches_index_or_document`
- `test_sec_index_endpoint_uses_full_cell_extensions_in_transport`
- `test_sec_index_fallback_requests_all_ledgered_with_raw_response`
- `test_sec_document_full_identity_reaches_transport_and_redacted_ledger`
- `test_checkpoint_resume_uses_frozen_plan_cell_id_without_reconstructing_extensions`
- `test_endpoint_dispatch_rejects_unknown_sec_endpoint`

```bash
.venv/bin/python -m pytest \
  packages/data-core/tests/test_sec_index_materialize.py \
  packages/data-core/tests/test_sec_document_materialize.py -q
```

**Focused regression:**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_update_pipeline.py -q --tb=line
```

---

### Task 8 — sec_source_ready vs sec_evidence_ready

**Files:**
- Modify: `packages/data-core/catalyst_data/coverage_audit.py`
- Modify: `packages/data-core/catalyst_data/manifests/snapshot.py` (coverage binds source ready)
- Test: `packages/data-core/tests/test_b2e_sec_readiness.py`

**RED names:**
- `test_sec_source_ready_true_with_zero_corpus_chunks`
- `test_sec_evidence_ready_false_with_zero_corpus_chunks`
- `test_data_snapshot_manifest_uses_sec_source_ready_not_evidence`
- `test_data_snapshot_manifest_uses_convergence_plan_hash`
- `test_sec_evidence_ready_false_when_doc_b_has_zero_chunks_doc_a_has_ten`
- `test_aggregate_chunk_count_does_not_pass_missing_document`
- `test_report_keys_include_sec_source_ready_and_sec_evidence_ready`
- `test_optional_degraded_excluded_from_mandatory_denominator`
- `test_missing_carry_in_slot_blocks_source_ready`

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_b2e_sec_readiness.py -q
```

---

### Task 9 — filing_v3 profile + section oracles

**Files:**
- Create: `packages/data-core/catalyst_data/corpus/filing_v3.py`
- Modify: `packages/data-core/catalyst_data/index_builder.py` (use filing_v3 for new SEC docs; `available_at` Z)
- Test: `packages/data-core/tests/test_filing_v3_sections.py`
- Fixtures: one HTML fixture per form class: `8k`, `ex99`, `6k`, `10q`, `10k`, `20f`

**RED names (each form):**
- `test_8k_item_section_keys_oracle`
- `test_ex99_role_is_section_root`
- `test_6k_role_bounded`
- `test_10q_part_item_oracle`
- `test_10k_part_item_oracle`
- `test_20f_item_1_to_19_oracle`
- `test_parse_failure_unknown_000_degraded_bounded`
- `test_long_doc_multiple_chunks_max_384`
- `test_no_whole_filing_single_chunk_fallback`
- `test_token_constants_384_320_48_64`

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_filing_v3_sections.py -q
```

**Constants must import from shared module or equal news_v2:**

```text
MAX_TOKENS=384 TARGET_TOKENS=320 MAX_OVERLAP=48 MAX_PREFIX_TOKENS=64
```

---

### Task 10 — plan/apply checkpoint reconciliation (request_count only)

**Files:**
- Create: `packages/data-core/catalyst_data/ingestion/checkpoint_reconciliation.py`
- Test: `packages/data-core/tests/test_checkpoint_reconciliation.py`

**API:**
- `plan_checkpoint_reconciliation(...) -> ProposedCheckpointChanges` (immutable)
- `apply_checkpoint_reconciliation(..., expected_plan_hash, dry_run=True)`

**RED names:**
- `test_plan_proposes_request_count_only`
- `test_dry_run_true_zero_writes`
- `test_apply_wrong_run_id_refused`
- `test_apply_empty_change_list_noop`
- `test_apply_rowcount_mismatch_full_rollback`
- `test_second_apply_idempotent`
- `test_refuses_frozen_realpath`
- `test_refuses_snapshots_dir_symlink`
- `test_refuses_active_promoted_path`
- `test_allows_only_candidates_dir`
- `test_no_cli_bypass_flag_exists`
- `test_does_not_modify_status_pages_items_raw_asset_id`
- `test_atomic_audit_json_before_after_hashes`

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_checkpoint_reconciliation.py -q
```

---

### Task 11 — 40 dual-gate probes + 12 case specs

**Files:**
- Create: `packages/eval/golden_set/pre_b6_coverage_probes_v1.json`
- Create: `packages/eval/golden_set/pre_b6_attribution_cases_v1.jsonl`
- Create: `packages/eval/catalyst_eval/probes/coverage_invariant.py`
- Create: `packages/eval/catalyst_eval/probes/lexical_smoke.py`
- Test: `packages/eval/tests/test_pre_b6_case_pack.py`
- Test: `packages/eval/tests/test_pre_b6_probes.py`

**RED names (case pack):**
- `test_exactly_40_probes_match_universe`
- `test_twelve_cases_mutex_histogram_4_2_2_2_2`
- `test_no_documented_empty_pass_flag_in_probe_schema`

**RED names (probes):**
- `test_corpus_coverage_invariant_requires_eligible_and_filing_v3`
- `test_lexical_smoke_query_uses_nfc_ascii_word_terms_not_bge_subwords`
- `test_lexical_smoke_stopwords_v1_exact_set`
- `test_lexical_smoke_query_deduplicates_and_uses_first_8_terms`
- `test_lexical_smoke_query_escapes_fts5_and_uses_and_semantics`
- `test_lexical_smoke_fails_when_no_non_stopword_terms`
- `test_lexical_smoke_query_stable_across_reruns`
- `test_lexical_smoke_ticker_filter_fixed`
- `test_lexical_smoke_cutoff_equals_anchor_available_at`
- `test_lexical_smoke_requires_anchor_or_sibling_document_hit`
- `test_lexical_smoke_is_not_quality_metric_label`

```bash
.venv/bin/python -m pytest packages/eval/tests/test_pre_b6_case_pack.py packages/eval/tests/test_pre_b6_probes.py -q
```

---

### Task 12 — Source bundle export (no vectors)

**Files:**
- Create: `packages/data-core/catalyst_data/retrieval/source_bundle.py`
- Test: `packages/data-core/tests/test_source_bundle.py`

**RED names:**
- `test_export_sorted_excludes_tombstones`
- `test_source_bundle_id_formula`
- `test_export_contains_no_vectors_field`
- `test_export_refuses_wrong_corpus_manifest`
- `test_checksums_file_covers_all_artifacts`

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_source_bundle.py -q
```

---

### Task 12b — Quarantine legacy embed script + B6-L pins

**Files:**
- Modify: `packages/data-core/scripts/build_embeddings_gpu.py` (LEGACY_FROZEN_EVAL banner)
- Modify: `packages/data-core/catalyst_data/config.py` (BGE revision constants)
- Create: `packages/data-core/catalyst_data/retrieval/embedder.py` (interface + fake for tests only)
- Test: `packages/data-core/tests/test_corpus_embedder_grain.py`

**RED names:**
- `test_legacy_script_help_mentions_legacy_frozen_eval`
- `test_embedder_sql_uses_corpus_chunks`
- `test_fake_vectors_only_in_test_helper_not_export_api`

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_corpus_embedder_grain.py -q
```

---

## 3. Authorized live / promote tasks

### Task 13a — Candidate bootstrap + v13

**Authorization:** none (local copy).

1. Verify Task 0 baseline sha256.
2. `sqlite3` backup baseline → `data/candidates/catalyst_b2e_<unix_ts>.working.db`
3. `run_migrations` → user_version **13** (table rebuild).
4. Baseline sha256 still `7cc49ba1…`.

---

### Task 13b — S1 submissions plan (independent root)

**Authorization:** `LIVE_SEC_DISCOVERY`

1. Build plan with **exactly 40** static `sec_submissions` cells.
2. First run: `parent_run_id=NULL`.
3. Resume only with same S1 `plan_hash` lineage.
4. **Must not** parent to S2/S4/B2-O.
5. Stop when 40/40 terminal-complete.

---

### Task 13c — S2 filing-index plan (independent root)

**Authorization:** `LIVE_SEC_DISCOVERY` (same grant covers S2 after S1 terminal)

1. After S1 terminal, expand selected filings → ordered `sec_filing_index` cells.
2. S2 `plan_hash` includes ordered `index_cell_id`s + form/carry-in/rate/retry policy versions.
3. First run: `parent_run_id=NULL`.
4. Resume only same S2 plan_hash lineage.
5. Use canonical index URL fallback order (design A.2).
6. **Must not** parent to S1/S4.

---

### Task 13d — S3 freeze inventory

**Authorization:** none after S2 terminal.

1. Build FilingInventoryManifest with frozen `requiredness` / `requiredness_reason`.
2. Write `data/manifests/sec_filing_inventory_<inventory_id>.json`.
3. Record `inventory_id`.
4. Baseline sha unchanged.

**Failure after any of 13b–13d:** stop; no S4; no promote.

---

### Task 14 — S4 document fetch (independent root)

**Authorization:** `LIVE_SEC_DOCUMENT_FETCH`

1. Load frozen inventory; build document plan; record document plan_hash
   (inventory_id + ordered document_cell_ids + policy versions).
2. First run: `parent_run_id=NULL`.
3. Resume only same S4 document plan_hash lineage.
4. **Must not** parent to S1/S2/B2-O.
5. After terminal: require **`sec_source_ready=true`** (no corpus yet).
6. Baseline sha256 check.

---

### Task 15 — FMP plan/apply reconciliation

**Authorization:** none (candidate offline).

```text
plan = plan_checkpoint_reconciliation(candidate, lineage, source_type=fmp_fundamentals, endpoint_name=balance_sheet)
apply_checkpoint_reconciliation(candidate, plan, expected_plan_hash=plan.hash, dry_run=True)  # evidence
apply_checkpoint_reconciliation(candidate, plan, expected_plan_hash=plan.hash, dry_run=False)
```

**Evidence:** broken request_count 25→0; dry-run zero writes; audit JSON; 402 still optional.
**Refuse** non-candidate paths.

---

### Task 16 — Snapshot, corpus, FTS, probes, promote_candidate

**Authorization:** `PROMOTE` required **before** calling `promote_candidate()` only.

**Exact order (design §G):**

1. Confirm `sec_source_ready=true`
2. Compute `reconciliation_evidence_hash` and `convergence_plan_hash`; verify the
   composite binds S1/S2/inventory/S4/v13/readiness policy and excludes timestamps/paths
3. `build_data_snapshot_manifest(plan_hash=convergence_plan_hash)` →
   **new `snapshot_id`** (coverage uses sec_source_ready)
4. `publish_corpus_with_resource_gate` / `build_corpus_and_lexical_index` with
   `certified_snapshot_identity=snapshot_id` (filing_v3)
5. FTS / `lexical_index_state`
6. Confirm **`sec_evidence_ready=true`**
7. 40 corpus coverage invariants + 40 lexical smoke probes
8. integrity_check / FK / hash / identity
9. Obtain **PROMOTE** authorization
10. Call **`promote_candidate()` once** (versioned snapshot + active pointer)
11. Verify active pointer, new snapshot SHA, baseline SHA == `7cc49ba1…`
12. Candidate path gone / final path exists

**Forbidden:** separate invent-publish API; promote before step 9 authorization;
corpus before snapshot_id.

---

### Task 17 — B4-L-B lexical baseline freeze

**Authorization:** none (read promoted post-B2-E DB).

**Output:**
`data/eval_reports/lexical_baseline_<corpus_manifest_id>.json`
(12-case top-20 + frozen smoke probe queries/anchors; look_ahead=0)

---

### Task 18 — B6-L production source_bundle export

**Authorization:** none.

Export text+hash source bundle for frozen corpus_manifest_id.
No vectors. Verify `source_bundle_id` + `checksums.sha256`.

---

### Task 19 — B6-L offline module suite (explicit; no “remaining”)

GPU not run. No model download. No CUDA in these tests.

#### Task 19.1 — Dense result adapter

**Files:** Create `packages/data-core/catalyst_data/retrieval/dense.py`;
Test `packages/data-core/tests/test_dense_retrieval.py`
**RED:** `test_dense_returns_retrieval_result_set`; `test_dense_respects_cutoff_and_ticker`
**Cmd:** `.venv/bin/python -m pytest packages/data-core/tests/test_dense_retrieval.py -q`
**Fail:** import/missing; **GREEN:** fixture embedder only.

#### Task 19.2 — Candidate scope equality

**Files:** Modify `packages/data-core/catalyst_data/retrieval/dense.py`;
Test `packages/data-core/tests/test_retrieval_candidate_scope.py`
**RED:** `test_dense_and_lexical_share_filter_scope`;
`test_scope_equality_includes_manifest_status_ticker_and_cutoff`
**Cmd:** `.venv/bin/python -m pytest packages/data-core/tests/test_retrieval_candidate_scope.py -q`
**GREEN:** same ticker/cutoff/manifest/status set.

#### Task 19.3 — RRF k=60

**Files:** Create `packages/data-core/catalyst_data/retrieval/fusion.py`;
Test `packages/data-core/tests/test_fusion.py`
**RED:** `test_rrf_k_is_60`; `test_rrf_score_formula_oracle`
**Cmd:** `.venv/bin/python -m pytest packages/data-core/tests/test_fusion.py -q`

#### Task 19.4 — Hybrid mode

**Files:** Create `packages/data-core/catalyst_data/retrieval/hybrid.py`;
Test `packages/data-core/tests/test_hybrid_retrieval.py`
**RED:** `test_hybrid_mode_serves_rrf_without_reranker`;
`test_hybrid_uses_exact_lexical_dense_candidate_scope`
**Cmd:** `.venv/bin/python -m pytest packages/data-core/tests/test_hybrid_retrieval.py -q`
**GREEN:** `mode_served=hybrid`; reranker not called.

#### Task 19.5 — Reranker top-8 presentation

**Files:** Create `packages/data-core/catalyst_data/retrieval/reranker.py`;
Test `packages/data-core/tests/test_reranker.py`
**RED:** `test_reranker_candidate_set_equality`; `test_reranker_top_8_presentation`
**Cmd:** `.venv/bin/python -m pytest packages/data-core/tests/test_reranker.py -q`

#### Task 19.6 — mode_requested / mode_served fallback

**Files:** Modify `packages/data-core/catalyst_data/retrieval/hybrid.py`;
Test `packages/data-core/tests/test_retrieval_degradation.py`
**RED:** `test_reranker_failure_serves_hybrid`; `test_one_arm_failure_mode_served`;
`test_mode_requested_and_mode_served_always_reported`
**Cmd:** `.venv/bin/python -m pytest packages/data-core/tests/test_retrieval_degradation.py -q`
**GREEN:** exact degradation reasons per B6 contract.

#### Task 19.7 — IndexManifest + source_bundle binding

**Files:** Create `packages/data-core/catalyst_data/retrieval/index_manifest.py`;
Test `packages/data-core/tests/test_index_manifest.py`
**RED:** `test_index_manifest_binds_source_bundle_id_corpus_and_revisions`;
`test_index_manifest_identity_excludes_created_at`
**Cmd:** `.venv/bin/python -m pytest packages/data-core/tests/test_index_manifest.py -q`

#### Task 19.8 — GPU result import validation (offline fixture)

**Binding file:** create `packages/data-core/catalyst_data/retrieval/import_vectors.py`
(do not choose between modules during execution);
Test `packages/data-core/tests/test_import_vectors.py`
**RED:** `test_import_rejects_wrong_source_bundle_id`; `test_import_rejects_dim_mismatch`;
`test_import_rejects_checksum_failure`
**Cmd:** `.venv/bin/python -m pytest packages/data-core/tests/test_import_vectors.py -q`
**GREEN:** no CUDA; synthetic float32 files.

#### Task 19.9 — No download / no CUDA fixtures

**Files:** Test `packages/data-core/tests/test_b6l_offline_boundary.py`
**RED:** `test_b6l_suite_does_not_import_torch_cuda`; `test_b6l_suite_does_not_call_snapshot_download`
**Cmd:** full B6-L list:

```bash
.venv/bin/python -m pytest \
  packages/data-core/tests/test_dense_retrieval.py \
  packages/data-core/tests/test_retrieval_candidate_scope.py \
  packages/data-core/tests/test_fusion.py \
  packages/data-core/tests/test_hybrid_retrieval.py \
  packages/data-core/tests/test_reranker.py \
  packages/data-core/tests/test_retrieval_degradation.py \
  packages/data-core/tests/test_index_manifest.py \
  packages/data-core/tests/test_import_vectors.py \
  packages/data-core/tests/test_b6l_offline_boundary.py \
  packages/data-core/tests/test_source_bundle.py \
  packages/data-core/tests/test_corpus_embedder_grain.py -q
```

---

### Task 20 — B6-E experiments (after B6-G)

**Authorization:** `ANSWER_EXPERIMENT` and separately `GPT_WEB_HUMAN`.

**Retrieval metrics arms only:** lexical, dense, hybrid, reranked.

**Answer experiment:** B5 workflow; freeze:

| Param | Value |
|---|---|
| provider | `aihubmix` |
| base_url | `https://aihubmix.com/v1` |
| model_id | `gemini-2.5-flash-nothink` |
| temperature | `0.0` |
| max_tokens | `2048` |

If `build_llm` cannot resolve this model_id → status
**`BLOCKED_BY_MODEL_FREEZE`** — do not pick another model.

**gpt_same_evidence:** hybrid top-20 texts; same model freeze; not a retrieval arm.

**gpt_web:** required external human comparison; log fields per design; does not block reranker gate.

**Reranker gate code:** seed=20260729, resamples=10000; KEEP/KILL per design.

**Files:**
- Create: `packages/eval/catalyst_eval/metrics/reranker_gate.py`
- Test: `packages/eval/tests/test_reranker_gate.py`

**RED names:**
- `test_kill_when_ci_low_nonpositive`
- `test_kill_when_candidate_equality_not_one`
- `test_kill_when_lookahead_positive`
- `test_kill_when_abstention_regresses`
- `test_keep_only_when_all_gates_pass`

---

## 4. Task completion evidence matrix

| Task | Phase | Offline tests | Live auth | Artifact | Baseline sha protected |
|---:|---|---|---|---|---|
| 0 | freeze | cmd | no | freeze json | verify |
| 1 | I | check-ignore | no | gitignore | n/a |
| 2 | I | v13 rebuild tests | no | — | n/a |
| 3–6 | I | inventory/index/doc | no | fixtures | n/a |
| 7 | I | materialize | no | — | n/a |
| 8 | I | source vs evidence ready | no | — | n/a |
| 9 | I | filing_v3 | no | — | n/a |
| 10 | I | plan/apply reconcile | no | — | n/a |
| 11 | I | dual probes + cases | no | golden_set | n/a |
| 12–12b | I | source_bundle | no | — | n/a |
| 13a | X | — | no | candidate v13 | yes |
| 13b | X | — | LIVE S1 root | submissions | yes |
| 13c | X | — | LIVE S2 root | index cells | yes |
| 13d | X | — | no | inventory_id | yes |
| 14 | D | — | LIVE S4 root | documents | yes |
| 15 | R | — | no | repair audit | yes |
| 16 | S | — | **PROMOTE** then `promote_candidate` | new snapshot | yes |
| 17 | B4-L-B | — | no | lexical baseline | read-only |
| 18 | B6-L | yes | no | source_bundle | read-only |
| 19.1–19.9 | B6-L | yes | no | — | n/a |
| 20 | B6-E | gate tests | ANSWER+WEB | arm reports | read-only |

---

## 5. Pre-B6 GO checklist

1. Baseline sha256 unchanged (`7cc49ba1…`).
2. Active DB `PRAGMA user_version = 13`.
3. `sec_source_ready == true` and `sec_evidence_ready == true`.
4. Unit tests: source ready with zero chunks; evidence false with zero chunks; A10/B0 evidence false.
5. FMP request_count broken count 0; 402 optional only.
6. New snapshot_id and corpus_manifest_id ≠ baseline.
7. 40/40 corpus coverage invariants + 40/40 lexical smoke probes.
8. Lexical baseline artifact exists.
9. source_bundle_id exists; no vectors.
10. Task 19.1–19.9 green; B6-G not started.
11. `promote_candidate` already used under PROMOTE auth; active pointer valid.

**GO** only if all true; else **NO_GO**.

---

## 6. Explicit non-goals

- OCR for PDF primary
- Parenting document runs to B2-O lineage
- Fake vectors in production bundles
- Silent model substitution
- Aggregating gpt_web into internal metrics
- In-place rewrite of baseline snapshot file

---

## 7. Doc-only wave confirmation

This file and peer design updates are documentation. Implementation starts only after manager says READY and offline implementation is authorized.

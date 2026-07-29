# B2-O Data Readiness Implementation Plan

Date: 2026-07-23
Status: executable binding plan; 40-name universe ratified on 2026-07-23
Design: `docs/plans/2026-07-23-b2o-data-readiness-design.md`

This plan is split into B2-O-I and B2-O-X.

- B2-O-I is implementation plus offline verification. It may finish without live authorization, but it must report only `B2-O-I COMPLETE`.
- B2-O-X is authorized operational execution. It is required for `B2-O COMPLETE` and is not deferred to another plan.

No task may call providers, mutate protected DBs, stage, commit, push, create a PR, access a GPU server, or start B6 unless the task explicitly says it is the B2-O-X live authorization step.

## Existing Inventory To Reuse

Before declaring any new module, inspect the actual production code and tests:

- `packages/data-core/catalyst_data/migrations.py`: existing migration registry and `run_migrations(conn)`. v8/v9/v10 already exist and must not be reimplemented; v11 adds B2-O cell identity fields and FMP statement storage; v12 converges checkpoint persistence on `checkpoint_id` plus `UNIQUE(run_id, cell_id)`.
- `packages/data-core/catalyst_data/update_planner.py`: existing `UpdatePlan`, `plan_update()`, `compute_plan_hash()`, and drift checking.
- `packages/data-core/catalyst_data/update_pipeline.py`: existing
  `execute_update(db=conn, plan=plan, transport=live_transport)` is the sole
  B2-O live executor. Current code rejects `transport=None` for live cells.
  `execute_update_v2()` is a compatibility alias and must not be called.
  `run_update_batch()` and `run_update()` are legacy entrypoints and must not
  be called.
- `packages/data-core/catalyst_data/index_builder.py`: existing
  `build_corpus_and_lexical_index(conn,
  certified_snapshot_identity=snapshot_id, clock=clock)` is the sole B2-O
  corpus/lexical publication entrypoint.
- `packages/data-core/catalyst_data/corpus/manifest.py`: existing `build_manifest()`, `compute_manifest_id()`, and `certified_snapshot_identity`.
- `packages/data-core/catalyst_data/retrieval/fts5_builder.py`: existing `build_fts5_index()` and `LexicalIndexBuildResult`; B4 persists `lexical_index_state`.
- `packages/data-core/catalyst_data/coverage_audit.py`: existing
  `run_coverage_audit()` plus the B2-O adapter
  `run_b2o_readiness_audit()`. The adapter calls the existing audit exactly
  once and adds readiness fields.
- `packages/data-core/tests/test_coverage_audit.py` and `packages/data-core/tests/db_fixtures.py`: existing tests/helpers, not new files.

Do not create a root-level `catalyst_data` universe module. The only B2-O manifest ownership package is `packages/data-core/catalyst_data/manifests/`.

## B2-O-I: Implementation And Offline Tests

Tasks 1-9 follow RED -> GREEN -> focused regression. Collection errors,
missing imports, and undefined fixtures are not valid RED. Task 0 is a
characterization baseline: it records existing contracts before production
changes and must pass before Task 1 begins.

## Binding Module And API Contract

Create exactly these B2-O-owned files:

- `packages/data-core/catalyst_data/manifests/__init__.py`
- `packages/data-core/catalyst_data/manifests/universe.py`
- `packages/data-core/catalyst_data/manifests/snapshot.py`
- `packages/data-core/catalyst_data/manifests/operations.py`
- `packages/data-core/catalyst_data/manifests/universe_v1_2025_08.spec.json`
- `packages/data-core/catalyst_data/b2o.py`
- `packages/data-core/tests/test_b2o_data_readiness.py`

The public functions and signatures are binding:

```python
load_universe_spec(path: Path) -> UniverseSpec
build_universe_manifest(
    spec: UniverseSpec,
    *,
    discovery_artifact_hashes: Mapping[str, str],
    observed_provider_capabilities: Mapping[str, object],
    source_windows: SourceWindows,
    coverage_status_summary: Mapping[str, object],
    approved_at: datetime,
    created_at: datetime,
) -> UniverseManifest
build_data_snapshot_manifest(
    conn: sqlite3.Connection,
    *,
    universe_manifest_id: str,
    plan_hash: str,
    protected_source_sha256: str,
    source_windows: SourceWindows,
    created_at: datetime,
) -> DataSnapshotManifest
bootstrap_candidate(
    *,
    source_path: Path,
    expected_source_sha256: str,
    candidate_path: Path,
) -> BootstrapResult
promote_candidate(
    *,
    candidate_path: Path,
    snapshot_id: str,
    snapshots_dir: Path,
    active_pointer_path: Path,
) -> PromotionResult
create_b2o_live_transport(
    *,
    env_path: Path,
    rate_policies: Mapping[str, RatePolicy],
    request_caps: Mapping[str, int],
) -> AsyncContextManager[B2OLiveTransport]
```

The B2 executor extension is also binding:

```python
execute_update(
    *,
    db: sqlite3.Connection,
    plan: UpdatePlan,
    transport: object,
    parent_run_id: str | None = None,
) -> dict
```

`catalyst_data.b2o` is the only operational facade. It may delegate to the
manifest modules and existing B2/B3/B4 production functions; it must not
duplicate their logic.

Its subcommands are exactly `manifest`, `bootstrap`, `plan`, `execute`,
`audit`, `snapshot`, `publish-corpus`, and `promote`. Every command writes one
JSON envelope to stdout:

```json
{"schema_version":"1.0.0","command":"plan","status":"SUCCEEDED","data":{},"errors":[]}
```

Exit codes are: `0` success, `2` usage or validation, `3` protected-state
violation, `4` plan drift or identity conflict, `5` incomplete coverage,
`6` provider execution partial/failed, and `7` publication/promotion failure.
Secret values, response bodies, and authorization headers never appear in the
envelope.

Required CLI arguments are binding:

- `manifest --spec <tracked-spec> --discovery-artifact <path>
  --output <universe-manifest-path>`; the
  discovery flag is repeatable, each artifact is credential-scanned and
  copied by content hash into `data/provider_discovery/b2o/`;
- `bootstrap --source-db <protected-dev-db>
  --expected-source-sha256 <sha256> --bootstrap-db <path>`;
- `plan --db <bootstrap-db> --universe-manifest <path>
  --latest-complete-session <YYYY-MM-DD> --output <plan-path>`;
- `execute --db <working-db> --plan <path>
  --expected-plan-hash <sha256> --authorized
  [--parent-run-id <run-id>]`;
- `audit --db <working-db> --universe-manifest <path> --output-dir <path>`;
- `snapshot --db <working-db> --universe-manifest <path> --plan <path>
  --protected-source-sha256 <sha256> --output <snapshot-manifest-path>`;
- `publish-corpus --db <candidate-db> --snapshot-manifest <path>`;
- `promote --db <candidate-db> --snapshot-manifest <path>`.

`manifest`, `plan`, and `audit` are read-only with respect to SQLite.
`bootstrap` performs local filesystem/SQLite writes but no network.
`execute` is the only network-enabled command and requires both `--authorized`
and an exact `--expected-plan-hash`. `snapshot` writes only the manifest and
the working-to-candidate rename. `publish-corpus` writes derived B3/B4 state
to the candidate DB. `promote` performs only final filesystem publication and
active-pointer writes.

`create_b2o_live_transport()` loads
`packages/data-core/.env` through `python-dotenv` without overwriting
already-set process environment variables. It rejects missing credentials for
mandatory providers before the first request. It dispatches OHLCV, Finnhub,
SEC, FRED, and FMP cells to the existing connector factories. Polygon news
pagination uses one shared `httpx.AsyncClient`, preserves each exact
`next_url`, adds the Polygon key only to the outbound request, and applies the
existing Polygon limiter/retry policy. The key and credential-bearing URL are
never logged or persisted. The async context manager closes every shared
client.

### Task 0: Characterization Baseline

Production files:

- `packages/data-core/catalyst_data/migrations.py`
- `packages/data-core/catalyst_data/update_planner.py`
- `packages/data-core/catalyst_data/update_pipeline.py`
- `packages/data-core/catalyst_data/index_builder.py`
- `packages/data-core/catalyst_data/corpus/manifest.py`
- `packages/data-core/catalyst_data/retrieval/fts5_builder.py`
- `packages/data-core/catalyst_data/coverage_audit.py`

Characterization tests in
`packages/data-core/tests/test_b2o_data_readiness.py`:

- `test_existing_b2o_entrypoints_match_binding_contract` asserts the migration registry through v12
  exist, `run_migrations()` is callable, `plan_update()` exists,
  `execute_update()` has keyword-only `db`, `plan`, and `transport`, and
  `build_corpus_and_lexical_index()` accepts
  `certified_snapshot_identity` and `clock`.
- `test_combined_builder_returns_existing_result_types` uses a migrated temp
  DB and asserts the combined builder returns the existing corpus result and
  `LexicalIndexBuildResult`.

Implementation:

- Add only the characterization test and any imports it needs.
- Do not add migration DDL.

Focused regression:

- Assert no B2-O code imports `packages/eval`.
- Assert no new `LexicalIndexManifest` type exists.

### Task 1: Tracked UniverseSpec

Production files:

- new `packages/data-core/catalyst_data/manifests/__init__.py`
- new `packages/data-core/catalyst_data/manifests/universe.py`
- new `packages/data-core/catalyst_data/manifests/universe_v1_2025_08.spec.json`
- `packages/data-core/tests/test_b2o_data_readiness.py`

RED tests:

- `test_tracked_universe_spec_contains_exact_ratified_order` fails until the
  file contains exactly the ratified 40 tickers:
  `AAPL`, `AMD`, `AMZN`, `GOOGL`, `JPM`, `META`, `MSFT`, `NVDA`, `TSLA`, `UNH`, `INTC`, `QCOM`, `TSM`, `MU`, `LRCX`, `ASML`, `DELL`, `HPQ`, `CRM`, `ADBE`, `NOW`, `ORCL`, `PINS`, `RDDT`, `SNAP`, `WMT`, `TGT`, `COST`, `DASH`, `F`, `LCID`, `GM`, `RIVN`, `C`, `GS`, `BAC`, `MS`, `CNC`, `HUM`, `CI`.
- `test_universe_spec_rejects_each_missing_or_drifting_field` parametrizes
  duplicate tickers, invalid or missing CIKs, missing legal name, sector,
  peer group, issuer class, filing form profile, source-policy expectations,
  and semantic-hash drift.

GREEN:

- Implement `UniverseSpec` parsing, validation, and canonical semantic hash in `catalyst_data.manifests.universe`.
- Implement the binding
  `load_universe_spec(path: Path) -> UniverseSpec`.
- Use canonical JSON: UTF-8, sorted keys, compact separators, deterministic arrays, lowercase SHA-256 hex.
- Exclude no spec field from `semantic_hash` except the `semantic_hash` field itself.

Focused regression:

- Reordering object keys does not change the hash.
- Reordering ticker arrays does change the hash unless the contract declares the sorted order.
- A clean clone can load the approved universe without runtime discovery artifacts.

### Task 2: Runtime UniverseManifest

Production files:

- `packages/data-core/catalyst_data/manifests/universe.py`
- `packages/data-core/tests/test_b2o_data_readiness.py`

RED tests:

- `test_runtime_universe_manifest_requires_all_identity_inputs` covers the
  tracked spec hash, discovery artifact hashes, observed provider
  capabilities, canonical/degraded windows, `approved_at`, and `created_at`.
- `test_runtime_universe_manifest_identity_is_deterministic` asserts a
  deterministic `runtime_manifest_id`.

GREEN:

- Implement runtime manifest generation from `UniverseSpec` plus provided discovery/coverage inputs.
- Implement the binding `build_universe_manifest(...) -> UniverseManifest`.
- Store generated artifacts under `data/manifests/` during operational execution, not in the committed tree.
- Exclude `approved_at` and `created_at` from semantic identity.

Focused regression:

- Different discovery artifact hashes change `runtime_manifest_id`.
- Different timestamps do not change `runtime_manifest_id`.

### Task 3: Source Windows And Coverage Cell Status

Production files:

- `packages/data-core/catalyst_data/manifests/universe.py`
- `packages/data-core/catalyst_data/update_planner.py`
- `packages/data-core/catalyst_data/coverage_audit.py`
- `packages/data-core/tests/test_b2o_data_readiness.py`
- existing `packages/data-core/tests/test_coverage_audit.py`

RED tests:

- `test_source_windows_reject_invalid_ordering` enforces
  `degraded.start <= degraded.end < canonical.start <= canonical.end`.
- `test_source_windows_use_ratified_boundaries` fixes degraded to
  `2025-01-02` through `2025-07-31` and canonical start to `2025-08-01`;
  canonical end is the injected latest complete session.
- `test_coverage_statuses_remain_distinct` covers provider capability,
  completed cell, `SUCCESS_EMPTY`, `PARTIAL`, unavailable,
  subscription-required, and `unresolved_rate_limited`.
- `test_b2o_source_scopes_use_correct_subject_and_calendar` asserts OHLCV
  90-day bounded windows, Polygon/Finnhub 7-day bounded calendar windows
  including weekends, one SEC/FMP as-of cell per ticker, and one FRED as-of
  cell per configured series ID. It rejects unbounded per-day request planning.
- `test_legacy_plan_update_behavior_is_unchanged_without_source_scopes`
  protects existing callers.
- `test_resume_uses_same_plan_lineage_and_skips_only_complete_cells` covers
  `success`, `success_empty`, partial, failed, rate-limited, and
  resource-stopped cells.
- `test_resume_rejects_plan_hash_or_expected_hash_drift` runs before any
  write or network call.

GREEN:

- Implement shared status normalization for B2-O coverage readiness.
- Add the optional hash-bearing
  `source_scopes: Mapping[str, SourceScope] | None = None` parameter to
  `plan_update()`. Store its canonical representation in `UpdatePlan.config`.
  When present, generate exactly the provider-native bounded windows in design
  section 9; when absent, preserve existing planner behavior byte-for-byte
  apart from the added default parameter.
- Add optional `parent_run_id` to `execute_update()`. A resume creates a child
  run, validates the full lineage hashes, and filters only lineage
  terminal-complete cells. Persist the direct parent ID in
  `ingestion_runs.parent_run_id`.
- Add `run_b2o_readiness_audit()` to
  `catalyst_data.coverage_audit`; it calls `run_coverage_audit()` exactly once.
- Treat `SUCCESS_EMPTY` as terminal complete.
- Treat `PARTIAL`, unavailable, subscription-required without accepted waiver, and `unresolved_rate_limited` as incomplete.

Focused regression:

- A canonical comparable gate fails until Polygon and Finnhub cells have terminal complete outcomes for all 40 tickers.

### Task 4: DataSnapshotManifest

Production files:

- new `packages/data-core/catalyst_data/manifests/snapshot.py`
- `packages/data-core/tests/test_b2o_data_readiness.py`

RED tests:

- `test_snapshot_id_changes_for_every_identity_field` parametrizes
  `schema_version`, `universe_manifest_id`, B2 `plan_hash`, DB
  `user_version`, logical table inventory, source checkpoints, coverage
  states, per-table logical hashes, protected source DB identity, and data
  window identities.
- `test_snapshot_id_ignores_runtime_metadata` covers `snapshot_id`,
  `created_at`, report path, runtime latency, and candidate/final paths.
- `test_snapshot_inventory_matches_design_exactly` asserts the table,
  projection, key, and included-column inventory from design section 7.

GREEN:

- Implement `DataSnapshotManifest` with deterministic `snapshot_id`.
- Implement the binding `build_data_snapshot_manifest(...)`.
- Implement per-table logical hashing by streaming rows ordered by declared primary-key columns.
- Include table name and ordered column list.
- Encode SQLite `NULL`, `BLOB`, `INTEGER`, `REAL`, and `TEXT` explicitly.
- Use exactly the typed encodings and newline-delimited hash framing in design
  section 7. Reject non-finite `REAL` values and absent required tables.

Focused regression:

- Changing a column type without changing its displayed value changes the table hash.
- `DataSnapshotManifest.snapshot_id` is passed to existing B3 `certified_snapshot_identity`.
- Existing B3 `compute_manifest_id()` remains the only corpus manifest identity function.

### Task 5: Working DB Bootstrap

Production files:

- new `packages/data-core/catalyst_data/manifests/operations.py`
- `packages/data-core/tests/test_b2o_data_readiness.py`

RED tests:

- `test_bootstrap_uses_readonly_sqlite_backup_and_migrates_to_current_schema` verifies a
  URI `mode=ro` source connection, `sqlite3.Connection.backup()`, fsync,
  existing `run_migrations()`, and `PRAGMA user_version = 12`.
- `test_bootstrap_rejects_source_hash_drift` verifies the binding
  `expected_source_sha256`.
- `test_bootstrap_failure_removes_only_incomplete_candidate` injects backup,
  migration, integrity, and fsync failures.

GREEN:

- Implement sequence:
  1. record protected Dev DB SHA;
  2. open Dev DB read-only;
  3. create candidate snapshot;
  4. fsync candidate file and parent;
  5. call existing `run_migrations(conn)`;
  6. verify `PRAGMA user_version = 12`;
  7. run `PRAGMA integrity_check`;
  8. run `PRAGMA foreign_key_check`;
  9. verify protected Dev DB SHA unchanged;
  10. retain all historical rows.
- Implement only the binding `bootstrap_candidate(...) -> BootstrapResult`;
  do not create a second migration path.
- Before bootstrap, require free disk of at least
  `max(10 GiB, 3 * protected_dev_db_size)`.

Focused regression:

- Rows before `2025-01-02` remain stored after bootstrap.
- Query/window policy excludes pre-window rows without deleting them.
- Bootstrap/migration failure deletes only the incomplete candidate.
- `test_bootstrap_resource_preflight_is_binding` covers insufficient disk.

### Task 6: Coverage Audit Extension

Production files:

- `packages/data-core/catalyst_data/coverage_audit.py`
- `packages/data-core/tests/test_b2o_data_readiness.py`
- existing `packages/data-core/tests/test_coverage_audit.py`

RED tests:

- `test_b2o_audit_rejects_each_incomplete_cell_state` parametrizes missing,
  partial, unresolved-rate-limited, subscription-required without waiver,
  and missing provenance.
- `test_b2o_comparable_gate_requires_both_news_sources_for_all_40` requires
  terminal-complete Polygon and Finnhub canonical cells for every ticker.
- `test_b2o_audit_calls_existing_audit_once` prevents a second audit engine.

GREEN:

- Implement
  `run_b2o_readiness_audit(db_path, *, universe_manifest, output_dir=None)`;
  call existing `run_coverage_audit(db_path, output_dir=None)` exactly once,
  preserve its nine dimensions, and add B2-O readiness fields.
- Preserve read-only behavior for audit mode.
- Preserve existing audit dimensions and tests.

Focused regression:

- `SUCCESS_EMPTY` counts as complete but remains visible in the report.
- Existing Step 3F coverage tests continue to pass.

### Task 7: B3/B4 Publication Wiring

Production files:

- `packages/data-core/catalyst_data/index_builder.py`
- `packages/data-core/catalyst_data/corpus/manifest.py`
- `packages/data-core/catalyst_data/retrieval/fts5_builder.py`
- `packages/data-core/tests/test_b2o_data_readiness.py`

RED tests:

- `test_combined_publication_binds_snapshot_to_corpus_and_lexical_state`
  verifies the exact snapshot ID, corpus ID, and lexical state.
- `test_combined_publication_failure_preserves_previous_current_state`
  injects corpus and FTS failures.
- `test_b2o_has_no_alternate_manifest_or_publication_path` statically rejects
  a second corpus identity formula, `LexicalIndexManifest`, direct
  `build_corpus()`, or direct `build_fts5_index()` call from B2-O code.

GREEN:

- Call only
  `build_corpus_and_lexical_index(conn,
  certified_snapshot_identity=snapshot_id, clock=clock)`.
- The existing combined entrypoint remains responsible for
  `compute_manifest_id()`, `build_fts5_index()`, and
  `lexical_index_state`.
- Before publication, require the same disk preflight as Task 5. Sample RSS
  before each producer batch and stop cooperatively at 6 GiB. Producer
  batches contain at most 100 source documents and FTS write transactions
  contain at most 500 chunks.

Focused regression:

- Corpus and FTS publication are atomic: previous current manifest/index remain served if the candidate build fails.
- Resource-stop tests prove the candidate DB remains resumable and the
  previous current corpus/lexical state remains selected.

### Task 8: Operational Facade, Promotion, And Recovery

Production files:

- `packages/data-core/catalyst_data/manifests/operations.py`
- new `packages/data-core/catalyst_data/b2o.py`
- `packages/data-core/tests/test_b2o_data_readiness.py`

RED tests:

- `test_promotion_never_overwrites_versioned_snapshot`
- `test_promotion_requires_file_and_directory_fsync`
- `test_interrupted_promotion_preserves_previous_pointer`
- `test_active_pointer_is_not_an_implicit_db_resolver`
- `test_cli_contract_has_exact_commands_envelope_and_exit_codes`
- `test_live_transport_preserves_polygon_next_url_and_redacts_api_key`
- `test_live_transport_dispatches_to_existing_connector_factories`
- `test_live_transport_closes_clients_and_rejects_missing_mandatory_keys`

GREEN:

- Promote from `data/candidates/catalyst_b2o_<snapshot_id>.candidate.db` to `data/snapshots/catalyst_b2o_<snapshot_id>.db`.
- Write `data/manifests/active_data_snapshot.json` atomically after the final DB exists and is fsynced.
- Implement the binding `promote_candidate(...) -> PromotionResult`.
- Implement only the binding `create_b2o_live_transport(...)` for live
  transport construction. Do not copy provider normalization or persistence
  logic into the facade.
- Print the absolute promoted path in the JSON envelope. The active pointer is
  audit metadata only. Subsequent processes must receive
  `CATALYST_DB_PATH=<absolute-promoted-path>` explicitly.

Focused regression:

- Live-fill interruption retains working DB, request ledger, raw assets, provenance, checkpoints, and ingestion run rows.
- Resume uses B2 run-control and does not restart bootstrap.
- A child CLI process never claims to mutate its parent shell environment.

### Task 9: B2-O-I Integration Gate

Production files:

- only the binding files and existing production files listed in Tasks 0-8

RED tests:

- `test_b2o_offline_flow_runs_end_to_end_without_network` fails until all
  offline contracts run against temp DB fixtures.
- `test_b2o_offline_flow_respects_resource_and_protected_db_guards` covers
  disk, RSS, protected SHA, and interruption paths.

GREEN:

- Execute the offline B2-O-I flow with fake/disconnected provider surfaces:
  UniverseSpec load -> Runtime UniverseManifest generation -> candidate
  bootstrap fixture -> existing migrations -> snapshot manifest -> coverage
  readiness classification -> combined B3/B4 publication -> promotion
  simulation.

Focused regression:

- Socket/network guard proves no provider, model, Hugging Face, or external network calls occur.
- Protected DB fixtures remain unchanged.
- No code imports `packages/eval`.

B2-O-I may report only `B2-O-I COMPLETE`. B2-O remains incomplete until B2-O-X finishes.

## Authorization Checkpoint

Stop here unless the operator explicitly authorizes live B2-O-X execution and provider credentials are present in the approved local environment. Do not read or print secrets.

Before B2-O-X:

- record HEAD and scoped git status;
- record protected Dev and Eval DB SHA-256;
- verify the tracked spec matches the architect-ratified 40-ticker identity;
- run the offline verification ladder;
- generate a zero-write B2 plan preview and record `plan_hash`;
- require the operator to confirm the expected plan hash before live execution.

## B2-O-X: Authorized Operational Execution

B2-O-X tasks are explicit executable tasks in this plan.

### Task X1: Ratify Universe And Runtime Manifest

- Confirm the tracked `UniverseSpec` is architect-ratified.
- Run `catalyst_data.b2o manifest` to generate the runtime
  `UniverseManifest` under `data/manifests/`.
- Record `UniverseSpec.semantic_hash` and `UniverseManifest.runtime_manifest_id`.

Evidence:

- 40 unique tickers;
- relationship manifest hash present;
- sorted relationships, relationship types, and validity windows verified;
- runtime manifest timestamps excluded from identity.

### Task X2: Bootstrap Candidate Working DB

- Verify protected Dev DB SHA.
- Open protected Dev DB read-only.
- Create candidate through SQLite backup.
- fsync file and parent directory.
- Run existing `run_migrations(conn)`.
- Verify `PRAGMA user_version = 12`, `integrity_check`, and `foreign_key_check`.
- Verify corpus publication does not change any `DataSnapshotManifest` source
  table hash and that `idx_index_state_chunk_id` exists before chunk upserts.
- Verify protected Dev DB SHA unchanged.
- Use the pre-snapshot path
  `data/candidates/catalyst_b2o_<universe_manifest_id>_<plan_hash>.working.db`;
  Task X3 supplies the final plan hash, so `bootstrap` first writes a
  `.bootstrap.db` temporary file and `plan` atomically renames it to the
  binding working path after the preview hash is known.

Evidence:

- candidate DB path;
- before/after protected DB SHA;
- user_version 12;
- historical rows retained.

### Task X3: Plan Preview

- Run existing `plan_update()` against the candidate.
- Include the approved 40-ticker universe, source windows, and injected latest
  complete session.
- The immutable plan contains exactly:
  - Polygon adjusted daily OHLCV for all 40 tickers from `2025-01-02`
    through the latest complete session in the `market` stage;
  - Polygon ticker news for all 40 tickers across degraded and canonical
    windows in the `evidence` stage;
  - Finnhub company news for all 40 tickers from `2025-08-01` through the
    latest complete session in the `evidence` stage;
  - SEC submissions and filing-document metadata for all 40 tickers using
    each ticker's ratified form profile;
  - the existing curated 11 FRED series as mandatory global context;
  - FMP annual income, balance sheet, and cash-flow cells as optional
    supplemental evidence;
  - no yfinance batch cell and no FMP article cell.
- Construct the exact `SourceScope` objects from design section 9; do not pass
  a single shared date list to all sources.
- Record `UpdatePlan.plan_hash`.
- Do not write provider data in this step.

Evidence:

- plan hash;
- requested cells by provider/source/ticker/window;
- no drift between preview and expected hash.

### Task X4: Live Execution Authorization

- Operator confirms `expected_plan_hash`.
- Operator confirms provider use for B2-O-X only.
- Do not print secrets.

Evidence:

- authorization timestamp;
- expected plan hash;
- provider list without secret values.

### Task X5: Execute And Resume B2 Fill

- Call only
  `await execute_update(db=conn, plan=plan, transport=live_transport,
  parent_run_id=parent_run_id)`, where a root execution passes `None` and a
  resume passes the explicitly selected prior run ID.
- Construct `live_transport` only through
  `create_b2o_live_transport(...)` inside an async context manager.
- Enforce plan-drift checks.
- Persist request ledger, raw assets, normalized provenance, source checkpoints, and ingestion run status.
- If interrupted, resume using run-control. Do not restart bootstrap solely because the live run is partial.
- Preserve the executor's binding stage order: complete the Polygon OHLCV
  `market` stage, record the resulting watermark, then enter `evidence`.
- News backfill begins at the first Polygon/Finnhub news cell in the
  `evidence` stage. No task before this point may issue a news request.
- Process one provider response page at a time. Between cells, stop
  cooperatively if RSS is at least 6 GiB and retain resumable state.
- Provider `429`, retry exhaustion, authorization failure, and transport
  interruption remain explicit cell/run outcomes; they are never converted to
  `SUCCESS_EMPTY`.

Evidence:

- run IDs;
- market-stage terminal status and OHLCV watermark before the first news
  request;
- first and last attempted news cell by provider/window, without response
  bodies or credentials;
- terminal run statuses;
- success, `SUCCESS_EMPTY`, partial, failed, and waived cells visible as separate states.

### Task X6: Coverage And Comparable Gate

- Run B2-O readiness coverage audit.
- Verify canonical Polygon and Finnhub cells have terminal complete outcomes for all 40 tickers.
- Treat `SUCCESS_EMPTY` as complete.
- Treat `PARTIAL`, unavailable, subscription-required without waiver, and `unresolved_rate_limited` as incomplete.

Evidence:

- coverage report path;
- complete/incomplete counts by source/window/status;
- canonical comparable gate result.

### Task X7: Snapshot, Corpus, And Lexical Build

- Build `DataSnapshotManifest` from the candidate.
- Atomically rename the verified working DB to
  `data/candidates/catalyst_b2o_<snapshot_id>.candidate.db` and fsync the
  candidates directory.
- Pass `snapshot_id` as B3 `certified_snapshot_identity`.
- Call only
  `build_corpus_and_lexical_index(conn,
  certified_snapshot_identity=snapshot_id, clock=clock)`.
- Enforce the Task 7 disk, RSS, producer-batch, and FTS-transaction resource
  bounds.

Evidence:

- `DataSnapshotManifest.snapshot_id`;
- B3 corpus manifest ID from existing `compute_manifest_id()`;
- B4 `LexicalIndexBuildResult`;
- `lexical_index_state` row for the exact current corpus manifest.

### Task X8: Promote

- Promote candidate to `data/snapshots/catalyst_b2o_<snapshot_id>.db`.
- Never overwrite an existing promoted snapshot.
- fsync file and parent directory.
- Atomically write `data/manifests/active_data_snapshot.json`.
- Print the shell-safe absolute final DB path and record it in the evidence
  report. The operator or supervisor must explicitly provide
  `CATALYST_DB_PATH=<absolute-final-path>` to later processes.

Evidence:

- final DB path;
- active pointer path;
- exact absolute value required for `CATALYST_DB_PATH`;
- previous active pointer preserved on simulated failure;
- promoted DB SHA.

### Task X9: Final Evidence Report

Report `B2-O COMPLETE` only if all B2-O-I and B2-O-X gates pass.

Required evidence:

- Task 0-9 and X1-X9 matrix;
- production files, RED test, GREEN test, and focused regression for each task;
- exact test counts and commands;
- UniverseSpec and UniverseManifest identities;
- DataSnapshotManifest identity;
- B2 plan hash;
- source-window and canonical comparable gate results;
- B3 corpus manifest ID and B4 lexical state;
- protected DB SHA before and after;
- promoted DB SHA;
- git status;
- empty staged diff;
- confirmation no commit, push, PR, GPU server access, provider requests outside B2-O-X, or B6 start occurred.

## Verification Ladder

B2-O-I offline verification:

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_b2o_data_readiness.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_coverage_audit.py -q
.venv/bin/python -m pytest packages/data-core -q
HF_HUB_OFFLINE=1 .venv/bin/python -m pytest packages/data-core -q
git diff --check
git diff --cached --name-status
shasum -a 256 data/catalyst_dev_ws4b.db data/catalyst_eval_frozen_v2.db
```

B2-O-X operational verification after authorization:

```bash
shasum -a 256 data/catalyst_dev_ws4b.db data/catalyst_eval_frozen_v2.db
.venv/bin/python -m catalyst_data.b2o manifest \
  --spec packages/data-core/catalyst_data/manifests/universe_v1_2025_08.spec.json \
  --discovery-artifact /tmp/catalyst-provider-coverage-round2/provider_coverage_round2.json \
  --discovery-artifact /tmp/catalyst-provider-coverage-round2/candidate_evidence_matrix.json \
  --output data/manifests/universe_v1_2025_08.runtime.json
.venv/bin/python -m catalyst_data.b2o bootstrap \
  --source-db data/catalyst_dev_ws4b.db \
  --expected-source-sha256 92731fb7c5c3e989b4fdcbefb9d1d2060974082a6c60f1bbecad6bc42ee846b0 \
  --bootstrap-db data/candidates/catalyst_b2o.bootstrap.db
.venv/bin/python -m catalyst_data.b2o plan \
  --db data/candidates/catalyst_b2o.bootstrap.db \
  --universe-manifest data/manifests/universe_v1_2025_08.runtime.json \
  --latest-complete-session YYYY-MM-DD \
  --output data/manifests/b2o_update_plan.json
.venv/bin/python -m catalyst_data.b2o execute \
  --db WORKING_DB_FROM_PLAN_ENVELOPE \
  --plan data/manifests/b2o_update_plan.json \
  --expected-plan-hash PLAN_HASH_FROM_PLAN_ENVELOPE \
  --authorized
.venv/bin/python -m catalyst_data.b2o audit \
  --db WORKING_DB_FROM_PLAN_ENVELOPE \
  --universe-manifest data/manifests/universe_v1_2025_08.runtime.json \
  --output-dir data/provider_discovery/b2o
.venv/bin/python -m catalyst_data.b2o snapshot \
  --db WORKING_DB_FROM_PLAN_ENVELOPE \
  --universe-manifest data/manifests/universe_v1_2025_08.runtime.json \
  --plan data/manifests/b2o_update_plan.json \
  --protected-source-sha256 92731fb7c5c3e989b4fdcbefb9d1d2060974082a6c60f1bbecad6bc42ee846b0 \
  --output data/manifests/b2o_data_snapshot.json
.venv/bin/python -m catalyst_data.b2o publish-corpus \
  --db CANDIDATE_DB_FROM_SNAPSHOT_ENVELOPE \
  --snapshot-manifest data/manifests/b2o_data_snapshot.json
.venv/bin/python -m catalyst_data.b2o promote \
  --db CANDIDATE_DB_FROM_SNAPSHOT_ENVELOPE \
  --snapshot-manifest data/manifests/b2o_data_snapshot.json
shasum -a 256 data/catalyst_dev_ws4b.db data/catalyst_eval_frozen_v2.db
git diff --check
git diff --cached --name-status
```

`YYYY-MM-DD` is replaced by the latest complete session computed immediately
before X3. The three uppercase values are copied exactly from the preceding
JSON envelopes; they are not inferred from filenames. No alternate CLI surface
is permitted by this plan.


## Operational Safety Amendment (2026-07-24 Round 2)

### Market Completeness Gate

Before entering evidence stage, all planned market cells must be terminal-complete in the current run + parent lineage. A market cell is complete when:
- checkpoint belongs to the selected lineage (run_id IN lineage)
- status in {success, success_empty}
- is_complete = 1
- endpoint_name, window_start, window_end match plan

If incomplete: stop_reason = market_stage_incomplete, status = PARTIAL (with success) or FAILED (without).

### Transport Circuit Breaker

Threshold: B2_TRANSPORT_CIRCUIT_THRESHOLD = 3 consecutive failures.

Only these terminal categories increment the counter:
- transport_error
- timeout

All other outcomes reset the counter to 0:
- success, success_empty
- HTTP_ERROR, AUTH_ERROR, RATE_LIMITED, PARSE_ERROR

When threshold reached: stop_reason = transport_circuit_open, STATUS = PARTIAL (with success) or FAILED (without). Remaining cells untouchted.

### Fatal Transport Configuration

ImportError and ModuleNotFoundError are fatal after one attempt:
- error_class = fatal_transport_configuration
- run = FAILED
- stop_reason = fatal_transport_configuration
- No further cells processed
- Error text is redacted

### Cooperative Cancellation

Executor polls is_cancelled(db, run_id) at:
- Each market cell before execution
- Market → evidence transition
- Each evidence cell before execution

Uses persisted cancel_requested flag in ingestion_runs. No signal required.

### Stop-State Priority

Fatal > Circuit > Operator Cancel > Runtime Budget > Market Incomplete

### Lineage-Bound Audit

All readiness audit and snapshot operations require --terminal-run-id.
Shared _resolve_b2_lineage() resolver validates:
- Terminal run exists
- All ancestors exist
- No cycles
- plan_hash matches at every level
- expected_plan_hash matches at every level

Returns ordered lineage list. Checkpoint queries filter by run_id IN (lineage).
Sibling runs cannot satisfy or break readiness.

### CLI Changes

- audit: --terminal-run-id required, --plan required
- snapshot: --terminal-run-id required
- Both use real UpdatePlan, not SimpleNamespace


### Polygon Transient Transport Retry (added 2026-07-25)

- Polygon transient transport/timeout maximum total attempts = 3
- Backoff = 5s then 10s (base_seconds=5.0, max_seconds=20.0, no jitter)
- Each attempt still passes the 12s Polygon rate limiter before the request
- Circuit increments only after one cell exhausts all 3 attempts
- Three consecutive exhausted cells open the transport circuit
- 401/403 never use this retry path (handled as terminal errors)
- 429/5xx retain their existing independent retry policies (rate_limit/server_error rules)

### Independent Retry Budgets (added 2026-07-25)

Per-page request counters are independent across error classes:
- `ledger_attempt_no` for sequential request IDs
- `transport_attempts` for transient transport exceptions only
- `rate_limit_attempts` for HTTP 429 only
- `server_error_attempts` for HTTP 5xx only
- Transport errors cannot exhaust rate limit budget, and vice versa

Only specific transient exception types (httpx.* transport errors, builtin ConnectionError/TimeoutError) trigger retry. Non-transient exceptions immediately fatal-stop.

Successful page data is preserved even when a later page in the same cell fails.

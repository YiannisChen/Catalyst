# B2-O Data Readiness Design

Date: 2026-07-23
Status: binding contract; 40-name universe ratified by the architect on 2026-07-23
Scope: data readiness only. No B6 dense/reranker work starts here.

## 1. Outcome

B2-O produces an operationally promoted Catalyst working data snapshot that is:

- bootstrapped from the protected Dev DB without mutating protected DBs;
- migrated by the existing production migration registry through `PRAGMA user_version = 12`;
- filled through the existing B2 planner/run-control/execution path after explicit live authorization;
- coverage-audited for the approved 40-ticker universe and the required source windows;
- frozen as a deterministic `DataSnapshotManifest`;
- passed into the existing combined B3/B4 publication entrypoint via
  `certified_snapshot_identity`, producing the B3 corpus and B4
  `lexical_index_state`;
- promoted only by atomic publication of a versioned candidate.

B2-O is split into two resumable phases:

- **B2-O-I**: implementation and offline tests. This phase may complete before live provider authorization. It must not call providers, mutate protected DBs, or claim B2-O completion.
- **B2-O-X**: authorized operational execution. This phase is part of the implementation plan and is required for B2-O completion. It performs the live fill, audit, snapshot, B3/B4 publication, and promotion.

## 2. Production Reuse Contract

B2-O reuses production modules instead of creating parallel systems.

- SQLite migrations: use existing `catalyst_data.migrations.run_migrations(conn)` through v12. v11 introduces B2-O full source-cell identity fields and `fundamental_statements`; v12 replaces the legacy composite checkpoint primary key with `checkpoint_id`, while enforcing `UNIQUE(run_id, cell_id)` so the three FMP endpoints can coexist. v8/v9/v10 remain unchanged.
- B2 planning: use existing `catalyst_data.update_planner.plan_update()` and `UpdatePlan.plan_hash`.
- B2 execution: use existing
  `catalyst_data.update_pipeline.execute_update(db=conn, plan=plan,
  transport=live_transport)` as the only authoritative B2-O-X executor.
  Current production code requires an injected transport for every live cell;
  `None` is not a live transport. The B2-O facade owns only the adapter that
  dispatches to existing connector factories and a Polygon pagination HTTP
  client. `execute_update_v2()` remains a compatibility alias and is not
  called by B2-O. `run_update_batch()` and `run_update()` remain legacy
  surfaces and are not B2-O entrypoints.
- B3/B4 publication: use existing `catalyst_data.index_builder.build_corpus_and_lexical_index(conn, certified_snapshot_identity=snapshot_id, clock=clock)` as the only B2-O publication entrypoint. It delegates to existing `build_corpus()`, `compute_manifest_id()`, and `build_fts5_index()` exactly once.
- B4 lexical: use existing `catalyst_data.retrieval.fts5_builder.build_fts5_index()` and its `LexicalIndexBuildResult`. B4 owns `lexical_index_state`; B2-O must not invent a `LexicalIndexManifest`.
- Coverage: add `run_b2o_readiness_audit()` to
  `catalyst_data.coverage_audit`. It must call existing
  `run_coverage_audit()` exactly once, then add B2-O cell-readiness and
  comparable-gate fields. It must not duplicate the existing nine audit
  dimensions.

Compatibility modules may delegate only. They must not own a second universe, snapshot, status, migration, coverage, corpus, or lexical implementation.

## 3. Universe Ownership

There is one ownership path for B2-O manifest code and tracked specs:

- code package: `packages/data-core/catalyst_data/manifests/`
- tracked approved universe spec: `packages/data-core/catalyst_data/manifests/universe_v1_2025_08.spec.json`
- runtime generated manifest artifacts: `data/manifests/`

Do not add a root-level `catalyst_data` universe module. Do not list existing `packages/data-core/tests/test_coverage_audit.py` or `packages/data-core/tests/db_fixtures.py` as new files.

## 4. Ratified Universe

The tracked `UniverseSpec` must contain exactly these 40 unique tickers. Any
change requires a later explicit design amendment and a new spec identity.

Existing 10:

`AAPL`, `AMD`, `AMZN`, `GOOGL`, `JPM`, `META`, `MSFT`, `NVDA`, `TSLA`, `UNH`

Recommended 30:

`INTC`, `QCOM`, `TSM`, `MU`, `LRCX`, `ASML`, `DELL`, `HPQ`, `CRM`, `ADBE`, `NOW`, `ORCL`, `PINS`, `RDDT`, `SNAP`, `WMT`, `TGT`, `COST`, `DASH`, `F`, `LCID`, `GM`, `RIVN`, `C`, `GS`, `BAC`, `MS`, `CNC`, `HUM`, `CI`

The spec must reject duplicates, missing identities, ticker drift, invalid CIKs, and unratified ticker changes.

## 5. UniverseSpec And UniverseManifest

`UniverseSpec` is committed to the repository. A clean clone must know the approved universe before any runtime discovery.

Required `UniverseSpec` fields:

- `schema_version`
- `universe_name`
- ordered `tickers`
- per-company identity: ticker, legal name, CIK, exchange/issuer class where known
- sector and peer group
- filing form profile
- static source-policy expectations
- `semantic_hash`

B2-O does not own B5/B7 attribution relationship manifests. UniverseSpec stores
company identity, CIK, issuer/form profile, sector, and peer-group labels only.

`UniverseManifest` is generated at runtime from the tracked spec and discovery/audit artifacts. It is stored untracked under `data/manifests/`.

Required `UniverseManifest` fields:

- `schema_version`
- `runtime_manifest_id`
- `universe_spec_semantic_hash`
- ordered approved tickers
- discovery artifact hashes
- observed provider capabilities
- source windows, including canonical and degraded identities
- coverage status summary
- `approved_at`
- `created_at`

`approved_at` and `created_at` are runtime metadata. They are included in the artifact body but excluded from semantic identity.

## 6. Canonical JSON And Identity

All B2-O semantic hashes use canonical JSON:

- UTF-8 bytes;
- sorted object keys;
- compact separators with no insignificant whitespace;
- deterministic array ordering declared by the owning contract;
- lowercase SHA-256 hex output.

Runtime timestamps, local paths, latency, report path, and operator-only notes are excluded from semantic identity unless explicitly listed as identity fields.

Identity chain:

1. `UniverseSpec.semantic_hash`
2. `UniverseManifest.runtime_manifest_id`
3. `UpdatePlan.plan_hash`
4. `DataSnapshotManifest.snapshot_id`
5. existing B3 corpus manifest ID from `compute_manifest_id()`, with `DataSnapshotManifest.snapshot_id` passed as `certified_snapshot_identity`
6. B4 `lexical_index_state` and `LexicalIndexBuildResult` for the exact current corpus manifest

B2-O must not invent a new corpus manifest formula. B2-O must not invent a lexical manifest type.

## 7. DataSnapshotManifest

`DataSnapshotManifest.snapshot_id` is the semantic identity of the promoted working DB snapshot.

Identity fields:

- manifest schema version;
- `universe_manifest_id`;
- B2 `plan_hash`;
- working DB `PRAGMA user_version`;
- logical table inventory;
- source checkpoints;
- coverage states;
- per-table logical content hashes;
- protected source DB identity for imported data;
- data window identities.

Excluded fields:

- `snapshot_id`;
- `created_at`;
- report path;
- runtime latency;
- local candidate path;
- local final path.

Per-table logical hash:

- stream SHA-256 over rows ordered by declared primary-key columns;
- include table name and ordered column list;
- each row encoded as canonical JSON;
- encode `NULL`, `BLOB`, `INTEGER`, `REAL`, and `TEXT` explicitly so type changes alter identity;
- do not exclude timestamps unless the table contract marks the column as runtime metadata.

The identity inventory is fixed. B2-O implementations may not add or remove a table without a design amendment:

| Table/projection | Ordered key | Identity treatment |
|---|---|---|
| `raw_assets` | `asset_id` | all columns |
| `clean_assets` | `asset_id` | all columns |
| `ohlcv` | `symbol`, `date` | all columns |
| `articles` | `article_id` | all columns |
| `article_tickers` | `article_id`, `ticker` | all columns |
| `filings` | `filing_id` | all columns |
| `filing_documents` | `filing_id`, `document_url` | all columns |
| `macro_observations` | `series_id`, `observation_date` | all columns |
| `normalized_provenance` | `entity_type`, `entity_id`, `entity_version`, `raw_asset_id` | all columns |
| source-checkpoint projection | `run_id`, `source_type`, `ticker`, `date` | `run_id`, `source_type`, `ticker`, `date`, `status`, `empty_reason`, `logical_fetch_id`, `request_count`, `pages_received`, `items_received`, `is_complete`, `raw_asset_id`, `items_count`, `http_status` |
| ingestion-run projection | `run_id` | `run_id`, `plan_hash`, `expected_plan_hash`, `parent_run_id`, `ticker_list_json`, `source_list_json`, `status`, `success_count`, `fail_count`, `cancel_requested` |

Corpus, FTS, vector, trace, assurance, and UI tables are derived downstream and are excluded from `DataSnapshotManifest.snapshot_id`. In particular, `corpus_chunks`, `corpus_tombstones`, `corpus_manifest`, `corpus_chunks_fts`, `lexical_index_state`, and `index_state` are not snapshot input tables.

Typed values use these canonical encodings before row JSON serialization:

- `NULL`: `{"type":"null"}`
- `BLOB`: `{"type":"blob","base64":"<RFC4648-base64>"}`
- `INTEGER`: `{"type":"integer","decimal":"<base-10>"}`
- `REAL`: `{"type":"real","ieee754_hex":"<big-endian-binary64-hex>"}`; non-finite values are rejected
- `TEXT`: `{"type":"text","utf8":"<exact-text>"}` with no Unicode normalization

Each table hash starts with canonical JSON containing the table/projection name, ordered key columns, and ordered included columns, followed by one newline-delimited canonical row object per ordered row. An absent required table is a contract error, not an empty table.

The snapshot ID is passed directly to B3 as `certified_snapshot_identity`.

## 8. Source Windows And Completion Status

Required windows:

- degraded start: `2025-01-02`
- degraded end: `2025-07-31`
- canonical start: `2025-08-01`
- canonical end: latest complete trading session at B2-O-X execution time

Required relation:

`degraded.start <= degraded.end < canonical.start <= canonical.end`

Historical rows before `2025-01-02` remain stored in the working DB if present from the protected Dev DB snapshot. They are excluded by query/window policy, not deleted.

Coverage must distinguish:

- provider query capability;
- completed ingestion cell;
- `SUCCESS_EMPTY`;
- `PARTIAL`;
- unavailable;
- subscription required;
- `unresolved_rate_limited`.

A canonical cell is comparable only after B2-O-X verifies that Polygon and Finnhub cells have terminal complete outcomes for all 40 tickers across the canonical window. `SUCCESS_EMPTY` is terminal complete. `PARTIAL`, unavailable, subscription-required without accepted waiver, and `unresolved_rate_limited` are not complete.

## 9. Provider And Data-Type Contract

The B2-O-X plan contains these exact source obligations:

| Source | Data type | Scope | Readiness |
|---|---|---|---|
| Polygon | adjusted daily OHLCV | all 40 tickers, `2025-01-02` through latest complete session | mandatory |
| Polygon | ticker news | all 40 tickers, degraded and canonical windows | mandatory |
| Finnhub | company news | all 40 tickers, canonical window | mandatory |
| SEC | submissions and filing-document metadata | all 40 tickers, per-ticker available history | mandatory |
| FRED | the existing curated 11-series manifest | series-specific history | mandatory global context |
| FMP | annual income, balance sheet, and cash flow | per-ticker when the current account permits | optional supplemental data |
| yfinance | diagnostic OHLCV fallback | operator diagnosis only | excluded from batch readiness |

FMP `402`, `429`, or empty responses never block B2-O readiness. They remain explicit unavailable/degraded supplemental coverage. FMP Articles, provider-plan upgrades, and new providers are out of scope.

Domestic issuer form profiles contain `10-K`, `10-Q`, and `8-K`. Foreign issuer profiles are ticker-specific and may contain `20-F`, `40-F`, and `6-K`; B2-O does not infer a form profile solely from a domestic/foreign boolean.

The B2 executor's stage order is binding:

1. `market`: Polygon adjusted daily OHLCV for all planned ticker/session cells;
2. recompute and record the latest OHLCV watermark;
3. `evidence`: Polygon news, Finnhub company news, SEC, FRED, and optional FMP
   cells from the same immutable plan.

Therefore news backfill starts only in B2-O-X Task X5, after the X5 market
stage has reached its terminal outcome. B2-O-I, X1-X4, bootstrap, and plan
preview perform no news request.

The B2-O plan uses source-specific cell domains. The existing
`plan_update()` must be extended with an optional, hash-bearing
`source_scopes` argument; callers that omit it retain existing behavior.
B2-O always supplies it:

| Source type | Subject | Request/cell windows |
|---|---|---|
| `polygon_ohlcv` | each approved ticker | inclusive, continuous, non-overlapping windows of at most 90 natural days from `2025-01-02` through canonical end; each provider request returns all exchange-session bars in the window |
| `polygon_news` | each approved ticker | inclusive, continuous, non-overlapping calendar windows of at most 7 natural days from `2025-01-02` through canonical end; pagination continues until `next_url` is empty or an explicit cap is reached |
| `finnhub_company_news` | each approved ticker | inclusive, continuous, non-overlapping calendar windows of at most 7 natural days from `2025-08-01` through canonical end |
| `sec_filings` | each approved ticker | one as-of cell at canonical end; the connector retrieves available submissions history under the ticker's form profile |
| `fmp_fundamentals` | each approved ticker | three as-of cells at canonical end: `income_statement`, `balance_sheet`, and `cash_flow`; FMP is degraded optional |
| `fred_macro` | each ID in the existing 11-series manifest | one as-of cell at canonical end; the connector retrieves the configured series history |

No source may inherit another source's calendar or subject domain. In
particular, news includes weekends, and FRED cells use series IDs rather than
equity tickers.

`SourceScope` has exactly these identity fields:
`source_type`, ordered `subjects`, `date_domain` (`trading_sessions`,
`calendar_days`, or `as_of`), `start_date`, `end_date`, `stage`,
`request_window_days`, `provider_profile_version`, `page_cap`, `item_cap`,
`request_cap`, and ordered `endpoint_names`. For `as_of`,
`start_date == end_date == canonical.end`.
Each source's candidate `SourceCell` is one provider-native request window,
not one daily news row. Windows are inclusive, continuous, non-overlapping,
and satisfy `next.start = previous.end + 1 day`; the final window may be
shorter. `cell_id` is lowercase SHA-256 over canonical JSON containing
`stage`, `source_type`, `endpoint_name`, `subject`, `window_start`,
`window_end`, `date_domain`, `provider_profile_version`, `page_cap`, and
`item_cap`. Plan cells are sorted by stage (`market`, then `evidence`),
source order from the table above, subject, endpoint, `window_start`, and
`window_end` before hashing.

When `source_scopes` is present, `UpdatePlan.plan_hash` includes the ordered
source scopes, complete ordered cells, stage order, provider profile
revisions, page/request caps, calendar revision, fallback policy, and
`allow_stale_ohlcv`. It excludes `created_at`, `db_path`, `db_sha256`,
`expected_plan_hash`, `plan_hash`, runtime estimates, warnings, PID, lease,
latency, and local report paths. Legacy callers without `source_scopes`
retain the existing hash payload and success-empty recheck policy.

Authorized resume uses the same immutable plan identity. Extend
`execute_update()` with optional `parent_run_id: str | None = None`. A resume
creates a new child `ingestion_runs` row, requires the complete ancestor chain
to have the same `plan_hash` and expected hash, and skips only
terminal-complete cells (`success` and `success_empty`) from that lineage.
Partial, failed, rate-limited, and resource-stopped cells remain eligible.
Changing the plan creates a new root run and requires a new authorization; it
is never treated as resume.

## 10. Working DB Bootstrap

Protected DBs are read-only inputs. B2-O never modifies `data/catalyst_dev_ws4b.db` or `data/catalyst_eval_frozen_v2.db`.

Bootstrap sequence:

1. Verify and record the protected Dev DB SHA-256.
2. Open the Dev DB through a read-only SQLite connection.
3. Create a consistent candidate DB snapshot by calling
   `sqlite3.Connection.backup()` from the read-only source connection into a
   temp candidate DB.
4. `fsync` the candidate DB file and parent directory.
5. Open the candidate DB read-write.
6. Call existing `run_migrations(conn)`.
7. Verify `PRAGMA user_version = 12`.
8. Run `PRAGMA integrity_check`.
9. Run `PRAGMA foreign_key_check`.
10. Recompute and verify the protected Dev DB SHA-256 unchanged.
11. Retain all historical rows in the candidate.

Manual reconstruction by copying selected tables into an undefined empty schema is forbidden.

## 11. Recovery Boundaries

Bootstrap or migration failure:

- delete only the incomplete candidate DB;
- leave protected DBs unchanged;
- do not promote.

Live fill interruption:

- retain the working DB, request ledger, raw assets, provenance, source checkpoints, and ingestion run rows;
- resume through B2 run-control and existing planner/executor;
- do not restart bootstrap merely because a provider run is partial.

B3 corpus and B4 FTS publication:

- build into candidate/current-manifest state atomically through existing transaction boundaries;
- the previous current corpus manifest and lexical state remain served until the new build succeeds.
- treat every table included in `DataSnapshotManifest` as immutable during
  corpus publication; source classification and dedup fallbacks are computed
  in memory and persisted only in corpus-owned tables;
- create `idx_index_state_chunk_id` before per-chunk index-state upserts so
  publication remains bounded and does not degrade into repeated table scans.

Promotion:

- pre-snapshot working path:
  `data/candidates/catalyst_b2o_<universe_manifest_id>_<plan_hash>.working.db`;
  both identity components use their full 64-character lowercase hex values;
- after `snapshot_id` is computed and verified, atomically rename the working
  DB to `data/candidates/catalyst_b2o_<snapshot_id>.candidate.db` and fsync the
  candidates directory;
- final versioned path: `data/snapshots/catalyst_b2o_<snapshot_id>.db`
- active pointer artifact: `data/manifests/active_data_snapshot.json`
- atomically rename the verified candidate to the final versioned path only
  after all gates pass;
- `fsync` the DB file and parent directory;
- never overwrite a promoted snapshot;
- set `CATALYST_DB_PATH` to the final versioned DB path for consumers.

Failure during promotion leaves the previous active snapshot and pointer intact.

The active pointer is an audit artifact, not an implicit database resolver. Existing `catalyst_data.config.db_path()` continues to resolve only `CATALYST_DB_PATH` or its existing default. The promotion command prints one shell-safe absolute final path and writes it to the final evidence report; the operator or process supervisor must explicitly export `CATALYST_DB_PATH=<absolute-final-path>` for every subsequent B3/B4/B6/API command. B2-O does not claim that a child process can mutate its parent shell environment.

## 12. Eight-GB Mac Resource Contract

B2-O-I and B2-O-X run on the 8 GB Mac with bounded streaming:

- provider execution processes one response page at a time through the existing B2 executor;
- no provider's complete corpus may be retained in memory;
- before bootstrap and before corpus publication, free disk must be at least `max(10 GiB, 3 * protected_dev_db_size)`;
- before corpus publication, estimate `estimated_chunks = sum(max(1, ceil(source_utf8_bytes / 320)))` for eligible source documents, then `estimated_peak_bytes = source_utf8_bytes * 8 + eligible_document_count * 4096 + estimated_chunks * 8192`, and `required_headroom = estimated_peak_bytes + 512 MiB`;
- the operational facade samples process RSS between cells/batches and stops cooperatively before starting the next cell/batch if current RSS plus required headroom would reach 6 GiB;
- a resource stop records a resumable incomplete state; it does not delete the working DB;
- WAL checkpoints use `PRAGMA wal_checkpoint(PASSIVE)` between stages and `TRUNCATE` only after no writer transaction is active;
- no BGE-M3 embedding model, reranker model, LanceDB production builder, or Hugging Face download may run in B2-O.

The existing B3 implementation currently materializes active chunk metadata for atomic publication. B2-O-I must measure or conservatively estimate fixture and candidate-corpus peak RSS and fail the resource gate instead of silently changing B3 atomicity. A future streaming refactor requires its own design amendment.

## 13. Boundary Before GPU Work

B2-O ends after the promoted v12 DB, `DataSnapshotManifest`, current B3 `CorpusManifest`, and B4 lexical state are verified. It does not create vectors.

B6-L may then implement local dense/RRF/reranker adapters with deterministic fixtures and export a checksummed active-chunk bundle. B6-G is the first phase allowed to load BGE-M3 on the GPU server. Model inference may use FP16 internally, but persisted vectors remain float32 unless the binding B6 contract is explicitly amended.

## 14. Completion Gates

B2-O-I completion requires implementation and offline tests for every contract above. It does not require live provider authorization and does not make B2-O complete.

B2-O completion requires B2-O-X evidence:

- ratified tracked `UniverseSpec`;
- generated runtime `UniverseManifest`;
- protected DB SHA before and after unchanged;
- candidate DB migrated to user_version 12;
- B2 plan hash recorded and enforced;
- live execution completed or resumed to terminal outcomes;
- Polygon and Finnhub canonical coverage complete for all 40 tickers;
- `DataSnapshotManifest.snapshot_id` computed;
- B3 corpus built using that snapshot ID;
- B4 lexical state built for the exact corpus manifest;
- versioned DB promoted and selected by `CATALYST_DB_PATH`;
- B6 not started.


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

Each page-level request maintains separate counters per error class:

- `ledger_attempt_no`: sequential request counter (1..N), used for `attempt_no` and `request_id`
- `transport_attempts`: incremented only on transient transport exceptions (httpx.ConnectError, httpx.TransportError, httpx.TimeoutException, builtin ConnectionError, builtin TimeoutError)
- `rate_limit_attempts`: incremented only on HTTP 429 responses
- `server_error_attempts`: incremented only on HTTP 500/502/503/504 responses

Each counter's maximum is governed by the corresponding RetryRule (timeout/rate_limit/server_error).
A transport error cannot consume rate_limit budget, and vice versa.

### Transient Exception Taxonomy (added 2026-07-25)

Only the following exception types trigger transient retry with 5s/10s backoff:
- `httpx.ConnectError`
- `httpx.TransportError`
- `httpx.TimeoutException`
- builtin `ConnectionError`
- builtin `TimeoutError`

All other exceptions (ValueError, KeyError, TypeError, AssertionError, etc.)
are classified as non-transient and cause immediate `fatal_transport_configuration`
stop without retry, without delay, and without incrementing the circuit counter.

### Page-Level Preservation (added 2026-07-25)

When a multi-page cell has a successful early page followed by a later page transport
failure:
- The successful page's raw_asset, articles, article_tickers, and normalized_provenance are preserved
- The failed page produces no raw_asset, articles, or provenance
- The cell checkpoint is `status=failed, is_complete=0`
- Resume re-executes the entire cell from page 1

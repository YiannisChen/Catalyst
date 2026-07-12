# Catalyst Backend-First Roadmap (WS5)

**Role:** Architecture and roadmap design. No implementation in this document.
**Branch:** ws4b/article-level-data.
**Status:** Draft for architect review. Decisions requiring approval are in §L.
**Process rule:** Per CLAUDE.md, this plan contains no code blocks. Contracts are
specified as tables and prose with conceptual anchors.

Every file path cited in this document was verified to exist on disk on
2026-07-09 unless explicitly marked **(proposed)**.

---

## A. Executive Assessment

### A.1 Current maturity

The system is **populated but not certified, instrumented but not unified**.

- **Data plane:** 269 MB Dev DB (`data/catalyst_dev_ws4b.db`, SHA `92731fb7…`)
  with 36,861 articles, 58,396 article-ticker associations, working
  checkpoint/resume machinery (`source_checkpoints`, `ingestion_runs`), a real
  per-cell atomic write path (the `persist_cell` BEGIN IMMEDIATE transaction in
  `update_pipeline.py`), and a corpus_items VIEW. But the update domain is
  **split-brained**: news/SEC flow through `run_update`, while OHLCV flows only
  through the separate backfill path (`scripts/backfill.py`,
  `backfill_pipeline.py`). OHLCV is simultaneously the trading calendar, the
  ticker universe, and the default watermark inside `compute_missing_cells` —
  so a stale OHLCV table (currently 66 calendar days behind news) silently
  truncates every downstream plan.
- **Test truth:** 153 failures across four packages (83 data-core, 18 agents,
  37 eval, 15 app). None are classified. Until each failure carries a verdict
  (contract-drift / regression / environment / obsolete), every "green" claim
  from any worker is unverifiable.
- **Agent/eval plane:** S1 ruler committed; S2 (harness honesty) and S4
  (regression gate) have amended plans but zero execution. The frozen eval
  plane (`data/catalyst_eval_frozen_v2.db`, `data/lancedb_gold/eval_frozen`,
  `data/embeddings/bge_m3_eval_frozen_v2.npy`) is an **older corpus**, useful
  as a deterministic regression fixture but no longer the headline data plane.
- **App plane:** BYOK base committed (e4bac95); connectivity amendment designed
  but not written into a plan; 15 app test failures unclassified.

### A.2 Highest risks (ranked)

1. **Watermark corruption.** `compute_missing_cells` defaults the ticker
   universe to `DISTINCT symbol FROM ohlcv`, and both `run_update` and
   `run_update_batch` default their date window to `latest_local_ohlcv_date`.
   With OHLCV frozen at 2026-05-01, a default-window update plans a single
   stale day and believes itself complete. (The trading-day *set* is partially
   fixed already — `_trading_days_in_window` delegates to
   `trading_days_for_window`, which unions ohlcv dates with the calendar
   oracle in `trading_calendar.py` — but the window default and the universe
   default are not.) This is the single most likely source of a silently
   incomplete corpus.
2. **False dry-run.** `run_update(dry_run=True)` runs `init_db`,
   `ensure_ingestion_quality_tables`, opens an `ingestion_runs` row, and writes
   a report file. Even `run_update_batch(dry_run=True)` calls `init_db` (DDL on
   a fresh DB). Any operator or future UI that trusts the "zero-write" docstring
   can mutate the DB during a preview.
3. **Unclassified red tests.** 153 failures mean the regression signal is dead:
   a new breakage is indistinguishable from old noise. This blocks honest exit
   gates for every other workstream.
4. **Vectorizing an uncertified corpus.** Embedding is the most expensive,
   least reversible step. Running it before completeness certification bakes
   data gaps into the index and invalidates every downstream experiment.
5. **Single-writer assumption without enforcement.** Nothing prevents two
   concurrent `run_update` processes; `request_cancel` is an in-process set
   (module-level `_CANCEL_REQUESTS`) and cannot cancel a run started in another
   process.

### A.3 Why the corrected order is appropriate

The earlier recommendation (run the 65-case Arm baseline early on the frozen
plane) optimized for cheapest-first evidence. The architect's new order
optimizes for **one authoritative data plane**, and it is correct for three
technical reasons:

1. **A baseline on the frozen corpus would be measured twice.** The frozen v2
   corpus (clean_assets era, 13,474 rows) and the current article-level corpus
   (58,396 corpus_items rows) differ in grain, coverage, and content contract.
   A headline baseline run against the old plane would have to be re-run on the
   new plane anyway; the money is better spent once, on the certified corpus,
   on GPU, with real hybrid retrieval instead of the Mac SQL fallback.
2. **The unified update domain is a prerequisite for certification, and
   certification is a prerequisite for vectorization.** Sequencing data
   completeness → export → embed → validate → experiment eliminates the class
   of "which corpus was this measured on?" questions that would otherwise
   contaminate the portfolio narrative.
3. **The frozen plane is not wasted.** It is demoted, not deleted: S2's
   no-behavior-drift replay and S4's zero-LLM gate continue to use it as a
   cheap deterministic fixture on the Mac (read-only). The headline baseline
   moves to Phase 4 on the new corpus.

One refinement to the phase list (not a reordering): Phase 1 workstreams W1–W6
are largely file-disjoint and can be parallelized on the Mac (see §D); the
phase boundary that must stay hard is **certification before export before
embedding before experiments**.

---

## B. Phase Map

### Phase 1 — Mac backend foundations

| Item | Content |
|---|---|
| Objective | All non-frontend foundations complete, tested, documented, and certified on the Mac. |
| Scope | W3-A test-environment baseline, W1 unified update service, W3-B remaining data-core remediation, W2 completeness/quality/certification, W4 agents/S2, W5 eval/S4-core, W6 app/BYOK internal services + connectivity CORE, operator CLIs, observability, docs. |
| Sequencing | The data-core track is **serialized**: W3-A → W1 → W3-B → W2. W1 and W3 research/triage may proceed in parallel, but implementation may not run concurrently in one worktree — both modify `packages/data-core`. Agents/eval (W4→W5) and app (W6) tracks remain parallel at the design level. |
| Out of scope | Frontend, formal HTTP API surface beyond existing internal FastAPI routers, vectorization, any local embedding, the 65-case experiment. |
| Prerequisites | None (current HEAD d10e71c). |
| Outputs | Certified Dev DB + certification artifact; green (or classified-and-quarantined) test suites; unified update CLI; S2/S4-core executed against frozen fixture; BYOK runtime with staged probe. |
| Exit gate | §F.1 (all Phase-1 acceptance criteria). Certification artifact green. |
| Rollback point | Each workstream is a separate commit chain (§H); rollback = revert the workstream's commits. Dev DB backed up (SHA-pinned copy) before W1 execution runs. |

### Phase 2 — Server data plane + vectorization

| Item | Content |
|---|---|
| Objective | The certified corpus exported, shipped, embedded, and indexed on the server. |
| Scope | W7 corpus export, W8 server vectorization (BGE-M3 + LanceDB + manifests). |
| Out of scope | Experiments, retrieval tuning, API. |
| Prerequisites | Phase 1 exit gate; architect decisions #6–#10 (§L). |
| Outputs | Immutable corpus export + embeddings + LanceDB index + manifest chain (DB SHA → export SHA → embedding SHA → index SHA). |
| Exit gate | Row-count reconciliation exact; manifest chain verifies end-to-end; §F.2. |
| Rollback point | Artifacts are immutable and versioned; rollback = discard the corpus_version directory. |

### Phase 3 — Server functional validation

| Item | Content |
|---|---|
| Objective | Prove every runtime capability on the server before spending experiment money. |
| Scope | W9: retrieval (vector/BM25/hybrid/rerank), agent graph end-to-end on 1–2 cases, trace/cache persistence, provider connectivity, resume/failure drills. |
| Out of scope | Metric collection at scale; any conclusion about quality. |
| Prerequisites | Phase 2 exit gate. |
| Outputs | Signed validation checklist artifact with command transcripts. |
| Exit gate | §F.3; every checklist item has a pasted command + output, independently re-runnable. |
| Rollback point | No state produced worth rolling back; failures loop back to Phase 1/2 fixes. |

### Phase 4 — GPU experiments

| Item | Content |
|---|---|
| Objective | Graduated experiment ladder: retrieval probes → 2-case smoke → 5–10 case pilot → full run. |
| Scope | W10. Three-arm A/B/C, cross-family judge, cost caps, cache/resume, full hash provenance. |
| Out of scope | Fixing anything mid-run (observe, record, stop). |
| Prerequisites | Phase 3 exit gate; architect decisions #8–#9 (judge/model/cost). |
| Outputs | Evidence pack (judge cache, per-case scores, baseline JSON) on the **new** corpus; experiment report. |
| Exit gate | Pilot passes §F.4 sanity gates before full run is authorized. |
| Rollback point | Cache/resume means an aborted run loses only uncached cases. |

### Phase 5 — Experiment-driven remediation

| Item | Content |
|---|---|
| Objective | Convert experiment failures into targeted fixes (W11), re-validating on server per §E.9 loop. |
| Scope | Retrieval, chunking, corpus quality, S2 policy values (v2 candidates), Critic/Judge/Validator, refusal, latency, cost, routing. |
| Out of scope | New features; API; frontend. |
| Prerequisites | Phase 4 evidence pack. |
| Exit gate | Architect accepts behavior; S4 gate green on final baseline. |
| Rollback point | Every remediation lands behind the S4 regression gate with a before/after delta. |

### Phase 6 — Backend and data contract freeze

Objective: freeze schemas (`agent_runs`, corpus export schema, PolicyConfig,
RunReport, certification schema) and tag a release. Exit gate: no open
blocking issues; ADR recording frozen contracts. Rollback: git tag.

### Phase 7 — Formal HTTP API

Objective: wrap the **already-stable** internal service boundaries (§E.1
service contract, run/status/report objects, BYOK validator, freshness report)
in a versioned HTTP surface. Deliberately thin: the API exposes, it does not
design. Outline in §K.11 (kept separate from Phase-1 work per requirements).

### Phase 8 — Frontend redesign

Objective: consume frozen API contracts. Update page with plan-preview →
confirm → progress/ETA → report; run browser; evidence/trace UI. Out of scope
here by instruction.

---

## C. Workstreams

| WS | Name | Phase | CORE deliverable | STRONG add-ons |
|---|---|---|---|---|
| W1 | Market/Evidence Update service | 1 | Unified plan/execute/inspect/cancel/resume contract with OHLCV first-class and true zero-write planning | ETA model beyond linear estimate; provider conflict ledger |
| W2 | DB Completeness & Quality | 1 | Certification artifact + gap backfill to close OHLCV/news windows | near-dup scoring (S3 Phase 6), freshness gate (S3 Phase 7) |
| W3-A | Data-core test **environment** baseline | 1 | Async/plugin/configuration failure groups fixed; a trustworthy red/green baseline for data-core recorded before W1 touches the package | — |
| W3-B | Data-core test remediation (remainder) | 1 | All remaining data-core failure groups (regressions + contract drift) driven to 0 red, after W1 lands | property-based tests for missing-cell math |
| W4 | Agents/S2 | 1 | S2 CORE Phases 1–4 per amended plan (PolicyConfig, expand_macro live-but-disabled, RunBudget, pricing fail-fast) + 18 agents failures classified | S2 STRONG (A4 structured outputs, A5 hard temporal gate) |
| W5 | Eval/S4-core | 1 | S4 Phases 1–2 (zero-LLM gate + check_p0_gate rewrite) against frozen fixture; 37 eval failures classified | S4 Phase 4 automation |
| W6 | App/BYOK internal services | 1 | Connectivity CORE (staged probe, profiles, explicit http_client, doctor), BYOK Tasks per plan, 15 app failures classified | fallback chains (C5), multi-server doctor |
| W7 | Corpus export | 2 | Immutable export + manifest from certified DB | incremental export deltas |
| W8 | Server vectorization | 2 | BGE-M3 embeddings + LanceDB + manifest chain + active-index pointer | incremental embed updates |
| W9 | Server functional validation | 3 | Scripted checklist with transcripts | automated nightly re-validation |
| W10 | GPU experiments | 4 | Graduated ladder + evidence pack on new corpus | ablation extensions |
| W11 | Experiment remediation | 5 | Triage taxonomy + fix loop behind S4 gate | — |
| W12 | API freeze & implementation | 7 | Versioned HTTP API over frozen services | — |
| W13 | Frontend redesign | 8 | Update/run/evidence UI on frozen API | — |

---

## D. Dependency Graph

Hard dependencies (→ means "must complete first"):

- **Data-core track is strictly serialized: W3-A → W1 → W3-B → W2.**
  W3-A first because W1's exit gate is unverifiable against an untrustworthy
  baseline (environment-class failures mask real ones). W1 before W3-B because
  W1 deliberately changes contracts (dry-run semantics, planning defaults) that
  many currently-failing tests assert — remediating them twice is waste.
  Research and failure-group triage for W3-B may proceed in parallel with W1,
  but no concurrent implementation in `packages/data-core` in one worktree.
- W1 → W2 (certification needs the unified planner to compute gaps honestly).
- W2 → W7 → W8 → W9 → W10 → W11.
- W4 (S2) → W10 (experiments run under PolicyConfig v1 with RunBudget).
- W5 (S4-core) → W10 (gate must exist to receive the new baseline) and → W11
  (remediation lands behind the gate).
- W6 → W9/W10 (provider connectivity + BYOK runtime used on server), but W6 is
  file-disjoint from W1/W2 and parallelizable.
- Within W4/W6: S2 Phase 4 (cost_tracker) before BYOK cost tri-state task
  (same file, single writer).
- W11 ⇄ W9 (remediation re-validates on server as needed).
- W6/W9/W11 → W12 → W13.

Parallel tracks on the Mac in Phase 1:

- Track α (serialized): W3-A → W1 → W3-B → W2 (data-core plane).
- Track γ: W4 then W5 (agents/eval, frozen fixture).
- Track δ: W6 (app/BYOK/connectivity).

Tracks α, γ, δ touch disjoint packages (data-core / agents+eval / app) except
the two named shared-file seams: `catalyst_agents/cost_tracker.py` (S2 vs
BYOK) and `catalyst_agents/retrieval/policy.py` (S2 Phase 2 vs W1 — W1 does
not touch it; no conflict).

---

## E. Detailed Designs

### E.1 Unified update planning/execution (W1)

**Evaluation of the proposed shape.** The five-verb contract
(plan/execute/inspect/cancel/resume) is correct and maps closely onto what
exists. Two amendments:

1. **Execution must re-plan.** A plan computed at time T is stale by time T+1
   (checkpoints move, OHLCV moves). `execute_update` therefore takes the
   original config plus an optional `expected_plan_hash`; it re-plans at start
   and, in strict mode, aborts with `PlanDriftError` if the recomputed hash
   differs. This gives the future UI a safe preview→confirm→execute flow
   without pretending plans are immutable inputs.
2. **Two-stage runs, one run_id.** Because of the OHLCV-first dependency
   (§E.2), a full "Evidence Data Update" is a single run with an internal
   stage field: stage 1 executes OHLCV cells, re-derives the watermark, then
   stage 2 plans and executes news/SEC/FRED cells against the fresh watermark.
   `ingestion_runs` gains a `stage` progress field (extends the existing
   `current_source`/`current_ticker`/`current_date` progress columns).

**Authoritative contract** (module: proposed
`packages/data-core/catalyst_data/update_service.py`, wrapping — not
duplicating — `update_pipeline.py` internals):

| Operation | Input | Output | Side effects |
|---|---|---|---|
| plan_update | RunConfig (tickers, sources, window, limit, fallback flags) | UpdatePlan | **None** (§E.1.1) |
| execute_update | RunConfig, expected_plan_hash? | RunReport | run row, **persisted run-plan artifact (data/run_plans/<run_id>.json, written before the first cell)**, checkpoints, silver rows, report file |
| inspect_update | run_id | RunStatus (status, stage, progress, ETA, last error) | none |
| cancel_update | run_id | ack | durable cancel flag (§E.3) |
| resume_update | run_id (parent) | RunReport | new run with parent_run_id; **cells = parent's persisted planned cells minus parent's success/success_empty cells** (recovers failed, skipped, and never-attempted alike; never re-planned from current DB/time) |

**UpdatePlan object** (deterministic, serializable):
plan_schema_version; created_at; db SHA-256; config echo; resolved ticker
universe with its provenance ("explicit" vs "ohlcv-derived"); calendar window
per source; missing cells per source with counts; provider request estimates
(cells × requests-per-cell per connector); predicted fallback exposure (cells
whose primary provider has recent failed checkpoints of transport class);
duration range (cells ÷ effective rate-limit throughput, min/max across
providers, from `provider_limits.py`); predicted index delta (articles whose
`index_state` would become pending — derivable from missing news cells only as
an upper bound, labeled as such); **plan_hash** = SHA-256 over the canonical
JSON of everything above except created_at.

CORE excludes any ML-ish ETA; the linear rate-limit estimate is honest and
sufficient. STRONG: per-provider empirical throughput from past `RunReport`
elapsed data.

#### E.1.1 True zero-write planning

Current behavior is disqualifying: `run_update(dry_run=True)` writes DDL, a
run row, and a report file; `run_update_batch(dry_run=True)` still calls
`init_db`. The fix is structural, not a flag:

- `plan_update` opens the DB with the SQLite URI read-only mode (`mode=ro`).
  Any write attempt raises at the driver level — this is the enforcement, not
  convention.
- No `init_db`, no `ensure_ingestion_quality_tables` on the plan path. If the
  schema is missing tables the plan needs, plan_update fails with
  `SchemaOutOfDate` telling the operator to run the migration CLI. Planning
  never migrates.
- No run row, no report file. The plan is returned to the caller; the operator
  CLI may print it or write it **outside** the data directory (stdout or an
  explicit `--out` path).
- `run_update(dry_run=True)` and `run_update_batch(dry_run=True)` are
  deprecated: dry_run=True delegates to plan_update (behavior change —
  landmine for existing tests, must be called out in the dscodex plan).

**Zero-write proof protocol** (this is a test, not a claim):

1. Record SHA-256 of the DB file, plus the DB's `-wal`/`-shm` siblings if
   present, plus a recursive listing (path, size, mtime) of `data/` filtered
   to run_reports and the DB.
2. Run plan_update against the real Dev DB with no network access (the plan
   path takes no fetch_fn; the test additionally installs a socket-blocking
   guard so any accidental network call fails loudly).
3. Re-record and compare: DB SHA identical, no new files, no mtime changes.
4. Determinism: run plan_update twice; plan_hash identical.

This test runs against a copy of the real Dev DB in CI-lite (Mac), and against
the live Dev DB once, manually, as part of the W1 exit gate.

### E.2 OHLCV-first freshness (W1/W2)

**Semantics:**

| Concern | Design |
|---|---|
| Primary provider | Polygon (existing connector `connectors/polygon.py`). |
| Fallback provider | `connectors/yfinance_fallback.py` exists; FMP (`connectors/fmp.py`) is a candidate. **Architect decision #3** fixes the order. Fallback rows are marked with provider provenance; a Polygon row always wins a conflict (provider precedence, not last-write-wins). |
| Ticker universe | Explicit configured list (architect decision #1). The current implicit universe (`DISTINCT symbol FROM ohlcv`) is retained only as a fallback with plan-level provenance labeling — a plan built on an implicit universe says so. |
| Exchange calendar | `trading_calendar.calendar_trading_days` becomes the **only** source of expected trading days for planning. The current `trading_days_for_window` (calendar ∪ ohlcv dates) already fixes day-set truncation; the union's ohlcv side is demoted to a reconciliation input (what we have), never a definition of expectation (what should exist). The remaining fixes are the window default (`latest_local_ohlcv_date` single stale day) and the universe default. |
| Latest complete session | `trading_calendar.latest_closed_trading_day_for_date` (exists), evaluated in exchange time. |
| Weekends/holidays | Absent from the calendar → never planned → never "missing". |
| Empty-valid | A trading day where the provider legitimately returns no bar (halt, new listing) persists a `success_empty` checkpoint with a recorded reason. Fully specified lifecycle — recheck count, recheck timing, cross-run attempt counting, terminal states, certification inputs — is in W1 design §6.10 with concrete default constants (recheck at most once, ≥ 1 day after the checkpoint, only for cells within 5 calendar days of reference_today; attempts counted across all runs). Constants are architect decision #13. |
| Adjusted vs unadjusted | v1 stores adjusted prices only (Polygon adjusted=true). Splits/dividends policy: adjusted-only is CORE; raw+adjusted dual columns is STRONG. **Architect decision #4.** |
| Upsert | PK (symbol, date); conflict → replace only if incoming provider precedence ≥ existing; provider recorded per row. |
| Missing-session retries | Failed OHLCV cells are already re-included by `compute_missing_cells` (failed ⇒ missing). Error-class-aware exclusion: cells failing with a permanent class (from `error_taxonomy.py`) on 3 cross-run attempts move to "unresolvable-pending-review"; terminal unresolvable status requires architect approval recorded in the certification. Transient-class failures are never auto-excluded; ≥ 5 cross-run attempts flags them "chronic-transient" for review. Full state machine: W1 design §6.10 (constants = architect decision #13). |
| Completeness definition | **Completeness is never defined by rows already present in `ohlcv`.** For every (ticker, expected session) pair — expected sessions derived from `calendar_trading_days` over the configured window — the pair must resolve to exactly one of: (a) bar present in `ohlcv`; (b) `success_empty` checkpoint with a valid recorded reason (halt, pre-listing, provider-confirmed no-bar); (c) an architect-approved unresolvable classification recorded in the certification. Any pair resolving to none of the three is incomplete, full stop. |
| Freshness watermark | Per-ticker watermark = latest expected session for which the tri-state above resolves to (a) or (b), with no unresolved session before it. **The per-ticker watermark is the control surface: it caps that ticker's evidence planning.** The global market watermark (min over the configured universe) is reporting/certification metadata only — it must never block evidence updates for unrelated tickers. Persisted nowhere; always derived (S3 D3 principle: derive from checkpoints, no new state table). |

**How OHLCV completion drives evidence planning:** the stage-2 planner caps
each ticker's news/SEC window at
min(that ticker's resolved OHLCV watermark, latest closed trading day) —
**per ticker, not globally.** One ticker with a stuck OHLCV provider lowers
its own evidence ceiling only; the other nine proceed to their own
watermarks. If the architect wants a ticker's news beyond its OHLCV watermark
(e.g., OHLCV provider outage but news should proceed), that requires an
explicit `--allow-stale-ohlcv` override which is recorded in the plan and the
RunReport — visible, never silent.

FRED nuance: macro observations are not ticker-scoped, so no ticker watermark
applies — the FRED planner's ceiling is the latest closed trading day only.
SEC nuance: filings are event-driven, not daily-dense; the SEC planner keeps
its existing cell semantics with the per-ticker ceiling applied to the cell
date range.

**Immediate consequence for the current DB:** the first certified update must
(1) backfill OHLCV 2026-05-02 → latest closed session for all 10 tickers,
(2) re-derive the watermark, (3) plan news/SEC/FRED against it —
`compute_missing_cells` being checkpoint-driven will correctly no-op the
already-ingested May–July news and fill only true gaps.

### E.3 Checkpoint / resume / cancel (W1)

- **Single active writer:** opening a run checks for any `ingestion_runs` row
  with status running and a live heartbeat; if found, refuse with
  `WriterConflictError`. Add `heartbeat_at` and `pid` columns. The heartbeat
  is a **lease**: a lightweight background ticker updates `heartbeat_at` on a
  fixed interval independent of cell progress, so a live run is never
  mistaken for an orphan during a long provider call, retry ladder, or
  rate-limit wait (W1 design §6.6 specifies interval/threshold and why a
  bounded-max-cell-duration threshold was rejected). A run whose lease
  heartbeat is older than the stale threshold is an **orphan**: `doctor.py`
  gains a reconciliation check that marks orphans interrupted (crash
  recovery), after which resume works normally.
- **Persisted run plan:** every executing run writes its exact plan to
  data/run_plans/<run_id>.json before the first cell runs, and the run row
  gains `plan_hash` and `plan_path` columns; atomic ordering and failure
  behavior are specified in W1 design §5.5. No cell ever executes under a
  run whose plan_path is null.
- **Durable cancel:** add `cancel_requested_at` to `ingestion_runs`.
  `cancel_update(run_id)` writes it (works cross-process). The executor's
  per-cell loop checks the column (cheap: it already writes progress per cell)
  in addition to the existing in-process `_CANCEL_REQUESTS` fast path. The
  existing behavior — remaining cells checkpointed as skipped with
  error_class canceled — is kept.
- **Resume:** a new run whose cell set is **the parent's persisted planned
  cells (from data/run_plans/<run_id>.json) minus the parent's
  success/success_empty checkpoints** — recovering failed, skipped, and
  never-attempted cells alike. Re-planning from the current DB/time is not an
  acceptable substitute (it would silently drop never-attempted cells whose
  window has moved). `parent_run_id` promoted to a real column. Resume of a
  two-stage run resumes at the failed stage (stage inferred per cell from
  source_type); W1 design §6.8 covers the crash-before-stage-2-final case.
- **Crash recovery invariant:** because `persist_cell` writes raw_asset +
  checkpoint in one transaction, a crash leaves either a fully-recorded cell
  or no record — resume subtracts success/success_empty checkpoints from the
  persisted plan and is idempotent. The test for this is a kill-mid-run drill
  (§G, W1 failure-path tests — fixture DB + child process only).
- **Retry taxonomy:** already exists (`error_taxonomy.py`, `retry.py`,
  `fallback.py` FallbackPolicy). W1 does not redesign it; it wires OHLCV
  through the same taxonomy.
- **Raw forensic storage / redaction:** existing (raw_assets on MALFORMED;
  `doctor.py` secret redaction pattern). RunReports must pass a
  redaction check: no substring of any configured API key appears in any
  report file (test asserts this with fake keys).
- **Backup/rollback:** before any execute_update on the certified-track DB,
  the operator CLI takes a SHA-pinned copy (SQLite backup API or file copy
  while no writer holds the lock). CORE = manual pre-run backup step in the
  runbook + CLI flag; STRONG = automatic rotation of N backups.

### E.4 Data completion certification (W2)

**Artifact:** versioned JSON + human-readable Markdown pair at
`data/certifications/` **(proposed directory)**, e.g.
`dev_ws4b_cert_v001.json` / `.md`. Generator: proposed
`packages/data-core/catalyst_data/certification.py` with an operator CLI
(extend `cli_index.py` family or a new `scripts/certify.py` **(proposed)**).

Content (all computed, none asserted):

| Section | Fields |
|---|---|
| Identity | db path, file SHA-256, sqlite user_version, schema table list hash, generated_at, cert schema version |
| Universe | configured tickers, provenance, exchange calendar version |
| Coverage | per-source date bounds; per-ticker × per-source cell coverage %, computed against `calendar_trading_days` (not against ohlcv-derived days — that would be circular) |
| OHLCV | tri-state resolution per (ticker, expected session): bars present / success_empty-with-reason / architect-approved unresolvable; unresolved remainder listed (capped, with count); watermark per ticker; global watermark (metadata only, never a planning gate). Expected sessions from `calendar_trading_days`, never from rows already present. |
| News | missing cells per (ticker, source); Finnhub-vs-Polygon overlap window stats |
| Checkpoints | success / success_empty / failed / skipped counts; failed broken down by error_class; lifecycle buckets per W1 design §6.10: terminal valid-empty, pending-recheck, unresolvable-pending-review (permanent-class, 3 attempts), architect-approved unresolvable, chronic-transient (≥ 5 attempts) — pending buckets block certification until resolved or approved |
| Quality | RAG-eligibility rate; empty/short-content rate (thresholds from `eligibility.py` / `quality.py`); duplicate rate from `dedup/cross_source.py` outputs |
| Filings | filings + filing_documents counts per ticker; forms coverage vs expected forms list (architect decision #3 scope) |
| Macro | `macro_freshness` output (freshness.py) |
| Index | index_state pending / embedded counts; corpus_items row count reconciliation against articles×article_tickers + filings |
| Verdict | per-gate pass/fail against thresholds in the cert config; overall CERTIFIED / NOT CERTIFIED |

**Gate rule:** W7 (export) refuses to run unless the newest certification for
the exact DB SHA has verdict CERTIFIED. Thresholds (e.g., coverage ≥ X%) are
architect decision #5 — the generator ships with proposed defaults and the
architect signs them.

### E.5 Corpus export (W7)

- **Source of truth:** the `corpus_items` VIEW on the certified Dev DB.
  Article-ticker duplication semantics are inherited from the VIEW grain (one
  row per article × ticker, INNER JOIN — per the committed S3 design). Filing
  primary-document semantics likewise inherited (window-function exhibit_99_1
  preference). Macro observations are **excluded** from the embedded corpus in
  v1 (they serve the MACRO retrieval layer via SQL, not vector search) —
  architect decision #10 can revisit.
- **Format:** JSONL, gzip-compressed shards under
  `data/corpus_exports/corpus_v001/` **(proposed)**: deterministic ordering
  (ORDER BY corpus_item_id), fixed field list matching the VIEW columns, plus
  export manifest: source db SHA, cert version, row count, per-source counts,
  per-shard SHA-256, whole-export SHA-256 (hash of shard hashes), export tool
  version.
- **Transfer:** export produced on the Mac, verified, rsync/scp to server,
  SHA re-verified server-side before any use. The Dev DB file itself is also
  shipped (269 MB) for validation queries, but the export is the embedding
  input — the DB is reference, the export is contract.
- **Immutability:** a corpus_version directory is never modified after its
  manifest is written; corrections mean corpus_v002.

### E.6 Server vectorization (W8)

- **Model:** BGE-M3, revision pinned by exact HF commit hash in the manifest
  (architect decision #8 confirms model + revision). Dense vectors, dimension
  1024, fp16 inference; normalize per current index-builder convention (the
  existing eval_frozen artifacts establish the precedent — verify the
  normalization flag from the frozen manifest before reuse).
- **Chunking:** v1 = L1 whole-item chunks, byte-identical to the corpus_items
  `content_md` contract (title + newline + description/body). This keeps the
  content contract already tested in S3. Body-level chunking is a Phase 5
  remediation candidate, not a Phase 2 gamble.
- **Batching/memory:** batch size and max sequence length are config, recorded
  in the manifest; OOM policy = halve batch and retry once, then fail the
  shard (no silent truncation of inputs).
- **Resume:** embedding runs shard-by-shard; a shard completes by writing
  `embeddings/shard_NNN.npy` + its hash into a running checkpoint file; resume
  skips completed shards. `index_state` rows transition pending → embedded
  only after the LanceDB build commits.
- **Artifact layout (proposed):** `corpus_v001/` containing export shards,
  `embeddings/` shards, `lancedb/` table, and `manifest.json` binding: source
  DB SHA → cert version → export SHA → embedding SHA (per-shard + aggregate) →
  LanceDB content hash → model id + revision → dimension → row counts at every
  stage (export rows == embedded rows == LanceDB rows, reconciled exactly).
- **Active index pointer:** a single small file (proposed
  `data/lancedb_gold/ACTIVE`) naming the current corpus_version. Runtime
  dependency loading resolves the pointer; rollback = rewrite the pointer to
  the previous version. No code change to switch indexes.
- **Incremental strategy (STRONG, design-only now):** future updates append a
  delta export (new corpus_item_ids since last export watermark), embed the
  delta, and add to LanceDB; full rebuild remains the correctness fallback.
  v1 ships full-rebuild only.

### E.7 Server functional validation (W9)

A scripted checklist (proposed `scripts/server_validation.py` or a documented
runbook of existing CLIs — dscodex plan decides which per item), each item
producing a transcript:

1. Export SHA re-verification on server.
2. Embedding integrity, layered (not a single cosine invariant):
   (a) artifact integrity — every embedding shard's SHA-256 matches the
   manifest; (b) alignment — vector count equals export row count, dimension
   equals manifest dimension, row order matches corpus_item_id order;
   (c) identity — model id, revision, tokenizer hash, max-length, and
   normalization flag in the manifest match the loaded runtime exactly;
   (d) numeric health — all vectors finite, norms within the expected band
   for the normalization mode; (e) re-embedding drift — sample re-embedded
   **on the pinned hardware/stack that produced the artifacts**, compared at a
   tolerance calibrated on that stack during W8 (recorded in the manifest);
   cross-hardware cosine equality is explicitly NOT an acceptance criterion.
3. LanceDB open/read; row count == manifest.
4. BM25 path, vector path, hybrid/RRF path each return non-empty, distinct
   rankings for probe queries with known-relevant documents (hand-picked from
   the corpus, ~10 probes).
5. Reranker loads and changes ordering on at least one probe.
6. Query embedding path uses the same model+revision as the corpus (manifest
   check in code, not by eye).
7. Retrieval filtering: ticker filter and date-window filter provably narrow
   results.
8. Agent Miner end-to-end on 2 cases with hybrid retrieval (no SQL fallback —
   assert `fallback_surface` absent in the trace).
9. `arm_b_evidence` persisted at the miner seam and reconstructable (this is
   the previously-flagged open verification; it must be proven here at the
   latest, and preferably in W4 on the Mac against the frozen fixture first).
10. Critic/Judge/Validator run; trace persistence lands in the designated
    trace DB (inspect the actual DB file, not logs); judge cache writes land
    at the tracked cache path.
11. Provider connectivity: staged probe (W6) green for the profile's chain;
    doctor report redacts secrets.
12. Kill-and-resume drill on a 2-case run.

### E.8 Experiment execution (W10)

Ladder with explicit authorization between rungs (architect approves each):
retrieval-only probes (zero LLM) → 2-case Arm smoke → 5–10 case pilot → full
run. Every run records: config (PolicyConfig JSON in agent_runs.config, per
S2), RunBudget caps, model ids (agent + judge, cross-family per prior
decision), prompt SHA-256s, DB SHA, corpus_version, index hash, embedding
model+revision, retrieval_mode. Hard rules: cost cap enforced by RunBudget
(downgrade to PARTIAL/INSUFFICIENT, never crash); judge cache-through; resume
from cache on abort; **any silent retrieval fallback (fallback_surface
present) invalidates the run** — the harness asserts its absence per case.
Pilot sanity gates (§F.4) decide whether the full run is money well spent.

### E.9 Remediation feedback loop (W11)

Triage taxonomy — every failed/低-quality case gets exactly one primary label:

| Symptom (from traces/metrics) | Layer | Fix lane |
|---|---|---|
| Relevant doc absent from corpus | data quality / coverage | W2-style backfill + re-cert |
| Doc present, not retrieved | retrieval (embedding/hybrid weights/filters) | retrieval config; chunking if content mismatch |
| Retrieved, wrong chunk granularity | chunking | Phase-5 chunking redesign |
| Retrieved, Critic dropped it | S2 policy (thresholds, temporal penalty, tier weights) | recalibration via S4 recalibrate procedure |
| Evidence fine, Judge misattributed | Judge prompt/model | prompt or routing change |
| Attribution fine, Validator wrongly blocked | Validator thresholds | validator recalibration |
| Should have refused, didn't (or inverse) | refusal policy | Critic/refusal rules |
| Correct but over budget / slow | latency/cost | routing, caching, budget tuning |

Process invariants: each remediation batch = one ADR with measured before/after
deltas (the S4 recalibration machinery is the vehicle); every fixed failure
becomes a pinned regression case; re-run the pilot subset after each batch
before considering another full run; all changes land behind the S4 gate.

---

## F. Acceptance Criteria (measurable)

### F.1 Phase 1 exit

1. `.venv/bin/python -m pytest packages/data-core -q` → 0 failed; quarantined
   tests are marked with skip reasons referencing a root-cause group id, and
   the quarantine count is ≤ the number of groups classified "obsolete —
   pending deletion decision". Same for agents, eval, app packages.
2. A root-cause-group triage document exists per package. Every one of the 153
   current failures maps to **exactly one group** (no per-failure bureaucracy;
   expected order-of-magnitude is 10–25 groups total). Each group records:
   signature (the shared error/assert pattern), affected tests (list or glob),
   classification ∈ {contract-drift, regression, environment, obsolete}, root
   cause (one paragraph), resolution (fix description or quarantine decision),
   verification command (the exact pytest invocation proving the group green),
   and owner/commit boundary. Zero groups classified "pre-existing noise";
   the sum of affected tests across groups equals the failure count at
   triage time.
3. Zero-write plan proof passes against a copy of the Dev DB: DB SHA identical
   before/after, no new/modified files under data/, plan_hash deterministic
   across two runs (§E.1.1 protocol, automated test + one manual transcript).
4. One full two-stage update executed on the real Dev DB: OHLCV watermark ==
   latest closed trading session for all configured tickers; then evidence
   stage; RunReport saved; cert regenerated.
5. Certification artifact verdict CERTIFIED with architect-signed thresholds;
   every (ticker, expected session) in the configured universe/date range
   resolves to bar-present, success_empty-with-reason, or architect-approved
   unresolvable — zero unresolved; news missing cells = 0 or each residual is
   classified unresolvable with error class.
6. Kill -9 during an update **of a child process running against a fixture
   DB**, then resume: final checkpoint set equals a never-killed control run
   on the same fixture. Destructive kill drills are never performed against
   the real Dev DB — the real-DB confidence comes from the fixture drill plus
   the per-cell transaction invariant, not from live destruction.
7. Cross-process cancel: cancel_update from a second process stops the run
   within one cell boundary; remaining cells checkpointed skipped/canceled.
8. S2 CORE executed: P0 replay identical under PolicyConfig v1 (including 3
   unanswerable), no module-level MAX_EXPANSIONS fallback (grep proof),
   UnknownModelPricingError raised for unknown model, budget downgrade never
   SYSTEM_ERROR — all per the amended S2 plan's own verification list.
9. S4 CORE executed: gate runs with zero LLM calls from tracked
   `packages/eval/eval_cache/` on a fresh checkout (frozen-fixture baseline);
   corrupted baseline → red; rubric_version bump → cache miss.
10. BYOK/connectivity CORE: staged probe classifies at minimum the two known
    live failures correctly (Anthropic-403 → region_or_permission_blocked;
    SiliconFlow-401 → auth_invalid); no API key substring in any log, report,
    or probe artifact (test with canary keys); hosted profile rejects
    server-env credentials with 403.
11. Operator runbook exists documenting: plan, execute, cancel, resume,
    certify, backup — each with the exact command and expected artifact paths.
12. Obsolete demo workflows removed or moved under a quarantine directory with
    a deprecation note (inventory listed in the W3 triage doc).

### F.2 Phase 2 exit

1. Manifest chain verifies by script: DB SHA → cert version → export SHA →
   per-shard embedding SHAs → LanceDB row count; all row counts equal
   (export == embedded == indexed), off-by-zero.
2. Export determinism: two exports from the same DB SHA produce identical
   export SHA.
3. Embedding checks per the layered §E.7 item-2 protocol: shard SHAs match
   manifest; row/dimension alignment exact; model/revision/tokenizer/
   normalization identity match; all vectors finite with in-band norms;
   ≥ 100-item re-embedding drift check on the pinned production stack within
   the tolerance calibrated and recorded during W8.
4. ACTIVE pointer resolves; flipping it to a nonexistent version fails loudly
   at dependency-load time.

### F.3 Phase 3 exit

All 12 checklist items in §E.7 have transcripts; items 8–10 additionally
verified by inspecting the landed DB/cache files (row counts before/after),
not runtime logs alone.

### F.4 Pilot sanity gates (before full run authorization)

Hard operational gates (all must pass; any failure blocks the full run):

1. 100% of pilot cases produce schema-valid outputs with complete traces.
2. Zero cases with fallback_surface present (no silent retrieval fallback).
3. Evidence relevance spot-check: for every pilot case, a human confirms the
   retrieved evidence set is plausibly on-topic for the case (catches an index
   pointed at the wrong corpus even when schemas validate).
4. Cache replay: judge cache hit rate on immediate re-run = 100%
   (determinism of cache keys).
5. Refusal sanity: unanswerable pilot cases refuse; answerable ones do not
   refuse en masse (≥ 1 refusal inversion = fail).
6. Projected cost: cost per case × 65 ≤ architect's cost cap (decision #9)
   with ≥ 30% margin.

Stop-and-review trigger (not a statistical gate — 5–10 cases carry no
significance): if Arm C < Arm B on citation_faithfulness in the pilot, stop
and review the traces with the architect before authorizing the full run.
The review decides remediate-first vs proceed-with-eyes-open; the pilot delta
itself is never quoted as a result.

---

## G. Test Strategy (per workstream)

Canonical commands, always: `.venv/bin/python -m pytest packages/<pkg> -q`
from repo root. Any worker report using a different interpreter or CWD is
non-canonical and rejected.

**W1 (data-core):**
- Unit: missing-cell math against `calendar_trading_days` (holiday, weekend,
  half-window cases); plan_hash determinism and sensitivity (one checkpoint
  flip changes the hash); provider request estimates; watermark derivation
  (per-ticker, global-min, empty universe).
- Integration: two-stage run on a fixture DB (tiny synthetic ohlcv + news
  fetch fakes); resume; cross-process cancel (spawn a second process).
- DB landmines: plan path must not create `-wal` files (read-only URI);
  `init_db` absence on plan path (assert schema untouched on a schema-stale
  fixture → SchemaOutOfDate, not migration); dry_run delegation breaking old
  tests that asserted report creation (update those tests deliberately,
  classified contract-drift).
- Failure paths: kill-mid-run drill — always a spawned child process against
  a fixture DB, never the real Dev DB and never the test-runner process
  itself; provider TRANSPORT error → fallback provider path → provenance
  columns set; permanent error class → excluded after 3 cross-run attempts
  and surfaced (lifecycle per W1 design §6.10).
- Live smoke: gated by env keys (pattern already exists in the gated live
  runner); 1 ticker × 3 days OHLCV against Polygon; assert rows + checkpoints.
- Manual artifacts: one real RunReport JSON; one certification MD read
  end-to-end by a human.

**W2:** unit tests for every cert section against fixture DBs with known gaps
(a missing session, a duplicate pair, an empty article); gate test: export
CLI refuses on NOT CERTIFIED; landmine: coverage computed vs calendar not vs
ohlcv (circularity test: delete an ohlcv row → coverage % must drop).

**W3-A / W3-B:** the deliverable *is* test truth. Process: cluster failures by
shared signature into root-cause groups (not 153 rows — one group per shared
cause, with signature / affected tests / classification / root cause /
resolution / verification command / owner+commit boundary). W3-A fixes the
environment/plugin/configuration groups first and re-runs the full suite to
establish the trustworthy baseline — environment failures mask real failures
beneath them, so the group inventory is only final after W3-A. W3-B (post-W1)
resolves the remaining regression and contract-drift groups; W1's deliberate
contract changes (dry-run delegation, planning defaults) will themselves
create new contract-drift groups, which W3-B absorbs. Landmines: tautological
fixes (loosening asserts to pass) are forbidden and checked in review; a group
whose fix doesn't turn *all* its affected tests green was mis-clustered —
split it, don't stretch it.

**W4 (agents/S2):** the amended S2 plan's own test intents govern. Additional:
the previously-flagged open item — prove `arm_b_evidence` is persisted at the
miner seam (pre-Critic) on the frozen fixture; if the earlier fix never
landed, it is a W4 task. Landmine: frozen DB opened read-only in all replay
tests; TraceWriter must never default into the frozen DB (explicit trace-db
in every fixture).

**W5 (eval/S4):** amended S4 plan's test intents govern; plus triage of the 37
failures (expect most are legacy R1/Scheme-C contract-drift → delete or
archive with the triage table as the record).

**W6 (app/BYOK):** staged-probe stage-ladder unit tests with a fake transport
(each stage failure surfaces the right class + hint); redaction canary tests;
profile resolution tests (hosted rejects server env); explicit http_client
injection test (proxy env honored even when a transport would be injected);
15-failure triage.

**W7/W8 (server):** export determinism unit test on fixture; shard-resume test
(delete one shard checkpoint → only that shard re-runs); reconciliation test
(drop one export row → manifest verification fails); live: the E.7 checklist.

**W9/W10:** the checklists in §E.7/§E.8 are the tests; each item's transcript
is the artifact.

---

## H. Commit Strategy

Never mix workstreams in one commit. Recommended chain (each independently
revertible), following the serialized data-core order W3-A → W1 → W3-B → W2.
Per-commit test gate for W1 slices (commits 2–7): the slice's focused tests
green; the canonical full package suite **always run**; the known-failure
manifest (established by W3-A) unchanged or reduced; zero new failure groups.
Full package green is required only at the W3-B and W2 exits (commits 8–9),
not at every W1 slice:

1. `test(data-core): fix environment/plugin groups, record baseline` (W3-A —
   environment-class root-cause groups only; the deliverable includes the
   **known-failure manifest**: the exact post-fix list of expected-failing
   test ids by group, which every W1 slice's full-suite run is diffed
   against).
2. `feat(data-core): true zero-write update planning` (plan_update +
   read-only enforcement + dry_run delegation + tests) (W1-A).
3. `feat(data-core): calendar-derived planning and explicit universe` (W1-B).
4. `feat(data-core): OHLCV first-class source with fallback` (W1-C).
5. `feat(data-core): two-stage unified update service facade` (W1-D — includes
   the additive `stage`, `plan_hash`, and `plan_path` migration so execution
   cannot precede persisted-plan linkage).
6. `feat(data-core): durable cancel, heartbeat, single-writer, resume
   hardening` (W1-E — adds heartbeat_at, pid, cancel_requested_at, and
   parent_run_id).
7. `feat(data-core): operator CLI and report extensions` (W1-F).
8. `test(data-core): remediate remaining failure groups` (W3-B — may split
   per group).
9. `feat(data-core): completeness certification generator` (W2).
10. Data-execution commits: certification artifacts and run reports that are
    meant to be tracked (decide trackedness explicitly; large reports stay
    untracked, cert JSON/MD tracked).
11. `feat(agents): S2 CORE phases 1–4` — per the amended S2 plan's own
    boundaries (PolicyConfig; expand_macro; RunBudget; pricing) — plus
    `test(agents): remediate failure groups`.
12. `feat(eval): S4 CORE gate + p0 gate rewrite` plus `test(eval): remediate
    failure groups`.
13. `feat(app): connectivity CORE (staged probe, profiles, doctor)` plus BYOK
    tasks per plan boundaries, plus `test(app): remediate failure groups`.
14. Phase 2+: `feat(data-core): corpus export + manifest`, then vectorization
    tooling, then validation runbook — each separate.

Prerequisites per commit: the per-commit test gate above; zero-write proof
attached (commit 2); real-DB update transcript attached (commit 5's PR
description). Per standing rule: stage only; the human authorizes every
commit.

---

## I. Follow-on Design Documents

| # | Path (proposed) | Objective | Owner |
|---|---|---|---|
| 1 | docs/plans/2026-07-10-w1-unified-update-service-design.md (**written**) | Full contract spec for plan/execute/inspect/cancel/resume, UpdatePlan schema, persisted run-plan artifact, zero-write protocol, two-stage semantics | Fable 5 |
| 2 | docs/plans/2026-07-XX-w1-unified-update-service-implementation.md | File-level tasks, tests, landmines, commit boundaries for W1 (sliced per the design's §10) | dscodex |
| 3 | docs/plans/2026-07-XX-w2-certification-design.md | Cert schema, thresholds proposal, gate wiring | Fable 5 |
| 4 | docs/plans/2026-07-XX-w2-certification-implementation.md | Implementation plan | dscodex |
| 5 | docs/plans/2026-07-XX-w3-test-triage-process.md | Root-cause-group rubric (signature/affected tests/classification/root cause/resolution/verification command/owner+commit), quarantine policy, W3-A vs W3-B split | Fable 5 (rubric) + orchestrator (verification checklist) |
| 6 | docs/plans/2026-07-08-byok-connectivity-amendment.md | Already-designed connectivity CORE as amendment tasks to the BYOK plan | Fable 5 (done in prior session; needs writing to disk) → dscodex tasks |
| 7 | docs/plans/2026-07-XX-w7w8-corpus-export-vectorization-design.md | Export format, manifest chain, embedding config, LanceDB layout, ACTIVE pointer | Fable 5 |
| 8 | docs/plans/2026-07-XX-w8-vectorization-implementation.md | Server-side implementation plan | dscodex |
| 9 | docs/plans/2026-07-XX-w9-server-validation-checklist.md | The 12-item checklist with exact commands and expected outputs | orchestrator (with Fable 5 review) |
| 10 | docs/plans/2026-07-XX-w10-experiment-protocol.md | Ladder, budgets, hash provenance, pilot gates, abort rules | Fable 5 |
| 11 | docs/plans/2026-07-XX-w11-remediation-taxonomy.md | Triage labels, ADR template, regression-pinning rule | Fable 5 |
| 12 | docs/plans/2026-07-XX-phase7-api-outline.md | Later-phase API endpoint outline (kept separate per instruction) | Fable 5, deferred until Phase 6 |

Existing S2/S4 amended plans (docs/plans/2026-07-07-ws4b-s2-harness-honesty.md,
2026-07-07-ws4b-s4-regression-gate-recalibration.md) remain the governing
implementation plans for W4/W5 — no rewrite, only the frozen-fixture demotion
note added.

---

## J. dscodex Handoff Structure

Every dscodex writing-plan prompt must contain these nine sections (the
orchestrator fills them from the design docs above, one phase at a time):

1. **Exact scope** — the workstream slice, with an explicit NOT-list (e.g.,
   "W1 commit 2 only: plan path; do not touch execution loop").
2. **Files to inspect first** — verified paths with the anchor concepts to
   read (e.g., "the dry_run branch in run_update; the trading-day derivation
   in compute_missing_cells; calendar_trading_days in trading_calendar.py").
3. **Files likely to modify** — and files **forbidden** to modify (e.g.,
   frozen DB, retrieval/policy.py during W1).
4. **Required tests** — named test intents from §G, including the landmine
   tests, with the canonical command that must be green.
5. **Forbidden shortcuts** — no loosening asserts, no skip-markers without a
   root-cause group id, no new module-level fallback constants, no writing
   under data/ from plan paths, no catching broad exceptions to force green.
6. **Landmines** — the specific ones from §G for that slice, plus standing
   ones: read-only frozen DB, TraceWriter default db path, .gitignore
   patterns (*.db, *.sqlite, .local/), 8GB-Mac constraint (no local
   embedding), key redaction.
7. **Report format** — claims-with-evidence: every green claim accompanied by
   the exact command and pasted output tail; every created artifact with its
   on-disk path and hash where applicable.
8. **Commit boundary** — which single commit from §H this plan produces;
   staging only, human commits.
9. **Verification hooks for the orchestrator** — what the orchestrator will
   independently re-run (from §K), so the worker knows the audit surface.

---

## K. Orchestrator Verification (per phase, adversarial)

Standing assumptions: reports may contain false greens, non-canonical
environments, stale tests, tautological tests, silent fallbacks, and
runtime-only verification. Standing checks for **every** phase:

- Re-run the canonical pytest command yourself from repo root with
  `.venv/bin/python`; compare counts with the report.
- `git status` + `git diff --stat` — confirm the touched-file set matches the
  plan's modify-list; anything extra is a finding.
- Grep-proofs where the plan promises deletion (e.g., MAX_EXPANSIONS module
  fallback; old metric names) — run the grep yourself.
- Inspect the landing point, not the log: open the actual DB/JSON/report file
  the worker claims to have produced; check row counts / keys / hashes.
- Diff test files: any weakened assertion (removed check, widened tolerance,
  added skip) without a root-cause-group reference is a rejection.

Phase-specific:

- **Phase 1 / W1:** run the zero-write proof personally against a DB copy
  (SHA before/after); attempt a second concurrent run (must refuse); run
  cancel from a separate shell; check `ingestion_runs` columns actually exist
  via sqlite3 PRAGMA table_info.
- **Phase 1 / W2:** regenerate the certification yourself and diff against the
  worker's; spot-check one claimed-complete ticker×month by direct SQL count
  vs calendar day count.
- **Phase 1 / W3-A/W3-B:** pick 3 random root-cause groups; for each,
  reproduce one affected test's original failure on the pre-fix commit, run
  the group's stated verification command on HEAD, and confirm every test the
  group claims is green. Confirm the group inventory sums to the failure count
  at triage time.
- **Phase 1 / W4–W5:** re-run S2/S4 plans' own post-implementation
  verification lists; confirm frozen DB SHA unchanged after all replays;
  fresh-checkout S4 gate run with network monitoring (zero LLM calls).
- **Phase 1 / W6:** run the staged probe against one live provider yourself;
  grep all logs/artifacts for key substrings using a canary key.
- **Phase 2:** verify the manifest chain by recomputing every SHA yourself
  server-side; count LanceDB rows directly.
- **Phase 3:** re-run 3 random checklist items from the transcripts.
- **Phase 4:** verify per-case cost accounting against provider dashboards;
  assert no fallback_surface in any trace by direct trace-DB query.

---

## L. Architect Decisions Required

| # | Decision | Options / proposal | Needed before |
|---|---|---|---|
| 1 | Final ticker universe | Keep the current 10 (proposal: yes, for the portfolio scope) vs expand | W1 execution run |
| 2 | Historical date range | Proposal: 2024-12-30 → rolling latest closed session (matches existing OHLCV start) | W1 execution run |
| 3 | Required providers & SEC forms scope | OHLCV: Polygon primary; fallback order (yfinance vs FMP). News: Polygon + Finnhub. SEC forms list (currently 7 filings — is that the intended scope or a gap?). FRED series list. | W1 design doc |
| 4 | Adjusted vs raw prices | Proposal: adjusted-only CORE | W1 design doc |
| 5 | Certification thresholds & freshness SLA | e.g., OHLCV lag ≤ 1 session; news coverage ≥ N%; propose defaults in W2 design | W2 exit |
| 6 | Server environment | Provider, region (CN vs overseas affects provider reachability per connectivity findings), storage | Phase 2 start |
| 7 | GPU environment | Same box as server vs separate; VRAM class for BGE-M3 batch sizing | Phase 2 start |
| 8 | Embedding model + revision pin; agent + judge models | BGE-M3 rev; judge stays cross-family per prior decision | Phase 2 / Phase 4 |
| 9 | Cost caps | Per-case and per-run USD caps for pilot and full run | Phase 4 |
| 10 | Immutable vs incremental artifacts; macro in corpus | Proposal: immutable full-rebuild v1, incremental STRONG later; macro excluded from vectors v1 | W7 design doc |
| 11 | API scope | Deferred to Phase 6/7 outline | Phase 7 |
| 12 | Frontend scope | Deferred to Phase 8 | Phase 8 |
| 13 | Checkpoint lifecycle + heartbeat constants | Proposed defaults (W1 design §6.6/§6.10): success_empty recheck ≤ 1×, ≥ 1 day after checkpoint, within 5 calendar days of reference_today; permanent-class unresolvable-pending-review at 3 cross-run attempts; chronic-transient flag at 5; heartbeat lease interval 60 s, stale threshold 10 min | W1-C / W1-E implementation plans |

---

## Appendix: Verified Evidence Anchors

- `compute_missing_cells` (update_pipeline.py): trading days via
  `_trading_days_in_window` → `trading_days_for_window` (ohlcv dates unioned
  with the calendar oracle — day-set truncation already fixed); default
  universe `DISTINCT symbol FROM ohlcv` (not fixed); failed cells re-included
  as missing.
- `run_update` dry-run branch: `init_db` + `ensure_ingestion_quality_tables` +
  `open_ingestion_run` + `save_run_report` + status update — writes on the
  "dry" path.
- `run_update_batch` dry-run branch: no run row/report, but still `init_db` on
  connect.
- Default window in both: `latest_local_ohlcv_date` (freshness.py) — the
  stale-watermark coupling.
- `request_cancel`: module-level in-process set (`_CANCEL_REQUESTS`), checked
  in the executor cell loop; not cross-process.
- OHLCV ingestion: only in scripts/backfill.py (`POLYGON_ENDPOINTS` includes
  ohlcv) + backfill_pipeline.py; absent from the run_update source contract.
- Calendar primitives already present: trading_calendar.py
  (`calendar_trading_days`, `latest_closed_trading_day_for_date`,
  `ohlcv_bounds`).
- Freshness surface: freshness.py (`news_freshness`, `index_freshness`,
  `filings_freshness`, `macro_freshness`, `freshness_report`).
- Fallback/error machinery: fallback.py (FallbackPolicy), error_taxonomy.py,
  retry.py, provider_limits.py, rate_limiter.py.
- Connectors present: polygon, finnhub, fmp, fred, sec, yfinance_fallback.
- Dedup: dedup/cross_source.py, dedup/hard.py. Quality/eligibility:
  quality.py, eligibility.py. Doctor with secret redaction: doctor.py.
- App package: dependencies.py, env_loader.py, llm_factory.py,
  model_catalog.py, provider_validator.py, runtime_credential_store.py,
  schemas.py, workbench_store.py, workspace_projection.py, routers/.

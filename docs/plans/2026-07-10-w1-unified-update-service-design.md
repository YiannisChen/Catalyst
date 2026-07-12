# W1: Unified Market/Evidence Update Service — Architecture Design

**Role:** Architecture design (Fable 5). No implementation code in this
document. Implementation plans are written later by dscodex, one slice at a
time (§10).
**Governs:** Roadmap workstream W1 plus its serialized neighbors W3-A, W3-B,
W2 (docs/plans/2026-07-09-backend-first-roadmap.md, §B Phase 1, §D).
**Process rule:** Per CLAUDE.md, no code blocks. Contracts are tables and
prose with conceptual anchors.
**Verification statement:** Every existing path, symbol, and data fact cited
below was verified on disk / in the Dev DB (read-only) on 2026-07-09. Items
that do not exist yet are marked **(proposed)**. Behavior changes to existing
code are marked **(behavior change)**. Schema changes are marked
**(schema change)**.
**Amended 2026-07-10:** per-ticker watermark control (§4.3, §5.1); persisted
run-plan artifact (§5.5); plan-based resume (§6.8); lease heartbeat (§6.6);
checkpoint lifecycle state machine with concrete constants (§6.10);
per-slice test gates (§10).

---

## 1. Scope and Non-Goals

**In scope:** one internal backend domain for updating OHLCV/K-lines, Polygon
news, Finnhub news, SEC filings, and FRED macro — planning, execution,
status, cancellation, resume, reporting — with OHLCV-first dependency order,
true zero-write planning, durable cross-process control, and an operator CLI.

**Non-goals (deferred, per roadmap Phases 7–8):** formal HTTP job APIs,
frontend update pages, progress/ETA UI, WebSocket streaming, multi-machine
workers. The service contract in §4 is designed so a future API can wrap it
verb-for-verb, but nothing here depends on that happening.

---

## 2. Verified Current State

### 2.1 Existing code inventory (all verified)

| Concern | Where it lives today | State |
|---|---|---|
| Missing-cell planning | `compute_missing_cells` in `packages/data-core/catalyst_data/update_pipeline.py` | Works; checkpoint-driven (failed ⇒ re-included); universe defaults to `DISTINCT symbol FROM ohlcv`; trading days via `_trading_days_in_window` → `trading_days_for_window` (calendar ∪ ohlcv) |
| Trading calendar | `packages/data-core/catalyst_data/trading_calendar.py` — `calendar_trading_days`, `latest_closed_trading_day_for_date`, `trading_days_for_window`, `ohlcv_bounds` | Dependency-free weekday-minus-holiday oracle; `US_MARKET_HOLIDAYS` frozenset covers **2025–2027 only** (see landmine §6.3) |
| Update entrypoint | `run_update(config)` in update_pipeline.py | Real cell loop, fallback, progress, report persistence. Defects: dry-run writes (§2.2); window defaults to `latest_local_ohlcv_date` for both ends |
| Batch/backfill engine | `run_update_batch` (update_pipeline.py), `run_backfill` (backfill_pipeline.py — chunks a window into `run_update_batch` calls) | Works; separate from `run_update`; no run rows per chunk |
| OHLCV ingestion | Only in `packages/data-core/scripts/backfill.py` (POLYGON_ENDPOINTS includes ohlcv) + `orchestrator.py` calling `upsert_ohlcv` | Not a source in `run_update`'s cell-handler set (`_fetch_cell`, `_fetch_cell_finnhub`, `_fetch_cell_sec`) |
| OHLCV storage | `upsert_ohlcv` in `catalyst_data/storage/sqlite.py` — INSERT OR REPLACE; `ohlcv` table PK (symbol, date), `source TEXT DEFAULT 'polygon'` | Last-write-wins; no provider precedence |
| Per-cell atomicity | `persist_cell` in update_pipeline.py — one connection, BEGIN IMMEDIATE, raw_asset + checkpoint in one transaction; split rollback semantics per error class | Sound; reuse as-is |
| Run rows / checkpoints | `catalyst_data/quality.py` — `open_ingestion_run`, `close_ingestion_run`, `write_source_checkpoint`, `close_stale_runs` (age-based, 24 h → 'interrupted'); DDL for `ingestion_runs` and `source_checkpoints` (with `fallback_provider`, `fallback_triggered`, ALTER-based ensure migrations) | Sound base; lacks heartbeat/pid/durable-cancel/stage/parent columns |
| Cancel | `request_cancel` in update_pipeline.py — module-level `_CANCEL_REQUESTS` set, checked between cells | In-process only |
| Config/report | `RunConfig`, `RunReport`, `save_run_report` in `catalyst_data/run_report.py` | RunReport already carries S3 §0.7 fields (watermarks, dedup, embedded/queued/pending counts) |
| Freshness | `catalyst_data/freshness.py` — `latest_local_ohlcv_date`, `news_freshness`, `index_freshness`, `filings_freshness`, `macro_freshness`, `freshness_report` | Works; `latest_local_ohlcv_date` is the stale-watermark coupling point |
| Fallback | `catalyst_data/fallback.py` — `FallbackPolicy` with single `DEFAULT_FALLBACK_CHAIN = (polygon_news, finnhub_company_news)`, `max_fallback_depth=1`, excluded error classes | News-only chain; no per-source-type chains |
| Errors/retry/limits | `error_taxonomy.py` (12-class `ErrorClass`), `retry.py`, `provider_limits.py` (per-provider policies from `config.RATE_POLICIES`), `rate_limiter.py` | Reuse as-is |
| Connectors | `catalyst_data/connectors/` — polygon (has `_build_ohlcv_url`, single-day aggs, **no explicit adjusted param** today), finnhub, sec, fred, fmp, yfinance_fallback (`create_yfinance_fetcher`) | OHLCV fetch capability exists on both primary and fallback sides |
| Migrations | `catalyst_data/migrations.py` (H4 registry) + `scripts/migrate_ingestion_quality.py`; quality.py also self-ensures columns via guarded ALTERs | Two mechanisms; W1 schema changes must go through the registered path (implementation plan verifies the exact registration API) |
| Frozen-DB guard | FROZEN_PATHS realpath guard (S3, committed in d10e71c) in storage/sqlite.py | Planner and executor must both stay behind it |
| Doctor | `catalyst_data/doctor.py` with `_redact_secrets` | Extend for orphan detection |

### 2.2 Verified defects W1 exists to fix

1. **False dry-run.** The dry-run branch of `run_update` calls `init_db`,
   `ensure_ingestion_quality_tables`, `open_ingestion_run`, `save_run_report`,
   and updates the run row — five write categories on a path whose module
   docstring promises "ZERO network, ZERO DB writes". `run_update_batch`'s
   dry-run branch is closer (no run row, no report) but still runs `init_db`
   on connect. Two planning contracts exist where the architecture needs one.
2. **Stale-watermark defaults.** Both entrypoints default `from_date` and
   `to_date` to `latest_local_ohlcv_date` — with OHLCV at 2026-05-01, a
   default run plans exactly one stale day. The universe defaults to ohlcv
   symbols. Expectation must come from configuration + calendar, never from
   the table being updated.
3. **OHLCV is not first-class.** No ohlcv cell handler in `run_update`; no
   OHLCV fallback chain; `upsert_ohlcv` is last-write-wins.
4. **Control is not durable.** Cancel is in-process; no single-writer
   enforcement; orphan detection is a blunt 24-hour age rule.

### 2.3 Verified Dev DB facts that constrain the design

Read-only queries against `data/catalyst_dev_ws4b.db` (2026-07-09):

- `source_checkpoints.source_type` tokens in the wild:
  `polygon_ohlcv` (3,349 success / 23 failed / 150 skipped),
  `polygon_news` (3,577 / 410 / 362), `finnhub_company_news` (420 success),
  `sec_filings` (69 success), `fmp_fundamentals` (3,500 / 22).
  **Consequence:** the OHLCV cell token is settled — `polygon_ohlcv` already
  exists and exactly matches the 3,349 ohlcv rows. No token migration needed.
- `ohlcv.source` = 'polygon' for all rows.
- `ingestion_runs.status` values in the wild: `success`, `completed`,
  `interrupted` — while code writes `succeeded` (dry-run branch) and
  `completed` (close default). **Consequence:** the status vocabulary is
  inconsistent and must be canonicalized read-side (§5.3), not by rewriting
  history.
- Finnhub: 22,941 article rows but only 420 success checkpoints.
  **Consequence:** checkpoint coverage ≠ content coverage. Planning stays
  checkpoint-driven (safe: upserts are idempotent, re-fetch is wasteful but
  correct); reconciling "content present without checkpoint" is a
  certification (W2) concern, and plan estimates must be labeled as
  checkpoint-derived upper bounds.

---

## 3. Alternatives Analysis

### Option 1 — Thin service facade over the existing update_pipeline

A new `update_service.py` exposing the five verbs, delegating to
`run_update` / `compute_missing_cells` / `request_cancel` unchanged.

- Pro: smallest diff; no behavior risk to the working cell loop.
- Con: **cannot deliver zero-write planning.** The plan-relevant logic
  (window resolution, universe resolution, run opening) is embedded inside
  the write path of `run_update`. A facade would have to duplicate that logic
  to plan without writing — two implementations of "what should we do", which
  is exactly the drift that produced today's `run_update` vs
  `run_update_batch` split. Also cannot deliver durable cancel or two-stage
  ordering without touching the loop anyway.

### Option 2 — Refactor update_pipeline into planner/executor modules

Extract planning (universe, window, calendar expectation, missing cells,
estimates, hash) into a pure read-only planner module; reshape the executor
so it consumes an explicit cell list; keep `persist_cell`, `_fetch_cell*`,
checkpoint/report machinery where they are.

- Pro: single source of planning truth; the read-only property becomes
  structural (the planner never holds a writable connection); executor
  surgery is bounded to its entry (accept cells + run context) and its loop
  (durable cancel + heartbeat checks).
- Con: touches the most load-bearing module in data-core; needs the
  trustworthy test baseline first (hence W3-A before W1).

### Option 3 — Full job/queue abstraction

A persistent job table or broker (queue, worker daemon, job states,
serialized job payloads), with update runs as jobs.

- Pro: would generalize to multi-tenant/multi-worker futures.
- Con: **YAGNI on every axis.** One operator, one machine, one SQLite writer
  by design. `ingestion_runs` already *is* the durable job table — it has
  run_id, status, progress, config JSON, and report path. A broker adds
  serialization contracts, delivery semantics, and a daemon lifecycle — three
  new failure modes — to serve a concurrency level of one. The future HTTP
  API (Phase 7) wraps the five verbs directly; it does not need a queue to do
  so, because SQLite's single-writer policy is the queue.

### Recommendation

**Option 1 + selective Option 2: a thin facade plus extraction of the planner
only.** The facade (`update_service.py`, proposed) is the sole public
contract. The planner (`update_planner.py`, proposed) is extracted because
zero-write planning is impossible without it. The executor is *reshaped in
place* inside `update_pipeline.py` (entry signature + loop checks), not moved
to a new module — `persist_cell`, the fetch handlers, fallback wiring, and
report assembly all stay put. No rewrite; no queue. This matches the stated
preference and the evidence: everything below the planning seam already
works and is checkpoint-tested in production use (11,882 checkpoints).

---

## 4. Target Architecture

### 4.1 Module map

| Module | Status | Responsibility |
|---|---|---|
| `catalyst_data/update_planner.py` | **(proposed)** | Pure planning: universe resolution, calendar expectation, window resolution, per-source missing cells, request/duration estimates, warnings, plan hash. Opens the DB **read-only** (SQLite URI mode=ro). Never imports anything that writes. |
| `catalyst_data/update_service.py` | **(proposed)** | The five-verb facade (§4.2). Owns run lifecycle: single-writer acquisition, two-stage orchestration, staleness re-plan, durable cancel polling contract, report assembly delegation. Thin: it sequences, it does not fetch or persist. |
| `catalyst_data/update_pipeline.py` | existing, **reshaped** | Executor: cell loop (accepts explicit cell list + run context), fetch handlers incl. new OHLCV handler, `persist_cell`, fallback execution. Loses: planning defaults, dry-run branches (delegated), direct entrypoint status. |
| `catalyst_data/run_report.py` | existing, **extended** | RunConfig gains service-level fields; RunReport gains additive fields (§5.4). |
| `catalyst_data/quality.py` | existing, **extended** | Run-row lifecycle gains heartbeat/cancel/stage/parent columns support; `close_stale_runs` upgraded to heartbeat-based. |
| `catalyst_data/config.py` | existing, **extended** | Gains the explicit ticker universe constant + provenance labels (architect decision #1 supplies values). No new module for this. |
| `catalyst_data/fallback.py` | existing, **extended** | Per-source-type chains (§6.5). |
| `catalyst_data/trading_calendar.py` | existing | Unchanged API; holiday-coverage guard added (§6.3). |
| `scripts/update.py` | **(proposed)** | Operator CLI over the five verbs (§8). |
| `scripts/backfill.py`, `backfill_pipeline.py` | existing, **demoted** | Legacy operator path; kept working during W1, formally deprecated in W1-F (§9.3). Window-chunking survives as an executor option. |

### 4.2 The five verbs (public internal contract)

| Verb | Signature (conceptual) | Writes | Notes |
|---|---|---|---|
| plan_update | RunConfig → UpdatePlan | **none** (proof: §7) | Pure; deterministic for fixed (DB content, config, today) |
| execute_update | RunConfig, expected_plan_hash?, strict? → RunReport | run row, **persisted run-plan artifact (§5.5, written before the first cell)**, checkpoints, bronze/silver rows, report file | Re-plans at start; hash mismatch under strict → PlanDriftError, no run row created |
| inspect_update | run_id → RunStatus | none | Derived from ingestion_runs + source_checkpoints; canonicalizes legacy statuses read-side |
| cancel_update | run_id → ack | one UPDATE (cancel_requested_at) | Cross-process; §6.7 |
| resume_update | parent run_id → RunReport | as execute (including its own run-plan artifact) | New run; **cells = parent's persisted planned cells minus parent's success/success_empty checkpoints** (§6.8) — recovers failed, skipped, and never-attempted alike; stage inferred per cell from source_type; parent_run_id recorded |

Design rulings embedded in the table:

- **Execution re-plans; plans are previews.** A plan computed at time T is
  advice; the executor recomputes at start. `expected_plan_hash` + strict
  mode gives the future preview→confirm UI its safety without pretending
  plans are immutable inputs. Non-strict mode logs the drift and proceeds
  (operator convenience for slow-moving sources).
- **resume_update takes no date window and never re-plans from the current
  DB/time** — its cell set is defined entirely by the parent's persisted
  run-plan artifact minus the parent's success/success_empty checkpoints
  (§6.8). Re-planning at resume time is explicitly rejected: it would
  silently drop never-attempted cells whose window has since moved.
- All verbs are synchronous in-process calls. "Background" execution is the
  operator running the CLI in a shell or a future API worker calling
  execute_update on a thread — the service itself stays synchronous
  (Option 3 rejection, §3).

### 4.3 Two-stage execution semantics

A full "Evidence Data Update" is **one run_id with two internal stages**:

| | Stage 1: market | Stage 2: evidence |
|---|---|---|
| Sources | polygon_ohlcv (fallback: yfinance ohlcv) | polygon_news, finnhub_company_news, sec_filings, fred (per config) |
| Planned | exactly, at plan time | **provisionally** at plan time (each ticker's window capped at that ticker's *predicted* post-stage-1 watermark), **finally** after stage 1 completes, against each ticker's *actual* re-derived watermark |
| Recorded | plan.stages[market] cells + counts | plan.stages[evidence] provisional counts; RunReport carries provisional vs final counts |

Rules:

- Stage 2 final planning runs in-process immediately after stage 1; there is
  no hash re-check between stages (same process, no TOCTOU window that
  matters).
- Stage 1 failures don't automatically abort stage 2; the gate is the
  **per-ticker watermark**, not the failure count: each ticker's stage-2
  window ceiling is min(that ticker's re-derived OHLCV watermark, latest
  closed session). One ticker's stuck provider lowers only that ticker's
  ceiling; the rest of the universe proceeds to its own watermarks. **The
  global watermark (min over the universe) is reporting/certification
  metadata only and never gates any ticker's evidence cells.** Non-ticker
  sources (FRED macro) take no ticker ceiling — their window ceiling is the
  latest closed session. If a ceiling would move backward relative to
  already-checkpointed evidence, stage 2 no-ops those cells
  (checkpoint-driven planning makes this automatic).
- `--allow-stale-ohlcv` (config flag): skips stage 1 and uses each ticker's
  currently derived watermark as its ceiling, recorded in both plan warnings
  and the RunReport. Explicit, never silent.
- Single-source invocations (e.g., sources=[polygon_ohlcv] only, or
  news-only with allow_stale_ohlcv) degenerate to one stage; the stage
  machinery is the general case, not an obligation.
- Checkpoints need **no stage column**: stage is a pure function of
  source_type (market vs evidence source sets). The run row's `stage` field
  (§5.3) is progress reporting, not source of truth.

---

## 5. Data Contracts

### 5.1 UpdatePlan (proposed object, serializable to canonical JSON)

| Field | Content |
|---|---|
| plan_schema_version | int, starts at 1 |
| created_at | ISO-8601 UTC (excluded from hash) |
| db | path, file SHA-256, sqlite user_version |
| config | canonical echo of RunConfig planning-relevant fields (tickers, sources, window, limit, enable_fallback, allow_stale_ohlcv) |
| universe | resolved ticker list + provenance: "explicit-config" \| "ohlcv-derived-fallback" (the fallback is allowed but always labeled; certified runs require explicit) |
| reference_today | the date used for latest-closed-session resolution (injectable for tests; defaults to today) |
| latest_closed_session | from `latest_closed_trading_day_for_date` |
| stages.market | resolved window; expected sessions (calendar-derived); missing cells (ticker, date, source) with count; per-source counts |
| stages.evidence | provisional per-ticker windows (each ticker's ceiling = that ticker's predicted post-stage-1 watermark; macro sources capped at latest closed session only); provisional missing cells + counts, labeled provisional; per-source counts; global watermark included as metadata, never as a cap |
| estimates | per-source provider request counts (1 request per cell for polygon_ohlcv/polygon_news/finnhub; SEC amortized by the submissions cache — labeled approximate); duration range = counts ÷ rate-policy throughput from `provider_limits.py`, min/max across sources; predicted fallback exposure (cells whose primary has recent failed checkpoints with transport-class errors); predicted index delta (upper bound = evidence cells, labeled upper-bound and checkpoint-derived per §2.3) |
| warnings | list of typed warnings: stale-ohlcv-override, universe-fallback, calendar-holiday-coverage-gap (§6.3), schema-behind (never fatal in a warning — fatal is SchemaOutOfDate), watermark-behind-news (the current DB state) |
| plan_hash | SHA-256 over the canonical JSON of everything above except created_at (§5.2) |

### 5.2 Plan hash and re-plan semantics

- Canonicalization: sorted keys, no whitespace variance, lists in
  deterministic order (cells sorted by source, ticker, date), floats
  round-tripped at fixed precision, db identified by content SHA (not path).
- Determinism contract: identical (DB content, config, reference_today) →
  identical hash. Two plans on different days legitimately differ (the
  latest closed session moved); this is captured, not suppressed.
- Sensitivity contract (tested): flipping one checkpoint row changes the
  hash; changing the universe changes the hash; renaming the DB file does
  not.
- Execution: strict mode recomputes and requires equality **before** any
  write (no run row on mismatch — PlanDriftError). Non-strict logs a
  structured drift note into the RunReport.

### 5.3 RunStatus (proposed read-model, no new storage)

Derived entirely from `ingestion_runs` + `source_checkpoints`:

| Field | Source |
|---|---|
| run_id, started_at, ended_at, report_path | run row |
| status | canonicalized enum: running \| succeeded \| failed \| partial \| canceled \| interrupted. Read-side mapping table absorbs legacy tokens (`success`, `completed`, `succeeded`) — **no historical rewrite** |
| stage | run row (new column), one of market \| evidence \| — |
| progress | cells_total, cells_done, current_source/ticker/date (existing columns) |
| per-status cell counts | GROUP BY on source_checkpoints for this run_id |
| heartbeat_at, pid | new columns (§6.6) |
| cancel_requested_at, canceled_at | new + existing columns |
| parent_run_id | new column |
| eta_range | derived: remaining cells × observed mean cell latency this run (floor), remaining ÷ rate-policy throughput (ceiling); null until ≥ 5 cells done. CORE is this linear model only |
| plan_hash, plan_path | new columns (§5.5) |
| last_error | most recent failed checkpoint's (error_class, redacted message) |

### 5.4 RunReport (existing dataclass, additive extension)

Existing fields kept verbatim (run_id, mode, config, providers,
retry_histogram, top_error_classes, fallbacks, rows_changed, index_state,
doctor, S3 §0.7 observability fields including watermarks and dedup).
Additive **(behavior change: new keys in saved JSON; consumers must tolerate
unknown keys — verified consumers are report tests and freshness tooling,
inventoried in the W1-F implementation plan):**

| New field | Content |
|---|---|
| plan_hash | hash the run executed under (post re-plan) |
| plan_drift | null or structured note (strict=false path) |
| stages | per stage: provisional cells, final cells, executed, success/empty/failed/skipped |
| universe_provenance | as in plan |
| allow_stale_ohlcv | bool echo |
| watermark_before / watermark_after | per-ticker + global (feeds the existing `watermarks` dict; exact shape reconciled in implementation plan). Per-ticker values are the control surface; the global min is metadata |
| plan_path | path of the persisted run-plan artifact (§5.5) |
| backup_path | if the CLI took a pre-run backup |

### 5.5 Persisted run-plan artifact (proposed)

Every executing run persists the exact plan it runs under **before the first
cell executes**: `data/run_plans/<run_id>.json` **(proposed directory,
untracked like data/run_reports)**, plus two new `ingestion_runs` columns
**(schema change, joins the W1-E migration)**: `plan_hash TEXT`,
`plan_path TEXT`.

Content: the full UpdatePlan JSON (§5.1) as re-planned at execution start —
stage-1 cells exact, stage-2 cells provisional — plus run_id and, for
resumes, parent_run_id and the resume cell set.

**Atomic ordering (normative):**

1. Re-plan; strict hash check (PlanDriftError here → nothing written at all).
2. Open the run row (BEGIN IMMEDIATE acquisition, §6.6) with `plan_hash` set
   and `plan_path` null.
3. Write the artifact: temp file in data/run_plans/, fsync, atomic rename to
   `<run_id>.json`.
4. UPDATE the run row's `plan_path`; commit.
5. Only now may the first cell execute. **Invariant: no cell ever executes
   under a run whose plan_path is null.**

**Failure behavior:** if step 3 or 4 fails, the run row is closed as
`failed` with zero cells attempted and the error surfaced; such a run is not
resumable (there is nothing planned to resume — `resume_update` refuses with
a clear error). A crash between 2 and 4 leaves a running row with null
plan_path and zero checkpoints; orphan handling (§6.6) marks it interrupted,
and resume refuses it for the same reason — re-execute from config instead.

**Stage-2 amendment point:** when stage-2 final planning completes
(mid-run), the artifact is rewritten once via the same temp+fsync+rename
mechanism, adding the `stage2_final` section **before the first stage-2 cell
executes**. After that single defined amendment, the artifact is immutable;
resume reads stage-1 cells and stage-2 final cells from it (fallback to
stage-2 provisional never happens — see §6.8 for the
crash-before-stage-2-final case).

---

## 6. Domain Designs

### 6.1 Calendar-derived expectation

Planning-time expected sessions come **only** from
`calendar_trading_days(from, to)`. The ohlcv-unioned variant
(`trading_days_for_window`) remains for read-side alignment uses (its
original purpose), but the planner does not call it for expectation: rows
already present must never define what should exist (roadmap correction #4).
Completeness tri-state per (ticker, expected session): bar present /
success_empty with valid reason / architect-approved unresolvable —
implemented in the planner (for planning) and certification (for the gate).

### 6.2 Explicit ticker universe

The universe is configuration: a constant in `catalyst_data/config.py`
(extended; values are architect decision #1), threaded through RunConfig.
`compute_missing_cells`'s ohlcv-derived default survives only as a labeled
fallback (provenance "ohlcv-derived-fallback"); plan warnings flag it;
certification requires "explicit-config". Rationale for config-not-DB: 10
tickers, changes are architect decisions, and a DB table would need its own
provenance story.

### 6.3 Calendar coverage landmine (verified)

`US_MARKET_HOLIDAYS` covers 2025–2027 only, but the corpus starts 2024-12-30.
Any window reaching into 2024 would silently treat 2024 holidays as expected
sessions (2024-12-30/31 happen to be real trading days, so today's exact
bounds are safe — by luck). Design rule: the planner computes the window's
year span and, if any year is outside the oracle's coverage set, emits a
fatal-by-default CalendarCoverageError (downgradeable to a warning with an
explicit flag). The frozenset gains 2024 entries as part of W1-B (data
addition, not a design change).

### 6.4 OHLCV source design

| Concern | Ruling |
|---|---|
| Cell token | `polygon_ohlcv` (already in the wild, 3,349 matching checkpoints — §2.3) |
| Primary | Polygon single-day aggs via the existing `_build_ohlcv_url`; **(behavior change)** add explicit adjusted=true param — today the URL relies on Polygon's default; pin it (architect decision #4: adjusted-only CORE) |
| Fallback | `yfinance_fallback.create_yfinance_fetcher` behind a per-source chain (§6.5); provider recorded in `ohlcv.source` and checkpoint `fallback_provider` |
| Provider precedence | **(behavior change)** `upsert_ohlcv` becomes precedence-aware: replace only when precedence(incoming) ≥ precedence(existing); polygon=2, yfinance=1 (fmp=1 if ever enabled). Kills the last-write-wins hazard where a fallback row overwrites a primary row on re-run |
| success_empty | A valid session with a legitimately empty provider response (halt, pre-listing, provider-confirmed no-bar) checkpoints as success_empty with a machine-readable reason in a new nullable `source_checkpoints.empty_reason` column **(schema change)** — the existing error_class field stays error-only; reasons are not errors |
| Empty re-check | governed by the lifecycle state machine in §6.10 (recheck at most once, ≥ 1 day after the checkpoint, only for cells within 5 calendar days of reference_today) |
| Duplicates/upsert | PK (symbol, date) unchanged; idempotent by construction |
| Missing-session retries | failed ⇒ re-included (existing); permanent-class exclusion and terminal review governed by §6.10 |
| Splits/dividends | adjusted-only v1; raw+adjusted dual storage is STRONG and out of W1 |
| Executor wiring | new ohlcv cell handler alongside `_fetch_cell_finnhub`/`_fetch_cell_sec`, persisting through the existing `persist_cell` storage_callable seam (raw forensic bytes + `upsert_ohlcv` in the same transaction) |

### 6.5 Fallback provenance and per-source chains

`FallbackPolicy` **(extended)** moves from one global chain to a mapping of
source_type → chain: polygon_ohlcv → (polygon_ohlcv, yfinance-ohlcv-endpoint);
polygon_news → (polygon_news, finnhub_company_news) (existing default
preserved). max_fallback_depth=1 and excluded-error-class semantics kept.
Provenance is already recorded (checkpoint `fallback_provider`,
`fallback_triggered`); OHLCV adds row-level provenance via `ohlcv.source`.

### 6.6 Single-writer enforcement, heartbeat, orphan detection

**(schema change)** `ingestion_runs` gains: `heartbeat_at TEXT`, `pid
INTEGER`, `cancel_requested_at TEXT`, `stage TEXT`, `parent_run_id TEXT`,
`plan_hash TEXT`, `plan_path TEXT` (the last two per §5.5) — additive,
nullable, via the H4 migration mechanism (`migrations.py`; the
implementation plan verifies the registration API and keeps quality.py's
guarded-ALTER ensure in sync).

- **Acquisition:** opening a run performs check-and-insert inside one BEGIN
  IMMEDIATE transaction: if another row has status running AND heartbeat_at
  within the stale threshold → WriterConflictError. BEGIN IMMEDIATE
  serializes the check against races (SQLite write lock).
- **Heartbeat is a lease, not a progress side effect.** Per-cell progress
  writes alone would stall the heartbeat during a long provider call, a
  retry ladder, or a rate-limit wait — a live run would look orphaned
  mid-cell. Design: an asyncio background task (the executor loop is already
  async) updates `heartbeat_at` every **60 seconds** on its own short-lived
  connection with a busy timeout, independent of cell progress; a missed
  tick is logged, never fatal. Cell-progress writes (`_mark_progress`)
  refresh `heartbeat_at` too (free ride), but the lease is the guarantee.
  Stale threshold = **10 minutes** (10 consecutive missed beats — generous
  against SQLite busy contention and scheduler hiccups). Constants are
  architect decision #13 (proposed defaults).
  *Rejected alternative:* deriving the threshold from a bounded maximum cell
  duration — provider `retry_after` waits are not bounded by us, so the
  bound would have to be enforced by clamping provider-directed waits, a
  behavior change with real cost; the lease task is one small async task
  with no new failure semantics. Smallest reliable design wins.
- **Orphans:** a running row with a lease heartbeat older than the stale
  threshold is an orphan. Upgrade `close_stale_runs` from 24-hour age to
  heartbeat-staleness → status interrupted; wire it into `doctor.py` checks
  AND into acquisition (a conflicting run that is provably orphaned is
  auto-interrupted, then acquisition proceeds). The 24-hour age rule
  survives as a backstop for pre-migration rows with null heartbeat.

### 6.7 Durable cancellation

`cancel_update(run_id)` writes cancel_requested_at (works from any process).
The executor checks, per cell, both the legacy in-process `_CANCEL_REQUESTS`
set (fast path, kept) and the column — the read piggybacks on the same
per-cell connection the progress write uses. Existing drain semantics kept:
remaining cells checkpointed skipped with error_class canceled; run status →
canceled; canceled_at set. Contract: cancellation takes effect within one
cell boundary, never mid-transaction.

### 6.8 Crash resume (plan-based, normative)

Unchanged core invariant: `persist_cell` writes raw_asset + checkpoint in one
transaction, so a crash leaves a cell fully recorded or absent.

**Resume definition:** the resume cell set is

  parent's persisted planned cells (from the §5.5 artifact)
  minus parent's cells with a success or success_empty checkpoint.

This single subtraction recovers all three loss categories at once: failed
cells, skipped cells (including cancel-drained ones), and never-attempted
cells (planned but no checkpoint at all). **Re-planning from the current
DB/time is not an acceptable substitute** — the window would have moved and
never-attempted cells would silently vanish from the recomputed plan.

Mechanics: resume opens its own run (own acquisition, own §5.5 artifact
whose planned-cells section is exactly the computed resume set, with
parent_run_id linkage in both the column and the artifact); stage per cell
is inferred from source_type; execution order is stage-1 cells before
stage-2 cells. Resume of a resume chains naturally (each run's artifact is
its own plan). Cells another run has meanwhile completed re-fetch
idempotently (upsert semantics) — wasteful but correct, and rare.

**Crash-before-stage-2-final case:** if the parent died before the stage-2
final section was persisted (§5.5 amendment point), the parent's artifact
contains stage-1 cells plus a provisional stage-2 section. Resume executes
the remaining stage-1 cells, then performs stage-2 *final* planning itself —
exactly as the parent would have — under the parent's persisted config,
against the post-stage-1 per-ticker watermarks, and persists it into its own
artifact. This is not "re-planning as a substitute": the stage-2 final plan
never existed to recover; producing it at that point is the two-stage
contract itself. The provisional section is never executed.

Kill-drill verification is fixture-DB + child-process only (roadmap
correction #5).

### 6.9 Retry/error taxonomy

No redesign. OHLCV cells flow through the existing 12-class `ErrorClass`
decision table; yfinance exceptions already map through
`_status_for_yfinance_exception`. The only addition is the mapping note that
an empty aggregate result on a valid session is EMPTY_VALID (existing
`persist_cell` semantics) with §6.4's empty_reason recorded.

### 6.10 Checkpoint lifecycle: success_empty and permanent failures

All constants below are concrete proposed defaults (architect decision #13);
none are left as undefined N/K. Attempt counting is **cross-run**: the
planner counts checkpoints for a (source_type, ticker, date) cell across all
run_ids (read-only query), never per-run.

**success_empty lifecycle:**

| State | Definition | Transition |
|---|---|---|
| empty-once | exactly 1 success_empty checkpoint for the cell | eligible for recheck iff (cell date ≥ reference_today − 5 calendar days) AND (≥ 1 day has elapsed since the checkpoint). Recheck = the planner re-includes the cell in the next plan; there is no scheduler. Cells older than the 5-day window skip recheck and go terminal directly |
| terminal valid-empty | 2 success_empty checkpoints, or 1 checkpoint with the recheck window expired | never re-planned; certifies as tri-state (b) with its recorded empty_reason |
| superseded | a later success checkpoint exists (data landed on recheck) | ordinary bar-present cell; certifies as tri-state (a) |

Recheck count is exactly 1 — a second empty answer is an answer.

**Failure lifecycle:**

| State | Definition | Transition |
|---|---|---|
| retrying | failed checkpoints only, any class, attempts < limits below | re-included in every plan (existing failed ⇒ missing rule) |
| unresolvable-pending-review | ≥ 3 cross-run attempts, all with permanent-class errors (per `error_taxonomy.py` decision table: auth, permission_paid, and other terminal classes) | excluded from further plans; listed in plan warnings and in the certification's pending-review section with (cell, error_class, attempt count, last redacted message) |
| unresolvable (terminal) | architect approves a pending-review entry; the approval is recorded **in the certification artifact** (cert section, signed list) — no DB write, no new table | certifies as tri-state (c) |
| chronic-transient | ≥ 5 cross-run attempts with transient-class errors | **never auto-excluded** (transient means retry is legitimate); flagged in the certification for operator investigation; blocks certification until it resolves or the architect approves it as unresolvable |

**Certification inputs (consumed by W2):** terminal valid-empty and approved
unresolvable satisfy the tri-state; empty-once (recheck pending),
unresolvable-pending-review, and chronic-transient are **blocking** states —
a certification run with any cell in a blocking state is NOT CERTIFIED until
the cell resolves or the architect signs it.

---

## 7. True Zero-Write Planning

Structural, not conventional:

1. The planner opens the DB via SQLite URI mode=ro; every write attempt
   raises at the driver. It never calls `init_db` or
   `ensure_ingestion_quality_tables`.
2. Schema staleness is a **failure, not a migration**: required tables/columns
   checked via sqlite_master/PRAGMA; missing → SchemaOutOfDate naming the
   migration command. Planning never mutates schema.
3. No run row, no report file. Plan output goes to the caller; the CLI prints
   or writes to an explicit --out path (outside data/ by default).
4. Old paths **(behavior change)**: `run_update(dry_run=True)` and
   `run_update_batch(dry_run=True)` delegate to plan_update and return a
   report-shaped adapter (mode plan-preview, empty run_id, no persistence),
   with a deprecation warning. Existing tests asserting dry-run run rows /
   report files become a named contract-drift group resolved in W3-B. One
   release later (post-W2), the dry_run parameters are removed outright.

**Proof protocol** (an automated test plus one manual transcript for the W1
exit gate): snapshot SHA-256 of the DB file and any -wal/-shm siblings plus a
recursive (path, size, mtime) listing of data/ scoped to the DB and
run_reports; run plan_update twice with a socket-blocking guard installed;
assert byte-identical DB, no new/modified files, identical plan_hash across
the two runs. Run against a fixture and once, manually, against a copy of the
real Dev DB.

---

## 8. Operator CLI

`scripts/update.py` **(proposed)** — subcommands mapping 1:1 onto the verbs:

| Subcommand | Behavior |
|---|---|
| plan | prints human summary + optional --json/--out; exits nonzero on SchemaOutOfDate/CalendarCoverageError |
| run | executes; flags: --strict-plan-hash HASH, --allow-stale-ohlcv, --sources, --tickers, --from/--to, --limit, --chunk-days (delegates window chunking to the executor option that absorbs backfill_pipeline's behavior), --backup (pre-run SHA-pinned copy; prints backup_path) |
| status | RunStatus for run_id or --latest; --watch polls |
| cancel | durable cancel; prints ack + observed stop within one cell |
| resume | resume_update on parent run_id |
| backup | standalone SHA-pinned copy |

Cross-cutting: secrets never printed (canary-key test); every subcommand
exits with documented codes; run prints the report_path on completion. The
CLI is the Phase-1 "button"; the Phase-7 API wraps the same verbs.

---

## 9. Compatibility and Migration Strategy

### 9.1 Schema (all additive, registered via H4 mechanism)

- ingestion_runs receives seven additive columns across two ordered migrations:
  `stage`, `plan_hash`, and `plan_path` in W1-D so the no-cell-without-plan
  invariant is enforceable in the same commit that introduces execution;
  `heartbeat_at`, `pid`, `cancel_requested_at`, and `parent_run_id` in W1-E
  for durable run control (§6.6, §5.5).
- source_checkpoints: + empty_reason (§6.4).
- New untracked data directory: data/run_plans/ (§5.5), sibling convention to
  data/run_reports/.
- No table renames, no row rewrites, no changes to frozen DBs (FROZEN_PATHS
  guard applies to migration application too — attempting to migrate a frozen
  path raises).

### 9.2 Status vocabulary

Canonical enum lives in the RunStatus read-model; legacy tokens (`success`,
`completed`, `succeeded`) map read-side. New writes use the canonical enum.
No UPDATE against historical rows.

### 9.3 Legacy paths

- `scripts/backfill.py` + `backfill_pipeline.py`: kept functional through
  W1-A..E; in W1-F the CLI's --chunk-days subsumes them, backfill.py prints a
  deprecation pointer, and the roadmap's obsolete-workflow inventory decides
  removal vs quarantine (F.1 criterion 12).
- `run_update_batch`: demoted to internal executor engine (the service is the
  only public entrance); its dry-run delegates per §7.
- `orchestrator.py`'s direct `upsert_ohlcv` use: inherits precedence-aware
  upsert semantics automatically; its tests are re-baselined in W3-B if they
  assumed last-write-wins.

### 9.4 Downstream consumers (verified relevant, not modified by W1)

`freshness.py` consumers, S3 corpus_items/index_state wiring, and
`coverage_audit.py` read tables W1 only appends columns to. The agents-side
retrieval (`catalyst_agents/retrieval/policy.py`) is **forbidden-to-modify**
in all W1 slices.

---

## 10. Implementation Slices (dscodex writing-plan units)

Order is the serialized data-core track: W3-A → W1-A → W1-B → W1-C → W1-D →
W1-E → W1-F → W3-B → W2. Each slice = one commit boundary from roadmap §H.

**Per-slice test gate (W1-A through W1-F):**
1. the slice's focused tests green;
2. the canonical full suite `.venv/bin/python -m pytest packages/data-core
   -q` **always run** at commit time;
3. its result diffed against the known-failure manifest from W3-A —
   unchanged or reduced (a slice may legitimately fix known failures, never
   add);
4. zero new failure groups.

Full package green is required only at the **W3-B and W2 exits**, not at
every W1 slice — W1 deliberately changes contracts whose old tests remain in
the manifest until W3-B remediates them. Common to every slice: stage only,
human commits; no code in plans.

### W3-A — Environment/test baseline

- Inspect: packages/data-core/tests/ conftest and pytest configuration
  (pyproject/pytest.ini — locate at plan time), the async/plugin failure
  groups from the current 83.
- Proposed files: docs/testing/data-core-failure-groups.md **(proposed)** —
  the root-cause-group table (signature / affected tests / classification /
  root cause / resolution / verification command / owner+commit).
- Forbidden: any change under catalyst_data/ that alters runtime behavior;
  fixing only what makes the *harness* lie (async config, plugin wiring,
  collection errors).
- Schema impact: none.
- Tests: the suite itself; deliverable = the **known-failure manifest** — the
  exact post-fix list of expected-failing test ids grouped by root cause,
  recorded in the group doc. Every subsequent slice's full-suite run is
  diffed against this manifest (per-slice gate, §10 preamble).
- Landmines: environment fixes can unmask new genuine failures — re-triage
  after, don't freeze the group list before W3-A completes; no assert
  loosening.
- Commit: roadmap §H #1.
- Orchestrator verification: re-run canonical command; diff test-file changes
  for weakened asserts; confirm group doc sums to the observed failure count.

### W1-A — Zero-write planner

- Inspect: update_pipeline.py (`compute_missing_cells`, dry-run branches of
  both entrypoints, `_trading_days_in_window`), freshness.py
  (`latest_local_ohlcv_date`), trading_calendar.py, run_report.py (RunConfig),
  provider_limits.py, storage/sqlite.py (FROZEN_PATHS guard).
- Proposed: catalyst_data/update_planner.py; UpdatePlan per §5.1; hash per
  §5.2; SchemaOutOfDate; delegation of both dry_run paths per §7.
- Forbidden: executor loop changes; schema changes; catalyst_agents/*.
- Schema impact: none (planner is read-only by construction).
- Tests: hash determinism + sensitivity; read-only enforcement (write attempt
  through the planner's connection raises); zero-write proof protocol on a
  fixture; SchemaOutOfDate on a stale fixture; delegation adapters return
  plan-preview shape; socket-guard test proves no network.
- Landmines: -wal/-shm siblings in the proof snapshot; mode=ro URI needs
  uri=True; dry-run delegation breaks tests that asserted run rows/report
  files (expected — pre-register the contract-drift group for W3-B);
  reference_today must be injectable or hash tests flake at midnight.
- Commit: §H #2.
- Orchestrator verification: run the proof protocol personally against a copy
  of the Dev DB; grep the planner module for init_db/INSERT/UPDATE tokens;
  run plan twice, diff hashes.

### W1-B — Calendar/OHLCV planning

- Inspect: trading_calendar.py (US_MARKET_HOLIDAYS coverage,
  latest_closed_trading_day_for_date), config.py (RATE_POLICIES shape, where
  the universe constant lands), compute_missing_cells universe default.
- Proposed changes: expectation from calendar_trading_days only (§6.1);
  explicit universe in config.py + provenance labels (§6.2); 2024 holidays
  added; CalendarCoverageError (§6.3); window defaults replaced (§2.2 defect
  2) — default window per ticker = explicit config or (that ticker's
  watermark + 1 → latest closed session), never a single stale day and never
  the global minimum.
- Forbidden: executor loop; connectors; upsert semantics.
- Schema impact: none.
- Tests: expected-session derivation across weekend/holiday/2024-boundary
  windows; universe provenance labeling; coverage-gap error; tri-state
  resolution math on fixtures (bar / success_empty / unresolved).
- Landmines: 2024 holiday additions change expected-session counts for
  historical windows — coverage percentages in any existing reports shift
  (document, don't hide); `trading_days_for_window` union must NOT be used
  for expectation (circularity test from roadmap §G-W2 applies here too).
- Commit: §H #3.
- Orchestrator verification: hand-compute expected sessions for one
  ticker-month containing a holiday and diff against planner output; confirm
  default-window plan on the real Dev DB (read-only) now spans 2026-05-02 →
  latest closed session for OHLCV.

### W1-C — OHLCV execution and fallback

- Inspect: connectors/polygon.py (`_build_ohlcv_url`, `create_polygon_fetcher`),
  connectors/yfinance_fallback.py, update_pipeline.py cell handlers +
  `persist_cell` storage_callable seam, storage/sqlite.py `upsert_ohlcv`,
  fallback.py, orchestrator.py's upsert_ohlcv call site, scripts/backfill.py
  (what it wrote historically for polygon_ohlcv checkpoints).
- Proposed changes: ohlcv cell handler in the executor; explicit
  adjusted=true; precedence-aware upsert_ohlcv **(behavior change)**;
  per-source fallback chains in FallbackPolicy; empty_reason column
  **(schema change)** + success_empty semantics + the §6.10 lifecycle
  (recheck-once rule, cross-run attempt counting, permanent-class exclusion
  at 3 attempts).
- Forbidden: planner module (frozen from W1-A except imports); news/SEC
  handlers.
- Schema impact: source_checkpoints + empty_reason (registered migration).
- Tests: cell success persists bar + raw + checkpoint atomically; empty-valid
  → success_empty + reason; fallback path sets fallback_provider +
  ohlcv.source; precedence test (yfinance row does not overwrite polygon
  row; polygon overwrites yfinance); §6.10 lifecycle tests (recheck-once,
  window expiry → terminal valid-empty, permanent-class exclusion at 3
  cross-run attempts, chronic-transient flag at 5, superseded-by-success);
  live smoke (env-gated) 1 ticker × 3 days.
- Landmines: Polygon single-day aggs return an empty results array with
  status OK for holidays — but holidays are never planned (calendar), so an
  empty response on a *planned* day is a real empty-valid, not calendar
  noise; yfinance date semantics (timezone of daily bars) must be normalized
  to the exchange date; orchestrator.py tests may assume last-write-wins.
- Commit: §H #4.
- Orchestrator verification: run the live smoke personally (env-gated);
  inspect the landed ohlcv rows + checkpoints by direct SQL; attempt a
  fallback-overwrites-primary write on a fixture and confirm refusal.

### W1-D — Two-stage service facade

- Inspect: run_update whole flow, quality.py open/close run,
  backfill_pipeline chunking, run_report.py.
- Proposed: catalyst_data/update_service.py with the five verbs; two-stage
  orchestration per §4.3 with per-ticker evidence ceilings; strict re-plan;
  **persisted run-plan artifact with the §5.5 atomic ordering and stage-2
  amendment point**; executor entry reshaped to accept explicit cells + run
  context; RunReport additive fields (§5.4); RunConfig additive fields
  (allow_stale_ohlcv, strict).
- Forbidden: CLI; schema beyond the three run-plan columns owned by this
  slice; catalyst_agents/*.
- Schema impact: ingestion_runs gains `stage`, `plan_hash`, and `plan_path`
  in a registered additive migration. This is intentionally shipped with
  the facade so the no-cell-without-plan invariant is true at the W1-D commit
  boundary. The data/run_plans/ directory convention starts here.
- Tests: two-stage run on fixture (stage-2 final ≠ provisional when stage 1
  moves a ticker's watermark; a second ticker's cells unaffected by the
  first ticker's stuck provider — per-ticker ceiling test); PlanDriftError
  before any write (assert no run row AND no plan artifact); artifact exists
  and matches executed cells before first checkpoint is written (ordering
  test); artifact-write failure → run failed with zero cells, resume
  refuses; stage-2 amendment happens before first stage-2 cell;
  allow_stale_ohlcv recorded; single-source degeneration; report contains
  stages + plan_hash + plan_path.
- Landmines: run_update remains as a thin delegate for one release (external
  callers exist in scripts and tests) — its signature must not break;
  fetch_fn injection seams must survive for tests; report JSON consumers
  tolerate new keys (inventory consumers first).
- Commit: §H #5.
- Orchestrator verification: fixture two-stage run transcript; deliberately
  stale hash under strict → confirm zero new rows in ingestion_runs; diff
  RunReport JSON against the field table in §5.4.

### W1-E — Durable status/cancel/resume

- Inspect: quality.py (open_ingestion_run, close_stale_runs, DDL),
  update_pipeline cell loop (`_mark_progress`, `_CANCEL_REQUESTS`),
  migrations.py registration API, doctor.py.
- Proposed: four additional ingestion_runs columns **(schema change —
  heartbeat_at, pid, cancel_requested_at, parent_run_id)**, building on the
  W1-D `stage`, `plan_hash`, and `plan_path` migration;
  BEGIN IMMEDIATE acquisition; **lease heartbeat task (60 s interval, 10 min
  stale threshold, §6.6)**; heartbeat-based orphan handling in
  close_stale_runs + doctor + acquisition; durable cancel polling; RunStatus
  read-model with legacy-status mapping; **plan-based resume (§6.8: parent
  artifact cells minus success/success_empty), including the
  crash-before-stage-2-final rule and refusal of plan-less parents**.
- Forbidden: planner; connectors; CLI.
- Schema impact: ingestion_runs migration for the four durable-control
  columns owned by this slice.
- Tests: cross-process cancel (child process against fixture; stops within
  one cell); WriterConflictError on concurrent open; orphan auto-interrupt
  then successful acquisition; **lease-vs-slow-cell test: a cell stalled
  longer than several lease intervals (simulated slow fetch) is NOT marked
  orphan while the lease ticks**; kill -9 child + resume equals control run
  (fixture only, per roadmap correction #5) — the control equality is on the
  final checkpoint set, and the resume set must include never-attempted
  cells; resume refuses a parent with null plan_path; resume-of-resume
  chains; legacy status tokens map correctly in RunStatus.
- Landmines: pre-migration rows have null heartbeat — the 24 h age backstop
  must keep handling them; BEGIN IMMEDIATE deadlock vs the executor's own
  per-cell connections (acquisition and the lease writer use their own short
  transactions with busy timeouts, never held across fetches); the lease
  task must die with the run (no heartbeats after crash — kill test asserts
  heartbeat stops); frozen-path migration refusal test.
- Commit: §H #6.
- Orchestrator verification: PRAGMA table_info(ingestion_runs) shows all
  seven cumulative W1-D/W1-E columns; run the cross-process cancel drill personally on a fixture;
  inspect a real fixture run's data/run_plans/<run_id>.json and confirm
  plan_path round-trips; grep for any code path that still trusts
  _CANCEL_REQUESTS alone.

### W1-F — Operator CLI and reports

- Inspect: scripts/backfill.py (arg surface operators are used to),
  scripts/migrate_ingestion_quality.py (CLI conventions), run_report.py
  save path behavior, doctor.py redaction.
- Proposed: scripts/update.py per §8; --backup; backfill deprecation pointer;
  runbook document (docs/runbooks/update-operations.md **(proposed)**)
  covering plan/run/cancel/resume/certify/backup with exact commands.
- Forbidden: service/executor logic changes (CLI is presentation only).
- Schema impact: none.
- Tests: subcommand exit codes; canary-key redaction across all output; plan
  --json round-trips to the same hash; --backup produces SHA-pinned copy.
- Landmines: CLI must not import anything that writes at import time; --out
  default must not land inside data/ (zero-write optics); watch-mode polling
  must use inspect_update only.
- Commit: §H #7.
- Orchestrator verification: run every subcommand once against a fixture from
  a fresh shell using only the runbook (docs-as-tested); grep captured output
  for canary key.

### W3-B — Remaining test remediation

- Inspect: docs/testing/data-core-failure-groups.md (from W3-A, updated with
  W1's pre-registered contract-drift groups: dry-run delegation, window
  defaults, upsert precedence, status vocabulary).
- Proposed: fixes/deletions per group; updated group doc as the record.
- Forbidden: behavior changes not traceable to a group; assert loosening
  without group id.
- Schema impact: none.
- Tests: **exit gate — full package green** (or quarantined-by-group); this
  is where the known-failure manifest is driven to empty (or to
  quarantine-only entries). The per-slice manifest-diff gate ends here.
- Landmines: groups whose fixes don't turn all affected tests green were
  mis-clustered — split them; contract-drift fixes must assert the NEW
  contract, not delete coverage.
- Commit: §H #8 (may split per group).
- Orchestrator verification: roadmap §K W3 procedure (3 random groups,
  pre-fix repro + verification command + count reconciliation).

### W2 — Completeness certification

- Inspect: freshness.py (all report functions), quality.py checkpoint
  queries, dedup/cross_source.py, eligibility.py, coverage_audit.py,
  update_planner.py (tri-state math to reuse, not duplicate).
- Proposed: catalyst_data/certification.py + scripts/certify.py +
  data/certifications/ per roadmap §E.4; the §2.3 Finnhub
  checkpoint-vs-content reconciliation lands here (content-present-without-
  checkpoint counted and classified, optionally backfilling synthetic
  reconciliation checkpoints — architect decision at plan time).
- Forbidden: update service changes; catalyst_agents/*.
- Schema impact: none (certification reads; artifact lives outside the DB).
- Tests: per-section unit tests on fixtures with known gaps; circularity test
  (coverage vs calendar, not vs ohlcv rows); gate refusal on NOT CERTIFIED;
  tri-state exhaustiveness (every expected session resolves or fails cert);
  §6.10 lifecycle-bucket tests (pending-recheck / pending-review /
  chronic-transient each block certification; approved-unresolvable and
  terminal valid-empty satisfy it); **exit gate — full package green**.
- Landmines: don't recompute expectation with a second implementation — reuse
  the planner's; cert JSON tracked, large listings capped with counts.
- Commit: §H #9.
- Orchestrator verification: regenerate the cert personally and diff;
  spot-check one ticker-month by direct SQL vs calendar; confirm export
  tooling (W7, later) actually checks the cert gate.

---

## 11. Deferred (explicitly out of W1)

HTTP job endpoints, run endpoints, retrieval/evidence endpoints,
freshness/status endpoints, BYOK/provider endpoints, frontend update page,
progress/ETA UI, evidence/trace UI — all Phase 7–8, wrapping the five verbs
and RunStatus/RunReport read-models defined here. ETA beyond the linear
model, provider conflict ledger, raw+adjusted dual pricing, incremental
export — STRONG items, listed in the roadmap, not in any W1 slice.

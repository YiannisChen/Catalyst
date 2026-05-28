# Live MCJ Runtime Implementation Plan

> **For executor:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build and verify the backend/API productization layer for live MCJ runs: LiveRun APIs, node artifact persistence, polling-friendly execution, failure/retry semantics, stable workbench read APIs, and backend-only end-to-end smoke coverage. Frontend work is intentionally deferred until the backend API contract is proven.

**Architecture:** Keep the existing MCJ graph and trace writer as the research/runtime core. Add an application-facing runtime layer around it: queued execution, a small artifact store beside trace tables, DTOs for polling endpoints, and stable workbench read APIs. The backend must prove that a run can be created, executed, observed through events/artifacts, retried, and failed safely before any UI design or frontend integration proceeds.

**Tech Stack:** Python 3.11, SQLite, FastAPI, Pydantic, LangGraph, LanceDB, sentence-transformers/FlagEmbedding-compatible reranking, pytest plus backend smoke scripts for verification.

---

## Preconditions

Before implementation starts:

- Work only inside `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console`.
- Treat `/Users/yiannischen/Desktop/Catalyst` as read-only reference.
- Do not modify `DemoUI/`, `docs/thesis/`, `docs/reports/`, `docs/figures/`, or `data/`.
- Keep the existing `.gitignore`, `CLAUDE.md`, and workspace boundary plan as preparation changes.
- Use neutral engineering names for branches, worktrees, packages, local directories, and commits.

### Data directory strategy

The worktree's `data/` directory does not contain the frozen DB or LanceDB index (these are gitignored and only exist in the root repo). Before integration tests or real graph execution can work:

- Set `CATALYST_DB_PATH` environment variable to point to the root repo's frozen DB: `export CATALYST_DB_PATH=/Users/yiannischen/Desktop/Catalyst/data/catalyst_eval_frozen_v2.db`
- For LanceDB, the `DEFAULT_LANCEDB_DIR` in `retrieval/policy.py` resolves relative to the agents package root. On the worktree this will resolve to a non-existent path. Task 9 (dependency loader) must support a `CATALYST_LANCEDB_DIR` environment variable that overrides `DEFAULT_LANCEDB_DIR`. Integration tests and the 3090 deployment must set this variable to point to the external LanceDB directory (e.g. the root repo's `data/lancedb_gold/eval_frozen`). Do not create symlinks inside the worktree's `data/` directory — this would violate the workspace boundary plan's `data/` freeze policy.
- Real-provider smoke tests may need the existing API key stored in `/Users/yiannischen/Desktop/Catalyst/packages/data-core/.env`. The expected variable name is `aihubmix_api_key`. Do not print, copy into source, commit, or expose this value in logs. Any loader or script that uses it must read it at runtime from that `.env` file or from the process environment.
- All unit tests (Tasks 1-8) must use temporary in-memory or `tmp_path` SQLite DBs and fake retrieval — they must not depend on the frozen dataset existing.
- Integration/smoke tests (Task 17) that need real data should be marked with `@pytest.mark.integration` and skipped by default when `CATALYST_DB_PATH` is not set.
- On the 3090 deployment target, the `data/` directory must be provisioned separately (copy or mount) before the server starts.

Run before starting each remaining task:

- `git status --short`
- Confirm the existing dirty worktree is understood before editing.
- Do not clean, delete, stage, or commit unrelated prior-worker changes.
- Only modify files listed in the current task unless a blocking compatibility issue is found and explicitly reported.

---

## Implementation Strategy

This implementation should proceed in strict dependency order:

1. Runtime persistence and artifact schema.
2. Runtime DTOs and status mapping.
3. Artifact capture inside the existing graph path.
4. Background runner and LiveRun service.
5. FastAPI endpoints.
6. Stable read API needed by the workbench.
7. Backend execution trigger from the API layer.
8. Backend integration smoke tests and failure-path verification.

Frontend implementation is explicitly out of scope for this backend/API plan. A separate frontend design and implementation plan should start only after Tasks 13-18 prove the backend contract.

---

## Task 1: Runtime Persistence Schema

**Files:**

- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/catalyst_agents/trace/schema.py`
- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/tests/test_trace.py`

**Purpose:** Add durable artifact and retry lineage storage without overloading `trace_events`.

**Required behavior:**

- `init_trace_db` must create a `node_artifacts` table.
- `node_artifacts` must store `run_id`, `event_seq`, `node`, `artifact_type`, `payload_json`, and `created_at`.
- `node_artifacts` must be queryable by `run_id` and `event_seq`.
- Add a lightweight retry lineage table or equivalent persisted relationship. Prefer a `run_links` table with `run_id`, `parent_run_id`, and `link_type`.
- Add a `queued_at` column to `agent_runs` (nullable TEXT, ISO-8601). This column is set when the background runner creates a QUEUED record (Task 8) and preserved when `TraceWriter` transitions the row to RUNNING (Task 4). The existing `started_at` column continues to represent when execution actually began.
- Existing trace tests must continue to pass unchanged except for new assertions.

**Steps:**

1. Add failing tests in `packages/agents/tests/test_trace.py` asserting `init_trace_db` creates `node_artifacts` and `run_links`.
2. Run `pytest packages/agents/tests/test_trace.py -q`.
3. Update `trace/schema.py` DDL to create the new tables and indexes.
4. Run `pytest packages/agents/tests/test_trace.py -q`.
5. Confirm no existing trace table contract regressed.

**Verification:**

- `pytest packages/agents/tests/test_trace.py -q`

**Parallelism:** Must run before artifact writer, runner, and API tasks.

---

## Task 2: Artifact Writer Utilities

**Files:**

- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/catalyst_agents/trace/artifacts.py`
- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/tests/test_trace.py`

**Purpose:** Provide a small, tested persistence API for node artifacts.

**Required behavior:**

- Provide a function to write one artifact for `run_id`, `event_seq`, `node`, and `artifact_type`.
- Provide a function to read artifacts by `run_id`, optionally filtered by `event_seq` and `artifact_type`.
- Payloads are persisted as JSON strings and returned as parsed dictionaries.
- Non-serializable payloads fail early with a clear exception.
- The utility must not know about frontend rendering.

**Steps:**

1. Add tests for write/read artifact roundtrip.
2. Add tests for filtering by `event_seq` and `artifact_type`.
3. Add tests for invalid JSON-serialization behavior.
4. Implement `trace/artifacts.py`.
5. Run `pytest packages/agents/tests/test_trace.py -q`.

**Verification:**

- `pytest packages/agents/tests/test_trace.py -q`

**Parallelism:** Can start after Task 1.

---

## Task 3: Artifact Projection Helpers

**Files:**

- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/catalyst_agents/trace/projection.py`
- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/tests/test_trace.py`

**Purpose:** Convert runtime state into frontend-safe artifact payloads.

**Required behavior:**

- Implement a **node → artifact_types mapping** that determines which artifacts each node produces. This is critical because `_trace_node` operates on `merged_state = {**state, **result}` which contains all prior node outputs plus the current node's outputs. Without this mapping, every node would redundantly store all predecessor data. The mapping must be:
  - `miner` → `retrieved_chunks`, `reranked_chunks`, `state_snapshot`
  - `critic` → `graded_evidence`, `all_graded_chunks`, `critic_decision`, `raw_llm_response`, `state_snapshot`
  - `decision_router` → `state_snapshot`
  - `expand_macro` → `state_snapshot`
  - `judge` → `judge_causes`, `judge_summary`, `raw_llm_response`, `state_snapshot`
  - `validator` → `validator_decision`, `raw_llm_response`, `state_snapshot`
  - `finalizer` → `state_snapshot`
  - `insufficient_handler` → `state_snapshot`
  - `system_error_handler` → `error_snapshot`
  - `baseline_prepare_evidence` → `state_snapshot`
- Project `retrieved_chunks` into a bounded payload with rank, asset id, ticker, source type, reference date, score, headline, and snippet.
- Project `reranked_chunks` with the same fields plus `rerank_score` when present.
- Derive `headline` and `snippet` from `content_md`, not from fields that do not exist in current retrieval output. Use the `## TICKER: Title` header convention from `quality.py:extract_title()` for headline; first 200 characters of body for snippet.
- Project `graded_evidence` and `all_graded_chunks` using the real Critic output shape: boolean `temporal_match`, relevance, category, reasoning, optional critic sub-scores (`event_specificity`, `temporal_alignment`, `evidence_granularity`, `conflict_signal`), and optional `original_relevance`.
- Project `causes`, `summary_md`, validator decision fields, and compact state snapshots.
- Never serialize LanceDB table handles, embedding functions, reranker objects, or `RetrievalMetadata` directly.
- Enforce payload caps: top 20 retrieved chunks, top 8 reranked chunks, bounded text previews, and bounded raw response text.
- Provide a `project_node_artifacts(node_name, merged_state, result)` entry point that uses the mapping to produce only the artifacts owned by that node.

**Steps:**

1. Add tests using representative chunk dicts from existing retrieval tests.
2. Add tests proving headline/snippet are derived from `content_md`.
3. Add tests proving `temporal_match` remains boolean.
4. Add tests proving non-serializable objects are excluded from snapshots.
5. Add tests proving the node → artifact_types mapping only produces artifacts owned by the specified node (e.g., calling with `node_name="critic"` does not produce `retrieved_chunks`).
6. Implement projection helpers including the mapping.
7. Run `pytest packages/agents/tests/test_trace.py -q`.

**Verification:**

- `pytest packages/agents/tests/test_trace.py -q`

**Parallelism:** Can run after Task 2. This is a good isolated worker task.

---

## Task 4: TraceWriter Artifact Integration

**Files:**

- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/catalyst_agents/trace/writer.py`
- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/catalyst_agents/graph.py`
- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/tests/test_trace.py`
- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/tests/test_graph.py`

**Purpose:** Persist artifacts immediately after each node completes, aligned to the emitted `trace_events.event_seq`. Also enable external `run_id` injection so the background runner can use a pre-created QUEUED run.

**Required behavior:**

- `TraceWriter.event` should return the inserted `event_seq` (currently returns `None`).
- `_trace_node` should use the returned event sequence to persist artifacts for that completed node. It must pass `node_name` to `project_node_artifacts()` so only artifacts owned by that node are stored (see Task 3 mapping).
- Artifact persistence failures should not hide the original node failure; they should be captured as `error_snapshot` where possible or raised as system errors in tests designed for that path.
- Do not implement node-started events in Phase 1.
- The public progress contract remains `last_completed_node` plus `predicted_next_node`.
- **`_TracedCompiledGraph.invoke()` must accept an optional `run_id` parameter** and pass it through to `TraceWriter(run_id=run_id)`. This is required so the background runner (Task 8) can create a QUEUED `agent_runs` record first, then have the graph execution use the same `run_id`. Currently `_TracedCompiledGraph` creates `TraceWriter` internally with an auto-generated `run_id`, making it impossible for the runner to pre-register the run.
- **`TraceWriter.__enter__` must support updating an existing QUEUED run** instead of always using `INSERT OR REPLACE` with status `"RUNNING"`. When a `run_id` is provided and a matching `agent_runs` row already exists with status `QUEUED`, the writer should `UPDATE` the status to `RUNNING` and set `started_at`, preserving the original `queued_at` and any other queue-time metadata. When no prior row exists, the current `INSERT` behavior is kept.

**Steps:**

1. Add a test proving `TraceWriter.event` returns `event_seq`.
2. Add a test proving `TraceWriter` can be initialized with a pre-existing `run_id` and transitions a QUEUED row to RUNNING.
3. Add a test proving `_TracedCompiledGraph.invoke(state, run_id="...")` uses the provided `run_id` in trace records.
4. Add graph trace test proving miner artifacts are stored after a graph run.
5. Add graph trace test proving critic artifacts include `graded_evidence` but NOT `retrieved_chunks`.
6. Modify `TraceWriter.event` to return event sequence.
7. Modify `TraceWriter.__enter__` to handle pre-existing QUEUED rows.
8. Modify `_TracedCompiledGraph.invoke` to accept and forward optional `run_id`.
9. Modify `_trace_node` to call artifact projection and artifact writer after node completion.
10. Run `pytest packages/agents/tests/test_trace.py packages/agents/tests/test_graph.py -q`.

**Verification:**

- `pytest packages/agents/tests/test_trace.py packages/agents/tests/test_graph.py -q`

**Parallelism:** Depends on Tasks 1-3.

---

## Task 5: Raw LLM Response Exposure

**Files:**

- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/catalyst_agents/nodes/critic.py`
- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/catalyst_agents/nodes/judge.py`
- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/catalyst_agents/nodes/validator.py`
- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/tests/test_critic.py`
- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/tests/test_judge.py`
- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/tests/test_validator.py`

**Purpose:** Make raw model responses available to artifact capture without changing public final attribution semantics.

**Required behavior:**

- Critic, Judge, and Validator should include raw response text under internal keys in returned partial state.
- Internal raw response keys must be ignored by eval adapter result construction.
- Artifact projection should persist raw response text as `raw_llm_response`.
- Raw response text must be bounded when persisted for frontend display.
- Existing output status and cost tracking behavior must remain unchanged.

**Steps:**

1. Add tests that mock LLM responses and assert each node returns an internal raw response field.
2. Add graph-level test proving raw response artifacts are stored for Critic and Judge.
3. Update node returns to include internal raw response fields after successful LLM calls.
4. Update projection helper to extract these fields.
5. Run node tests and graph tests.

**Verification:**

- `pytest packages/agents/tests/test_critic.py packages/agents/tests/test_judge.py packages/agents/tests/test_validator.py packages/agents/tests/test_graph.py -q`

**Parallelism:** Assigned to Worker 1. Can run after Task 3, but graph-level artifact assertions depend on Task 4. The `nodes/` directory modifications in this task are exclusively owned by Worker 1 — see updated Subagent Execution Guidance.

---

## Task 6: Runtime Status And Failure Mapping

**Files:**

- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/catalyst_agents/runtime/__init__.py`
- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/catalyst_agents/runtime/status.py`
- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/tests/test_runtime_status.py`

**Purpose:** Normalize core graph statuses into frontend-facing run statuses and failure payloads.

**Required behavior:**

- Create `runtime/__init__.py` first so the package is importable by subsequent tasks.
- Map internal `SUFFICIENT` to `SUCCEEDED`.
- Preserve `PARTIAL` and `INSUFFICIENT`.
- Map internal `SYSTEM_ERROR` to `FAILED_SYSTEM`.
- Map pre-run validation failures to `FAILED_REQUEST`.
- Use simple top-level statuses and richer `sub_reason` values.
- Include retryable flags based on category and lifecycle state.
- Keep `NON_MATERIAL_MOVE` and `INCONCLUSIVE` as sub-reasons, not top-level statuses.

**Steps:**

1. Create `packages/agents/catalyst_agents/runtime/__init__.py` (empty or minimal).
2. Add tests for status mapping.
3. Add tests for failure payload construction from run rows and trace event rows.
4. Add tests for retryability rules.
5. Implement runtime status utilities.
6. Run `pytest packages/agents/tests/test_runtime_status.py -q`.

**Verification:**

- `pytest packages/agents/tests/test_runtime_status.py -q`

**Parallelism:** Can run after Task 1. Does not require API package.

---

## Task 7: Runtime Request Validation

**Files:**

- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/catalyst_agents/runtime/validation.py`
- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/tests/test_runtime_validation.py`

**Purpose:** Validate ticker/date/query before a live run is queued.

**Required behavior:**

- Validate ticker against the supported frozen dataset ticker universe.
- Validate trade date against available OHLCV context.
- Validate query length and empty-query behavior.
- Return structured request failures for unsupported ticker, date out of range, and missing trading-day context.
- Do not query or modify legacy experiment outputs.

**Steps:**

1. Add tests using a temporary SQLite DB with minimal `ohlcv` rows.
2. Add tests for unsupported ticker.
3. Add tests for date out of range or missing trading session.
4. Add tests for query length validation.
5. Implement validation utilities.
6. Run `pytest packages/agents/tests/test_runtime_validation.py -q`.

**Verification:**

- `pytest packages/agents/tests/test_runtime_validation.py -q`

**Parallelism:** Can run after Task 6 or in parallel with it.

---

## Task 8: LiveRun Service And Background Runner

**Files:**

- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/catalyst_agents/runtime/service.py`
- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/catalyst_agents/runtime/runner.py`
- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/catalyst_agents/runtime/__init__.py`
- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/tests/test_live_run_service.py`

**Purpose:** Provide a backend service layer that creates queued runs, executes graph jobs in the background, and exposes polling-friendly run state.

**Required behavior:**

- `create_run` validates request and persists `QUEUED` before graph execution.
- The background runner uses the pre-created `run_id` when invoking the traced graph.
- The runner transitions to `RUNNING` when `TraceWriter` starts.
- Terminal statuses are persisted through existing `TraceWriter.complete`.
- `get_run` returns product-facing status, `last_completed_node`, and `predicted_next_node`.
- `get_events` returns trace events after an optional sequence.
- `get_artifacts` returns node artifacts with optional filters.
- `retry_run` rejects queued/running runs, creates a new run for terminal runs, links retry lineage, and allows model override only.
- Default concurrency is one. The implementation should make future `2-3` concurrency possible without API changes.
- Background runner must enforce an overall timeout per run (recommended 120 seconds). If a run exceeds the timeout, mark it `FAILED_SYSTEM` with `sub_reason: timeout`. This prevents a stuck LLM call from blocking the single-concurrency queue indefinitely.

**Steps:**

1. Add tests for queued run creation without executing the graph.
2. Add tests for background execution using a fake graph.
3. Add tests for event polling after fake graph completion.
4. Add tests for artifact polling.
5. Add tests for retry edge cases.
6. Implement service and runner modules.
7. Run `pytest packages/agents/tests/test_live_run_service.py packages/agents/tests/test_runtime_status.py packages/agents/tests/test_runtime_validation.py -q`.

**Verification:**

- `pytest packages/agents/tests/test_live_run_service.py packages/agents/tests/test_runtime_status.py packages/agents/tests/test_runtime_validation.py -q`

**Parallelism:** Depends on Tasks 1, 2, 4, 6, and 7.

---

## Task 9: Runtime Dependency Loader

**Files:**

- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/catalyst_agents/runtime/dependencies.py`
- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/data-core/catalyst_data/storage/lancedb_store.py` only if a small compatibility helper is needed
- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/tests/test_runtime_dependencies.py`

**Purpose:** Load heavy runtime dependencies once at application startup and expose health state.

**Required behavior:**

- Open the configured LanceDB table once.
- Build an embedding function compatible with the existing index model.
- Load reranker once and mark fallback mode if unavailable.
- Validate model/index compatibility before live runs.
- Provide a health payload with DB, LanceDB, embedding, reranker, and default model status.
- Do not initialize heavy models per request.

**Steps:**

1. Add tests with dependency fakes for ready, degraded, and failed states.
2. Add a test that rejects incompatible embedding/index configuration.
3. Implement dependency loader and health object.
4. Run `pytest packages/agents/tests/test_runtime_dependencies.py -q`.

**Verification:**

- `pytest packages/agents/tests/test_runtime_dependencies.py -q`

**Parallelism:** Can run after Task 7. Integration with service waits for Task 8.

---

## Task 10: App Package Scaffold And DTOs

**Files:**

- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/pyproject.toml`
- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/catalyst_app/__init__.py`
- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/catalyst_app/schemas.py`
- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/tests/test_schemas.py`

**Purpose:** Create a first-class application package for APIs instead of modifying ignored `DemoUI/`.

**Required behavior:**

- Define Pydantic DTOs for create run, run summary, run events, artifacts, retry request, and health response.
- DTOs must use `last_completed_node` and `predicted_next_node`, not `current_node`.
- DTOs must include model id, retry metadata, terminal result, and structured error payload.
- Keep this package thin. Core runtime logic remains in `packages/agents`.
- `pyproject.toml` must declare dependencies on `catalyst-agents>=0.1.0`, `catalyst-data>=0.1.0`, `fastapi`, and `pydantic>=2.0`.

**Steps:**

1. Create package scaffold including `pyproject.toml` with correct dependencies.
2. Install local packages in development mode: `pip install -e packages/data-core -e packages/agents -e packages/app` (or equivalent). Verify `from catalyst_agents.runtime.status import ...` imports succeed.
3. Add schema tests for each LiveRun DTO.
4. Add tests proving invalid statuses or invalid artifact types fail validation.
5. Implement DTOs.
6. Run `pytest packages/app/tests/test_schemas.py -q`.

**Verification:**

- `pytest packages/app/tests/test_schemas.py -q`

**Parallelism:** Can run after Task 6, in parallel with Tasks 8-9 once DTO fields are locked.

---

## Task 11: LiveRun API Endpoints

**Files:**

- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/catalyst_app/main.py`
- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/catalyst_app/dependencies.py`
- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/catalyst_app/routers/live_runs.py`
- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/tests/test_live_run_api.py`

**Purpose:** Expose the live runtime service through polling-friendly FastAPI endpoints.

**Required endpoints:**

- `POST /api/live-runs`
- `GET /api/live-runs/{run_id}`
- `GET /api/live-runs/{run_id}/events`
- `GET /api/live-runs/{run_id}/artifacts`
- `POST /api/live-runs/{run_id}/retry`
- `GET /api/health/runtime`

**Required behavior:**

- API methods delegate to `catalyst_agents.runtime.service`.
- `POST /api/live-runs` returns quickly after queuing.
- Polling endpoints are deterministic and return ordered events.
- Retry endpoint follows the edge-case policy.
- Health endpoint reports degraded reranker state without failing the whole service.

**Steps:**

1. Add FastAPI tests using service fakes.
2. Add tests for create/get/events/artifacts/retry.
3. Add tests for request validation failures.
4. Implement app factory and router.
5. Run `pytest packages/app/tests/test_live_run_api.py -q`.

**Verification:**

- `pytest packages/app/tests/test_live_run_api.py -q`

**Parallelism:** Depends on Tasks 8 and 10.

---

## Task 12: Stable Workbench Read API

**Files:**

- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/catalyst_app/routers/workbench.py`
- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/catalyst_app/workbench_read.py`
- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/tests/test_workbench_api.py`

**Purpose:** Keep stable chart/news/evidence data available independently from live runtime success.

**Minimum endpoints (Phase 1):**

- `GET /api/tickers`
- `GET /api/ohlcv/{symbol}`
- `POST /api/attribution/range-local`

**Deferred endpoints (require article materialization — see Known Risks):**

- `GET /api/news/{symbol}`
- `GET /api/news/{symbol}/categories`

These news endpoints depend on article-level materialization from `raw_assets.content_raw`, which is out of scope for this plan. They must not be implemented as stubs that silently return empty data — this would mask a missing data dependency. They will be added in a follow-up plan that includes the materialization work.

**Required behavior:**

- These endpoints do not trigger live MCJ runs.
- They read from stable SQLite/materialized data.
- The live runtime API must not depend on these endpoints to execute.

**Steps:**

1. Add tests for ticker and OHLCV read behavior using temporary SQLite DB.
2. Implement read helpers and router for Phase 1 endpoints only.
3. Register router in app.
4. Run `pytest packages/app/tests/test_workbench_api.py -q`.

**Verification:**

- `pytest packages/app/tests/test_workbench_api.py -q`

**Parallelism:** Can run after Task 10. This can be a separate worker task because it does not write `packages/agents`.

---

## Task 13: API-Triggered Backend Execution

**Files:**

- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/catalyst_app/routers/live_runs.py`
- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/catalyst_app/dependencies.py`
- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/tests/test_live_run_api.py`
- Optionally modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/catalyst_agents/runtime/service.py`

**Purpose:** Ensure `POST /api/live-runs` can start backend execution instead of only inserting a `QUEUED` row.

**Current gap:** Task 11 exposes the API and Task 8 provides `LiveRunService.run_next()`, but the FastAPI route does not currently trigger the runner. A run can be queued and polled, but it will not progress unless external code calls `run_next()`.

**Required behavior:**

- `POST /api/live-runs` must return quickly after creating the run.
- A P1 execution trigger must start the queued run without requiring frontend/manual intervention.
- Prefer FastAPI `BackgroundTasks` for P1: after a successful `QUEUED` response, schedule a single `service.run_next()` call.
- Do not introduce Celery, Redis, WebSocket, or a complex queue manager in this task.
- Validation failures (`FAILED_REQUEST`) must not schedule background execution.
- The route must remain testable with fake services.
- Keep the single-runner assumption explicit; multi-worker atomic claim remains P2.

**Steps:**

1. Add a FastAPI test proving successful `POST /api/live-runs` schedules one background execution call.
2. Add a test proving `FAILED_REQUEST` does not schedule execution.
3. Implement `BackgroundTasks` wiring in the create-run endpoint.
4. Keep the response contract unchanged.
5. Run focused API smoke with a fake service that records `run_next()` calls.

**Verification:**

- `python -m py_compile packages/app/catalyst_app/routers/live_runs.py packages/app/tests/test_live_run_api.py`
- `PYTHONPATH=packages/agents:packages/data-core:packages/app python - <<'PY' ... PY` TestClient smoke proving queue plus background trigger.
- `pytest packages/app/tests/test_live_run_api.py -q` when the local pytest environment is usable.

**Parallelism:** Depends on Tasks 8 and 11. Must complete before backend E2E smoke.

---

## Task 14: Backend API Contract Smoke

**Files:**

- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/tests/test_backend_api_contract.py`
- Optionally create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/scripts/backend_api_contract_smoke.py`

**Purpose:** Prove every backend endpoint returns the intended JSON shape with fake services/stores, without heavy ML dependencies or external credentials.

**Required endpoint coverage:**

- `POST /api/live-runs`
- `GET /api/live-runs/{run_id}`
- `GET /api/live-runs/{run_id}/events`
- `GET /api/live-runs/{run_id}/artifacts`
- `POST /api/live-runs/{run_id}/retry`
- `GET /api/health/runtime`
- `GET /api/tickers`
- `GET /api/ohlcv/{ticker}`
- `GET /api/range-local`

**Required assertions:**

- No endpoint returns an unstructured 500 for expected failure cases.
- `RunSummaryResponse` does not include `current_node`.
- `RunSummaryResponse` includes `last_completed_node` and `predicted_next_node`.
- `RunEventResponse` accepts internal trace statuses such as `SUFFICIENT` and `SYSTEM_ERROR`.
- `ArtifactResponse` externally returns `payload`, not `payload_json`.
- Stable read API returns `volume` as a JSON number compatible with the real `ohlcv.volume REAL` schema.
- Missing DB or missing `ohlcv` table returns clear `503` errors.

**Steps:**

1. Add fake service, fake dependency loader, and temporary workbench DB fixtures.
2. Exercise every endpoint through `TestClient`.
3. Assert exact response keys for DTO-sensitive endpoints.
4. Add a script variant if pytest continues to segfault in the local environment.

**Verification:**

- `python -m py_compile packages/app/tests/test_backend_api_contract.py`
- `PYTHONPATH=packages/agents:packages/data-core:packages/app python scripts/backend_api_contract_smoke.py` if the script is created.
- `pytest packages/app/tests/test_backend_api_contract.py -q` when the local pytest environment is usable.

**Parallelism:** Can run after Tasks 11-13.

---

## Task 15: End-To-End Backend Smoke

**Files:**

- Create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/tests/test_live_runtime_smoke.py`
- Optionally create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/scripts/live_runtime_smoke.py`
- Optionally create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/scripts/live_provider_smoke.py`

**Purpose:** Verify the backend can create a run, execute a fake graph path, persist trace/artifact data, and expose the result through API polling.

**Required behavior:**

- Smoke test can run with fake LLM and fake retrieval without external API keys.
- Test proves create-run returns quickly.
- Test polls until terminal state.
- Test verifies trace events exist.
- Test verifies node artifacts exist.
- Test verifies raw response artifacts exist in the fake LLM path.
- Test verifies retry creates a new linked run.
- Test verifies the background execution trigger from Task 13 is sufficient to move a run out of `QUEUED`.
- Real-provider smoke is optional and must be opt-in. It may read `aihubmix_api_key` from `/Users/yiannischen/Desktop/Catalyst/packages/data-core/.env` or the environment, but must never print the key.

**Steps:**

1. Add a fake graph that writes trace events and artifacts through the same service/runner path used by the API.
2. Add API smoke for `create -> background run -> poll run -> poll events -> poll artifacts`.
3. Add retry smoke test.
4. Add timeout/system-error smoke if it can be done without slow sleeps.
5. Add an opt-in real-provider smoke script only if the app has an LLM client factory available. Gate it behind an explicit env var such as `CATALYST_RUN_REAL_PROVIDER_SMOKE=1`.
6. Run app and agents test subsets or the script fallback.

**Verification:**

- `pytest packages/app/tests/test_live_runtime_smoke.py -q`
- `pytest packages/agents/tests/test_trace.py packages/agents/tests/test_graph.py packages/agents/tests/test_live_run_service.py -q`
- `CATALYST_RUN_REAL_PROVIDER_SMOKE=1 PYTHONPATH=packages/agents:packages/data-core:packages/app python scripts/live_provider_smoke.py` if created and credentials are available.

**Parallelism:** Depends on Tasks 4, 5, 8, 11, and 13.

---

## Task 16: Failure Path Verification

**Files:**

- Create or modify tests under:
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/agents/tests/`
- `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/tests/`

**Purpose:** Prove failures are classified and displayed as inspectable runtime outcomes.

**Required scenarios:**

- invalid request becomes `FAILED_REQUEST`.
- LLM/provider failure becomes `FAILED_SYSTEM` with retryable true when appropriate.
- no trading day context returns request failure or insufficient status consistently.
- insufficient evidence remains `INSUFFICIENT`, not system failure.
- partial validation remains `PARTIAL`.
- artifact write failure has a clear failure behavior.

**Steps:**

1. Add targeted tests for request validation failures.
2. Add targeted tests for model failure after internal retries.
3. Add targeted tests for insufficient evidence.
4. Add targeted tests for partial validation.
5. Run focused agent/app tests.

**Verification:**

- `pytest packages/agents/tests/test_runtime_status.py packages/agents/tests/test_runtime_validation.py packages/app/tests/test_live_run_api.py -q`

**Parallelism:** Depends on Tasks 6, 7, 8, and 11.

---

## Task 17: Real Data Configuration Smoke

**Files:**

- Create or modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/scripts/backend_real_data_smoke.py`
- Optionally create: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/packages/app/tests/test_real_data_config.py`

**Purpose:** Prove the backend can read the real frozen SQLite DB and LanceDB configuration that will be used for demo/deployment, without requiring a live provider call.

**Required environment:**

- `CATALYST_DB_PATH=/Users/yiannischen/Desktop/Catalyst/data/catalyst_eval_frozen_v2.db`
- `CATALYST_LANCEDB_DIR=/Users/yiannischen/Desktop/Catalyst/data/lancedb_gold/eval_frozen`

**Required behavior:**

- `/api/tickers` returns the 10 frozen-dataset tickers.
- `/api/range-local` returns a non-empty date range and row count.
- `/api/ohlcv/AAPL` returns at least one candle for a known available date.
- Runtime dependency health can open the LanceDB table and detect vector dimension.
- The smoke must not call a real LLM unless explicitly enabled by an opt-in env var.
- If real-provider mode is enabled, load `aihubmix_api_key` from `/Users/yiannischen/Desktop/Catalyst/packages/data-core/.env` or the process environment. Never print the key or write it into generated files.

**Steps:**

1. Create a script that checks env vars and exits with a clear skipped message if they are not set.
2. Use `TestClient` with the real workbench store and dependency loader.
3. Assert frozen DB read APIs.
4. Assert dependency health is `ready` or `degraded`, not `failed`.
5. Print a compact readiness summary for demo setup.
6. If opt-in real-provider mode is enabled, perform one minimal model connectivity check and report only success/failure plus model name.

**Verification:**

- `PYTHONPATH=packages/agents:packages/data-core:packages/app python scripts/backend_real_data_smoke.py`

**Parallelism:** Depends on Tasks 9, 11, and 12.

---

## Task 18: Final Backend Verification Matrix

**Files:**

- Modify: `/Users/yiannischen/.config/superpowers/worktrees/Catalyst/feature/live-mcj-runtime-console/docs/plans/2026-05-25-live-mcj-runtime-implementation-plan.md` only if verification commands drift during implementation.

**Purpose:** Run the smallest credible test matrix before calling implementation complete.

**Commands:**

- `pytest packages/agents/tests/test_trace.py -q`
- `pytest packages/agents/tests/test_graph.py -q`
- `pytest packages/agents/tests/test_runtime_status.py -q`
- `pytest packages/agents/tests/test_runtime_validation.py -q`
- `pytest packages/agents/tests/test_live_run_service.py -q`
- `pytest packages/app/tests -q`
- `PYTHONPATH=packages/agents:packages/data-core:packages/app python scripts/backend_api_contract_smoke.py` if created
- `PYTHONPATH=packages/agents:packages/data-core:packages/app python scripts/live_runtime_smoke.py` if created
- `PYTHONPATH=packages/agents:packages/data-core:packages/app python scripts/backend_real_data_smoke.py` with real data env vars set

**Expected result:**

- All listed backend tests pass or, in the current local environment, pytest segfault is explicitly isolated to pytest startup with script smokes passing.
- Any skipped live-provider test is explicitly marked as requiring real credentials and is not part of default local verification.
- Backend API contract is stable enough for frontend design to begin.

---

## Deferred: Frontend Workbench UI

Frontend scaffold, Runtime Console UI, and Workbench Shell Integration are intentionally deferred from this backend/API plan. They should move into a separate frontend plan after Task 18 is complete.

Deferred frontend scope:

- `apps/workbench` app scaffold and package management.
- K-line/chart integration.
- Runtime console panels and polling UI.
- Article/news feed and category UI.
- Design system and visual polish.

Reason for deferral: frontend design should not lock onto unstable backend behavior. The backend must first prove real queue execution, event/artifact polling, stable read APIs, failure paths, and real data configuration.

---

## Subagent Execution Guidance

This plan is suitable for subagent execution after the user approves it.

Recommended split:

- Worker 1: Tasks 1-5, runtime schema/artifacts/trace integration and raw response capture.
- Worker 2: Tasks 6-9, status, validation, runner, dependency loading.
- Worker 3: Tasks 10-12, FastAPI app and stable read API.
- Worker 4: Tasks 13-15, API-triggered execution plus backend contract/E2E smoke.
- Worker 5: Tasks 16-18, failure-path verification, real-data smoke, and final backend verification matrix.

Write scopes must remain disjoint:

- Worker 1 owns `packages/agents/catalyst_agents/trace`, `graph.py`, `nodes/critic.py`, `nodes/judge.py`, `nodes/validator.py`, and their tests (`test_trace.py`, `test_graph.py`, `test_critic.py`, `test_judge.py`, `test_validator.py`). Worker 1 must modify these node files to capture raw LLM responses for artifact projection (Task 5).
- Worker 2 owns `packages/agents/catalyst_agents/runtime` and runtime tests.
- Worker 3 owns `packages/app`.
- Worker 4 owns backend execution trigger and backend smoke tests/scripts under `packages/app/tests/` and `scripts/`.
- Worker 5 owns failure-path tests, real-data smoke scripts, and final verification documentation.

Workers must not modify:

- `DemoUI/`
- `apps/workbench/` until a separate frontend plan is approved
- `data/`
- `docs/thesis/`
- `docs/reports/`
- historical reports or old experiment outputs

---

## Known Risks

- The current worktree does not contain the earlier untracked full design documents; those remain in the root tree and were used only as read-only reference.
- True in-flight `current_node` is not available without node-started events. Phase 1 uses completed-node polling semantics.
- Raw model responses require node changes; they are not available through `_trace_node` alone.
- Embedding/index compatibility must be verified before using the 3090 runtime for live demos.
- Hardware improves resource headroom but does not fix contract, instrumentation, or payload-shaping issues.
- Workbench layer article feed requires independent materialization work (splitting `raw_assets.content_raw` into article-level rows with per-article metadata). This is not in scope for this plan but must be completed before frontend article feed integration can render individual articles rather than raw asset blobs.

---

## Completion Criteria

Implementation is ready for review when:

- LiveRun API supports create/get/events/artifacts/retry.
- Background runner queues runs without blocking HTTP handlers.
- Trace events and node artifacts are persisted and retrievable.
- Retrieval, rerank, graded evidence, raw response, and state snapshot artifacts are visible through API.
- Failure and retry semantics match the simplified status/sub-reason model.
- Stable read APIs remain independent from live runtime.
- `POST /api/live-runs` can trigger backend execution without manual runner calls.
- Backend API contract smoke passes for all LiveRun and stable read endpoints.
- Backend E2E smoke proves create -> execute -> poll events -> poll artifacts -> retry.
- Real-data configuration smoke proves frozen SQLite and LanceDB paths are readable in the expected deployment shape.
- Focused backend tests and script smokes pass, or pytest startup segfault is documented with script smokes passing.

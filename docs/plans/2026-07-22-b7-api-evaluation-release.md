# B7 — API, Compact Evaluation, and Backend Release Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement and verify the local API/evaluation/replay machinery, pause for the architect's two-pass human grading of 12 BenchmarkCases, then resume to make named-case component decisions and produce the backend release pack.

**Architecture:** B7 has three explicit checkpoints: B7-A implements API, metrics, fixtures, and zero-network replay; B7-H is a human-only labeling interval with a second pass at least seven days later; B7-R resumes automation to freeze ResultPacks, make keep/kill decisions, and certify release. One Goal may execute B7-A or B7-R, never bridge the B7-H waiting period.

**Tech Stack:** Python 3.12+, FastAPI, pydantic, SQLite, pytest, Markdown generation

**Binding Contract:** `docs/plans/2026-07-21-b2-b7-technical-contracts.md` §9

---

## 0. Execution Rules

- B2–B6 must be independently verified before B7-A.
- Do not stage, commit, push, call live providers/models/judges, inspect secrets, or mutate canonical DB files.
- Extend/refactor the existing workbench and live-run routers; do not create duplicate endpoints for capabilities already present.
- The production `SQLiteContextProvider` is an app composition adapter implementing the B5 protocol. Domain formulas remain in agents; SQL remains outside agents.
- No `/embedding_text` endpoint is exposed. URL/image exclusion is a data-core chunk test, not a public API.
- Security tests generate a random sentinel at runtime and inspect only captured responses, logs, trace rows, and temporary DB/files. They never recursively scan source files containing the sentinel literal.
- Zero-network tests install a socket/HTTP guard that raises on real connection attempts. A boolean simulation flag or pytest marker alone is insufficient.
- ResultPack tests use complete 64-character fixture hashes or dynamically computed hashes. Truncated SHAs and placeholders are invalid.
- B7-H is performed by the architect, not by an autonomous Goal. B7-A must end paused with unsigned label templates; B7-R starts only after signed first/second-pass timestamps validate.


## 1. Objective

Deliver B7 that passes ALL Core Exit Gates (A–H) and completes Stage 1 (Backend + API). Three streams:

**Stream A — Stable API (Gates A–E exposed, Gate H security):**
- Data API: update preview, plan_hash, confirmation, run status, cancel, rerun/resume, corpus/index identities
- News API: stable article ID, title, description, publisher, source_class, available_at, image_url, original_url, ticker links, provenance summary
- Chart API: OHLCV candles, session calendar, market/sector/peer context, freshness flags
- Attribution API: request identity, cutoff, ranked hypotheses, supporting/counter-evidence, unavailable evidence, output status, run assurance
- Trace API: retrieval stages, node events, decisions, model/prompt/manifest identities, latency, tokens, cost
- Run Status API: durable status, cancel
- Local security: loopback bind, Host/Origin allowlists, CORS closed, key memory-only, sentinel key redaction

**Stream B — Compact Evaluation (Gate F):**
- Grade 12 BenchmarkCase records (6 answerable + 2 abstain + 4 retrieval-only)
- Delayed second adjudication pass
- Label/pool freeze
- Recall@8, nDCG@8, primary_hit@8, unjudged@8 computation
- Reranker deltas (Δ nDCG@8, Δ primary_hit@8, improved/regressed cases)
- 2×2 answer/abstain matrix, abstain-reason match
- MetricRecord and ResultPack generation
- Markdown scorecard (deterministic)
- Zero-network replay gate
- Named-case component keep/kill decisions
- Tie → cheaper configuration wins

**Stream C — Quickstarts and Release (Gate G):**
- Key-free data quickstart (plan/plan_hash/corpus/cutoff-safe lexical query)
- Key-free agent quickstart (stub-model attribution with trace + RunAssuranceRecord + replay)
- Key-free eval quickstart (score fixture pack, regenerate scorecard deterministically, zero-network regression gate)
- Synthetic fixture (committed: ~2 tickers, ~15 sessions OHLCV, ~20 sanitized articles, 1 filing, 1 benchmark case, stub ModelClient)
- Network-disabled tests pass
- Backend release checklist

## 2. Current Verified State

### Stream A — API

| Component | File | Status |
|---|---|---|
| FastAPI app | `catalyst_app/main.py` | implemented |
| Workbench router | `catalyst_app/routers/workbench.py` | implemented — needs contract alignment |
| Live runs router | `catalyst_app/routers/live_runs.py` | implemented |
| Schemas | `catalyst_app/schemas.py` | implemented — needs News contract alignment |
| Workbench store | `catalyst_app/workbench_store.py` | implemented |
| Workspace projection | `catalyst_app/workspace_projection.py` | implemented |
| BYOK wiring | `catalyst_app/llm_factory.py`, `catalyst_app/env_loader.py` | implemented |
| Local security | `catalyst_app/main.py` | partial — loopback bind, needs Host/Origin/CORS |
| Provider validator | `catalyst_app/provider_validator.py` | implemented |

### Stream B — Evaluation

| Component | File | Status |
|---|---|---|
| BenchmarkCase schema | B4 deliverable | implemented — empty, no labels |
| Evidence judgment schema | B4 deliverable | implemented |
| Frozen eval harness | `catalyst_eval/harness/frozen_eval.py` | implemented |
| Metrics base | `catalyst_eval/metrics/base.py` | implemented — needs Recall@8, nDCG@8 |
| Judge cache | needs model-identity-in-key audit | partial |
| Reports (JSON, Markdown) | `catalyst_eval/reports/` | implemented — needs alignment |
| Regression gate | `packages/eval/scripts/check_p0_gate.py` | partial |

### Stream C — Quickstarts

| Component | Status |
|---|---|
| Synthetic fixture | missing — needs creation |
| Key-free quickstarts | missing |
| Network-disabled tests | partial — some tests may require network |

### Missing (all streams)

| Component | File to create |
|---|---|
| News API contract alignment | `catalyst_app/schemas.py` (modify) |
| Chart API router | `catalyst_app/routers/chart.py` (new) |
| Attribution API router | `catalyst_app/routers/attribution.py` (new) |
| Trace API router | `catalyst_app/routers/trace.py` (new) |
| Data/run status API extensions | `catalyst_app/routers/data.py` (new) |
| Security middleware | `catalyst_app/security.py` (new) |
| Retrieval metrics (Recall@8, nDCG@8) | `catalyst_eval/metrics/retrieval_metrics.py` (new) |
| Workflow metrics (2×2, abstain_reason) | `catalyst_eval/metrics/workflow_metrics.py` (new) |
| ResultPack | `catalyst_eval/packs/result_pack.py` (new) |
| Scorecard generator | `catalyst_eval/reports/scorecard.py` (new) |
| Zero-network replay | `catalyst_eval/replay/replay.py` (new) |
| Quickstart fixtures | `packages/data-core/tests/fixtures/` (new) |
| Quickstart scripts | `packages/data-core/scripts/quickstart.py` (new) |
| Stub ModelClient | `packages/agents/tests/fixtures/stub_client.py` (new) |

## 3. Scope / Non-goals

### Scope — Stream A

- Six stable API groups: Data, News, Chart, Attribution, Trace, Run Status
- News contract: stable article ID, title, description, publisher, source_class, available_at, image_url, original_url, ticker links, provenance summary
- Image URLs excluded from embedding text (presentation only)
- Local security: loopback, Host/Origin allowlist, CORS closed, keys memory-only, redaction

### Scope — Stream B

- 12 BenchmarkCase records with human labels
- Second adjudication pass (≥7 days after first)
- Recall@8, nDCG@8, primary_hit@8, unjudged@8
- Reranker deltas (named cases)
- 2×2 confusion matrix, abstain_reason_match
- MetricRecord, ResultPack, Markdown scorecard
- Zero-network replay
- Component keep/kill with named-case justification

### Scope — Stream C

- One committed synthetic fixture DB
- Three key-free offline quickstarts
- Network-disabled CI tests
- Backend release checklist

### Non-goals

- Composite score (prohibited)
- Self-reported confidence calibration (prohibited)
- MRR/Precision@K (prohibited)
- Generalized attribution accuracy claims (prohibited)
- Paper-style significance experiments (prohibited)
- Live judge in CI (prohibited)
- Frontend UI (Stage 2)
- SaaS/infrastructure/accounts/billing
- Benchmark metrics in normal UI

## 4. Dependencies

### Inputs

- B2–B6 completion
- Union judgment pools (from B6)
- Frozen labels (human, after B6 pools generated)
- Eval Foundation schemas (from B4)

### Output Artifacts

- Stable HTTP API (local)
- 12 labeled BenchmarkCase records
- ResultPack (JSON)
- Markdown scorecard
- Synthetic fixture DB
- Quickstart scripts

### Consumed By

- Stage 2 frontend (F1, F2)
- Portfolio/reviewer journey

## 5. Artifact Ownership

### Owned

- All API routes (Data, News, Chart, Attribution, Trace, Run Status)
- 12 BenchmarkCase labels
- MetricRecord pack
- ResultPack
- Markdown scorecard
- Synthetic fixture DB
- Quickstart scripts

### Not Owned

- Trace DB (agents owns)
- Corpus chunks (B3 owns)
- Retrieval manifests (B4/B6 owns)
- RunAssuranceRecord (B5 owns)

## 6. File Allowlist

### Stream A — New Files

```
packages/app/catalyst_app/routers/chart.py
packages/app/catalyst_app/routers/attribution.py
packages/app/catalyst_app/routers/trace.py
packages/app/catalyst_app/routers/data.py
packages/app/catalyst_app/security.py
packages/app/catalyst_app/context_provider.py
packages/app/tests/test_chart_api.py
packages/app/tests/test_data_api.py
packages/app/tests/test_news_api_contract.py
packages/app/tests/test_attribution_api.py
packages/app/tests/test_trace_api.py
packages/app/tests/test_security.py
```

### Stream A — Modified

```
packages/app/catalyst_app/main.py                  — security middleware
packages/app/catalyst_app/schemas.py               — News contract alignment
packages/app/catalyst_app/routers/workbench.py     — refactor into new routers
packages/app/catalyst_app/routers/live_runs.py     — extend
packages/app/tests/test_backend_api_contract.py    — extend
packages/app/tests/test_no_secret_leak.py          — extend for sentinel key
```

### Stream B — New Files

```
packages/eval/catalyst_eval/metrics/retrieval_metrics.py
packages/eval/catalyst_eval/metrics/workflow_metrics.py
packages/eval/catalyst_eval/packs/__init__.py
packages/eval/catalyst_eval/packs/result_pack.py
packages/eval/catalyst_eval/reports/scorecard.py
packages/eval/catalyst_eval/replay/__init__.py
packages/eval/catalyst_eval/replay/replay.py
packages/eval/catalyst_eval/adapters/union_pool.py
packages/eval/tests/test_retrieval_metrics.py
packages/eval/tests/test_workflow_metrics.py
packages/eval/tests/test_result_pack.py
packages/eval/tests/test_scorecard.py
packages/eval/tests/test_replay.py
packages/eval/tests/test_union_pool_adapter.py
```

### Stream B — Modified

```
packages/eval/catalyst_eval/metrics/__init__.py
packages/eval/catalyst_eval/reports/__init__.py
packages/eval/catalyst_eval/harness/frozen_eval.py
packages/eval/scripts/check_p0_gate.py
```

### Stream C — New Files

```
packages/data-core/tests/fixtures/synthetic_fixture.db
packages/data-core/tests/fixtures/build_synthetic_fixture.py
packages/data-core/tests/fixtures/synthetic_fixture_manifest.json
packages/data-core/scripts/quickstart.py
packages/agents/tests/fixtures/stub_client.py
packages/eval/scripts/generate_scorecard.py
packages/eval/tests/eval_fixtures.py
```

### Files Explicitly Forbidden

- `data/catalyst_eval_frozen_v2.db` — frozen SHA 0d97a7ec…, no migration, no overwrite
- `data/catalyst_dev_ws4b.db` — do not open writable during B7 implementation; use reviewed migrated copies and manifests

## 7. TDD Tasks

Execution order is intentionally non-numeric: **B7-A runs Tasks 1–7 and 9–11, then pauses. B7-H performs Task 8 outside Goal mode. B7-R resumes with Task 12 and the release verification ladder.**

Before Task 1, create `packages/eval/tests/eval_fixtures.py` with complete identity hashes, valid/invalid ResultPacks, judged and unjudged retrieval cases, and deterministic replay packs. Extend `packages/app/tests/runtime_fixture.py` with literal independent `EXPECTED_*` API responses, pagination IDs/cursors, update requests, manifest identities, trace events/artifacts, and temporary migrated DB builders referenced by Tasks 1–3. Expected cursors and response bodies may not call production encoders, Pydantic response models, routers, or store methods. API tests reuse app dependency overrides plus temporary DBs; no test reads the canonical Dev DB.

### Stream A Task 1: News API contract

**Step 1: Write News API contract test**

```python
# packages/app/tests/test_news_api_contract.py

from urllib.parse import quote

def test_news_article_has_all_contract_fields():
    """GET /api/news/articles/{article_id} returns all required fields."""
    response = client.get("/api/news/articles/poly:article1")

    assert response.status_code == 200
    data = response.json()
    required = ["article_id", "title", "description", "publisher",
                "source_class", "available_at", "image_url",
                "original_url", "ticker_links", "provenance_summary"]
    for field in required:
        assert field in data, f"Missing field: {field}"


def test_image_and_original_url_are_presentation_metadata():
    """News returns presentation links without exposing internal embedding text."""
    response = client.get("/api/news/articles/poly:article1")
    data = response.json()
    assert "image_url" in data
    assert "original_url" in data
    assert "embedding_text" not in data
    assert client.get("/api/news/articles/poly:article1/embedding_text").status_code == 404


def test_news_list_returns_deterministic_order():
    """News is ordered available_at DESC, article_id ASC with an exact cursor."""
    path = "/api/news/AAPL?from_ts=2026-01-01T00:00:00Z&to_ts=2026-01-06T00:00:00Z&limit=2"
    response1 = client.get(path)
    response2 = client.get(path)
    assert response1.status_code == 200
    ids1 = [a["article_id"] for a in response1.json()["articles"]]
    ids2 = [a["article_id"] for a in response2.json()["articles"]]
    assert ids1 == ids2 == EXPECTED_FIRST_PAGE_IDS
    assert response1.json()["next_cursor"] == EXPECTED_FIRST_PAGE_CURSOR

    page2 = client.get(path + "&cursor=" + quote(EXPECTED_FIRST_PAGE_CURSOR))
    assert [a["article_id"] for a in page2.json()["articles"]] == EXPECTED_SECOND_PAGE_IDS
    assert not (set(ids1) & set(EXPECTED_SECOND_PAGE_IDS))


def test_news_range_cursor_and_ticker_errors_are_exact():
    """Malformed ranges/cursors are 400; unknown ticker/article is 404."""
    assert client.get("/api/news/AAPL?from_ts=bad&to_ts=2026-01-06T00:00:00Z").status_code == 422
    assert client.get("/api/news/AAPL?from_ts=2026-01-06T00:00:00Z&to_ts=2026-01-01T00:00:00Z").status_code == 400
    assert client.get("/api/news/AAPL?from_ts=2026-01-01T00:00:00Z&to_ts=2026-01-06T00:00:00Z&cursor=bad").status_code == 400
    assert client.get("/api/news/UNKNOWN?from_ts=2026-01-01T00:00:00Z&to_ts=2026-01-06T00:00:00Z").status_code == 404
    assert client.get("/api/news/articles/missing").status_code == 404


def test_provenance_summary_in_news():
    """Provenance summary includes source provider and request lineage."""
    response = client.get("/api/news/articles/poly:article1")
    prov = response.json()["provenance_summary"]
    assert "provider" in prov
    assert prov["raw_lineage_count"] >= 1
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/app/tests/test_news_api_contract.py -q
```

Expected: FAIL — News API contract not aligned.

**Step 3: Implement**

Align `catalyst_app/schemas.py` and router to expose all contract fields from canonical `articles` plus B3 source class and B2 normalized provenance. Implement contract §9.4 `[from_ts,to_ts)`, limit 20/default and 100/max, ordering `available_at DESC, article_id ASC`, and URL-safe base64 canonical-JSON cursor containing the final sort keys. `runtime_fixture.py` owns the literal expected pages and cursor; tests do not call the production cursor encoder to compute expected values.

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/app/tests/test_news_api_contract.py -q
```

Expected: PASS.

### Stream A Task 2: Security middleware

**Step 1: Write security test**

```python
# packages/app/tests/test_security.py

def test_unknown_host_rejected():
    """Request with unknown Host header → 400."""
    response = client.get("/api/health", headers={"Host": "evil.example.com"})
    assert response.status_code in (400, 421, 403)


def test_unknown_origin_rejected():
    """CORS rejects unknown Origin."""
    response = client.options("/api/health", headers={"Origin": "https://evil.example.com"})
    assert "Access-Control-Allow-Origin" not in response.headers


def test_non_loopback_bind_rejected_by_default():
    """Starting server on non-loopback without override raises error."""
    from catalyst_app.main import create_app
    import uvicorn

    with pytest.raises(UnsafeBindAddressError):
        # Attempt non-loopback without override
        create_app(host="0.0.0.0", unsafe_allow_non_loopback=False)


def test_sentinel_key_not_in_response():
    """A sentinel API key never appears in any response body."""
    sentinel = secrets.token_urlsafe(32)
    response = client.post("/api/attribution", json={
        "ticker": "AAPL", "session_date": "2026-01-15",
        "api_key": sentinel,
    })
    assert sentinel not in response.text


def test_key_not_persisted():
    """Keys are memory-only; never written to files, DB, logs, traces, errors."""
    sentinel = secrets.token_urlsafe(32)
    result = run_with_captured_artifacts(api_key=sentinel, tmp_path=tmp_path, caplog=caplog)
    inspected = [result.response_body, caplog.text, dump_temp_db(result.trace_db), read_temp_files(tmp_path)]
    assert all(sentinel not in artifact for artifact in inspected)
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/app/tests/test_security.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_app/security.py`**

- Host allowlist middleware
- Origin allowlist middleware
- CORS closed by default
- Non-loopback bind guard
- Key redaction filter

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/app/tests/test_security.py -q
```

Expected: all PASS.

### Stream A Task 3: Data, Chart, Attribution, Trace routers

Inventory the existing `/api/tickers`, `/api/ohlcv/{ticker}`, `/api/news/{ticker}`, `/api/session/{ticker}`, `/api/live-runs`, event, cancel, retry, workspace, model, and health routes first. The News refactor replaces the existing `/api/news/{ticker}` handler in place and adds `/api/news/articles/{article_id}` for detail; do not add `/api/news/{article_id}`, which collides with the ticker-list route. Refactor or extend all other existing handlers behind typed schemas; create a new route only when no existing endpoint owns the capability. Add `SQLiteContextProvider` in app composition and inject it into B5 runtime. Implement contract §9.4 exactly; the tests below are required, not illustrative.

**Step 1: Write Data/update API tests**

```python
# packages/app/tests/test_data_api.py

def test_update_preview_is_zero_write_zero_network_and_identity_complete(tmp_path):
    db, before = prepared_update_api_db(tmp_path)
    response = client_with_no_network(db).post("/api/updates/preview", json=VALID_UPDATE_REQUEST)
    assert response.status_code == 200
    assert db_logical_dump(db) == before
    assert response.json() == EXPECTED_PREVIEW_RESPONSE


def test_execute_requires_matching_plan_hash_before_starting_run(tmp_path):
    client = prepared_update_client(tmp_path)
    stale = client.post("/api/updates/execute", json={**VALID_UPDATE_REQUEST,
                                                       "expected_plan_hash": "00" * 32})
    assert stale.status_code == 409
    assert stale.json()["code"] == "plan_drift"
    assert ingestion_run_count(client.db) == 0

    accepted = client.post("/api/updates/execute", json={**VALID_UPDATE_REQUEST,
                                                          "expected_plan_hash": EXPECTED_PLAN_HASH})
    assert accepted.status_code == 202
    assert accepted.json() == EXPECTED_EXECUTE_RESPONSE


def test_update_status_cancel_resume_and_conflict_contracts(tmp_path):
    client = seeded_update_run_client(tmp_path)
    assert client.get("/api/updates/run-1").json() == EXPECTED_UPDATE_STATUS
    assert client.post("/api/updates/run-1/cancel").json() == EXPECTED_CANCEL_RESPONSE
    assert client.post("/api/updates/run-1/cancel").json() == EXPECTED_CANCEL_RESPONSE
    resumed = client.post("/api/updates/run-1/resume",
                          json={"expected_plan_hash": EXPECTED_PLAN_HASH})
    assert resumed.status_code == 202
    assert resumed.json() == EXPECTED_RESUME_RESPONSE
    assert active_writer_conflict(client).status_code == 409


def test_capability_and_identity_responses_never_contain_secrets_or_paths(tmp_path):
    client = prepared_update_client(tmp_path)
    for path, expected in [("/api/capabilities", EXPECTED_CAPABILITIES),
                           ("/api/data/identities", EXPECTED_DATA_IDENTITIES)]:
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == expected
        assert_no_secret_or_local_path(response.text)
```

**Step 2: Write Chart/context API tests**

```python
# packages/app/tests/test_chart_api.py

def test_ohlcv_contract_order_freshness_and_missing_sessions():
    response = client.get("/api/ohlcv/AAPL?start_date=2026-01-02&end_date=2026-01-08")
    assert response.status_code == 200
    assert response.json() == EXPECTED_OHLCV_RESPONSE
    assert [row["date"] for row in response.json()["candles"]] == EXPECTED_ASCENDING_DATES


def test_ohlcv_valid_empty_unknown_and_range_errors_are_distinct():
    assert client.get("/api/ohlcv/EMPTY?start_date=2026-01-02&end_date=2026-01-08").json() == EXPECTED_EMPTY_OHLCV
    assert client.get("/api/ohlcv/UNKNOWN?start_date=2026-01-02&end_date=2026-01-08").status_code == 404
    assert client.get("/api/ohlcv/AAPL?start_date=bad&end_date=2026-01-08").status_code == 422
    assert client.get("/api/ohlcv/AAPL?start_date=2000-01-01&end_date=2026-01-08").status_code == 400


def test_session_context_contains_formula_inputs_reasons_cutoff_and_manifests():
    response = client.get("/api/session/AAPL?trade_date=2026-01-08")
    assert response.status_code == 200
    assert response.json() == EXPECTED_SESSION_CONTEXT_RESPONSE
    assert client.get("/api/session/AAPL?trade_date=2026-01-10").json()["is_trading_day"] is False
```

**Step 3: Write Attribution and Trace API tests**

```python
# packages/app/tests/test_attribution_api.py

def test_attribution_submission_and_summary_bind_all_identities():
    created = client.post("/api/live-runs", json=VALID_ATTRIBUTION_REQUEST)
    assert created.status_code == 202
    assert created.json() == EXPECTED_ATTRIBUTION_CREATED
    summary = client.get(f"/api/live-runs/{created.json()['run_id']}")
    assert summary.json() == EXPECTED_ATTRIBUTION_SUMMARY


def test_workspace_citations_resolve_and_benchmark_metrics_are_absent():
    workspace = client.get("/api/live-runs/run-complete/workspace").json()
    evidence_ids = {item["chunk_id"] for item in workspace["evidence"]}
    cited_ids = {cid for h in workspace["ranked_hypotheses"]
                 for cid in h["supporting_evidence_ids"] + h["counter_evidence_ids"]}
    assert cited_ids <= evidence_ids
    assert "benchmark_metrics" not in workspace
    assert workspace == EXPECTED_WORKSPACE_RESPONSE
```

```python
# packages/app/tests/test_trace_api.py

def test_trace_after_seq_order_and_versioned_serialization():
    response = client.get("/api/live-runs/run-complete/events?after_seq=2")
    assert response.status_code == 200
    assert response.json() == EXPECTED_EVENTS_AFTER_2
    assert [e["event_seq"] for e in response.json()] == [3, 4, 5]


def test_artifact_filters_order_empty_missing_and_invalid_type():
    assert client.get("/api/live-runs/run-complete/artifacts?event_seq=3&artifact_type=graded_evidence").json() == EXPECTED_GRADED_ARTIFACTS
    assert client.get("/api/live-runs/run-empty/artifacts").json() == []
    assert client.get("/api/live-runs/missing/artifacts").status_code == 404
    assert client.get("/api/live-runs/run-complete/artifacts?artifact_type=invalid").status_code == 422
```

All `EXPECTED_*` objects come from `packages/app/tests/runtime_fixture.py` as literal independent contract fixtures. They may not be generated from the response models or router implementation.

**Step 4: Implement the typed routers and schemas**

- `routers/data.py`: all seven Data/update routes and exact 409 semantics from contract §9.4;
- `routers/chart.py`: extend existing OHLCV/session ownership without duplicate paths;
- `routers/attribution.py`: extend the existing live-run handlers and identity-complete response schemas;
- `routers/trace.py`: deterministic event/artifact ordering and versioned envelopes;
- `schemas.py`: explicit request/response models for every contract field plus one common `ErrorPayload`;
- `context_provider.py`: app-owned `SQLiteContextProvider` injected into B5 runtime;
- `main.py`: register each route once; a route-uniqueness test compares exact `(method,path)` pairs.

Do not create compatibility aliases that make two handlers own the same method/path. Existing external paths named in contract §9.4 remain stable; internal functions may move.

**Step 5: Run focused and package tests**

```bash
.venv/bin/python -m pytest packages/app/tests/test_data_api.py -q
.venv/bin/python -m pytest packages/app/tests/test_chart_api.py -q
.venv/bin/python -m pytest packages/app/tests/test_attribution_api.py -q
.venv/bin/python -m pytest packages/app/tests/test_trace_api.py -q
.venv/bin/python -m pytest packages/app -q
```

Expected: focused tests and the package suite PASS with exact new-test counts reported; do not use `128+` as an assertion.

### Stream B Task 4: Retrieval metrics

**Step 1: Write metrics test**

```python
# packages/eval/tests/test_retrieval_metrics.py

import pytest

from catalyst_eval.metrics.retrieval_metrics import MetricStatus, compute_dcg_at_k
from catalyst_eval.benchmark.judgment import EvidenceJudgment


def judged(grades):
    return {
        chunk_id: EvidenceJudgment(
            chunk_id=chunk_id,
            grade=grade,
            rationale=f"fixture grade {grade}",
            annotator="fixture",
            judged_at="2026-07-22T00:00:00Z",
        )
        for chunk_id, grade in grades.items()
    }

def test_recall_at_8():
    """Recall uses explicit judgments_by_chunk_id; IDs never encode grades."""
    from catalyst_eval.metrics.retrieval_metrics import compute_recall_at_k

    top8 = ["a", "b", "c", "d", "e", "f", "g", "h"]
    judgments = judged({"a": 2, "b": 0, "c": 1, "d": 0, "e": 0,
                        "f": 0, "g": 0, "h": 0, "x": 1, "y": 1, "z": 2})
    result = compute_recall_at_k(top8, judgments, k=8)
    assert result.status == MetricStatus.OK
    assert result.value == 2 / 5
    assert (result.numerator, result.denominator) == (2, 5)


def test_ndcg_at_8():
    """nDCG@8 = DCG@8 / IDCG@8 with grades 2/1/0."""
    from catalyst_eval.metrics.retrieval_metrics import compute_ndcg_at_k

    top8 = ["a", "b", "c", "d", "e", "f", "g", "h"]
    judgments = judged({"a": 2, "b": 0, "c": 1, "d": 2, "e": 0,
                        "f": 0, "g": 0, "h": 0, "x": 2, "y": 1})
    result = compute_ndcg_at_k(top8, judgments, k=8)
    expected_idcg = compute_dcg_at_k([2, 2, 2, 1, 1, 0, 0, 0], k=8)
    expected_dcg = compute_dcg_at_k([2, 0, 1, 2, 0, 0, 0, 0], k=8)
    assert result.status == MetricStatus.OK
    assert result.value == pytest.approx(expected_dcg / expected_idcg)
    assert result.denominator == pytest.approx(expected_idcg)


def test_ndcg_formula():
    """DCG@8 = sum((2^rel_i - 1) / log2(i+1))."""
    from catalyst_eval.metrics.retrieval_metrics import compute_dcg_at_k
    import math

    grades = [2, 1, 0]
    dcg = compute_dcg_at_k(grades, k=3)
    expected = (2**2 - 1) / math.log2(2) + (2**1 - 1) / math.log2(3) + 0
    assert dcg == pytest.approx(expected)


def test_unjudged_returns_needs_judgment_not_a_numeric_metric():
    """Any unjudged top-K chunk blocks the point estimate."""
    from catalyst_eval.metrics.retrieval_metrics import compute_ndcg_at_k

    result = compute_ndcg_at_k(["a", "missing", "b"], judged({"a": 2, "b": 0}), k=3)
    assert result.status == MetricStatus.NEEDS_JUDGMENT
    assert result.value is None
    assert result.unjudged_ids == ["missing"]


def test_no_relevant_evidence_is_not_applicable_not_zero():
    """Recall/nDCG/primary-hit return NOT_APPLICABLE when their denominator is absent."""
    from catalyst_eval.metrics.retrieval_metrics import (
        compute_ndcg_at_k, compute_primary_hit, compute_recall_at_k,
    )

    judgments = judged({"a": 0, "b": 0})
    for result in (
        compute_recall_at_k(["a", "b"], judgments, k=8),
        compute_ndcg_at_k(["a", "b"], judgments, k=8),
        compute_primary_hit(["a", "b"], judgments, k=8),
    ):
        assert result.status == MetricStatus.NOT_APPLICABLE
        assert result.value is None


def test_primary_hit_at_8():
    """primary_hit@8 = 1 when any grade-2 chunk in top-8."""
    from catalyst_eval.metrics.retrieval_metrics import compute_primary_hit

    top8 = ["a", "b", "c"]
    judgments = judged({"a": 2, "b": 1, "c": 0, "x": 2})
    hit = compute_primary_hit(top8, judgments, k=8)
    assert hit.status == MetricStatus.OK
    assert hit.value == 1

    no_hit = compute_primary_hit(["b", "c"], judgments, k=8)
    assert no_hit.status == MetricStatus.OK
    assert no_hit.value == 0
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_retrieval_metrics.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_eval/metrics/retrieval_metrics.py`**

- `MetricValue(value, status, numerator, denominator, unjudged_ids)` and `MetricStatus`
- `compute_recall_at_k(top_ids, judgments_by_chunk_id, k=8)`
- `compute_dcg_at_k(grades, k=8)`
- `compute_ndcg_at_k(top_ids, judgments_by_chunk_id, k=8)`; IDCG uses all judged pool grades sorted descending
- `compute_primary_hit(top_ids, judgments_by_chunk_id, k=8)`
- `classify_unjudged(top_ids, judgments_by_chunk_id, k=8)`
- exact `OK`, `NOT_APPLICABLE`, and `NEEDS_JUDGMENT` behavior from contract §9.2

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_retrieval_metrics.py -q
```

Expected: all PASS.

### Stream B Task 5: Workflow metrics

**Step 1: Write workflow metrics test**

```python
# packages/eval/tests/test_workflow_metrics.py

def test_answer_abstain_matrix_2x2():
    """2×2 confusion: expected (answerable/abstain) × actual (answer/abstain)."""
    from catalyst_eval.metrics.workflow_metrics import compute_confusion_matrix

    cases = [
        {"case_id": "R1", "expected_answerable": True, "actual_abstained": False},
        {"case_id": "R2", "expected_answerable": True, "actual_abstained": False},
        {"case_id": "A1", "expected_answerable": False, "actual_abstained": True},
        {"case_id": "A2", "expected_answerable": False, "actual_abstained": False},  # wrong!
    ]
    matrix = compute_confusion_matrix(cases)
    assert matrix["answerable_answered"] == 2
    assert matrix["abstain_abstained"] == 1
    assert matrix["abstain_answered"] == 1  # A2


def test_abstain_reason_match():
    """abstain_reason_match = correct abstention reason class / expected-abstain cases."""
    from catalyst_eval.metrics.workflow_metrics import compute_abstain_reason_match

    abstain_cases = [
        {"case_id": "A1", "expected_reason": "insufficient_evidence", "actual_reason": "insufficient_evidence"},
        {"case_id": "A2", "expected_reason": "context_quality", "actual_reason": "insufficient_evidence"},
    ]
    match_rate = compute_abstain_reason_match(abstain_cases)
    assert match_rate == 0.5
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_workflow_metrics.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_eval/metrics/workflow_metrics.py`**

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_workflow_metrics.py -q
```

Expected: all PASS.

### Stream B Task 6: ResultPack and scorecard

**Step 1: Write ResultPack test**

```python
# packages/eval/tests/test_result_pack.py

def test_result_pack_validates():
    """ResultPack with missing identity field fails validation."""
    from catalyst_eval.packs.result_pack import ResultPack

    pack = ResultPack(
        pack_version="1.0.0",
        # Missing corpus_snapshot_sha → validation failure
        corpus_snapshot_sha=None,
    )
    errors = pack.validate()
    assert len(errors) > 0
    assert any("corpus_snapshot" in e.lower() for e in errors)


def test_result_pack_all_required_fields_valid():
    """Complete pack with all identity fields passes validation."""
    from catalyst_eval.packs.result_pack import ResultPack

    pack = ResultPack(
        pack_version="1.0.0",
        created_at="2026-07-21T00:00:00Z",
        corpus_snapshot_sha="92" * 32,
        index_manifest_id="index-v1",
        code_commit_sha="ab" * 20,
        prompt_shas={"critic": "cd" * 32, "judge": "ef" * 32},
        generation_model_identities={"critic": "gpt-4o", "judge": "gpt-4o"},
        judge_model_identity="gpt-4o",
        judge_rubric_version="1.0.0",
        benchmark_dataset_version="1.0.0",
        chunk_profile_versions={"news": "news_v2"},
        metric_records=[],
    )
    assert pack.validate() == []


def test_scorecard_deterministic():
    """Same pack → byte-identical Markdown scorecard."""
    from catalyst_eval.reports.scorecard import generate_scorecard

    pack = make_valid_pack()
    sc1 = generate_scorecard(pack)
    sc2 = generate_scorecard(pack)
    assert sc1 == sc2  # byte-identical
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_result_pack.py -q
.venv/bin/python -m pytest packages/eval/tests/test_scorecard.py -q
```

Expected: FAIL.

**Step 3: Implement**

- `catalyst_eval/packs/result_pack.py`: `ResultPack`, `MetricRecord`
- `catalyst_eval/reports/scorecard.py`: `generate_scorecard(pack) → str`

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_result_pack.py packages/eval/tests/test_scorecard.py -q
```

Expected: all PASS.

### Stream B Task 7: Zero-network replay

**Step 1: Write replay test**

```python
# packages/eval/tests/test_replay.py

def test_zero_network_replay_succeeds():
    """Replay from committed pack + judge cache reproduces byte-identical artifacts."""
    from catalyst_eval.replay.replay import replay_pack

    pack_dir = "data/eval_packs/test_pack_v1"
    with deny_all_network():
        result = replay_pack(pack_dir)

    assert result.success
    assert result.artifacts_match  # byte-identical


def test_zero_network_replay_fails_on_real_socket_attempt():
    """A replay component that opens a socket is rejected by the real guard."""
    from catalyst_eval.replay.replay import replay_pack

    with deny_all_network(), pytest.raises(NetworkAccessDuringReplay):
        replay_pack("data/eval_packs/socket-attempt-pack")
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_replay.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_eval/replay/replay.py`**

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_replay.py -q
```

Expected: PASS.

### Stream B Task 7b: Adapt B6 union pools without reversing dependencies

**Step 1: Write the failing adapter tests**

```python
# packages/eval/tests/test_union_pool_adapter.py

def test_union_pool_adapter_preserves_all_lineage():
    from catalyst_eval.adapters.union_pool import adapt_union_pool

    source = {
        "schema_version": "1.0.0",
        "case_id": "B001",
        "chunk_ids": ["c1", "c2"],
        "per_arm_chunk_ids": {"fts5": ["c1"], "dense": ["c2"]},
        "corpus_manifest_id": "corpus-v1",
        "index_manifest_id": "index-v1",
        "source_artifact_id": "retrieval-arm-artifact-v1",
    }
    manifest = adapt_union_pool(source, pool_id="pool-B001",
                                created_at="2026-07-21T00:00:00Z")

    assert manifest.schema_version == "1.0.0"
    assert manifest.case_id == source["case_id"]
    assert manifest.chunk_inventory == source["chunk_ids"]
    assert {arm["arm"]: arm["chunk_ids"] for arm in manifest.arms} == source["per_arm_chunk_ids"]
    assert manifest.corpus_manifest_id == source["corpus_manifest_id"]
    assert manifest.index_manifest_id == source["index_manifest_id"]
    assert manifest.source_artifact_id == source["source_artifact_id"]


def test_union_pool_adapter_rejects_unknown_version_or_missing_identity():
    from catalyst_eval.adapters.union_pool import UnsupportedPoolVersion, adapt_union_pool

    source = make_valid_union_pool_dict()
    source["schema_version"] = "2.0.0"
    with pytest.raises(UnsupportedPoolVersion):
        adapt_union_pool(source, pool_id="pool-B001", created_at="2026-07-21T00:00:00Z")

    source = make_valid_union_pool_dict()
    del source["corpus_manifest_id"]
    with pytest.raises(ValueError, match="corpus_manifest_id"):
        adapt_union_pool(source, pool_id="pool-B001", created_at="2026-07-21T00:00:00Z")
```

**Step 2: Run the focused tests**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_union_pool_adapter.py -q
```

Expected: FAIL.

**Step 3: Implement the eval-owned adapter**

Implement `catalyst_eval.adapters.union_pool` against plain mappings/JSON only. It must not import `catalyst_data`; it validates B6 schema version `1.0.0`, rejects missing identities, and constructs the B4-owned eval `PoolManifest` without dropping or recomputing `corpus_manifest_id`, `index_manifest_id`, or `source_artifact_id` lineage.

**Step 4: Run the focused tests**

```bash
.venv/bin/python -m pytest packages/eval/tests/test_union_pool_adapter.py -q
```

Expected: PASS.

### B7-H Task 8: Human labeling protocol — mandatory pause

Labels are authored outside autonomous execution. B7-A writes unsigned templates and stops. The architect then:
1. Load each B6 `UnionJudgmentPool` JSON through `catalyst_eval.adapters.union_pool`, require supported `schema_version`, validate all identities, and create the eval-owned `PoolManifest`; the adapter must preserve `case_id`, `chunk_ids` as `chunk_inventory`, per-arm provenance as `arms`, `corpus_manifest_id`, `index_manifest_id`, and `source_artifact_id` exactly. Eval consumes plain JSON and data-core never imports eval
2. Grade each chunk 2/1/0 with rationale
3. Write acceptable cause labels from graded evidence only
4. Delayed second pass ≥ 7 days later
5. Freeze labels and pool manifests
6. Record real per-action timestamps (no batch stamps)

After labels freeze, validate lineage and signatures. Do not start B7-R until the second-pass timestamp is at least seven days after the first-pass completion and every pool identity still matches B6.

### Stream C Task 9: Deterministic synthetic fixture

Create a small fixture plus a deterministic builder and content manifest:
- `packages/data-core/tests/fixtures/synthetic_fixture.db`
- `packages/data-core/tests/fixtures/build_synthetic_fixture.py`
- `packages/data-core/tests/fixtures/synthetic_fixture_manifest.json`
- ~2 tickers, ~15 sessions OHLCV, ~20 sanitized articles with provenance, 1 filing, 1 benchmark case
- No provider payloads, no secrets, no licensed content

Two consecutive builds must produce identical logical table dumps and manifest hashes; SQLite file-byte identity is not required. The fixture becomes tracked only when the architect authorizes the corresponding Git boundary.

### Stream C Task 10: Key-free quickstarts

1. **Data quickstart:** `packages/data-core/scripts/quickstart.py` — plan update, print plan_hash, build corpus, run one FTS5 query
2. **Agent quickstart:** stub ModelClient run with trace + RunAssuranceRecord + replay
3. **Eval quickstart:** score fixture pack, regenerate scorecard, run regression gate

### Stream C Task 11: Network-disabled tests

Add `@pytest.mark.no_network` to the committed offline contract tests. An autouse fixture for that marker monkeypatches socket connection functions and configured HTTP transports to raise `NetworkAccessDuringTest`; the marker is selection metadata, not the enforcement mechanism.

```bash
.venv/bin/python -m pytest packages/data-core -q -m no_network --no-header
.venv/bin/python -m pytest packages/agents -q -m no_network --no-header
.venv/bin/python -m pytest packages/eval -q -m no_network --no-header
```

### B7-R Task 12: Keep/kill decisions and release

After all metrics computed:
- Cite specific cases that moved for each keep decision
- Asymmetric standard: killing is valid on absence of benefit; keeping requires named-case evidence
- Tie → cheaper configuration
- Reranker implementation is mandatory; retaining it is optional
- Record all decisions in ResultPack

## 8. Landmine Tests

1. **Composite score computed** — `rg "composite\|overall.*score\|aggregate.*score\|Catalyst Score" packages/eval/catalyst_eval/` must return zero in metric code.
2. **MRR/Precision@K used** — `rg "MRR\|Precision@K\|mrr\|precision_at" packages/eval/catalyst_eval/metrics/` must return zero.
3. **Benchmark metric in normal UI** — API contract test verifies no metric field in normal response schemas.
4. **Frozen DB overwritten** — SHA check after B7: `0d97a7ec61…` must still match.
5. **Unjudged treated as grade 0** — `test_unjudged_is_not_zero` in retrieval metrics.
6. **Keep/kill without named-case justification** — verify every keep decision has case IDs in ResultPack.
7. **Missing identity fields in ResultPack** — `test_result_pack_validates`.
8. **Security: key leaked to response** — `test_sentinel_key_not_in_response`.
9. **Network called during zero-network replay** — `test_zero_network_replay_fails_on_real_socket_attempt`.
10. **Duplicate route ownership** — enumerate `(method, path)` pairs from FastAPI and fail on duplicates or shadowed legacy routes.
11. **B7-R starts before adjudication delay** — reject labels whose second-pass start is less than seven days after first-pass completion or whose pool identity changed.
12. **Internal embedding text exposed** — News schemas and route inventory contain no `embedding_text` field or endpoint.
13. **Pool identity lost in adaptation** — `test_union_pool_adapter.py` asserts exact case ID, chunk union, per-arm membership, corpus manifest ID, and index manifest ID before any grade is accepted.

## 9. Verification Ladder

```bash
# Stream A
.venv/bin/python -m pytest packages/app/tests/test_news_api_contract.py -q
.venv/bin/python -m pytest packages/app/tests/test_security.py -q
.venv/bin/python -m pytest packages/app/tests/test_chart_api.py -q
.venv/bin/python -m pytest packages/app/tests/test_attribution_api.py -q
.venv/bin/python -m pytest packages/app/tests/test_trace_api.py -q
.venv/bin/python -m pytest packages/app/tests/test_no_secret_leak.py -q
.venv/bin/python -m pytest packages/app -q

# Stream B
.venv/bin/python -m pytest packages/eval/tests/test_retrieval_metrics.py -q
.venv/bin/python -m pytest packages/eval/tests/test_workflow_metrics.py -q
.venv/bin/python -m pytest packages/eval/tests/test_result_pack.py -q
.venv/bin/python -m pytest packages/eval/tests/test_scorecard.py -q
.venv/bin/python -m pytest packages/eval/tests/test_replay.py -q
.venv/bin/python -m pytest packages/eval -q

# Stream C
.venv/bin/python -m pytest packages/data-core -q -m no_network
.venv/bin/python -m pytest packages/agents -q -m no_network
.venv/bin/python -m pytest packages/eval -q -m no_network

# Full canonical (all 4 packages)
.venv/bin/python -m pytest packages/data-core -q
.venv/bin/python -m pytest packages/agents -q
.venv/bin/python -m pytest packages/eval -q
.venv/bin/python -m pytest packages/app -q

# DB SHA
shasum -a 256 data/catalyst_dev_ws4b.db
shasum -a 256 data/catalyst_eval_frozen_v2.db

# Git
git diff --check
git diff --cached --name-status
```

## 10. Evidence Report Template

```markdown
## B7 Completion Report — Backend + API Complete

### Stream A: API
- Routers: Data, News, Chart, Attribution, Trace, Run Status
- Security: loopback ✓, Host allowlist ✓, Origin allowlist ✓, CORS closed ✓, keys memory-only ✓

### Stream B: Evaluation
- BenchmarkCases labeled: 12/12
- Second pass completed: [date]
- Recall@8 (mean): X
- nDCG@8 (mean): X
- primary_hit@8: X/Y
- Abstain reasoning match: X/Y
- Component decisions: [list with named-case justification]
- ResultPack validated: ✓
- Scorecard generated: ✓
- Zero-network replay: PASS

### Stream C: Quickstarts
- Synthetic fixture: committed (N tickers, M sessions, P articles)
- Data quickstart: PASS
- Agent quickstart: PASS
- Eval quickstart: PASS
- Network-disabled tests: PASS

### Canonical Counts (final)
- data-core: X passed
- agents: X passed
- eval: X passed
- app: X passed

### Core Exit Gates
- Gate A (Data lineage): PASS
- Gate B (Corpus/chunking): PASS
- Gate C (Retrieval): PASS
- Gate D (Agent workflow): PASS
- Gate E (Runtime assurance): PASS
- Gate F (Evaluation): PASS
- Gate G (Contributor experience): PASS
- Gate H (Local security): PASS

### DB SHA (final)
- Dev DB: [final SHA]
- Frozen DB: 0d97a7ec61… (unchanged)

### Git Index
- [git status --short]
- Index empty: [yes/no]

### Stage 1 Complete
- Backend + API complete. Stage 2 (frontend) may begin.
```

## 11. Git Boundaries

**Stream A:**
1. `feat(app): align News API with contract fields`
2. `feat(app): add local security middleware`
3. `feat(app): add Chart, Attribution, Trace, Data routers`
4. `feat(app): add Run Status API extensions`

**Stream B:**
5. `feat(eval): add retrieval metrics (Recall@8, nDCG@8, primary_hit@8)`
6. `feat(eval): add workflow metrics (2x2, abstain_reason_match)`
7. `feat(eval): add ResultPack validation and Markdown scorecard`
8. `feat(eval): add zero-network replay gate`
9. `data(benchmark): add 12 BenchmarkCase labels` (separate from code)
10. `docs(eval): add scorecard` (separate from code and labels)

**Stream C:**
11. `test(fixtures): add synthetic fixture DB and quickstarts`
12. `test(all): add network-disabled marker and CI guard`

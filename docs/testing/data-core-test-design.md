> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# Data-Core Test Design

**Project:** Catalyst  
**Scope:** `packages/data-core` (`catalyst-data`)  
**Status:** Active working test strategy for Phase 1 validation and Phase 2/3 safety  
**Last Updated:** 2026-04-04

## 1. Purpose

This document defines how `catalyst-data` is validated beyond basic unit correctness.

The goal is not only to prove that individual functions work against fixtures. The goal is to establish confidence that the current data-core can survive real provider behavior:

- empty-but-successful responses
- malformed or drifting payloads
- timeouts and rate limits
- partial endpoint failure
- async concurrency pressure
- retry and fallback paths
- repeated runs against the same request

This is the acceptance layer between "Phase 1 code exists" and "Phase 1 data-core is trustworthy enough to support eval and agent work."

## 2. Goals

This test design should answer the following questions:

1. Can the current pipeline fetch, clean, deduplicate, transform, and persist data correctly?
2. Does the system behave correctly when providers return incomplete, empty, slow, or failing responses?
3. Are Bronze and Silver writes stable and idempotent across reruns?
4. Do transformed outputs preserve grounding-critical fields such as source attribution and URLs?
5. Are live-provider behaviors covered by a controlled validation process outside default CI?

## 3. Non-Goals

This document does not attempt to define:

- the full eval package test strategy
- the LangGraph agent test strategy
- frontend or API testing
- performance benchmarking beyond basic smoke expectations

This document also does not treat Phase 1 as equivalent to the final `catalyst-data` package from the full system spec. Gold-layer indexing, broader provider support, and downstream agent integration are outside current scope.

## 4. Current Risk Assessment

The current repository already has meaningful unit and mocked integration coverage. That is necessary, but still incomplete in the following areas:

- failure injection is not yet systematic
- provider schema drift expectations are not fully documented
- idempotent rerun behavior is not yet defined as an acceptance criterion
- live validation exists as a smoke script but does not yet have a formal test matrix
- package contracts are not fully aligned

Important contract issue:

- [`packages/data-core/catalyst_data/models.py`](/Users/yiannischen/Desktop/Catalyst/packages/data-core/catalyst_data/models.py) still models an older monolithic `DataAsset` shape
- [`packages/data-core/catalyst_data/storage/sqlite.py`](/Users/yiannischen/Desktop/Catalyst/packages/data-core/catalyst_data/storage/sqlite.py) implements the new Bronze/Silver split

This mismatch should be tracked as a Phase 1.5 cleanup item because later phases should not be forced to bridge two competing storage contracts.

Phase 1 closeout decision:

- Phase 2 evaluation work must not depend on the monolithic `DataAsset` model as a representation of persisted Bronze/Silver storage.
- Until the model is aligned or replaced, evaluation code should read the SQLite Bronze/Silver reality directly through storage-layer helpers or explicit query results.

## 5. Test Layers

Validation for `catalyst-data` is divided into five layers.

### Layer A: Unit Tests

Purpose:

- validate deterministic logic with no network and no SQLite file IO beyond local memory DBs

Targets:

- source mapping
- retry classification and backoff
- dedup fingerprinting
- alignment logic
- markdown conversion
- SQLite helper behavior
- rate limiter core behavior

Requirements:

- fast
- deterministic
- runs in CI on every push/PR

Examples:

- `map_logical_source("fmp_fundamentals")` returns the expected endpoint list
- two near-identical titles in the same 2-hour window deduplicate
- empty-but-successful ingest payload remains valid
- Polygon-shaped article transforms preserve source and reference URL
- limiter enforces real in-flight concurrency cap

### Layer B: Mocked Integration Tests

Purpose:

- validate multi-step pipeline behavior using provider-realistic payloads without real network access

Targets:

- connector output -> ingest -> clean -> transform -> storage
- orchestrator per-source summaries
- Bronze/Silver persistence
- partial success behavior across multiple endpoints

Requirements:

- runs in CI on every push/PR
- uses realistic saved fixtures or explicit response stubs
- covers both happy paths and degraded paths

Examples:

- Polygon news fixture flows into Markdown with references
- FMP multi-endpoint fundamentals merge correctly
- one endpoint in a logical source fails while others still produce usable output
- rerunning the same request upserts stably instead of producing inconsistent state

### Layer C: Failure-Injection Tests

Purpose:

- verify behavior under failure, slowness, rate limit, and contract drift

Targets:

- retry classification
- timeout handling
- structured error reporting
- fallback routing
- async behavior under contention

Requirements:

- runs in CI when feasible
- can be implemented using mocks/stubs without real network
- should focus on high-value scenarios, not exhaustive combinatorics

Required scenarios:

- HTTP 429
- HTTP 500/502/503/504
- timeout / slow request
- one endpoint failing while sibling endpoints succeed
- malformed JSON shape
- missing expected fields
- unsupported endpoint or unmapped source
- cancellation-safe or interruption-safe orchestrator behavior where applicable

Expected outcomes must be explicit:

- retryable vs non-retryable classification
- fallback vs no fallback
- structured error result instead of crash
- successful partial pipeline result when business logic allows it

### Layer D: Storage and Rerun Validation

Purpose:

- establish data persistence semantics, especially for repeated processing of the same request

Targets:

- `compute_asset_id`
- Bronze upsert behavior
- Silver upsert behavior
- repeated `process_request()` behavior against the same ticker/date/source

Required assertions:

- asset IDs are deterministic
- repeated processing of the same request does not create duplicate logical records
- repeated processing updates the same rows consistently
- empty-success days are persisted as valid outputs where expected
- partial failures do not corrupt previously valid stored state

This layer matters because later eval and agent stages will assume that `data-core` storage is stable and queryable over reruns.

### Layer E: Live Validation

Purpose:

- validate real provider behavior that mocked tests cannot guarantee

Targets:

- live fetch shape
- provider field drift
- real no-news days
- real earnings/news-heavy days
- live rate-limit or degraded behavior

Requirements:

- does not run in default CI
- requires API keys
- manual or scheduled execution only
- results should be logged or saved as artifacts for inspection

Primary tool:

- [`packages/data-core/scripts/smoke_test.py`](/Users/yiannischen/Desktop/Catalyst/packages/data-core/scripts/smoke_test.py)

## 6. Execution Modes

Three execution modes should be defined and used consistently.

### Mode 1: Default CI

Runs:

- unit tests
- mocked integration tests
- selected failure-injection tests

Must not require:

- network
- API keys
- flaky timing assumptions

Suggested command shape:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/data-core
python -m pytest tests/ -v --tb=short
```

### Mode 2: Local Deep Validation

Runs:

- targeted failure-injection suites
- rerun/idempotency suites
- smoke-test dry runs with controlled inputs

Purpose:

- before Phase 2/3 work
- before tagging a milestone
- before any release-worthy commit claiming Phase 1 acceptance

### Mode 3: Live Validation

Runs:

- real network calls with configured providers
- fixed ticker/date matrix

Purpose:

- verify real-world behavior
- detect payload drift
- confirm source attribution and references survive end-to-end

Must be:
- manual or scheduled only
- outside default CI
- recorded with a fixed operator checklist and source matrix

Live validation matrix:

- [`docs/testing/data-core-live-validation-matrix.md`](/Users/yiannischen/Desktop/Catalyst/docs/testing/data-core-live-validation-matrix.md)

- manual or scheduled
- outside default CI

## 7. Source-by-Source Test Matrix

Each logical source needs both happy-path and degraded-path coverage.

### `polygon_news`

Must validate:

- successful news fetch with standard Polygon payload
- successful no-news day with empty `results`
- malformed article item with missing fields
- transformation preserves:
  - title
  - `publisher.name`
  - `article_url`
  - `published_utc`
- dedup removes near-duplicates within the intended window
- quiet day is not treated as an ingest failure

### `polygon_ohlcv`

Must validate:

- successful single-day OHLCV payload
- empty or missing `results` behavior
- malformed bar structure behavior
- storage into `ohlcv` is correct and stable

### `fmp_fundamentals`

Must validate:

- all three endpoints succeed
- one endpoint fails while others succeed
- empty list response from one endpoint
- merge behavior takes the first element from non-empty lists
- transformed Markdown remains deterministic across reruns

### `yfinance_fundamentals`

Must validate:

- fallback path works when no FMP key exists
- same logical fundamentals endpoint fanout as FMP
- threaded fetch errors are captured as structured failures
- no mismatch between logical source name and endpoint routing

### `fred_macro`

Must validate:

- normal observation list
- `.` placeholder values are filtered
- empty observations list is handled as valid-but-empty where business logic allows
- transformed generic output remains readable and deterministic

## 8. Required Failure-Injection Scenarios

The following scenarios should be covered explicitly by tests or documented live checks.

### HTTP/Network

- 401/403 unauthorized
- 429 rate-limited
- 500/502/503/504 server failure
- timeout
- connection error

### Payload/Contract

- invalid JSON shape
- expected field missing
- type mismatch, e.g. list vs dict
- source-specific field drift, especially provider naming changes

### Async/Concurrency

- true in-flight concurrency cap
- daily budget exhaustion
- daily budget reset behavior
- multiple requests against the same source under contention

### Pipeline Integrity

- one endpoint fails, source still succeeds partially where appropriate
- all endpoints fail, source fails cleanly
- Bronze write succeeds but Silver transformation fails
- rerun after prior partial failure behaves predictably

## 9. Live Validation Matrix

Live validation should use a small, fixed matrix rather than arbitrary dates.

Suggested tickers:

- `AAPL`
- `NVDA`
- `MSFT` or `SPY`

Suggested date categories:

- one normal trading day with modest activity
- one quiet day with little or no news
- one earnings-related day
- one weekend/holiday-adjacent case for alignment validation

Each live run should answer:

1. Did fetch succeed?
2. Was the payload shape compatible with current parsing logic?
3. Did ingest treat empty-success correctly?
4. Did dedup behave reasonably?
5. Did Markdown preserve source attribution and references?
6. Were Bronze and Silver records stored?
7. Did rerunning the same request remain stable?

Live validation output should be written to artifacts when possible:

- per-date source summaries
- stored asset counts
- representative Markdown outputs
- structured failures

## 10. Phase 1 Acceptance Criteria

Phase 1 should be considered accepted only when all of the following are true:

1. Unit tests pass.
2. Mocked integration tests pass.
3. Failure-injection tests cover the required high-risk scenarios.
4. Repeated processing of the same request is explicitly tested and stable.
5. At least one live validation run has been completed for each currently supported provider path:
   - Polygon
   - FMP
   - FRED
   - yfinance fallback
6. Quiet/no-data days are not misclassified as system failures.
7. News Markdown preserves grounding-critical metadata:
   - source attribution
   - published timestamp
   - reference URL

Phase 1 is not the full Catalyst system. It is only the acceptance boundary for the current `data-core` milestone.

## 11. Gaps to Prioritize Next

The next highest-value additions after this document are:

1. explicit 429/503/timeout failure-injection tests across connectors and orchestrator
2. malformed payload / schema drift tests for each provider
3. idempotent rerun tests at the orchestrator level
4. live validation checklist and artifact capture
5. cleanup of the model/storage contract mismatch in [`packages/data-core/catalyst_data/models.py`](/Users/yiannischen/Desktop/Catalyst/packages/data-core/catalyst_data/models.py)

## 12. Recommended File Ownership

Primary implementation and validation files:

- [`packages/data-core/catalyst_data/connectors/`](/Users/yiannischen/Desktop/Catalyst/packages/data-core/catalyst_data/connectors)
- [`packages/data-core/catalyst_data/pipeline/`](/Users/yiannischen/Desktop/Catalyst/packages/data-core/catalyst_data/pipeline)
- [`packages/data-core/catalyst_data/storage/sqlite.py`](/Users/yiannischen/Desktop/Catalyst/packages/data-core/catalyst_data/storage/sqlite.py)
- [`packages/data-core/catalyst_data/orchestrator.py`](/Users/yiannischen/Desktop/Catalyst/packages/data-core/catalyst_data/orchestrator.py)
- [`packages/data-core/tests/`](/Users/yiannischen/Desktop/Catalyst/packages/data-core/tests)
- [`packages/data-core/scripts/smoke_test.py`](/Users/yiannischen/Desktop/Catalyst/packages/data-core/scripts/smoke_test.py)

Documentation anchor:

- [`docs/testing/data-core-test-design.md`](/Users/yiannischen/Desktop/Catalyst/docs/testing/data-core-test-design.md)

## 13. Summary

The current `data-core` implementation is strong enough to justify a formal validation strategy.

The repo already has meaningful code and tests. What was missing is a written test design that defines:

- what confidence means
- which failures matter
- what runs where
- what Phase 1 acceptance actually requires

This document is intended to prevent the project from advancing into eval and agent work on top of a data layer that only works against happy-path fixtures.

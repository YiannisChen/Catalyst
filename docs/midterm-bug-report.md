# Catalyst Midterm Bug Report

**Author:** Yiannis Chen
**Date:** 2026-04-13
**Scope:** All bugs and defects discovered in the midterm codebase (branch `feat/agents`)
**Severity scale:** Critical (system produces wrong results), Major (functionality broken), Minor (code quality / dead code)

---

## Critical Bugs

### BUG-001: GroundingRate metric always returns 0.0

**Location:** `packages/eval/catalyst_eval/metrics/grounding_rate.py:28-34`

**Description:**

The metric computes:
```python
retrieved_set = set(predicted.retrieved_chunks)   # content_md strings
grounded = sum(
    1 for cause in predicted.causes
    if retrieved_set.intersection(cause.evidence_ids)  # asset_id hashes
)
```

`predicted.retrieved_chunks` contains full Markdown content strings (populated in `e2e_strict.py:335` as `[c.get("content_md", "") for c in reranked]`). `cause.evidence_ids` contains `asset_id` SHA-256 hashes (populated by the Judge LLM from chunk IDs passed in its prompt).

These two sets live in entirely different namespaces. Their intersection is always empty. The metric returns 0.0 for every input, regardless of whether the Judge correctly cited its evidence.

**Impact:** One of five core eval metrics is non-functional. Any grounding_rate score reported in acceptance reports is meaningless. This undermines the project's claim to have a working evaluation framework.

**Root cause:** `AttributionResult.retrieved_chunks` is typed as `list[str]` without specifying whether it holds IDs or content. The e2e script populated it with content; the metric assumes IDs.

**Fix:** See design delta §5. The `AttributionResult` schema must explicitly separate chunk IDs from chunk content. The metric must compare against IDs.

**Verification:** After fix, construct a test case where a cause cites evidence_id "abc123" and retrieved_evidence includes a chunk with asset_id "abc123". The metric must return 1.0.

---

### BUG-002: AttributionF1 threshold is uncalibrated and near-degenerate

**Location:** `packages/eval/catalyst_eval/metrics/attribution_f1.py:18`

**Description:**

`_MATCH_THRESHOLD = 0.2` for Jaccard word-overlap similarity. In financial text, common vocabulary (stock, price, market, revenue, growth, company, investors, shares, trading, etc.) ensures that nearly any two cause descriptions about the same ticker achieve Jaccard > 0.2 regardless of semantic correctness.

Example — these two texts have Jaccard ≈ 0.25 despite describing completely different causes:
- Predicted: "Tesla missed delivery expectations for Q4 2024."
- Golden: "Tesla's stock was affected by rising interest rates and market rotation."

Shared tokens: {tesla, stock/tesla's} → even with minimal overlap, the high-frequency financial vocabulary inflates the score past the 0.2 threshold.

**Impact:** The metric over-counts matches, inflating both precision and recall. F1 scores are systematically higher than they should be, giving false confidence in attribution quality.

**Root cause:** The spec (§5.3) called for embedding cosine with threshold 0.8. The implementation substituted Jaccard with an arbitrary 0.2 threshold, likely for simplicity. No calibration was performed.

**Fix:** See design delta §6. Either implement embedding-based matching per spec, or empirically calibrate the Jaccard threshold using the golden set.

**Verification:** Run threshold sensitivity analysis on v1_2.jsonl. Plot F1 vs. threshold. Identify the point where increasing the threshold causes F1 to drop sharply — that is the true discrimination boundary.

---

## Major Bugs

### BUG-003: Orchestrator processes sources sequentially despite async architecture

**Location:** `packages/data-core/catalyst_data/orchestrator.py:206-211`

**Description:**

```python
async def process_request(...) -> list[dict]:
    results: list[dict] = []
    for source in sources:
        summary = await _process_source(ticker, date, source, conn, fetch_fn)
        results.append(summary)
    return results
```

Three data sources (polygon_news, polygon_ohlcv, fmp_fundamentals) are awaited sequentially. With Polygon's 12-second rate limit interval, fetching 3 sources takes ~36 seconds minimum instead of ~12 seconds with concurrent execution.

**Impact:** Data pipeline is 3x slower than it should be. More importantly, this is an architectural contradiction: the entire stack (httpx.AsyncClient, TokenBucketLimiter with asyncio.Semaphore, async connectors) is designed for concurrent execution, but the orchestrator serializes everything.

**Fix:** Replace the `for` loop with `asyncio.gather(*[_process_source(...) for source in sources])`. Additionally, resolve the sync SQLite issue (BUG-004) to avoid blocking the event loop during writes.

**Verification:** Measure wall-clock time for 3-source fetch before and after. Concurrent execution should show ~12s instead of ~36s (limited by the slowest provider's rate limit, not the sum).

---

### BUG-004: Synchronous SQLite calls inside async event loop

**Location:** `packages/data-core/catalyst_data/orchestrator.py` (calls `upsert_raw_asset`, `upsert_clean_asset` which use `sqlite3.Connection`)

**Description:**

`sqlite3.Connection.execute()` is a blocking call. When called inside an `async def` function running on the asyncio event loop, it blocks the entire loop for the duration of the disk write. This prevents other coroutines from making progress and defeats the purpose of async concurrency.

**Impact:** Even after fixing BUG-003 (concurrent source fetching), SQLite writes will serialize execution. Under burst traffic (backfill script), this becomes a bottleneck.

**Fix:** Either:
- Replace `sqlite3` with `aiosqlite` throughout `storage/sqlite.py` (all functions become `async def`).
- Or, if the project chooses to drop async entirely (see design delta §1, Option B), convert the orchestrator and connectors to sync code with `ThreadPoolExecutor` for IO parallelism.

**Verification:** If async path: all `upsert_*` and `init_db` calls are `await`-ed. If sync path: no `async def` functions remain in the data pipeline.

---

### BUG-005: Error/insufficient conflation in agent graph

**Location:** `packages/agents/catalyst_agents/nodes/critic.py:153-164`, `graph.py:38-40`

**Description:**

When the Critic LLM call fails after 3 retries (API error, parse failure, timeout), the node returns `{"graded_evidence": []}`. The graph's `route_after_critic` checks `if not state.get("graded_evidence")` and routes to `insufficient_handler`.

The user sees: "No evidence meeting the relevance threshold (>0.5) was found for {ticker}..."

This is factually incorrect when the failure was an API error. The system did not determine that evidence was insufficient — it failed to evaluate the evidence at all.

**Impact:** Users cannot distinguish between "the system found no relevant evidence" (informative data gap) and "the system's LLM API was down" (transient infrastructure failure). The eval harness cannot distinguish these either, so experiment results are contaminated by infrastructure failures.

**Fix:** See design delta §3. Add `error_log` to state; split routing into three branches: judge, insufficient_handler, error_handler.

**Verification:** Mock an LLM that always raises `ConnectionError`. The graph must route to error_handler, not insufficient_handler. The output must contain "System error" not "Insufficient evidence".

---

### BUG-006: Dedup logic duplicated across two modules

**Location:**
- `packages/data-core/catalyst_data/dedup/hard.py:8-15` — `compute_dedup_fingerprint`
- `packages/data-core/catalyst_data/pipeline/clean.py:12-20` — `_compute_dedup_fingerprint`

**Description:**

Identical fingerprint logic (normalize title, 2-hour time window, SHA-256 hash) is implemented in two places. `clean.py` uses its own private copy; `dedup/hard.py` is never imported by any module in the codebase.

**Impact:** If the fingerprint algorithm is updated in one location, the other becomes silently inconsistent. The `dedup/` module is dead code, misleading readers into thinking it is the authoritative implementation.

**Fix:** Delete `_compute_dedup_fingerprint` and `_clean_news`'s inline dedup logic from `clean.py`. Import `deduplicate_articles` from `dedup.hard` instead.

**Verification:** `grep -r "compute_dedup_fingerprint" packages/data-core/` returns exactly one definition (in `dedup/hard.py`) and one or more imports.

---

### BUG-007: Connectors lack retry logic; `retry.py` is unwired

**Location:**
- `packages/data-core/catalyst_data/retry.py` — exists but unused
- `packages/data-core/catalyst_data/connectors/polygon.py` — no retry on 429/5xx
- `packages/data-core/catalyst_data/connectors/fmp.py` — no retry on 429/5xx

**Description:**

Both connectors return a `FetchResult` with the error status when the HTTP request fails (429, 5xx, timeout). No retry is attempted. The `TokenBucketLimiter` prevents sending requests too fast, but it does not handle the case where a request is rate-limited by the server (429 response) or encounters a transient server error (5xx).

The `retry.py` module exists in the package but is not imported by any connector.

**Impact:** Transient API failures cause permanent data loss for that fetch cycle. During backfill (hundreds of requests), even a 1% failure rate means several missing assets with no recovery.

**Fix:** See design delta §4. Wire `retry.py` into a base connector class. Parse `Retry-After` header for 429 responses.

**Verification:** Mock a server that returns 429 on the first request and 200 on the second. The connector must succeed on the second attempt without caller intervention.

---

### BUG-008: SourceConnector Protocol is dead code

**Location:** `packages/data-core/catalyst_data/connectors/base.py:15-18`

**Description:**

```python
class SourceConnector(Protocol):
    async def fetch(self, ticker: str, endpoint: str, date: str) -> FetchResult: ...
```

Neither `polygon.py` nor `fmp.py` implements this Protocol. Both use closure-based factory functions (`create_polygon_fetcher`, `create_fmp_fetcher`) that return bare async functions, not classes.

**Impact:** The Protocol suggests a class-based interface that does not exist. New contributors may attempt to implement it, only to find the rest of the codebase ignores it.

**Fix:** Either implement the Protocol (convert connectors to classes that satisfy it) or delete the Protocol and document the closure-factory pattern as the project's connector convention.

---

## Minor Issues

### BUG-009: `_SequentialRunner` creates test/production divergence

**Location:** `packages/agents/catalyst_agents/graph.py:148-198`

**Description:**

When `langgraph` is not installed, `build_attribution_graph` silently falls back to `_SequentialRunner`. This runner does not replicate LangGraph's channel semantics, checkpoint behavior, or error propagation model. Tests passing under `_SequentialRunner` do not guarantee correctness under the real `StateGraph`.

**Fix:** Make `langgraph` a required dependency. Remove `_SequentialRunner`.

---

### BUG-010: E2E strict script tests a date not in the golden set

**Location:** `scripts/e2e_strict.py:43-44`

**Description:**

```python
TICKER = "NVDA"
TRADE_DATE = "2026-04-03"
```

The golden set (v1_2.jsonl) contains NVDA events for 2025-01-27, 2025-04-03, 2025-04-09, 2025-10-14, 2025-10-28. There is no NVDA event for 2026-04-03. The script acknowledges this mismatch in its output but proceeds to compute eval scores anyway.

**Impact:** Eval scores computed against a mismatched golden event are meaningless. The acceptance report gives a false sense of validation.

**Fix:** The e2e script should either:
- Use a date that exists in the golden set (e.g., 2025-01-27).
- Or skip eval scoring when no matching golden event is found, instead of scoring against the wrong event.

---

### BUG-011: `time.sleep()` in async-adjacent code

**Location:**
- `packages/agents/catalyst_agents/nodes/critic.py:156` — `time.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt))`
- `packages/agents/catalyst_agents/nodes/judge.py:172` — same pattern

**Description:**

Both nodes use synchronous `time.sleep()` for retry backoff. If the agent graph is ever run inside an async context (e.g., a FastAPI endpoint or the async orchestrator), this blocks the event loop.

**Impact:** Currently low (agent nodes are called synchronously via `graph.invoke()`). Becomes a blocker when the system moves to async API serving.

**Fix:** Replace with `await asyncio.sleep()` if the nodes become async, or document that the agent graph is intentionally synchronous.

---

## Summary

| ID | Severity | Component | One-line description |
|---|---|---|---|
| BUG-001 | **Critical** | eval | GroundingRate compares incompatible types, always returns 0.0 |
| BUG-002 | **Critical** | eval | AttributionF1 threshold 0.2 is uncalibrated, inflates scores |
| BUG-003 | **Major** | data-core | Orchestrator is sequential despite async architecture |
| BUG-004 | **Major** | data-core | Sync SQLite inside async event loop blocks concurrency |
| BUG-005 | **Major** | agents | System errors misreported as "insufficient evidence" |
| BUG-006 | **Major** | data-core | Dedup fingerprint logic duplicated; `dedup/` module is dead code |
| BUG-007 | **Major** | data-core | No retry in connectors; `retry.py` is unwired dead code |
| BUG-008 | **Major** | data-core | `SourceConnector` Protocol is dead code |
| BUG-009 | Minor | agents | `_SequentialRunner` diverges from real LangGraph behavior |
| BUG-010 | Minor | scripts | E2E script tests a date absent from golden set |
| BUG-011 | Minor | agents | `time.sleep()` in nodes blocks event loop if called async |

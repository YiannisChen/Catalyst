> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# Data-Core Phase 1 Closeout Summary

**Project:** Catalyst  
**Scope:** `packages/data-core`  
**Status:** Phase 1 closeout summary for handoff into `catalyst-eval`  
**Last Updated:** 2026-04-05

## 1. What Phase 1 now proves offline

The current offline suite proves the following with deterministic, no-network tests:

- connectors return structured results across success and representative failure paths
- orchestrator summaries preserve per-source status and failed endpoint detail
- malformed provider payload drift is tolerated for the cases covered so far
- Bronze/Silver writes remain stable across offline reruns
- rate limiter behavior is correct for interval enforcement, budget exhaustion, concurrency caps, and exception release

Canonical offline gate:

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/data-core
.venv/bin/python -m pytest tests/ -q --tb=short
```

Closeout verification result during this phase:

- `132 passed`

## 2. What the completed live matrix proved

The live validation matrix is documented in:

- [`docs/testing/data-core-live-validation-matrix.md`](/Users/yiannischen/Desktop/Catalyst/docs/testing/data-core-live-validation-matrix.md)

Live matrix rows completed in a real network-capable environment:

| Ticker | Sources | Reference Date | Result |
|---|---|---|---|
| `AAPL` | `polygon_news,polygon_ohlcv,fmp_fundamentals,fred_macro` | `2026-04-03` | `4 OK / 0 FAIL` |
| `TSLA` | `polygon_news,polygon_ohlcv` | `2026-04-03` | `2 OK / 0 FAIL` |
| `NVDA` | `fmp_fundamentals,polygon_news,polygon_ohlcv` | `2026-04-03` | `3 OK / 0 FAIL` |
| `SPY` | `fred_macro,polygon_ohlcv,polygon_news` | `2026-04-03` | `3 OK / 0 FAIL` |

What these runs demonstrated:

- real provider fetches succeed for the sampled matrix rows
- Bronze rows are written for the expected logical sources
- Silver rows are written for the expected logical sources
- source attribution and URLs survive the news transform where provider news exists
- a rerun of the known-good `AAPL` row against the same SQLite database does not create duplicate logical Bronze or Silver records

Important environment note:

- sandboxed runs may fail with DNS or host-resolution errors even when the same command succeeds outside the sandbox
- those environment-level failures should not be interpreted as `data-core` regressions

## 3. Pressure-testing decision

Decision:

- do **not** add another orchestrator-level pressure test in Phase 1 closeout

Reasoning:

- the current suite already covers the most important correctness guarantees for this phase: partial failure containment, per-source summary isolation, concurrency caps in the limiter, and limiter recovery after exceptions
- no additional high-confidence correctness gap was identified that would justify more Phase 1 scope before evaluation work starts

Carry-forward backlog:

- optional orchestrator-level parallel `process_request()` stress tests can be added later if evaluation or agent integration exposes a concrete need

## 4. Storage contract decision

Decision:

- treat `models.py` versus Bronze/Silver storage mismatch as explicit Phase 1.5 debt

Immediate rule for Phase 2:

- `catalyst-eval` must **not** depend on the monolithic `DataAsset` model as if it were the persisted storage contract
- evaluation work should read the actual Bronze/Silver SQLite reality through storage helpers or explicit query outputs

## 5. Residual risks accepted into Phase 2

These are known and acceptable to carry forward for now:

- live validation covered a small fixed matrix, not every provider/date combination
- quiet-news and empty-success cases still deserve more live repetition over time
- provider outages, 429s, and transient live-network issues remain possible and should continue to be turned into offline regressions when observed
- `align.py` is still not part of the default orchestrator path
- `models.py` remains older than the storage contract until Phase 1.5 cleanup

## 6. Handoff condition for `catalyst-eval`

`data-core` is now good enough to support evaluation development when all of the following are treated as true:

- offline gate remains green
- live validation uses the documented manual matrix rather than ad hoc one-off checks
- evaluation code depends on Bronze/Silver storage reality, not the old `DataAsset` abstraction
- any future live provider drift is converted into targeted offline regression coverage instead of left as tribal knowledge

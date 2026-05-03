# T-06 Backfill Inspection Note

## Outcome

The contract-bound T-06 rerun completed successfully against `data/catalyst_eval_frozen.db`.

- Full backfill scope: `10` tickers × trailing `365` calendar days
- Active processing days after contract alignment: `261` weekdays
- Total checkpoints: `10,440`
- Wall clock: `2,352.2s` (`39.2m`)
- Final run status: `success`
- OD-8: not triggered
- Backup key rotation: not used

## Final dataset counts

- `clean_assets`
  - `polygon_news = 2363`
  - `fmp_fundamentals = 2610`
  - `fred_macro = 2610`
  - `polygon_ohlcv = 2509`
  - total `= 10092`
- `ohlcv = 2509`
- `source_checkpoints = 10440`
  - `failed = 14`
  - `skipped = 334`
  - `fail_rate = 0.001`

Failure breakdown for the final run:

- `polygon_news / ingest = 13`
- `polygon_ohlcv / ingest = 1`

## Plan / implementation mismatch fixes

These remain in place and are now validated by the successful rerun:

1. Verification queries use `clean_assets.source_type`, not `clean_assets.source`.
2. The production `polygon_ohlcv` path writes to the dedicated `ohlcv` table.

Additional contract-alignment fix discovered during T-06 execution:

3. `fred_macro` was initially being processed on weekends because the earlier T-06 prompt said “all days for FRED macro”.
   - The binding reviewer-approved contract (`docs/testing/t06-verification-contract.md`) expects `fred_macro` to follow the same ~weekday geometry as the other source types (`2000–2800` rows).
   - I aborted the first full run, changed `sources_for_date(...)` so weekends schedule no sources, reset `data/catalyst_eval_frozen.db`, and reran the full backfill from scratch.

## Minimal code changes used to pass T-06

- `packages/data-core/scripts/backfill.py`
  - Added the backfill CLI and run/checkpoint persistence.
  - Added batched/cached fetch routing so the full run is practical within one session:
    - Polygon news cached by `(ticker, month)`
    - Polygon OHLCV cached by `(ticker, full-year range)`
    - FMP cached by `(ticker, endpoint)`
    - FRED cached by `(series_id, full-year range)` then sliced per request date
  - Suppressed `httpx` request logging so raw API keys do not appear in logs.
  - Aligned weekend scheduling with the binding contract.
- `packages/data-core/catalyst_data/orchestrator.py`
  - No-data `news` and `ohlcv` payloads are marked `skipped` instead of being stored as empty content rows.
  - This is what kept `V-05` honest (`short_rate = 0.000`) and prevented holiday / no-article stubs from counting as bad content.

## Verification contract result

All contract gates passed on the clean rerun:

- `V-01 PASS`
- `V-02 PASS`
- `V-03 PASS`
- `V-04 PASS`
- `V-05 PASS`
- `V-06 PASS`
- `V-07 PASS`

# T-05 Inspection Note: Provider Rate-Limiter Hardening + Key Pool Config

## Rate-Limit Constants

The single source of truth for runtime limiter behavior remains `RATE_POLICIES` in [packages/data-core/catalyst_data/config.py](/Users/yiannischen/Desktop/Catalyst/packages/data-core/catalyst_data/config.py).

T-05 adds a thin plan-shaped shim at [packages/data-core/catalyst_data/provider_limits.py](/Users/yiannischen/Desktop/Catalyst/packages/data-core/catalyst_data/provider_limits.py):

- `POLYGON = {"rate_per_min": 5, "burst": 5, "concurrency": 1}`
- `FMP = {"rate_per_day": 250, "concurrency": 2}`
- `FRED = {"rate_per_min": 120, "concurrency": 3}`

Choice made: keep `RATE_POLICIES` as the underlying runtime config and re-export plan-facing constants from `provider_limits.py` for minimum churn.

## Retry Policy Table

Per-provider retry policy now lives in [packages/data-core/catalyst_data/retry.py](/Users/yiannischen/Desktop/Catalyst/packages/data-core/catalyst_data/retry.py) as `RETRY_POLICIES` plus `get_retry_policy(provider)`.

| Provider | Condition | Params |
|---|---|---|
| `polygon` | `429` | exponential backoff, base `60s`, max `300s`, `max_retries=3`, jitter on, minimum delay `60s` |
| `polygon` | `5xx` | exponential backoff, base `5s`, max `60s`, `max_retries=5` |
| `polygon` | `timeout` | retry once, same params path, `max_retries=1` |
| `fmp` | `429` | same `60s` base / `300s` max / `3` retries shape |
| `fmp` | `5xx` | exponential backoff, base `5s`, `max_retries=3` |
| `fmp` | `timeout` | exponential backoff, base `5s`, `max_retries=3` |
| `fred` | `5xx` | exponential backoff, base `10s`, `max_retries=3` |
| `fred` | `timeout` | exponential backoff, base `10s`, `max_retries=3` |

Compatibility note: the legacy no-provider `with_retry(fetch_fn)` path was preserved so existing retry tests continue to pass, while connectors now bind explicit provider names for the new T-05 behavior.

## API Key Audit Hash

The API-key helpers live in [packages/data-core/catalyst_data/config.py](/Users/yiannischen/Desktop/Catalyst/packages/data-core/catalyst_data/config.py):

- `provider_api_key(provider)` reads `POLYGON_API_KEY`, `POLYGON_API_KEY_BACKUP`, `FMP_API_KEY`, or `FRED_API_KEY`
- `api_key_id(provider)` returns the first 12 hex chars of `sha256(key)`

Why it is safe to log:

- it is a one-way hash prefix, not the raw credential
- the raw key is never returned by `api_key_id(...)`
- stored audit data can identify “which key was used” without exposing the secret itself

## Day-4 FMP Re-Audit

Audit file:

- `data/eval_reports/provider_audit_20260502_004308.json`

Observed values from the fresh 5-day FMP-only run:

- `source = fmp_fundamentals`
- `run_ok_rate = 1.0`
- `recommendation = primary_candidate`

OD-7 decision: **keep** FMP as the primary fundamentals source. The demotion trigger (`run_ok_rate < 0.85`) did not fire.

## RATE_POLICIES Status

`RATE_POLICIES` was **kept** and remains the runtime limiter source. It was not replaced. T-05 adds:

- plan-shaped re-export module `provider_limits.py`
- per-provider retry lookup in `retry.py`
- env/key-hash helpers in `config.py`

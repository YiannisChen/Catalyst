# yfinance (Yahoo Finance) — Catalyst Data-Core

> **Scope:** Unofficial **community** library over Yahoo Finance; not a vendor contract API. Behavior and availability can change without notice. Use only as **FMP fallback** per project policy. **Convention:** [README](./README.md).

---

## Role in Catalyst

- **Connector:** `packages/data-core/data_core/connectors/yfinance_fallback.py`
- **Trigger:** Smoke / orchestration wires this as **fallback** when FMP is configured (`smoke_test_fetch.py`: `fallback_yf` for fundamentals).
- **Policy (README / schema):** Prefer **FMP**; on missing key, **401/403**, or after retries on **429/5xx/timeout**, fall back to yfinance for the same logical endpoints where mapped.

---

## Mapped “endpoints” (FMP physical → yfinance attribute)

| FMP-style endpoint   | yfinance `Ticker` attribute |
|---------------------|-----------------------------|
| `income_statement`  | `income_stmt` (DataFrame → dict) |
| `balance_sheet`     | `balance_sheet` |
| `cash_flow`         | `cashflow` |

Anything else returns `Unsupported endpoint for yfinance`.

---

## Usage pattern (library, not HTTP)

```python
import yfinance as yf
ticker = yf.Ticker("AAPL")
df = ticker.income_stmt  # or balance_sheet, cashflow
```

Catalyst wraps this in `asyncio.to_thread` (or equivalent) so sync pandas work does not block the event loop.

---

## Limits & risks

- **No published quota** — treat as **low concurrency**, **cache results**, avoid hammering Yahoo.
- **No API key** — nothing to put in `.env` for Yahoo itself.
- **Terms of use:** comply with Yahoo’s terms; academic/research use is common but not guaranteed.
- **Data quality / delays** may differ from FMP; keep `source_label` as `yfinance:*` for lineage.

---

## Package documentation

- PyPI / repo: [yfinance](https://github.com/ranaroussi/yfinance)  

Pin a **specific version** in your environment for reproducible parses.

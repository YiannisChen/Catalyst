> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# Polygon.io / Massive API — Catalyst Data-Core

> **Scope:** REST / WebSocket / Flat Files overview. Branding is shifting to **Massive** (`api.massive.com`); billing may still read **Polygon.io**. Confirm **host and paths** in the [dashboard](https://polygon.io/dashboard). **Convention:** [README](./README.md).

---

## Basic (free) tier — limits

Verify on the live **pricing** page; values below are a working snapshot for planning.

| Constraint | Value |
|------------|--------|
| **REST rate limit** | **5 requests / minute** (strict) |
| **Data latency** | **15-minute delayed** |
| **REST** | Included |
| **WebSocket** | **Not** included on Basic |
| **Flat Files (S3)** | **Not** included on Basic |

---

## Basic plan — what you can pull (summary)

| Asset class | Coverage (as stated) | History | Notes |
|-------------|----------------------|---------|--------|
| **Stocks** | US, full ticker set | **~2 years** | **EOD** + **1-minute aggregates**; reference data; **dividends / splits**; **technical indicators** (SMA, EMA, RSI, …). |
| **Options** | US options | **~2 years** | Reference; corp actions; technicals; minute aggregates. |
| **Forex** | Major / minor pairs | **~2 years** | **EOD**; reference; technicals; minute aggregates. |
| **Indices** | Limited (e.g. S&P, Dow — may be restricted) | **1+ years** | EOD; reference; technicals; minute aggregates. |

Higher tiers add real-time, WebSocket, flat files, higher rate limits — see official pricing.

---

## REST API

### Base URL

- Host (current docs): **`https://api.massive.com`**
- Paths in examples use **`/v3/...`** (e.g. `/v3/reference/dividends`).

If your dashboard still shows **`api.polygon.io`**, treat it as the same product line until you migrate; **use the host your key is issued for**.

### Authentication (choose one)

| Method | How |
|--------|-----|
| **Query** | `?apiKey=YOUR_API_KEY` |
| **Header** | `Authorization: Bearer YOUR_API_KEY` |

Do **not** commit keys; rotate any key that appeared in old scrapes or screenshots.

### Typical JSON envelope

```json
{
  "results": [ /* ... */ ],
  "status": "OK",
  "request_id": "…"
}
```

`results` is usually an array of records; exact fields vary by endpoint.

### First-call example (pattern only)

```bash
curl "https://api.massive.com/v3/reference/dividends?apiKey=YOUR_API_KEY"
```

See Massive/Polygon docs for filters, pagination, and path list (stocks aggregates, snapshots, fundamentals, etc.).

### Client libraries (official)

Python, Go, Kotlin/JVM, JavaScript clients are linked from Massive quickstart — prefer them in production for auth, retries, and parsing.

---

## WebSocket API *(typically not on Basic / Free)*

Documented for when your plan includes streaming.

| Feed | URL |
|------|-----|
| **15-minute delayed** | `wss://delayed.massive.com/stocks` (example path; asset-class suffix may vary — check docs) |
| **Real-time** | `wss://socket.massive.com/stocks` |

**Flow (conceptual):**

1. Connect → receive `ev: "status", status: "connected"`.
2. Send auth: `{"action":"auth","params":"YOUR_API_KEY"}` → `auth_success`.
3. Subscribe, e.g. minute aggregates: `{"action":"subscribe","params":"AM.AAPL,AM.MSFT"}`.

**Aggregate minute (`AM`) message (illustrative fields):** `ev`, `sym`, `v` (volume), `o/c/h/l`, `a` (VWAP), `s`/`e` (start/end **Unix ms**).

**Notes:** Often **one concurrent WebSocket per asset class** by default; server may disconnect slow consumers; bundled JSON arrays possible during high volume.

---

## Flat Files *(typically not on Basic / Free)*

Bulk **gzip CSV** via **S3-compatible** API — avoids millions of REST calls when your subscription includes it.

| Setting | Value |
|---------|--------|
| **Endpoint** | `https://files.massive.com` |
| **Bucket** | `flatfiles` |
| **Credentials** | Separate **S3 Access Key / Secret** from Dashboard (not the REST `apiKey`) |
| **Freshness** | Each trading day’s data ≈ **11:00 AM ET** next business day |

**Example key prefixes** (from docs):

- `us_stocks_sip` — US stocks (SIP)
- `us_options_opra` — US options (OPRA)
- `us_indices` — US indices
- `global_forex`, `global_crypto` — FX / crypto

Use AWS CLI, Rclone, MinIO, or Boto3 with `endpoint_url=https://files.massive.com` and bucket `flatfiles`. **Do not paste real keys into the repo** — use env vars.

---

## Practical guidance for Catalyst

- **Basic plan:** design around **≤5 calls/min** and **15-minute delay**; batch date ranges where the API allows; cache aggressively.
- **OHLC / minute bars on Basic:** you have **EOD + 1-min aggregates** per your notes — still subject to the **5/min** cap, so backfills must be **slow and incremental**.
- **WebSocket / Flat Files:** plan upgrades or alternate providers (e.g. FMP) if you need real-time or bulk historical without burning REST quota.

---

## Official links

- **Docs index:** https://massive.com/docs (Polygon REST docs redirect here; see also `https://massive.com/docs/llms.txt` for LLM-oriented index)  
- **Product / dashboard:** https://polygon.io/  

---

## Catalyst integration

- Add **`POLYGON_API_KEY`** (or Massive-equivalent) to `packages/data-core/.env` when you wire a connector; mirror in `.env.template` with a short comment.  
- No `data-core` Polygon connector exists in-repo at the time of this doc — implement with the same patterns as `fmp.py` / `finnhub.py` (httpx, semaphore, rate limiter for **5/min**).

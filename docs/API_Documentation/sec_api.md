> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# SEC EDGAR Data APIs — Catalyst Data-Core

> **Scope:** `data.sec.gov` JSON APIs, fair-access rules, bulk ZIPs. Primary sources: [EDGAR APIs](https://www.sec.gov/edgar/sec-api-documentation), [Developer Resources](https://www.sec.gov/developer). SEC page stamp reviewed: March 10, 2025. **Convention:** [README](./README.md).

---

## What this covers

- **`data.sec.gov`** — REST APIs returning **JSON** (no API key).
- **Fair-access / rate limits** — apply to all programmatic use.
- **Bulk ZIPs** — nightly full dumps.
- **Pointers** to RSS, index files, and full EDGAR file system (not full detail here).

Filing **submission** APIs (tokens, filers) are a different surface; see SEC’s *EDGAR APIs* / *API Development Toolkit* from the official site.

---

## Authentication

**`data.sec.gov` JSON APIs do not require API keys.**

You still must comply with [SEC.gov Privacy and Security Policy](https://www.sec.gov/os/webmaster-faq) and [Developer FAQs](https://www.sec.gov/about/developer-resources) (including **User-Agent** — see below).

---

## Fair access & rate limits

| Rule | Detail |
|------|--------|
| **Throughput** | **No more than ~10 requests per second** per user, **regardless of how many machines** send traffic. |
| **Excess traffic** | SEC may **block IPs** that submit excessive requests. |
| **Bots** | “Unclassified” bots / automation outside policy may be throttled or blocked. |
| **User-Agent** | Requests should identify your app and include **contact** (e.g. email). **Omitting a proper User-Agent often yields 403** even at low rates — set `SEC_USER_AGENT` in Catalyst’s `.env` and send it on **every** request. |
| **Practical throttle** | Stay **under 10 r/s**; during peak filing windows, **~5–7 r/s** is safer. |

---

## REST base: `data.sec.gov`

JSON is updated **throughout the day** as filings are disseminated.

| API area | Typical delay |
|----------|----------------|
| **Submissions** | Often under **~1 second** after dissemination |
| **XBRL APIs** | Often under **~1 minute**; can be longer at peak |

### Submissions (filing history by filer)

**Path pattern:**

```http
GET https://data.sec.gov/submissions/CIK##########.json
```

- `##########` = **10-digit CIK**, **with leading zeros**.
- Contains metadata (e.g. current/former name, tickers, exchanges).
- Includes **at least one year** of filings **or** **1,000** most recent filings (whichever is more) in a compact columnar array; older filings may be split across **additional JSON files** referenced in the object (with date ranges).

### XBRL — company concept (one company + one taxonomy tag)

Returns all disclosures for a **single CIK** and **single concept** (taxonomy + tag), with facts grouped by **unit** (e.g. USD vs CAD).

```http
GET https://data.sec.gov/api/xbrl/companyconcept/CIK##########/us-gaap/AccountsPayableCurrent.json
```

Concepts use **non-custom** taxonomies (e.g. `us-gaap`, `ifrs-full`, `dei`, `srt`), **entity-wide** context — for comparability across companies and time.

### XBRL — company facts (all concepts, one company)

**Single call** for all company-facts-style XBRL data for that CIK:

```http
GET https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
```

**Ingestion tip:** Prefer **`companyfacts`** when you need many line items — **one request** beats many `companyconcept` calls under the 10 r/s cap.

### XBRL — frames (cross-company slice)

One fact per reporting entity for a calendar-aligned period (annual / quarterly / instantaneous):

```http
GET https://data.sec.gov/api/xbrl/frames/us-gaap/AccountsPayableCurrent/USD/CY2019Q1I.json
```

**Period tokens:**

| Pattern | Meaning |
|---------|---------|
| `CY####` | Annual (~365 days ±30) |
| `CY####Q#` | Quarterly (~91 days ±30) |
| `CY####Q#I` | **I**nstantaneous (point in time) |

**Units:** If numerator/denominator exist in XBRL, they appear as `UNIT-per-DENOM` (e.g. `USD-per-shares`). Default pure number unit is `pure`.

**Caveat:** Fiscal calendars differ; frame dates are **calendar-aligned approximations** — compare `start`/`end` on facts when doing time series.

---

## Bulk downloads (nightly)

Efficient for large backfills.

| Archive | URL | Contents |
|---------|-----|----------|
| **Company facts (XBRL)** | `https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip` | Data aligned with **Frames** + **Company Facts** APIs |
| **All submissions** | `https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip` | Public EDGAR filing history for all filers (Submissions API mirror) |

Per SEC: a **ZIP of all JSON** for an API surface is also republished **nightly ~3:00 a.m. ET** (see official page for exact packaging).

---

## CORS

**`data.sec.gov` does not support CORS** for browser cross-origin use. Server-side or same-origin clients are the intended model.

---

## Other access (not JSON API detail)

| Mechanism | Use |
|-----------|-----|
| **HTTPS EDGAR tree** | Full filing documents — see *Accessing EDGAR Data* on [Developer Resources](https://www.sec.gov/developer). |
| **RSS** | Subscribe to company searches, latest filings, form filters (e.g. 10-K). |
| **Index files** | Daily / full / master / company indexes under `/edgar/daily-index`, `/edgar/full-index` — HTML, XML, JSON. |
| **Daily tar archives** | `/edgar/Feed/` (e.g. `20061207.nc.tar.gz` per day); `/edgar/Oldloads/` concatenated submissions. |

---

## Forms called out for XBRL APIs

Included in the official description: **10-Q, 10-K, 8-K**, **20-F, 40-F, 6-K**, and variants (plus extracted XBRL facts as published).

---

## Feedback (SEC)

- API feedback: `webmaster@sec.gov`  
- Open data ideas: `opendata@sec.gov`  

SEC states they **do not provide technical support** for debugging scripted downloads.

---

## Official links

- [EDGAR / SEC API documentation (data.sec.gov)](https://www.sec.gov/edgar/sec-api-documentation)  
- [SEC Developer Resources](https://www.sec.gov/developer)  
- [Accessing EDGAR Data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data) (full file system)  
- [Developer FAQs (policy, User-Agent, automation)](https://www.sec.gov/about/developer-resources)  

---

## Catalyst alignment

- **Env:** `SEC_USER_AGENT` in `packages/data-core/.env` — required for compliant access.  
- **Code:** `packages/data-core/data_core/connectors/sec_edgar.py` — keep concurrency and spacing consistent with **≤10 r/s** and a descriptive User-Agent.

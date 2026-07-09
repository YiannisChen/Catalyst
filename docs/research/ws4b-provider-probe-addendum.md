# WS4B Provider Probe Addendum — R6 Final (Corrected)

**Status:** COMPLETE — READY FOR CLAUDE REVIEW
**Date:** 2026-07-01
**Branch:** `ws4b/article-level-data`

---

## Final Provider Decisions

| Provider | Verdict | Step | Ready | Note |
|----------|---------|------|-------|------|
| **SEC 8-K/10-Q/10-K** | BUILD_NOW | 3C | ✅ | Plan + design spike approved |
| **Finnhub company-news** | BUILD_NOW_AFTER_SEC | 3D | ✅ | 0% MF, L1 only, 152 chars median |
| **SEC Form 4** | BUILD_AS_SEC_EXTENSION | 3E | ⚠️ partial | Owner/issuer OK. Transactions need namespace parser. |
| **FRED macro** | BUILD_IN_STEP_3E | 3E | ✅ | 11/11 series. Replace stale TEDRATE. |
| **Nasdaq earnings** | BUILD_WITH_COVERAGE_GUARD | 3E | ⚠️ partial | 4/10 tickers. Missing AMD/MSFT/NVDA too (not just NYSE). |
| **FINRA short interest** | BUILD_AS_CONTEXT_LAYER | 3E | ✅ | Bi-monthly batch download |
| **Tiingo EOD** | KEEP_FOR_MARKET_DATA_AUDIT | 3E opt | ✅ | Adjusted OHLCV + dividends + splits |
| **Tiingo News** | SKIP_UNTIL_POWER_PLAN | — | ❌ | 403 no permission |
| **yfinance news** | SKIP_PRIMARY | — | ❌ | 0/3 rate-limited, no date filter |
| **Company IR** | DEFER_WITH_TEMPLATE_PLAN | WS4D | ❌ | 9/10 accessible, TSLA blocked |

---

## A. Key State (R6 Corrected)

| Key | .env Name | Present | Verified |
|-----|-----------|---------|----------|
| FINNHUB_API_KEY | FINNHUB_API_KEY | ✅ | company-news 200 |
| TIINGO_API_KEY | **tiingo_api_key** (lowercase) | ✅ | EOD 200, News 403 |
| FRED_API_KEY | FRED_API_KEY | ✅ | 11 series 200 |
| POLYGON_API_KEY | POLYGON_API_KEY | ✅ | news 200 |
| FMP_API_KEY | FMP_API_KEY | ✅ | profile 200 |
| SEC_USER_AGENT | SEC_USER_AGENT | ✅ | submissions 200 |

**R5 error:** Used uppercase `TIINGO_API_KEY` — missed the lowercase `tiingo_api_key` in .env. Corrected.

---

## B. Tiingo EOD — Confirmed Working ✅

**3 tickers probed:** AAPL, NVDA, JPM — all 200 with 4 bars each (Jun 25 – Jul 1).
**Fields:** `date, close, high, low, open, volume, adjClose, adjHigh, adjLow, adjOpen` — 10 fields including adjusted OHLCV.
**Dividends/splits:** `divCash` and `splitFactor` fields present in full schema (not in this narrow date range sample).

**Value for Catalyst:** Free adjusted OHLCV from independent source — useful for price data cross-validation against Polygon. Not an evidence/news source. Step 3E optional candidate.

## C. Tiingo News — SKIP_UNTIL_POWER_PLAN ❌

403 "You do not have permission to access the News API." Requires Power plan ($10+/mo). Not available on current key.

---

## D. SEC Form 4 — Honest Assessment ⚠️

**What works:**
- Owner name extraction via regex on primary doc XML ✅
- Issuer name extraction ✅
- Complete submission .txt accessible (200) ✅

**What doesn't work yet:**
- Transaction blocks: 0 extracted (need namespace-aware parser for `<ns:nonDerivativeTransaction>`)
- Director/officer flags: not extracted (in different XML location — `ownershipDocument` header)

**Conclusion:** BUILD_AS_SEC_EXTENSION_STEP_3E. Owner/issuer parsing is a proof of concept. Full implementation needs namespace-aware XML parser. Not a blocker for Step 3C — Form 4 is separate from 8-K/10-Q/10-K.

---

## E. Nasdaq Earnings — Corrected Conclusion ⚠️

**R5 error:** Said "Nasdaq-only" implying all NYSE tickers missing. In reality, AMD/META/MSFT/NVDA are Nasdaq-listed and still missing.

**Correct:** 4/10 tickers found across 9 dates. Coverage is incomplete regardless of listing venue. Step 3E must include coverage guard (skip tickers not found) + fallback to alternative earnings calendar source for missing tickers.

---

## F. Finnhub — Confirmed ✅

0% MF/Zacks. 5.6% SeekingAlpha (T5 opinion). 16.2% T3. L1 only (152 chars median). URL redirect caveat (all finnhub.io).

---

## G. FRED — Confirmed ✅

11/11 series accessible. TEDRATE stale (last 2022). Replace with DGS10-DGS2 spread.

---

## H. Build Sequence

```
Step 3C:  SEC 8-K/10-Q/10-K     ✅ Ready
Step 3D:  Finnhub company-news   ✅ Ready
Step 3E:  FRED + SEC Form4 + Nasdaq + FINRA + Tiingo EOD (optional)
Defer:    Tiingo News (Power plan), yfinance (skip), Company IR (WS4D)
```

---

## I. Artifact Index (R6)

`data/provider_discovery/20260701T091156Z/`

| File | Content |
|------|---------|
| `provider_probe_matrix.json` | 10-provider final matrix |
| `provider_dossiers/key_audit/summary.json` | Corrected key states (lowercase tiingo) |
| `provider_dossiers/tiingo_eod/probe_structure.json` | EOD confirmed (adj OHLCV) |
| `provider_dossiers/tiingo_news/probe_structure.json` | News 403 |
| `provider_dossiers/sec_form4/probe_structure.json` | Honest: owner OK, transactions partial |
| `provider_dossiers/nasdaq_calendars/probe_structure.json` | Corrected: 4/10, not "Nasdaq-only" |
| `provider_dossiers/finnhub_company_news/decision.md` | BUILD_NOW_AFTER_SEC |
| `provider_dossiers/yfinance_news/decision.md` | SKIP_PRIMARY |
| `docs/research/ws4b-provider-probe-addendum.md` | This document |

---

## Verification

- ✅ Secret grep: PASS
- ✅ DB unchanged
- ✅ Frozen DB SHA: `0dfc81b154a9...` unchanged
- ✅ No packages/app or packages/agents changes

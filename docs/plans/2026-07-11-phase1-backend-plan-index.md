# Catalyst Phase 1 — Backend Plan Index

**Role:** Plan registry and dependency map. No implementation in this document.
**Branch:** ws4b/article-level-data
**Date:** 2026-07-11
**Amended:** 2026-07-12 (W3-A verified; all plans drafted; orchestrator review corrections applied)
**Author:** dscodex (plan authoring); orchestrator (W3-A verification)

---

## Purpose

Single source of truth for Phase 1 execution order, plan status, dependency
gates, and architect decisions.

---

## Normative Design Documents

| Document | Path |
|---|---|
| Backend-first roadmap | `docs/plans/2026-07-09-backend-first-roadmap.md` |
| W1 unified update service design | `docs/plans/2026-07-10-w1-unified-update-service-design.md` |
| S2 harness honesty (amended v3) | `docs/plans/2026-07-07-ws4b-s2-harness-honesty.md` |
| S4 regression gate (amended v3) | `docs/plans/2026-07-07-ws4b-s4-regression-gate-recalibration.md` |
| BYOK multi-provider model layer | `docs/plans/2026-07-08-byok-multi-provider-model-layer.md` |
| BYOK/S1/S4 execution design | `docs/plans/2026-07-08-s1-s4-byok-execution-design.md` |

---

## Final Architecture Decisions

| Decision | Value |
|---|---|
| Ticker universe | AAPL, AMD, AMZN, GOOGL, JPM, META, MSFT, NVDA, TSLA, UNH |
| Historical start | 2024-12-30 |
| End | Rolling latest closed US market session |
| OHLCV primary | Polygon |
| OHLCV fallback | yfinance (pending AD-OHLCV-1) |
| News providers | Polygon + Finnhub |
| SEC forms | 8-K, 10-Q, 10-K |
| FRED | Current configured series |
| Prices | Adjusted-only (pending AD-OHLCV-2) |
| Heartbeat lease interval | 60 seconds |
| Stale threshold | 10 minutes |

---

## W1 Invariants (Must Preserve)

1–20: Per the roadmap §W1 Invariants. These are fixed.

---

## Plan Generation Strategy

All Phase 1 implementation plans are written and reviewed. Implementation
remains strictly serialized. Each plan is revalidated immediately before its
slice begins execution.

---

## Ordered Slice List

| # | Slice | Plan File | Commit Message | Status |
|---|---|---|---|---|
| 0 | W3-A | `docs/plans/2026-07-11-w3a-data-core-test-baseline.md` | `test(data-core): fix environment test baseline, install dev extra` | **verified** (unstaged) |
| 1 | W1-A | `docs/plans/2026-07-12-w1a-zero-write-update-planner.md` | `feat(data-core): true zero-write update planning` | drafted |
| 2 | W1-B | `docs/plans/2026-07-12-w1b-calendar-ohlcv-planning.md` | `feat(data-core): calendar-derived planning and explicit universe` | drafted |
| 3 | W1-C | `docs/plans/2026-07-12-w1c-ohlcv-execution-fallback.md` | `feat(data-core): OHLCV first-class source with fallback` | drafted |
| 4 | W1-D | `docs/plans/2026-07-12-w1d-two-stage-update-service.md` | `feat(data-core): two-stage unified update service facade` | drafted |
| 5 | W1-E | `docs/plans/2026-07-12-w1e-durable-run-control.md` | `feat(data-core): durable cancel, heartbeat, single-writer, resume hardening` | drafted |
| 6 | W1-F | `docs/plans/2026-07-12-w1f-operator-cli-reports.md` | `feat(data-core): operator CLI and report extensions` | drafted |
| 7 | W3-B | `docs/plans/2026-07-12-w3b-data-core-runtime-remediation.md` | `test(data-core): remediate remaining failure groups` | drafted |
| 8 | W2 | `docs/plans/2026-07-12-w2-data-completeness-certification.md` | `feat(data-core): completeness certification generator` | drafted |

**Execution runbook:** `docs/plans/2026-07-12-phase1-backend-execution-runbook.md`

---

## Dependency Graph

```
W3-A (✓) → W1-A → W1-B → W1-C → W1-D → W1-E → W1-F → W3-B → W2
```

Parallel tracks (different packages, can run alongside):
- W4 (S2) → W5 (S4) — agents/eval
- W6 (BYOK) — app; blocked on missing connectivity amendment

---

## Architect Decision Register

| ID | Decision | Affected Slice | Recommendation | Required By |
|---|---|---|---|---|
| AD-GIT-1 | Commit W3-A before W1-A? | W1-A (pre-slice) | Commit now for tracked baseline | W1-A start |
| AD-UNIVERSE-1 | Final ten-ticker universe | W1-B | Use existing ten: AAPL, AMD, AMZN, GOOGL, JPM, META, MSFT, NVDA, TSLA, UNH | W1-B certification |
| AD-DATE-1 | Historical start date | W1-B | 2024-12-30 | W1-B execution |
| AD-OHLCV-1 | OHLCV fallback order | W1-C | Polygon → yfinance (connector exists) | W1-C fallback task |
| AD-OHLCV-2 | Adjusted vs raw vs both | W1-C | Adjusted-only | W1-C polygon URL task |
| AD-LIFECYCLE-1 | Lifecycle thresholds (recheck/terminal) | W1-C | 1 recheck, 5d window, 3 permanent, 5 chronic | W1-C lifecycle task |
| AD-CERT-1 | Certification thresholds | W2 | Per-check recommendations in W2 plan | W2 implementation |
| AD-FILINGS-1 | SEC forms + expected-filing model | W2 | 8-K, 10-Q, 10-K; N/A if no model | W2 filings check |
| AD-W3B-1 | Obsolete test deletion per case | W3-B | Architect approves each deletion | W3-B |

## Connectivity Amendment Status

`docs/plans/2026-07-08-byok-connectivity-amendment.md` — **missing**.
W6 connectivity implementation blocked until written by Fable 5.

---

## W3-A Verified State (2026-07-12)

- **703 passed, 0 failed, 1 skipped, 1 xfailed**
- G1 resolved: all 83 async-plugin failures gone
- Modified (unstaged): `README.md`, `conftest.py`, `packages/data-core/README.md`
- DB SHAs unchanged. No production code or test content changed.

---

## Per-Slice Test Gate

Every W1 slice: focused tests green; canonical suite unchanged or improved
from 703/0/1/1; no new failure groups. Full package green required at W3-B
and W2 exits.

---

## File Inventory

| File | Purpose | Date |
|---|---|---|
| `docs/plans/2026-07-11-phase1-backend-plan-index.md` | This index | 2026-07-11 / 2026-07-12 |
| `docs/plans/2026-07-11-w3a-data-core-test-baseline.md` | W3-A plan | 2026-07-11 |
| `docs/testing/data-core-failure-groups.md` | G1 + resolution log | 2026-07-11 / 2026-07-12 |
| `docs/plans/2026-07-12-phase1-backend-execution-runbook.md` | Execution runbook | 2026-07-12 |
| `docs/plans/2026-07-12-w1a-zero-write-update-planner.md` | W1-A plan | 2026-07-12 |
| `docs/plans/2026-07-12-w1b-calendar-ohlcv-planning.md` | W1-B plan | 2026-07-12 |
| `docs/plans/2026-07-12-w1c-ohlcv-execution-fallback.md` | W1-C plan | 2026-07-12 |
| `docs/plans/2026-07-12-w1d-two-stage-update-service.md` | W1-D plan | 2026-07-12 |
| `docs/plans/2026-07-12-w1e-durable-run-control.md` | W1-E plan | 2026-07-12 |
| `docs/plans/2026-07-12-w1f-operator-cli-reports.md` | W1-F plan | 2026-07-12 |
| `docs/plans/2026-07-12-w3b-data-core-runtime-remediation.md` | W3-B plan | 2026-07-12 |
| `docs/plans/2026-07-12-w2-data-completeness-certification.md` | W2 plan | 2026-07-12 |

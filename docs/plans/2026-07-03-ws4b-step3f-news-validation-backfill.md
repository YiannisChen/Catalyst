# WS4B Step 3F — News Fetching Validation & Backfill Hardening

Status: DESIGN SPEC (orchestrator). This is the architecture/what+why. dscodex
produces the executable step-by-step plan via /writing-plans (reviewed before any
code), then implements via /executing-plans + /test-driven-development +
/subagent-driven-development.
Branch: `ws4b/article-level-data`
Precedes: Step 4 (cloud embedding) — **hard-gated behind 3F pass criteria**
Scope: `packages/data-core` only. Do not touch `packages/app` or `packages/agents`.

---

## 0. Why this phase exists (grounded in live DB state)

Independent read-only audit of `data/catalyst_dev_ws4b.db` on 2026-07-03:

| Fact | Value | Meaning |
| --- | --- | --- |
| `articles` sources | `polygon_news` only (11,772) | Finnhub (3D) never ran live |
| `articles` span | 2024-12-30 → 2026-05-01 | Stale ~2 months |
| `filings` / `filing_documents` | 0 / 0 | SEC (3C) 100% fixtures, never live |
| `macro_observations` table | absent | FRED (3E) schema never migrated here |
| `index_state` | 0 | No index records exist to embed |
| `source_checkpoints` | AAPL polygon_news @ 2025-05-02 = failed/ingest | Polygon layer has failed cells |

**Architectural gap:** `cli_index.py` hard-blocks every live path
(`sys.exit(1)` on non-dry-run in `cmd_update_news`, `cmd_update_macro`,
`cmd_backfill`; always passes `fetch_fn=None, dry_run=True`). The pipeline
(`run_update_batch`) IS live-capable via injected `fetch_fn` +
`_fetch_cell_sec` / `_fetch_cell_finnhub`, but **no committed entrypoint
constructs real connectors and writes**. Therefore 3F must build the gated
live runner before anything can be validated end-to-end.

**Decision: Step 3F is REQUIRED before Step 4.** Embedding the current corpus
would embed a Polygon-only, stale, partially-failed snapshot and silently omit
all three new evidence planes.

---

## 1. Phase shape

Name confirmed: **Step 3F — News Fetching Validation & Backfill Hardening**.
Scope tightened to include the live-runner gap discovered above.

Compressed to **THREE** substeps (each ends in an independent orchestrator
review + a scoped commit), grouped by purpose:

### 3F.1 — Readiness (no live network; all mock-testable)
Everything required *before* any real fetch, in one PR:
- **Coverage & integrity audit (READ-ONLY):** machine-readable report + human
  summary; opens dev DB with `PRAGMA query_only=ON`; zero writes. (Contract §2.)
- **Schema reconciliation:** idempotent, additive migration of the dev DB to
  current DDL — creates the missing `macro_observations` table, verifies
  filings / filing_documents / index_state DDL. Prints a dry-run diff of missing
  objects first, then applies (additive-only, safe).
- **Gated live runner:** `--live --confirm` flag added to the existing
  `cli_index.py` subcommands (`update-news`, `backfill`, `update-macro`) that
  constructs real connectors + limiter + httpx client and passes them as
  `fetch_fn`. Guards: env keys required, refuses the frozen DB path, echoes
  redacted config, requires `--confirm`. **Default stays dry-run.** Built and
  **mock-tested only** here — no live call in this step.
- **LOCKED:** live path lives on the existing CLI (one surface). **LOCKED:**
  dscodex builds + mock-tests; the human runs real `--live` in 3F.2. API keys
  never enter the agent session.

### 3F.2 — Live ingestion & validation (human runs `--live`)
dscodex builds the orchestration + deterministic fixtures; the human executes the
gated live commands with real keys and reports results back for review:
- **Polygon gap-fill + failed-cell repair:** extend 2026-05-01 → latest closed
  trading day; re-run the failed AAPL cell; prove idempotency & no dup raw_assets.
- **Finnhub bounded live:** 60-day backfill + daily incremental; validate
  cross-source dedup vs Polygon; rate-limit / `Retry-After` behavior.
- **SEC tiny live:** recent 8-K/10-Q/10-K for a few tickers; prove EX-99.1
  resolution + raw-HTML Bronze on real filings; filings/filing_documents land.
- **FRED macro live:** materialize `macro_observations` with point-in-time
  integrity (per-observation first-release `released_at`).

### 3F.3 — Invariants & gate
- Automated mock/fixture tests for every SQLite invariant (§4), each bite-proven.
- Final go/no-go report against the pre-embedding gates (§8) → unblocks Step 4.

Ordering within 3F.2: Polygon first (dedup needs its rows) → Finnhub → SEC →
FRED. The original 8-part breakdown maps 1→3F.1 (audit+schema+runner),
4–7→3F.2 (live), 8→3F.3 (gate).

---

## 2. 3F1 — Coverage audit (read-only) — the audit contract

Output: `data/provider_discovery/step3f_coverage_YYYYMMDD.json` (untracked,
gitignored) + printed summary. **Zero writes to any `.db`.** Opens dev DB with
`PRAGMA query_only=ON`. Frozen DB SHA re-asserted before & after.

Audit dimensions (all as SQL against dev DB):

1. **Per source × per table counts:** `raw_assets`, `clean_assets`, `articles`,
   `article_tickers`, `filings`, `filing_documents`, `macro_observations`,
   `index_state`, grouped by `source_type` / `source_kind`.
2. **Per ticker × per source:** article counts + min/max `published_utc`.
3. **Date coverage:** min/max per source; compare max vs **latest closed
   trading day** (NYSE calendar, weekend/holiday aware).
4. **Missing ranges:** trading-day gaps between per-ticker article dates within
   the covered span (distinguish "true no-news day" from "never fetched").
5. **Duplicate diagnostics:** count of `dedup_group_id` groups with ≥2 members;
   count of articles with `is_canonical=0`; provider-native-id collisions;
   canonical-URL collisions.
6. **Canonical counts:** `is_canonical=1` per source; multi-ticker articles
   (article_id appearing in >1 `article_tickers` row) preserved count.
7. **Checkpoint reconciliation:** `source_checkpoints` status histogram
   (success/failed/skipped) per source; reconcile checkpoint `as_of` dates vs
   actual max `published_utc` (explain the 2025-05-02 vs 2026-05-01 gap).
8. **Rederivability spot-check:** for N random article_ids, confirm a matching
   `raw_assets` row exists whose decompressed payload contains the title
   (proves Bronze → Silver rederive works). Read-only; no rewrite.
9. **Source-tier distribution:** `source_tier` histogram across articles +
   filings; flag any NULL/unexpected tier.

This report is the ground truth all later substeps are measured against.

---

## 3. Backfill / rate-limit policy (D)

Universe (10): AAPL AMD AMZN GOOGL JPM META MSFT NVDA TSLA UNH.

### Polygon news (3F4)
- Free tier ≈ 5 req/min (already modeled in `provider_limits`). Respect existing
  `TokenBucket`; do not raise limits.
- **Strategy: incremental gap-fill, not full recompute.** Fill from current per
  (ticker, source) max date → latest closed trading day using the existing
  cell/checkpoint model. Cells already `success` are skipped (idempotent). Only
  the failed AAPL cell + post-2026-05-01 cells are fetched.
- **No dup raw_assets:** `upsert_raw_asset` is content-addressed / keyed; rerun
  must not create new Bronze rows for identical payloads. Prove with before/after
  `raw_assets` count on a repeat run (delta = 0).
- **Canonical preservation:** article-level layer rebuilt via existing rederive;
  `article_tickers` multi-ticker rows must be lossless (audit count before/after).
- **"News soup" guard:** the orchestrator request path must return per-cell
  provider responses scoped to (ticker, date); assert every fetched article maps
  to the requested ticker or is dropped, never fanned across the universe.

### Finnhub company-news (3F5)
- Free tier: 60 req/min. Unknown hard daily quota → **bound the backfill**.
- **Scope: 60-day bounded backfill** (not full history). Rationale: Finnhub is a
  breadth/recency supplement, L1-only; deep history adds dedup cost without
  attribution value. Daily incremental thereafter.
- Rate: reuse `TokenBucket` at ≤ 60/min with jitter; honor real `Retry-After`
  (regression already fixed in 3D — re-assert it is not re-broken).
- **Yahoo-heavy overlap:** expected; handled by cross-source dedup
  (`compute_cross_source_dedup`), per-association fingerprint
  `SHA256(NFC(title.lower())|ticker|reference_date)[:16]`, OR-semantics canonical.
- **Safe dedup across providers:** run dedup only after both providers' rows for
  the overlapping window exist; assert no group ends with zero canonical
  (the OR-semantics invariant proven in 3D) and no cross-group clobber.

### SEC (3F6)
- Fair-access: ≤ 10 req/s with descriptive `User-Agent`; run at ~5/s.
- **Tiny live validation only; full SEC backfill DEFERRED.** Fetch the most
  recent 1–2 8-K + latest 10-Q/10-K for ~3 tickers (incl. one known EX-99.1
  earnings 8-K, e.g. AAPL).
- Guards proving real path works: (a) at least one filing resolves a separate
  `EX-99.1` document with `document_type='exhibit_99_1'` and extracted text
  containing financial terms; (b) `filing_documents.raw_bytes` archived to Bronze
  as **raw HTML** (decompress → `<html` present), not extracted text;
  (c) `filing_id = sec:{cik}:{accession}`, `source_tier=1`.

### FRED (3F7)
- Free: 120 req/min; 12-series manifest = 11 fetched + T10Y2Y derived.
- Materialize `macro_observations`; confirm per-observation `released_at` =
  first-release `realtime_start` (output_type=4), NOT fetch date. Plane-2:
  assert `macro_observations` is referenced by neither `index_builder` nor
  `ticker_scope` (never embedded).

---

## 4. SQLite / data-quality invariants (E)

Enforced by automated mock/fixture tests in 3F8 (must bite — mutation proves
failure):

1. **One canonical per dedup group.** Every `dedup_group_id` with ≥1 member has
   exactly one `is_canonical=1`; no group has zero. (Cross-run stable.)
2. **Provider-native uniqueness.** No two rows share the same provider-native id
   within a source; canonical-URL collisions collapse to one canonical.
3. **Multi-ticker lossless.** `article_tickers` row count is preserved across
   rederive; a multi-ticker article keeps all its ticker associations.
4. **One vector per unit (future-proofing).** `index_state` has ≤1 row per
   `(corpus_item_id, source_kind)`; L1/L2 chunk counts match
   `is_rag_eligible=1` counts (the 3C guard). Prevents duplicate embeddings.
5. **Rederivability.** Every `articles`/`filings` row has a `raw_assets`
   ancestor whose decompressed payload reproduces its title.
6. **Source-tier correctness.** Every article/filing has a non-NULL
   `source_tier` in the expected set; Finnhub tier-by-publisher; SEC tier=1.
7. **T5 / opinion cap enforceable at retrieval.** `retrieval_policy` can cap
   low-tier/opinion sources at query time (assert the cap function honors tier).
8. **Stale detection after new rows.** Inserting a new article marks the
   relevant `index_state` stale / creates a pending index record (freshness
   detects it).
9. **Plane separation.** `macro_observations` never appears in `index_state`,
   `clean_assets`, or any embed path.

---

## 5. CLI / command matrix that must pass before Step 4 (question 5)

Automated (mock/fixture, committed tests):
- `status` / `status --freshness` renders with SEC+Finnhub+macro planes.
- `update-news --dry-run` returns correct missing-cell set (zero network/write).
- `backfill --dry-run` chunks correctly; zero network/write.
- idempotent rerun (mock): second dry-run identical; second live (mocked) writes
  delta 0 for already-success cells.
- zero-new-article day → no rows, no crash, checkpoint recorded.
- provider 429 / `Retry-After` honored (assert real header wins).
- interrupted run → resume skips completed cells (checkpoint correctness).

Live (manual, named commands, reported — not in CI):
- `update-news --live --confirm` small scope (1 ticker, few days).
- `backfill --live --confirm` small window.
- `update-macro --live --confirm`.
- SEC tiny live fetch.

---

## 6. Scripts / commands dscodex implements (question 7)

- **Read-only audit** (committed as `catalyst_data` module or `scripts/` per
  convention; report artifact untracked): `step3f_coverage_audit`.
- **Dry-run commands:** already exist; extend output to cover new planes.
- **Live-fetch commands (NEW, committed):** the gated `--live` path in
  `cli_index.py` that builds real connectors + limiter + httpx client and passes
  them as `fetch_fn`. Guards: requires env keys; refuses frozen DB path; echoes
  redacted config; requires `--confirm`. Default remains dry-run.
- **Test-only fixtures/mocks:** deterministic FetchResult fixtures for each
  provider; no network in tests.
- **Commands that write SQLite:** only the `--live --confirm` path and the 3F2
  migration, both gated and dev-DB-only.

---

## 7. What gets committed (question 8)

- Commit: data-core code + tests + fixtures only. Scoped commits per substep.
- Do NOT commit: coverage/backfill **reports** under `data/provider_discovery/`
  (generated artifacts) — add to `.gitignore`.
- Gitignore + keep untracked: `scripts/provider_discovery.py`,
  `packages/data-core/scripts/fmp_reprobe.py`,
  `packages/data-core/scripts/provider_discovery.py`, `data/provider_discovery/`.
- Dev DB (`catalyst_dev_ws4b.db`) is a build artifact — not committed; its
  mutation is intentional and only under approved live/migration steps.

---

## 8. Pass / fail gates before Step 4 (F)

Step 4 is unblocked only when ALL hold:

1. **Coverage complete** through latest closed trading day for Polygon across all
   10 tickers (no unexplained trading-day gaps).
2. **All three planes materialized live:** filings > 0 with ≥1 real EX-99.1;
   Finnhub articles > 0 with working cross-source dedup; `macro_observations`
   populated with PIT-correct `released_at`.
3. **No duplicate explosion:** dedup-group canonical invariant holds; repeat live
   run writes 0 net new raw_assets for unchanged input.
4. **Expected source counts** within sane bounds (documented in audit).
5. **Scoped automated tests green** (full data-core suite + new 3F invariant
   tests), each discriminating test bite-proven.
6. **Dry-run + live small-scope commands succeed** and are reported.
7. **Frozen DB SHA unchanged** = `0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf`.
8. **Dev SQLite mutated only intentionally** under approved steps; documented.
9. **No secrets** in any artifact/log (keys redacted to `[REDACTED]`).
10. **`index_state` populated** for prose plane (articles + filings) so Step 4
    has a defined work set (the embed target).

---

## 9. Step 4 preview (question 10)

After 3F green:
- Full cloud-GPU embed of **prose plane only**: `articles` (Polygon + Finnhub) +
  SEC `filings`/`filing_documents`. L1/L2 chunk via polymorphic
  `index_state(corpus_item_id, source_kind)`.
- Do NOT embed structured plane: FRED macro, and any future Tiingo/FINRA/Form4 —
  joined at query time by date, never vectorized.
- Write `index_manifests` + `index_state`; populate LanceDB gold.
- Daily incremental upsert: new/stale `index_state` rows only.

---

## 10. Constraints (standing)

- data-core only; never touch `packages/app` / `packages/agents`; leave their
  dirty files untouched (no reset/revert/clean/stash).
- Frozen DB SHA immutable (above). Dev DB `catalyst_dev_ws4b.db` only.
- No embeddings / LanceDB / model loads on the 8GB Mac in 3F.
- No new dependencies (httpx + stdlib). No network in automated tests.
- Never log API keys — redact to `[REDACTED]`. Real redacted fixtures only.
- English only; Conventional Commits; no "AI/Claude/GPT" in commits.
- Stage-only unless the user authorizes the commit.

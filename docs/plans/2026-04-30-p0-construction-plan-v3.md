# Catalyst P0 Construction Plan v3.2 — Data-First Defense Sprint

> **For agentic workers:** REQUIRED SUB-SKILL — `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans`. Per project CLAUDE.md, never run `git commit` or `git push` without explicit user authorization. `git add` (stage) is the maximum default action.

**Plan revision:** v3.2 (binding decisions codified, 2026-05-01). Supersedes `docs/plans/2026-04-30-p0-implementation-plan.md` v2 of the same date. v2 ordered control-plane first; v3 puts data + retrieval engineering first.

**v3.1 → v3.2 changes (D-1..D-10, all binding):**
- D-1 Task numbering authority formalized: Plan T-NN is authoritative for tags/branches/commits/scripts; strategy-spec tasks are referenced by name, not number.
- D-2 Freeze semantics locked: `catalyst_eval_frozen.db` is created/populated pre-freeze; marker captured at T-13a and anchored by `task/T-13b-close` on Day 9 EOD. Declared deviation DEV-01 from strategy Day-8 freeze timing.
- D-3 T-06 verification SQL canonicalized: news count whitelist query, OHLCV table query, non-empty checkpoint assertion before fail-rate division.
- D-4 GDELT moved from conditional to deferred: P0 ships `geo_corpus_tier=2` unconditionally; GDELT stays P1/roadmap.
- D-5 RAG precision minimum in P0: add optional `sentence_offset` field and rerank-ready retrieval signature (`rerank=None` in P0). Full two-level chunking/reranker deferred under W-14.
- D-6 Display contract frozen for notebook-now / UI-later mapping (`RunList`, `CaseDetail`, `EvidencePanel`, `RetrievalTimeline`, `OpsPanel`).
- D-7 Frozen schemas v1 added (`trace_events`, `comparison.json`, `preflight.json`) with additive-only compatibility rule through defense.
- D-8 Day 6–9 compression resolved: T-11 pulled forward; T-13 split into T-13a/T-13b; verification tags updated to `task/T-13b-close`.
- D-9 All OD rulings resolved and codified; operational triggers retained where runtime-dependent.
- D-10 Deck P7 canonical `Done / Deferred / Waivers` shape added for T-14/T-16 fill-in.

**Goal:** Deliver the Catalyst P0 closed loop for thesis defense in 14 days. P0 = (a) industrial-grade data ingestion (run/checkpoint discipline, conditional fallback, cross-source dedup, quality tagging) for 10 tickers × 1-year backfill; (b) reproducible vector index built on the frozen corpus; (c) policy-driven control plane (validator, 4-state output, deterministic DecisionRouter, Layer 1+2 retrieval, trace persistence); (d) baseline-comparison eval (`direct_llm` vs `mcj_full`, 10-case 5/2/3 set) with hard gate enforcement; (e) pre-recorded defense artifacts.

**Sources of truth (canonical, all four are anchors):**
- `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (r3) — strategic positioning, gates, waivers, task contracts.
- `docs/full-version-execution-spec.md` — technical execution baseline.
- `docs/data-ingestion-operating-spec.md` — primary + conditional fallback policy, quality gating, key governance.
- `data/eval_reports/provider_audit_20260426_164611.json` — empirical provider stability + payload + throughput data; **all capacity estimates in this plan derive from this artifact or are explicitly marked unknown**.
- `docs/testing/provider-audit-7d-test-spec.md` — audit acceptance thresholds.

**Hard scope rules:**
1. P0 only. P1 / P2 items are explicitly listed as waivers in §A.4. Anything not waived is forbidden inside the 14-day window.
2. Day 10 Gate (§I.4), Day 13 freeze (§J.4), and sample-protocol immutability (§I.2) are hard gates. Failure freezes downstream work.
3. **No multi-account ToS evasion.** API key pools are for resilience, isolation, and audit — never for circumventing per-account quotas. See §D.
4. Every task has entry / exit / verification with executable commands or checkable file paths.
5. Capacity estimates use formulas + assumptions + P50/P90/P95 bounds. Pseudo-precise numbers without provenance are not accepted.

## §0.bis Commit-Authorization Protocol & Task-Tag Convention

**Stage-only default.** Per CLAUDE.md, Claude does not run `git commit` or `git push` without explicit user authorization. Every task closes with `git add` of its artifacts and a prompt to the user for commit approval.

**Task-tag convention (load-bearing for verification).** When the user authorizes a task-close commit, the executor also creates a **lightweight tag** at that commit named `task/T-NN-close` (where `NN` is the task number). This makes downstream verification scripts deterministic — they reference the tag, not the commit message:

- `git rev-parse task/T-13b-close` returns the eval-freeze anchor commit SHA.
- `git log task/T-13b-close..HEAD` enumerates everything since the eval freeze anchor.
- The tag is `lightweight` (`git tag <name>`) for routine task closes; **annotated** (`git tag -a <name> -m "..."`) is reserved for `defense-freeze-YYYY-MM-DD` (T-16).

**Commit-message convention.** Authorized task-close commits use Conventional Commits with the task number in the subject scope, e.g. `feat(T-03): ingestion runs + checkpoints + quality flags`. This is for human readability; verification does **not** depend on grep-ing the message — that is what the task tag is for.

**Verification gating.** Tasks T-13a, T-13b, and T-16 must commit + tag before downstream verifications (T-15, T-16, §J.6 source-freeze invariant) can run. If a required tag does not exist, verification exits 2 (process blocker) rather than producing a false negative.

---

## A. Executive Summary

### A.1 P0 Minimum Closed Loop

The smallest set of artifacts that, taken together, demonstrate "evidence-bounded agent system" at thesis defense:

1. **Frozen corpus.** `catalyst_eval_frozen.db` populated by a 10-ticker × 365-day backfill via the `Primary + Conditional Fallback` policy. SHA-256 fingerprint pinned in run header. Pre-flight passes the §B thresholds.
2. **Frozen vector index.** LanceDB Gold tables at `data/lancedb_gold/eval_frozen/` built from `is_rag_eligible=true` canonical assets. Doc-count, chunk-count, retrieval smoke tests pass.
3. **Control plane.** Validator (4 checks per execution-spec §8) + deterministic DecisionRouter + Critic decision contract (4 fields) + Retrieval Layer 1 + Layer 2 (Layer 3 short-circuits). All wired in `graph.py`. Unit tests green.
4. **Trace persistence.** Single `trace_id` per case execution; node-level events in SQLite; JSON exporter; ≥20 trace files for the 10-case × 2-config eval.
5. **Eval report.** `direct_llm` and `mcj_full` runs over the frozen 10-case golden set (5 sufficient / 2 partial / 3 should-refuse). Markdown + JSON comparison report. Gate enforcement passes per §I.3.
6. **Pre-recorded defense artifacts.** Demo notebook reading from `catalyst_demo.db` (no live API), executed copy committed, deck draft (8 slides + appendix), audit log.

### A.2 Top-5 Risks

| # | Risk | Probability | Impact | Mitigation |
|---|---|---|---|---|
| RT-1 | Polygon news backfill blows past Day 5 due to 5 req/min limit + retries | Medium-High | Critical (blocks T-07 vector build) | Run backfill **starting Day 4 EOD in background**; budgeted §C.1 at ~3.5h wall-clock with 10 tickers; if running late, activate W-12 (reduce backfill window from 365 → 180 days) — protocol is to shrink the time window, not the ticker set, since the 10-ticker × n-case eval distribution must hold |
| RT-2 | FMP fundamentals run-OK rate audited at 0.77 (8/35 failures) — unstable | Medium | Medium (fundamentals are not on the critical eval path) | T-04 defines fmp as "fundamental Primary, fail-soft fallback to skip"; missing fundamentals are documented per case but not gating |
| RT-3 | Layer 2 macro corpus depth insufficient (FRED-only is structured, not narrative) | Medium | High (Layer 2 silently empty-spins) | T-13a preflight requires Tier-2 corpus thresholds (macro >=30 and geo-tagged >=20 in case windows). If not met, run targeted macro ingestion before freeze. |
| RT-4 | Tier-A reproducibility breaks because model_id drifts between freeze and rerun | Medium | Critical (entire reproducibility claim collapses) | T-13b captures `model_id` per role + DB SHA + git SHA in eval run header; T-15 §J.6 source-freeze invariant blocks any `packages/` change between T-13b and T-16 freeze tag |
| RT-5 | Day 10 Gate fails (`evidence_validity < 0.95` or `should_refuse_hit_rate < 2/3`) | Medium | High (defense narrative weakens) | §I.4 Day 10 Gate decision tree: P1 immediately frozen, Day 11 EOD recheck, Day 13 ships honest disclosure if still red — protocol stays intact, never reduce sample to pass |

### A.3 Hard Gate Metrics (defense floor; from strategy spec r3 §4)

| Gate | Threshold | Failure consequence |
|---|---|---|
| `evidence_validity` | ≥ 0.95 | Hard fail |
| `schema_validity` | = 1.00 (10/10) | Hard fail |
| `trace_completeness` | = 1.00 (10/10 runs full chain) | Hard fail |
| `should_refuse_hit_rate` | ≥ 2/3 on the 3 should-refuse cases | Hard fail |
| `cost_latency_reported` | All configs report cost/latency/tokens | Hard fail |

Quality metrics (`attribution_f1`, `category_accuracy`, `grounding_rate`, `temporal_precision`) are reported but **not gated** — strategy spec §1.3 explicitly does not commit to beating direct LLM baselines on answer quality.

### A.4 P0 Waivers

**Active waivers** (reference strategy spec §11; kept here for scope discipline):

- **W-01** rag_only baseline → P1 only if Day 10 Gate green.
- **W-02** Layer 3 retrieval → P2.
- **W-03** Critic LLM-graded sufficiency → P2; P0 uses thresholds (§H.2).
- **W-04** Budget circuit breaker → P2; P0 reports cost/latency without enforcement.
- **W-05** Model routing → P2; P0 single-model.
- **W-06** LangSmith → P1; P0 local SQLite trace only.
- **W-07** Full eval matrix → roadmap; P0 = `direct_llm` vs `mcj_full` on 10 cases.
- **W-08** `refusal_precision`/`refusal_recall` → substituted by `should_refuse_hit_rate` for n=3.
- **W-09** GDELT 2.0 geopolitical corpus → deferred to P1/roadmap. P0 ships `geo_corpus_tier=2` unconditionally (Polygon news tagged macro/policy/tariff).

**Note on ticker scope.** P0 covers 10 tickers (AAPL, AMD, AMZN, GOOGL, JPM, META, MSFT, NVDA, TSLA, UNH — from `packages/eval/golden_set/v1_2.jsonl`). This is the **in-spec lower bound** of `data-ingestion-operating-spec.md §4.1` ("10–30 tickers"); not a deviation, not a waiver. Expansion toward 30 is post-defense roadmap.

**Conditional waivers** (activated only if their failure trigger fires; otherwise dormant):

- **W-11** Cross-source semantic dedup → P1. **Trigger:** T-04 cross-source URL canonicalization passes but full semantic dedup proves too risky to ship in one day. **Effect when activated:** ship URL-canon + same-source dedup only; semantic-similarity dedup deferred. **Audit:** record in `docs/testing/audit_<date>.md` Day-3 entry.
- **W-12** Backfill window reduction → reduced-scope P0. **Trigger:** T-06 produces < 20,000 news articles (well below P50 floor) due to provider outage. **Effect when activated:** narrow backfill window from 365 → 180 days; document the actual window in eval report header. **Audit:** record in deck P7 with the reduced-window number.
- **W-13** Demo notebook → static walkthrough. **Trigger:** T-16 cannot get `notebooks/demo.ipynb` to execute offline. **Effect when activated:** swap to `docs/defense/walkthrough.md` referencing frozen artifacts; live notebook deferred to post-defense. **Audit:** record in audit log + deck P7.
- **W-14** Two-level chunking + reranker + sentence-offset validator enforcement → deferred to P1. **Trigger:** P1 starts. **Effect when activated:** introduce `doc_chunk`+`span_chunk`, two-stage retrieval (recall→rerank), and validator checks for sentence offsets.

A conditional waiver becomes "active" only when its trigger fires; until then it is documented for transparency but does not affect scope.

### A.5 Task Numbering Authority

Plan T-NN labels are authoritative for task tags (`task/T-NN-close`), branches (`tNN-<noun>`), commits (`feat(T-NN): ...`), and verification scripts. Strategy-spec §13 task contracts are referenced by **name** (e.g., "Strategy spec — Validator + 4-state"), never by number, in this plan and in audit logs.

#### A.5.1 Plan ↔ Strategy Task Mapping

| Plan T-NN | Strategy-spec named contract | Notes |
|---|---|---|
| T-01 | Canonical-doc alignment | Plan-specific hardening task |
| T-02 | DB triple split + artifact paths | 1:1 equivalent |
| T-03 | Ingestion runs/checkpoints/quality flags | Plan data-first addition |
| T-04 | Conditional fallback + cross-source dedup | Plan data-first addition |
| T-05 | Provider limits/retry/key policy | Plan data-first addition |
| T-06 | Backfill execution | Plan data-first addition |
| T-07 | Vector index rebuild | Plan data-first addition |
| T-08 | Validator + 4-state + Critic schema | Strategy "Validator + 4-state" |
| T-09 | DecisionRouter | Strategy "Deterministic Router" |
| T-10 | Retrieval Layer 1+2 wiring | Strategy "Retrieval Layering" |
| T-11 | Trace persistence | Strategy "Trace persistence schema/writer" |
| T-12 | direct_llm baseline + failure taxonomy tests | Strategy "baseline + taxonomy" merged |
| T-13a | Preflight + golden-set freeze + SHA capture | Plan split from strategy eval-freeze step |
| T-13b | Eval runs + threshold calibration | Plan split from strategy eval-freeze step |
| T-14 | Comparison report + gate script + deck draft | Strategy "Eval report + deck draft" |
| T-15 | Day10 gate + verification audit | Strategy "Verification audit" |
| T-16 | Dry-run + code freeze + rehearsal | Strategy "Tech dry-run + freeze + rehearsal" merged |

### A.6 Strategy-Spec Deviations Declared In Plan v3.2

| ID | Strategy spec | Plan deviation | Rationale |
|---|---|---|---|
| DEV-01 | §6 / §8.5 freeze on Day 8 | Freeze on Day 9 EOD | Data-first reordering puts ingestion/vector build on Days 2–5, control-plane Days 6–8, eval freeze on Day 9. One-day right shift is explicit and audited. |

---

## B. Data Volume & Capacity Model

All numbers below are derived from `data/eval_reports/provider_audit_20260426_164611.json` (n=35 runs, 5 tickers × 7 days, 2026-04-16 to 2026-04-24) and the source policy in `data-ingestion-operating-spec.md`. **Where audit data is absent (10-ticker generalization, 1-year extrapolation, FMP/FRED/GDELT volume), the estimation method is stated and the unknown is flagged.**

### B.1 Universe and Window

- **Tickers:** N = 10 (AAPL, AMD, AMZN, GOOGL, JPM, META, MSFT, NVDA, TSLA, UNH — from `golden_set/v1_2.jsonl`).
- **Backfill window:** 365 calendar days (≈ 252 trading days) ending at the latest case date in golden set.
- **Daily-sync window:** D-1 main + D-2..D rolling compensation per `data-ingestion-operating-spec.md §5.2`.
- **Generalization risk:** audit covered 5 mega-cap tech tickers. UNH/JPM news density is plausibly lower; AMD volume similar to NVDA. Estimation uses audit mean × `tier_factor ∈ [0.6, 1.2]` to bracket P50/P90/P95.

### B.2 Polygon News (primary, news source)

**Audit observations (per ticker × day):**
- `articles/ticker/day` mean = 10.8.
- `median_article_chars` = 443; `p90_article_chars` = 540.
- `avg_payload_bytes` per response = 31,371.
- `avg_endpoint_latency_ms` = 6,286.
- `run_ok_rate` = 1.0; `endpoint_ok_rate` = 1.0.
- `dedup_removed_total` = 0 within 7d, 5-ticker window — but cross-ticker, cross-event duplicates rise on big-event days.

**Formulas:**

```
articles_per_ticker_day = audit_mean × tier_factor          # mean 10.8 × [0.6 ~ 1.2]
articles_per_year_per_ticker = articles_per_ticker_day × 365
articles_per_year_total = articles_per_year_per_ticker × N
bronze_bytes_per_article = avg_payload_bytes / avg_articles_per_run
                         = 31371 / 10.8 = 2905 bytes
silver_bytes_per_article = avg_markdown_chars × utf8_factor / articles_per_run
                         ≈ 8896 × 1.2 / 10.8 = 988 bytes (conservative ~1.2 KB)
chunks_per_article = ceil((article_chars - overlap_chars) / (chunk_chars - overlap_chars))
                   ≈ ceil((443 - 256) / (1024 - 256)) = 1   (most articles fit in 1 chunk)
bytes_per_chunk = embedding_bytes + text_bytes + metadata_bytes
                = 1024_dim × 4_bytes + 0.6_KB + 0.4_KB
                = 4 KB + 1 KB = 5 KB
```

**Three-tier estimate (per year, 10 tickers, news only):**

| Bound | tier_factor | articles | Bronze (raw JSON) | Silver (markdown) | Gold (vector chunks) |
|---|---|---|---|---|---|
| P50 (mean) | 1.0 | 39,420 | 110 MB | 39 MB | 197 MB |
| P90 | 1.2 | 47,304 | 132 MB | 47 MB | 237 MB |
| P95 | 1.4 | 55,188 | 154 MB | 55 MB | 276 MB |

**Cross-source duplicates (vs FMP news / Finnhub news / GDELT) — when conditional fallback fires:** duplicate rate observed = 0 in audit (single source). Spec §7.1 expects ~10–30 % cross-source overlap on big-event days. After dedup, expected unique-article count drops by 10–25 %. The Gold-only count after dedup is the relevant figure for vector index size.

### B.3 Polygon OHLCV (primary, market structure)

- 1 row per ticker per trading day. `avg_payload_bytes` for full-year query ≈ 290 (very compact — daily candles).
- `run_ok_rate` audited at 0.971 (1 retry case in 35 runs).
- 10 tickers × 252 trading days = 2,520 rows. Storage ≈ 75 KB total (P50/P90/P95 bounds collapse — fixed schema).

### B.4 FMP Fundamentals

- 4 statement types (income, balance, cash flow, ratios) × 4 quarters × 10 tickers = 160 records max for 1 year.
- `avg_payload_bytes` audit = 18,124. Storage worst case: 160 × 18 KB = ~3 MB. **Run-OK rate audited at 0.771 — flagged as unstable (RT-2).**
- **Daily sync impact:** fundamentals refresh quarterly only — daily call volume averages < 1 call/ticker/day.

### B.5 FRED Macro

- 10 macro series (FEDFUNDS, CPIAUCSL, UNRATE, DGS10, DGS2, DFF, DEXUSEU, VIXCLS, BAA10Y, T10YIE per `data-ingestion-operating-spec.md §3.2`).
- One historical request returns full series. ~1,300 rows/series × ~50 bytes = 65 KB/series. Total ≈ 650 KB. **Run-OK rate audited at 1.0 — most stable source.**
- Series list is resolved in OD-3 and fixed for P0 in §M.

### B.6 GDELT (deferred, P1 planning only)

- GDELT is **not ingested in P0**. P0 Layer-2 geopolitical evidence uses Tier-2 corpus only (Polygon news tagged macro/policy/tariff), and `geo_corpus_tier=2` is enforced in preflight.
- **Estimation method (for P1):** GDELT daily file ≈ 50 MB compressed; full-year backfill ≈ 18 GB if downloaded raw. Filtering to case-window dates and case-relevant entities (10 tickers' sector + macro categories) cuts to ~5 % → ~900 MB.
- **Implementation note (P1):** ingestion script does not exist in `packages/data-core/scripts/`; P0 does not author new ingestion scripts.

### B.7 Total P0 Storage Footprint (10 tickers × 1 year)

| Layer | News | OHLCV | Fundamentals | Macro | Layer 2 (Tier 2 fallback) | Total P50 | Total P95 |
|---|---|---|---|---|---|---|---|
| Bronze | 110 MB | 0.1 MB | 3 MB | 1 MB | included in News | 114 MB | 158 MB |
| Silver | 39 MB | 0.1 MB | 3 MB | 1 MB | included | 43 MB | 59 MB |
| Gold (LanceDB, w/ index overhead 1.4×) | 276 MB | n/a (not vectorized) | 4 MB | 1 MB | included | 281 MB | 387 MB |
| **Total disk** | | | | | | **~440 MB** | **~600 MB** |

This fits comfortably on local SSD. No external storage tier required.

---

## C. Provider Rate-Limit Strategy

All numbers are derived from audit observations or documented free-tier limits. Where vendor docs are not cited inline, the limit is conservative — actual paid-tier limits may be higher.

### C.1 Polygon News + OHLCV

- **Free tier limit:** 5 requests/minute per API key (Polygon documented).
- **Audit latency:** news 6.3s/call, OHLCV 10.6s/call (likely retry-amplified — single-shot OHLCV typically <2s). Latency is **not** the binding constraint at 5/min; the rate-limit bucket is.
- **Token bucket parameters:**
  - `rate = 5/60 = 0.083 req/s` (free tier).
  - `burst = 5` (refill to bucket).
  - `concurrency_semaphore = 1` (single in-flight, since burst=5 saturates the bucket on completion).

**Backfill request volume (10 tickers × 1 year):**
- News: assume per-ticker chunked by 7-day windows × pagination ≈ 2 pages avg → 10 × 52 × 2 = **1,040 calls**.
- OHLCV: 1 call per ticker for full year history → 10 calls.
- Total: ~1,050 polygon calls.
- At 5 req/min: ⌈1,050 / 5⌉ = 210 minutes = **3.5 hours wall-clock**.
- With 25 % retry overhead (transient 429/5xx): ~4.4 hours.

**Daily-sync request volume:**
- News: 10 tickers × 1 call/day = 10 calls.
- OHLCV: 10 calls.
- Daily-sync: ~20 calls = 4 minutes wall-clock.

**Retry / backoff policy:**
- 429: exponential backoff with jitter, base 60s, max 300s, max-retries 3. **Do not** retry below the rate-bucket window — wait for next refill.
- 5xx: exponential backoff base 5s, max 60s, max-retries 5.
- Connection timeout (audit shows occasional 10s+ tail): retry once with same params; if still failing, defer to checkpoint and continue next ticker.
- Permanent fail (4xx other than 429): **mark checkpoint as `failed` with error class, do not retry**, surface to ingestion report.

### C.2 FMP Fundamentals

- **Free tier limit:** 250 requests/day (FMP documented). No per-minute constraint at this volume.
- **Audit run-OK 0.77** — 23 % failure rate is anomalous for a stable endpoint. Likely either intermittent provider issue or audit ran while FMP had degraded availability. **Action item:** before Day 4, **re-run the audit for FMP only** to confirm whether the failure is transient (network) or persistent (account/quota); record decision in T-05.

**Backfill request volume:** 4 statements × 10 tickers = 40 calls (statement endpoints return multi-quarter history). Fits in one day budget.

**Daily-sync:** quarterly cadence — 0–10 calls on earnings days, 0 most days.

**Retry / backoff:**
- 429 (rare here): same as Polygon.
- 5xx / timeout: 3 retries, base 5s exponential.
- If `run_ok_rate` over a 10-call sample stays < 0.85 on Day 4 re-audit, **fmp is demoted to fallback-only** for fundamentals; missing fundamentals are documented per case but the case is not blocked.

### C.3 FRED Macro

- **Free tier limit:** 120 requests/minute (FRED documented; very generous).
- **Audit run-OK 1.0**, latency 1.8s.

**Backfill request volume:** 10 series × 1 call each = 10 calls. ~10 seconds wall-clock.

**Retry / backoff:** 429 unlikely; 3 retries with 10s base on 5xx.

**Single key sufficient.** No fallback needed at P0 scope.

### C.4 GDELT (not in P0; retained for P1 planning)

- No API key required; rate limits informal.
- P0 does not ingest GDELT. Preflight enforces `geo_corpus_tier=2` (Polygon news macro-tagged corpus).
- Section is retained so P1 can estimate integration cost and storage impact without rewriting the plan.

### C.5 Single-Key Sufficiency Decision

| Source | Single key sufficient? | Rationale |
|---|---|---|
| polygon_news | **Yes for backfill**; tight for parallel daily-sync of 10 tickers if eval reruns happen on same day | 5 req/min × 60 min × 24 h ≈ 7,200 req/day budget; backfill 1,050 calls + daily 20 calls + 25 % overhead well within |
| polygon_ohlcv | Yes | shares the polygon key; OHLCV is low-frequency |
| fmp | Yes for P0 (40 backfill calls), but unstable — see C.2 | 250/day budget × 1 day of backfill leaves daily-sync untouched |
| fred | Yes | 120/min is enormous relative to 10/year |

**No multi-key requirement at P0.** Multi-key would be required only if Polygon rate budget needs to be multiplied for parallel backfill on >30 tickers — which is out of P0 scope (defense covers 10).

### C.6 Throughput & Wall-Clock Budget Summary

| Phase | Calls | Wall-clock (incl. 25 % retry) | Notes |
|---|---|---|---|
| Polygon backfill (10 tickers × 1 yr) | ~1,050 | ~4.4 h | Run in background starting Day 4 EOD; finish before T-07 (Day 5) |
| FMP backfill | ~40 | ~2 min | Run synchronously Day 3 |
| FRED backfill | ~10 | ~10 s | Run synchronously Day 3 |
| Daily sync (combined) | ~20 | ~4 min | Run once per day from Day 5 onward |
| Re-runs (eval) | ~30 (no ingestion) | ~10 min | Eval reads frozen DB; no provider calls |

---

## D. API Key Management Policy

### D.1 Storage and Loading

- Keys live in `.env` at repo root (already gitignored). **Verified before T-01:** `.gitignore` includes `.env`.
- Keys loaded via `python-dotenv` in `packages/data-core/catalyst_data/config.py` (existing module — to be inspected in T-02 for the actual mechanism).
- Keys never written to logs, traces, or reports. The provider audit JSON does not contain keys (verified — payload contains only response data).

### D.2 Key Rotation and Pool

A "key pool" in this project means **one or two keys per provider**, used for:
- **Resilience:** if one key is rate-limited or temporarily blocked, fall over to the second.
- **Environment isolation:** dev key vs. eval-frozen key (the eval-frozen run uses a key recorded in the run header for audit trail).
- **Audit trail:** `run_header.api_key_id` stores a hash of the key used (not the key itself) so reruns can be tied to the same credential.

**The pool is NOT used to:**
- Multiply per-account quotas (per strategy spec §11 W-04 budget governance and `data-ingestion-operating-spec.md §9`).
- Spread load across "alt accounts" — that would constitute ToS evasion and is forbidden in this project.

### D.3 Multi-Key Decision Tree

```
Need more than 1 key for provider X?
├─ Quota insufficient at P0 scope (10 tickers × 1 year × 1.25× retry)?
│   └─ Yes → Reduce backfill window (W-12) or activate fallback source first.
│            Multi-key for quota multiplication is FORBIDDEN.
├─ Single point of failure if key revoked/rate-limited?
│   └─ Yes → 1 backup key permitted (resilience use case).
└─ Need eval-frozen vs dev separation for reproducibility?
    └─ Yes → 1 dedicated eval key permitted (environment isolation).
```

At P0 scope, the answer for every provider is "no" on the first branch. Backup key is **optional** (recommended for Polygon since it's the longest-running call), eval-frozen separation is **required** for the T-13a/T-13b freeze flow (the same key is hashed and recorded; not a different key).

### D.4 Provider-Outage Degradation Path

| Provider | Outage signal | Degradation |
|---|---|---|
| Polygon | run_ok_rate over rolling 20 calls < 0.5, OR sustained 429 with backoff > 5 min | Mark current ingestion run as `failed`; retry next scheduled window. Backfill resumes from last `success` checkpoint. |
| FMP | already-low ok rate; treat as fail-soft | Skip; fundamentals missing for affected case is logged in `asset_quality_flags`; case is **not** blocked from eval. |
| FRED | (unlikely) | Same as Polygon — checkpoint-based resume. |
| Embedding model load | bge-m3 not available (HF down or local model missing) | Vector index build (T-07) blocks; this is a P0 critical-path dependency. Local model file at `~/.cache/huggingface/hub/` must be verified before T-07 starts. |

### D.5 Alerting

- All ingestion runs write to `ingestion_runs` table (T-03). Daily ingestion report (markdown to `data/eval_reports/ingestion_<YYYYMMDD>.md`) summarizes coverage, failure counts, and per-source ok rates.
- **No external alerting (PagerDuty, email) at P0** — operator manually reviews report each day.

---

## E. Ingestion Pipeline Design

### E.1 End-to-End Step Order (idempotent for both backfill and daily sync per spec §2 Decision C)

```
1. source fetch          → returns raw payload bytes + provider metadata
2. schema normalize      → maps to canonical Asset record (models.py)
3. hard dedup            → (canonical_url, published_utc) within source
4. quality tagging       → is_rag_eligible + quality_reason + quality_score
5. silver write          → SQLite clean_assets table (markdown)
6. cross-source dedup    → links duplicates to canonical_asset_id
7. gold eligibility      → filter is_rag_eligible=true AND is_canonical=true
8. vector build/update   → LanceDB chunks + bge-m3 embeddings
```

### E.2 Step-Level Input/Output Contract

**Step 1 — source fetch.**
- Input: `(source_type, ticker, date_window, api_key_id)`.
- Output: `RawAsset(source, payload_bytes, fetched_at_utc, http_status, provider_request_id)`.
- Side-effect: write row to `raw_assets` (Bronze) with `INSERT OR REPLACE` on key `(source, canonical_url, published_utc, ticker)`.

**Step 2 — schema normalize.**
- Input: `RawAsset`.
- Output: `NormalizedAsset(asset_id, source, source_type, ticker_set, title, body_md, url, published_utc, provider_metadata_json)`.
- Computed: `asset_id = sha256(source_type|canonical_url|published_utc|ticker_primary)` — deterministic per spec §4.4.

**Step 3 — hard dedup.**
- Input: stream of `NormalizedAsset` from one source-run.
- Rule: drop if `(normalized_title, ticker, abs(published_utc_delta) < 2 h)` matches earlier row. Spec §7.1.
- Output: deduplicated stream with provenance preserved (dropped rows logged to `dedup_log`).

**Step 4 — quality tagging.**
- Input: `NormalizedAsset`.
- Logic: `is_rag_eligible = (char_count >= 200) AND (title not null) AND (published_utc not null) AND (source not null) AND (language == 'en')` (spec §6.1; thresholds configured in `packages/data-core/catalyst_data/config.py`, not hardcoded).
- Output: `QualityFlag(asset_id, is_rag_eligible: bool, quality_reason: enum, quality_score: float)`.
- Side-effect: row in `asset_quality_flags`.

**Step 5 — silver write.**
- Input: `NormalizedAsset` + `QualityFlag`.
- Side-effect: row in `clean_assets` table; idempotent on `asset_id` (UPSERT).

**Step 6 — cross-source dedup.**
- Input: candidate `NormalizedAsset` against existing canonical set with same `(ticker, published_date_bucket)`.
- Rule: URL-canonicalization (strip utm_*, ref_, query-order normalization); if canonical URL matches existing canonical, mark new row as `is_canonical=false` and set `canonical_asset_id = existing_canonical.asset_id`.
- Spec §7.2 ordering: same-source first, then cross-source.

**Step 7 — gold eligibility.**
- Filter: `is_rag_eligible=true AND is_canonical=true`. Apply in `build_index.py` (existing script, T-07 modifies for new schema).

**Step 8 — vector build.**
- Input: filtered Gold-eligible `clean_assets` rows.
- Chunk: `chunk_size=1024 chars (≈ 256 tokens), chunk_overlap=256 chars`. For 443-char articles, this yields 1 chunk per article (no split).
- Embed: `bge-m3`, dim=1024, normalized.
- Write: LanceDB table `gold_chunks` with columns `(chunk_id, asset_id, ticker_primary, text, embedding, published_utc, source_type)`.

### E.3 Checkpoint Granularity & Replay

- **Checkpoint key:** `(source_type, ticker, date)` per spec §4.3.
- **Status enum:** `pending | success | failed | skipped`.
- **Replay rule:** re-running the orchestrator processes only `pending | failed`; `success` rows are skipped without re-fetching.
- **Skipped:** for cases where the source explicitly returns "no data" (e.g., FRED on a market holiday for daily series) — logged but not retried.
- **Storage:** `source_checkpoints` table — added in T-03.

### E.4 Idempotent Write Strategy

All Bronze and Silver writes use SQLite `INSERT OR REPLACE` keyed on `asset_id` (per spec §4.4). The `INSERT OR IGNORE` path is **not** used because re-fetching the same source can return enriched payload (e.g., body backfill); REPLACE preserves the latest copy.

LanceDB writes use append-with-dedup-by-`chunk_id` semantics. For full-rebuild scenarios (vector model change), the table is dropped and rebuilt (T-07.4).

---

## F. Dedup, Normalize, Quality Rules

### F.1 Same-Source Hard Dedup Rule

```
duplicate_within_source(a, b) :=
    a.source == b.source
  AND normalize_title(a.title) == normalize_title(b.title)
  AND a.ticker_primary == b.ticker_primary
  AND abs(a.published_utc - b.published_utc) < 7200  # 2 hours
```

`normalize_title`: lowercase, strip punctuation, collapse whitespace, drop ticker mentions (so "AAPL Q4 Earnings Beat" and "Apple Q4 Earnings Beat" match).

### F.2 Cross-Source Hard Dedup Rule

```
duplicate_across_source(a, b) :=
    canonical_url(a.url) == canonical_url(b.url)
  OR (
       normalize_title(a.title) == normalize_title(b.title)
    AND a.ticker_primary == b.ticker_primary
    AND abs(a.published_utc - b.published_utc) < 14400  # 4 hours
  )
```

`canonical_url`: drop fragments, drop tracking query params (`utm_*`, `ref_*`, `mc_*`), sort remaining query params alphabetically, lowercase host.

### F.3 Canonical Selection Strategy

When multiple rows are duplicates:
1. **Primary source priority:** `polygon_news > fmp_news > finnhub_company_news > gdelt_news` (spec §3.2). Pick the one from the highest-priority source.
2. **Tiebreaker (same source, time):** earliest `published_utc` wins.
3. **Tiebreaker (same time):** longest `body_md` wins.

Non-canonical rows: `is_canonical=false`, `canonical_asset_id` set to the chosen canonical's `asset_id`.

### F.4 `is_rag_eligible` Decision Logic

```
is_rag_eligible(asset) :=
     char_count(body_md) >= MIN_CHAR_COUNT       # default 200, configurable
  AND title is not null and not empty
  AND published_utc is not null
  AND source is not null
  AND detected_language == "en"
  AND not is_template_or_spam(asset)              # heuristic: dedup_count of normalized_title across all sources < 5
```

Failures populate `quality_reason`:
- `short_text` — char_count below threshold.
- `missing_fields` — title/published/source missing.
- `non_target_language` — non-English.
- `template_spam` — repeated boilerplate detected.

### F.5 Conflict-Handling Table

| Scenario | Rule |
|---|---|
| Same title, different URL, same source, < 2 h apart | Same-source dedup (F.1). Keep earliest. |
| Same title, different URL, different source, < 4 h apart | Cross-source dedup (F.2). Keep primary-source row. |
| Same URL after canonicalization, different title text | Cross-source dedup (F.2). Keep primary-source row; log title mismatch in `dedup_log`. |
| Different titles, same URL after canonicalization | Treat as duplicate (URL is stronger signal). Keep primary-source row. |
| Same article scraped on different days (re-publication) | NOT a duplicate; both rows stay. Each has its own `published_utc`. |
| Title macher but published_utc differs > 4 h | NOT a duplicate; events with same title can repeat (e.g., "Tesla recalls Model 3"). |
| One has body, other empty | Prefer the one with body (canonical). Empty one stays as audit trail with `is_canonical=false`. |

---

## G. Vector DB Construction Plan

### G.1 Three Index Lifecycles

| Index path | Source DB | Lifecycle | Mutation policy |
|---|---|---|---|
| `data/lancedb_gold/dev/` | `data/catalyst_dev.db` | Daily — rebuilt on demand | Free to mutate during development (T-01..T-12) |
| `data/lancedb_gold/eval_frozen/` | `data/catalyst_eval_frozen.db` | File created/populated by T-06 (Days 4–5). Freeze marker captured at T-13a; freeze anchor tag is `task/T-13b-close` on Day 9 EOD. | After T-13b anchor, DB+index are read-only. Any write is a P0 protocol violation. |
| `data/lancedb_gold/demo/` | `data/catalyst_demo.db` | Built on Day 12 (T-16 prep) from eval_frozen artifacts | Read-only during defense rehearsal |

### G.2 Embedding Model Pinning

| Field | Value | Recorded where |
|---|---|---|
| `model_id` | `BAAI/bge-m3` | `data/eval_reports/<frozen>_run_header.json` |
| `model_revision` | exact HuggingFace commit SHA at load time | same |
| `embedding_dim` | 1024 | same |
| `chunk_size_chars` | 1024 (≈ 256 tokens) | same |
| `chunk_overlap_chars` | 256 (≈ 64 tokens) | same |
| `normalize_embeddings` | true | same |

If the local model cache (`~/.cache/huggingface/hub/`) does not contain bge-m3 at T-07 entry, T-07 entry-criterion fails — the model must be pre-fetched (Day 4, no API quota cost since it's HF download).

### G.3 Build-Trigger Conditions

| Trigger | Action |
|---|---|
| Embedding model_id or revision changes | Full rebuild of the affected index. |
| `chunk_size` or `chunk_overlap` changes | Full rebuild. |
| New rows written to silver after last build | Incremental upsert on `(asset_id, chunk_position)` if model unchanged. |
| `is_rag_eligible` flips false on existing row | Delete the corresponding chunks from the index. |
| Cross-source dedup re-canonicalization | Delete non-canonical chunks; reinsert canonical's chunks if not already present. |

### G.4 Index Health Check (run after every build, before any retrieval test)

```bash
# Inputs: env LANCEDB_PATH, env CATALYST_DB_PATH
# Smoke-tests, not full validation. T-07 verification block calls this script.

python -c "
import lancedb, sqlite3, os
db = lancedb.connect(os.environ['LANCEDB_PATH'])
t = db.open_table('gold_chunks')
n_chunks = t.count_rows()
sql = sqlite3.connect(os.environ['CATALYST_DB_PATH'])
n_eligible = sql.execute('SELECT COUNT(*) FROM clean_assets WHERE is_rag_eligible=1 AND is_canonical=1').fetchone()[0]
ratio = n_chunks / max(n_eligible, 1)
assert 0.9 <= ratio <= 1.5, f'chunk/article ratio {ratio:.2f} out of expected [0.9, 1.5] range'
print(f'OK: chunks={n_chunks}, eligible_articles={n_eligible}, ratio={ratio:.2f}')
"
```

The expected ratio range `[0.9, 1.5]` reflects: most articles fit in 1 chunk (median 443 chars → 1 chunk); a few longer pieces split to 2 chunks. A ratio > 1.5 suggests over-chunking; < 0.9 suggests missing assets.

### G.5 SQLite ↔ LanceDB Consistency Check

Every chunk's `asset_id` in LanceDB must resolve to a row in `clean_assets` with `is_rag_eligible=1 AND is_canonical=1`. Run after each build:

```bash
python -c "
import lancedb, sqlite3, os
db = lancedb.connect(os.environ['LANCEDB_PATH'])
t = db.open_table('gold_chunks')
chunk_asset_ids = set(r['asset_id'] for r in t.to_pandas()[['asset_id']].to_dict('records'))
sql = sqlite3.connect(os.environ['CATALYST_DB_PATH'])
sql_eligible = set(r[0] for r in sql.execute(
    'SELECT asset_id FROM clean_assets WHERE is_rag_eligible=1 AND is_canonical=1'
).fetchall())
orphans = chunk_asset_ids - sql_eligible
assert not orphans, f'{len(orphans)} chunks reference non-eligible/non-canonical assets: {list(orphans)[:3]}'
print('OK: every chunk maps to an eligible canonical asset.')
"
```

A failure here means the build either (a) missed quality filtering (process bug) or (b) ran against a stale silver state (sequencing bug).

---

## H. Agent Workflow Integration

### H.1 RetrievalPolicy Read Path

`packages/agents/catalyst_agents/retrieval/policy.py` (created in T-10):

```
retrieve(query, layer, metadata, *, rerank=None) →
   Layer.DIRECT  → SQL filter on clean_assets WHERE ticker=? AND published_utc IN [t-W, t+W]
                   THEN LanceDB hybrid query (BM25 + vector) on gold_chunks filtered by asset_id IN (...)
                   Returns top_k=8.
   Layer.MACRO   → SQL filter on clean_assets WHERE source_type IN ('macro','geopolitical')
                                                 AND published_utc IN [t-W, t+W]
                   THEN LanceDB hybrid query on gold_chunks filtered by asset_id IN (...)
                   Returns top_k=8.
   Layer.RELATED → raises NotImplementedError("layer3_not_implemented") (per W-02).
```

### H.2 Critic Decision Contract Schema (P0 — threshold-based per W-03)

```
CriticDecision:
  sufficiency:        Literal["sufficient", "partial", "insufficient"]
  next_action:        Literal["proceed", "expand_macro", "expand_related", "refuse"]
  magnitude_coverage: float ∈ [0, 1]
  reasoning:          str
```

P0 internal rule (calibrated against the 10-case set on Day 9):

```
sufficient   if evidence_count >= K_sufficient  AND magnitude_coverage >= M_threshold
partial      if K_partial <= evidence_count < K_sufficient
                                                OR magnitude_coverage < M_threshold
insufficient otherwise
```

Initial values (OD-1): `K_sufficient=4, K_partial=2, M_threshold=0.6`.  
T-13b calibration may adjust within `K_* ±1` and `M_threshold ±0.1` without re-approval; larger swings require user ruling plus deck P7 footnote.

### H.3 retrieval_metadata Field Schema (mirrored to trace)

```
RetrievalMetadata:
  layers_attempted:    List[Layer]
  expansion_reasons:   List[str]      # e.g. ["initial", "evidence_count<K_partial"]
  stop_reason:         Literal["sufficiency_reached", "expansions_exhausted",
                                "layer3_not_implemented", "system_error"]
  hit_counts_per_layer: Dict[Layer, int]
  geo_corpus_tier:     Literal[2]      # fixed in P0 preflight
  total_unique_evidence: int
```

Trace events (T-11 schema, written per node transition) include a `decision` field that captures the relevant slice of `RetrievalMetadata` for the triggering step.

### H.4 Layer 2 Insufficient-Data Behavior

If Layer 2 retrieval returns < 2 hits (defined as `RetrievalMetadata.hit_counts_per_layer[Layer.MACRO] < 2`):

1. DecisionRouter does NOT promote `expand_macro` to a third try (max_expansions=2 enforces).
2. Critic's next decision is forced to `refuse` with `reason=insufficient_macro_evidence`.
3. Final status becomes `INSUFFICIENT` with explicit reason in trace.
4. Eval `should_refuse` cases are designed to land here (Day 9 case authoring).

### H.5 RAG Precision Roadmap (P0 minimum, P1 upgrade path)

P0 keeps article-level chunking and hybrid retrieval as-is (`chunk_size=1024 chars`, overlap `256`, BM25+vector). To preserve evidence governance while preparing for precision upgrades:

1. **Sentence anchor (P0 minimal add):** each cited evidence entry may include optional `sentence_offset = {start_char:int, end_char:int}`.
2. **Validator scope (P0):** validator enforces `evidence_id` existence only; sentence offsets are captured for audit, not gate-enforced.
3. **Rerank-ready API (P0):** retrieval signature includes `rerank=None` keyword, always passed as `None` in P0.

Deferred to P1 under W-14:
- dual-granularity chunking (`doc_chunk` + `span_chunk`),
- two-stage retrieval (`recall -> rerank`),
- validator enforcement of sentence offsets.

---

## I. Eval & Gate Execution Plan

### I.1 10-Case Sample Definition (frozen on Day 9)

| Bucket | Count | Selection rule |
|---|---|---|
| sufficient | 5 | Pick from `golden_set/v1_2.jsonl` rows where the case has clear single-cause and existing corpus contains ≥4 directly-relevant articles. |
| partial | 2 | Pick rows where causes span both company-level and macro evidence — partial coverage by Layer 1 alone. |
| should_refuse | 3 | (a) future-dated (post corpus end); (b) ticker/entity outside corpus (private or non-listed acceptable); (c) deliberately ambiguous query. Source from `golden_set/v1_2.jsonl` where possible; **at most 1 of 3 may be authored fresh on Day 9 AM.** |

Final 10-case set committed to `packages/eval/golden_set/v1_2_p0_set.jsonl` on Day 9 (T-13a). The original 50-row file stays unchanged.

### I.2 Sample-Protocol Immutability (hard rule)

After T-13b commit on Day 9:
- `packages/eval/golden_set/v1_2_p0_set.jsonl` is **frozen**.
- `data/catalyst_eval_frozen.db` SHA-256 is captured in eval-run header.
- `data/lancedb_gold/eval_frozen/` SHA-256 of the directory tree is captured.
- Any change to any of these three is a P0 protocol violation. Cannot be modified to make Day 10 Gate pass — strategy spec §6.1 explicitly forbids this.

### I.3 Gate Computation Script

Path: `packages/eval/scripts/check_p0_gate.py` (created in T-14). Inputs: comparison-report JSON path. Behavior: parses `report.gates` block, asserts thresholds per §A.3, prints per-gate PASS/FAIL summary, exits 0 (all green) / 1 (any red) / 2 (script error).

### I.4 Day 10 Gate Decision Tree

```
All gates green?
├─ YES → Optionally proceed to G5 (eval extraction, P1) if buffer ≥ 1 day; otherwise hold P1.
└─ NO  → Freeze ALL P1 / P2 work immediately.
         Day 11 EOD recheck:
           ├─ Now green → Resume verification audit (T-15); no P1 work.
           └─ Still red → P0 ships with explicit failed-gate disclosure
                         in deck slide P7. Defense narrative shifts to
                         "what we measured and what we'd build next."
```

### I.5 Reproducibility Tier Mapping to Files & Commands

| Tier | What | Command |
|---|---|---|
| **A — strict equality** | `status`, `evidence_ids`, `next_action`, `magnitude_coverage` (rounded 2dp), gate values, model_ids, db_sha, code_sha, golden-set SHA | `diff` between `data/eval_reports/<frozen>_mcj_full.json` and `data/eval_reports/rerun_<ts>_mcj_full.json` projecting only the strict fields (T-15 verification block) |
| **B — bounded variance** | `cost_usd`, `latency_ms_p50/p95`, `tokens_in/out` | Logged in both runs but **not** diffed; defense cites frozen artifact values |
| **C — unconstrained** | `summary_md`, Critic `reasoning` text | Not compared. LLM nondeterminism on free text accepted. |

### I.6 Pre-Recorded Defense Artifact Inventory (Day 13 commit)

| Artifact | Path | Purpose |
|---|---|---|
| Frozen DB | `data/catalyst_eval_frozen.db` | Source of truth for all defense numbers |
| Frozen vector index | `data/lancedb_gold/eval_frozen/` | RAG corpus reproducibility |
| `direct_llm` run report | `data/eval_reports/<frozen_ts>_direct_llm.{md,json}` | Baseline numbers |
| `mcj_full` run report | `data/eval_reports/<frozen_ts>_mcj_full.{md,json}` | Catalyst numbers |
| Comparison report | `data/eval_reports/<frozen_ts>_comparison.{md,json}` | Side-by-side + gate values |
| Trace files | `data/traces/<run_id>.json` for 20 runs | Replayability proof |
| Audit log | `docs/testing/audit_<date>.md` | Verification record |
| Demo notebook (cleared + executed) | `notebooks/demo.ipynb`, `notebooks/demo_executed.ipynb` | Live walkthrough |
| Deck | `docs/defense/deck.md` | Presentation source |
| Preflight report | `data/eval_reports/preflight_<ts>.json` | Data prerequisite proof |
| Ingestion runs | `data/eval_reports/ingestion_<YYYYMMDD>.md` × 2+ | Operational discipline proof |
| Golden set (frozen) | `packages/eval/golden_set/v1_2_p0_set.jsonl` | Sample protocol |

### I.7 Display Contract (Notebook now, UI later)

P0 ships no frontend code. This section defines the display contract that the notebook uses now and UI can reuse later without schema breaks.

| Panel | Source table(s) | Primary key | Foreign keys | Display fields | Used in notebook (P0) |
|---|---|---|---|---|---|
| RunList | `agent_runs` | `run_id` | `case_id` (logical) | `run_id`, `case_id`, `config`, `status`, `started_at`, `total_cost_usd`, `total_latency_ms`, `model_id_per_role` | Yes |
| CaseDetail | `agent_runs` + frozen golden-set JSONL | `run_id` | `case_id` | `run_id`, `case_id`, `ticker`, `trade_date`, `price_move_pct`, `expected_status`, `observed_status`, `summary_md`, `causes[]` | Yes |
| EvidencePanel | `clean_assets` + LanceDB `gold_chunks` | `asset_id` | `chunk_id`, `canonical_asset_id` | `asset_id`, `ticker_primary`, `source`, `published_utc`, `title`, `body_md`, `sentence_offset` (NEW, T-08), `is_canonical`, `is_rag_eligible` | Yes |
| RetrievalTimeline | `trace_events` filtered on retrieval/critic/router nodes | `(run_id,event_seq)` | `run_id` | `event_seq`, `node`, `layer`, `hit_count`, `expansion_reason`, `decision`, `latency_ms` | Yes |
| OpsPanel | `ingestion_runs`, `source_checkpoints`, ingestion report markdown | `run_id` | `run_id` | `run_id`, `started_at`, `ticker_list`, `source_list`, `success_count`, `fail_count`, `cost_usd`, `fail_rate_per_source` | No (pointer to report files only) |

Every display field above must exist by end of T-11. Missing columns are added via migration before T-12.

### I.8 Deck P7 Canonical Shape (Done / Deferred / Waivers)

`docs/defense/deck.md` Slide 7 must keep this shape (T-14 draft, T-16 final fill):

- **Done in P0 (defense):** 4-state output enum, validator (4 checks), deterministic router, critic contract, retrieval L1+L2, trace persistence, frozen 10-case set, direct_llm vs mcj comparison + 5 gates, Tier-A pins (`model/db/lancedb/code sha`), frozen corpus/index/demo notebook.
- **Deferred to P1:** `rag_only` baseline (W-01), LangSmith (W-06), cross-source semantic dedup (W-11), RAG precision upgrades (W-14).
- **Deferred to P2/Roadmap:** Layer3 retrieval (W-02), Critic LLM grading (W-03), budget breaker (W-04), model routing (W-05), full eval matrix (W-07), expanded refusal precision/recall (W-08), GDELT integration (W-09), ADR-004~007.
- **Defense-window waivers:** W-01..W-09 and W-11..W-14 map one-to-one to strategy spec §11 and this plan §A.4.

---

## J. Git Branch & Delivery Governance

### J.1 Branch Naming

Format: `t<NN>-<short-noun>` (lowercase, hyphens). Examples: `t01-doc-sweep`, `t06-backfill-execution`, `t14-gate-script`.

### J.2 Branch Lifecycle

- **Target lifetime:** 1 day. **Hard upper bound:** 2 days.
- One branch per task T-NN. Sub-tasks (T-NN.M) land as commits on the same branch.
- A branch reaching 2 days without merging triggers a "split or abandon" review at end-of-day-2; no exceptions during this 14-day sprint.
- Merge target: `feat/full-v1` (current branch per `git status` snapshot).

### J.3 Daily Integration Cadence

- **EOD checkpoint each day:** `git status` clean, all task branches either merged or staged for review. User authorizes commits per §0.bis Commit-Authorization Protocol (referenced from v2 plan top section).
- **Day 5, Day 9, Day 13 are integration freezes:** all merge targets must be current; rebase/conflict resolution happens before EOD.

### J.4 Day 13 → Day 14 Freeze Whitelist

After T-16 creates the annotated tag `defense-freeze-YYYY-MM-DD`, only these paths may receive new commits:

```
^docs/.*\.md$
^docs/defense/.*$
^README\.md$    # only typo/clarity fixes; no scope changes
```

**Forbidden** between freeze tag and defense:
- Any path under `^packages/`.
- Any file under `^data/eval_reports/`, `^data/traces/`, `^data/.*\.db$`, `^data/lancedb_gold/`.
- `^notebooks/` (the executed notebook is part of the frozen artifact set).
- `^pyproject.toml`, `^uv.lock`, dependency files.

### J.5 Freeze Violation Detection

```bash
# Run on Day 14 morning before rehearsal.
TAG=$(git tag --list 'defense-freeze-*' | sort | tail -1)
test -n "$TAG" || { echo "FAIL: no freeze tag"; exit 1; }
VIOLATIONS=$(git log "$TAG"..HEAD --name-only --pretty=format: 2>/dev/null \
  | grep -v '^$' \
  | grep -vE '^(docs/.*\.md|docs/defense/.*|README\.md)$')
if [ -n "$VIOLATIONS" ]; then
  echo "FREEZE VIOLATION:"
  echo "$VIOLATIONS"
  exit 1
fi
echo "Freeze whitelist OK."
```

### J.6 Source-Freeze Invariant (T-13b → T-16)

Between the eval freeze anchor (`task/T-13b-close`, Day 9 EOD) and the defense freeze tag (T-16, Day 13 EOD), `packages/` must not be modified. The eval-run header records `code_git_sha`; the defense-freeze tag must point to the same SHA. Verified by §J.5 plus the SHA-equality check in T-16 verification.

---

## K. Day-Level Schedule + Critical Path

### K.1 14-Day Calendar

| Day | AM | PM | Deliverable |
|---|---|---|---|
| 1 | T-01 doc sweep (full day) | T-01 continues | All four anchors aligned to v3 strategy spec |
| 2 | T-02 DB triple split + path config | T-03 ingestion runs + checkpoints + quality flags (start) | DB layout finalized; data layer schema migrations begin |
| 3 | T-03 finishes | T-04 conditional fallback orchestration + cross-source dedup | Data layer hardening complete |
| 4 | T-05 provider rate-limiter audit re-run + key pool config | T-06 backfill execution starts (background, ~4.4h) | Backfill running |
| 5 | T-06 backfill finishes (early AM) | T-07 vector index rebuild + health check | Frozen-corpus candidate ready |
| 6 | T-08 Validator + 4-state output + Critic decision contract (start) | T-08 continues | Validator skeleton in graph |
| 7 | T-09 router + T-11 trace (parallel, both small) | T-10 retrieval Layer 1+2 wiring | Control plane wired; trace ready |
| 8 | T-12 direct_llm baseline + failure taxonomy | T-12 continues; T-13a prep | Both eval configs runnable |
| 9 | T-13a preflight + golden-set freeze + SHA capture | T-13b eval runs + threshold calibration | Eval freeze achieved |
| 10 | **T-14 Day 10 Gate** (decision before noon) | If GREEN: G5 evaluation. If RED: T-15 audit early. | Gate decision recorded |
| 11 | T-15 verification audit (full day) | T-15 continues | Tier-A reproducibility verified |
| 12 | T-15 audit AM | T-16 tech dry-run (notebook execution) | Demo validated offline |
| 13 | T-16 code freeze (early AM) + deck polish (full day) | T-16 deck polish continues | Annotated freeze tag created |
| 14 | T-16 defense rehearsal (≥2 end-to-end run-throughs) | Buffer | Ready |

### K.2 Critical Path

**T-01 → T-02 → T-03 → T-06 (backfill) → T-07 (vector build) → T-08 → (T-09 || T-11) → T-10 → T-12 → T-13a → T-13b (eval freeze) → T-14 (gate) → T-15 (audit) → T-16 (freeze).**

Hottest stretch: **Day 5 → Day 9** (T-07 vector → T-08 control-plane → T-13a/b freeze). Split T-13 and pulling T-11 earlier removes the prior 35h/24h overcommit risk in Days 6–8.

### K.3 Dependency Graph (text)

```
T-01 ──┐
       ├─► T-02 ──► T-03 ──► T-04 ──► T-06 ──► T-07 ──┐
                                                       │
                            T-05 (parallel to T-04) ──┘
                                                       ▼
                                                      T-08 ──┬─► T-09 ──► T-10 ──┐
                                                             └─► T-11 ───────────┘
                                                                                ▼
                                                                              T-12 ──► T-13a ──► T-13b ──► T-14 ──► T-15 ──► T-16
```

### K.4 Day 10 Gate Decision Tree (executable)

See §I.4 above. Run before noon Day 10. Decision recorded in `docs/testing/audit_<date>.md` Day-10-Gate section.

### K.5 Day 11–13 Verification & Freeze Window (mandatory shape)

- Day 11 + Day 12 AM: `verification-before-completion` discipline applied to every "done" claim from T-01 through T-14.
- Day 12 PM: tech dry-run only — no source code changes.
- Day 13: code freeze (annotated tag). Deck polish allowed; nothing under `packages/` or `data/` changes.

---

## L. Task Contracts (T-01 → T-16)

Every task: entry, exit, verification (executable), failure fallback, estimated work.

### T-01 Doc Sweep (Day 1) — 6 h

- **Entry:** Strategy spec r3 approved (already done).
- **Exit:**
  - `README.md` first 30 lines aligned to strategy spec §1.1–§1.3; verbatim phrase "legacy stock-attribution framing" removed.
  - `docs/current_situation.md` rewritten with midterm→defense→roadmap delta.
  - `packages/{data-core,eval,agents}/README.md` lead paragraphs aligned.
  - All non-anchor `*.md` under `docs/` carry `> Supporting reference. Canonical source: <path>` header within the first 5 lines.
- **Verification:**
  ```bash
  grep -ri "legacy stock-attribution framing" README.md docs/ packages/*/README.md
  # Expected: zero hits.

  find docs -type f -name "*.md" \
    ! -path "docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md" \
    ! -path "docs/full-version-execution-spec.md" \
    ! -path "docs/current_situation.md" \
    ! -path "docs/data-ingestion-operating-spec.md" \
    ! -path "docs/superpowers/specs/2026-04-02-catalyst-system-design.md" \
    ! -path "docs/superpowers/specs/2026-04-07-catalyst-midterm-freeze-boundary.md" \
    | xargs grep -L "Supporting reference"
  # Expected: empty.

  for f in packages/*/README.md; do
    grep -qE "(evidence-bounded|evidence validity|refusal|replayability|failure governance)" "$f" \
      || echo "MISSING: $f"
  done
  # Expected: no MISSING.
  ```
- **Failure fallback:** if a doc carries content that genuinely needs the deprecated phrase as a quotation, paraphrase it inline and add a footnote rather than restoring the verbatim phrase.

### T-02 DB Triple Split + Path Config (Day 2 AM) — 4 h

- **Entry:** T-01 complete.
- **Exit:**
  - `data/catalyst_dev.db`, `data/catalyst_eval_frozen.db`, `data/catalyst_demo.db` exist (eval/demo may be schema-only stubs).
  - Existing `data/dev_assets.db` either symlinked as `catalyst_dev.db` or schema-replicated (decision recorded in T-02 inspection note).
  - Env var `CATALYST_DB_PATH` (default `data/catalyst_dev.db`) wired into the existing `packages/data-core/catalyst_data/config.py`.
  - `data/eval_reports/`, `data/traces/`, `docs/testing/`, `notebooks/` exist (with `.gitkeep` if newly created).
- **Verification:**
  ```bash
  ls data/catalyst_dev.db data/catalyst_eval_frozen.db data/catalyst_demo.db
  ls data/eval_reports/ data/traces/ docs/testing/ notebooks/
  CATALYST_DB_PATH=/tmp/test_catalyst.db /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  from catalyst_data.config import db_path
  assert str(db_path()) == '/tmp/test_catalyst.db'"
  # Expected: silent success.
  ```
- **Failure fallback:** if `catalyst_data.config` does not currently expose a `db_path` callable, expose one as the minimum addition; document in inspection note.

### T-03 Ingestion Runs + Checkpoints + Quality Flags (Day 2 PM → Day 3 AM) — 8 h

- **Entry:** T-02 complete.
- **Exit:**
  - New tables created via migration script in all three DBs:
    - `ingestion_runs(run_id PK, started_at, ended_at, ticker_list_json, source_list_json, status, success_count, fail_count, cost_usd, notes)`.
    - `source_checkpoints(run_id, source_type, ticker, date, status, error_class, retries, PK(run_id, source_type, ticker, date))`.
    - `asset_quality_flags(asset_id PK, is_rag_eligible, quality_reason, quality_score, evaluated_at)`.
  - `is_rag_eligible` computed and persisted for every existing `clean_assets` row (per §F.4 logic).
  - Migration is idempotent (rerunning the script does not error on existing tables).
- **Verification:**
  ```bash
  /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  import sqlite3
  for db in ['data/catalyst_dev.db','data/catalyst_eval_frozen.db','data/catalyst_demo.db']:
      conn = sqlite3.connect(db)
      tables = {r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")}
      for t in ('ingestion_runs','source_checkpoints','asset_quality_flags'):
          assert t in tables, f'{db} missing {t}'
  print('All three tables present in all three DBs.')"

  # is_rag_eligible coverage on dev DB.
  /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  import sqlite3
  c = sqlite3.connect('data/catalyst_dev.db')
  total = c.execute('SELECT COUNT(*) FROM clean_assets').fetchone()[0]
  flagged = c.execute('SELECT COUNT(*) FROM asset_quality_flags').fetchone()[0]
  if total > 0:
      assert flagged / total >= 0.95, f'flag coverage {flagged}/{total}'
  print(f'flag coverage {flagged}/{total} OK')"
  ```
- **Failure fallback:** if migration breaks existing reads (FK or NOT NULL conflicts on legacy rows), wrap the new columns in a side table and link by `asset_id` rather than altering `clean_assets` directly.

### T-04 Conditional Fallback Orchestration + Cross-Source Dedup (Day 3 PM) — 6 h

- **Entry:** T-03 complete.
- **Exit:**
  - `orchestrator.py` extended with `should_trigger_fallback(ticker, date, primary_result)` per `data-ingestion-operating-spec.md §3.3` rules: empty primary AND |price_move| ≥ 3 %; OR primary connectivity failures ≥ N retries; OR primary all-text fails `is_rag_eligible`.
  - Cross-source URL canonicalization function added (per §F.2).
  - Canonical-row selection per §F.3 implemented in `dedup/` module.
  - Unit tests in `packages/data-core/tests/test_fallback_orchestration.py` and `test_cross_source_dedup.py` (≥4 tests each).
- **Verification:**
  ```bash
  ( cd packages/data-core && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_fallback_orchestration.py tests/test_cross_source_dedup.py -v )
  # Expected: ≥8 tests pass. Subshell `( ... )` keeps caller cwd unchanged (CLAUDE.md).
  ```
- **Failure fallback:** if cross-source dedup is too risky to ship in one day, ship URL-canonicalization + same-source dedup only; record `cross_source_semantic_dedup` as W-11 (deferred to P1).

### T-05 Provider Rate-Limiter Hardening + Key Pool Config (Day 4 AM) — 4 h

- **Entry:** T-04 complete; `rate_limiter.py` exists.
- **Exit:**
  - `rate_limiter.py` exposes per-provider token-bucket parameters (per §C); polygon=5/min, fmp=250/day, fred=120/min — hardcoded constants moved into a `provider_limits.py` config module.
  - `retry.py` policies parameterized per provider per §C.1–C.3 (separate handling for 429, 5xx, timeout).
  - `config.py` reads `POLYGON_API_KEY`, `POLYGON_API_KEY_BACKUP` (optional), `FMP_API_KEY`, `FRED_API_KEY` from env.
  - **`api_key_id` (sha256 prefix of the key) is recorded** to `ingestion_runs.notes` JSON on every run.
  - **Day-4 FMP re-audit** per §C.2: re-run `provider_audit.py` with `--sources fmp_fundamentals --tickers AAPL,NVDA,TSLA,MSFT,AMZN --days 5`. If new run_ok_rate < 0.85, demote fmp to fallback-only.
- **Verification:**
  ```bash
  ( cd packages/data-core && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m scripts.provider_audit \
      --tickers AAPL,NVDA,TSLA,MSFT,AMZN --days 5 --sources fmp_fundamentals --min-news-chars 200 )
  # Inspect new report at data/eval_reports/provider_audit_*.json — record run_ok_rate.

  /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  from catalyst_data.config import provider_limits
  assert provider_limits.POLYGON['rate_per_min'] == 5
  assert provider_limits.FMP['rate_per_day'] == 250
  assert provider_limits.FRED['rate_per_min'] == 120
  print('Limits OK')"
  ```
- **Failure fallback:** if FMP re-audit still shows < 0.85 ok-rate, demote and proceed without fundamentals (acceptable per `data-ingestion-operating-spec.md §3.3` fallback rules; fundamentals are not on the eval critical path).

### T-06 1-Year Backfill Execution (Day 4 PM → Day 5 AM, background) — 6 h

- **Entry:** T-05 complete; rate-limiter and key-pool active.
- **Exit:**
  - Backfill orchestrator run for 10 tickers × 365 days against `catalyst_eval_frozen.db` (eval-frozen file is written pre-content-freeze; freeze SHA marker is captured at T-13a/T-13b).
  - `ingestion_runs` table contains 1 row with status `success`; `source_checkpoints` populated with per (source, ticker, date) outcomes.
  - Article counts roughly within §B.2 P50 ± 30 % (≈ 27,500–51,000 news articles for 10 tickers; full-year).
- **Verification:**
  ```bash
  # 1. News article count (primary + conditional fallback news sources).
  /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  import sqlite3
  c = sqlite3.connect('data/catalyst_eval_frozen.db')
  n_news = c.execute(\"\"\"
      SELECT COUNT(*) FROM clean_assets
      WHERE source IN ('polygon_news','fmp_news','finnhub_company_news')
  \"\"\").fetchone()[0]
  assert 25000 <= n_news <= 60000, f'news count {n_news} outside expected [25000,60000]'
  print(f'news_count={n_news} OK')"

  # 2. OHLCV row count from dedicated table.
  /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  import sqlite3
  c = sqlite3.connect('data/catalyst_eval_frozen.db')
  n_ohlcv = c.execute('SELECT COUNT(*) FROM ohlcv').fetchone()[0]
  assert 2200 <= n_ohlcv <= 2800, f'ohlcv count {n_ohlcv} outside expected [2200,2800]'
  print(f'ohlcv_count={n_ohlcv} OK')"

  # 3. Checkpoint fail rate over non-empty table.
  /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  import sqlite3
  c = sqlite3.connect('data/catalyst_eval_frozen.db')
  total = c.execute('SELECT COUNT(*) FROM source_checkpoints').fetchone()[0]
  assert total > 0, 'source_checkpoints empty — backfill did not run'
  failed = c.execute(\"SELECT COUNT(*) FROM source_checkpoints WHERE status='failed'\").fetchone()[0]
  fail_rate = failed / total
  assert fail_rate < 0.05, f'checkpoint fail rate {fail_rate:.3f} >= 0.05'
  print(f'checkpoints={total} failed={failed} fail_rate={fail_rate:.3f} OK')"
  ```
- **Failure fallback:** if news count < 20,000 (significantly below P50 even at low tier_factor), investigate before T-07. If root cause is Polygon outage, accept partial coverage and document W-12 (reduced backfill window).

### T-07 Vector Index Rebuild (Day 5 PM) — 5 h

- **Entry:** T-06 complete; bge-m3 model present in `~/.cache/huggingface/hub/`.
- **Exit:**
  - `data/lancedb_gold/eval_frozen/` rebuilt from `catalyst_eval_frozen.db` rows where `is_rag_eligible=1 AND is_canonical=1`.
  - `gold_chunks` table exists with `chunk_id`, `asset_id`, `ticker_primary`, `text`, `embedding` (1024-dim), `published_utc`, `source_type` columns.
  - Health check (§G.4) and consistency check (§G.5) both pass.
  - Smoke retrieval: pick 3 known events from golden set, call `retrieve(query, Layer.DIRECT)`, get back ≥1 expected article per query.
- **Verification:**
  ```bash
  LANCEDB_PATH=data/lancedb_gold/eval_frozen CATALYST_DB_PATH=data/catalyst_eval_frozen.db \
    /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  import lancedb, sqlite3, os
  db = lancedb.connect(os.environ['LANCEDB_PATH'])
  t = db.open_table('gold_chunks')
  n = t.count_rows()
  assert n >= 5000, f'too few chunks: {n}'
  print(f'chunks: {n}')"

  # Run health + consistency scripts (commands in §G.4 / §G.5).
  ```
- **Failure fallback:** if model not in HF cache, run `huggingface-cli download BAAI/bge-m3` (no API quota cost; one-time ~2 GB). If chunk count < 5,000, T-06 likely under-delivered — return to T-06 to investigate.

### T-08 Validator + 4-State Output + Critic Decision Contract (Day 6 → Day 7 AM) — 12 h

- **Entry:** T-07 complete.
- **Exit (full per strategy spec §13 T-03):**
  - `state.py`: `OutputStatus(Enum)`, `Phase(Enum)`, `CriticDecision` dataclass.
  - `nodes/critic.py` modified to emit `CriticDecision` with threshold-based `sufficiency`.
  - Evidence schema extends each cited evidence entry with optional `sentence_offset = {start_char:int, end_char:int}` (audit field; not validator-gated in P0).
  - `nodes/validator.py` created with 4 checks per execution-spec §8: evidence-id existence, time-window, schema, magnitude sanity.
  - On validator failure: 1 regenerate; second fail → `PARTIAL` (citation/window/schema/magnitude) or `SYSTEM_ERROR` (model timeout).
  - Validator wired between Judge and Finalizer in `graph.py`.
  - Unit tests in `packages/agents/tests/test_validator.py` (≥4 tests).
- **Verification:**
  ```bash
  ( cd packages/agents && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_validator.py -v )
  # Expected: ≥4 tests pass.

  /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  from catalyst_agents.state import OutputStatus, CriticDecision
  assert {'SUFFICIENT','PARTIAL','INSUFFICIENT','SYSTEM_ERROR'} == {s.name for s in OutputStatus}
  print('Status enum OK')"
  ```
- **Failure fallback:** ship validator without auto-regenerate (single-pass) and record as runtime-spec deviation if regenerate cannot stabilize within 1 day.

### T-09 DecisionRouter (Day 7 PM) — 4 h

- **Entry:** T-08 complete.
- **Exit:**
  - `nodes/decision_router.py` created. Pure-function dispatcher (no LLM). Decision rules per §H + execution-spec §6 narrowed by W-02/W-04 (no Layer 3 emission, no budget branch).
  - `expand_related` short-circuits to `refuse` with `reason=layer3_not_implemented`.
  - Wired into `graph.py` between Critic and Judge/Refuse fan-out.
  - Unit tests in `packages/agents/tests/test_decision_router.py` (≥6 tests covering all decision branches).
- **Verification:**
  ```bash
  ( cd packages/agents && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_decision_router.py -v )
  # Expected: ≥6 tests pass.

  grep -E "(budget_overflow|partial_budget_flag|cost_cap|latency_cap)" packages/agents/catalyst_agents/nodes/decision_router.py
  # Expected: zero hits (W-04 compliance).
  ```
- **Failure fallback:** none — DecisionRouter is small and isolated; if blocked, halt and debug.

### T-10 Retrieval Policy Layer 1 + Layer 2 (Day 7 PM → Day 8 AM) — 6 h

- **Entry:** T-09 complete.
- **Exit:**
  - `packages/agents/catalyst_agents/retrieval/policy.py` implements `Layer.DIRECT` + `Layer.MACRO` per §H.1; `Layer.RELATED` raises `NotImplementedError`.
  - Retrieval signature is rerank-ready: `retrieve(query, layer, metadata, *, rerank=None)`. P0 always passes `rerank=None`.
  - `RetrievalMetadata` per §H.3.
  - Wired into RetrievalPolicy / Miner node so `expand_macro` from DecisionRouter triggers Layer 2.
  - `max_layers=2` effective (Layer 3 short-circuit), `max_expansions=2` enforced.
  - Fixture file `packages/agents/tests/fixtures/retrieval_fixture.py` provides a tiny in-memory corpus for tests.
  - Unit tests in `test_retrieval_policy.py` (≥4 tests).
- **Verification:**
  ```bash
  ( cd packages/agents && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_retrieval_policy.py -v )
  # Expected: ≥4 tests pass.
  ```
- **Failure fallback:** if Layer 2 retrieval against frozen DB returns sparse hits during smoke test, adjust query templates (broader source-type filters) before T-13a freeze capture.

### T-11 Trace Persistence (Day 7 AM, parallel to T-09) — 5 h

- **Entry:** T-08 schema stable (`state.py` contains `OutputStatus` + `CriticDecision`).
- **Exit:**
  - `packages/agents/catalyst_agents/trace/schema.py` defines SQLite DDL for `agent_runs` (run-level) and `trace_events` (node-level) per execution-spec §10.
  - `trace/writer.py` provides a context-manager API.
  - `trace/exporter.py` CLI: `python -m catalyst_agents.trace.exporter --run-id <id> --out <path>`.
  - Every node transition in `graph.py` writes a trace event.
  - Unit tests in `test_trace.py` (≥3 tests).
- **Verification:**
  ```bash
  ( cd packages/agents && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_trace.py -v )

  # Smoke export: query the latest run_id from the DB and export.
  LATEST_RUN=$(/Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  import sqlite3, os
  conn = sqlite3.connect(os.environ.get('CATALYST_DB_PATH','data/catalyst_dev.db'))
  row = conn.execute('SELECT run_id FROM agent_runs ORDER BY started_at DESC LIMIT 1').fetchone()
  print(row[0] if row else '')")
  test -n "$LATEST_RUN" || { echo "FAIL: no agent_runs row"; exit 1; }
  /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m catalyst_agents.trace.exporter --run-id "$LATEST_RUN" --out /tmp/trace.json
  /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  import json
  d = json.load(open('/tmp/trace.json'))
  assert 'trace_id' in d and len(d.get('events',[])) >= 6"
  ```
- **Failure fallback:** if SQLite write throughput is a problem (very unlikely at P0 scale), batch trace events per node-transition group; do not switch to a different storage backend in P0.

### T-12 direct_llm Baseline + Failure Taxonomy + 2 Regression Tests (Day 8 PM → Day 9 AM) — 8 h

- **Entry:** T-08 (output schema stable), T-11 (trace ready).
- **Runtime guard:** OD-6 cap active. Start with `claude-sonnet-4-6`; if projected full-run cost exceeds USD 20 after first 2 cases (`observed * 5 * 1.5`), downgrade to `claude-haiku-4-5` and record substitution in report header.
- **Exit:**
  - `packages/eval/catalyst_eval/harness/baselines/direct_llm.py` + `__init__.py`. Function `run_direct_llm(case, model_id, db_path) -> dict` returns same schema as `mcj_full`.
  - DB path is a parameter, never hardcoded (B-3 fix).
  - Cost/latency/tokens recorded per call (§B / §C).
  - `packages/eval/tests/test_direct_llm_baseline.py` smoke tests pass.
  - `docs/testing/failure-taxonomy.md` documents 5 classes (`retrieval_failure`, `model_failure`, `consistency_failure`, `budget_failure`, `provider_failure`) as `### <class_name>` H3 headings.
  - `packages/agents/tests/test_failure_taxonomy.py` has 2 regression tests with `# class:` tags.
- **Verification:**
  ```bash
  ( cd packages/eval && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_direct_llm_baseline.py -v )
  # Expected: smoke pass.

  for c in retrieval_failure model_failure consistency_failure budget_failure provider_failure; do
    grep -qE "^### ${c}\b" docs/testing/failure-taxonomy.md || { echo "MISSING H3: $c"; exit 1; }
  done

  ( cd packages/agents && /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -m pytest tests/test_failure_taxonomy.py -v )
  # Expected: ≥2 tests pass.

  grep -E "catalyst_eval_frozen\.db" packages/eval/catalyst_eval/harness/baselines/direct_llm.py
  # Expected: zero hits.
  ```
- **Failure fallback:** model default is `claude-sonnet-4-6` with cost cap USD 20 for the full 10-case direct run. After first 2 cases, if projected total (`observed_cost * 5 * 1.5 safety factor`) exceeds USD 20, downgrade to `claude-haiku-4-5` and record `direct_llm_model_substitution` in eval-report header.

### T-13a Pre-Eval Preflight + Golden-Set Freeze + SHA Capture (Day 9 AM) — 4 h

- **Entry:** T-10, T-11, T-12 complete.
- **Exit:**
  - `packages/eval/scripts/preflight.py` exits 0 against `catalyst_eval_frozen.db`.
  - Preflight JSON records `geo_corpus_tier=2` (mandatory in P0).
  - `packages/eval/golden_set/v1_2_p0_set.jsonl` finalized with `expected_status` and `should_refuse`.
  - Freeze marker captured: `db_sha256` + `lancedb_dir_sha256`.
  - User-authorized task-close commit creates lightweight tag `task/T-13a-close`.
- **Verification:**
  ```bash
  /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python packages/eval/scripts/preflight.py --db data/catalyst_eval_frozen.db
  # Expected exit: 0.

  ls data/eval_reports/preflight_*.json | tail -1 | xargs -I {} /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  import json
  d = json.load(open('{}'))
  assert d['pass'] is True
  assert d['geo_corpus_tier'] == 2
  assert d['checks']['macro_doc_count']['value'] >= 30
  assert d['checks']['direct_evidence_per_ticker_min']['value'] >= 5
  assert d['checks']['geo_tier2_doc_count']['value'] >= 20
  assert 'db_sha256' in d
  print('preflight OK: geo_corpus_tier=2')"

  git rev-parse --verify task/T-13a-close >/dev/null 2>&1 \
    && echo "task/T-13a-close OK: $(git rev-parse task/T-13a-close)" \
    || { echo "BLOCKER: task/T-13a-close missing — authorize commit and tag first"; exit 2; }
  ```
- **Failure fallback:** none. `geo_corpus_tier=2` is mandatory in P0; failing Tier-2 threshold is a blocker.

### T-13b Eval Runs + Threshold Calibration (Day 9 PM) — 6 h

- **Entry:** T-13a complete.
- **Exit:**
  - `direct_llm` and `mcj_full` runs complete on all 10 cases (~20 trace files written).
  - Critic thresholds (`K_sufficient`, `K_partial`, `M_threshold`) calibrated within OD-1 allowed window (±1 on K, ±0.1 on M).
  - Eval-run headers include Tier-A pinning fields.
  - User-authorized task-close commit creates lightweight tag `task/T-13b-close` (this tag is the source-freeze anchor for T-15/T-16/§J.6).
- **Verification:**
  ```bash
  TRACE_COUNT=$(ls data/traces/*.json 2>/dev/null | grep -v rerun_ | wc -l | tr -d ' ')
  test "$TRACE_COUNT" -ge 20 || { echo "FAIL: only $TRACE_COUNT trace files"; exit 1; }

  /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  import json, glob
  for f in glob.glob('data/eval_reports/*_direct_llm.json') + glob.glob('data/eval_reports/*_mcj_full.json'):
      h = json.load(open(f)).get('header', {})
      for k in ('model_id_per_role','db_sha256','code_git_sha','random_seed','lancedb_dir_sha256'):
          assert k in h, f'{f} missing {k}'
  print('Tier-A pinning OK')"

  git rev-parse --verify task/T-13b-close >/dev/null 2>&1 \
    && echo "task/T-13b-close OK: $(git rev-parse task/T-13b-close)" \
    || { echo "BLOCKER: task/T-13b-close missing — authorize commit and tag first"; exit 2; }
  ```
- **Failure fallback:** if calibration needs values outside OD-1 allowed window, pause and request user ruling; do not silently apply larger swings.

### T-14 Eval Comparison Report + Gate Script (Day 9 EOD → Day 10 AM) — 6 h

- **Entry:** T-13b complete; both eval runs landed.
- **Exit:**
  - `packages/eval/catalyst_eval/harness/runner.py` implements gate enforcement layer. If file does not exist from midterm, create at this exact path.
  - `packages/eval/catalyst_eval/reports/markdown_writer.py` and `json_writer.py` produce the comparison report.
  - `data/eval_reports/<frozen_ts>_comparison.{md,json}` written with `schema_version="1.0"`, `gates`, `quality_metrics`, `cost_latency`, and `header` blocks. `header` includes `case_distribution = {sufficient: 5, partial: 2, should_refuse: 3}` and `geo_corpus_tier=2`.
  - `packages/eval/scripts/check_p0_gate.py` created — exits 0 (green) / 1 (red) / 2 (script error).
  - Deck draft `docs/defense/deck.md` with 8 slides (`## Slide N: <Title>` format) + Appendix A9.
- **Verification:**
  ```bash
  ls data/eval_reports/*_comparison.md data/eval_reports/*_comparison.json

  /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  import json, glob
  d = json.load(open(sorted(glob.glob('data/eval_reports/*_comparison.json'))[-1]))
  for g in ('evidence_validity','schema_validity','trace_completeness','should_refuse_hit_rate'):
      assert g in d['gates']
  print('Gate keys OK:', list(d['gates'].keys()))"

  test -f packages/eval/scripts/check_p0_gate.py
  /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python packages/eval/scripts/check_p0_gate.py "$(ls -t data/eval_reports/*_comparison.json | head -1)"
  # Expected: exit 0 (GREEN) or 1 (RED), prints summary.

  SLIDES=$(grep -cE "^## Slide [0-9]+:" docs/defense/deck.md)
  test "$SLIDES" -ge 8 || { echo "FAIL: only $SLIDES slides"; exit 1; }
  ```
- **Failure fallback:** if gate script returns 1 (RED), do **not** modify the sample to make it pass — proceed to §I.4 Day 10 Gate decision tree.

### T-15 Day 10 Gate + Verification Audit (Day 10 → Day 12 AM) — 16 h spread

- **Entry:** T-14 complete.
- **Exit:**
  - **Day 10:** `check_p0_gate.py` run; decision (GREEN / RED-RECOVERABLE / RED-DISCLOSE) recorded in `docs/testing/audit_<date>.md` Day-10-Gate section.
  - **Day 11–12 AM:** every T-01 through T-14 verification command re-run; outcome recorded per task in audit log.
  - Tier-A reproducibility rerun: `mcj_full` against frozen DB, output to `data/eval_reports/rerun_<ts>_mcj_full.{md,json}`, traces to `data/traces/rerun_<run_id>.json`. Diff matches per §I.5 Tier A.
  - **§J.6 source-freeze invariant holds**: no `packages/` changes since T-13b commit (verified by `git log $T13B_COMMIT..HEAD --name-only`).
- **Verification:**
  ```bash
  AUDIT=$(ls docs/testing/audit_*.md | sort | tail -1)
  ROWS=$(grep -c "^| T-" "$AUDIT")
  test "$ROWS" -ge 14 || { echo "FAIL: $ROWS audit rows"; exit 1; }

  ls data/eval_reports/rerun_*_mcj_full.json | head -1

  # Frozen DB SHA unchanged.
  /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  import json, hashlib, glob
  r = json.load(open(sorted(glob.glob('data/eval_reports/*_comparison.json'))[-1]))
  freeze_sha = r['header']['db_sha256']
  current = hashlib.sha256(open('data/catalyst_eval_frozen.db','rb').read()).hexdigest()
  assert freeze_sha == current"

  # Frozen schema versions must remain 1.0 in defense window.
  /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  import json, glob
  c = json.load(open(sorted(glob.glob('data/eval_reports/*_comparison.json'))[-1]))
  p = json.load(open(sorted(glob.glob('data/eval_reports/preflight_*.json'))[-1]))
  assert c.get('schema_version') == '1.0'
  assert p.get('schema_version') == '1.0'
  print('artifact schema_version=1.0 OK')"

  # Source freeze holds since T-13b. Use the lightweight tag created at T-13b close (§0.bis).
  if ! git rev-parse --verify task/T-13b-close >/dev/null 2>&1; then
    echo "BLOCKER: tag task/T-13b-close missing — user must authorize T-13b commit + tag before §J.6 can run"
    exit 2
  fi
  T13B_COMMIT=$(git rev-parse task/T-13b-close)
  VIOLATIONS=$(git log "$T13B_COMMIT"..HEAD --name-only --pretty=format: 2>/dev/null \
    | grep -v '^$' \
    | grep -E '^(packages/.*|data/catalyst_eval_frozen\.db|data/lancedb_gold/eval_frozen/.*|packages/eval/golden_set/v1_2_p0_set\.jsonl)$')
  test -z "$VIOLATIONS" || { echo "§J.6 VIOLATION: $VIOLATIONS"; exit 1; }
  ```
- **Failure fallback:** if Tier-A rerun shows divergence, isolate the offending field (likely `model_id` drift); re-pin and rerun once; if still divergent, document as known nondeterminism in deck P7 and proceed.

### T-16 Tech Dry-Run + Code Freeze + Deck Polish + Defense Rehearsal (Day 12 PM → Day 14) — 10 h spread

- **Entry:** T-15 complete.
- **Exit:**
  - **Day 12 PM:** `notebooks/demo.ipynb` authored; `notebooks/demo_executed.ipynb` produced via `jupyter nbconvert --execute`. No live API calls.
  - **Day 13:** annotated tag `defense-freeze-YYYY-MM-DD` created at HEAD; tag commit SHA equals eval-frozen `code_git_sha` (per §J.6).
  - **Day 13:** deck final; placeholders replaced with real screenshots from frozen artifacts.
  - **Day 14:** ≥2 end-to-end rehearsals run.
- **Verification:**
  ```bash
  # 1. Notebook executes offline.
  jupyter nbconvert --to notebook --execute notebooks/demo.ipynb --output /tmp/demo_check.ipynb
  # Expected: no errors.

  # 2. No live API references in notebook source.
  grep -E "(api\.openai|api\.anthropic|claude\.ai)" notebooks/demo.ipynb
  # Expected: empty.

  # 3. Annotated freeze tag exists.
  TAG=$(git tag --list 'defense-freeze-*' | sort | tail -1)
  test -n "$TAG" || { echo "FAIL: no freeze tag"; exit 1; }
  test "$(git for-each-ref refs/tags/$TAG --format='%(objecttype)')" = "tag" \
    || { echo "FAIL: $TAG is lightweight, not annotated"; exit 1; }

  # 4. Tag SHA equals the eval-frozen code SHA (no source drift since T-13b freeze anchor).
  #    Resolve in shell first, then pass into Python via env var — avoids f-string / shell-var escaping bugs.
  TAG_SHA=$(git rev-parse "${TAG}^{commit}")
  TAG_SHA="$TAG_SHA" /Users/yiannischen/Desktop/Catalyst/packages/data-core/.venv/bin/python -c "
  import json, glob, os
  r = json.load(open(sorted(glob.glob('data/eval_reports/*_comparison.json'))[-1]))
  eval_sha = r['header']['code_git_sha']
  tag_sha = os.environ['TAG_SHA']
  assert eval_sha == tag_sha, f'§J.6 violation: eval={eval_sha[:8]} tag={tag_sha[:8]}'
  print(f'SHA match OK: {eval_sha[:8]}')"

  # 5. Day 14 freeze whitelist holds. Inline; no nested bash -c.
  VIOLATIONS=$(git log "${TAG}..HEAD" --name-only --pretty=format: 2>/dev/null \
    | grep -v '^$' \
    | grep -vE '^(docs/.*\.md|docs/defense/.*|README\.md)$')
  if [ -n "$VIOLATIONS" ]; then
    echo "FREEZE VIOLATION:"
    echo "$VIOLATIONS"
    exit 1
  fi
  echo "Freeze whitelist OK."
  ```
- **Failure fallback:** if notebook fails to execute offline, swap to a static markdown walkthrough at `docs/defense/walkthrough.md` referencing frozen artifacts; document as W-13 (notebook deferred).

---

## M. Open Decisions (Resolved in v3.2)

| OD | Final ruling | Codified in |
|---|---|---|
| OD-1 Critic thresholds | Initial `K_sufficient=4`, `K_partial=2`, `M_threshold=0.6`. T-13b calibration may adjust `K_* ±1` and `M ±0.1` without re-approval; larger changes require user ruling + deck footnote. | §H.2, T-13b |
| OD-2 Should-refuse cases | 1 future-dated, 1 out-of-corpus ticker/entity, 1 ambiguous query. Prefer `v1_2.jsonl`; at most one fresh-authored case on Day 9 AM. | §I.1, T-13a |
| OD-3 FRED series | Confirmed: FEDFUNDS, CPIAUCSL, UNRATE, DGS10, DGS2, DFF, DEXUSEU, VIXCLS, BAA10Y, T10YIE. | §B.5 |
| OD-4 Deck format/path | `docs/defense/deck.md` using `## Slide N: <Title>`, at least 8 slides + appendix A9. | §I.6, §I.8, T-14 |
| OD-5 Talk length | 15 minutes, 8 slides + appendix. | §M, T-16 |
| OD-6 Direct-LLM model + cap | Default `claude-sonnet-4-6`, cap USD 20 for 10-case run. If projected total after first 2 cases (x5 * 1.5 safety factor) exceeds $20, downgrade to `claude-haiku-4-5` and log `direct_llm_model_substitution` in report header. | T-12, §O.2 |
| OD-7 FMP demotion | If Day-4 re-audit run_ok_rate < 0.85, demote FMP to fallback-only and proceed without fundamentals on eval critical path. | §C.2, T-05 |
| OD-8 Backup Polygon key | No backup key at start. Add only on incident (`run_ok_rate<0.5` over rolling 20 calls OR sustained 429 backoff >5 min during T-06). | §D.3, T-05/T-06 |

Operational (runtime) decisions still exist by design:
- OD-1 calibration window application at T-13b.
- OD-6 downgrade trigger evaluation in T-12.
- OD-7 demotion trigger evaluation in T-05.
- OD-8 incident trigger evaluation in T-06.

---

## O. Frozen Schemas (v1)

All defense artifacts below carry `schema_version: "1.0"` and are frozen at T-13b.

Backward-compatibility rule (defense window): additive-only changes are allowed; rename/removal/type-change/enum-removal are forbidden. Any breaking change requires `schema_version: "2.0"` and is out of P0 scope.

### O.1 `trace_events` SQLite table (v1)

Required fields:
- `schema_version` TEXT (`"1.0"`)
- `run_id` TEXT, `event_seq` INTEGER, `trace_id` TEXT
- `node` TEXT (`parser|retrieval|miner|critic|decision_router|judge|expand_macro|refuse|validator|finalizer`)
- `started_at` INTEGER, `ended_at` INTEGER, `latency_ms` INTEGER

Nullable fields:
- `model_id`, `input_tokens`, `output_tokens`, `cost_usd`
- `decision`, `error_type`, `error_message`
- `status_before`, `status_after`
- `payload_json` (node-specific extras, including retrieval metadata slices)

### O.2 `<frozen_ts>_comparison.json` (v1)

Top-level required keys:
- `schema_version`
- `header`
- `gates`
- `quality_metrics`
- `cost_latency`
- `per_case`

`header` required keys:
- `frozen_ts`
- `model_id_per_role`
- `provider_version`
- `db_sha256`
- `lancedb_dir_sha256`
- `code_git_sha`
- `random_seed`
- `case_distribution` (`5/2/3`)
- `geo_corpus_tier` (must be `2` in P0)

`gates` required keys:
- `evidence_validity`
- `schema_validity`
- `trace_completeness`
- `should_refuse_hit_rate`
- `cost_latency_reported`

OD-6 note: if direct baseline downgrades model, set `header.direct_llm_model_substitution`.

### O.3 `preflight_<ts>.json` (v1)

Required keys:
- `schema_version`
- `ts`
- `db_path`
- `db_sha256`
- `geo_corpus_tier` (must be `2`)
- `checks`
- `pass`

`checks` required keys:
- `macro_doc_count` (threshold ≥ 30)
- `direct_evidence_per_ticker_min` (threshold ≥ 5)
- `geo_tier2_doc_count` (threshold ≥ 20)

T-15 audit requirement: verify `schema_version == "1.0"` for all frozen artifacts.

---

## N. Self-Review

Conducted on plan v3.2 immediately after writing.

### N.1 P1 / P2 contamination check

| Waiver (from §A.4) | Plan compliance |
|---|---|
| W-01 rag_only → P1 | ✅ T-12 implements only direct_llm; T-14 comparison only direct vs mcj |
| W-02 Layer 3 → P2 | ✅ T-09 router short-circuits; T-10 Layer 3 raises NotImplementedError |
| W-03 Critic LLM-graded → P2 | ✅ T-08 + §H.2 use thresholds only |
| W-04 Budget circuit breaker → P2 | ✅ T-09 has explicit grep test forbidding budget keywords; T-09 rules describe record-only cost reporting |
| W-05 Model routing → P2 | ✅ Single model throughout; OD-6 picks one model |
| W-06 LangSmith → P1 | ✅ T-11 local SQLite only |
| W-07 Full eval matrix → roadmap | ✅ 10-case set throughout |
| W-08 should_refuse hit rate substitution | ✅ §A.3 + §I.3 use hit rate |
| W-09 GDELT deferred | ✅ §B.6 + §C.4 enforce P0 `geo_corpus_tier=2`; GDELT is P1/roadmap only |
| W-11 cross-source semantic dedup (conditional) | ✅ Dormant by default; T-04 fallback path is the trigger; if activated, audit log records it |
| W-12 backfill window reduction (conditional) | ✅ Dormant by default; T-06 fallback if news < 20K |
| W-13 demo notebook → static walkthrough (conditional) | ✅ Dormant by default; T-16 fallback only |
| W-14 two-level chunking + reranker deferred | ✅ P0 keeps article-level chunking + hybrid retrieval; upgrade path documented in §H.5 |

**Result: clean. No P1/P2 leakage detected.** Conditional waivers (W-11/12/13/14) do not affect scope unless their trigger fires.

### N.2 Verification command runnability

Spot-checked each verification block:

- ✅ POSIX `find`, `grep`, `xargs`, `wc`, `sort`, `head`, `tail` — standard.
- ✅ Python one-liners using `sqlite3`, `json`, `hashlib`, `glob` — stdlib only.
- ✅ `pytest` invocations follow the existing `verify_local_pytest.sh` venv path convention.
- ✅ `jupyter nbconvert --execute` — assumed available in dev venv (declared in pyproject most likely; if not, T-16 fails fast with clear error).
- ✅ `git` commands assume commits authorized at T-13a / T-13b / T-16 task-close per §0.bis.
- ⚠ Templates `<frozen_ts>` resolve at runtime via `$(ls -t data/eval_reports/*_comparison.json | head -1)`. Task-commit references resolve via `git rev-parse task/T-NN-close` (lightweight tag from §0.bis), not by grepping commit messages. Acceptable.

### N.3 Conflicts with strategy spec r3 / execution spec

- ✅ Strategy spec §11 W-01 through W-09 honored at intent level, with explicit P0 narrowing documented in this plan.
- ✅ No W-10 deviation: 10 tickers is the in-spec lower bound of `data-ingestion-operating-spec.md §4.1` ("10–30 tickers").
- ✅ DEV-01 declared: strategy freeze timing shifted Day 8 → Day 9 EOD due to data-first ordering (§A.6).
- ✅ Strategy spec §4.1 Tier-A pinning fields — `model_id`, `db_sha256`, `code_git_sha`, `random_seed` — all populated in T-13b exit; T-15 cross-verifies; T-16 tag-SHA equality check enforces.
- ✅ Strategy spec §6.1 Day-10-Gate decision tree — §I.4 mirrors exactly, including immutability rule.
- ✅ Strategy spec §8 Engineering Hard Rules — schema freeze (T-13b onward), DB boundary rules (§G.1), artifact commit rules (§I.6), branch discipline (§J.2) — all present.
- ✅ Execution spec §5 retrieval — Layer 1 + 2 implemented; Layer 3 declared per W-02 (audit-visible).
- ✅ Execution spec §6 router rules — P0 implements a strict subset (no `expand_related` emission, no budget-overflow branch). P2 adds those branches as a backward-compatible superset.
- ✅ Execution spec §7 output states — all four states.
- ✅ Execution spec §8 validator — 4 checks per T-08 exit.
- ✅ Execution spec §9 failure taxonomy — 5 classes documented per T-12.
- ✅ Execution spec §10 trace fields — all required fields per T-11 schema.
- ✅ Data-ingestion-spec §3.2 Primary + Fallback — implemented in T-04.
- ✅ Data-ingestion-spec §3.3 fallback triggers — encoded in T-04 logic.
- ✅ Data-ingestion-spec §6.1 quality thresholds — T-03 implements, configurable.
- ✅ Data-ingestion-spec §9 key governance — §D restates and enforces.

**No soft divergence on ticker scope:** 10 is the in-spec lower bound (`data-ingestion-operating-spec.md §4.1` says "10–30"). Expanding to 30 is post-defense roadmap; not a deviation, no waiver needed.

### N.4 Blocking and Non-Blocking Issues from Self-Review

**Blocking issues found inline and patched:** none. The plan was constructed against the v2 strict-review checklist and inherits its fixes (B-1 through B-10).

**Non-blocking issues (acknowledged, not blockers):**

- **NB-1.** `data-ingestion-operating-spec.md §11` allocates 2 weeks for the data layer alone; v3 compresses data layer to Days 2–5. Risk: any deep data-layer issue (cross-source dedup edge cases, FMP instability) eats into agent time. Mitigation: T-04 fallback to URL-canon-only dedup; T-05 fmp demotion path. **Accepted risk.**
- **NB-2.** Polygon news 6.3 s avg latency may include retries from the audit run; real fresh-cache latency is likely 2–4 s. Backfill wall-clock estimate (§C.6) uses 6.3 s baseline, which is conservative. Could finish earlier; doesn't affect correctness.
- **NB-3.** §B.6 GDELT estimate is heuristic — no audit data. Tier 2 fallback is the safety valve; estimate accuracy doesn't gate any task.
- **NB-4.** §C.5 single-key sufficiency assumes Polygon free tier is 5 req/min. If the project actually has a paid tier with higher quota, backfill finishes faster and §C.6 estimate is overly conservative; this only helps.
- **NB-5.** OD-3 macro-series list is now resolved and fixed in §B.5 / §M.

### N.5 Plan Operability Check

- New engineer cold-read viability: **passes for someone with Python+SQLite+LanceDB familiarity.** All commands include the venv path. All file paths are explicit. All tasks have entry/exit/verification.
- "Did today move a gate forward?" answerability: each task's verification block is a binary pass/fail; daily standup question is `which T-NN passed today, which is blocked`.
- "Did today introduce risk?" answerability: §A.2 RT-1..RT-5 are the watchlist; check each at EOD.

---

**End of Plan v3.2.** OD items are resolved in §M; only runtime trigger checks remain during execution.

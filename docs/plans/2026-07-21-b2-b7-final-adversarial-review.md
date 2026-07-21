# B2–B7 Final Adversarial Review

- Reviewer role: adversarial architecture / systems-design reviewer (design review and direction only; no production code)
- Date: 2026-07-21
- Subjects: `2026-07-21-b2-b7-technical-contracts.md` + the six execution plans `2026-07-22-b2…b7`
- Discipline: read-only inspection; one new review document only; no staging, commits, provider calls, secret inspection, or DB mutation; canonical DBs opened `mode=ro` only
- Verified state: branch `ws4b/article-level-data` at `4d4e3a9`; Git index empty; Dev DB `data/catalyst_dev_ws4b.db` SHA `92731fb7c5c3…`; frozen `data/catalyst_eval_frozen_v2.db` SHA `0d97a7ec61b6…`
- All four suites independently rerun this session with `.venv/bin/python -m pytest packages/<pkg> -q`

---

## A. Executive verdict

**APPROVE WITH REQUIRED PLAN AMENDMENTS.**

The B2→B7 architecture is coherent, correctly scoped for one developer, and grounded in the real repository: every current-state claim I spot-checked against code was accurate (§B, §E–J). The contract's identities, state machines, cutoff discipline, and package boundaries are sound and genuinely non-toy. The required amendments are **executable-defect and ownership fixes in the plans, not architecture reopenings**. Two are P0 (block the specific package's TDD from running as written); the rest are P1/P2 and block only their own package, not B2. No positioning, package-boundary, or workflow decision needs reopening.

The single most important finding: the plans' own discipline rule — "tests are executable rather than pseudocode" — is violated in several plans (B2 Task 9 undefined `db` + sync/async `execute_update` collision; B2 Task 7 `fresh_db_at_version` typo; B3 tokenizer test contradicts the pinned revision; B4 6-value inserts into an 18-column table). These are cheap to fix but must be fixed before the affected package executes, or the executor will "fix" them ad hoc and drift from the contract.

## B. Independently verified repository state

| Fact | Plan claim | Verified? |
|---|---|---|
| Migration registry v1–v7; Dev DB `user_version=6` | B2 contract §3 | ✅ 7 migrations registered; `PRAGMA user_version=6` (read-only) |
| `PlanDriftError` does not exist | B2 | ✅ zero matches outside tests |
| Two-stage OHLCV-first executor absent | B2 | ✅ no `RUNNING_OHLCV`/`RUNNING_EVIDENCE` anywhere |
| `raw_assets` uses `INSERT OR REPLACE` | B2 | ✅ `update_pipeline.py:131,190` |
| `index_builder.py` uses sentence splitter | B3 | ✅ `_split_sentences`, L2 sentence chunks |
| `corpus/` and `ingestion/` dirs absent | B2/B3 | ✅ neither exists; `dedup/{cross_source,hard}.py` present |
| `trading_calendar.py` lacks session-close/early-close/zoneinfo | B4 | ✅ only trading-day logic; no close times |
| `catalyst_eval/benchmark/` absent | B4 | ✅ absent; existing: schema/{golden_event,result}, adapters, harness, metrics, reports |
| FTS5 absent from data-core | B4 | ✅ zero matches |
| `lancedb_store.py` has RRF (k=60) + reranker | B6 | ✅ `reciprocal_rank_fusion`, `RRF_K=60`, `_apply_reranker`, `load_reranker` |
| `attribution/` and `runtime/assurance/` absent in agents | B5 | ✅ neither exists |
| `adapter.py` already moved out of agents; agents does not import eval | B5 | ✅ no `adapter.py` in agents; zero `catalyst_eval` imports; adapter lives at `catalyst_eval/adapters/agents.py` |
| `OutputStatus.INSUFFICIENT` is current; `ABSTAIN` absent | B5 | ✅ `state.py:27` + 8 call sites use INSUFFICIENT |
| App routers = workbench + live_runs only | B7 | ✅ no chart/attribution/trace/data/security routers |

**Test baselines (reran this session):**

| Package | Result | Exit |
|---|---|---|
| data-core | 758 passed, 1 skipped, 1 xfailed (88 s) | 0 |
| agents | 237 passed | 0 |
| eval | 93 passed | 0 |
| **app** | **128 passed, 0 failed** | 0 |

**Baseline correction (required):** the task brief's B7 note "app: 114 passed, 15 failed" and "prior report 115/14 is not current" are **both stale**. The app suite is fully green at **128 passed, 0 failed**, matching the brief's own header figure and every plan's "was 128" evidence template. The plans' baseline numbers (237/93/128) are correct; use them.

## C. Overall architecture assessment

The seven-document structure (one contract + six sequenced plans) is the right shape. Assessment against the positioning goals:

- **Logically coherent:** yes. The B2→B7 dependency chain is real: provenance (B2) → corpus/chunks (B3) → lexical retrieval + eval scaffold (B4) → workflow/assurance (B5) → dense/rerank/pool (B6) → API/metrics/release (B7). Migration ownership v8→v9→v10 is cleanly partitioned and matches the registry.
- **Technically implementable in this repo:** yes. Every "extract/adapt" claim (RRF/reranker from `lancedb_store.py`, planner/plan_hash from `update_planner.py`) is grounded in code that exists.
- **One-developer scoped:** yes, given the B7-H human-labeling pause is honored. The heaviest package is B5 (agents workflow) and B7-A (API + metrics); both are decomposed sensibly.
- **Demonstrates a non-toy system:** yes — request-scoped provenance, append-only raw with triggers, cutoff parity via injected policy, deterministic Context Builder, per-run assurance, zero-network replay. These are real invariants a wrapper cannot fake.
- **Admissions/interview evidence:** strong. The reviewer journey (§K) is exactly an interview walkthrough.
- **Free of theater:** mostly. See §L — the migration/table count is justified; the one place to watch is artifact/manifest proliferation (CorpusManifest, IndexManifest, PoolManifest, ResultPack, RunAssuranceRecord, trace-version registry) — each is load-bearing, but the **pool** artifact has a genuine ownership defect (§O P1-1).

**Missing load-bearing component:** none that blocks Core. The peer/relationship edge manifest (consumed by B5 gates) is referenced but its *authoring/format* is under-specified in B5 (§H). Not a blocker for B2; must be pinned before B5.

**Too-ambitious for positioning:** nothing egregious. B6's full GPU build is correctly fenced behind operator authorization with Mac-fixture fallback.

**Too-weak / still toy-like:** none. The 12-case benchmark is deliberately small; §J confirms it is adequate for the *bounded* claims the contract permits (named-case keep/kill, no generalized accuracy).

## D. Package-boundary and dependency review

Contract invariant (§2.1): `catalyst-agents` may depend on `catalyst-data`, never on `catalyst-eval`; `catalyst-eval` consumes artifacts. Verified clean today (no eval import in agents; adapter relocated).

**One real boundary defect (P1):** B6 places the **union judgment pool generator** in `catalyst_data/retrieval/pool.py` and its implement-step prose says it returns `PoolManifest`, but `PoolManifest` is created by B4 in `catalyst_eval/benchmark/pool_manifest.py`. If data-core imports the eval type, that is a `data-core → eval` dependency — a direction that does not exist anywhere today and inverts the artifact-consumer rule. Compounding it, the two shapes differ: B4 `PoolManifest` has `pool_id/arms/chunk_inventory/created_at`; B6's object is asserted to have `chunk_ids/per_arm_chunk_ids/corpus_manifest_id`. These are two different concepts sharing a name. Resolution in §O.

**Cutoff parity (well-designed):** B4 §6.2 and B5 landmine 8 correctly require parity to be proven by injecting **one** recording `ExchangeCutoffPolicy` into Miner and Validator and asserting identical arguments/values — not by calling a function twice. This is the right anti-tautology construction and should be preserved verbatim.

**Agents-opens-SQLite nuance (P2):** `catalyst_agents/retrieval/policy.py` currently does `import sqlite3` and uses `default_db_path` — i.e., agents opens the corpus DB directly today. B5 asserts "agents must not open corpus SQLite directly" and routes context through the injected `ContextProvider`, but B5's modify-allowlist covers `nodes/miner.py` and not `retrieval/policy.py`. Either the Miner stops calling the direct-SQLite policy in favor of an injected `Retriever`, or `retrieval/policy.py` must be in the B5 allowlist. Flag before B5, not B2.

## E. B2 review — Data update and provenance

**Verdict: strongest plan; two P0 executable defects in the test snippets.**

Current-state table is accurate. Migration v8 scope, request/raw identity, append-only triggers, redaction-before-log ordering, two-stage re-plan, and durable run control are all correctly specified and match the contract.

**P0-A (blocks B2 TDD as written):** `execute_update` is called **synchronously** in Task 2 (`with pytest.raises(PlanDriftError): execute_update(plan)`, line 375) and **asynchronously** in Task 9/10 (`report = await execute_update(plan)`, lines 926–1038). One symbol cannot be both. Additionally the Task 9/10 async tests are not marked `async def` and query a module-level `db` that is never assigned in the test body (they build `plan` and `report` only). As written these tests error at collection/first-line, not "fail then pass."

**P0-B (blocks the pagination task):** Task 7 line 794 `db = fresh_db_at_version(8)` — missing leading underscore; every other call uses `_fresh_db_at_version`. Undefined-helper `NameError` in `test_pagination_cursor_loop_detected`.

**P2:** Task 10 `test_full_provenance_chain_two_articles` asserts `raw_count >= 1`, `article_count >= 2`, `prov_count >= 2` — the plan's own rule bans `>=` against integration counts ("exact IDs/counts, never `>=`"). Use the isolated-DB exact-count form already demonstrated in `test_one_polygon_response_n_articles` (B3 Task 10).

Landmine set is genuinely non-tautological (transactional rollback test #8; per-field plan-hash parametrization + one hand-computed SHA #9). Keep them.

## F. B3 review — Corpus and chunking

**Verdict: correct design; one P1 contract contradiction.**

**P1 (blocks B3 tokenizer task):** `test_tokenizer_is_pinned_revision` asserts `"BAAI/bge-m3" in TOKENIZER_REVISION`, and `test_tokenizer_revision_in_manifest` asserts `TOKENIZER_REVISION.count("/") >= 1` and `"latest" not in …`. But the contract (§5) and B3 Task 0 pin `TOKENIZER_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"` — a bare 40-hex commit SHA with **no slash and no model name**. The two assertions can never pass with the mandated value; they conflate *model id* (`BAAI/bge-m3`) with *revision* (commit hash). Note B6's `test_bge_m3_revision_is_pinned` correctly asserts `re.fullmatch(r"[0-9a-f]{40}", …)` — so B3 and B6 disagree on the constant's format. Fix: split into `TOKENIZER_MODEL_ID = "BAAI/bge-m3"` and `TOKENIZER_REVISION = "5617a9f…"`; assert the model id contains the slash and the revision matches `^[0-9a-f]{40}$`.

Everything else is sound: `news_v2` 384/320/48 with title-prefix-in-budget and no whole-parent-with-children; `filing_v2` section-first with degraded `section_key=unknown` fallback; content/metadata hash separation; dedup representative vs `cluster_first_available_at` separation (landmine #3 proves representative change ≠ novelty change — excellent); atomic manifest publication with injected mid-reconciliation failure (Task 9 test #3). The exclusion landmines (OHLCV/FRED/FMP/SEC-submissions → zero chunks) are correctly behavioral.

**P2:** `test_reconcile_handles_all_failure_modes` (Task 8) and `test_source_classifier`/others reference helpers (`all_nine_case_inventory`, `expected_embed_ids`, `seeded_reconciliation_db`) that Task 0 must define in `corpus_fixtures.py`; the plan says so but the "all 9 cases" oracle is the highest-risk hollow-green surface. Require the fixture to encode each of the nine reconciliation cases as a *named, independently-asserted* sub-case, not one bulk set-equality.

## G. B4 review — Cutoff-safe lexical retrieval + Eval Foundation

**Verdict: correct; one P2 executable defect, one naming caution.**

The cutoff design is the centerpiece and it is right: `filter-before-score` proven behaviorally with a post-cutoff document that would otherwise rank #1 (Task 8); official session close with early-close (Nov 28 2025 → `T18:00:00Z`, regular Jan 15 2026 → `T21:00:00Z` — both correct for EST); intraday explicit UTC `as_of`; `bm25()` treated as engine-specific lower-is-better with one-based `lexical_rank`, never numerically compared across arms. Owning migration v10 for a persistent FTS5 index is the correct call for reproducibility.

**P2 (blocks B4 retrieval tests as written):** several Stream-B tests do `INSERT INTO corpus_chunks VALUES ('…','…','…','…','eligible','["AAPL"]')` — **6 positional values into the 18-column v9 `corpus_chunks`** (`test_filter_before_score`, `test_ticker_filter`, `test_top_k_after_filter`). SQLite raises `table corpus_chunks has 18 columns but 6 values were supplied`. Use explicit column lists in every insert, sourced from `retrieval_fixtures.py`.

**Naming caution (P2):** B4 creates `catalyst_data/retrieval/` while `catalyst_data/retrieval_policy.py` and `catalyst_agents/retrieval/policy.py` already exist. Three "retrieval" homes invites confusion. Have B4 state explicitly that the new `catalyst_data/retrieval/` package is the canonical retrieval home and that the legacy `retrieval_policy.py` is either wrapped or scheduled for supersession, so the executor does not create a fourth path.

Stream A (BenchmarkCase, EvidenceJudgment, PoolManifest, lineage validator, grade tooling) is clean and label-free as required. Lineage landmines (batch-stamp rejection, timestamp-predates-session rejection) are non-tautological.

## H. B5 review — Attribution workflow and runtime assurance

**Verdict: correct and honest; two specification gaps (P2), no blocker.**

The workflow is honestly agentic and correctly bounded: deterministic Context Builder via injected `ContextProvider` (production `SQLiteContextProvider` composed in B7 — the right place, resolving the earlier "data-core implementation" contradiction); two principal LLM calls (Critic + Judge) with at most one Validator repair; lexicographic ranking with **no** confidence score; SUFFICIENT/PARTIAL/ABSTAIN/SYSTEM_ERROR; RunAssuranceRecord owned by `runtime/assurance/`. Additive descriptive decomposition is explicitly labeled non-causal. The peer gate correctly requires edge + peer context + pre-cutoff peer evidence (edge-alone rejected). The INSUFFICIENT→ABSTAIN migration is real work (8 verified call sites) and correctly keeps a compatibility decoder for historical rows.

**P2 gap 1 — ranking test under-determined:** `test_lexicographic_order` sets `direct_support=2` vs `1` as if criterion 2 were a magnitude, but contract criterion 2 is "direct pre-cutoff support **exists**" (boolean). With h1 and h3 both at `dedup_clusters=2`, the asserted order (h1 before h3) is not determined by criteria 1–3 as given; it silently depends on `make_hypothesis` defaults for criteria 4–8. Make the fixture set every criterion that the asserted order depends on, or the test is a hidden hollow-green.

**P2 gap 2 — edge manifest under-specified:** gates consume a "versioned relationship edge" manifest, but neither B5 nor the contract pins its authoring format/location or how it enters the fixture. Pin it in B5 (a small versioned JSON, same discipline as the peer/relationship manifest) before B5 executes.

**P2 — allowlist gap:** see §D — `retrieval/policy.py` (direct sqlite) should be in scope so the Miner stops opening the corpus DB directly.

Landmine set is excellent: assurance self-certification is defeated by independently corrupting a citation id, a visibility set, and a cutoff and asserting each flips its check red (#10); cutoff-policy divergence caught by the injected recording policy (#8).

## I. B6 review — Dense retrieval and reranker

**Verdict: correct extract-don't-duplicate design; one P1 ownership defect (shared with §D).**

The "adapt the existing `reciprocal_rank_fusion()`/reranker in `lancedb_store.py`, don't create a competing algorithm" instruction is grounded and includes a compatibility landmine (#8: old public fn and new adapter must produce identical ordering; only one body remains). RRF k=60 one-based; reranker candidate-set preservation as exact set + cardinality equality before the top-8 slice; timeout → explicit RRF fallback; revision pins as 40-hex; identical filters across all arms (#10). All correct.

**P1 (pool ownership):** as in §D — `catalyst_data/retrieval/pool.py` generating an eval-owned `PoolManifest` is a boundary inversion, and the two pool shapes differ. Resolution: define a data-core-native `UnionJudgmentPool` dataclass (chunk_ids, per_arm_chunk_ids, corpus_manifest_id, index_manifest_id) owned by data-core/B6; keep B4's `PoolManifest` as the eval-side labeling manifest; have B7's labeling adapter map one to the other. Neither package imports "up."

**P2:** `test_pool_includes_manifest_identities` calls `generate_union_pool(arms={}, …, corpus_manifest_id=…, index_manifest_id=…)` while `test_union_pool_combines_all_arms` calls it with only `arms`+`case_id`; signature must give the manifest ids defaults. Harmless once the type is data-core-native.

## J. B7 review — API, evaluation, release

**Verdict: correct three-checkpoint split; one strong process control; minor duplicate-route risk (already mitigated).**

The B7-A / B7-H / B7-R split is the right way to handle a ≥7-day human interval inside an otherwise-automated pipeline; landmine #11 (reject if second-pass start < 7 days after first-pass completion or pool identity changed) makes the gate machine-enforced, not honor-system. Security tests correctly generate a **runtime random sentinel** and inspect only captured responses/logs/trace/temp-DB (never scan source), and zero-network replay installs a **real socket guard** that raises, not a boolean — both are the non-hollow constructions the contract demands.

**Duplicate-route risk (already mitigated, keep it):** existing routes are `workbench.py` (`/tickers`, `/ohlcv/{ticker}`, `/news/{ticker}`, `/session/{ticker}`, `/fundamentals/{ticker}`, `/range-local`) and `live_runs.py` (`/live-runs…`, `/models…`, `/health/runtime`). B7 adds `/api/news/{article_id}` (single) and `/api/news?ticker=` (list) which overlap the existing `/news/{ticker}`. Task 3 already requires a route inventory first and a `(method, path)` uniqueness landmine (#10). Preserve that; require the News refactor to *replace* `/news/{ticker}` semantics rather than sit beside them.

**Metric correctness:** Recall@8 / DCG / nDCG / primary_hit@8 formulas are standard and correct; unjudged is explicitly a distinct `JudgmentStatus`, never grade-0 (#5). Prohibited-metric landmines (composite/MRR/Precision@K/benchmark-in-UI) are behavioral greps plus a schema contract test. ResultPack identity-completeness validation is correct.

**12-case sufficiency:** adequate **for the bounded claims only**. 6 answerable + 2 abstain + 4 retrieval-only supports named-case keep/kill and the 2×2 abstain matrix; it does **not** support any generalized accuracy claim, and the contract correctly forbids one. Keep the asymmetric standard (kill on absence of benefit; keep requires named cases; tie → cheaper).

## K. Interaction / reviewer journey

The ten-step journey is coherent and demonstrable end-to-end once B2–B7 land:

1. Preview update → 2. confirm exact `plan_hash` → 3. execute + inspect status/provenance (ledger + raw + normalized_provenance) → 4. build/reconcile corpus (manifest id) → 5. lexical/dense/hybrid/reranked retrieval with per-stage trace → 6. attribution run → 7. ranked hypotheses + supporting/counter/missing evidence → 8. trace + RunAssuranceRecord → 9. zero-network offline replay → 10. compact scorecard (separate from product responses).

No missing interaction for the 5- and 15-minute journeys. One reviewer-clarity note: steps 2 and 9 (plan-hash confirmation; offline replay) are the two most persuasive "not-a-toy" moments and should be the headline of the quickstarts. The only thing that would confuse a reviewer is the three-"retrieval"-homes naming (§G) — fixing that naming is worth it for the walkthrough.

## L. Over-engineering and simplification findings

Applying the four-question test (real consumer? enforces an invariant? simpler form? portfolio-relevant?) to each significant abstraction:

| Abstraction | Real consumer | Enforces invariant | Verdict |
|---|---|---|---|
| Migrations v8/v9/v10 (3) | B2/B3/B4 | schema identity | **Keep** — minimal; each owns a distinct concern |
| CorpusManifest / IndexManifest | B4/B6 retrieval identity | reproducibility | **Keep** — load-bearing for cutoff/replay |
| RunAssuranceRecord + trace-version registry | B5/B7 | per-run integrity, schema drift | **Keep** — the differentiating asset |
| Source classifier (7 classes) | B3/B5 gates + assurance flags | evidence honesty | **Keep** — but it is the most "taxonomy-heavy" piece; ensure it stays a static table with exactly 3 consumers, no scores |
| **Union pool object** | B6 gen, B7 label | pool identity | **Fix ownership** (P1); do not add a third pool type |
| `catalyst_data/retrieval/` vs `retrieval_policy.py` vs agents `retrieval/policy.py` | — | — | **Simplify** — declare one canonical home (§G) |
| Provider canary (B2) | operator | isolation | **Keep** — one function, authorization-gated; not theater |

No agent-node bloat (Context Builder + existing six nodes; no personas, no Falsifier — correct). No table proliferation beyond the three migrations. Recommendation: the only *deletion/merge* actions are the pool-type unification (P1) and the retrieval-home naming declaration (P2). Everything else earns its place.

## M. Non-Toy Core Exit Gate (final)

**Required to avoid a toy (all MUST; unchanged from contract — verified load-bearing):**
- Gate A: zero-write planner + `plan_hash` + `PlanDriftError`; append-only request ledger; connector-side redaction; append-only request-scoped raw; normalized provenance; pagination/partial semantics; N-items→N-articles.
- Gate B: `news_v2` + `filing_v2`; raw/OHLCV/FRED/FMP/submissions never chunked; content/metadata/chunk identities; reconciliation + tombstones; atomic manifest; dedup-representative ≠ novelty; source taxonomy in manifest.
- Gate C: FTS5/BM25; pinned BGE-M3; RRF k=60; reranker with candidate preservation; cutoff parity across every path (injected policy); per-stage trace; degraded fallback; named-case reranker keep/kill.
- Gate D: typed contracts; deterministic Context Builder + required ETF/benchmark inputs; two LLM calls; hypothesis + peer gates; mixed/unexplained; abstention with reason; system_error vs insufficient separation.
- Gate E: RunAssuranceRecord every run, offline, no labels.
- Gate F: 12 evidence-first BenchmarkCases; union pools; Recall@8/nDCG@8/primary_hit@8/unjudged@8; 2×2 + abstain-reason; MetricRecord/ResultPack; zero-network replay; named-case decisions; no composite/no generalized accuracy.
- Gate G: committed synthetic fixture (no provider payloads); three key-free offline quickstarts; network-disabled tests; README matches code; claims link code+test; 5- and 15-min journeys.
- Gate H: loopback bind; Host/Origin allowlists; CORS closed; nonce/equivalent; keys memory-only; sentinel absent from responses/files/DB/logs/traces/reports/errors.

**Valuable but deferrable (Showcase, not Core):** broad SEC 8-K backfill; 10-K/10-Q parsing; operator UX beyond durable status/cancel/resume; browser walkthrough (fourth quickstart); evaluation UI; feedback Surface C; per-publisher caps; case-set expansion; live-variance reruns; cross-family judge; Finnhub peer expansion; package extraction; adversarial variants (SHOULD unless a release-blocking failure needs one).

**Unnecessary for this project (reject):** GraphRAG; RL/fine-tuning; SaaS/multi-tenant/hosted infra; multi-agent personas; standalone Falsifier; composite scores; generalized attribution-accuracy claims.

This matches the contract; no scope is added.

## N. Plan-by-plan executable defects (consolidated)

| # | Plan | Location | Defect | Severity |
|---|---|---|---|---|
| 1 | B2 | Task 2 (l.375) vs Task 9/10 (l.926–1038) | `execute_update` called sync and async; async tests not `async def`; undefined module-level `db` | **P0** |
| 2 | B2 | Task 7 (l.794) | `fresh_db_at_version` missing underscore → `NameError` | **P0** |
| 3 | B2 | Task 10 (l.1014–1017) | `>=` integration assertions violate the plan's own exact-count rule | P2 |
| 4 | B3 | Task 1 tokenizer tests | asserts `"BAAI/bge-m3" in TOKENIZER_REVISION` + `count("/")>=1` vs contract's bare-SHA revision | **P1** |
| 5 | B3 | Task 8 (l.847) | "all 9 cases" bulk set-equality is a hollow-green risk | P2 |
| 6 | B4 | Tasks 8–9 corpus_chunks inserts | 6 positional VALUES into 18-column table → `OperationalError` | P2 |
| 7 | B4 | new `retrieval/` vs existing two retrieval homes | naming ambiguity; declare canonical home | P2 |
| 8 | B5 | `test_lexicographic_order` | asserted order under-determined by given inputs | P2 |
| 9 | B5 | gates edge manifest | authoring format/location unspecified | P2 |
| 10 | B5 | modify-allowlist | `retrieval/policy.py` (direct sqlite) omitted | P2 |
| 11 | B6/B4 | `pool.py` returns eval `PoolManifest` | data-core→eval inversion; two pool shapes share a name | **P1** |

## O. Required amendments, ranked

### P0 — block B2 execution (fix before Goal B2)

**P0-1 — B2 async/sync `execute_update` + undefined `db`.**
*Plan/section:* B2 Tasks 2, 9, 10. *Correction:* choose one calling convention (the runtime is async elsewhere → make the executor `async` and mark Task 9/10 tests `async def` with `pytest.mark.asyncio`; for the sync `PlanDriftError` test either await it or expose a sync `plan_and_check(plan)` that raises before any await). Assign `db` explicitly in each integration test from the isolated fixture (as Task 10's `seeded_polygon_response_db` already does). *Blocks:* B2 only. *Smallest fix:* make executor async, mark the three async tests, replace the sync raise-site with the pre-network sync drift check, and bind `db` per test.

**P0-2 — B2 `fresh_db_at_version` typo.**
*Plan/section:* B2 Task 7, `test_pagination_cursor_loop_detected` (l.794). *Correction:* `_fresh_db_at_version(8)`. *Blocks:* B2 pagination task only. *Smallest fix:* one-character edit.

### P1 — block their own package (not B2)

**P1-1 — Union-pool ownership/boundary.**
*Plan/section:* B6 Task 6 (`catalyst_data/retrieval/pool.py`) + B4 `PoolManifest`. *Correction:* define a data-core-native `UnionJudgmentPool` dataclass owned by B6 (`chunk_ids`, `per_arm_chunk_ids`, `corpus_manifest_id`, `index_manifest_id`); keep eval `PoolManifest` as the labeling-side manifest; B7 adapts one to the other. Data-core must not import `catalyst_eval`. Add a landmine: `rg "catalyst_eval" packages/data-core/` returns zero. *Blocks:* B6 (and B7 labeling), not B2.

**P1-2 — B3 tokenizer revision contradiction.**
*Plan/section:* B3 Task 0/1 + `test_tokenizer.py`. *Correction:* `TOKENIZER_MODEL_ID = "BAAI/bge-m3"`, `TOKENIZER_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"`; assert model-id contains `/` and revision matches `^[0-9a-f]{40}$` (aligning with B6's `test_bge_m3_revision_is_pinned`). *Blocks:* B3, not B2.

### P2 — quality / anti-hollow-green (fix in the owning package)

- **P2-1 (B4):** replace all 6-value `corpus_chunks` inserts with explicit column lists from `retrieval_fixtures.py`.
- **P2-2 (B4):** declare `catalyst_data/retrieval/` the canonical retrieval home; state the fate of `retrieval_policy.py`.
- **P2-3 (B3):** make `test_reconcile_handles_all_failure_modes` nine *named, independently-asserted* sub-cases, not one set-equality.
- **P2-4 (B5):** make `test_lexicographic_order` set every criterion the asserted order depends on.
- **P2-5 (B5):** pin the relationship-edge manifest format/location; add it to the fixture.
- **P2-6 (B5):** add `catalyst_agents/retrieval/policy.py` to the B5 allowlist (or route Miner through an injected `Retriever`) so agents stops opening the corpus DB directly.
- **P2-7 (B2):** replace Task 10 `>=` integration assertions with exact-count isolated-DB assertions.
- **P2-8 (B7):** require the News refactor to replace `/news/{ticker}` rather than coexist; keep the `(method,path)` uniqueness landmine.

None of P1/P2 blocks B2.

## P. Decisions that must not be reopened

Verified sound; do not reopen absent hard technical impossibility: local open-source workbench positioning; no thesis/paper/SaaS; two principal LLM calls (Critic+Judge) with ≤1 Validator repair; deterministic Context Builder; timestamp cutoff as release blocker with parity via injected policy; three evaluation surfaces; 12 base cases (+≤4 SHOULD variants); RunAssuranceRecord owned by `catalyst_agents/runtime/assurance/`; FTS5 + pinned BGE-M3 + RRF(k=60) + measured reranker with keep/kill; no multi-agent personas / no Falsifier / no GraphRAG / no RL; migration ownership v8→v9→v10; agents never import eval; benchmark metrics never in normal responses; stage-only Git discipline; B7-H human interval as a hard, machine-enforced pause.

## Q. Final B2 readiness decision

**B2 is ready to execute after the two P0 edits (P0-1 async/sync + `db`; P0-2 typo).** Both are mechanical, confined to B2 test snippets, and change no architecture. B2's design, migration-v8 scope, identities, and landmines are correct and grounded in verified current state. Recommended gate: apply P0-1/P0-2, run the B2 verification ladder, produce the evidence report, and pause for orchestrator review before B3 — during which P1-2 (B3 tokenizer) must be fixed. P1-1 (pool ownership) must be resolved before B6; the remaining P2s in their owning packages.

## R. Integrity report

**Files read (this session):** the four canonical designs were read in the prior session; this session read `docs/plans/2026-07-21-b2-b7-technical-contracts.md` and all six execution plans (`2026-07-22-b2…b7`) in full; and inspected code in `packages/data-core/catalyst_data/` (`migrations.py`, `update_pipeline.py`, `articles.py`, `index_builder.py`, `trading_calendar.py`, `storage/sqlite.py`, `storage/lancedb_store.py`, `retrieval_policy.py`, `dedup/`), `packages/agents/catalyst_agents/` (`state.py`, `nodes/{validator,finalizer,critic}.py`, `runtime/`, `retrieval/policy.py`), `packages/eval/catalyst_eval/` (`schema/`, `adapters/agents.py`, `metrics/`), and `packages/app/catalyst_app/routers/`.

**Commands run:** `git branch/rev-parse/status/diff --cached`; `.venv/bin/python -m pytest packages/<pkg> -q` ×4 (reran); read-only `sqlite3 "file:…?mode=ro" "PRAGMA user_version"`; `grep`/`ls` inspections; `shasum -a 256` on both DBs before and after.

**Test results (reran):** data-core 758 passed / 1 skipped / 1 xfailed (exit 0); agents 237 passed (exit 0); eval 93 passed (exit 0); app 128 passed / 0 failed (exit 0). The brief's "app 114/15" figure is stale; app is green.

**DB SHA before/after:** Dev `92731fb7c5c3e989b4fdcbefb9d1d2060974082a6c60f1bbecad6bc42ee846b0` — unchanged; frozen `0d97a7ec61b6ec8fb5f9263b0b37b0efc9755812739d7afead7f720c3567e8dd` — unchanged. Neither DB opened writable.

**Git index before/after:** empty (`git diff --cached` = 0 entries) before and after.

**Confirmation:** no runtime code implemented; no plans modified; no databases mutated; no secrets inspected; no provider/model/judge calls; nothing staged, committed, pushed, or branched. The only new artifact is this untracked review document.

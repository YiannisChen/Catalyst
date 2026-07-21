# B2–B7 Pre-Execution Ratification

- Reviewer role: final architecture and pre-execution reviewer (last gate before dscodex executes B2→B7)
- Date: 2026-07-22
- Discipline: read-only except one new review document + one minimal roadmap amendment; no code, no DB mutation, no provider calls, no secret inspection, no staging/commits
- Verified state: branch `ws4b/article-level-data` at `4d4e3a9`; Git index empty; Dev DB `data/catalyst_dev_ws4b.db` SHA `92731fb7c5c3…` (`user_version=6`); frozen `data/catalyst_eval_frozen_v2.db` SHA `0d97a7ec61b6…`; second worktree `Catalyst-ws2` present at `3d01a6f`
- All four suites reran this session with `.venv/bin/python -m pytest packages/<pkg> -q`

---

## A. Executive verdict

**GO — B2 may begin.**

Every P0 and P1 defect from the prior adversarial review (`2026-07-21-b2-b7-final-adversarial-review.md`) has been independently re-verified as **fixed in the current plan text**, and every P2 was addressed with a sound construction, not a cosmetic edit. The architecture is coherent, correctly scoped for one developer, and genuinely non-toy. The migration DAG (v8→v9→v10) is consistent, package boundaries are enforced with real landmine greps, and the plans' test snippets are now executable as written.

The final orchestrator pass found and resolved three execution-hygiene items after the initial review: the app retry test race, an undefined raw-store hash helper, and fixture factories referenced but not declared by B2 Task 0. The roadmap inaccuracy ("existing Context Builder") was also amended in place. No architecture is reopened.

I did not re-issue amendments to B2–B7: they were already corrected by whoever revised the plans after the prior review. My role this session was to verify that the corrections are real and sound. They are.

**Addendum (2026-07-22, Opus 4.8 anti-drift pass):** a strict pre-execution pass against the now-binding §4.6 v8 DDL found five B2 test snippets that contradicted the schema or used undeclared helpers, plus one B3 helper gap. All were amended in place (docs only); verdict is unchanged **GO**. Fixes: (1) B2 `test_insert_request_attempt` used `page_no: 0` (violates `CHECK (page_no >= 1)`) and a non-hex `request_fingerprint` (violates the 64-hex CHECK) → corrected to `page_no: 1` and a 64-hex literal; (2) all three B2 provenance tests used `entity_version="v1"` (violates the 64-hex CHECK) and did not seed `raw_assets` for the `raw_asset_id` FK (`ON DELETE RESTRICT`) → now use a 64-hex SHA-256 `entity_version` and seed via `seed_v2_raw_row`; (3) `base_attempt` (B2) and `base_params` (B3) were referenced but undeclared → declared in each Task 0 with exact contract-valid contents; (4) `RawResponseIntegrityError` import added to the raw-store snippet; (5) `store_raw_response` signature gains `page_no=1` and pins `raw_asset_id='raw:'+request_id`; (6) Task 0 now states fixtures open `PRAGMA foreign_keys=ON` and every test invoking a production writer for a `run_id` seeds the `ingestion_runs` parent first. These convert latent execution-time drift into contract-conformant, copy-runnable snippets.

## B. Verified repository state

| Check | Result |
|---|---|
| Branch / HEAD | `ws4b/article-level-data` @ `4d4e3a938ac6` |
| Git index | empty (0 staged) |
| Worktrees | `Catalyst` (this, ws4b) + `Catalyst-ws2` (ws2/view-model-parity @ 3d01a6f) |
| Dev DB SHA | `92731fb7c5c3…` (unchanged); `PRAGMA user_version=6` (read-only URI) |
| Frozen DB SHA | `0d97a7ec61b6…` (unchanged) |

**Test baselines (reran, canonical commands, main venv):**

| Package | Result | Exit |
|---|---|---|
| data-core | 758 passed, 1 skipped, 1 xfailed (89 s) | 0 |
| agents | 237 passed | 0 |
| eval | 93 passed | 0 |
| app | **128 passed** after deterministic retry-test repair | 0 |

**App timing race resolved before B2.** The orchestrator reproduced `test_retry_semantics_and_lineage` failing once in ten full-package runs. The parent run was created through the HTTP endpoint, which immediately started a background worker; that worker could overwrite the test's manual `RUNNING` state before the retry request. The test now creates the parent through `LiveRunService.create_run()` without starting a worker, then drives the RUNNING and terminal states explicitly. This isolates retry semantics and lineage without adding a blocking graph or changing production behavior.

The working tree is a large, coherent **B1 cleanup** (superseded plans/reports/ADRs/thesis-era eval tests deleted; READMEs and package boundaries repaired; `adapter.py` moved to `catalyst_eval/adapters/`) plus untracked canonical designs, contracts, roadmap, and the B2–B7 plans. Git strategy for it is §Q.

## C. Non-Toy Core assessment

The Core Exit Gate is defined by the technical contract and the six plans, and B2→B7 make each property **inevitable through tests**, not aspirational:

| Non-toy property | Made inevitable by | Verdict |
|---|---|---|
| Requests → attempts → immutable raw | B2 v8 ledger + append-only triggers (landmine: UPDATE/DELETE raises) | Inevitable |
| Many-to-many normalized provenance | B2 `normalized_provenance` composite PK + idempotency test | Inevitable |
| Deterministic/previewable/idempotent/resumable/cancellable/drift-safe planning | B2 `plan_hash` (runtime-field exclusion test), `PlanDriftError`, run_control lease/cancel/resume | Inevitable |
| Partial/failure explicit, never silent success | B2 PARTIAL/SUCCESS_EMPTY state machine + partial-page test | Inevitable |
| Stable chunk identity, versioned profiles, reconciliation, tombstones, manifest | B3 chunk_id/content/metadata hashes + nine parametrized reconciliation oracles + atomic manifest failure-injection | Inevitable |
| `available_at <= cutoff` before scoring | B4 filter-before-score with a post-cutoff doc that would rank #1 | Inevitable |
| One typed retrieval contract across lexical/dense/fusion/reranker | B4 `RetrievalResult`; B6 extends same type | Inevitable |
| Reranker measured and disable-able | B6 candidate-preservation + B7 named-case keep/kill, retain-optional | Inevitable |
| Per-run evidence/decisions/trace/model+prompt id/cost/latency/degradation/assurance | B5 RunAssuranceRecord + trace-version registry | Inevitable |
| Abstain / unexplained vs system_error | B5 four-state output + gate tests | Inevitable |
| Reproducible eval without network | B7 zero-network replay with a **real socket guard** that raises | Inevitable |
| Offline quickstart, no keys, no 269 MB DB | B7 synthetic fixture + three key-free quickstarts + network-disabled marker | Inevitable |
| Typed local API, not fake SaaS | B7 six typed route groups; SaaS explicitly out of scope | Inevitable |
| README claims provable by command/test | Gate G "every capability claim links code + test" | Inevitable |

**Conclusion:** the plans make non-toy status a test-enforced outcome. This clears Professor Mac's standard (reproducible behavior, explicit contracts and failure states, independently testable modules, honest limitations, evidence over claims).

## D. Industrial agent-system assessment

Judged against what a credible single-developer open-source project requires (not a hyperscale platform):

| Category | Classification |
|---|---|
| Package dependency direction | Adequate for Core (enforced by import-direction landmines) |
| Typed boundaries + schema versioning | Adequate (trace-version registry, dataset_version, manifest ids) |
| Deterministic identity + replay | Adequate (canonical-JSON hashing rule; zero-network replay) |
| Idempotency | Adequate (raw store, provenance, upserts) |
| Migration ownership | Adequate (v8/v9/v10, one owner each) |
| Concurrency / writer leases | Adequate for Core (single-writer lease + TTL) |
| Retry / rate-limit | Adequate (existing limiter/retry, instrumented) |
| Cancellation / orphan recovery | Adequate (cooperative cancel + resume-from-checkpoint); richer orphan sweep = useful later |
| Partial-success semantics | Adequate (explicit PARTIAL/SUCCESS_EMPTY) |
| Provider fallback | Adequate (OHLCV fallback with per-attempt records) |
| Secret redaction | Adequate (connector-side redaction before log/ledger; sentinel-capture tests) |
| Temporal leakage prevention | Adequate — a headline strength (cutoff parity via one injected policy) |
| Retrieval observability | Adequate (per-stage trace, per-stage ranks) |
| Model/prompt version binding | Adequate (identities in assurance + ResultPack) |
| Cost/latency records | Adequate (cost_tracker with `cost_status="unknown"`) |
| Error taxonomy | Adequate (existing taxonomy + integrity errors) |
| Abstention / degraded operation | Adequate (four-state + degraded flags) |
| Offline tests / synthetic fixtures / zero-network CI | Adequate (Gate G) |
| Artifact compatibility | Adequate (schema-version stamping, legacy read compatibility) |
| Operator diagnostics | Adequate for Core (reduced CLI: plan/run/status/backup); rich UX = useful later |
| Documentation accuracy | Adequate after §S amendment |

No category is "missing but required." Deferred-useful: orphan-lease sweeper, richer operator UX. Unnecessary for this project: multi-tenant concurrency, distributed coordination, hosted telemetry.

## E. Over-engineering / YAGNI findings

Every heavyweight abstraction is load-bearing; none exists merely to look industrial.

| Component | Verdict | Failure prevented | Reason |
|---|---|---|---|
| Request-attempt ledger | **Keep** | untraceable provider calls; silent retry loss | the provenance spine; consumed by B5 assurance |
| Raw immutability triggers | **Keep** | silent raw overwrite (current `INSERT OR REPLACE` bug) | enforced by DB, not convention |
| logical_fetch_id | **Keep** | can't group retries/pages of one cell | cheap deterministic id |
| normalized_provenance | **Keep** | evidence with no source lineage | many-to-many is real (article in multiple requests) |
| durable cancel/lease/resume | **Keep (Core minimum)** | orphaned/duplicate writers; unresumable partial runs | scoped to status/cancel/resume only; richer UX deferred |
| CorpusManifest / IndexManifest | **Keep** | non-reproducible retrieval identity | required for cutoff/replay |
| Tombstones | **Keep** | stale chunks resurfacing after content change | required by reconciliation |
| Source taxonomy (7 classes) | **Keep** | opinion/aggregated masquerading as evidence | static table, exactly 3 consumers, no scores |
| Relationship manifest | **Keep** | free-form peer hypotheses without edges | now fully specified (B5 §0), reviewed JSON, no runtime discovery |
| ContextProvider protocol | **Keep** | agents opening corpus SQLite directly | the injection seam; production adapter in app |
| RunAssuranceRecord | **Keep** | unverifiable per-run integrity | works on arbitrary runs, not just benchmark |
| BenchmarkCase / UnionJudgmentPool separation | **Keep** | data-core→eval boundary inversion | now data-core-native pool; eval owns labeling manifest |
| FTS5 + dense + RRF + reranker | **Keep** | unmeasured retrieval claims | reranker is measured and disable-able |
| Human labeling pause (B7-H) | **Keep** | fabricated/hindsight labels | the ≥7-day gate is machine-enforced |
| Result packs | **Keep** | non-reproducible scorecards | identity-complete, replayable |
| Local API routers | **Keep (typed, not SaaS)** | shared internal domain leaking into HTTP | six typed groups, no accounts/billing |

Nothing to remove or defer beyond what the plans already defer. No YAGNI violation found.

## F. Package and dependency review

Intended DAG `data-core ← agents ← app`, `eval` consumes artifacts, production never imports eval — **verified enforced**:

- `catalyst_data` imports no internal package (clean pyproject: httpx/pydantic/bs4/dotenv).
- `catalyst_agents` no longer imports eval (adapter moved to `catalyst_eval/adapters/agents.py`); B5 landmine `rg "catalyst_eval" packages/agents/` = 0; `test_package_boundaries.py` exists.
- `catalyst_eval` consumes plain artifacts (schema/result, adapters) — no agent internals.
- `catalyst_app` owns composition; the production `SQLiteContextProvider` is placed in app (B7), not agents/data-core.
- **Pool boundary inversion RESOLVED (was P1):** B6 now defines a data-core-native `UnionJudgmentPool` (schema_version/case_id/chunk_ids/per_arm_chunk_ids/corpus_manifest_id/index_manifest_id/source_artifact_id) and explicitly forbids importing `catalyst_eval.benchmark.PoolManifest`, with landmine #11 `rg "catalyst_eval" packages/data-core/` = 0.
- **SQLite-in-agent-nodes RESOLVED (was P2):** B5 allowlist now includes `retrieval/policy.py` — "remove direct SQLite access; delegate through injected B4 Retriever."
- **Retrieval ownership unambiguous:** `catalyst_data/retrieval/` is the canonical home; agents consume via injected Retriever/ContextProvider.

No remaining boundary inversion, current or planned.

## G–L. Per-plan review (B2–B7)

**G. B2 — GO.** Prior P0s all fixed: one async `execute_update(*, db, plan, transport)` (line 989, "do not add a same-named synchronous wrapper"); every test `@pytest.mark.asyncio async def` with `db` defined and exact counts (`expected_ohlcv_cell_count`); no `fresh_db_at_version` typo; no `>=` integration asserts. v8 scope, identities, state machines, redaction, two-stage re-plan, durable control, and non-tautological landmines (transactional rollback #8, per-field plan-hash + one hand-computed SHA #9) all sound. **Blocks B2: none.**

**H. B3 — GO (after B2).** Tokenizer P1 fixed: `TOKENIZER_MODEL_ID = "BAAI/bge-m3"` + `TOKENIZER_REVISION = "5617a9f…"` with `re.fullmatch(r"[0-9a-f]{40}", …)` — now consistent with B6. Reconciliation P2 fixed: nine **named, independently-asserted** cases via `@pytest.mark.parametrize("case_name", NINE_RECONCILIATION_CASE_NAMES)`, each with an exact oracle. news_v2/filing_v2 rules, dedup-representative≠novelty landmine, atomic manifest failure-injection intact. v9 owns corpus tables.

**I. B4 — GO (after B3).** corpus_chunks insert P2 fixed: tests seed via `_fresh_db_with_chunks()`/insert helpers, not 6-value positional inserts. Filter-before-score proven behaviorally; official/early-close session cutoffs (EST dates correct); bm25 lower-is-better with one-based rank; v10 owns FTS5; parity deferred to B5's injected-policy test (correct anti-tautology construction). Eval Foundation is label-free. `PoolManifest` carries B6's immutable `source_artifact_id`, so labels cannot detach from the retrieval-arm artifact that created the union pool.

**J. B5 — GO (after B4).** Edge-manifest P2 fixed: `relationships_core_v1.json` fully specified (schema_version/manifest_id/edges[] with typed fields; manifest_id = SHA-256 of canonical JSON excluding itself; no runtime discovery). Ranking P2 fixed: `make_hypothesis` sets all eight criteria (direct_support boolean, dedup_clusters, max_relevance, degradation_flags), fixtures "expose all eight ranking criteria." Two LLM calls (Critic+Judge) + ≤1 Validator repair; INSUFFICIENT→ABSTAIN with historical decoder; assurance self-certification defeated by independent corruption of citation/visibility/cutoff (#10). No SQLite in nodes (policy.py in allowlist). No new migration.

**K. B6 — GO (server-fenced).** Pool inversion fixed (§F). Extract-don't-duplicate RRF/reranker from `lancedb_store.py` (verified present: `reciprocal_rank_fusion`, `RRF_K=60`, `_apply_reranker`), compatibility landmine #8. Revisions pinned as 40-hex; candidate-set preservation exact-set; timeout→RRF fallback. Full GPU build correctly operator-authorized; Mac runs fixture-scale with injected embedders/rerankers. No new migration.

**L. B7 — GO (three checkpoints).** News-route collision P2 fixed: refactor replaces `/api/news/{ticker}` in place and adds `/api/news/articles/{article_id}` (explicitly not `/api/news/{article_id}`); route-uniqueness landmine #10. B7-A/B7-H/B7-R split with machine-enforced ≥7-day interval (#11); runtime-random sentinel security tests inspecting only captured artifacts; real socket-guard zero-network replay; identity-complete ResultPack. `SQLiteContextProvider` composed in app.

## M. Attribution / RAG correctness review

Genuinely useful RAG/agent engineering, not prompt theater:
- Chunking matches evidence granularity (article-level news, section-level 8-K); provenance preserved to document_id.
- Cutoff applied before scoring on every arm via one injected `ExchangeCutoffPolicy` (parity test asserts identical args/values across Miner and Validator — not a double call).
- Lexical/dense comparable only via rank (bm25 raw never numerically compared to cosine); RRF(k=60) implemented once (extracted, compatibility-tested).
- Reranker optional and measured; candidate universe preserved.
- Evidence dedup-clustered before the model; representative ≠ novelty timestamp.
- Context Builder deterministic (market/sector/peer decomposition explicitly descriptive, never causal).
- Hypotheses gated by required evidence (edge + peer context + pre-cutoff peer evidence; edge-alone rejected).
- Judge separates fact/inference/counter-evidence/change-condition; Validator checks machine-verifiable properties; "unexplained" distinct from ABSTAIN and SYSTEM_ERROR; exactly two LLM calls.
- Every-run assurance provides value outside benchmark (works on arbitrary runs). **No prompt theater found.**

## N. Evaluation and replay review

12 evidence-first BenchmarkCases (6 answerable + 2 abstain + 4 retrieval-only) — adequate for the **bounded** claims only (named-case keep/kill, 2×2 abstain matrix), never a generalized accuracy claim (correctly forbidden). Union-pool-before-labels ordering enforced; grade 2/1/0 with unjudged as a distinct status (never silent 0); Recall@8/nDCG@8/primary_hit@8 formulas standard and correct; ResultPack identity-complete; zero-network replay with a real socket guard. Human pause is the honest solo-annotator protocol with a delayed second pass. Proportional — not a research experiment.

## O. API and future frontend compatibility

The six typed route groups (Data, News, Chart, Attribution, Trace, Run Status) supply exactly what Stage-2 F1/F2 need: update preview/confirm/status, K-line + synchronized news with publisher/image/original-url, ranked hypotheses with supporting/counter-evidence and abstention, and trace/assurance/latency/cost. Benchmark metrics are kept out of normal responses. No internal domain model leaks into HTTP schemas. Backend contracts can support the later frontend without rework.

## P. Portfolio / admissions / interview value

- **Master's essays:** the architect can honestly narrate discovering the ±3-day temporal leak and replacing it with cutoff parity; designing reproducible retrieval evaluation with union pools and unjudged handling; separating deterministic systems logic from model judgment (Context Builder vs Critic/Judge); measuring reranker utility rather than assuming it; artifact identity + zero-network replay; and scoping against frontier web-search models honestly. All are backed by tests/artifacts, not claims.
- **Open-source trust:** focused per-boundary commits, enforced package boundaries, meaningful (non-tautological) tests, honest READMEs, reproducible key-free fixtures, stable typed interfaces, no abandoned generated docs, no secret leakage, and real extension points (Connector/ChunkProfile/Retriever/Reranker/Metric with reference impls).
- **Interviews (10–15 min):** load synthetic fixture → lexical retrieval → optional dense/RRF/reranker → one attribution with stub model → inspect evidence/trace/assurance/latency/cost → replay eval offline → explain the temporal-leak fix. Every step is demonstrable offline.

No planned feature fails to strengthen at least one narrative.

## Q. Git branch / worktree / staging / commit strategy

**Continue in the current working tree on `ws4b/article-level-data`. Do NOT create a new worktree for B2.** The B1 cleanup is uncommitted (package-boundary repairs in `agents/pyproject.toml`, `adapter.py` deletion, `migrations.py`, connector/config edits, new `catalyst_eval/adapters/`, `test_package_boundaries.py`). A fresh worktree would start from committed `4d4e3a9` and **lack every one of these B2 prerequisites** — B2 would fail immediately. The instruction's own caveat ("do not recommend a new worktree if uncommitted dependencies required by B2 would be absent there") applies directly.

**Branch name:** `ws4b/article-level-data` no longer describes a full B2→B7 backend program, but renaming mid-flight is disruptive and not technically required. Keep it; optionally, after committing B1, the architect may branch `backend/b2-b7` from that commit for cleaner history. Not a blocker.

**Never stage (verified untracked/ignored gaps):** `data/provider_discovery/` (only `*.json` is gitignored — the directories contain more), `data/provider_probe/`, `packages/data-core/scripts/provider_discovery.py`, `scripts/discover_models.py`, `scripts/probe_news_sources.py`, `scripts/run_polygon_live.sh`, `data/catalyst_eval_frozen_v2.db.damaged-0d97-backup`, both canonical DBs, `.env`. `.gitignore` does **not** currently cover `data/provider_probe/` or the damaged backup — rely on explicit path allowlists, never `git add -A`.

**Proposed boundaries (exact groups; stage only on architect authorization):**
- **G1 — `chore(repo): remove superseded plans, reports, and thesis-era eval tests`**: all `D` docs/reports/testing/research files + deleted `packages/eval/tests/test_*` + deleted `scripts/*` (p1_*, g013_*, verify_*, etc.).
- **G2 — `refactor(packages): repair boundaries and honest READMEs`**: `packages/agents/pyproject.toml`, deleted `adapter.py` + `test_adapter.py`, new `catalyst_eval/adapters/` + `test_agents_adapter.py`, `test_package_boundaries.py`, README edits (root, agents, app, data-core, eval), `packages/eval/pyproject.toml`, app test repairs, data-core edits that are pure boundary/baseline (`config.py`, connectors, `migrations.py` if only baseline, `update_*`, `trading_calendar.py`, `quality.py`, `fallback.py`, `storage/*`), new `test_ohlcv_execution.py`, `runtime_fixture.py`. Migrations reviewed separately from runtime within this group.
- **G3 — `docs(plans): add canonical designs, technical contracts, roadmap, and B2–B7 plans`**: the untracked `2026-07-19-*`, `2026-07-21-*`, `2026-07-22-*` docs (incl. this ratification and the prior review).
- **G4+ — one coherent group per B2 task/migration/runtime/test boundary**, per each plan's own "Git Boundaries" section. Migrations (v8, v9, v10) each get a separately reviewable commit; tests land with the behavior they verify.

**Conventional Commits, no assistant/generated-content references.** Do not reset/checkout/stash/clean/rebase/delete. Do not stage or commit during review (none occurred).

## R. Goal-mode execution protocol

**One package per Goal, evidence report, pause, orchestrator landmine check, architect authorizes the Git boundary, next Goal.** Grouping decision:

- **B2 alone** (heaviest data plane; v8).
- **B3 then B4** may be one Goal only if review cost stays manageable; safer as **B3 alone → B4 alone** (v9 then v10 are independently reviewable and B4 also owns the Eval Foundation).
- **B5 alone.**
- **B6 alone** (GPU/server steps differ from Mac; Mac runs fixture-scale only).
- **B7 split into B7-A (API/eval/fixtures/replay) → B7-H (architect human labeling, ≥7-day gap, outside Goal mode) → B7-R (metrics/keep-kill/release).** A single Goal must never bridge B7-H.

**Mandatory stop conditions (any ⇒ halt, report, do not proceed):** canonical DB SHA changes; a test touches a live provider; a migration is applied to the canonical Dev DB without authorization; a package dependency inversion appears (`rg "catalyst_eval" packages/{data-core,agents}` ≠ 0, or agents/app imported by data-core); cutoff parity fails; secret-redaction/sentinel test fails; a result artifact lacks an identity field; any canonical suite regresses; staged files fall outside the authorized boundary; or a plan step needs an unratified architecture decision.

## S. Required amendments applied

**Initial reviewer amendment — cross-document consistency:**
`docs/plans/2026-07-21-catalyst-roadmap.md` §B5 said "Strengthen the **existing** Context Builder → Miner → …". Verified false: `catalyst_agents/attribution/` does not exist; Context Builder is new (B5 plan §2 says so; §Current Verified State confirms). Changed to "Add a **new** deterministic Context Builder ahead of the existing Miner → … workflow, and strengthen that workflow …". Severity **P3**; does not block anything; removes a roadmap-vs-B5-vs-code contradiction.

**Final orchestrator amendments — execution hygiene:**

- B2 Task 0 now declares every transport/count fixture referenced by later tests and requires count oracles to be independent of the production planner.
- B2 raw-store examples use explicit `hashlib.sha256(...).hexdigest()` values instead of an undefined `sha256()` helper.
- B2 Task 11 explicitly forbids autonomous execution of the authorized live-canary path.
- The app retry test creates its parent run without launching a background worker, removing the reproduced state-overwrite race without changing production code.

No product architecture, migration ownership, locked decision, or production behavior changed.

## T. Remaining architect decisions

1. Git: authorize the G1/G2/G3 staging boundaries and commit messages (nothing staged in this review).
2. Branch: keep `ws4b/article-level-data` or branch `backend/b2-b7` after committing B1.
3. B2 external-operation timing: the supervised ETF/relationship-manifest and any provider canary remain separately authorized (no provider call is part of B2 tests).
4. B6 server/GPU window; B7 judge model + one-time cache re-record; B7-H labeling calendar.

## U. Exact GO / NO-GO gates

**GO to start B2 now.** B2 proceeds unblocked; between packages, the following must hold or execution halts (§R stop conditions), checked by the orchestrator before each Git authorization:

- both DB SHAs unchanged (`92731fb7…`, `0d97a7ec…`);
- `git diff --cached` contains only the authorized boundary's files (never provider probes/secrets/DBs);
- `rg "catalyst_eval" packages/data-core packages/agents` = 0 (except agents test that asserts absence);
- migration applied only to disposable copies until an explicit operator step;
- cutoff-parity, secret-redaction, and result-identity landmines green;
- all canonical suites green with no known-flaky exemption;
- no plan step demands an unratified architecture decision.

## V. Integrity report

- **Files amended:** `docs/plans/2026-07-21-catalyst-roadmap.md`, `docs/plans/2026-07-22-b2-data-update-provenance.md`, and `packages/app/tests/test_failure_paths.py`.
- **Files created:** `docs/plans/2026-07-22-b2-b7-pre-execution-ratification.md` (this document).
- **Test commands + results:** the orchestrator reproduced the app race once in ten package runs, repaired the test setup, then ran the focused retry test 30 consecutive times successfully. Final canonical results: data-core `758 passed, 1 skipped, 1 xfailed`; agents `237 passed`; eval `93 passed`; app `128 passed`. All commands exited zero.
- **DB SHAs (before == after):** Dev `92731fb7c5c3e989b4fdcbefb9d1d2060974082a6c60f1bbecad6bc42ee846b0`; frozen `0d97a7ec61b6ec8fb5f9263b0b37b0efc9755812739d7afead7f720c3567e8dd`. Neither opened writable; `user_version` read via `file:…?mode=ro`.
- **Git index:** empty before and after (`git diff --cached` = 0). `git diff --check` clean.
- **Network/providers:** none called. **Secrets:** none inspected. **Staging/commits:** none.
- **Mandatory grep (§15):** `rg "TODO|TBD|placeholder|pass$|except Exception|git add|git commit|git push" docs/plans/2026-07-22-b*.md` → only benign prose matches ("placeholder is invalid", lines ending in the word "pass"); no code `pass`, no `except Exception`, no git-mutation commands. Markdown fences balanced in all six plans. Migration ownership v8/v9/v10 with one owner each.

---

## Verdict

**GO — B2 may begin.**

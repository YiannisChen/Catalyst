# Final Package Architecture — Kernels, Boundaries, and Contributor Surface

- Date: 2026-07-19
- Reviewer role: final architecture reviewer (package structure, reuse boundaries, contributor surface)
- Status: **ratified subordinate package design** (amended 2026-07-19 per orchestrator review: explicit layer DAGs §C.1–C.2, exact public APIs §J, fixtures promoted to MUST §P, local BYOK security §I.1, landmine test corrections §O; correction amendment 2026-07-21: README provider-table completeness §L, ragas-extra removal and thesis-era test quarantine §G/§P)
- Parents: `2026-07-19-catalyst-open-source-workbench-design.md`, provenance/chunking design, evaluation architecture review
- Discipline: review only; no code, dependency, DB, or canonical-design changes; nothing staged or committed; no providers; no secrets
- Verified state: branch `ws4b/article-level-data` at `4d4e3a9`; Git index empty; Dev DB SHA `92731fb7c5c3…`; Frozen DB SHA `0d97a7ec61b6…`

The package dependency lists, READMEs, and module inventories were independently verified during design. B1 later removed the agents→eval runtime dependency and replaced the stale public documentation.

---

## A. Verdict

**Approve the proposed direction: monorepo, three engineering kernels plus the app, domain adapters inside each package, no extraction now, no fourth contracts package.** The proposal is the right scope for the corrected positioning. Binding adjustments in this review: (1) runtime assurance lives in **agents/runtime**, not eval and not a neutral package (§H); (2) the internal re-layering of data-core and agents proceeds by *target layout + shims + opportunistic moves*, never a bulk physical rewrite (§M); (3) the app loses every SaaS-shaped requirement including the "public HTTP hardening" phase, rescoped to a local-only Developer Workbench (§I); (4) three verified stale claims must be corrected in documentation (§L); (5) agents' hard dependencies on both `langchain-anthropic` and `langchain-openai` become optional extras with the model wiring staying in app (§F).

## B. Audience and project-positioning correction

Accepted as stated: the audiences are contributors, admissions reviewers, interviewers, and the architect. The consequence that does real work in this review: **every architectural decision below is judged by "does a contributor understand and trust this in one sitting," not "does this scale to users."** Concretely that kills multi-tenancy thinking, hosted anything, and generic-framework abstraction, and it elevates: readable module boundaries, one-command quickstarts, synthetic fixtures that run without API keys, and honest READMEs. Prohibited claims stay in force (no causal truth, no product superiority, no "multi-agent").

## C. Package dependency graph (binding)

- `catalyst-data` → (nothing internal)
- `catalyst-agents` → `catalyst-data` only
- `catalyst-app` → `catalyst-agents`, `catalyst-data`
- `catalyst-eval` → `catalyst-data` (optional, for benchmark corpus access) and **artifacts only** from agents: persisted traces, plain state dicts, assurance records, result packs. `catalyst-agents` never imports `catalyst-eval` — the verified violation (`adapter.py:12` + `pyproject.toml:15`) is fixed by moving the adapter into `catalyst_eval/adapters/` consuming plain dicts and deleting the dependency declaration, per the evaluation review; an import-direction test and an "agents tests pass with eval uninstalled" check make it permanent.

Verified current dependency facts: eval is already clean (pydantic + numpy only); data-core is already clean (httpx, pydantic, beautifulsoup4, dotenv; lancedb correctly optional); app correctly depends on agents + data; agents is the only violator.

### C.1 Data-core internal layer DAG (amended — named edges, binding)

Allowed edges (anything not listed is forbidden):

- `domain` → (nothing internal): financial schemas, calendar, precedence, dedup import no other layer;
- `ingestion` → `domain`: connectors/planner/pipeline/ledger normalize into domain schemas;
- `corpus` → `domain`: eligibility/chunking read canonical domain records;
- `corpus` → `ingestion` is forbidden by default; the single permitted exception is a small, documented artifact contract (reading certified-snapshot/coverage metadata produced by ingestion) and it must be registered as a grandfathered/justified edge, not imported ad hoc;
- `retrieval` → `corpus` and `retrieval` → `domain` (types only): retrieval consumes chunk manifests and domain types;
- `ingestion` → `retrieval` is forbidden; `retrieval` never performs canonical-domain writes (read-only against the Canonical Domain Store; its only writes are index-side artifacts).

### C.2 Agents internal layer DAG (amended — named edges, binding)

- `core` → (nothing internal): typed state/output/failure contracts, provider-agnostic protocols, retry/backoff, budget, and cost-accounting primitives;
- `trace` → `core` only (artifact subsystem; no upward imports);
- `attribution` (Context Builder, taxonomy, gates, rubric, prompts, nodes) → `core`, `trace`, and `catalyst-data`;
- `workflow` (graph assembly and deterministic routing) → `attribution`, `core`, `trace`;
- `runtime` (runner, service, status, `assurance/`) → `workflow`, `core`, `trace`;
- no layer imports `catalyst_eval` (package-level prohibition);
- concrete model/provider wiring (Anthropic/OpenAI clients, BYOK) lives in `catalyst-app`, injected through the ModelClient protocol.

This direction matches the real assembly responsibility: workflow imports and wires attribution nodes; attribution nodes never import graph assembly. Shared execution primitives live in `core` specifically to avoid an `attribution ↔ workflow` or `attribution ↔ runtime` cycle.

**The layering test enforces these named edges**: it loads the declared edge list, walks imports, and fails on any edge not in the list. Grandfathered legacy edges are registered per §O with owner and removal condition — unknown edges fail immediately.

## D. Reuse-value matrix for existing modules

| Module (verified present) | Class | Notes |
|---|---|---|
| data-core: retry, rate_limiter, fallback, error_taxonomy | **Reusable kernel** | already provider-agnostic in shape |
| data-core: update_planner (zero-write plan, plan hash), update_pipeline | **Reusable kernel** | deterministic planning is the flagship primitive |
| data-core: storage/sqlite (migrations, checkpoints), quality | **Kernel with domain residue** | checkpoint/run-state generic; table schemas financial |
| data-core: forthcoming request ledger, raw store, normalized_provenance, ChunkProfile, CorpusManifest, reconciliation, FTS5/dense/RRF/reranker adapters | **Reusable kernel (to be built)** | per provenance/chunking design |
| data-core: connectors (polygon, finnhub, fmp, fred, sec, yfinance_fallback), trading_calendar, OHLCV precedence, articles/filings/macro schemas, cik_map, dedup | **Domain-specific** | correct and expected; these are the adapters |
| agents: graph assembly, decision_router, backoff, cost_tracker, runtime/ (runner, service, status), trace/ (writer, artifacts, projection, schema), state typing pattern | **Reusable kernel** | trace subsystem is the strongest reusable asset in the repo |
| agents: Critic/Judge prompts, hypothesis taxonomy, forthcoming Context Builder, gates, ranking rubric, retrieval policy layers | **Domain-specific** | attribution logic |
| agents: adapter.py | **Misplaced** | moves to eval |
| eval: judge cache (post model-identity fix), forthcoming MetricRecord/ResultPack/rank metrics/replay/gates | **Reusable kernel** | per evaluation review |
| eval: legacy metrics, golden_set fixtures, three-arm harness | **Domain/legacy** | dev fixtures and superseded metrics; quarantined |
| app: workbench_store, routers, workspace projection, BYOK wiring | **Domain-specific app** | never a kernel; model/provider wiring correctly lives here |

Answer to question 2 in one line: the genuinely reusable material is *planning/provenance, retry/limit, chunk/corpus/retrieval interfaces, the trace subsystem, deterministic routing/budget, and the metric/pack/replay machinery* — everything else is (correctly) Catalyst's financial domain.

## E. Final data-core package design

Target internal layout (four layers, import direction strictly downward — ingestion and corpus may import domain; nothing imports upward):

1. `ingestion/` — connector protocol + ProviderRequest/ProviderResponse, request ledger, raw payload store, retry/rate-limit/fallback, planner + plan_hash + PlanDriftError, pipeline, checkpoints.
2. `domain/` — financial schemas (articles, filings, macro, OHLCV), trading calendar, source precedence, dedup, cik_map, normalization per provider.
3. `corpus/` — eligibility, ChunkProfile protocol + news_v2/filing_v2, CorpusManifest, index reconciliation/tombstones, certified snapshot/export.
4. `retrieval/` — FTS5 lexical, dense interface, RRF fusion, reranker adapter, lancedb_store (optional extra), retrieval result types with per-stage scores.

Public API (question 3): plan/execute update; read canonical records (articles/news query with provenance fields, OHLCV, filings, macro); export/certify snapshot; build/reconcile corpus + manifest; retrieve (lexical/dense/hybrid/rerank) under an explicit cutoff; provider capability report. Everything else is internal. Migration to this layout per §M (shims, no bulk rewrite).

## F. Final agents package design

Target layout: `core/` (typed state/output/failure contracts, Protocols, retry/backoff, budget, and cost primitives), `workflow/` (graph assembly and deterministic routing), `runtime/` (runner, service, status, **assurance/** per §H), `trace/` (writer, artifacts, projection, schema), and `attribution/` (Context Builder, hypothesis taxonomy + gates, ranking rubric, prompts, nodes). Existing `state.py`, `backoff.py`, `cost_tracker.py`, and `nodes/` remain physically where they are until an already-scheduled slice touches them; target-layer re-export shims preserve compatibility. Retro-splitting working nodes into generic framework/domain packages is rejected; new financial logic lands in `attribution/` from the start.

Protocol surface (question 3): ContextProvider, Retriever, Reranker, ModelClient, TraceSink — defined as minimal Protocols in agents, implemented by data-core adapters and app wiring. **Dependency amendment (verified defect-adjacent):** `langchain-anthropic` and `langchain-openai` are hard dependencies of agents today; both move to optional extras (`[anthropic]`, `[openai]`) with the concrete client wiring staying in app's model layer — agents' core must import neither. LangGraph remains a core dependency (it is the workflow engine, honestly named).

## G. Final eval package design

As ratified in the evaluation architecture review, restated as package structure: `benchmark/` (BenchmarkCase, lineage validators, splits), `metrics/` (rank metrics and deterministic grounding checks), `judges/` (cache with model identity, governance, audit sampling), `adapters/` (the relocated state-dict adapter; trace reader against the documented trace artifact schema), `packs/` (MetricRecord, ResultPack, manifest validation), `replay/` (zero-network replay), `reports/` (Markdown/JSON scorecards), and `gates/` (zero-network regression gate). B1 deleted stale thesis-era tests and scripts rather than preserving a fake active `legacy/` suite; legacy schemas and fixtures remain readable only through explicit compatibility adapters. The restructure also deletes the unused `ragas` optional extra from eval's pyproject unless a currently executable path requires it. Agent-agnosticism: eval consumes four *artifact contracts* — trace DB schema, plain state dicts, assurance records, and packs — and imports no agents code; the trace schema doc is version-stamped so drift fails loudly in the trace reader.

## H. Runtime assurance ownership decision (question 4)

**RunAssuranceRecord and the deterministic Surface-B checks live in `catalyst_agents/runtime/assurance/`.** Grounds: assurance must execute inside every run including production-shaped ones, so it belongs to the runtime that owns the run lifecycle; its inputs (state, trace events, evidence pack, manifest identities, cutoff) are all available to agents, which already depends on data-core for cutoff/corpus identity; putting it in eval would either reverse the dependency direction or force the app to orchestrate checks it doesn't own; a neutral contracts module for one record type is premature (§ next). Eval *re-verifies* persisted assurance records when packaging results — a consumer, not the owner. The record schema is documented alongside the trace schema as an artifact contract.

**Question 5 — fourth `catalyst-contracts` package: rejected as premature.** The inventory of genuinely cross-package types is three result-schema classes (now solved by the eval adapter) plus two artifact schemas (trace, assurance — solved by documentation-versioned contracts). A fourth pyproject for that is packaging ceremony. Re-entry condition in §Q.

## I. App/workbench scope cut (question 12)

Reframed as **local Developer Workbench / Demonstration Console** with exactly the five surfaces proposed (Data / Explore / Attribution / Trace / Evaluation-as-files). Removed from all plans and definitions of done: user accounts, sessions, billing, multi-tenancy, hosted telemetry, admin dashboards, enterprise auth/RBAC, external rate limiting, production deployment hardening, online feedback learning, user analytics, cloud monitoring, and public SaaS deployment. Retained: BYOK local key wiring (it is local model configuration, not SaaS), run integrity line, news provenance UI per the ratified news contract, trace view. The frontend remains a late phase and must not block backend completion; the Evaluation surface ships as Markdown + JSON files, no UI.

### I.1 Local BYOK security boundary (amended — binding)

Removing SaaS hardening does not remove local secret safety. The workbench must: bind loopback only by default (127.0.0.1 / ::1) and reject non-loopback binds without an explicit unsafe override; enforce Host and Origin allowlists with CORS closed by default; accept key-bearing requests as JSON only; carry a local per-process nonce (or equivalent CSRF/DNS-rebinding defense); hold credentials in memory only — no persistence, no response echo, and no appearance in logs, traces, errors, configs, reports, or artifacts. Verification is behavioral (unknown Host rejected; unknown Origin rejected; non-loopback default rejected; persistence/echo/redaction tests) — never a brittle assertion that auth/session modules do not exist.

## J. Public APIs and extension points (amended — exact enumeration)

Stability classes: **stable** (implemented, tested, and semver-respected after contract freeze), **internal** (no compatibility promise), **proposed** (named here but not yet frozen; an existing partial implementation does not make the contract stable). Every entry below remains `proposed` until its implementation, fixture tests, and contract-freeze gate land; the table may then be amended entry by entry to `stable`.

**catalyst-data public API:**

| Entry point (signature → output) | Class |
|---|---|
| `plan_update(universe, window, config) → UpdatePlan` (with `plan_hash`; raises `PlanDriftError` on drift) | proposed (planner exists; full drift contract pending) |
| `execute_update(plan, confirm) → RunReport` | proposed (two-stage form lands with B2) |
| `query_articles(ticker, window, source_types, pagination) → list[ArticleRecord]` (full provenance fields) | proposed (replaces legacy `list_news` path) |
| `query_ohlcv(ticker, window) → list[OHLCVBar]` | proposed façade over existing storage/query paths |
| `certify_snapshot(db_path) → SnapshotCertificate` / `export_snapshot(...) → ExportManifest` | proposed |
| `build_corpus(snapshot, profiles) → CorpusManifest` / `reconcile_index(manifest) → IndexReport` | proposed |
| `retrieve(query, cutoff, filters, mode) → RetrievalResult` (mode ∈ lexical/dense/hybrid/reranked; per-stage scores; cutoff mandatory) | proposed façade over existing paths |
| `provider_capabilities() → CapabilityReport` | proposed |

Extension points: `Connector` protocol (+ ProviderRequest/ProviderResponse), `ChunkProfile` protocol, Retriever/Reranker adapter interfaces. Everything else in data-core is internal.

**catalyst-agents public API:** `run_attribution(request, deps) → AttributionRun` (result + trace id + RunAssuranceRecord); `replay_run(run_id | artifacts) → ReplayReport` (proposed); protocols `ContextProvider`, `Retriever`, `Reranker`, `ModelClient`, `TraceSink`; typed `AttributionState`/output/failure contracts; `RunAssuranceRecord` schema. Internal: nodes, prompts, graph assembly, router internals.

**catalyst-eval public API:** `BenchmarkCase` schema + loaders; `Metric` interface; `MetricRecord`/`ResultPack` schemas + validators; `evaluate_pack(cases, run_artifacts, config) → ResultPack`; `generate_scorecard(pack) → Markdown` (deterministic); `run_regression_gate(pack_dir)` (zero-network). Internal: judges, adapters, and compatibility readers.

**catalyst-app:** HTTP routes only; no public Python API.

Rejected as too broad/aspirational: a generic `Pipeline`/`Framework` facade, plugin registries, dynamic connector discovery, a universal `Dataset` abstraction — none has a second consumer. Each retained protocol gets: a docstring contract, one shipped implementation, one synthetic-fixture test demonstrating a third-party implementation, and a docs section — that combination is what makes an extension point real rather than aspirational.

## K. Contributor quickstarts and examples (questions 10–11)

One command each, all runnable offline against committed synthetic fixtures, none requiring API keys:

1. **data-core:** plan an update against the bundled fixture DB and print the zero-write plan + plan hash; second example: build the corpus for the fixture and run one FTS5 query under a cutoff.
2. **agents:** run one attribution over the fixture snapshot with the stub ModelClient (deterministic canned responses), emitting a full trace + assurance record; open the trace with the projection tool.
3. **eval:** score a committed recorded run pack and regenerate the Markdown scorecard byte-identically; run the zero-network regression gate.
4. **app:** launch the workbench against the fixture snapshot and walk Data → Explore → Attribution → Trace.

Fixture set (to be built once, small): ~2 tickers × ~15 sessions OHLCV, ~20 sanitized articles with provenance fields, 1 filing, one benchmark case — enough for every quickstart and most tests. Genuine contribution surfaces to advertise: new provider connector (protocol + ledger + fixtures), new ChunkProfile, alternative reranker adapter, new deterministic metric, trace-viewer improvements. These are real because each has a protocol, a reference implementation, and a fixture harness.

## L. Documentation/README architecture (questions 9, 13)

**B1 documentation correction:** active READMEs use the real graph, list exactly the six implemented providers, use functional data terminology, describe the system as a multi-stage LLM workflow, and contain no agents→eval dependency claim.

Narrative structure (binding for the root README): what Catalyst is (a deliberately bounded open-source local workbench; the architect's first serious agent/RAG project) → what it demonstrates (the ten capabilities, each linking to the module and a test) → what it is not (no causal claims, no product, no multi-agent, not a ChatGPT competitor) → architecture diagram matching the real graph → quickstarts → evaluation scorecard table with denominators → limitations and honest negative results → roadmap → contribution guide. Per-package READMEs follow the same shape one level down, each with its kernel-vs-domain map from §D. No superlatives; every capability claim carries a pointer to code and a test — that is the admissions/interviewer register that survives questioning.

## M. Migration strategy with minimal churn (questions 6–7)

Rules: **target layout is declared now; physical moves are opportunistic; no bulk rewrite commit.** (1) Declare §E/§F layouts in the architecture doc and per-package READMEs. (2) New modules land in their target subpackage from day one (ledger → ingestion/, chunk profiles → corpus/, FTS5 → retrieval/, Context Builder → attribution/, assurance → runtime/assurance/). (3) Existing modules move only when a scheduled slice already touches them, leaving a one-line re-export shim at the old path for one release; shims carry a deprecation note and a test. (4) A layering test (new) asserts no upward imports across declared layers and fails on *new* violations without blocking grandfathered paths listed in an explicit allowlist that must only shrink. (5) The adapter move + dependency removal is its own small slice and lands first (it unblocks the layering test). (6) Nothing in this migration changes behavior; every move commit is mechanically verifiable (same tests green before/after).

## N. Git boundaries

1. `fix(agents): remove eval dependency` — adapter relocation, pyproject edit, import-direction + eval-uninstalled tests.
2. `docs(architecture): adopt package kernel boundaries` — this proposal ratified + README corrections (§L) + terminology fixes.
3. `refactor(data-core): declare layered package layout` — subpackage skeletons, layering test, allowlist, first shims.
4. `refactor(agents): add attribution and assurance layers` — skeletons + assurance record schema (implementation lands with its runtime slice).
5. `feat(eval): restructure evaluation package` — per evaluation review boundaries.
6. `test(fixtures): add synthetic contributor fixtures` — fixture DB + articles + stub model + quickstart tests.
7. `chore(agents): move model clients to extras` — langchain extras split, app wiring confirmed.
Ordering: 1 → 2 → (3, 4, 7 parallel) → 5 → 6. All under the standing rules: stage only on authorization, no mixed boundaries, both DB SHAs checked per boundary.

## O. Landmine tests

1. Import-direction: any `catalyst_eval` import under `catalyst_agents` fails; agents' suite passes with eval uninstalled.
2. Layering (amended): the test enforces the named allowed-edge lists of §C.1/§C.2; any import edge not in the allowed list and not in the grandfather registry fails immediately. Every grandfathered edge is registered with: the exact import edge, the reason, the owning slice, and its planned removal condition — no count-vs-previous-commit comparison is used.
3. Extras: `import catalyst_agents` succeeds with neither langchain-anthropic nor langchain-openai installed; only app requires concrete clients.
4. Shim integrity: every re-export shim imports the moved module and warns; removing a shim before its deprecation window fails the test.
5. Assurance ownership: assurance record is produced by the runtime for a stub-model run with eval uninstalled.
6. Trace artifact contract: eval's trace reader rejects a trace DB whose schema version it does not know (loud drift, no silent misparse).
7. Fixture quickstarts: all four §K quickstarts run offline in CI with networking disabled.
8. README honesty (amended — reject active false claims, not raw words): fails if the *active architecture diagram* contains a nonexistent "Parser → RetrievalPolicy" stage; if the *active provider table* lists GDELT while no GDELT connector exists; if *active package terminology* describes current layers as Bronze/Silver/Gold instead of the canonical functional names; or if *active positioning text* calls Catalyst a multi-agent system. Historical migration notes and negative statements ("Catalyst is not multi-agent"; "formerly called Bronze") remain valid and must not trip the test.
9. App local security (amended — behavior, not module absence): unknown Host header rejected; unknown Origin rejected; non-loopback bind rejected by default; a submitted key never appears in any persisted file, response body, log line, trace event, or artifact (redaction tests with a sentinel key).

## P. MUST / SHOULD / DEFER

| Tier | Items |
|---|---|
| MUST | B1 boundary fix; README/terminology corrections (§L); layered layouts declared + named-edge layering test (§C.1/C.2) + shims policy; assurance in runtime (§H); app SaaS-requirement removal + local BYOK security (§I/§I.1); artifact contracts (trace + assurance schemas) documented and versioned; eval restructure per evaluation review; **synthetic fixture set (one committed fixture DB: ~2 synthetic tickers, ~15 trading sessions, ~20 sanitized/synthetic articles with provenance, 1 small filing, 1 benchmark case, deterministic stub ModelClient) + the three key-free offline quickstarts (data: plan/zero-write/plan-hash/corpus/cutoff-safe lexical query; agents: stub-model attribution with trace + RunAssuranceRecord + replay; eval: score fixture pack, regenerate scorecard deterministically, run the zero-network regression gate) + network-disabled tests** |
| SHOULD | app/browser fixture walkthrough (fourth quickstart); langchain extras split (N7); protocol docs with third-party-implementation fixture tests; per-package kernel/domain maps in READMEs |
| DEFER | any physical bulk moves beyond opportunistic slices; contribution-surface marketing (until fixtures exist); frontend workbench surfaces (existing phase order unchanged); any packaging/publishing work (PyPI etc.) |

## Q. Re-entry rules for future package extraction (question 14)

A kernel may be extracted into a standalone open-source package only when **all** hold: (1) a second real consumer exists — another project of the architect's or an external user with a concrete, stated use case (an interview talking point is not a use case); (2) the kernel's public API has survived ≥ 2 months of Catalyst development without Catalyst-driven breaking changes; (3) the extraction has a named maintenance budget (issue triage, releases) that does not tax the flagship-project timeline; (4) the extracted package can test itself without Catalyst fixtures. Until then the honest formulation — used in READMEs and interviews — is "**extractable by design, deliberately not extracted**," with the layering test as the evidence. Extraction performed for portfolio appearance is rejected permanently, not deferred.

## R. Exact amendments required in canonical design and roadmap

1. Workbench design §4 (system boundaries): append the §C dependency graph as binding; record the agents→eval fix as a named Phase-1 slice; note langchain extras split.
2. Workbench design §4.4 + §7 Phase 6: replace "public HTTP hardening" with local-workbench polish; delete SaaS-shaped requirements per §I; the five-surface workbench framing becomes canonical.
3. Workbench design §3 (terminology): add the README-correction obligations (§L) to the terminology migration; the data-core README medallion rewrite joins the docs boundary.
4. Roadmap Phase 1: insert N1 (boundary fix) before the eval restructure; N3/N4 skeleton slices attach to already-scheduled data-core/agents work; no new phases.
5. Evaluation architecture review §J: superseded in one detail — assurance ownership is resolved to agents/runtime (this proposal §H); its "either location satisfies the rule" open point is closed.
6. Git management plan: append boundaries N1–N7 to the boundary table.
7. Provenance/chunking design: no changes; its new modules land directly in the §E target layout (ingestion/, corpus/, retrieval/).

---

Integrity: review conducted read-only; Git index empty before and after (`git diff --cached` = 0 lines); this proposal is the only new artifact and is untracked; no runtime code, dependencies, databases, or canonical designs modified; Dev DB SHA `92731fb7c5c3e989b4fdcbefb9d1d2060974082a6c60f1bbecad6bc42ee846b0` and Frozen DB SHA `0d97a7ec61b6ec8fb5f9263b0b37b0efc9755812739d7afead7f720c3567e8dd` unchanged.

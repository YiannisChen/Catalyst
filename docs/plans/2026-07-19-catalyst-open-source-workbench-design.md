# Catalyst Open-Source Attribution Workbench — Canonical Design

- Date: 2026-07-19
- Status: **final and ratified** (consolidation amendment, 2026-07-19: final identity/audience, package dependency graph, evaluation surfaces and case counts, Toy Exit Gate, reviewer journey, local workbench security — §1, §4.4, §6, §13–§15; correction amendment, 2026-07-21: source/evidence taxonomy §4.5, dedup/novelty separation §4.5, peer-news policy §4.6, app-contract repair scope §7 item 7, required external data operation §7 item 9, evaluation implementation order §6.4, Core Exit Gate / Showcase Complete split §14, attribution decision-record requirement §11 item 6)
- Canonical database identities: Dev DB is `data/catalyst_dev_ws4b.db` (SHA `92731fb7c5c3e989b4fdcbefb9d1d2060974082a6c60f1bbecad6bc42ee846b0`); frozen artifact is `data/catalyst_eval_frozen_v2.db` (SHA `0d97a7ec61b6ec8fb5f9263b0b37b0efc9755812739d7afead7f720c3567e8dd`). The similarly named `data/catalyst_dev.db` is not the canonical Dev DB. Wherever this document family cites a DB SHA, it refers to these paths.
- Scope: a deliberately bounded, credible open-source project; not a graduation thesis and not a research-paper program
- Supersedes: the binding scope and positioning decisions in the 2026-07-17 benchmark design and the 2026-07-18 full evaluation design
- Subordinate binding designs: `2026-07-19-catalyst-provider-provenance-and-chunking-design.md` (data/retrieval), `2026-07-19-catalyst-final-package-architecture.md` (packages, ratified), `2026-07-19-catalyst-evaluation-architecture-review.md` (evaluation, binding as amended)
- Git rule: stage only; no commit or push without architect authorization

This document is the single design authority for the 60% milestone. Earlier roadmap, product, benchmark, and evaluation documents remain historical inputs only where this document explicitly references them. New implementation plans must conform to this document; conflicts are resolved in favor of this document.

## 1. Product decision

Catalyst is an open-source, evidence-bounded financial-attribution workbench that demonstrates reliable data ingestion, temporal retrieval, reranking, structured LLM workflow orchestration, abstention, traceability, and engineering evaluation in one understandable system.

The financial scenario is a demanding case study, not a claim that Catalyst is a better general financial analyst than frontier products with web search. Catalyst must not promise causal truth, trading advice, prediction, Bloomberg-level source breadth, or superiority over ChatGPT or Claude.

The project target is intentionally **60/100**:

- complete enough that another engineer can install it, update supported data, inspect a chart and its news, run one attribution, and understand the trace;
- rigorous enough that retrieval, reranking, and workflow behavior are measured rather than asserted;
- small enough to finish and use as evidence of systems judgment, honest scoping, testing, and open-source contribution readiness;
- not optimized for a thesis defense, publication, statistical novelty, or exhaustive financial coverage.

README one-liner:

> Catalyst is an open-source workbench for replayable, evidence-bounded financial attribution using temporal retrieval, reranking, and a traceable LLM workflow.

### 1.1 Final identity and audience (amended, binding)

One-sentence definition: **Catalyst is the architect's first serious RAG/agent systems project — a deliberately bounded open-source local workbench that demonstrates provider ingestion with request provenance, deterministic planning, cutoff-safe lexical/dense/hybrid retrieval, reranking, a traceable LLM workflow with validation and abstention, component-level evaluation, run assurance, and zero-network replay, using financial attribution as a demanding case study.**

Audience hierarchy: (1) open-source contributors and technical reviewers; (2) Fall 2027 master's admissions reviewers for AI systems / AI platforms / agent infrastructure / backend-distributed systems / reliable RAG programs, and engineering interviewers evaluating practical AI systems judgment; (3) the architect, as a learning and portfolio artifact.

Explicit non-users and non-identities: Catalyst is not a SaaS product, multi-user financial-analysis service, trading platform, investment-advice system, Bloomberg/Reuters/FT replacement, ChatGPT/Claude competitor, production enterprise platform, research paper, graduation thesis, generic multi-agent framework, universal RAG framework, or RL/fine-tuning project. Retail investors, financial institutions, paying users, and production analyst teams are not the audience.

Every design and scope decision is judged by one question: *can a contributor, admissions reviewer, or interviewer understand, run, and trust this system in one sitting?* — never by "can this scale to thousands of users."

## 2. User-visible workflow

1. The operator updates OHLCV and news for the configured universe.
2. Catalyst previews the plan, expected request scope, estimated duration, and provider limitations before confirmation.
3. The update service persists normalized market and news records, records progress and errors, and later synchronizes eligible text into the retrieval index.
4. The analyst selects a ticker and date/range. The workbench displays the K-line chart and the matching news timeline.
5. Each news card shows its publisher, publication time, image when available, title, summary, and original-source link.
6. The analyst requests attribution. Catalyst constructs market, sector, peer, company, and macro context under an explicit information cutoff.
7. Retrieval finds candidate evidence; reranking selects the most useful evidence; the workflow generates and challenges competing hypotheses.
8. The response ranks supported explanations, exposes supporting and counter-evidence, states unavailable evidence, and abstains when the evidence is insufficient.
9. The analyst can inspect the evidence set, retrieval/reranking trace, workflow decisions, latency, and model/provider identity.

The frontend remains a later roadmap phase, but the backend API and data contracts required by this workflow are part of the backend definition of done.

## 3. Canonical terminology

Active documentation, new APIs, schemas, code identifiers, and UI copy use functional names. Medallion jargon and evaluation-specific “gold” language are not used for product concepts.

| Legacy term | Canonical term | Meaning |
|---|---|---|
| Bronze | **Raw Source Archive** | Immutable or append-only provider payloads and ingestion provenance |
| Silver | **Canonical Domain Store** | Normalized articles, ticker links, OHLCV, filings, macro observations, and run state |
| Gold / Gold corpus | **Retrieval Corpus** | Versioned text chunks eligible for retrieval |
| Vector DB / Gold index | **Retrieval Index** | Embeddings and retrieval metadata derived from a corpus manifest |
| Frozen DB | **Certified Snapshot** | Read-only dataset fixture or certified corpus snapshot with a pinned identity |
| Golden Set | **Benchmark Cases** | Human-reviewed cases used for engineering validation |
| Gold evidence | **Reference Evidence** | Human-reviewed relevant evidence IDs for a benchmark case |
| GoldenEvent | **BenchmarkCase** | Future schema/class name |

Existing database tables such as `raw_assets`, `articles`, `article_tickers`, `ohlcv`, and `index_state` already use appropriately functional names and should not be renamed merely for aesthetics. Historical ADRs and result artifacts remain unchanged; they receive a legacy terminology note when touched. Renaming paths or public classes is a separate compatibility migration with aliases and deprecation tests, not a bulk search-and-replace.

## 4. System boundaries

### 4.1 Data Core

Responsibilities:

- provider connectors and provider-capability reporting;
- zero-write planning and explicit update confirmation;
- calendar-aware OHLCV planning and two-stage OHLCV-first update execution;
- normalized article, ticker, filing, macro, and price storage;
- idempotency, source precedence, checkpoints, retries, rate-limit handling, durable run state, cancellation, and resume;
- data completeness certification and deterministic export;
- incremental retrieval-corpus and retrieval-index synchronization.

The update unit is one logical run. OHLCV updates first because chart range and trading sessions define the evidence window; news and other evidence update second against the refreshed watermark. A successful data update is not equivalent to a synchronized retrieval index. Both states are exposed separately.

### 4.2 Retrieval

Provider-request provenance, normalization boundaries, and source-specific chunking are governed by `docs/plans/2026-07-19-catalyst-provider-provenance-and-chunking-design.md`.

Retrieval is layered and replaceable. The lexical baseline is a required Phase 2 implementation rather than a description of current Mac capability:

1. metadata filters enforce ticker scope and `available_at <= cutoff`;
2. lexical retrieval provides a cheap deterministic baseline;
3. dense retrieval adds semantic recall when the server index exists;
4. hybrid fusion combines lexical and dense candidates;
5. a cross-encoder reranker orders the candidate set for final evidence selection.

Every result retains article/chunk identity, source URL, timestamps, scores by stage, and the corpus/index manifest identity. SQL fallback is an explicit degraded mode, never silently merged with vector results.

### 4.3 Attribution workflow

The workflow borrows the strongest reproducible parts of the observed GPT/Claude analyses without pretending to reproduce their proprietary search breadth.

Required reasoning contract:

1. **Identity and time boundary:** verify security, session, timezone, analysis cutoff, and whether the move is intraday or close-to-close.
2. **Price facts:** compute the target move, volume context, and data-quality status.
3. **Cross-sectional context:** compare market benchmark, sector benchmark, and selected peers/suppliers/customers where data exists.
4. **Evidence timeline:** order company, peer, sector, macro, filing, and official-source events by when they became available.
5. **Novelty check:** distinguish newly available information from older facts that may already be priced in.
6. **Competing hypotheses:** generate market, sector, earnings/guidance, product/demand, legal/regulatory, macro, peer propagation, supply-chain propagation, mixed, and unexplained candidates only when their prerequisites exist.
7. **Transmission mechanism:** explain how each candidate could affect revenue, cost, valuation, risk premium, positioning, or sentiment; do not infer direction from a relationship edge alone.
8. **Falsification:** attach supporting evidence, counter-evidence, missing evidence, and a condition that would change the conclusion.
9. **Ranking and abstention:** rank the remaining hypotheses deterministically or with an auditable rubric; permit mixed and unexplained outcomes; abstain when minimum evidence gates fail.
10. **Communication:** separate verified facts, system calculations, model inference, and unavailable/private information.

The confirmed minimal mapping is:

- a new deterministic **Context Builder** runs before Miner and produces session/cutoff resolution, target price facts, benchmark/sector comparisons, peer moves, data-quality status, and degradation flags;
- Context Builder depends on an injected context-provider protocol backed by data-core pure functions; the agent node does not open SQLite directly;
- Miner performs cutoff-safe retrieval and reranking;
- Critic remains one LLM call for evidence grading and sufficiency; it is not split;
- DecisionRouter invokes a pure timeline/novelty projection over graded evidence before routing to Judge;
- Judge owns competing hypotheses, transmission mechanisms, supporting/counter-evidence, missing evidence, change conditions, and fact/inference separation in a versioned output schema;
- Validator checks evidence IDs, prerequisite gates, required fields, cutoff compliance, and output structure; its exceptional repair call remains bounded;
- Finalizer invokes a pure ranking/abstention rubric and assigns final status.

No separate Falsifier node is added at 60% scope. It re-enters only if at least two of the eight answerable workflow cases retain unsupported material claims after the strengthened Judge/Validator contract. Expected LLM calls remain Critic + Judge; Validator repair and bounded retrieval expansion are exceptional paths.

Hypothesis gates are deterministic:

- market and sector candidates require their corresponding session OHLCV;
- earnings/guidance, product/demand, legal/regulatory, and macro candidates require a matching pre-cutoff evidence item or explicitly modeled event;
- peer or supply-chain propagation requires a versioned relationship edge, same-session peer context where available, and a pre-cutoff evidence item describing the peer event; an edge plus co-movement is insufficient;
- novelty uses `cluster_first_available_at` — the minimum valid pre-cutoff `available_at` across the dedup cluster — relative to the prior session boundary; the presentation representative of a cluster never changes its novelty time (§4.5);
- “price-in” is only a flag that prior sessions existed for reaction, never proof of market expectations;
- missing target OHLCV, failed data quality, zero graded evidence, or all candidate gates failing forces abstention;
- `mixed` and `unexplained` are valid outcomes distinct from abstention.

The current node graph remains a multi-stage LLM workflow, not a multi-agent system. Improving typed contracts is preferred over adding personas.

### 4.4 App: Local Developer Workbench / Demonstration Console (amended)

`catalyst-app` is a **local developer workbench and demonstration console**, not a hosted product. The internal API exposes stable domain contracts for chart data, news, attribution requests, run status, evidence, and traces. It must stop reconstructing modern news records from legacy `clean_assets` markdown when canonical `articles JOIN article_tickers` data is available.

Required conceptual surfaces (frontend remains a later phase; the backend contracts for all five are part of the definition of done):

1. **Data** — preview update plan, plan hash, execute/update status, provider capabilities, corpus/index identity.
2. **Explore** — K-line chart, news timeline, original source links, publisher/image metadata, article provenance.
3. **Attribution** — ranked hypotheses, transmission mechanisms, supporting and counter-evidence, unavailable evidence, explicit abstention, run-integrity line.
4. **Trace** — lexical/dense/RRF/reranker stages, Critic/Judge/Validator path, deterministic gates, model/prompt/manifest identities, latency/token/cost, degraded/fallback states.
5. **Evaluation** — Markdown + JSON scorecards only; no dedicated evaluation UI in the 60% milestone. Benchmark metrics never appear on normal surfaces; only run-integrity facts (Surface B) are user-visible.

Removed from the roadmap entirely: accounts, billing, multi-tenancy, hosted telemetry, admin dashboards, enterprise auth/RBAC, production deployment hardening, online feedback learning, user analytics, cloud monitoring platforms, and public SaaS deployment.

**Local security boundary (binding — removing SaaS hardening does not remove local secret safety):** the workbench binds loopback only by default (127.0.0.1 / ::1) and rejects non-loopback binds without an explicit unsafe override; enforces a Host allowlist and an Origin allowlist with CORS closed by default; accepts key-bearing requests as JSON only; uses a local per-process nonce (or equivalent) as CSRF/DNS-rebinding defense; holds BYOK credentials in memory only — never persisted, never echoed in any response, and never written to logs, traces, errors, configs, reports, or artifacts. These are behavioral requirements verified by behavior tests (unknown Host rejected; unknown Origin rejected; non-loopback default rejected; key persistence/echo/redaction tests), not by asserting the absence of modules.

### 4.5 Source/evidence taxonomy and dedup/novelty contract (amended 2026-07-21, binding)

Catalyst classifies every evidence source by **data role first, publisher identity second**. The full class definitions, classification algorithm, and manifest versioning are bound in the provenance/chunking design; the seven classes are:

1. `structured_market_data` — Polygon/yfinance OHLCV; supports deterministic price/volume calculations only; can never establish an event narrative or causal explanation;
2. `official_government` — SEC, FRED; supports official filing/macro facts;
3. `issuer_disclosure` — issuer-hosted filings or future direct IR releases; proves that the issuer disclosed something; issuer claims remain issuer claims unless independently corroborated;
4. `corporate_press_release` — GlobeNewswire and identified PR/Business Wire content; primary evidence of the announcement, not independent verification of its statements;
5. `reported_news` — CNBC, MarketWatch, identifiable original reporting; may support event narratives;
6. `analysis_opinion` — The Motley Fool, Seeking Alpha, Zacks, ChartMill, Fintel; may generate and support hypotheses, but can never be presented as independently corroborated when it is the only supporting class;
7. `aggregated_unknown` — Yahoo, Finnhub-tagged, or unrecognized publishers whose originating source cannot be resolved; never silently treated as opinion or official evidence; emits an origin-unknown flag.

The taxonomy affects exactly three things and nothing else: (1) evidence-pack dedup representative selection; (2) Judge evidence labels; (3) three deterministic assurance flags — `opinion_only_support`, `unknown_origin_support`, `issuer_claim_only_support`. No source-quality scores, no hardcoded financial truth weights, no learned ranking.

**Dedup identity, representative, and novelty are three separate bound fields:**

- `dedup_cluster_id` — cluster identity;
- `cluster_first_available_at` — the minimum valid pre-cutoff `available_at` across cluster members; the only input to novelty/timeline logic;
- `representative_document_id` — selected independently for presentation and Judge input; preference may consider identifiable origin and source class.

Choosing a later official or better-described representative must never rewrite novelty time. All cluster members retain provenance. One cluster contributes at most one default evidence-pack item. Benchmark records retain both the representative and the cluster identity. A landmine test proves that changing a cluster's representative does not change its `cluster_first_available_at`.

### 4.6 Peer-news policy (amended 2026-07-21, binding)

Peer news is **not zero marginal cost**: every peer ticker adds provider calls, rate-limit consumption, update cells, raw storage, canonical records, and embedding/index cost. Peer coverage is therefore bound to: a versioned peer/relationship manifest; an explicit ticker tier; a rolling coverage window; a per-run request budget included in the plan hash; the provider capability report; and a separately authorized initial backfill.

**Core Exit Gate peer policy — Option A: Polygon peer news only; Finnhub remains target-only.** Grounds (measured in the Dev DB): 73% of the current Finnhub corpus is Yahoo-publisher aggregated content that substantially duplicates coverage, so extending Finnhub to peer tickers roughly doubles Finnhub request cells for mostly `aggregated_unknown`-class material; Polygon articles already carry multi-ticker associations (41% of Polygon articles link more than one ticker), so peer events involving the target are partially captured at zero additional request cost, and dedicated Polygon peer cells add the remainder under the budgeted tier. Finnhub peer expansion is a Showcase re-entry that requires a measured peer-evidence recall gap in the benchmark's per-case evidence tables.

## 5. News presentation contract

The database and retrieval export already retain `article_url`, `image_url`, `publisher_name`, and `publisher_logo_url`. The current app `NewsItem` and `WorkbenchStore.list_news()` do not expose them, so the PokieTicker-style news experience is **not implemented end to end today**.

Required API item:

```text
NewsItem
  article_id: string
  provider: string
  title: string
  description: string | null
  published_at: UTC timestamp
  publisher_name: string | null
  publisher_logo_url: https URL | null
  article_url: required http/https URL
  image_url: https URL | null
  image_alt: string
  tickers: string[]
  source_type: registered article/news source type
  dedup_cluster_id: string | null
  relevance: optional attribution metadata, separate from provider content
```

Backend rules:

- query `articles JOIN article_tickers`; do not parse title/source from markdown;
- include every registered article/news source type, including current Polygon and Finnhub articles; the existing Polygon-only filter is a defect;
- keep SEC filings in their filing domain unless a later explicit materialization contract creates article records;
- preserve the original provider URL and canonicalize only for deduplication, never fabricate a source URL;
- validate schemes, reject embedded credentials and unsafe/non-web URLs, cap URL length, and never follow article redirects server-side;
- keep image metadata outside RAG text and embedding content;
- treat images and publisher logos as optional presentation fields;
- return deterministic ordering and stable article identity;
- preserve attribution/licensing metadata required by the provider.

Frontend rules:

- show image with lazy loading and a fixed aspect ratio when `image_url` is usable;
- use direct provider image hotlinks in v1 with `referrerpolicy="no-referrer"`; do not proxy or cache licensed media in the 60% milestone;
- fallback in order: publisher logo, neutral placeholder; broken images must not collapse the card;
- show publisher and publication time next to the title;
- clicking the title or explicit “Read original” action opens `article_url` in a new tab using `noopener noreferrer`;
- an interaction that highlights the article on the K-line chart must be a separate control, so chart selection does not unexpectedly navigate away;
- never imply that the linked article is freely accessible; paywalls and expired links are valid source states.

Whole-card navigation is rejected because it conflicts with chart highlighting and accessible nested controls. A read-only allowlisted image proxy may re-enter only if measured provider-image failures justify its licensing and security cost.

## 6. Engineering Evaluation Lite

Open-source projects do not need a paper-style experiment. Catalyst does need compact, repeatable evidence that its named components work.

### 6.0 Three evaluation surfaces (amended, binding)

- **Surface A — offline benchmark:** developer/contributor-only; runs against a Certified Snapshot with a pinned index; requires labels; may use the governed cached judge; **no benchmark metric ever appears in normal user UI**.
- **Surface B — per-run assurance:** deterministic, every run, no labels, no judge; produced by `catalyst_agents/runtime/assurance/`; user-visible only as run-integrity facts (cutoff enforced, citations resolve, gates respected, coverage complete/partial).
- **Surface C — optional feedback:** quarantined; never benchmark truth, never training data, never a metric input; may only nominate cases into a manual review queue whose promotion requires the full labeling protocol.

The binding metric registry, MetricRecord/ResultPack contracts, and judge governance live in `2026-07-19-catalyst-evaluation-architecture-review.md`. Exact B2–B7 formulas, state machines, migration ownership, and execution sequencing live in `2026-07-21-b2-b7-technical-contracts.md`; that contract is authoritative when an older count or formula conflicts.

### 6.1 Benchmark scope (amended — unambiguous counts)

- **MUST: exactly 12 base `BenchmarkCase` records** — 6 answerable workflow cases + 2 should-abstain cases + 4 retrieval-only cases (reference-evidence labels only; no workflow annotation or paid workflow run);
- **SHOULD: 4 derived adversarial variants** (post-cutoff evidence, distractor evidence, missing primary evidence, provider/system failure), each derived from a named base case and never counted in headline aggregates;
- **maximum Core total: 12**; additional cases and adversarial variants are Showcase work triggered only by a concrete regression;
- evidence-first labels: case facts, cutoff, answerability with expected abstention-reason class, acceptable hypothesis labels where reviewable, and `reference_evidence_ids`;
- no canonical prose answer and no use of the old model-written answerable set for headline claims;
- legacy cases remain development fixtures only.

Labeling effort budget (binding schedule anchor): initial evidence grading ≈ 12–18 hours; second adjudication pass ≈ 4–6 hours, separated from the first pass by at least seven calendar days; setup and validation ≈ 3–5 hours; total ≈ 19–29 hours.

### 6.2 Required checks

- **Retrieval:** Recall@8, nDCG@8, cutoff-violation count, primary-evidence-in-top-8, and per-case retrieved evidence table over 10 retrieval cases (6 answerable + 4 retrieval-only);
- **Reranker:** candidate-set preservation, delta primary-evidence-in-top-8/nDCG@8, median and maximum latency, and failure count;
- **Grounding:** cited-evidence validity, unsupported material-claim count, and explicit unavailable-evidence statements, split into two denominators — `structured_context_support` (deterministic: market/sector/OHLCV numeric claims must match Context Builder outputs) and `citation_grounding` (every event-narrative cause, **including market/sector event narratives**, requires cited pre-cutoff evidence; only purely numeric decomposition statements are exempt from the citation denominator);
- **Workflow:** answer/abstain correctness over 8 workflow cases (6 answerable + 2 abstain), abstain-reason correctness, budget violations, path trace completeness, and failure outcomes; expansion utility is N/A until bounded expansion is explicitly implemented and enabled;
- **Systems:** deterministic manifest identity, replay success, latency, token/cost accounting, and zero-network regression replay.

Results are engineering scorecards with raw counts, denominators, per-case records, and named-case justification for every keep/kill decision. MRR, Precision@K, source-diversity metrics, risk-coverage curves, aggregate confidence calibration, and p95 latency are omitted because they do not change a decision at this sample size. No mandatory bootstrap, McNemar test, publication-grade significance claim, large frontier-model matrix, or 3–4 week annotation program.

### 6.3 Keep/kill discipline

- retain a component if it fixes identifiable cases or improves retrieval/evidence quality at acceptable cost;
- remove or disable it if it produces no observable benefit, adds silent failure modes, or cannot be replayed;
- report negative results plainly;
- do not claim global attribution accuracy from 12 base records.

Compatibility rule: new benchmark modules use `BenchmarkCase` and `reference_evidence_ids`. `GoldenEvent` remains a deprecated legacy alias while old fixtures are loaded. Existing legacy fixture paths, caches, manifests, and historical result artifacts are never bulk-renamed; compatibility changes use explicit migrations and tests.

### 6.4 Evaluation implementation order (amended 2026-07-21, binding)

Benchmark labeling must not begin before the evaluation scaffold exists. The binding dependency order is:

1. **B4 Eval Foundation scaffold:** `BenchmarkCase` schema, evidence-judgment schema, pool manifest, lineage validator, dataset versioning, and grade-2/1/0 tooling;
2. B4 implements FTS5 and B6 implements dense, hybrid, and reranked retrieval under the same cutoff contract;
3. B6 generates union judgment pools without making a final keep/kill decision;
4. human evidence grading;
5. delayed second adjudication pass (≥ 7 calendar days);
6. freeze labels and pool manifests;
7. **B7 Eval Metrics/Gate slice:** implement/recompute full metrics and make named-case keep/kill decisions;
8. generate MetricRecord/ResultPack;
9. generate the Markdown scorecard;
10. run the zero-network regression gate.

The eval package is implemented in exactly these two logical slices while preserving the seven-package roadmap: its Foundation scaffold lands inside B4 before any pool is persisted, and Metrics/Gate lands inside B7 after labels freeze.

## 7. Roadmap

The binding roadmap is `docs/plans/2026-07-21-catalyst-roadmap.md`: Stage 1 contains seven Backend + API packages (B1–B7); Stage 2 contains two frontend packages (F1–F2). Plans are execution packages, not commit boundaries. No paper-style experiment phase exists.

## 8. Git management

- Never commit the mixed working tree as one change.
- Keep docs, runtime code, migrations, benchmark labels, and generated evidence packs in separate reviewable boundaries.
- Stage only after independent verification; commit or push only with architect authorization.
- Never track provider probes, secrets, large databases, embeddings, caches with private/licensed content, or run artifacts not explicitly approved.
- Every runtime boundary includes focused tests, the canonical package suite, `git diff --check`, staged-file review, and both database SHA checks.
- Conventional Commit subjects contain no assistant/tool attribution.

Each B/F package may contain several small Conventional Commit boundaries. Migrations, labels, generated packs, and runtime code remain separate commits even when they belong to the same execution package.

## 9. Explicit non-goals

- no graduation-thesis deliverable;
- no research-paper experiment requirement;
- no claim to replace general web-search assistants;
- no Bloomberg/Reuters/FT full-text ingestion without explicit licensing;
- no GraphRAG or graph database in the 60% milestone;
- no reinforcement learning, fine-tuning, order-flow model, options model, or trading signal;
- no artificial multi-agent personas;
- no automatic image scraping beyond provider-supplied metadata;
- no frontend implementation before backend contracts and server validation are stable.

## 10. Ratified architecture decisions

The adversarial review is accepted with two orchestrator corrections:

1. SEC filings do not enter the News API without an explicit article-materialization contract.
2. Agents/eval isolation is complete in B1: the adapter lives under `catalyst_eval/adapters`, agents declares no eval dependency, and a package-boundary test prevents regression.

Ratified decisions: deterministic Context Builder; no Critic split; no Falsifier at 60%; prerequisite-gated hypotheses and explicit abstention; direct image hotlinking with privacy/fallback safeguards; title/action-only external navigation; 12 compact benchmark cases; alias-based benchmark naming migration; required cutoff correction, ETF/context data, FTS5 baseline, and package-boundary cleanup.

These decisions do not block B2 planning. Provider calls for ETF ingestion remain separately authorized at execution time.

## 11. Implementation planning

B1 is complete and independently verified. Before dscodex writes exactly six backend plans for B2–B7, it must read `2026-07-21-b2-b7-technical-contracts.md`. Each plan includes an exact file allowlist, schema ownership, TDD commands, canonical package commands, database SHA checks, network/secret rules, rollback notes, and internal commit checkpoints. Frontend plans F1–F2 are written only after B7 passes.

## 12. Final decision lock

The following are closed design decisions for the 60% milestone and must not be reopened during implementation without an architect-approved design amendment:

- open-source attribution workbench positioning; no thesis or paper requirement;
- functional data terminology and compatibility-first migration;
- deterministic Context Builder plus existing two-call Critic/Judge workflow;
- no multi-agent personas, Critic split, standalone Falsifier, GraphRAG, RL, or model fine-tuning;
- timestamp cutoff enforcement as a release blocker;
- prerequisite-gated peer/supply-chain attribution and explicit abstention;
- direct provider image hotlinking, title/action-only external navigation, and separate chart highlighting;
- Polygon and Finnhub article visibility; SEC filings remain a separate domain;
- FTS5 lexical baseline plus server dense/hybrid/reranker path;
- Engineering Evaluation Lite instead of a publication-style experiment;
- backend and retrieval contracts freeze before the local internal HTTP API and frontend implementation.

The following are execution authorizations, not unresolved architecture:

- when to perform the supervised ETF/provider run;
- which provider credentials and reachable model profiles are used at execution time;
- whether optional component comparisons run after mandatory scorecards are green;
- commit creation and push authorization.

The design is complete when the corresponding implementation plans are reviewed. Future review should target plan correctness and implementation evidence rather than reopening product positioning.

Additional locked decisions from the consolidation amendment: §1.1 identity/audience; §13 dependency graph; §14 gate structure; §15 reviewer journey; §4.4 workbench surfaces and local security boundary; §6.0 evaluation surfaces and §6.1 counts.

Additional locked decisions from the 2026-07-21 correction amendment remain binding. Execution order and package scope live in `2026-07-21-catalyst-roadmap.md`.

## 13. Package dependency graph (amended, binding)

Monorepo, four packages, no extraction ("extractable by design, deliberately not extracted"; extraction re-entry rules in the package design):

- `catalyst-data` imports no internal Catalyst package;
- `catalyst-agents` imports `catalyst-data` only;
- `catalyst-app` imports `catalyst-agents` and `catalyst-data`;
- `catalyst-eval` consumes documented artifacts (traces, plain state dicts, assurance records, result packs) and may optionally read `catalyst-data` for benchmark corpus access;
- `catalyst-agents` never imports `catalyst-eval` (current violation in `adapter.py` + agents' pyproject is a named Phase-1 fix);
- app runtime attribution does not depend on `catalyst-eval`.

RunAssuranceRecord and every-run deterministic checks are owned by `catalyst_agents/runtime/assurance/`; eval consumes and re-verifies persisted assurance artifacts. Intra-package layer DAGs, exact public APIs, named-edge layering tests, and extraction re-entry rules are bound in `2026-07-19-catalyst-final-package-architecture.md` (ratified subordinate design).

## 14. Non-Toy Core Exit Gate and Showcase Complete (amended 2026-07-21, binding)

"Not a toy" is defined by measurable engineering properties, never by more agents, prompts, providers, UI pages, framework generality, benchmark size, or deployment. The Backend Exit Gate is the Stage 1 milestone; Portfolio Complete additionally requires Stage 2. The concise package registry lives in `docs/plans/2026-07-21-catalyst-roadmap.md`.

**Core Exit Gate (all MUST; eight gates):**

- **Gate A — Data lineage:** zero-write planner; deterministic plan hash; PlanDriftError; append-only request-attempt ledger; connector-side secret redaction; append-only request-scoped raw payloads; canonical normalization; normalized provenance; pagination and partial-cell semantics; idempotent writes; one raw response containing N news items produces N canonical articles/chunks.
- **Gate B — Corpus and chunking:** news_v2; filing_v2 for 8-K/EX-99; raw JSON never chunked; OHLCV/FRED arrays/FMP raw statements excluded; document/chunk/content/metadata identities; reconciliation; tombstones; interrupted builds never publish a current manifest; dedup representative separated from novelty timestamp (§4.5); source taxonomy versioned in the manifest.
- **Gate C — Retrieval:** FTS5/BM25 baseline; pinned BGE-M3 revision; RRF; reranker adapter; candidate-set preservation; cutoff parity across every path; corpus/index identity; explicit degraded fallback; per-stage trace; a recorded named-case reranker keep/kill result. Implementing and measuring the reranker is mandatory; retaining it is not — it may be disabled after evaluation.
- **Gate D — Agent workflow:** typed state/output/failure contracts; deterministic Context Builder with required ETF/benchmark inputs; Miner/Critic/Router/Judge/Validator/Finalizer; two normal LLM calls; hypothesis prerequisites; peer/supply-chain gate; explicit mixed/unexplained; explicit abstention with reason; system_error vs insufficient_evidence; bounded retry/repair/expansion; no multi-agent personas.
- **Gate E — Runtime assurance:** RunAssuranceRecord on every run covering cutoff violations, citation resolution, Judge-visible evidence, hypothesis gate checks, source-support flags (§4.5), legal path, trace completeness, corpus/index/model/prompt identity, budget/retry/repair, degraded/partial state; zero network and no benchmark labels required.
- **Gate F — Evaluation:** 12 base BenchmarkCase records (6 answerable + 2 abstain + 4 retrieval-only); evidence-first labels; union judgment pools; Recall@8; nDCG@8; primary-hit@8; unjudged@8; reranker deltas; answer/abstain and abstain-reason correctness; MetricRecord; ResultPack; Markdown scorecard; zero-network replay; named-case decisions; no composite score; no generalized accuracy claim.
- **Gate G — Contributor/reviewer experience:** committed synthetic fixture with no private/provider payloads; three key-free offline quickstarts (data, agent, eval); networking-disabled tests; README architecture matches code; every active capability claim links code + test; 5-minute and 15-minute reviewer journeys work; honest limitations and killed-component record.
- **Gate H — Local security minimum (before claiming local workbench security):** loopback bind; Host allowlist; Origin allowlist; CORS closed; local nonce or equivalent; keys memory-only; a sentinel key absent from responses, files, DB, logs, traces, reports, and errors.

The frontend itself remains Showcase Complete: the offline quickstarts and the internal API demonstrate the Core Gate without it.

**Durable-run scope split:** Core keeps the minimum durable behavior — persisted run state, status query, cooperative cancel honored at cell boundaries, and resume-from-checkpoint on rerun. Showcase keeps the operator UX beyond that: sub-cell progress detail, ETA estimation, interactive operator flows, and rich run-report formatting.

**Showcase Complete (deferrable; never blocks the milestone):** broad SEC 8-K backfill; generic 10-K/10-Q parsing; operator UX beyond the minimum durable-run scope; advanced browser walkthrough (fourth quickstart); dedicated evaluation UI; feedback Surface C; per-publisher top-K caps before measured need; benchmark case expansion; live-variance 3× reruns; cross-family judge; package extraction/PyPI; frontend surfaces and polish; Finnhub peer expansion (§4.6); langchain extras split; additional providers. Permanently rejected (not deferred): GraphRAG; RL/fine-tuning; SaaS/production infrastructure; multi-agent personas.

The gate rejects a thin API wrapper (a wrapper satisfies none of the provenance, cutoff-parity, assurance, or replay rows) without demanding production-platform properties (no uptime, scale, multi-user, or hosted rows exist).

**Status rule:** ratifying this design does not mean Catalyst has passed the Core Exit Gate. Until every MUST row has a linked passing test or inspectable artifact, active README and portfolio language must say the capability is planned, partial, or under implementation. “Non-toy,” “replayable,” “cutoff-safe,” and similar capability claims may be stated without qualification only after the corresponding gate rows pass against the committed synthetic fixture and documented package suites.

## 15. Reviewer journey (amended, binding)

- **Five minutes:** README positioning and honest non-goals; architecture diagram (matching the real graph); one-command fixture attribution producing a result with evidence links, abstention behavior, and a trace summary; the current scorecard table.
- **Fifteen minutes:** inspect an update plan and its plan hash (zero-write proof); follow one raw-payload → canonical article → chunk provenance chain; open one retrieval trace and compare RRF vs reranker ranks; read one RunAssuranceRecord.
- **Thirty minutes:** run the offline test suites; add or modify a synthetic connector or a metric against the fixture; replay a scorecard byte-identically; run the package dependency/layering tests; read the limitations and negative-results section.

The minimum demonstration bar for admissions/interviews is the five-minute journey plus one fifteen-minute artifact of the reviewer's choosing, all offline and key-free.

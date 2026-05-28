> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# Design Review Rationale: Why Each Change Is Necessary

**Author:** Yiannis Chen
**Date:** 2026-04-13
**Purpose:** For every proposed change in `full-version-design-delta.md` and every bug in `midterm-bug-report.md`, this document explains **why** the change is necessary from three perspectives:

1. **Teacher Mac's Philosophy** — Does it satisfy the seven principles?
2. **Graduation Committee** — Does it address the examiners' likely questions?
3. **Industry Standard** — Does it meet the bar for a portfolio-grade engineering project?

---

## Perspective Definitions

**Teacher Mac's Seven Principles:**
1. Technical Decision Chain — every choice has a business justification
2. Anti-Marketing Metrics — numbers must be grounded, reproducible, contextual
3. Evaluation is Non-Negotiable — no eval = toy
4. Guardrails & Observability — every guard must prevent a specific failure
5. Asset Compound Interest — reusable, publishable, independent modules
6. High Completion > Quick Demos — finish one thing completely
7. Four Pillars of AI Apps — orchestration, context, agents, infrastructure

**Graduation Committee Concerns** (derived from `current_situation.md` §3):
- Q1: "What is the research value compared to direct LLM querying?"
- Q2: "Is the external LLM dependency feasible for real-world deployment?"
- Q3: "How do you handle cross-entity evidence that single-ticker filtering misses?"
- Q4: "How will you systematically improve attribution quality?"

**Industry Standard** benchmarks:
- Production error handling (retry, circuit breaker, error classification)
- Correct metrics (a metric that computes wrong values is worse than no metric)
- Concurrent IO in async systems
- Package independence and dependency management
- Reproducible evaluation with statistical rigor

---

## Change-by-Change Rationale

### 1. Fix GroundingRate (BUG-001) → Design Delta §5

| Perspective | Why this matters |
|---|---|
| **Teacher Mac** | **Anti-Marketing Metrics.** A metric that silently returns 0.0 for all inputs is the worst kind of hollow number. It appears in acceptance reports, looks like a real measurement, but measures nothing. Mac's principle demands: "A number without a computation method is marketing, not engineering." This number has a computation method — it is just wrong. |
| **Graduation Committee** | If an examiner asks "What is your grounding rate?" and the answer is "0.0 on all runs", the follow-up is "Then how do you know the Judge isn't hallucinating?" If the answer is "The metric has a bug", the follow-up is "Then your evaluation framework is not validated." Either path is damaging. |
| **Industry Standard** | In any production ML system, a metric pipeline that produces incorrect values is a P0 incident. It means every decision made based on that metric (model selection, threshold tuning, go/no-go for deployment) is potentially wrong. Netflix, Uber, and Airbnb ML teams have published post-mortems specifically about incorrect metric computation causing cascading bad decisions. |

---

### 2. Calibrate AttributionF1 (BUG-002) → Design Delta §6

| Perspective | Why this matters |
|---|---|
| **Teacher Mac** | **Anti-Marketing Metrics** + **Evaluation is Non-Negotiable.** An F1 score computed with an uncalibrated threshold is a vanity metric. Mac specifically warns: "Hollow claims like 'accuracy improved by 10%' with no context are a red flag." An F1 of 0.7 means nothing if the threshold allows almost any two financial texts to match. The metric itself needs evaluation (meta-evaluation). |
| **Graduation Committee** | Q4 asks "How will you systematically improve quality?" If the metric used to measure quality is unreliable, there is no feedback loop for improvement. The committee will test whether the student understands the difference between "the pipeline ran" and "the pipeline produced validated results." Threshold calibration with a precision-recall curve is standard methodology that demonstrates rigor. |
| **Industry Standard** | RAGAS, deepeval, and BizFinBench all document their threshold selection methodology. In the RAGAS paper, the authors explicitly discuss the impact of threshold choice on metric sensitivity. An undocumented, arbitrary threshold would not pass peer review. For a portfolio project, the standard is: "Can you defend every number in your eval report?" |

---

### 3. Resolve Async/Sync Contradiction (BUG-003 + BUG-004) → Design Delta §1

| Perspective | Why this matters |
|---|---|
| **Teacher Mac** | **Technical Decision Chain.** "Why did you use async?" must have a concrete answer: "Because we fetch from 3+ providers concurrently, and async with semaphore-based rate limiting lets us maximize throughput within API rate limits." If the orchestrator is sequential, this answer is a lie. The technology choice (async) does not solve the claimed business problem (concurrent multi-source ingestion). |
| **Graduation Committee** | An examiner who reads the code will see `async def process_request` with a sequential `for` loop and sync `sqlite3` calls. This is a textbook example of "using a buzzword without understanding the implication." The question will be: "Walk me through how your async pipeline achieves concurrency." The honest answer right now is: "It doesn't." |
| **Industry Standard** | Mixing sync blocking calls inside async event loops is a well-documented anti-pattern. Python's `asyncio` documentation explicitly warns against it. In production systems (e.g., FastAPI applications), a sync `sqlite3` call inside an async handler degrades the entire server's throughput because the event loop thread is blocked. The fix is either aiosqlite or a thread pool — both are standard solutions. |

---

### 4. Tiered Retrieval Strategy → Design Delta §2

| Perspective | Why this matters |
|---|---|
| **Teacher Mac** | **Guardrails & Observability.** The current single-ticker retrieval is not just a feature gap — it is a **missing guardrail**. When the system cannot find direct evidence, it should escalate (broader search), not silently proceed with insufficient data. Mac's principle: "What hard limits prevent the system from producing dangerous or incorrect outputs?" The current answer is: the system will try to attribute a tariff-driven crash using only the target ticker's news, miss the actual cause, and produce a wrong attribution with high confidence. |
| **Graduation Committee** | Q3 directly asks this: "How do you handle cross-entity evidence?" This is not a theoretical concern — of the 50 golden events, approximately 30 involve causes that are not company-specific (tariffs, Fed policy, sector rotations, market-wide rallies). A system that can only correctly attribute ~40% of its golden set due to a retrieval limitation is not demonstrating the claimed capability. The tiered strategy is the answer to Q3. |
| **Industry Standard** | gpt-researcher solves this with a planner node that decomposes queries into sub-questions, each targeting different search scopes. PokieTicker uses layered processing (layer0 → layer1 → layer2) with escalation. The concept of "retrieval augmentation with fallback expansion" is standard in enterprise RAG systems (e.g., Microsoft's GraphRAG expands from local to global context). A single WHERE clause is not a retrieval strategy; it is a database query. |

---

### 5. Error Classification → Design Delta §3

| Perspective | Why this matters |
|---|---|
| **Teacher Mac** | **Guardrails & Observability.** "What exactly is being monitored? What actionable decisions come from analyzing the logs?" If the system reports "insufficient evidence" for both data gaps and API failures, the logs are misleading. An operator cannot distinguish "we need more data sources" from "our LLM provider had an outage." The guardrail (insufficient_handler) exists but prevents the wrong failure mode. |
| **Graduation Committee** | The DY review criteria (current_situation.md §4.1) explicitly asks: "Does the system have recovery mechanisms for tool failures, or does it require manual re-runs?" The current answer is: Critic failure → silent misclassification as data gap → no recovery. This is a "toy" behavior pattern. A system that classifies errors enables: automatic retry for transient failures, alerting for persistent failures, and coverage analysis for genuine data gaps. |
| **Industry Standard** | Error classification is fundamental to operational systems. AWS, GCP, and Azure all use structured error taxonomies (transient vs. permanent, client vs. server, retriable vs. fatal). In LLM applications specifically, the distinction between "model returned bad output" (parse error → retry with adjusted prompt) and "model API is down" (infrastructure → backoff and alert) determines the correct recovery strategy. Conflating them means neither is handled correctly. |

---

### 6. Connector Refactoring + Retry → Design Delta §4

| Perspective | Why this matters |
|---|---|
| **Teacher Mac** | **Asset Compound Interest.** If adding a new data source (e.g., Finnhub, SEC EDGAR) requires copy-pasting 80 lines of HTTP client management, error handling, and rate limiting, then the connector architecture is not a reusable asset — it is a template for technical debt. A well-designed base class means each new connector is 20-30 lines of domain logic, and the shared behavior (retry, rate limiting, error classification) is tested once. |
| **Graduation Committee** | The committee will assess code quality and architectural maturity. Copy-pasted modules with identical error handling is a signal of undergraduate-level engineering. A shared base class with concrete specializations demonstrates understanding of the Template Method pattern, dependency injection, and separation of concerns — these are expected at the thesis level. |
| **Industry Standard** | Every mature data integration system uses a connector abstraction. findatapy has `DataVendor` base class with vendor-specific subclasses. crawl4ai has a dispatcher that handles retry and rate limiting at the infrastructure layer, not in each fetcher. The absence of retry on 429 responses is particularly concerning: Polygon's free tier documentation explicitly states that 429s should be retried with backoff. Ignoring this means data loss during any burst of requests. |

---

### 7. Baseline Comparison Experiment → Design Delta §8

| Perspective | Why this matters |
|---|---|
| **Teacher Mac** | **Anti-Marketing Metrics** + **Evaluation is Non-Negotiable.** "An AI project without an Evaluation Module is a toy." But even with an eval module, if you cannot show that your system outperforms the simplest baseline (direct LLM query), the eval proves that your system is unnecessary. The baseline experiment is the minimum viable evidence for the project's existence. |
| **Graduation Committee** | Q1 is the most dangerous question: "A general-purpose LLM can also give similar explanations. What is your system's research and practical value?" The only defensible answer is quantitative: "On 50 financial events, Catalyst MCJ achieves F1=X.XX vs. direct LLM F1=Y.YY (p < 0.05), while maintaining grounding rate of Z.ZZ (direct LLM: 0.00 by construction)." Without this data, the committee may conclude the project has no marginal value over ChatGPT. |
| **Industry Standard** | Every ML paper and every serious ML project report includes baseline comparisons. RAGAS papers compare against BM25-only, vector-only, and no-retrieval baselines. BizFinBench compares model families against each other and against random baselines. A system that only reports absolute metrics without baselines cannot be assessed. In industry, the question is always: "Is the added complexity worth it compared to the simpler alternative?" The Pareto chart (cost vs. quality across configurations) answers this directly. |

---

### 8. Full Golden Set Evaluation → Design Delta §8

| Perspective | Why this matters |
|---|---|
| **Teacher Mac** | **Evaluation is Non-Negotiable** + **High Completion > Quick Demos.** You have 50 carefully annotated golden events. You have a harness that can run them. You have 5 metrics. But you have never run the full evaluation. This is the definition of "incomplete": the infrastructure exists but the validation does not. Mac's standard is clear: "A partial system with all three pillars half-built is worse than one pillar fully complete." The eval pillar is half-built until it produces a full report. |
| **Graduation Committee** | The committee expects to see evaluation results in the thesis (Chapter 4/5). A single-case result on a mismatched date is not publishable. 50 events with per-category breakdowns, standard deviations, and statistical significance tests is a credible evaluation section. The difference between a passing and failing thesis defense may hinge on whether the evaluation is convincing. |
| **Industry Standard** | deepeval's entire value proposition is making it easy to run eval suites in CI. RAGAS provides `evaluate()` as a one-call function specifically so teams run it routinely, not once. In production ML, eval runs are automated, versioned, and compared across model versions. Running eval once on the wrong date is not evaluation — it is a smoke test. |

---

### 9. Observability → Design Delta §9

| Perspective | Why this matters |
|---|---|
| **Teacher Mac** | **Guardrails & Observability.** "What exactly is being monitored? What data do the logs produce? What actionable decisions come from analyzing them?" Currently: nothing is monitored, logs produce scattered warnings, and no actionable decisions can be derived. The spec promises LangSmith integration; the code delivers `print()` statements. |
| **Graduation Committee** | The DY review asks: "Are intermediate results visible? Can the system's reasoning be audited?" Without traces, the answer is no. An examiner cannot ask "Show me what the Critic decided for event g009" and get an answer. Traces are the evidence that the system's multi-agent architecture actually functions as designed, rather than being a single monolithic LLM call with extra steps. |
| **Industry Standard** | LangSmith, Langfuse, Phoenix, and Weights & Biases Prompts all exist because production LLM systems require trace-level observability. Without it, debugging is guesswork, performance optimization is impossible, and cost attribution is manual. For a portfolio project claiming "enterprise-grade" quality, the minimum is: every LLM call has a trace ID, input/output tokens, latency, and cost, queryable after the fact. |

---

### 10. Dedup Consolidation (BUG-006) → Design Delta §7

| Perspective | Why this matters |
|---|---|
| **Teacher Mac** | **Asset Compound Interest.** Dead code is the opposite of asset accumulation — it is debt that confuses future readers and maintenance. A dedup module that exists but is never called is not a reusable asset; it is a liability. Consolidation is the minimal action to make the module actually function as designed. |
| **Graduation Committee** | Code reviewers will search for inconsistencies. Two identical functions in different files is a red flag for "development by copy-paste rather than design." It suggests the developer did not maintain awareness of the existing module structure. |
| **Industry Standard** | DRY (Don't Repeat Yourself) is a basic engineering principle. In production codebases, duplicate logic is a frequent source of bugs: one copy gets updated, the other doesn't, and the system produces inconsistent results. The fix is trivial (import instead of redefine) and eliminates the risk entirely. |

---

### 11. data-core Package Independence → Design Delta §11

| Perspective | Why this matters |
|---|---|
| **Teacher Mac** | **Asset Compound Interest.** This is the single most important principle for Yiannis's career. Mac's philosophy: "Extract highly decoupled, reusable modules from every project and package them as independent open-source libraries. Each reusable module is a personal industrial-grade IP asset." If `catalyst-data` cannot be `pip install`-ed and used independently, it is not a reusable asset. It is a subdirectory of a monorepo. The difference is enormous: a published package is a portfolio entry; a subdirectory is invisible. |
| **Graduation Committee** | The thesis claims three publishable packages. If one cannot be installed without pulling in PyTorch and FlagEmbedding (multi-GB dependencies), the claim is unsubstantiated. An examiner running `pip install ./packages/data-core` and getting a dependency resolution error will question the project's engineering maturity. |
| **Industry Standard** | Python packaging best practice (documented in PyPA guidelines) mandates that heavy optional dependencies are declared as extras, not core requirements. findatapy, crawl4ai, and deepeval all follow this pattern: core functionality installs lightweight; ML/GPU features are opt-in. A package that forces 2GB of ML downloads for basic data ingestion violates the principle of least surprise and is unpublishable on PyPI as a general-purpose tool. |

---

### 12. SequentialRunner Elimination (BUG-009) → Design Delta §10

| Perspective | Why this matters |
|---|---|
| **Teacher Mac** | **Evaluation is Non-Negotiable.** If tests run a different code path than production, the tests do not evaluate the production system. This violates the principle that evaluation must be rigorous and representative. |
| **Graduation Committee** | An examiner may ask: "How do you ensure your tests validate the real system?" If the answer is "Tests use a simplified runner that doesn't replicate LangGraph semantics," the follow-up is: "Then what do your tests actually prove?" |
| **Industry Standard** | Test-production parity is a core principle of continuous delivery. The Twelve-Factor App methodology states: "Keep development, staging, and production as similar as possible." A test suite running against a mock runner while production runs LangGraph is the opposite of this principle. |

---

## Cross-Cutting Themes

### Theme 1: "The eval infrastructure exists but was never fully exercised."

This applies to: GroundingRate (BUG-001), AttributionF1 (BUG-002), full golden set (Delta §8), baseline experiments (Delta §8).

All three perspectives converge: **building evaluation infrastructure is necessary but not sufficient; the infrastructure must be run, validated, and its results must be trustworthy.** The current state is analogous to building a testing lab with broken instruments — the lab exists, but no valid measurements have been produced.

### Theme 2: "The async architecture was chosen but not committed to."

This applies to: Orchestrator (BUG-003), SQLite (BUG-004), `time.sleep` in nodes (BUG-011).

The business justification for async is concurrent multi-source data ingestion. If the code does not actually achieve concurrency, the justification is hollow. Teacher Mac's Technical Decision Chain demands either committing to async (and making it work end-to-end) or honestly choosing sync (and justifying that choice). The current hybrid is indefensible.

### Theme 3: "Abstractions were planned but not wired."

This applies to: SourceConnector Protocol (BUG-008), `retry.py` (BUG-007), `dedup/hard.py` (BUG-006), `_SequentialRunner` (BUG-009).

Each of these represents a design intent that was not followed through to implementation. The modules exist in the file tree, suggesting a well-designed architecture, but they are not connected to the system's execution path. This is a common pattern in research prototypes that transition to implementation under time pressure — the architecture document moves ahead of the code. The fix is not to add more architecture, but to wire what already exists.

---

## Final Note: The Gap Is Closable

The critique is harsh because the standard is high. But the fundamental observation is this: **the design quality of Catalyst is strong; the implementation-to-design gap is the problem.** The golden set (50 events, 8 tickers, multi-category) is better than most academic projects. The Medallion schema design is sound. The MCJ graph topology is well-reasoned. The cost tracker is a nice touch that most projects omit.

The work required is not "redesign the system" — it is "make the system match its own design document." That is a much more tractable problem, and it is exactly the kind of engineering rigor that distinguishes a portfolio-grade project from a prototype.

# Reference Projects Analysis

> Context-loading guide for Claude Code sessions. Maps 13 open-source projects in `~/Desktop/references/` to Catalyst's three pillars: **data-core**, **agent engine**, and **eval suite**.

---

## Quick Pillar Map

| Catalyst Pillar | Strongest References |
|---|---|
| **data-core** (ingestion, cleaning, SQLite, async, rate limiting) | PokieTicker, crawl4ai, findatapy, Crucix, situation-monitor, valuecell |
| **agent engine** (LangGraph workflow, RAG, retrieval, grading, synthesis) | gpt-researcher, opengpts, valuecell, PokieTicker, Crucix, FinGPT |
| **eval suite** (metrics, golden sets, CI gates, RAGAS) | ragas, deepeval, BizFinBench, gpt-researcher |

---

## 1. BizFinBench

**What it does:** Large-scale bilingual (EN/ZH) financial LLM benchmark with 100k+ items across nine task families — including **Anomalous Event Attribution** — plus an Iterajudge-style second model for scoring.

**Tech stack:** Python, vLLM inference, aiohttp for API paths, YAML-driven eval configs.

**Pillar relevance:** eval suite (primary)

### Core Files

| File | Relevance |
|---|---|
| `benchmark_code/BizFinBench/eval_AEA_2.py`, `eval_AEA.py` | Financial anomaly/attribution scoring with set overlap and partial credit — directly aligned with Catalyst's attribution eval. |
| `config/offical/eval_fin_eval_diamond.yaml` | YAML task registry mapping `data_path` + `compare_func` per task — template for CI eval pipelines. |
| `utils/parser/grader.py` | Numeric/symbolic answer normalization (sympy, LaTeX, equivalence checks). |
| `post_eval.py` | Dynamic loading of `evaluation(input_path)` — pattern for plugging golden-set evaluators. |
| `tools/ExternalApi.py` | Async client with `semaphore_limit` for API-based eval — reusable for RAGAS batch runs. |
| `datasets/*.jsonl` | Gold-style financial tasks including `Anomalous_Event_Attribution` fixtures. |
| `statistic.py` | Aggregates per-task scores into `statistic.jsonl` — reporting/dashboard pattern. |

---

## 2. Crucix

**What it does:** Local OSINT + financial macro dashboard that aggregates open feeds (FRED, Yahoo, news, conflict, sanctions) on a ~15-minute sweep with SSE to browser, optional LLM for trade ideas, and Telegram/Discord bots.

**Tech stack:** Node 22+, ESM (.mjs), Express, `Promise.allSettled` parallel I/O, JSON persistence.

**Pillar relevance:** data-core (primary), agent engine (secondary)

### Core Files

| File | Relevance |
|---|---|
| `apis/briefing.mjs` | Orchestrator pattern: parallel sources, per-source timeout, structured merge — closest to Catalyst's async pipeline. |
| `apis/utils/fetch.mjs` | Retries, AbortController, JSON parse fallback — template for resilient HTTP ingestion. |
| `apis/sources/fred.mjs`, `yfinance.mjs` | Economic/market pull patterns for FRED and Yahoo-style quotes. |
| `lib/delta/engine.mjs` | Change detection, semantic dedup, severity scoring — useful for "what changed since last run" in attribution. |
| `lib/llm/provider.mjs`, `lib/llm/index.mjs` | Provider abstraction with `complete()` contract — maps to LangGraph tool-calling backends. |
| `apis/BRIEFING_PROMPT.md`, `BRIEFING_TEMPLATE.md` | Prompt + structure for synthesis from multi-source JSON — analogous to final synthesis node. |
| `lib/delta/memory.mjs` | Hot/cold persistence of sweep history (JSON). |

---

## 3. FinGPT

**What it does:** Umbrella open financial LLM effort: LoRA fine-tuning (sentiment, multi-task), FinGPT-RAG (retrieval-augmented sentiment), benchmark eval scripts. This checkout also contains `finogrid/`, a separate payments/ledger platform with FastAPI, SQL migrations, and ChromaDB RAG.

**Tech stack:** Python, PyTorch/LoRA, LangChain, FastAPI (finogrid), PostgreSQL, ChromaDB.

**Pillar relevance:** agent engine (primary), eval suite (secondary)

### Core Files

| File | Relevance |
|---|---|
| `finogrid/fingpt_integration/rag/knowledge_base.py` | ChromaDB + SentenceTransformers RAG: chunking, upsert, query — portable retrieval node pattern. |
| `finogrid/agents/*/agent.py` | Role-based agents as Python classes with async methods — clear separation of agent responsibilities. |
| `fingpt/FinGPT_RAG/multisource_retrieval/news_scraper.py`, `scrapers/*` | News retrieval/scraping pipeline for RAG context. |
| `fingpt/FinGPT_RAG/instruct-FinGPT/inference/chatbot.py`, `batchbot.py` | Inference loops over RAG-augmented prompts. |
| `fingpt/FinGPT_Benchmark/benchmarks.py` | Multi-dataset eval (`fpb,fiqa,tfns,...`) — pattern for CI eval matrices. |
| `fingpt/FinGPT_MultiAgentsRAG/Evaluation_methods/*` | Hallucination/truthfulness eval scripts (HaluEval, TruthfulQA). |
| `finogrid/database/migrations/*.sql` | Relational schema patterns (PostgreSQL — adapt ideas to SQLite). |

---

## 4. findatapy

**What it does:** Unified Python API to download and normalize market and economic time series from many vendors (FRED, Yahoo, Dukascopy, crypto, Bloomberg) with caching, filters, and quality checks.

**Tech stack:** Python, pandas/numpy, Redis/Arctic optional cache, CSV ticker maps.

**Pillar relevance:** data-core (primary)

### Core Files

| File | Relevance |
|---|---|
| `findatapy/market/market.py` | `fetch_market()` entry point: merges requests, caching, post-processing — central ingestion orchestration. |
| `findatapy/market/marketdatarequest.py` | `MarketDataRequest` canonical request object with `generate_key()` for cache identity — model for Catalyst's request DTOs. |
| `findatapy/market/marketdatagenerator.py` | Dispatches to vendors with batching/chunking of downloads. |
| `findatapy/market/datavendorfred.py`, `datavendorcrypto.py`, `datavendorweb.py` | Vendor-specific fetch logic — templates for new source adapters. |
| `findatapy/market/ioengine.py` | `IOEngine` + `SpeedCache`: disk/Redis/Parquet/S3 IO — persistence and cache layer patterns. |
| `findatapy/timeseries/dataquality.py` | NaN/gap/integrity reporting — data quality gates before agents run. |
| `findatapy/timeseries/filter.py`, `calendar.py` | Trading calendars and session filters — financial time semantics. |
| `findatapy/util/configmanager.py`, `dataconstants.py` | API keys, paths, and cache policy centralization. |

---

## 5. FinanceToolkit

**What it does:** Open-source Python toolkit computing 150+ financial ratios and indicators with explicit documented formulas, pulling fundamentals and prices from FMP with optional Yahoo Finance fallback.

**Tech stack:** Python, pandas/numpy, requests, yfinance, pytest with VCR-style HTTP recording.

**Pillar relevance:** data-core (primary)

### Core Files

| File | Relevance |
|---|---|
| `financetoolkit/toolkit_controller.py` | Central `Toolkit` API: tickers, dates, `use_cached_data`, `enforce_source` — ingestion orchestration. |
| `financetoolkit/fmp_model.py` | HTTP fetch, retry loops, rate-limit / subscription-aware handling (sleep on `LIMIT REACH`). |
| `financetoolkit/yfinance_model.py` | Secondary source and fallback behavior for multi-source attribution. |
| `financetoolkit/normalization_model.py` | Cross-vendor cleaning: align labels and formats — strong analogy to Catalyst's source mapping. |
| `financetoolkit/historical_model.py` | OHLC/returns pipeline patterns. |
| `financetoolkit/fundamentals_model.py` | Financial statement collection into consistent structures. |
| `financetoolkit/utilities/cache_model.py` | Local persistence of DataFrames (pickle) — idempotent re-runs. |

---

## 6. PokieTicker

**What it does:** Full-stack app that overlays news on stock charts, scores and filters news via batched LLM sentiment/analysis, and explains price moves with XGBoost forecasts. Closest in spirit to Catalyst's "why did the price move?" question.

**Tech stack:** React/TypeScript/Vite frontend, FastAPI backend, SQLite (WAL), Polygon.io, Anthropic Batch API (Haiku for scoring, Sonnet for deep analysis), XGBoost/sklearn.

**Pillar relevance:** data-core (primary), agent engine (primary)

### Core Files

| File | Relevance |
|---|---|
| `backend/database.py` | SQLite schema: OHLC, raw news, join tables, layered result tables, `batch_jobs` tracking, WAL pragma — **closest schema reference to Catalyst**. |
| `backend/polygon/client.py` | Exponential backoff, 429 + Retry-After, 5xx handling — reusable HTTP discipline. |
| `backend/bulk_fetch.py` | Rolling-window rate limit (N requests/min) + SQLite inserts — template for throttled ingestion. |
| `backend/pipeline/layer0.py` | Cheap rule-based filter before LLM (spam/listicles) — router/gating node analogy. |
| `backend/pipeline/layer1.py` | Batch prompts, keyword context extraction, relevance/sentiment JSON — grading/synthesis node pattern. |
| `backend/pipeline/layer2.py` | On-demand deep analysis, JSON-only response contract, DB cache — "expensive node only when needed". |
| `backend/pipeline/alignment.py` | News-to-trading-day-to-forward-returns alignment — attribution-friendly logic. |
| `backend/pipeline/similarity.py` | TF-IDF + cosine "similar events" — lightweight retrieval without embeddings API. |
| `backend/batch_submit.py`, `batch_collect.py` | Async batch LLM jobs with custom_id-to-article mapping in DB. |
| `backend/ml/backtest.py` | Expanding-window CV, accuracy/precision/recall/F1 vs baseline — offline model eval. |
| `backend/config.py` | Pydantic settings / env loading pattern. |

---

## 7. crawl4ai

**What it does:** Async web crawler/scraper focused on LLM-ready Markdown output, structured extraction (including LLM-based), deep crawling, caching, and browser automation — aimed at RAG and agent pipelines.

**Tech stack:** Python, asyncio/aiohttp/httpx, Playwright, aiosqlite, BM25 content filtering, LiteLLM.

**Pillar relevance:** data-core (primary), agent engine (secondary)

### Core Files

| File | Relevance |
|---|---|
| `crawl4ai/async_webcrawler.py` | `AsyncWebCrawler`: `arun`/`arun_many`, lifecycle, dispatcher + DB cache integration. |
| `crawl4ai/async_dispatcher.py` | `RateLimiter` (per-domain timing, exponential backoff on 429) + `MemoryAdaptiveDispatcher` for backpressure. |
| `crawl4ai/async_database.py` | `aiosqlite` pool, retries, schema init/migrations — **async SQLite reference**. |
| `crawl4ai/async_url_seeder.py` | Semaphore-based rate cap for URL discovery (`hits_per_sec`). |
| `crawl4ai/extraction_strategy.py` | LLM-driven structured extraction with rate-limit delays. |
| `crawl4ai/content_filter_strategy.py` | BM25/relevance filtering of HTML chunks — retrieval/noise reduction before embedding. |
| `crawl4ai/chunking_strategy.py` | Chunking strategies for downstream RAG. |
| `crawl4ai/cache_context.py`, `cache_validator.py` | Cache modes and validation. |

---

## 8. gpt-researcher

**What it does:** Autonomous deep research over the web: plans sub-questions, gathers and scrapes sources in parallel, curates and compresses context, and writes long cited reports. Features a **multi-agent LangGraph** path with browser, planner, human review, parallel research, writer, and publisher nodes.

**Tech stack:** Python, FastAPI, LangChain, **LangGraph**, WebSockets, aiohttp async worker pools.

**Pillar relevance:** agent engine (primary), data-core (secondary), eval suite (secondary)

### Core Files

| File | Relevance |
|---|---|
| `multi_agents/agents/orchestrator.py` | **`StateGraph(ResearchState)`**: nodes (browser, planner, researcher, writer, publisher, HITL) with conditional edges — **template for Catalyst's LangGraph workflow**. |
| `multi_agents/memory/research.py` | Typed research state for LangGraph — pair with orchestrator. |
| `gpt_researcher/agent.py` | `GPTResearcher`: end-to-end orchestration (skills, memory, vector store, report generation). |
| `gpt_researcher/context/compression.py` | RAG-style contextual compression, embedding filters, chunk pipelines. |
| `gpt_researcher/context/retriever.py` | Custom `BaseRetriever` implementations wired to research artifacts. |
| `gpt_researcher/skills/curator.py` | LLM-based source ranking/curation — "grading" sources before synthesis. |
| `gpt_researcher/utils/rate_limiter.py` | Singleton global async rate limiter shared across pools. |
| `gpt_researcher/utils/workers.py` | `WorkerPool`: semaphore + global rate limiter integration for concurrent bounded work. |
| `gpt_researcher/scraper/` | Fetching and HTML/PDF extraction pipelines with provider-specific adapters. |
| `gpt_researcher/retrievers/` | Pluggable search/retrieval backends (Tavily, Serper, arXiv, etc.) — source adapter pattern. |
| `gpt_researcher/vector_store/vector_store.py` | Chunking + vector ingest wrapper around LangChain `VectorStore`. |
| `gpt_researcher/prompts.py` | Central prompt families for research/curation/writing. |
| `evals/simple_evals/simpleqa_eval.py` | LLM-as-judge factual scoring. |
| `evals/hallucination_eval/evaluate.py` | Hallucination-oriented evaluation pipeline. |

---

## 9. opengpts

**What it does:** Open-source analogue to ChatGPT "GPTs"/Assistants: configurable LLM + tools + retrieval + memory, with FastAPI backend and Vite frontend. Supports swapping models, tools, vector DB, and cognitive architecture (assistant, RAG, chatbot).

**Tech stack:** Python, FastAPI, **LangChain**, **LangGraph**, PostgreSQL + pgvector, asyncpg, structlog.

**Pillar relevance:** agent engine (primary)

### Core Files

| File | Relevance |
|---|---|
| `backend/app/retrieval.py` | **RAG LangGraph `StateGraph`**: nodes `invoke_retrieval` → `retrieve` → `response`, search query from conversation, checkpointer — **strong reference for retrieve-grade-synthesis flows**. |
| `backend/app/agent.py` | Factory wiring tool agents, RAG executor, chatbot, LLM providers, shared checkpointer — composition root for graphs. |
| `backend/app/agent_types/tools_agent.py` | `MessageGraph` + `ToolExecutor`, conditional edges agent-to-tools — classic tool-calling agent loop. |
| `backend/app/checkpoint.py` | `AsyncPostgresCheckpoint`: singleton LangGraph checkpoint setup — adapt for durable run state (Postgres vs SQLite). |
| `backend/app/ingest.py` | Minimal ingest pipeline: parse blobs, sanitize content, batch `add_documents`, namespace metadata. |
| `backend/app/storage.py` | Async Postgres CRUD for assistants/threads/users — structured app state pattern. |
| `backend/app/tools.py` | Large tool registry (search, retrieval, domain tools) — pattern for financial data tools. |
| `backend/app/schema.py` | Pydantic models for persisted entities. |

---

## 10. deepeval

**What it does:** Pytest-style LLM evaluation framework: define test cases, run metrics (G-Eval, RAG metrics, agent metrics), `assert_test`/`evaluate`, optional tracing, datasets/goldens, and CI-friendly test runs.

**Tech stack:** Python, Pydantic v2, pytest plugin, RAGAS integration, OpenTelemetry tracing, Typer CLI.

**Pillar relevance:** eval suite (primary)

### Core Files

| File | Relevance |
|---|---|
| `deepeval/evaluate/evaluate.py` | `assert_test` and orchestration of metric execution with thresholds/failures — **CI gate pattern**. |
| `deepeval/evaluate/execute.py` | Execution engine for test runs and metric batching. |
| `deepeval/test_case/llm_test_case.py` | `LLMTestCase` (input, actual/expected output, `retrieval_context`) — **golden-style record**. |
| `deepeval/metrics/base_metric.py` | `BaseMetric` / `BaseConversationalMetric` — how to implement custom metrics consistently. |
| `deepeval/metrics/ragas.py` | **RAGAS metric wrappers** (`RAGASFaithfulnessMetric`, contextual precision/recall, answer relevancy) — **direct RAGAS integration reference**. |
| `deepeval/metrics/g_eval/g_eval.py` | G-Eval (flexible LLM-as-judge criteria) — good for attribution quality scoring. |
| `deepeval/dataset/dataset.py`, `deepeval/dataset/utils.py` | `Golden` / conversion between goldens and test cases — **golden set management**. |
| `deepeval/plugins/plugin.py` | Pytest plugin: session hooks, `--identifier`, test run creation — **CI integration pattern**. |
| `deepeval/test_run/test_run.py` | Test run aggregation, hyperparameters, reporting — gate metrics on a run. |

---

## 11. ragas

**What it does:** Python framework for evaluating RAG and LLM applications: objective metrics (LLM-judge and classical), test-set generation, dataset/experiment handling, and integrations with the LangChain ecosystem.

**Tech stack:** Python, pydantic, Hugging Face `datasets`, numpy, diskcache, tenacity retries, openai.

**Pillar relevance:** eval suite (primary)

### Core Files

| File | Relevance |
|---|---|
| `src/ragas/dataset_schema.py` | `SingleTurnSample`, `EvaluationDataset`, column contracts (`user_input`, `retrieved_contexts`, `response`, `reference`) — **schema for golden sets and RAG metrics**. |
| `src/ragas/evaluation.py` | `evaluate`/`aevaluate`: wiring metrics, embeddings, validation, callbacks — **end-to-end scoring pipeline**. |
| `src/ragas/metrics/base.py` | Metric abstraction, required columns, single vs multi-turn — template for custom Catalyst metrics. |
| `src/ragas/run_config.py` | `RunConfig`: timeouts, retries, `max_workers`, seed — production eval robustness. |
| `src/ragas/executor.py` | Batched async jobs, cancellation, error handling — parallel metric execution. |
| `src/ragas/validation.py` | Column remaps, metric/dataset compatibility checks — CI gates on datasets. |
| `src/ragas/experiment.py` | Experiment datatable + `version_experiment` (Git) — reproducible eval runs. |
| `src/ragas/integrations/langgraph.py` | Converts LangChain messages to Ragas messages — use if Catalyst's LangGraph outputs LangChain messages for scoring. |
| `src/ragas/backends/local_jsonl.py` | JSONL persistence for datasets — golden-set file format. |
| `tests/e2e/test_amnesty_in_ci.py` | E2E eval with `@pytest.mark.ragas_ci` — pattern for gated LLM eval in CI. |

---

## 12. situation-monitor

**What it does:** SvelteKit dashboard aggregating macro/news/crypto/geo feeds into panels (markets, Fed, maps). Talks to public HTTP APIs (Finnhub, FRED, GDELT, CoinGecko) with a custom CORS proxy and optional vendored OpenBB tree.

**Tech stack:** Svelte 5, SvelteKit, TypeScript, Vite, Tailwind, D3, Vitest/Playwright.

**Pillar relevance:** data-core (secondary — patterns only, TypeScript not Python)

### Core Files

| File | Relevance |
|---|---|
| `src/lib/services/client.ts` | Unified HTTP client: cache → circuit breaker → dedupe → execute with retries — **strong fetch resilience pattern** (translate to Python/httpx). |
| `src/lib/services/registry.ts` | Per-service base URL, timeout, retries, TTL, stale-while-revalidate, breaker thresholds — **rate-limit/SLA policy table**. |
| `src/lib/services/circuit-breaker.ts` | Circuit breaker implementation — failure isolation for flaky providers. |
| `src/lib/services/deduplicator.ts` | In-flight request coalescing — avoids duplicate work under burst traffic. |
| `src/lib/services/cache.ts` | L1/L2 cache + TTL/stale logic. |
| `src/lib/config/api.ts` | API keys, base URLs, `API_DELAYS` for throttling, CORS proxy primary/fallback. |

---

## 13. valuecell

**What it does:** Python multi-agent financial platform: stock research, strategies, news, trading integrations with FastAPI server, local-first storage (SQLite + LanceDB + knowledge directories), and Agno-based agents exposed via A2A (Agent-to-Agent) patterns.

**Tech stack:** Python, FastAPI, SQLAlchemy 2 + aiosqlite, Pydantic, Agno agents, LanceDB, edgartools, yfinance, akshare, ccxt.

**Pillar relevance:** data-core (primary), agent engine (primary)

### Core Files

| File | Relevance |
|---|---|
| `python/valuecell/server/db/connection.py` | SQLite engine: `StaticPool`, `check_same_thread`, timeouts — **SQLAlchemy patterns for Catalyst persistence**. |
| `python/valuecell/server/db/init_db.py` | Schema bootstrap — migration/init flow. |
| `python/valuecell/server/db/models/*.py` | ORM models (strategies, conversations, assets) — relational modeling reference. |
| `python/valuecell/utils/db.py` | `resolve_lancedb_uri()` and path conventions — **local vector store layout next to SQLite**. |
| `python/valuecell/core/coordinate/orchestrator.py` | `AgentOrchestrator`: user input → planning/super-agent/task execution, background producers, execution contexts — **workflow state machine ideas for LangGraph**. |
| `python/valuecell/core/plan/planner.py` | `ExecutionPlanner`: LLM → `ExecutionPlan`, `UserInputRequest` with `asyncio.Event` — HITL pattern. |
| `python/valuecell/core/task/executor.py` | `TaskExecutor`: runs tasks, streams events, scheduled tasks — multi-step execution. |
| `python/valuecell/core/super_agent/core.py` | Triage agent (`SuperAgentOutcome`, structured output) — router node analogue. |
| `python/valuecell/agents/research_agent/knowledge.py` | RAG-style Knowledge + chunking readers — ingest + retrieval glue. |
| `python/valuecell/agents/research_agent/vdb.py` | LanceDB + embedder optional init — vector DB lifecycle. |

---

## Notes

- **LangGraph usage:** Only **gpt-researcher** and **opengpts** use LangGraph directly. Other agent-style repos (valuecell, Crucix, PokieTicker, FinGPT) use custom orchestration — adapt their patterns to LangGraph nodes/edges.
- **SQLite usage:** **PokieTicker** (sync, WAL) and **crawl4ai** (async via aiosqlite) are the closest SQLite references. **valuecell** uses SQLAlchemy + aiosqlite.
- **RAGAS as a library:** **ragas** is designed to be called directly — Catalyst can import `ragas.evaluate()` rather than reimplementing faithfulness/context metrics. **deepeval** wraps RAGAS and adds pytest integration.
- All file paths above are relative to `/Users/yiannischen/Desktop/references/<project>/`.

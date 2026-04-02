# Catalyst System Design Spec

**Author:** Yiannis Chen
**Date:** 2026-04-02
**Status:** Approved
**Version:** 1.0

---

## 1. Product Vision

Catalyst is a high-fidelity financial attribution system that answers: **"Why did this stock move on a given day?"**

It ingests multi-source financial data, processes it into agent-friendly Markdown, and orchestrates a Miner-Critic-Judge workflow to produce causal attribution reports with quantified confidence scores and source citations.

### 1.1 Design Principles (Teacher Mac's Philosophy)

| Principle | How Catalyst applies it |
|---|---|
| Technical Decision Chain | Every tech choice below includes a business justification |
| Anti-Marketing Metrics | All metrics include when/what/how — no hollow claims |
| Eval is Non-Negotiable | Eval harness built before agent workflow, with publishable golden set |
| Guardrails & Observability | LangSmith traces every node; Guardrail node validates grounding |
| Asset Compound Interest | 3 standalone publishable packages: catalyst-data, catalyst-eval, catalyst-mcp |
| High Completion > Quick Demos | One pillar fully complete beats three half-built |
| Four Pillars of AI Apps | Orchestration (LangGraph), Context (RAG), Agents (MCJ), Infrastructure (eval harness) |

### 1.2 Three Publishable Packages

| Package | PyPI name | Standalone value |
|---|---|---|
| `packages/data-core` | `catalyst-data` | Financial data ingestion → clean Markdown → SQLite. Anyone building financial AI can use this. |
| `packages/eval` | `catalyst-eval` | Financial attribution evaluation framework with domain-specific metrics. No equivalent exists. |
| `packages/mcp` | `catalyst-mcp` | MCP server exposing data-core as tools for Claude Desktop/Cursor. |

### 1.3 Timeline

| Milestone | Date | Deliverable |
|---|---|---|
| Design complete | Apr 2 | This document |
| Data-core v2 | Apr 5 | Consolidated package, schema, Polygon connector, pipeline |
| Eval harness | Apr 7 | Golden set, metrics, harness, experiment infrastructure |
| Agent workflow | Apr 9 | LangGraph MCJ, run experiments, generate eval report |
| **Midterm** | **Apr 10** | **Demo: pipeline + eval report + agent comparison** |
| Frontend | May 5 | React dashboards (geo map, candlestick + events, NLP query) |
| MCP + polish | May 12 | MCP server, integration, documentation |
| **Thesis** | **May 20** | **Full system submitted** |

---

## 2. Project Structure

```
Catalyst/
├── packages/
│   ├── data-core/                     → pip install catalyst-data
│   │   ├── pyproject.toml
│   │   ├── catalyst_data/
│   │   │   ├── __init__.py
│   │   │   ├── config.py              RatePolicy, environment config
│   │   │   ├── rate_limiter.py        TokenBucketLimiter per provider
│   │   │   ├── models.py             CatalystDataRequest, DataAsset, OHLCVBar
│   │   │   ├── connectors/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── base.py            SourceConnector protocol
│   │   │   │   ├── polygon.py         OHLCV + news (PRIMARY) — NEW
│   │   │   │   ├── fmp.py             fundamentals
│   │   │   │   ├── fred.py            macro rates
│   │   │   │   ├── gdelt.py           global events
│   │   │   │   ├── sec_edgar.py       SEC filings
│   │   │   │   ├── finnhub.py         company news supplement
│   │   │   │   └── yfinance.py        fundamentals fallback
│   │   │   ├── pipeline/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── stages.py          StageResult, PipelineContext
│   │   │   │   ├── ingest.py          Stage 1: validate raw fetch
│   │   │   │   ├── clean.py           Stage 2: normalize + title dedup
│   │   │   │   ├── transform.py       Stage 3: → structured Markdown
│   │   │   │   └── align.py           Stage 4: news → trading day alignment
│   │   │   ├── storage/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── sqlite.py          Bronze/Silver tables, init_db, upsert, query
│   │   │   │   └── lancedb.py         Gold layer: vector index build + query
│   │   │   ├── dedup/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── hard.py            SHA256 title hash (at clean stage)
│   │   │   │   └── semantic.py        cosine >0.92 (at index build)
│   │   │   ├── transmuter.py          HTML→Markdown, JSON→Markdown table
│   │   │   └── orchestrator.py        process_request: ties it all together
│   │   ├── scripts/
│   │   │   ├── smoke_test.py          3-day fetch for 1 ticker
│   │   │   ├── backfill.py            golden set dates or full year
│   │   │   ├── build_index.py         Silver → Gold (LanceDB embeddings)
│   │   │   └── daily_fetch.py         production cron job
│   │   └── tests/
│   │       ├── test_connectors.py
│   │       ├── test_pipeline.py
│   │       ├── test_dedup.py
│   │       ├── test_storage.py
│   │       └── fixtures/              saved JSON responses for offline testing
│   │
│   ├── eval/                          → pip install catalyst-eval
│   │   ├── pyproject.toml
│   │   ├── catalyst_eval/
│   │   │   ├── __init__.py
│   │   │   ├── schema/
│   │   │   │   ├── golden_event.py    GoldenEvent, Cause, CauseCategory
│   │   │   │   └── result.py          AttributionResult (agent contract)
│   │   │   ├── metrics/
│   │   │   │   ├── base.py            BaseMetric protocol
│   │   │   │   ├── attribution_f1.py
│   │   │   │   ├── category_accuracy.py
│   │   │   │   ├── grounding_rate.py
│   │   │   │   ├── temporal_precision.py
│   │   │   │   └── confidence_calibration.py
│   │   │   ├── harness/
│   │   │   │   ├── runner.py          evaluate(predict_fn, golden_set, metrics)
│   │   │   │   └── experiment.py      compare(configs) → ComparisonReport
│   │   │   └── reports/
│   │   │       ├── markdown.py        thesis-ready comparison tables
│   │   │       └── json_export.py
│   │   ├── golden_set/
│   │   │   ├── v1.jsonl               15 verified events
│   │   │   └── README.md              annotation guidelines
│   │   └── tests/
│   │
│   └── mcp/                           → pip install catalyst-mcp (POST-MIDTERM)
│
├── catalyst/                           The integrated agent + API
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── state.py                   AttributionState (LangGraph typed state)
│   │   ├── graph.py                   build_graph(use_critic=True) → CompiledGraph
│   │   ├── nodes/
│   │   │   ├── miner.py              hybrid retrieval + reranking
│   │   │   ├── critic.py             evidence grading + filtering
│   │   │   └── judge.py              synthesis + attribution report
│   │   └── prompts/
│   │       ├── critic.md
│   │       └── judge.md
│   ├── api/
│   │   ├── main.py                    FastAPI app
│   │   ├── routes/
│   │   │   ├── data.py               /api/data/* (query SQLite)
│   │   │   ├── attribution.py        /api/attribute (trigger agent)
│   │   │   └── eval.py               /api/eval/* (run harness)
│   │   └── config.py                 CatalystConfig (all API keys, env)
│   └── adapters/
│       └── catalyst_adapter.py        wraps graph into predict_fn for eval
│
├── web/                                React frontend (POST-MIDTERM)
├── docs/
├── data/                               gitignored, local databases
└── pyproject.toml                      root project
```

**Migration from current code:** The existing `data_core/` folder and `catalyst_data/` folder both get consolidated into `packages/data-core/catalyst_data/`. The `core_references/` folder is deleted (reference files are in `~/Desktop/references/`).

---

## 3. Data-Core

### 3.1 Provider Strategy

| Provider | What we fetch | Rate limit | Priority |
|---|---|---|---|
| **Polygon.io** | OHLCV daily bars + ticker news | 5 req/min (free) | P0 — must have |
| **FMP** | Income statement, balance sheet, cash flow | 250 calls/day | P1 |
| **yfinance** | Same 3 statements (FMP fallback) | No published limit, treat gently | P1 |
| **FRED** | Fed funds, CPI, 10Y yield, VIX, unemployment | 120 req/min | P1 |
| **Finnhub** | Company news supplement | 60 req/min, 30 req/sec | P2 |
| **SEC EDGAR** | 10-K, 10-Q, 8-K filings | 10 req/sec | P2 |
| **GDELT** | Global event articles | ~1 req/5.5s (unofficial) | P2 |

For midterm: Polygon + FMP + yfinance + FRED are must-haves. Others are nice-to-haves.

### 3.2 Rate Limiting

Per-provider `RatePolicy` loaded from environment config. Replaces all `_last_api_request_time` globals.

```python
@dataclass
class RatePolicy:
    min_interval_sec: float     # minimum time between requests
    max_concurrent: int         # asyncio.Semaphore limit
    daily_budget: int | None    # None = unlimited

RATE_POLICIES = {
    "dev": {
        "polygon":  RatePolicy(12.0, 1, None),    # 5/min
        "finnhub":  RatePolicy(1.0,  3, None),    # 60/min
        "fred":     RatePolicy(0.5,  3, None),    # 120/min
        "gdelt":    RatePolicy(5.5,  1, None),    # sequential only
        "fmp":      RatePolicy(1.0,  2, 250),     # 250/day
        "sec":      RatePolicy(0.2,  3, None),    # 5/sec conservative
        "yfinance": RatePolicy(2.0,  1, None),    # treat gently
    },
    "test": {
        # all mocked, no network
    },
}
```

Implementation: `TokenBucketLimiter` with `asyncio.Semaphore` + interval enforcement + daily counter. Pattern from crawl4ai's `RateLimiter` + situation-monitor's `registry.ts`.

### 3.3 Pipeline: 4 Stages

```
Raw API → [Ingest] → [Clean] → [Transform] → [Align]
              ↓           ↓           ↓            ↓
         raw_assets   clean_assets  content_md  news_alignment
         (Bronze)     (Silver)      (Silver)    (Alignment)
```

**Stage 1 — Ingest:** Validate HTTP responses, store raw JSON (zlib compressed) in `raw_assets`.

**Stage 2 — Clean:** Normalize raw data + title dedup.

- Financial sources: merge multi-endpoint data, take first item from lists
- News sources: extract article list, then **title dedup**: normalize title (lowercase, strip punctuation) → SHA256 hash → skip if hash exists in same (ticker, date) group
- Other: extract text/HTML payload

**Stage 3 — Transform:** Convert to structured Markdown with consistent format:

News articles:
```markdown
## AAPL: Apple Reports Record Q1 Revenue
*Source: Reuters via Polygon | 2026-01-28 16:05 ET | Category: earnings*

Apple reported quarterly revenue of $124.3B, beating analyst estimates...

## References
[1] https://reuters.com/apple-q1: Apple Reports Record Q1 Revenue
```

Financial statements:
```markdown
## AAPL Fundamentals — Income Statement (FY 2025)
| Metric | Value |
|---|---|
| Total Revenue | $394,328,000,000 |
| Net Income | $96,995,000,000 |
*Source: FMP API | Retrieved: 2026-01-28 | Period: annual*
```

Macro data:
```markdown
## Macro Indicators — 2026-01-15
| Series | Value | Previous | Change |
|---|---|---|---|
| Fed Funds Rate (DFF) | 5.50% | 5.25% | +0.25% |
*Source: FRED | Retrieved: 2026-01-28*
```

**Stage 4 — Align (NEW):** Map news `published_utc` to trading day. Adapted from PokieTicker's `alignment.py`:

- Published before 9:30 ET → affects that trading day
- Published during trading hours → affects that day
- Published after 16:00 ET → affects NEXT trading day
- Weekend/holiday → shifts forward to next trading day

Computes forward returns: T+0, T+1, T+3, T+5 from OHLCV close prices. Stored in `news_alignment` table.

### 3.4 Database Schema

```sql
-- BRONZE: raw fetched data, never modified after insert
CREATE TABLE IF NOT EXISTS raw_assets (
    asset_id        TEXT PRIMARY KEY,        -- SHA256(ticker|date|source|version)
    ticker          TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    reference_date  TEXT NOT NULL,            -- YYYY-MM-DD
    fetched_at      TEXT NOT NULL,            -- UTC ISO
    data_version    TEXT NOT NULL DEFAULT 'v1',
    content_raw     BLOB NOT NULL,            -- zlib compressed JSON
    http_status     INTEGER,
    metadata_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_raw_ticker_date ON raw_assets(ticker, reference_date);

-- SILVER: cleaned Markdown, deduplicated
CREATE TABLE IF NOT EXISTS clean_assets (
    asset_id        TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    reference_date  TEXT NOT NULL,
    cleaned_at      TEXT NOT NULL,
    content_md      TEXT NOT NULL,
    title_hash      TEXT,
    is_duplicate    INTEGER DEFAULT 0,
    FOREIGN KEY (asset_id) REFERENCES raw_assets(asset_id)
);
CREATE INDEX IF NOT EXISTS idx_clean_ticker_date ON clean_assets(ticker, reference_date);
CREATE INDEX IF NOT EXISTS idx_clean_title_hash ON clean_assets(title_hash);

-- OHLCV price data
CREATE TABLE IF NOT EXISTS ohlcv (
    symbol          TEXT NOT NULL,
    date            TEXT NOT NULL,
    open            REAL, high REAL, low REAL, close REAL,
    volume          REAL,
    source          TEXT DEFAULT 'polygon',
    PRIMARY KEY (symbol, date)
);

-- News-to-trading-day alignment with forward returns
CREATE TABLE IF NOT EXISTS news_alignment (
    asset_id        TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    published_utc   TEXT,
    ret_t0          REAL, ret_t1 REAL, ret_t3 REAL, ret_t5 REAL,
    PRIMARY KEY (asset_id, ticker),
    FOREIGN KEY (asset_id) REFERENCES clean_assets(asset_id)
);
CREATE INDEX IF NOT EXISTS idx_align_ticker_date ON news_alignment(ticker, trade_date);

-- Agent attribution results
CREATE TABLE IF NOT EXISTS attributions (
    id              TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    causes_json     TEXT NOT NULL,
    summary_md      TEXT NOT NULL,
    grounding_rate  REAL,
    model_id        TEXT,
    agent_config    TEXT,
    token_count     INTEGER,
    cost_usd        REAL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attr_ticker_date ON attributions(ticker, trade_date);

-- Eval golden set
CREATE TABLE IF NOT EXISTS golden_events (
    id              TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    price_move_pct  REAL NOT NULL,
    causes_json     TEXT NOT NULL,
    source_ids      TEXT,
    annotator       TEXT DEFAULT 'manual',
    created_at      TEXT NOT NULL
);
```

SQLite pragmas at init:
```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

**Business justification for SQLite over PostgreSQL:** Single-user research tool. Zero-config, single file, portable as `assets.db.zst` snapshot. No server process, no Docker. Users clone + download = running in minutes. See ADR-001.

### 3.5 Deduplication

| Type | When | How | Cost |
|---|---|---|---|
| **PK dedup** | At insert | `asset_id = SHA256(ticker\|date\|source\|version)` — ON CONFLICT UPDATE | Zero |
| **Title dedup** | At clean stage | Normalize title → SHA256, skip if hash exists in same (ticker, date) | Zero |
| **Semantic dedup** | At index build | Group by (ticker, date) → batch embed with bge-m3 → cosine similarity matrix → flag pairs >0.92 | Heavy — only at build time |

### 3.6 Data Ingestion Phases

| Phase | When | What | Duration |
|---|---|---|---|
| **Smoke test** | Day 1 | 1 ticker (AAPL), 3 days, all providers | ~10 min |
| **Golden set fetch** | Day 4-5 | 15 events × ±3 day window = ~90 pairs | ~45 min |
| **Full year backfill** | Post-midterm | 30-50 tickers × 365 days | Overnight batch |
| **Daily cron** | Production | All tickers, today's data, 21:00 ET weekdays | ~15 min |

### 3.7 Gold Layer: LanceDB Index

Built by `scripts/build_index.py` from Silver layer:

1. Load all `clean_assets` where `is_duplicate=0`
2. Chunk by source type (news: per-article ~500-1500 tokens, SEC filings: section-based ~800 tokens with 100 token overlap, financial statements: per-statement, macro: per-series)
3. Run semantic dedup within (ticker, date) groups — flag pairs >0.92 cosine
4. Embed non-duplicate chunks with bge-m3
5. Upsert into LanceDB with metadata: `{asset_id, ticker, date, source_type, chunk_idx}`
6. Create full-text search index for BM25

---

## 4. Agent Workflow: Miner-Critic-Judge

### 4.1 Framework Choice

**LangGraph** — explicit state machine with typed state, conditional edges, checkpointing, native LangSmith tracing.

Why not alternatives:
- **Raw LangChain LCEL:** No state management, can't checkpoint mid-workflow, hard to debug
- **CrewAI:** Abstracts away the graph — can't inspect individual node decisions. Violates observability requirement
- **AutoGen:** Designed for conversational multi-agent, not structured pipelines

See ADR-003 for full analysis.

### 4.2 LangGraph State

```python
class AttributionState(TypedDict):
    # Input
    ticker: str
    trade_date: str
    query: str | None              # None for candle-click, string for NLP
    price_move_pct: float | None

    # Miner output
    retrieved_chunks: list[dict]   # hybrid retrieval results (top-20)
    reranked_chunks: list[dict]    # after reranker (top-8)

    # Critic output
    graded_evidence: list[dict]    # scored + category-tagged chunks
    critic_reasoning: str          # chain-of-thought for LangSmith

    # Judge output
    causes: list[dict]             # [{text, category, confidence, evidence_ids, direction}]
    summary_md: str                # final Markdown report with citations
    grounding_rate: float | None

    # Metadata
    token_count: int
    model_id: str
```

### 4.3 The Three Nodes

**Miner (no LLM — deterministic retrieval):**

1. Hybrid search: BM25 + vector in parallel → merge with Reciprocal Rank Fusion (RRF) → top-20
2. Rerank with bge-reranker-v2 cross-encoder → top-8
3. Filter by ticker + date ±3 day window

Why hybrid over vector-only: Financial text is full of exact terms (ticker symbols, dollar amounts, dates) where BM25 excels. Vector search catches semantic similarity (paraphrases). RRF merges both ranking lists by rank position, avoiding the scale mismatch between BM25 scores and cosine similarity.

See ADR-002 for full RAG algorithm details.

**Critic (1 LLM call — evidence grading):**

Input: 8 reranked chunks + ticker + date + price move
Output per chunk: `{relevance: 0-1, category, temporal_match: bool, reasoning}`
Filter: keep chunks with relevance > 0.5

The Critic's job is ONLY to grade evidence — it has no pressure to produce a narrative, so it doesn't cherry-pick evidence to support a predetermined story.

**Judge (1 LLM call — synthesis):**

Input: graded evidence from Critic
Output: `{causes: [{text, category, confidence, evidence_ids, direction}], summary_md, self_grounding_check}`

Rules enforced in prompt:
- Confidence scores must sum to <= 1.0
- Every claim must cite an evidence chunk as `[chunk_id]`
- "Unknown/insufficient evidence" is a valid cause
- Maximum 5 causes
- Do NOT fabricate — if evidence is insufficient, say so

### 4.4 Graph Construction

```python
def build_attribution_graph(use_critic: bool = True) -> CompiledGraph:
    graph = StateGraph(AttributionState)
    graph.add_node("miner", miner)
    if use_critic:
        graph.add_node("critic", critic)
    graph.add_node("judge", judge)

    graph.set_entry_point("miner")
    if use_critic:
        graph.add_edge("miner", "critic")
        graph.add_edge("critic", "judge")
    else:
        graph.add_edge("miner", "judge")
    graph.add_edge("judge", END)

    return graph.compile()
```

`use_critic=True` → Miner-Critic-Judge (default)
`use_critic=False` → Miner-Judge baseline (for experiment E2)

### 4.5 Token Budget

| Node | Est. tokens | Cost (Claude Sonnet) |
|---|---|---|
| Miner | 0 (no LLM) | $0 |
| Critic | ~3,800 | ~$0.014 |
| Judge | ~3,700 | ~$0.016 |
| **MCJ total** | **~7,500** | **~$0.030** |
| **Baseline total** | **~5,000** | **~$0.020** |

15 golden set evals: ~$0.45 per run. Affordable.

### 4.6 Two Interaction Modes

**Mode A — Candle Click (pre-computed, no LLM):**
- `GET /api/data/attribution?ticker=AAPL&date=2026-01-15`
- Reads from `attributions` table (pre-computed by batch job)
- Displays PokieTicker-style panel: causes by category, confidence bars, evidence links

**Mode B — NLP Query (live agent):**
- `POST /api/attribute {query: "Why did AAPL drop last Tuesday?"}`
- Parse query → extract ticker + date
- Run LangGraph workflow → stream response via WebSocket

**Batch pre-computation (powers Mode A):**
```bash
python -m catalyst.agents.batch --ticker AAPL --from 2025-04-01 --to 2026-04-01 --threshold 1.5
# Runs attribution for each day where |return| > 1.5%
```

### 4.7 API Key Interface

```python
class CatalystConfig(BaseSettings):
    # LLM (required for agent)
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    LLM_PROVIDER: str = "anthropic"
    LLM_MODEL: str = "claude-sonnet-4-20250514"

    # Data providers (optional — degrade gracefully)
    POLYGON_API_KEY: str = ""
    FMP_API_KEY: str = ""
    FRED_API_KEY: str = ""
    FINNHUB_API_KEY: str = ""
    SEC_USER_AGENT: str = "Catalyst/1.0 (academic research)"

    # Local models (auto-downloaded)
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    RERANKER_MODEL: str = "BAAI/bge-reranker-v2-m3"

    class Config:
        env_file = ".env"
```

Three degradation levels:
1. **Full** (all keys): live ingestion + agent
2. **Snapshot only** (no provider keys): use pre-built assets.db.zst
3. **Data-core only** (no LLM key): ingestion works, no attribution

---

## 5. Evaluation Framework

### 5.1 Design Principle

`catalyst-eval` is designed as an **agent-agnostic financial attribution evaluation framework**. The harness takes a `predict_fn: (ticker, date) -> AttributionResult` callable — Catalyst's own agent is one adapter, but any agent implementing this interface can be evaluated.

### 5.2 Schema

```python
class CauseCategory(str, Enum):
    EARNINGS = "earnings"
    MACRO = "macro"
    GEOPOLITICAL = "geopolitical"
    SECTOR = "sector"
    TECHNICAL = "technical"
    REGULATORY = "regulatory"

class Cause:
    text: str                       # "China expanded H20 chip export ban"
    category: CauseCategory
    weight: float                   # 0.0-1.0, must sum to 1.0
    temporal_anchor: str            # when cause became known
    evidence_ids: list[str]

class GoldenEvent:
    id: str
    ticker: str
    trade_date: str
    price_move_pct: float
    causes: list[Cause]

class AttributionResult:
    """Contract: any agent must return this to be evaluated."""
    ticker: str
    trade_date: str
    causes: list[PredictedCause]
    summary: str
    retrieved_chunks: list[str]
```

### 5.3 Metrics (5 financial-domain metrics)

| Metric | What it catches | How computed | Novel? |
|---|---|---|---|
| **Attribution F1** | Wrong causes identified | Embed predicted + golden causes with bge-m3, cosine match (>0.8 = match), compute precision/recall/F1 with partial credit | Adapted from BizFinBench AEA |
| **Category Accuracy** | Correct cause, wrong type | Exact match on CauseCategory for matched causes | **New** |
| **Grounding Rate** | Hallucinated claims | RAGAS faithfulness metric: for each claim in summary, check if supporting chunk exists in retrieved_chunks | Wraps RAGAS |
| **Temporal Precision** | Right event, wrong day | Check if agent's trade_date assignment matches golden temporal_anchor | **New** |
| **Confidence Calibration** | Over/under-confident agent | Group predictions by confidence bucket, check accuracy per bucket. sklearn calibration_curve. Caveat: 15 samples is insufficient for statistical significance. | Standard, applied to attribution |

Financial Entity Accuracy (NER + value matching) deferred to post-midterm due to implementation complexity.

### 5.4 Harness

```python
# Core function:
def evaluate(
    predict_fn: Callable[[str, str], AttributionResult],
    golden_set: list[GoldenEvent],
    metrics: list[BaseMetric],
) -> EvalReport

# Experiment comparison:
def compare(
    configs: dict[str, Callable],     # {"baseline": fn1, "mcj": fn2}
    golden_set: list[GoldenEvent],
    metrics: list[BaseMetric],
) -> ComparisonReport
```

### 5.5 Experiments

**Core Experiments (for midterm):**

| # | Name | Independent variable | Key metrics |
|---|---|---|---|
| E1 | Raw Chunks vs Markdown | Transform stage ON/OFF | Attribution F1, Grounding Rate |
| E2 | Single Agent vs MCJ | Critic node ON/OFF | Grounding Rate, Token Cost |
| E3 | Hybrid vs Vector-Only Retrieval | BM25 component ON/OFF | Retrieval P@8, Attribution F1 |

**Extended Experiments (for thesis, post-midterm):**

| # | Name | Independent variable | Key metrics |
|---|---|---|---|
| E4 | Reranker Ablation | Reranker ON/OFF | P@8, F1, Latency |
| E5 | Model Comparison | Claude Sonnet vs GPT-4o vs Haiku | F1, Grounding Rate, Cost |
| E6 | Chunk Size Sensitivity | 256/512/1024 token chunks | F1, Grounding Rate |
| E7 | Source Coverage | 1 source vs 3 vs all 6 | Attribution Recall |

**LangSmith Observability Analysis (for thesis):**

| Analysis | What it produces |
|---|---|
| Latency breakdown | Bar chart: time per node across golden set |
| Failure mode classification | Pie chart: wrong retrieval (40%), wrong synthesis (30%), temporal confusion (20%), entity confusion (10%) |
| Token efficiency | Table: input/output tokens per node |
| Critic effectiveness | Rejection rate, rejection accuracy |

### 5.6 Eval Report Structure

The eval report serves triple duty: thesis Chapter 4, interview demo, package documentation.

```
1. Dataset (golden set description, methodology, limitations)
2. Metrics (definition, computation, thresholds)
3. Core Experiments E1-E3 (hypothesis, setup, results table, finding)
4. System Observability (LangSmith analysis)
5. Extended Experiments E4-E7 (if applicable)
6. Limitations and Future Work (honest assessment)
7. Reproducibility (commands to re-run)
```

### 5.7 Known Limitations (must be documented)

1. Golden set is 15 events — F1 has ~±0.07 variance per sample
2. Grounding rate uses LLM-as-judge — circular evaluation risk. Mitigate: use stronger model as judge, spot-check 10% manually
3. Attribution is fundamentally subjective — two analysts may disagree
4. Agent can only attribute what the data contains — data gaps = attribution gaps

---

## 6. RAG Algorithm

### 6.1 Hybrid Retrieval: BM25 + Vector

Two parallel retrieval paths, merged with Reciprocal Rank Fusion:

**Vector path:** Embed query with bge-m3 → cosine similarity search in LanceDB → top-20. Catches semantic similarity (paraphrases, related concepts).

**BM25 path:** Full-text search on `content_md` field in LanceDB → top-20. Catches exact keyword matches (ticker symbols, dollar amounts, specific names).

**RRF merge:** For each document, `RRF_score = sum(1/(k + rank_in_list))` where `k=60`. Documents ranked highly in both lists score highest. Avoids the scale mismatch between BM25 scores and cosine similarity.

See ADR-002 for detailed algorithm and justification.

### 6.2 Reranking: bge-reranker-v2

Cross-encoder reranking on the merged top-20 → top-8.

Why cross-encoder after bi-encoder: Bi-encoder embeds query and doc independently — fast but misses query-document interaction. Cross-encoder sees (query, doc) as a pair — 10x slower but significantly more accurate. Using it as stage 2 (not stage 1) balances cost and quality.

Model: `BAAI/bge-reranker-v2-m3`, ~570MB, ~200ms per batch of 20 pairs.

### 6.3 Chunking Strategy

| Source Type | Strategy | Size | Overlap |
|---|---|---|---|
| News articles | Per-article (1 article = 1 chunk) | ~500-1500 tokens | None |
| SEC filings | Section-based at Markdown headers | ~800 tokens | 100 tokens |
| Financial statements | Per-statement | ~300-600 tokens | None |
| Macro rates | Per-series per-date | ~100-200 tokens | None |

### 6.4 Full RAG Pipeline

```
Query → Hybrid Retrieval (BM25 + Vector, parallel)
           → RRF merge → top-20
              → Reranker (bge-reranker-v2) → top-8
                 → Critic (LLM grades each chunk) → 3-6 graded chunks
                    → Judge (LLM synthesizes attribution) → AttributionResult
```

---

## 7. Frontend (Post-Midterm)

### 7.1 Stack

React + TypeScript + Vite. Three dashboard views:

1. **Geopolitical map:** react-simple-maps + GDELT GEO 2.0
2. **Attribution dashboard:** lightweight-charts (TradingView) + event markers + PokieTicker-style cause panels
3. **NLP query chat:** conversational agent interface via WebSocket

### 7.2 Data Flow

| View | Data source | Why |
|---|---|---|
| Geopolitical map | GDELT GEO API direct | Live display, ephemeral, not worth storing |
| Everything else | FastAPI → SQLite/LanceDB | All pipeline data goes through backend |
| NLP query | WebSocket → LangGraph stream | Real-time agent execution |

---

## 8. Integration: LangSmith + MCP

### 8.1 LangSmith

Set env vars — all LangGraph node executions auto-trace:
```
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=<key>
LANGCHAIN_PROJECT=catalyst
```

No extra code for basic tracing. For the observability analysis (Section 5.6), extract trace data via LangSmith API.

### 8.2 MCP Server (Post-Midterm)

Thin wrapper exposing data-core functions as MCP tools:
- `get_stock_data(ticker, date)` → cleaned Markdown from SQLite
- `attribute_move(ticker, date)` → triggers LangGraph workflow

---

## 9. Git Strategy

```
main                               tagged releases only
├── feat/data-core-v2              Phase 1: consolidate, schema, connectors, pipeline
├── feat/eval-harness              Phase 2: golden set, metrics, harness
├── feat/agent-workflow            Phase 3: LangGraph MCJ, experiments, report
├── feat/frontend                  Phase 4 (post-midterm): React dashboards
└── feat/mcp-server                Phase 5 (post-midterm): MCP integration
```

Each branch merges to `main` via PR when phase is complete + tests pass.

---

## 10. Environment Strategy

| Env | Database | Config |
|---|---|---|
| test | `:memory:` SQLite | All connectors mocked, fixtures in `tests/fixtures/` |
| dev | `data/dev_assets.db` | Real APIs, conservative rate limits |
| prod | `data/assets.db` | Full dataset, daily cron fetch |

Pre-built snapshot published as GitHub Release: `assets.db.zst`. Users run `catalyst data download` to get started without API keys.

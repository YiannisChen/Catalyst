__CATALYST__

Financial Attribution & Research System

System Architecture Whitepaper

__Yiannis Chen__

Chengdu University of Information Technology

*Version 0\.1  —  March 2026  |  Draft*

__Abstract__

Catalyst is a high\-fidelity financial attribution and research system designed to answer a deceptively hard question: why did a stock move on a given day? The system ingests multi\-source financial data — news, SEC filings, macro rates, and price data — processes it into agent\-friendly Markdown, and orchestrates a LangGraph\-based multi\-node workflow to synthesize causal attribution reports with quantified confidence scores\. Unlike existing tools that display news next to charts, Catalyst traces specific market events to specific price movements with measurable grounding\. This document defines the system architecture, technical decision chain, data strategy, agent workflow, and evaluation methodology for Catalyst v1\.

# __Table of Contents__

# __1\. Product Vision__

Catalyst exists to solve a concrete business problem: financial information is abundant, but causal attribution is nearly absent\. A trader watching AAPL fall 4% on a given day can find dozens of news articles, but no tool that systematically traces which specific event drove the move — with what confidence, supported by which evidence\.

Catalyst answers this across three independent, publishable packages:

| Pillar | Package | Responsibility | Status |
| --- | --- | --- | --- |
| Data\-Core | packages/data\-core | Ingestion, cleaning, persistence | V1\.5 — implemented |
| Agent Engine | packages/agents | LangGraph workflow, RAG, attribution | Planned |
| Eval Suite | packages/eval | Metrics, golden set, CI gates | Priority — build first |
Each pillar is designed to be useful standalone\. Data\-Core published as an open\-source Python package demonstrates enterprise\-grade data engineering independently of the agent layer — a deliberate career\-asset decision\.

__Core Design Philosophy__

Technology is chosen to solve specific business problems, not to demonstrate familiarity with buzzwords\. Every architectural decision in this document includes a justification: what pain point it solves and what the measurable outcome is\. This is the Technical Decision Chain standard\.

# __2\. Goals and Non\-Goals__

## __2\.1 Primary Goals__

- Graduation project: a complete, demonstrable system with quantified quality metrics
- Career asset: a published open\-source Data\-Core package proving engineering maturity
- Attribution accuracy: measurable F1 score against a hand\-crafted golden set of known market events
- Hallucination control: every claim in the output is traceable to a source chunk \(grounding\_rate >= 0\.85\)
- Reproducibility: other developers can clone the repo, download a data snapshot, and run the full system without paying for any APIs

## __2\.2 Non\-Goals \(V1\)__

- Real\-time trading signals — attribution is retrospective by design
- Multi\-tenant SaaS deployment — single\-user research tool
- Coverage of all US equities — curated universe of 30–50 tickers
- LLM fine\-tuning — all inference uses off\-the\-shelf models via API
- Social media sentiment \(Reddit, X\) — excluded due to noise/signal ratio
- Non\-US equity markets, options, or crypto

# __3\. Data Strategy__

## __3\.1 Provider Philosophy__

The business case for attribution is retrospective analysis — we never need real\-time data\. This justifies a free\-tier\-first provider strategy\. All providers below offer free tiers sufficient for a curated ticker universe\. Users bring their own API keys; the system degrades gracefully when a provider is unavailable\.

| Source Type | Provider | Free Limit | Business Justification |
| --- | --- | --- | --- |
| OHLCV prices | Polygon\.io \(free\) | 5 calls/min, EOD | Sufficient for daily attribution; no intraday needed |
| Company news | Polygon\.io \(free\) | Included with OHLCV | Ticker\-scoped news with date filtering |
| Fundamentals | yfinance \(unofficial\) | No hard limit | Income stmt, balance sheet, earnings — zero cost |
| Macro rates | FRED \(official, free\) | 120 calls/min | Fed funds rate, CPI, yield curve — authoritative source |
| SEC filings | SEC EDGAR \(official, free\) | 10 req/sec | 10\-K, 10\-Q, 8\-K — gold\-standard regulatory data |
| Global events | GDELT \(free\) | ~1 req/5\.5s | Macro event correlation; throttled by design |
__Technical Decision — Why Not FMP Paid Tier?__

FMP Starter requires $228/year annual commitment\. This is a barrier for every user of an open\-source package\. The free\-tier stack above covers all data types needed for attribution with zero annual lock\-in\. If a user has an FMP or Polygon paid key, the provider abstraction accepts it transparently via environment variable\.

## __3\.2 Data Ingestion — Two\-Phase Model__

### __Phase 1 — Historical Backfill \(one\-time\)__

A CLI script fetches 1 year of data for the curated ticker universe, processes it through the full pipeline, and exports a compressed snapshot\. The snapshot is published as a GitHub Release asset, not committed to the repository\.

- Snapshot format: assets\.db\.zst \(zstandard\-compressed SQLite\)
- Users run: catalyst data download — unzips snapshot and the system is immediately queryable
- Eliminates API cost and setup friction for all downstream users
- This snapshot is the 'asset compound interest' artifact — built once, reused indefinitely

### __Phase 2 — Daily Incremental Ingestion__

APScheduler \(not Celery — Celery requires an external broker process with no benefit at this scale\) runs after market close at 6pm ET each trading day\.

- Frequency: once daily post\-close, not hourly — no business case for intra\-day attribution of EOD price movements
- ET\-alignment: all date fields normalized to US/Eastern Time to prevent misattribution across timezone boundaries
- Fallback: if a primary provider fails, the system retries with the designated fallback before recording a failed fetch

## __3\.3 Async Concurrency and Rate Limiting__

Technical Decision — async justification: a naive sequential fetch across 5 providers takes ~25 seconds\. asyncio\-based concurrent fetch reduces this to ~5–7 seconds — a 5x improvement that directly affects backfill wall\-clock time for large historical runs\.

Rate limiting uses per\-provider asyncio\.Semaphore objects\. Sleep timers waste wall\-clock time; Semaphores allow the maximum safe concurrency per provider independently:

| Provider | Semaphore | Justification |
| --- | --- | --- |
| Polygon\.io | Semaphore\(4\) | Stay under 5/min free limit with safety margin |
| FRED | Semaphore\(10\) | 120/min limit; 10 concurrent is conservative and safe |
| SEC EDGAR | Semaphore\(8\) | 10 req/sec; 8 leaves safety margin for bursts |
| GDELT | Semaphore\(1\) | Documented 5\.5s throttle; serial access enforced |
| yfinance | Semaphore\(5\) | Unofficial API; conservative to avoid rate bans |
## __3\.4 Data Processing Pipeline__

Every asset passes through three explicit stages\. Failures at any stage are persisted with is\_final=False so raw data is never lost\.

1. run\_ingest — fetch raw payload, persist zlib\-compressed content\_raw \(bronze layer\)
2. run\_clean — deterministic HTML stripping \(remove nav, footer, script, style, aside\), normalize whitespace, enforce UTF\-8
3. run\_transform — convert to Markdown: HTML becomes prose paragraphs; financial JSON becomes pipe tables with headers and units \(silver layer\)

__Technical Decision — Why Markdown as the Clean Format?__

LLMs process prose and tables far better than raw JSON or HTML\. \{'revenue': 89500000000\} gives the model no linguistic context for attribution reasoning\. '| Revenue | $89\.5B | \+8% YoY |' provides schema, magnitude, and trend in a single token window\. The noise\_reduction\_ratio metric quantifies how much boilerplate was removed — making the transmutation step directly measurable\.

## __3\.5 Deduplication Strategy__

The same earnings announcement often appears in Polygon news, Finnhub news, and GDELT within the same trading day\. Without semantic deduplication, the RAG index over\-retrieves on high\-coverage events and the model may interpret repetition as stronger evidence\.

### __Hard Dedup \(exact\)__

SHA256\(ticker \+ date \+ source\_type \+ data\_version\) as primary key \(asset\_id\)\. Prevents re\-fetching the same article from the same provider\. Implemented in Data\-Core V1\.5\.

### __Soft Dedup \(semantic\)__

At RAG index time: if two articles share the same ticker, same trading day, and cosine similarity > 0\.92, one is marked is\_duplicate=True with a canonical\_asset\_id pointer\. The RAG index ingests only canonical assets\.

# __4\. Database Design__

## __4\.1 Architecture — Two Databases__

__Technical Decision — SQLite \+ LanceDB, not PostgreSQL \+ Chroma__

SQLite is zero\-infrastructure, ships with Python, and is embeddable in the package\. LanceDB natively supports hybrid BM25 \+ vector search with metadata filtering in a file\-based format — no server process required\. Together they deliver full RAG capability with zero operational infrastructure\. PostgreSQL \+ Chroma would add two external process dependencies with no benefit at this scale\.

| Database | File | Purpose | Technology |
| --- | --- | --- | --- |
| Asset store | data/assets\.db | Raw and clean data, price data, run logs | SQLite \(WAL mode\) |
| RAG index | data/index\.lancedb/ | Vector \+ BM25 hybrid search index | LanceDB \(file\-based\) |
## __4\.2 SQLite Schema__

### __data\_assets — bronze and silver layers__

| Column | Type | Description |
| --- | --- | --- |
| asset\_id | TEXT PK | SHA256\(ticker \+ date \+ source\_type \+ data\_version\) |
| ticker | TEXT NOT NULL | US equity ticker, uppercase |
| event\_date | TEXT NOT NULL | ET\-aligned trading date, YYYY\-MM\-DD |
| source\_type | TEXT NOT NULL | e\.g\. polygon\_news, sec\_filing, fred\_rates |
| content\_raw | BLOB | zlib\-compressed original API response \(bronze\) |
| content\_clean | TEXT | Deterministic Markdown output \(silver\) |
| is\_final | BOOLEAN | True only when all three pipeline stages succeeded |
| is\_duplicate | BOOLEAN | True = cross\-provider semantic duplicate |
| canonical\_id | TEXT | FK to canonical asset when is\_duplicate is True |
| noise\_ratio | REAL | len\(clean\_tokens\) / len\(raw\_tokens\) — quality metric |
| metadata | TEXT | JSON: http\_status, pipeline\_flags, failed\_step |
| ingested\_at | TEXT | ISO 8601 UTC timestamp |
| data\_version | TEXT | Pipeline version string for schema migrations |
### __price\_data — OHLCV__

| Column | Type | Description |
| --- | --- | --- |
| ticker | TEXT | Equity ticker \(PK with trade\_date\) |
| trade\_date | TEXT | ET\-aligned trading date, YYYY\-MM\-DD \(PK with ticker\) |
| open / high / low / close | REAL | Adjusted OHLC prices in USD |
| volume | INTEGER | Daily share volume |
| pct\_change | REAL | Daily return computed at ingest time |
### __attribution\_runs — audit and eval log__

| Column | Type | Description |
| --- | --- | --- |
| run\_id | TEXT PK | UUID per attribution query |
| query | TEXT | Raw user query string |
| ticker / event\_date | TEXT | Resolved from query by Query Parser node |
| result\_json | TEXT | Full attribution report as JSON |
| grounding\_rate | REAL | Fraction of claims with traced source chunk |
| evidence\_count | INTEGER | Number of source chunks used in synthesis |
| latency\_ms | INTEGER | Wall\-clock time for the full agent graph run |
| created\_at | TEXT | ISO 8601 UTC timestamp |
## __4\.3 LanceDB RAG Index Schema__

Built from data\_assets where is\_final=True and is\_duplicate=False\. Rebuilt from SQLite at any time — it is a derived artifact, not the source of truth\.

| Field | Type | Description |
| --- | --- | --- |
| chunk\_id | string | UUID; unique per chunk |
| asset\_id | string | FK to data\_assets\.asset\_id |
| ticker | string | Primary metadata filter key |
| event\_date | string | Secondary metadata filter key \(ET\-aligned\) |
| source\_type | string | Tertiary filter key for source diversity checks |
| chunk\_text | string | The Markdown chunk, 512 tokens max |
| embedding | vector\[1536\] | Dense embedding \(text\-embedding\-3\-small or bge\-m3\) |
| chunk\_index | int | Position within parent asset for ordering |
### __Chunking Strategy__

- Long documents \(SEC filings, long news articles\): 512 tokens with 64\-token overlap
- Short documents \(< 100 tokens\): indexed as a single chunk without splitting
- SEC filings: semantic chunking at section boundaries \(Item 1A Risk Factors, Item 7 MD&A\) takes priority over fixed\-window chunking
- The 64\-token overlap prevents evidence from being split across chunk boundaries, which would cause the Grader to score relevant chunks as irrelevant

# __5\. Agent Workflow__

## __5\.1 Framework Decision__

| Option | Verdict | Reason |
| --- | --- | --- |
| Plain LangChain chain | Rejected | No shared state; no conditional routing; no loop\-on\-failure — a dumb sequential pipe |
| Forum\-style multi\-agent | Rejected | Uncontrollable token costs; non\-deterministic output shape; impossible to write CI tests against |
| LangGraph state machine | Selected | Typed shared state; conditional edges; loop\-on\-failure; replayable; CI\-testable output |
## __5\.2 GraphState — The Node Contract__

The GraphState TypedDict is defined before any node is written\. It is the contract between all nodes\. Every node is a pure function: \(GraphState\) \-> GraphState\.

| Field | Type | Set By |
| --- | --- | --- |
| ticker | str | Query Parser |
| event\_date | str | Query Parser |
| event\_type | str | None | Query Parser \(optional classification\) |
| raw\_query | str | User input |
| retrieved\_chunks | list\[Chunk\] | Retriever |
| graded\_chunks | list\[GradedChunk\] | Grader |
| retry\_count | int | Evidence Validator \(loop counter\) |
| evidence\_sufficient | bool | Evidence Validator \(routing decision\) |
| attribution\_draft | AttributionDraft | None | Attribution Synthesizer |
| grounding\_map | dict\[str, str\] | Claim Grounder \(claim text \-> chunk\_id\) |
| grounding\_rate | float | Claim Grounder |
| quality\_flags | list\[str\] | Evidence Validator \+ Claim Grounder |
| final\_report | AttributionReport | None | Output node |
## __5\.3 Node Definitions__

### __Node 1 — Query Parser__

Extracts structured parameters from free\-form user input using a lightweight LLM call with strict JSON output schema\. Stateless and fast — no retrieval, no tool use\.

- Accepts: ticker\+date, event description, or natural language query
- Outputs: normalized ticker \(uppercase\), ET\-aligned event\_date, optional event\_type
- Guardrail: if ticker or date cannot be resolved with confidence, return an error state — never guess

### __Node 2 — Retriever__

Hybrid BM25 \+ vector search against LanceDB, filtered by ticker and a date window \(event\_date \+/\- 3 trading days\)\. Metadata filtering is applied before vector search — never retrieving NVDA data when analyzing AAPL\.

- BM25 weight: 0\.4; vector cosine similarity weight: 0\.6
- Top\-20 candidates enter the cross\-encoder reranker; top\-8 are passed to the Grader
- Source diversity check: if all top\-8 chunks come from one source\_type, broaden the search

### __Node 3 — Grader__

Scores each retrieved chunk on three binary flags using a fast \(Haiku\-class\) LLM call\. This is the cost\-control gate — only relevant chunks proceed to the expensive Attribution Synthesizer\.

- relevant: does this chunk contain information about the specified ticker near the event date?
- stale: is this chunk dated more than 5 trading days from the event? \(flagged, not discarded\)
- noise: is this a duplicate, listicle, or low\-signal filler article?

Chunks with relevant=False and noise=True are dropped\. If fewer than 3 relevant chunks remain, the Grader signals the Evidence Validator to retry\.

### __Node 4 — Evidence Validator \(conditional routing node\)__

The only node that performs conditional routing\. Sufficiency requires all three conditions:

- At least 3 relevant chunks after grading
- At least 2 distinct source\_types represented \(e\.g\. news \+ SEC filing, or news \+ macro data\)
- No chunk older than 10 trading days from the event

If conditions are not met: rewrites the retrieval query \(broader date window, synonym expansion\) and loops back to the Retriever\. Maximum 2 retries\. After 2 failed retries, routes to the Fallback node\.

__Fallback Node__

When evidence is insufficient, the system returns a partial report with data\_quality\_warning: 'Insufficient evidence for confident attribution\. Available sources cover \[N\] relevant events within \[M\] trading days of the specified date\.' The system never fabricates causes when evidence is thin — this is the most important guardrail in the entire pipeline\.

### __Node 5 — Attribution Synthesizer__

The primary LLM call \(Sonnet\-class model\)\. Receives the graded, validated evidence set and produces a structured attribution draft\. The system prompt enforces:

- Up to 3 primary causes, each with a confidence score \(0\.0–1\.0\) and at least one cited chunk\_id
- Causal language must be appropriately hedged: 'primarily driven by', 'contributed to' — never 'caused by'
- The narrative must reference the actual price change percentage for magnitude context
- No claims about events that are not present in the provided evidence chunks

### __Node 6 — Claim Grounder__

Post\-processes the attribution draft to verify every factual claim\. For each claim in the narrative, identifies the supporting chunk and records the mapping in grounding\_map\.

__grounding\_rate = grounded\_claims / total\_claims__\. If grounding\_rate < 0\.85, the node loops back to the Attribution Synthesizer once with a stricter prompt instructing it to remove any claim not directly supported by the evidence\. If grounding\_rate is still below threshold after one retry, the report is returned with a hallucination\_warning flag rather than being suppressed\.

## __5\.4 Output Schema__

| Field | Type | Description |
| --- | --- | --- |
| run\_id | str | UUID; stored in attribution\_runs for audit |
| ticker | str | Resolved ticker |
| event\_date | str | ET\-aligned date |
| price\_change\_pct | float | Actual daily return on the event date |
| causes | list\[Cause\] | Up to 3 causes: label, confidence float, source\_ids list |
| narrative | str | Prose attribution summary with inline source citations |
| grounding\_rate | float | Fraction of claims with traced source \(target >= 0\.85\) |
| evidence\_count | int | Number of source chunks used in synthesis |
| quality\_flags | list\[str\] | e\.g\. stale\_evidence, thin\_evidence, partial\_report |
| latency\_ms | int | Wall\-clock time for the full agent graph run |
# __6\. Evaluation Suite__

__Core Mandate__

An AI project without an evaluation module is a toy\. The eval suite is the first package to scaffold — before writing a single agent node\. The golden set defines what 'working' means\. Everything else is measured against it\.

## __6\.1 Golden Set__

A hand\-curated collection of known market events with verified causes\. Each fixture is a ground truth record against which agent output is scored\. Initial target: 10–15 fixtures across diverse event types \(earnings miss, guidance cut, macro shock, regulatory action, product announcement\)\.

| Field | Description |
| --- | --- |
| ticker | e\.g\. AAPL |
| event\_date | e\.g\. 2024\-08\-01 |
| price\_change\_pct | Actual daily return, e\.g\. \-4\.8 |
| expected\_causes | List of verified cause labels with expected confidence range |
| expected\_source\_types | Which source types must be represented in retrieved evidence |
| notes | Human annotation: what actually happened and why |
Example: AAPL 2024\-08\-01\. Price change: \-4\.8%\. Expected causes: \[\{label: 'revenue\_miss', confidence: \[0\.8, 1\.0\]\}, \{label: 'guidance\_cut', confidence: \[0\.6, 0\.9\]\}\]\. Expected source types: \[sec\_filing, polygon\_news\]\. Notes: Q3 2024 earnings miss; services revenue below consensus; management guided Q4 revenue below analyst estimates\.

## __6\.2 Offline Metrics \(Deterministic\)__

### __Attribution F1__

- True positive: predicted cause label matches expected cause AND confidence is within expected range
- Precision = TP / \(TP \+ FP\) — are the predicted causes correct?
- Recall = TP / \(TP \+ FN\) — were all expected causes found?
- F1 = harmonic mean of precision and recall
- CI gate: attribution F1 >= 0\.70 on the full golden set

### __noise\_reduction\_ratio__

Measures data cleaning effectiveness per asset: len\(clean\_tokens\) / len\(raw\_tokens\)\. Target range is 0\.25–0\.65\. Below 0\.25 suggests over\-stripping; above 0\.65 suggests insufficient boilerplate removal\.

- CI gate: mean noise\_reduction\_ratio across all assets is in \[0\.25, 0\.65\]

## __6\.3 Online Metrics \(Live Traces\)__

### __grounding\_rate__

Primary hallucination control metric\. Logged to attribution\_runs for every production query\. Computed per run as fraction of claims with a traced supporting chunk\.

- CI gate: grounding\_rate >= 0\.85 on all golden set fixtures
- Alert threshold: if 7\-day rolling mean drops below 0\.80, trigger a data freshness investigation

### __RAGAS Metrics__

- Context Precision: fraction of retrieved chunks that are actually relevant
- Context Recall: fraction of expected evidence that was retrieved
- Faithfulness: fraction of claims supported by retrieved context \(complements grounding\_rate\)
- Answer Relevancy: does the attribution narrative address the actual query?

## __6\.4 CI Regression Gates__

| Gate | Threshold | Failure Action |
| --- | --- | --- |
| Attribution F1 \(golden set\) | >= 0\.70 | Block merge |
| grounding\_rate \(golden set\) | >= 0\.85 | Block merge |
| noise\_reduction\_ratio \(mean\) | 0\.25 – 0\.65 | Block merge; review transmuter |
| Unit tests \(data\-core\) | All passing | Block merge |
| Latency p95 \(golden set\) | <= 15 seconds | Warn only; do not block |
__Why Gates, Not Aspirations?__

A metric without a gate is marketing\. A gate without a threshold is a vibe check\. Each threshold above is derived from a specific business requirement: grounding\_rate >= 0\.85 means fewer than 1 in 7 claims is potentially ungrounded — an acceptable bar for research tooling\. These thresholds should be revised upward as the system matures\. They should never be revised downward to make a PR pass\.

# __7\. Development Order__

The recommended build sequence minimizes wasted work by ensuring every stage produces something verifiable before the next stage begins\.

| Phase | Deliverable | Acceptance Condition |
| --- | --- | --- |
| 0\. Golden set | 10–15 fixture JSON files in golden\_set/ | Hand\-verified by developer; covers 3\+ event types |
| 1\. Eval scaffold | packages/eval with scorer \+ RAGAS integration | Can score a manually\-constructed mock output against golden set |
| 2\. Data slice | 3 golden\-set tickers x 30 days through Data\-Core | noise\_reduction\_ratio in target range; Markdown is readable |
| 3\. LanceDB indexer | scripts/build\_index\.py consuming assets\.db | Hybrid search returns correct chunks for golden set queries |
| 4\. Agent workflow | Full LangGraph graph, all 6 nodes | F1 >= 0\.70 and grounding\_rate >= 0\.85 on golden set |
| 5\. Historical backfill | 1\-year snapshot for 30–50 tickers | Only after Phase 4 passes all CI gates |
| 6\. Snapshot release | GitHub Release with assets\.db\.zst | Other users can download and query without any API keys |
| 7\. UI | Attribution dashboard \+ event display | All CI gates still green after UI layer is added |
__The Key Insight on Data Fetching__

Never fetch 1 year of data before verifying the cleaning algorithm\. The correct verification unit is the golden set: fetch data only for the 10–15 golden set tickers and dates, run the pipeline, score against fixtures\. If F1 >= 0\.70 on this small slice, the algorithm is valid — then backfill\. This avoids wasting API quota and compute on data processed through an unvalidated pipeline\.

# __8\. Technical Decision Chain__

Every significant architectural choice is recorded below with its business justification\. This section is the primary artifact demonstrating engineering maturity to technical reviewers\.

| Decision | Choice Made | Alternatives Rejected | Business Justification |
| --- | --- | --- | --- |
| Agent framework | LangGraph | Plain LangChain, CrewAI, forum\-style multi\-agent | Typed shared state; conditional edges; loop\-on\-failure; CI\-testable output shape |
| Asset storage | SQLite | PostgreSQL, DuckDB | Zero\-infra; embeddable in package; sufficient for single\-machine research use |
| RAG index | LanceDB | ChromaDB, FAISS | Native hybrid BM25\+vector; metadata filtering; file\-based; no server process |
| Clean format | Markdown | Raw JSON, raw HTML | LLM token efficiency; prose context for attribution reasoning; measurable via noise\_reduction\_ratio |
| Rate limiting | asyncio\.Semaphore | sleep\(\) timers, Celery | Maximum safe concurrency per provider; sleep wastes wall\-clock time; Celery adds broker dependency |
| Task scheduler | APScheduler | Celery \+ Redis | Celery requires an external broker with no benefit at this ingestion frequency |
| Provider cost | Free\-tier first | FMP paid \($228/yr\) | Attribution is retrospective; EOD data is sufficient; zero\-cost reproducibility for all users |
| Evaluation | RAGAS \+ golden set | Manual review only | Reference\-free CI\-integrable metrics; grounding\_rate is a first\-class gate not an afterthought |
| Dedup strategy | SHA256 hard \+ cosine soft | SHA256 only | Cross\-provider same\-event articles skew RAG confidence without semantic dedup |
| Chunking | 512 tokens \+ 64 overlap \+ semantic for SEC | Fixed 256, no overlap | 512 tokens balances context richness with retrieval precision; overlap prevents evidence split |
# __9\. Repository Structure__

| Path | Purpose | Status |
| --- | --- | --- |
| packages/data\-core/ | Ingestion, cleaning, SQLite persistence | V1\.5 — implemented |
| packages/agents/ | LangGraph workflow — all 6 nodes | To build \(Phase 4\) |
| packages/eval/ | Golden set scorer, RAGAS integration, CI runner | To build first \(Phase 1\) |
| docs/ADR/ | Architecture Decision Records — one \.md per major decision | To create alongside each decision |
| docs/API\_Documentation/ | Provider cheat sheets and PROVIDER\_INGESTION\_BRIEF\.md | Exists |
| golden\_set/ | Hand\-curated fixture JSON files — committed to git | To create \(Phase 0\) |
| data/assets\.db | SQLite asset store — gitignored | Runtime artifact |
| data/index\.lancedb/ | LanceDB RAG index — gitignored; rebuildable from assets\.db | Runtime artifact |
| data/snapshots/ | Compressed snapshots — gitignored; released on GitHub | Release artifact |
| scripts/backfill\.py | Historical data fetch CLI | To build \(Phase 5\) |
| scripts/build\_index\.py | LanceDB index builder consuming assets\.db | To build \(Phase 3\) |
| scripts/smoke\_test\_fetch\.py | Live integration smoke test | Exists |
| \.github/workflows/eval\.yml | CI: run eval suite on every PR against golden set | To create \(Phase 1\) |
__Git Policy__

Committed: code, schemas, ADRs, golden set fixtures, CI configuration\. Never committed: \*\.db files, \*\.lancedb directories, data/ contents, debug\_output/ artifacts, \.env files\. Data snapshots are GitHub Release assets, not committed files\. The repo should be cloneable to a fresh machine with no data artifacts — data comes from catalyst data download\.

# __10\. Open Questions and Future Work__

## __10\.1 Resolved__

- Provider strategy: free\-tier first with bring\-your\-own\-key abstraction
- Agent framework: LangGraph sequential state machine
- Node naming: Query Parser, Retriever, Grader, Evidence Validator, Attribution Synthesizer, Claim Grounder
- Storage: SQLite \(assets\) \+ LanceDB \(RAG index\)
- Development order: golden set first, eval second, data slice third, agents fourth
- Dedup: SHA256 hard \+ cosine soft at index time
- Chunking: 512 tokens \+ 64 overlap; semantic boundaries for SEC filings

## __10\.2 Still Open__

- UI framework: React \+ FastAPI vs Svelte — deferred until agent layer is stable and CI gates are green
- Embedding model: text\-embedding\-3\-small vs bge\-m3 — evaluate on golden set context recall before committing
- Reranker model: cross\-encoder/ms\-marco vs bge\-reranker\-v2 — evaluate latency vs quality tradeoff on golden set
- Polygon connector: documented in API\_Documentation but connector not yet implemented — needed for news and OHLCV
- Snapshot cadence: how frequently to publish updated snapshots to GitHub Releases
- Event display UI: PokieTicker\-style news dots on candlestick chart — scope and timeline TBD after attribution layer is working

## __10\.3 Explicitly Out of Scope for V1__

- Real\-time or intraday streaming data
- Options, futures, or cryptocurrency markets
- Non\-US equity markets
- Multi\-user or multi\-tenant deployment
- LLM fine\-tuning on financial corpora
- Social media sentiment \(Reddit, X/Twitter\)

# __11\. Glossary__

__Term__

__Definition__

attribution

The identification of specific events or conditions that caused a measurable price movement on a given trading day

grounding\_rate

grounded\_claims / total\_claims per attribution run; primary hallucination control metric; CI gate >= 0\.85

noise\_reduction\_ratio

len\(clean\_tokens\) / len\(raw\_tokens\); measures cleaning pipeline effectiveness per asset; target 0\.25–0\.65

golden set

Hand\-curated fixtures of known market events with verified causes; the ground truth for all eval metrics

canonical asset

When multiple providers cover the same event, the canonical asset is the one indexed for RAG; duplicates point to it via canonical\_id

ET\-alignment

Normalization of all date fields to US/Eastern Time to prevent misattribution caused by timezone differences

graded chunk

A retrieved chunk annotated with relevant, stale, and noise flags by the Grader node

GraphState

The shared TypedDict passed between all LangGraph nodes; the API contract for the entire agent workflow

evidence\_sufficient

Boolean flag set by the Evidence Validator; True when >= 3 relevant chunks from >= 2 distinct source\_types are available

Technical Decision Chain

The documented justification for each architectural choice: what business pain it solves and what the measurable outcome is

soft dedup

Semantic deduplication using cosine similarity at RAG index time to prevent cross\-provider same\-event inflation

partial report

The output when evidence is insufficient; includes data\_quality\_warning rather than fabricated causes

*End of Document*

Catalyst v0\.1 Whitepaper  |  Yiannis Chen  |  March 2026


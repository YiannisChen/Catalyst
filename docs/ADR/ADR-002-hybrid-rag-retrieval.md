> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# ADR-002: Hybrid RAG Retrieval (BM25 + Vector + Reranking)

**Status:** Accepted
**Date:** 2026-04-02
**Decision:** Use hybrid BM25 + vector retrieval with RRF fusion, followed by cross-encoder reranking.

## Context

The attribution agent needs to retrieve the most relevant evidence chunks from the LanceDB Gold layer for a given (ticker, date) query. Retrieval quality directly determines attribution quality — if the right evidence isn't retrieved, the agent cannot produce correct attribution regardless of how good the LLM is.

## Options Considered

### Option A: Vector-Only Retrieval
- Embed query with bge-m3 → cosine similarity → top-K
- Catches semantic similarity (paraphrases, related concepts)
- **Misses:** exact keyword matches. "AAPL" the ticker symbol and "apple" the fruit have similar embeddings. A query about "AAPL earnings" might miss chunks containing the exact ticker symbol if semantically dissimilar articles rank higher.

### Option B: BM25-Only Retrieval
- Full-text keyword search → TF-IDF ranking → top-K
- Catches exact term matches (ticker symbols, dollar amounts, dates)
- **Misses:** paraphrases. "China export restrictions" won't find chunks about "trade war tariffs" even though they're the same event.

### Option C: Hybrid BM25 + Vector with RRF Fusion + Reranking
- Run both A and B in parallel
- Merge with Reciprocal Rank Fusion (avoids score scale mismatch)
- Rerank merged results with cross-encoder (bge-reranker-v2)
- Most accurate, but adds ~200ms latency from reranker

## Decision

**Option C: Hybrid + Reranking.** Business justification:

1. **Financial text has both keyword and semantic signals:** Ticker symbols, dollar amounts, and dates are exact keywords where BM25 excels. But cause-and-effect reasoning ("tariff → supply chain disruption → revenue impact") requires semantic understanding. Neither alone is sufficient.

2. **RRF fusion is simple and proven:** `RRF_score = sum(1/(k + rank))` with `k=60`. Documents that rank well in both keyword AND semantic lists get the highest combined score. No hyperparameter tuning needed beyond `k`.

3. **Cross-encoder reranking is the highest-impact improvement:** Bi-encoder (vector search) embeds query and document independently — fast but misses interaction effects. Cross-encoder sees (query, document) as a pair — understands relevance in context. Research consistently shows +15-25% precision improvement.

4. **Latency budget:** Retrieval is not user-facing latency — it's an internal pipeline step. 200ms for reranking is negligible against the 2-3 second LLM calls that follow.

## Algorithm Detail

### Step 1: Parallel Retrieval
```
BM25 path:  full-text search on content_md → top-20
Vector path: embed(query) → cosine search → top-20
```

### Step 2: Reciprocal Rank Fusion
```
For each document d across all result lists:
  RRF(d) = sum over lists L of: 1 / (k + rank_of_d_in_L)
Sort by RRF score descending → top-20
```

### Step 3: Cross-Encoder Reranking
```
Model: BAAI/bge-reranker-v2-m3 (~570MB)
Input: [(query, doc_text) for each of 20 docs]
Output: relevance score per pair
Keep top-8 by cross-encoder score
```

## Consequences

- bge-reranker-v2 requires ~570MB model download on first use
- ~200ms added latency per query for reranking step
- LanceDB must support both vector search and full-text search (it does natively)
- Experiment E3 (hybrid vs vector-only) and E4 (reranker ablation) will measure the actual impact

## References

- Confirmed in project beginning report: BM25 + vector + reranking
- LanceDB native hybrid search: vector + FTS in single query
- opengpts `retrieval.py`: LangGraph RAG StateGraph pattern
- gpt-researcher `compression.py`: contextual compression after retrieval

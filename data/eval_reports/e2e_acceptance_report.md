# E2E Acceptance Report

**Date:** 2026-04-06 20:43:55
**Ticker:** NVDA  |  **Trade date:** 2026-04-03  |  **Model:** gpt-4o

## 1. Data Layer (Silver)
- Loaded **3** non-duplicate Silver chunks for NVDA
  - `255ddc98bf4e...` fmp_fundamentals     2026-04-03 (5233 chars)
  - `8f5b529072fa...` polygon_news         2026-04-03 (5935 chars)
  - `9e2d6118e362...` polygon_ohlcv        2026-04-03 (164 chars)

## 2. Agent Graph (Miner → Critic → Judge)
- LLM: **DeterministicLLM** (mock — no API key: OPENAI_API_KEY not set)
  *To run with real LLM: export OPENAI_API_KEY=sk-...*
- Running MCJ pipeline...
- Pipeline completed in **3 ms**

### Retrieved Chunks (Miner output)
- Retrieved: 3 chunks  |  Reranked: 3 chunks
  - `255ddc98bf4e...` fmp_fundamentals (rrf=0.0164)
  - `8f5b529072fa...` polygon_news (rrf=0.0161)
  - `9e2d6118e362...` polygon_ohlcv (rrf=0.0159)

### Critic Output
- Graded evidence: **2** chunks passed relevance > 0.5
  - `255ddc98bf4e...` relevance=0.70 cat=sector
  - `8f5b529072fa...` relevance=0.80 cat=earnings
- Critic reasoning: News and fundamentals provide direct evidence; OHLCV is contextual only.

### Judge Output (AttributionResult)
- **3 causes** identified:
  1. [sector] Broader semiconductor sector pressure amid AI spending uncertainty and export co
     confidence=0.45 | direction=negative | evidence=['255ddc98bf4ef872f64f6bac81c0a263364a63ef7b05336707498436b9bb1708']
  2. [earnings] Mixed earnings signals with strong revenue but margin compression
     confidence=0.35 | direction=negative | evidence=['8f5b529072fa4995877bcc9a23468cd861dac10efeb5227305d356d7d117a049']
  3. [technical] Market-wide risk rotation out of mega-cap tech
     confidence=0.15 | direction=negative | evidence=[]

**Summary:**
> NVDA experienced downward pressure on 2026-04-03 driven primarily by semiconductor sector concerns [255ddc98bf4e] and mixed earnings signals [8f5b529072fa]. A broader tech rotation contributed as a secondary factor.

**Grounding rate:** 0.6666666666666666

### Cost
- critic: 3200 in + 450 out = $0.012500
- judge: 3500 in + 520 out = $0.013950
- **Total:** $0.026450 | 7670 tokens

## 3. Evaluation Scoring
- Golden event: `g001` NVDA 2025-01-27 (-16.97%)
- Note: trade dates may differ (golden=2025-01-27, predicted=2026-04-03)

| Metric | Score |
|--------|-------|
| attribution_f1 | 0.3333 |
| category_accuracy | 0.6667 |
| grounding_rate | 0.0000 |
| temporal_precision | 0.0000 |
| confidence_calibration | 0.5250 |

## 4. Residual Risks & Failure Points

| Risk | Severity | Notes |
|------|----------|-------|
| LanceDB bypassed (in-memory search) | High | Real hybrid BM25+vector not tested |
| No reranker (bge-reranker-v2) | Medium | Chunks returned in insertion order, not relevance |
| Golden set date mismatch | Medium | v1.jsonl events are Jan 2025, data is Apr 2026 |
| Cost tracking uses gpt-4o pricing | Low | MODEL_PRICING table has gpt-4o entry |
| Deterministic LLM (no real inference) | High | Set OPENAI_API_KEY to test real LLM |

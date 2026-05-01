> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# ADR-003: Miner-Critic-Judge Agent Workflow

**Status:** Accepted
**Date:** 2026-04-02
**Decision:** Use a 3-stage sequential pipeline (Miner → Critic → Judge) with Critic as an optional node for A/B experimentation.

## Context

Catalyst needs an agent workflow that takes a (ticker, date) pair and produces a causal attribution report explaining why the stock moved. The workflow must:
- Retrieve relevant evidence from multiple sources
- Filter and grade that evidence for relevance
- Synthesize a final attribution with confidence scores and source citations
- Be observable via LangSmith (every decision traceable)
- Have predictable, bounded token cost

## Options Considered

### Option A: Single Agent (Retrieve → Synthesize)
- One LLM call: receives all retrieved chunks, produces attribution
- Simplest, cheapest (~$0.020/query)
- **Problem:** The LLM tends to latch onto dramatic headlines regardless of relevance, confuse events from different dates, and mix up which company the news is about. There's no quality gate on the evidence before synthesis.

### Option B: Miner-Critic-Judge (3-stage pipeline)
- Miner: hybrid retrieval + reranking (no LLM)
- Critic: 1 LLM call to grade each chunk (relevance, category, temporal match)
- Judge: 1 LLM call to synthesize from graded evidence
- Moderate cost (~$0.030/query, 1.5x baseline)
- **Advantage:** Critic's ONLY job is evaluating evidence quality — it has no narrative pressure, so it doesn't cherry-pick evidence.

### Option C: Forum/Debate Style (multi-round)
- Multiple agents debate causes back-and-forth until convergence
- **Rejected:** Token cost is unbounded. In testing, response quality did not measurably improve over simpler pipelines. Convergence is unpredictable.

### Option D: Parallel Specialist Agents (News + Macro + Filing miners)
- Separate LLM call per source type, then merge
- More expensive (3+ LLM calls for mining alone)
- **Rejected for V1:** Adds complexity without proven benefit. Can be revisited post-thesis if E7 (Source Coverage experiment) shows value.

## Decision

**Option B: Miner-Critic-Judge**, with Critic as an optional node that can be toggled off for the baseline experiment.

```python
graph = build_attribution_graph(use_critic=True)    # MCJ (default)
graph = build_attribution_graph(use_critic=False)   # Baseline (experiment E2)
```

### Why MCJ over Single Agent

The Critic solves three specific failure modes in financial attribution:

1. **Irrelevant evidence pollution:** Without Critic, the Judge receives all 8 reranked chunks and must simultaneously evaluate relevance AND synthesize. It often fails to reject irrelevant chunks, leading to hallucinated attribution ("AAPL dropped due to Microsoft earnings miss" — because a Microsoft chunk was in the retrieval results).

2. **Temporal confusion:** A news article from January 12 gets retrieved for a January 15 query because it mentions the same ticker. The Critic explicitly checks `temporal_match` — does the timing of this event align with the price move date?

3. **Category misclassification:** Without structured category tagging, the Judge conflates macro events with company-specific events. The Critic assigns `CauseCategory` per chunk, giving the Judge pre-structured evidence.

### Why NOT Forum/Debate

Tested in conversation: forum-style multi-agent debate showed uncontrollable token usage (3-8x single agent) with no measurable improvement in attribution accuracy. The fundamental issue is that debate adds noise — agents generate plausible-sounding but unsupported alternative explanations to "win" the debate.

## Token Budget

| Node | Tokens | Cost (Claude Sonnet) |
|---|---|---|
| Miner | 0 (no LLM) | $0 |
| Critic | ~3,800 | ~$0.014 |
| Judge | ~3,700 | ~$0.016 |
| **MCJ total** | **~7,500** | **~$0.030** |
| **Baseline** | **~5,000** | **~$0.020** |

## Consequences

- 2 LLM calls per attribution (vs 1 for baseline) — 50% more cost
- Experiment E2 will measure whether the quality improvement justifies this cost
- The Critic's category tags directly feed the eval metric "Category Accuracy"
- LangSmith traces show Miner → Critic → Judge as separate nodes — easy to diagnose failures

## References

- gpt-researcher: `StateGraph(ResearchState)` with browser → planner → researcher → writer nodes (similar sequential pipeline)
- PokieTicker: Layer 0 (cheap filter) → Layer 1 (batch LLM) → Layer 2 (deep analysis) — same cost-optimization pattern
- opengpts: `invoke_retrieval → retrieve → response` RAG graph — simpler but same structure

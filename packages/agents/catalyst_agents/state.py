"""AttributionState — canonical LangGraph state schema for the Miner-Critic-Judge pipeline.

Spec reference: Section 4.2 — Agent State Contract.
"""
from __future__ import annotations

from typing import TypedDict


class AttributionState(TypedDict):
    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------
    ticker: str
    trade_date: str
    query: str | None          # None for candle-click, string for NLP entry
    price_move_pct: float | None

    # ------------------------------------------------------------------
    # Miner output
    # ------------------------------------------------------------------
    retrieved_chunks: list[dict]   # hybrid retrieval results (top-20)
    reranked_chunks: list[dict]    # after reranker (top-8)

    # ------------------------------------------------------------------
    # Critic output
    # ------------------------------------------------------------------
    graded_evidence: list[dict]    # scored + category-tagged chunks
    critic_reasoning: str          # chain-of-thought logged to LangSmith

    # ------------------------------------------------------------------
    # Judge output
    # ------------------------------------------------------------------
    causes: list[dict]             # [{text, category, confidence, evidence_ids, direction}]
    summary_md: str                # final Markdown report with inline citations
    grounding_rate: float | None

    # ------------------------------------------------------------------
    # Cost tracking (per-node granularity for experiments)
    # ------------------------------------------------------------------
    cost_breakdown: list[dict]     # [{node, input_tokens, output_tokens, model_id, cost_usd}]
    total_cost_usd: float
    total_tokens: int
    model_id: str

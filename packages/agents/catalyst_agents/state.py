"""AttributionState — canonical LangGraph state schema for the Miner-Critic-Judge pipeline.

Spec reference: Section 4.2 — Agent State Contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, TypedDict


class Phase(str, Enum):
    PARSER = "parser"
    RETRIEVAL_L1 = "retrieval_l1"
    RETRIEVAL_L2 = "retrieval_l2"
    MINER = "miner"
    CRITIC = "critic"
    ROUTER = "router"
    JUDGE = "judge"
    VALIDATOR = "validator"
    FINALIZER = "finalizer"


class OutputStatus(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    SYSTEM_ERROR = "SYSTEM_ERROR"


@dataclass(frozen=True)
class CriticDecision:
    sufficiency: Literal["sufficient", "partial", "insufficient"]
    next_action: Literal["proceed", "expand_macro", "expand_related", "refuse"]
    magnitude_coverage: float
    reasoning: str


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
    all_graded_chunks: list[dict]  # pre-filter graded chunks (for observability)
    critic_reasoning: str          # chain-of-thought logged to LangSmith
    critic_decision: CriticDecision | None

    # ------------------------------------------------------------------
    # Error classification (system_error vs insufficient_evidence)
    # ------------------------------------------------------------------
    error_type: str | None             # "system_error" | None

    # ------------------------------------------------------------------
    # Judge output
    # ------------------------------------------------------------------
    causes: list[dict]             # [{text, category, confidence, evidence_ids, direction}]
    summary_md: str                # final Markdown report with inline citations
    grounding_rate: float | None
    output_status: OutputStatus | None
    validation_error: str | None
    validator_attempts: int
    phase: Phase | None
    router_edge: str | None
    router_reason: str | None
    expansions_used: int
    max_expansions: int
    current_layer: str | None
    retrieval_metadata: object | None

    # ------------------------------------------------------------------
    # Cost tracking (per-node granularity for experiments)
    # ------------------------------------------------------------------
    cost_breakdown: list[dict]     # [{node, input_tokens, output_tokens, model_id, cost_usd}]
    total_cost_usd: float
    total_tokens: int
    model_id: str

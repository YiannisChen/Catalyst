"""AttributionState — canonical LangGraph state schema for the Miner-Critic-Judge pipeline.

Spec reference: Section 4.2 — Agent State Contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, TypedDict

from catalyst_agents.attribution.output_status import OutputStatus


class Phase(str, Enum):
    PARSER = "parser"
    RETRIEVAL_L1 = "retrieval_l1"
    RETRIEVAL_L2 = "retrieval_l2"
    CONTEXT_BUILDER = "context_builder"
    MINER = "miner"
    CRITIC = "critic"
    ROUTER = "router"
    JUDGE = "judge"
    VALIDATOR = "validator"
    FINALIZER = "finalizer"


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
    query_ticker_raw: str | None
    ticker_consistent: bool | None
    market_session_valid: bool | None
    magnitude_plausible: bool | None

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
    context_artifact: dict | None
    context_artifact_sha256: str | None
    cutoff: str | None
    context_cutoff: str | None
    retrieval_cutoff: str | None
    validator_cutoff: str | None
    corpus_manifest_id: str | None
    index_manifest_id: str | None
    hypothesis_drafts: list[dict]
    hypotheses: list[dict]
    ranked_hypotheses: list[dict]
    source_support_flags: dict
    cost_status: str
    retry_count: int
    repair_count: int

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Arm B/C evidence artifacts (pre-Critic from miner, post-Critic from judge)
    # ------------------------------------------------------------------
    arm_b_evidence: dict | None
    # ^ pre-Critic top-8 reranked chunks, {per_asset: {asset_id: content_md}, sha256}
    #   Persisted at miner node — Arm B consumes this (NOT judge_evidence).
    #   Exists even when Critic later refuses.

    # Arm C runtime instrumentation (judge_evidence — SECONDARY fidelity check)
    # ------------------------------------------------------------------
    judge_evidence: dict[str, str] | None
    # ^ per asset_id -> content_md, Critic fields stripped (§0.5)
    judge_evidence_sha256: str | None
    # ^ SHA-256 of the assembled evidence block the Judge received

    # ------------------------------------------------------------------
    # Cost tracking (per-node granularity for experiments)
    # ------------------------------------------------------------------
    cost_breakdown: list[dict]     # [{node, input_tokens, output_tokens, model_id, cost_usd}]
    total_cost_usd: float
    total_tokens: int
    model_id: str


# ---------------------------------------------------------------------------
# V1.1 foundation graph state (M4-8; Final TSD §3 "Graph state")
# ---------------------------------------------------------------------------
# Thin orchestration state: run/stage, immutable artifact refs/hashes, round,
# attempt counters, deadline/cancel, and terminal error only. No copied
# evidence lists, prose, causes, hidden reasoning, or mutable semantic state
# lives in the V1.1 fields. The sealed MCJ fields above remain baseline-only.


class FoundationStage(str, Enum):
    RUN_ADMISSION = "run_admission"
    QUERY_VALIDATION = "query_validation"
    OBSERVATION_BUILD = "observation_build"
    RESEARCH_POLICY = "research_policy"
    RESEARCH_EXECUTION = "research_execution"
    EVIDENCE_STATE = "evidence_state"
    COVERAGE_SUMMARY = "coverage_summary"
    CONTEXT_PACK_BUILD = "context_pack_build"
    ANALYST_BOUNDARY = "analyst_boundary"
    CLAIM_PLAN_BUILD = "claim_plan_build"
    CLAIM_VALIDATION = "claim_validation"
    STREAMING_ANSWER_WRITER = "streaming_answer_writer"
    POST_STREAM_ASSURANCE = "post_stream_assurance"
    FINALIZER = "finalizer"
    TERMINAL = "terminal"


class FoundationGraphState(TypedDict, total=False):
    run_id: str
    stage: FoundationStage | None
    round: int
    attempt: int
    deadline_epoch_ms: int | None
    cancel_requested: bool
    terminal_error: str | None

    # Artifact references and hashes only (never duplicated payloads).
    observation_ref: str | None
    observation_hash: str | None
    research_tasks_ref: str | None
    research_results_ref: str | None
    evidence_state_ref: str | None
    evidence_state_hash: str | None
    coverage_summary_ref: str | None
    coverage_summary_hash: str | None
    context_pack_ref: str | None
    context_pack_hash: str | None
    rendered_messages_ref: str | None
    rendered_messages_hash: str | None
    prompt_template_version: str | None
    prompt_template_sha256: str | None
    policy_version: str | None

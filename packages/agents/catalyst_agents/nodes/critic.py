"""Critic node — grades evidence chunks for relevance to the price move.

One LLM call. Filters chunks with relevance > 0.5.

Spec reference: Section 4.3 — Critic Node.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from catalyst_agents.state import AttributionState, CriticDecision, OutputStatus, Phase
from catalyst_agents.cost_tracker import track_cost
from catalyst_agents.backoff import invoke_with_retries, MAX_RETRIES

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RELEVANCE_THRESHOLD = 0.5
K_SUFFICIENT = 2
K_PARTIAL = 2
M_THRESHOLD = 0.6
_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "critic.md"
_VALID_CATEGORIES = {
    "earnings", "macro", "geopolitical", "sector", "technical", "regulatory", "other",
}


class GradedChunk(BaseModel):
    chunk_id: str = Field(min_length=1)
    relevance: float = Field(ge=0.0, le=1.0)
    category: Literal["earnings", "macro", "geopolitical", "sector", "technical", "regulatory", "other"]
    temporal_match: bool
    reasoning: str = Field(min_length=1)


class CriticResponse(BaseModel):
    graded_chunks: list[GradedChunk]
    reasoning: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _load_prompt() -> str:
    """Read the critic prompt template from disk."""
    return _PROMPT_PATH.read_text()


def _format_chunks(chunks: list[dict]) -> str:
    """Format reranked chunks as numbered Markdown sections for the prompt.

    Args:
        chunks: List of chunk dicts with at minimum ``asset_id`` and ``content_md``.

    Returns:
        Formatted string with each chunk separated by a horizontal rule,
        or an empty string when the input list is empty.
    """
    if not chunks:
        return ""

    parts = []
    for i, chunk in enumerate(chunks, 1):
        parts.append(
            f"### Chunk {i} (ID: {chunk['asset_id']})\n"
            f"Source: {chunk.get('source_type', 'unknown')} | "
            f"Date: {chunk.get('reference_date', 'unknown')}\n\n"
            f"{chunk.get('content_md', '')}"
        )
    return "\n\n---\n\n".join(parts)


def _parse_critic_response(text: str) -> dict:
    """Parse LLM JSON response, stripping optional Markdown fences.

    Args:
        text: Raw LLM output, optionally wrapped in ```json ... ``` or ``` ... ```.

    Returns:
        Schema-validated dict from the JSON payload.

    Raises:
        json.JSONDecodeError: If the content after fence stripping is not valid JSON.
        pydantic.ValidationError: If parsed JSON does not match CriticResponse schema.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Drop the opening fence line and any trailing closing fence
        inner_lines = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        cleaned = "\n".join(inner_lines)
    parsed = json.loads(cleaned)
    if isinstance(parsed, list):
        if (
            len(parsed) == 1
            and isinstance(parsed[0], dict)
            and "graded_chunks" in parsed[0]
        ):
            parsed = parsed[0]
        elif parsed and all(
            isinstance(item, dict) and {"chunk_id", "relevance"}.issubset(item.keys())
            for item in parsed
        ):
            # Some models return the graded_chunks array directly.
            parsed = {
                "graded_chunks": parsed,
                "reasoning": "Auto-wrapped from graded_chunks list payload.",
            }
        else:
            raise ValueError("critic response list payload is invalid")
    if isinstance(parsed, dict):
        for chunk in parsed.get("graded_chunks", []) or []:
            cat = str(chunk.get("category", "")).strip().lower()
            if cat not in _VALID_CATEGORIES:
                chunk["category"] = "other"
    validated = CriticResponse.model_validate(parsed)
    return validated.model_dump()


def _compute_magnitude_coverage(filtered: list[dict]) -> float:
    if not filtered:
        return 0.0

    avg_relevance = sum(chunk.get("relevance", 0.0) for chunk in filtered) / len(filtered)
    # K_PARTIAL is retained for magnitude scaling only.
    count_factor = min(1.0, len(filtered) / K_PARTIAL)
    return min(1.0, avg_relevance * count_factor)


def _build_critic_decision(filtered: list[dict], reasoning: str) -> CriticDecision:
    evidence_count = len(filtered)
    magnitude_coverage = _compute_magnitude_coverage(filtered)

    if evidence_count >= K_SUFFICIENT and magnitude_coverage >= M_THRESHOLD:
        sufficiency = "sufficient"
        next_action = "proceed"
    elif evidence_count == 0:
        sufficiency = "insufficient"
        next_action = "refuse"
    else:
        sufficiency = "partial"
        next_action = "proceed"

    return CriticDecision(
        sufficiency=sufficiency,
        next_action=next_action,
        magnitude_coverage=magnitude_coverage,
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# Public nodes
# ---------------------------------------------------------------------------

def critic(state: AttributionState, *, llm: Any = None) -> dict:
    """Critic node for the LangGraph attribution pipeline.

    Grades each reranked chunk for relevance to the observed price move
    using a single LLM call, then filters results below the relevance
    threshold. No narrative is produced — that is the Judge's responsibility.

    Args:
        state: Current attribution state. Must have ``reranked_chunks`` populated.
        llm:   LLM client with a ``.invoke(prompt: str)`` interface that returns
               a response object with ``.content`` (str) and ``.usage`` attributes.

    Returns:
        Partial state dict containing:
          - ``graded_evidence``: chunks that passed the relevance threshold.
          - ``critic_reasoning``: overall assessment from the LLM.
    """
    chunks = state.get("reranked_chunks", [])
    if not chunks:
        return {
            "graded_evidence": [],
            "all_graded_chunks": [],
            "critic_reasoning": "No chunks to grade.",
            "critic_decision": CriticDecision(
                sufficiency="insufficient",
                next_action="refuse",
                magnitude_coverage=0.0,
                reasoning="No chunks to grade.",
            ),
            "phase": Phase.CRITIC,
        }

    prompt_template = _load_prompt()
    prompt = prompt_template.format(
        ticker=state["ticker"],
        price_move_pct=state.get("price_move_pct", "unknown"),
        trade_date=state["trade_date"],
        chunks_formatted=_format_chunks(chunks),
    )

    try:
        response, parsed = invoke_with_retries(
            llm, prompt, parse_fn=_parse_critic_response, node_name="Critic",
        )
        track_cost(state, "critic", response)
        graded = parsed.get("graded_chunks", [])

        # Strict greater-than filter per spec Section 4.3
        filtered = [g for g in graded if g.get("relevance", 0) > RELEVANCE_THRESHOLD]
        decision = _build_critic_decision(filtered, parsed.get("reasoning", ""))

        return {
            "graded_evidence": filtered,
            "all_graded_chunks": graded,
            "critic_reasoning": parsed.get("reasoning", ""),
            "critic_decision": decision,
            "cost_breakdown": state.get("cost_breakdown", []),
            "total_cost_usd": state.get("total_cost_usd", 0.0),
            "total_tokens": state.get("total_tokens", 0),
            "phase": Phase.CRITIC,
        }
    except RuntimeError as exc:
        return {
            "graded_evidence": [],
            "all_graded_chunks": [],
            "critic_reasoning": str(exc),
            "critic_decision": CriticDecision(
                sufficiency="insufficient",
                next_action="refuse",
                magnitude_coverage=0.0,
                reasoning=str(exc),
            ),
            "error_type": "system_error",
            "cost_breakdown": state.get("cost_breakdown", []),
            "total_cost_usd": state.get("total_cost_usd", 0.0),
            "total_tokens": state.get("total_tokens", 0),
            "phase": Phase.CRITIC,
        }


def system_error_handler(state: AttributionState) -> dict:
    """Fallback node when the Critic encountered a system-level failure.

    Unlike insufficient_handler (which indicates the data simply lacked
    relevant evidence), this node signals that the pipeline could not
    complete due to an infrastructure issue (LLM timeout, bad JSON, etc.).

    Args:
        state: Current attribution state.

    Returns:
        Partial state dict with error-specific cause and summary.
    """
    ticker = state["ticker"]
    trade_date = state["trade_date"]
    reasoning = state.get("critic_reasoning", "Unknown error")

    return {
        "causes": [],
        "summary_md": (
            f"**System error** during attribution for {ticker} on {trade_date}. "
            f"The Critic node failed to produce a valid response: {reasoning}. "
            f"This is an infrastructure issue, not an evidence gap."
        ),
        "grounding_rate": None,
        "output_status": OutputStatus.SYSTEM_ERROR,
    }


def insufficient_handler(state: AttributionState) -> dict:
    """Fallback node when the Critic filters ALL evidence below the threshold.

    No LLM call is made. Returns a canned AttributionResult signalling that
    the data coverage could not explain the observed price move.

    Args:
        state: Current attribution state (must have ``ticker`` and ``trade_date``).

    Returns:
        Partial state dict containing:
          - ``causes``: single-item list with category "unknown".
          - ``summary_md``: explanatory Markdown message.
          - ``grounding_rate``: None (no evidence was grounded).
    """
    ticker = state["ticker"]
    trade_date = state["trade_date"]

    return {
        "causes": [
            {
                "text": "Insufficient evidence in available data sources",
                "category": "unknown",
                "confidence": 1.0,
                "evidence_ids": [],
                "direction": "unknown",
            }
        ],
        "summary_md": (
            f"No evidence meeting the relevance threshold (>{RELEVANCE_THRESHOLD}) was found "
            f"for {ticker} on {trade_date}. This may indicate the price move was driven by "
            f"factors outside our data coverage (private information, market microstructure, "
            f"or sources we do not ingest)."
        ),
        "grounding_rate": None,
        "output_status": OutputStatus.INSUFFICIENT,
    }

"""Critic node — grades evidence chunks for relevance to the price move.

One LLM call. Filters chunks with relevance > 0.5.

Spec reference: Section 4.3 — Critic Node.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from catalyst_agents.state import AttributionState
from catalyst_agents.cost_tracker import track_cost

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RELEVANCE_THRESHOLD = 0.5
_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "critic.md"


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
        Parsed dict from the JSON payload.

    Raises:
        json.JSONDecodeError: If the content after fence stripping is not valid JSON.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Drop the opening fence line and any trailing closing fence
        inner_lines = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        cleaned = "\n".join(inner_lines)
    return json.loads(cleaned)


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
            "critic_reasoning": "No chunks to grade.",
        }

    prompt_template = _load_prompt()
    prompt = prompt_template.format(
        ticker=state["ticker"],
        price_move_pct=state.get("price_move_pct", "unknown"),
        trade_date=state["trade_date"],
        chunks_formatted=_format_chunks(chunks),
    )

    response = llm.invoke(prompt)

    # Mutate state for cost accounting before any early return
    track_cost(state, "critic", response)

    parsed = _parse_critic_response(response.content)
    graded = parsed.get("graded_chunks", [])

    # Strict greater-than filter per spec Section 4.3
    filtered = [g for g in graded if g.get("relevance", 0) > RELEVANCE_THRESHOLD]

    return {
        "graded_evidence": filtered,
        "critic_reasoning": parsed.get("reasoning", ""),
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
    }

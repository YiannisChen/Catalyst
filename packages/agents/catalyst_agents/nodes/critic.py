"""Critic node — grades evidence chunks for relevance to the price move.

One LLM call. Filters chunks with relevance > 0.5.

Spec reference: Section 4.3 — Critic Node.
"""
from __future__ import annotations

import json
import re
from functools import partial
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from catalyst_agents.state import AttributionState, CriticDecision, OutputStatus, Phase
from catalyst_agents.cost_tracker import track_cost
from catalyst_agents.backoff import invoke_with_retries, MAX_RETRIES

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RELEVANCE_THRESHOLD = 0.25
K_SUFFICIENT = 1
K_PARTIAL = 1
M_THRESHOLD = 0.3
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
    event_specificity: float | None = Field(default=None, ge=0.0, le=1.0)
    temporal_alignment: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_granularity: float | None = Field(default=None, ge=0.0, le=1.0)
    conflict_signal: float | None = Field(default=None, ge=0.0, le=1.0)


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

    _MAX_CONTENT_CHARS = 600  # Truncate content to keep prompt compact
    parts = []
    for i, chunk in enumerate(chunks, 1):
        content = (chunk.get('content_md', '') or '')[:_MAX_CONTENT_CHARS]
        parts.append(
            f"### Chunk {i} (ID: {chunk['asset_id']})\n"
            f"Source: {chunk.get('source_type', 'unknown')} | "
            f"Date: {chunk.get('reference_date', 'unknown')}\n\n"
            f"{content}"
        )
    return "\n\n---\n\n".join(parts)


def _build_critic_prompt(ticker: str, price_move_pct: float | str, trade_date: str, chunks: list[dict]) -> str:
    prompt_template = _load_prompt()
    return prompt_template.format(
        ticker=ticker,
        price_move_pct=price_move_pct,
        trade_date=trade_date,
        chunks_formatted=_format_chunks(chunks),
    )


def _retry_prompt_fn(base_prompt: str, attempt: int) -> str:
    marker = "## Evidence Chunks\n"
    sep = "\n## Instructions\n"
    if marker not in base_prompt or sep not in base_prompt:
        return base_prompt
    before_marker, after_marker = base_prompt.split(marker, 1)
    chunk_block, tail = after_marker.split(sep, 1)
    chunks = [c for c in chunk_block.split("\n\n---\n\n") if c.strip()]
    k = 4 if attempt == 1 else 2
    shrunk = "\n\n---\n\n".join(chunks[:k])
    return f"{before_marker}{marker}{shrunk}{sep}{tail}"


def _retry_prompt_fn_factory(state: AttributionState, chunks: list[dict]):
    base = _build_critic_prompt(
        state["ticker"],
        state.get("price_move_pct", "unknown"),
        state["trade_date"],
        chunks,
    )

    def _fn(_: str, attempt: int) -> str:
        return _retry_prompt_fn(base, attempt)

    return _fn


def _strip_md_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        inner_lines = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        cleaned = "\n".join(inner_lines)
    return cleaned


def _coerce_payload(parsed: Any) -> dict[str, Any]:
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
            parsed = {
                "graded_chunks": parsed,
                "reasoning": "Auto-wrapped from graded_chunks list payload.",
            }
        else:
            raise ValueError("critic response list payload is invalid")
    if not isinstance(parsed, dict):
        raise ValueError("critic response payload must be object-like")
    return parsed


def _normalize_chunk_categories(payload: dict[str, Any]) -> dict[str, Any]:
    for chunk in payload.get("graded_chunks", []) or []:
        cat = str(chunk.get("category", "")).strip().lower()
        if cat not in _VALID_CATEGORIES:
            chunk["category"] = "other"
    return payload


def _salvage_graded_chunks_with_regex(cleaned: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for match in re.finditer(r"\{[^{}]*\}", cleaned, flags=re.DOTALL):
        try:
            obj = json.loads(match.group(0))
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if not {"chunk_id", "relevance"}.issubset(obj.keys()):
            continue
        obj = _normalize_chunk_categories({"graded_chunks": [obj]}).get("graded_chunks", [obj])[0]
        try:
            gc = GradedChunk.model_validate(obj)
        except Exception:
            continue
        out.append(gc.model_dump())
    return out


def _parse_critic_response(text: str, *, expected_chunk_count: int = 0) -> dict:
    """Parse Critic response with strict-first and chunk-salvage fallback."""
    cleaned = _strip_md_fence(text)
    parse_mode = "strict"

    try:
        parsed = json.loads(cleaned)
        payload = _coerce_payload(parsed)
        payload = _normalize_chunk_categories(payload)
        validated = CriticResponse.model_validate(payload).model_dump()
    except Exception:
        chunks = _salvage_graded_chunks_with_regex(cleaned)
        if not chunks:
            raise
        parse_mode = "chunk_salvage"
        validated = {
            "graded_chunks": chunks,
            "reasoning": "auto-salvaged from partial critic output",
        }

    actual = len(validated.get("graded_chunks", []))
    validated["_parse_meta"] = {
        "parse_mode": parse_mode,
        "expected_chunk_count": expected_chunk_count,
        "parsed_chunk_count": actual,
        "degraded": bool(expected_chunk_count > 0 and actual < expected_chunk_count),
    }
    return validated


def _compute_magnitude_coverage(filtered: list[dict]) -> float:
    if not filtered:
        return 0.0

    avg_relevance = sum(chunk.get("relevance", 0.0) for chunk in filtered) / len(filtered)
    # K_PARTIAL is retained for magnitude scaling only.
    count_factor = min(1.0, len(filtered) / K_PARTIAL)
    return min(1.0, avg_relevance * count_factor)


def _apply_temporal_penalty_and_filter(graded: list[dict]) -> list[dict]:
    adjusted: list[dict] = []
    for g in graded:
        row = dict(g)
        rel = float(row.get("relevance", 0.0) or 0.0)
        row["original_relevance"] = rel
        if row.get("temporal_match") is False:
            rel = max(0.0, rel * 0.9)
        row["relevance"] = rel
        adjusted.append(row)
    return [g for g in adjusted if g.get("relevance", 0.0) > RELEVANCE_THRESHOLD]


def _build_critic_decision(filtered: list[dict], reasoning: str) -> CriticDecision:
    evidence_count = len(filtered)
    magnitude_coverage = _compute_magnitude_coverage(filtered)

    if evidence_count == 1:
        c0 = filtered[0]
        if float(c0.get("relevance", 0.0) or 0.0) >= 0.8 and bool(c0.get("temporal_match", False)):
            return CriticDecision(
                sufficiency="sufficient",
                next_action="proceed",
                magnitude_coverage=magnitude_coverage,
                reasoning=reasoning,
            )

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

    prompt = _build_critic_prompt(
        state["ticker"],
        state.get("price_move_pct", "unknown"),
        state["trade_date"],
        chunks,
    )

    try:
        response, parsed = invoke_with_retries(
            llm,
            prompt,
            parse_fn=partial(_parse_critic_response, expected_chunk_count=len(chunks)),
            node_name="Critic",
            retry_prompt_fn=_retry_prompt_fn_factory(state, chunks),
        )
        track_cost(state, "critic", response)
        graded = parsed.get("graded_chunks", [])

        filtered = _apply_temporal_penalty_and_filter(graded)
        decision = _build_critic_decision(filtered, parsed.get("reasoning", ""))

        return {
            "graded_evidence": filtered,
            "all_graded_chunks": graded,
            "critic_reasoning": parsed.get("reasoning", ""),
            "critic_decision": decision,
            "critic_raw_llm_response": str(response.content),
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
    reason_map = {
        "ticker_mismatch_guard": "Query ticker does not match resolved ticker.",
        "market_session_guard": "No trading session on requested date for this ticker.",
        "magnitude_guard": "Claimed price move is inconsistent with OHLCV data.",
    }
    guard_reason = reason_map.get(state.get("router_reason"))
    reason_prefix = f"{guard_reason} " if guard_reason else ""

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
            f"{reason_prefix}No evidence meeting the relevance threshold (>{RELEVANCE_THRESHOLD}) was found "
            f"for {ticker} on {trade_date}. This may indicate the price move was driven by "
            f"factors outside our data coverage (private information, market microstructure, "
            f"or sources we do not ingest)."
        ),
        "grounding_rate": None,
        "output_status": OutputStatus.INSUFFICIENT,
    }

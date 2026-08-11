"""Cost tracking utilities for the Miner-Critic-Judge pipeline.

MODEL_PRICING stores per-million-token rates (USD) for all supported models.
track_cost() mutates the pipeline state in-place — it is designed to be called
at the end of each LangGraph node after the LLM response is received.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

log = logging.getLogger(__name__)

# USD per 1 million tokens -------------------------------------------------
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Direct-API models
    "claude-sonnet-4-20250514":  {"input": 3.0,   "output": 15.0},
    "gpt-4o":                     {"input": 2.5,   "output": 10.0},
    "claude-haiku-4-5-20251001": {"input": 0.8,   "output": 4.0},
    "gemini-2.5-flash":           {"input": 0.15,  "output": 0.60},
    # Aihubmix-routed models
    "gemini-2.5-flash-nothink":   {"input": 0.15,  "output": 0.60},
    "claude-opus-4-6":            {"input": 15.0,  "output": 75.0},
    "deepseek-v4-flash":          {"input": 0.20,  "output": 0.60},
    "deepseek-v3":                {"input": 0.27,  "output": 1.10},
    "deepseek-chat":              {"input": 0.27,  "output": 1.10},
}

@dataclass(frozen=True)
class CostEstimate:
    model_id: str
    tokens_prompt: int
    tokens_completion: int
    cost_status: str
    cost_usd: float | None

    def __init__(self, *, model_id: str, tokens_prompt: int, tokens_completion: int) -> None:
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "tokens_prompt", tokens_prompt)
        object.__setattr__(self, "tokens_completion", tokens_completion)
        pricing = MODEL_PRICING.get(model_id)
        if pricing is None:
            object.__setattr__(self, "cost_status", "unknown")
            object.__setattr__(self, "cost_usd", None)
        else:
            cost = (tokens_prompt * pricing["input"] + tokens_completion * pricing["output"]) / 1_000_000
            object.__setattr__(self, "cost_status", "known")
            object.__setattr__(self, "cost_usd", cost)


def _extract_usage(response: Any) -> tuple[int, int, int]:
    """Extract (input_tokens, output_tokens, total_tokens) from an LLM response.

    Supports multiple response formats:
    - ``response.usage.input_tokens``  (Anthropic SDK / raw OpenAI)
    - ``response.usage_metadata``      (LangChain ChatOpenAI ≥0.1)
    - ``response.response_metadata["token_usage"]``  (LangChain ChatOpenAI)
    Returns ``(0, 0, 0)`` when token data is unavailable.
    """
    # 1. Direct .usage attribute (Anthropic SDK style)
    usage = getattr(response, "usage", None)
    if usage is not None and hasattr(usage, "input_tokens"):
        return (
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
            getattr(usage, "total_tokens", 0) or 0,
        )

    # 2. LangChain usage_metadata (ChatOpenAI ≥0.1)
    usage_meta = getattr(response, "usage_metadata", None)
    if isinstance(usage_meta, dict):
        inp = usage_meta.get("input_tokens", 0) or 0
        out = usage_meta.get("output_tokens", 0) or 0
        return inp, out, inp + out

    # 3. LangChain response_metadata.token_usage (ChatOpenAI)
    resp_meta = getattr(response, "response_metadata", None)
    if isinstance(resp_meta, dict):
        token_usage = resp_meta.get("token_usage", {})
        if isinstance(token_usage, dict):
            inp = token_usage.get("prompt_tokens", 0) or 0
            out = token_usage.get("completion_tokens", 0) or 0
            total = token_usage.get("total_tokens", 0) or 0
            return inp, out, total or (inp + out)

    log.warning("Could not extract token usage from %s", type(response).__name__)
    return 0, 0, 0


def track_cost(state: dict[str, Any], node: str, response: Any) -> None:
    """Append per-node token usage and cost to *state* in-place.

    Args:
        state:    The mutable LangGraph state dict.  Must contain the keys
                  ``model_id``, ``cost_breakdown``, ``total_cost_usd``, and
                  ``total_tokens``.
        node:     Name of the calling LangGraph node (e.g. ``"miner"``).
        response: LLM response object — supports Anthropic SDK ``.usage``,
                  LangChain ``usage_metadata``, and ``response_metadata``
                  token-usage formats.  Falls back to zero when unavailable.
    """
    model_id = state.get("model_id", "unknown")
    inp_tok, out_tok, total_tok = _extract_usage(response)
    estimate = CostEstimate(model_id=model_id, tokens_prompt=inp_tok, tokens_completion=out_tok)
    if estimate.cost_status == "unknown":
        log.warning("No pricing entry for model '%s'; cost recorded as unknown", model_id)

    state["cost_breakdown"].append(
        {
            "node": node,
            "input_tokens": inp_tok,
            "output_tokens": out_tok,
            "model_id": model_id,
            "cost_status": estimate.cost_status,
            "cost_usd": estimate.cost_usd,
        }
    )
    if estimate.cost_status == "unknown" or state.get("cost_status") == "unknown":
        state["cost_status"] = "unknown"
        state["total_cost_usd"] = None
    else:
        state["cost_status"] = "known"
        state["total_cost_usd"] = float(state.get("total_cost_usd", 0.0) or 0.0) + float(estimate.cost_usd or 0.0)
    state["total_tokens"] += total_tok

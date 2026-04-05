"""Cost tracking utilities for the Miner-Critic-Judge pipeline.

MODEL_PRICING stores per-million-token rates (USD) for all supported models.
track_cost() mutates the pipeline state in-place — it is designed to be called
at the end of each LangGraph node after the LLM response is received.
"""
from __future__ import annotations

from typing import Any

# USD per 1 million tokens -------------------------------------------------
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "gpt-4o":                    {"input": 2.5, "output": 10.0},
    "claude-haiku-4-5-20251001": {"input": 0.8, "output": 4.0},
}


def track_cost(state: dict[str, Any], node: str, response: Any) -> None:
    """Append per-node token usage and cost to *state* in-place.

    Args:
        state:    The mutable LangGraph state dict.  Must contain the keys
                  ``model_id``, ``cost_breakdown``, ``total_cost_usd``, and
                  ``total_tokens``.
        node:     Name of the calling LangGraph node (e.g. ``"miner"``).
        response: LLM response object that exposes a ``.usage`` attribute with
                  ``input_tokens``, ``output_tokens``, and ``total_tokens``.

    Raises:
        KeyError: If ``state["model_id"]`` is not present in MODEL_PRICING.
    """
    pricing = MODEL_PRICING[state["model_id"]]
    cost = (
        response.usage.input_tokens * pricing["input"]
        + response.usage.output_tokens * pricing["output"]
    ) / 1_000_000

    state["cost_breakdown"].append(
        {
            "node": node,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "model_id": state["model_id"],
            "cost_usd": cost,
        }
    )
    state["total_cost_usd"] += cost
    state["total_tokens"] += response.usage.total_tokens

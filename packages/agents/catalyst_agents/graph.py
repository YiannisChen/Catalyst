"""LangGraph MCJ workflow assembly.

Provides build_attribution_graph() which wires Miner → Critic → DecisionRouter
→ Judge/Refuse with validation and finalization.

langgraph is a required dependency — there is no fallback runner.  If it
is missing, build_attribution_graph raises ImportError immediately so the
failure is explicit (BUG-009).

Spec reference: Section 4.4 — Graph Construction.
"""
from __future__ import annotations

from functools import partial
from typing import Any

from catalyst_agents.state import AttributionState
from catalyst_agents.nodes.miner import miner
from catalyst_agents.nodes.critic import critic, insufficient_handler, system_error_handler
from catalyst_agents.nodes.decision_router import decision_router, route_after_decision_router
from catalyst_agents.nodes.judge import judge
from catalyst_agents.nodes.validator import validator
from catalyst_agents.nodes.finalizer import finalizer
from catalyst_agents.retrieval.policy import Layer


# ---------------------------------------------------------------------------
# Expansion transition
# ---------------------------------------------------------------------------

def expand_macro_transition(state: dict) -> dict:
    """Switch the retrieval path to Layer 2 and loop back through Miner."""
    metadata = state.get("retrieval_metadata")
    if metadata is not None and hasattr(metadata, "expansion_reasons"):
        reasons = getattr(metadata, "expansion_reasons")
        if "critic_expand_macro" not in reasons:
            reasons.append("critic_expand_macro")
    return {
        "router_reason": state.get("router_reason", "critic_expand_macro"),
        "current_layer": Layer.MACRO,
        "expansions_used": int(state.get("expansions_used", 0) or 0) + 1,
        "retrieval_metadata": metadata,
    }


def _baseline_graded_evidence(reranked_chunks: list[dict]) -> list[dict]:
    """Project Miner output into Judge-readable evidence when Critic is disabled."""
    return [
        {
            "chunk_id": chunk.get("asset_id", ""),
            "relevance": chunk.get("rerank_score", chunk.get("rrf_score", 1.0)),
            "category": "unknown",
            "temporal_match": True,
            "reasoning": "Critic disabled; forwarding Miner evidence directly to Judge.",
        }
        for chunk in reranked_chunks
    ]


def baseline_prepare_evidence(state: dict) -> dict:
    """Prepare Judge-readable evidence when Critic is disabled."""
    return {
        "graded_evidence": _baseline_graded_evidence(state.get("reranked_chunks", [])),
    }


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_attribution_graph(
    *,
    use_critic: bool = True,
    table: Any = None,
    embedding_fn: Any = None,
    reranker: Any = None,
    llm: Any = None,
):
    """Build the MCJ attribution graph.

    Dependencies are bound via functools.partial so nodes receive their
    injected deps when invoked by the graph runner.

    Uses the real ``langgraph.graph.StateGraph``.  ``langgraph`` is a required
    dependency (see ``pyproject.toml``); if it is missing the import fails
    loudly at call time — no silent fallback (BUG-009).

    Args:
        use_critic:   When True, inserts the Critic node and conditional routing
                      between Miner and Judge. When False, Miner feeds directly
                      into Judge (baseline ablation mode).
        table:        LanceDB table passed to the Miner node.
        embedding_fn: Callable(str) -> list[float] passed to the Miner node.
        reranker:     Cross-encoder reranker passed to the Miner node.
        llm:          LLM client passed to Critic and Judge nodes.

    Returns:
        A compiled langgraph StateGraph with ``.invoke(state: dict) -> dict``.
    """
    from langgraph.graph import StateGraph, END

    # Bind dependencies to nodes via partial application
    bound_miner = partial(miner, table=table, embedding_fn=embedding_fn, reranker=reranker)
    bound_critic = partial(critic, llm=llm)
    bound_router = decision_router
    bound_judge = partial(judge, llm=llm)
    bound_validator = partial(validator, llm=llm)

    graph = StateGraph(AttributionState)
    graph.add_node("miner", bound_miner)
    graph.add_node("decision_router", bound_router)
    graph.add_node("expand_macro", expand_macro_transition)
    graph.add_node("judge", bound_judge)
    graph.add_node("validator", bound_validator)
    graph.add_node("finalizer", finalizer)
    graph.add_node("insufficient_handler", insufficient_handler)
    graph.add_node("system_error_handler", system_error_handler)
    graph.add_node("baseline_prepare_evidence", baseline_prepare_evidence)

    graph.set_entry_point("miner")

    if use_critic:
        graph.add_node("critic", bound_critic)
        graph.add_edge("miner", "critic")
        graph.add_edge("critic", "decision_router")
        graph.add_conditional_edges(
            "decision_router",
            route_after_decision_router,
            {
                "judge": "judge",
                "expand_macro": "expand_macro",
                "insufficient": "insufficient_handler",
                "system_error": "system_error_handler",
            },
        )
    else:
        graph.add_edge("miner", "baseline_prepare_evidence")
        graph.add_edge("baseline_prepare_evidence", "judge")

    graph.add_edge("judge", "validator")
    graph.add_edge("expand_macro", "miner")
    graph.add_edge("validator", "finalizer")
    graph.add_edge("insufficient_handler", "finalizer")
    graph.add_edge("system_error_handler", "finalizer")
    graph.add_edge("finalizer", END)
    return graph.compile()

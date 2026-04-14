"""LangGraph MCJ workflow assembly.

Provides build_attribution_graph() which wires Miner → Critic → Judge
with conditional routing on evidence quality.

If langgraph is installed, uses real StateGraph.
If not, provides a lightweight sequential runner for testing.

Spec reference: Section 4.4 — Graph Construction.
"""
from __future__ import annotations

from functools import partial
from typing import Any

from catalyst_agents.state import AttributionState
from catalyst_agents.nodes.miner import miner
from catalyst_agents.nodes.critic import critic, insufficient_handler, system_error_handler
from catalyst_agents.nodes.judge import judge


# ---------------------------------------------------------------------------
# Routing logic
# ---------------------------------------------------------------------------

def route_after_critic(state: dict) -> str:
    """Conditional edge: route to judge, insufficient, or system_error.

    Checks error_type first — a system_error (LLM failure, bad JSON after
    retries) is distinct from legitimately empty evidence. This prevents
    BUG-005: conflating infrastructure failures with data gaps.

    Args:
        state: Current graph state dict.

    Returns:
        "system_error" when error_type is set,
        "insufficient" when graded_evidence is empty,
        "judge" otherwise.
    """
    if state.get("error_type") == "system_error":
        return "system_error"
    if not state.get("graded_evidence"):
        return "insufficient"
    return "judge"


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

    Attempts to use langgraph.graph.StateGraph when available. Falls back
    to a lightweight _SequentialRunner for environments where langgraph is
    not installed (e.g., CI, tests).

    Args:
        use_critic:   When True, inserts the Critic node and conditional routing
                      between Miner and Judge. When False, Miner feeds directly
                      into Judge (baseline ablation mode).
        table:        LanceDB table passed to the Miner node.
        embedding_fn: Callable(str) -> list[float] passed to the Miner node.
        reranker:     Cross-encoder reranker passed to the Miner node.
        llm:          LLM client passed to Critic and Judge nodes.

    Returns:
        An object with .invoke(state: dict) -> dict. Either a compiled
        langgraph StateGraph or a _SequentialRunner instance.
    """
    # Bind dependencies to nodes via partial application
    bound_miner = partial(miner, table=table, embedding_fn=embedding_fn, reranker=reranker)
    bound_critic = partial(critic, llm=llm)
    bound_judge = partial(judge, llm=llm)

    try:
        from langgraph.graph import StateGraph, END  # type: ignore[import]

        graph = StateGraph(AttributionState)
        graph.add_node("miner", bound_miner)
        graph.add_node("judge", bound_judge)
        graph.add_node("insufficient_handler", insufficient_handler)
        graph.add_node("system_error_handler", system_error_handler)
        graph.add_node("baseline_prepare_evidence", baseline_prepare_evidence)

        graph.set_entry_point("miner")

        if use_critic:
            graph.add_node("critic", bound_critic)
            graph.add_edge("miner", "critic")
            graph.add_conditional_edges(
                "critic",
                route_after_critic,
                {
                    "judge": "judge",
                    "insufficient": "insufficient_handler",
                    "system_error": "system_error_handler",
                },
            )
        else:
            graph.add_edge("miner", "baseline_prepare_evidence")
            graph.add_edge("baseline_prepare_evidence", "judge")

        graph.add_edge("judge", END)
        graph.add_edge("insufficient_handler", END)
        graph.add_edge("system_error_handler", END)
        return graph.compile()

    except ImportError:
        # Fallback: lightweight sequential runner when langgraph is not installed
        return _SequentialRunner(
            bound_miner=bound_miner,
            bound_critic=bound_critic,
            bound_judge=bound_judge,
            insufficient_handler=insufficient_handler,
            system_error_handler=system_error_handler,
            use_critic=use_critic,
        )


# ---------------------------------------------------------------------------
# Fallback runner
# ---------------------------------------------------------------------------

class _SequentialRunner:
    """Minimal graph runner for environments where langgraph is not installed.

    Executes nodes in the same topological order as the real StateGraph would,
    including the conditional routing decision after the Critic node.
    State is mutated in-place after each node via dict.update().
    """

    def __init__(
        self,
        *,
        bound_miner,
        bound_critic,
        bound_judge,
        insufficient_handler,
        system_error_handler,
        use_critic: bool,
    ) -> None:
        self._miner = bound_miner
        self._critic = bound_critic
        self._judge = bound_judge
        self._insufficient = insufficient_handler
        self._system_error = system_error_handler
        self._use_critic = use_critic

    def invoke(self, state: dict) -> dict:
        """Run the pipeline sequentially and return the final state.

        Args:
            state: Starting AttributionState dict. Modified in-place.

        Returns:
            The state dict after all nodes have executed.
        """
        # Miner — deterministic retrieval
        state.update(self._miner(state))

        if self._use_critic:
            # Critic — evidence grading
            state.update(self._critic(state))

            # Conditional routing after Critic
            route = route_after_critic(state)
            if route == "system_error":
                state.update(self._system_error(state))
                return state
            if route == "insufficient":
                state.update(self._insufficient(state))
                return state
        else:
            state.update(baseline_prepare_evidence(state))

        # Judge — synthesis
        state.update(self._judge(state))
        return state

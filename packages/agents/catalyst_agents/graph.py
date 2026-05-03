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
import json
import time
from typing import Any

from catalyst_agents.state import AttributionState
from catalyst_agents.nodes.miner import miner
from catalyst_agents.nodes.critic import critic, insufficient_handler, system_error_handler
from catalyst_agents.nodes.decision_router import decision_router, route_after_decision_router
from catalyst_agents.nodes.judge import judge
from catalyst_agents.nodes.validator import validator
from catalyst_agents.nodes.finalizer import finalizer
from catalyst_agents.retrieval.policy import Layer
from catalyst_agents.trace.writer import TraceWriter, activate_writer, get_current_writer


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


def _status_name(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "name", None) or str(value)


def _decision_payload(node_name: str, merged_state: dict) -> str | None:
    payload: dict[str, Any] = {}
    if node_name == "decision_router":
        payload = {
            "router_edge": merged_state.get("router_edge"),
            "router_reason": merged_state.get("router_reason"),
        }
    elif node_name == "critic" and merged_state.get("critic_decision") is not None:
        decision = merged_state["critic_decision"]
        payload = {
            "sufficiency": decision.sufficiency,
            "next_action": decision.next_action,
            "magnitude_coverage": decision.magnitude_coverage,
        }
    elif node_name in {"miner", "expand_macro"} and merged_state.get("retrieval_metadata") is not None:
        metadata = merged_state["retrieval_metadata"]
        payload = {
            "layers_attempted": [getattr(layer, "value", str(layer)) for layer in getattr(metadata, "layers_attempted", [])],
            "stop_reason": getattr(metadata, "stop_reason", None),
            "hit_counts_per_layer": {
                getattr(layer, "value", str(layer)): count
                for layer, count in getattr(metadata, "hit_counts_per_layer", {}).items()
            },
            "expansion_reasons": list(getattr(metadata, "expansion_reasons", [])),
        }
    if not payload:
        return None
    return json.dumps(payload, sort_keys=True)


def _error_message(merged_state: dict) -> str | None:
    if merged_state.get("error_type") == "system_error":
        return merged_state.get("critic_reasoning") or merged_state.get("summary_md")
    return merged_state.get("validation_error")


def _trace_node(node_name: str, fn):
    """Wrap a graph node so every invocation emits one trace event when tracing is active."""

    def wrapped(state: dict) -> dict:
        writer = get_current_writer()
        if writer is None:
            return fn(state)

        started_wall = time.time()
        started_perf = time.perf_counter()
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_wall))
        before_status = _status_name(state.get("output_status"))
        before_breakdown_len = len(state.get("cost_breakdown", []) or [])

        try:
            result = fn(state)
        except Exception as exc:
            ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            writer.event(
                node=node_name,
                started_at=started_at,
                ended_at=ended_at,
                latency_ms=int((time.perf_counter() - started_perf) * 1000),
                model_id=state.get("model_id"),
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                decision=None,
                error_type="system_error",
                error_message=str(exc),
                status_before=before_status,
                status_after=before_status,
            )
            raise

        merged_state = {**state, **result}
        new_breakdown = (merged_state.get("cost_breakdown", []) or [])[before_breakdown_len:]
        writer.event(
            node=node_name,
            started_at=started_at,
            ended_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            latency_ms=int((time.perf_counter() - started_perf) * 1000),
            model_id=merged_state.get("model_id"),
            input_tokens=sum(int(entry.get("input_tokens", 0) or 0) for entry in new_breakdown),
            output_tokens=sum(int(entry.get("output_tokens", 0) or 0) for entry in new_breakdown),
            cost_usd=sum(float(entry.get("cost_usd", 0.0) or 0.0) for entry in new_breakdown),
            decision=_decision_payload(node_name, merged_state),
            error_type=merged_state.get("error_type"),
            error_message=_error_message(merged_state),
            status_before=before_status,
            status_after=_status_name(merged_state.get("output_status")),
        )
        return result

    return wrapped


class _TracedCompiledGraph:
    """Thin wrapper that attaches a TraceWriter around compiled graph execution."""

    def __init__(self, compiled_graph: Any, *, config_name: str) -> None:
        self._compiled_graph = compiled_graph
        self._config_name = config_name

    def invoke(self, state: dict) -> dict:
        with TraceWriter(
            ticker=state.get("ticker"),
            trade_date=state.get("trade_date"),
            config=self._config_name,
        ) as writer:
            with activate_writer(writer):
                result = self._compiled_graph.invoke(state)
            writer.complete(result)
            return {
                **result,
                "run_id": writer.run_id,
                "trace_id": writer.trace_id,
            }

    def __getattr__(self, name: str) -> Any:
        return getattr(self._compiled_graph, name)


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
    bound_miner = _trace_node("miner", partial(miner, table=table, embedding_fn=embedding_fn, reranker=reranker))
    bound_critic = _trace_node("critic", partial(critic, llm=llm))
    bound_router = _trace_node("decision_router", decision_router)
    bound_judge = _trace_node("judge", partial(judge, llm=llm))
    bound_validator = _trace_node("validator", partial(validator, llm=llm))
    bound_finalizer = _trace_node("finalizer", finalizer)
    bound_insufficient = _trace_node("insufficient_handler", insufficient_handler)
    bound_system_error = _trace_node("system_error_handler", system_error_handler)
    bound_baseline = _trace_node("baseline_prepare_evidence", baseline_prepare_evidence)
    bound_expand_macro = _trace_node("expand_macro", expand_macro_transition)

    graph = StateGraph(AttributionState)
    graph.add_node("miner", bound_miner)
    graph.add_node("decision_router", bound_router)
    graph.add_node("expand_macro", bound_expand_macro)
    graph.add_node("judge", bound_judge)
    graph.add_node("validator", bound_validator)
    graph.add_node("finalizer", bound_finalizer)
    graph.add_node("insufficient_handler", bound_insufficient)
    graph.add_node("system_error_handler", bound_system_error)
    graph.add_node("baseline_prepare_evidence", bound_baseline)

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
    compiled = graph.compile()
    return _TracedCompiledGraph(compiled, config_name="mcj_full" if use_critic else "baseline")

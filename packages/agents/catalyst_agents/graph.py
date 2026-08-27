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
from catalyst_agents.attribution.context_builder import ContextBuilder, canonical_context_bytes
from catalyst_agents.nodes.miner import miner
from catalyst_agents.nodes.critic import critic, insufficient_handler, system_error_handler
from catalyst_agents.nodes.decision_router import decision_router, route_after_decision_router
from catalyst_agents.nodes.judge import judge
from catalyst_agents.nodes.validator import validator
from catalyst_agents.nodes.finalizer import finalizer
from catalyst_agents.retrieval.policy import Layer
from catalyst_agents.trace.artifacts import write_node_artifact
from catalyst_agents.trace.projection import project_node_artifacts
from catalyst_agents.trace.writer import TraceWriter, activate_writer, get_current_writer


class AttributionDependencyError(RuntimeError):
    pass


def context_builder_node(state: dict, *, context_provider: Any, cutoff_policy: Any) -> dict:
    cutoff = cutoff_policy.compute_cutoff(ticker=state["ticker"], session_date=state["trade_date"], mode="attribution")
    artifact = ContextBuilder(provider=context_provider).build(
        ticker=state["ticker"],
        session_date=state["trade_date"],
        cutoff=cutoff,
    )
    return {
        "context_artifact": artifact.model_dump(mode="json"),
        "context_artifact_sha256": __import__("hashlib").sha256(canonical_context_bytes(artifact)).hexdigest(),
        "cutoff": cutoff,
        "context_cutoff": cutoff,
        "price_move_pct": artifact.target_return_pct,
        "market_session_valid": artifact.target_return_pct is not None,
    }


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
            "relevance": min(1.0, float(chunk.get("rerank_score", chunk.get("rrf_score", 1.0)) or 0.0)),
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
        event_seq = writer.event(
            node=node_name,
            started_at=started_at,
            ended_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            latency_ms=int((time.perf_counter() - started_perf) * 1000),
            model_id=merged_state.get("model_id"),
            input_tokens=sum(int(entry.get("input_tokens", 0) or 0) for entry in new_breakdown),
            output_tokens=sum(int(entry.get("output_tokens", 0) or 0) for entry in new_breakdown),
            cost_usd=None if any(entry.get("cost_status") == "unknown" or entry.get("cost_usd") is None for entry in new_breakdown) else sum(float(entry.get("cost_usd", 0.0) or 0.0) for entry in new_breakdown),
            decision=_decision_payload(node_name, merged_state),
            error_type=merged_state.get("error_type"),
            error_message=_error_message(merged_state),
            status_before=before_status,
            status_after=_status_name(merged_state.get("output_status")),
        )
        try:
            for artifact in project_node_artifacts(node_name, merged_state, result):
                write_node_artifact(
                    writer.conn,
                    run_id=writer.run_id,
                    event_seq=event_seq,
                    node=node_name,
                    artifact_type=artifact["artifact_type"],
                    payload=artifact["payload_json"],
                )
        except Exception:
            # Artifact persistence is observability-only and must not override MCJ node success.
            pass
        return result

    return wrapped


class _TracedCompiledGraph:
    """Thin wrapper that attaches a TraceWriter around compiled graph execution."""

    def __init__(self, compiled_graph: Any, *, config_name: str) -> None:
        self._compiled_graph = compiled_graph
        self._config_name = config_name

    def invoke(self, state: dict, run_id: str | None = None) -> dict:
        with TraceWriter(
            run_id=run_id,
            ticker=state.get("ticker"),
            trade_date=state.get("trade_date"),
            config=self._config_name,
        ) as writer:
            try:
                with activate_writer(writer):
                    result = self._compiled_graph.invoke(state)
            except Exception as exc:
                writer.complete({
                    **state,
                    "output_status": "SYSTEM_ERROR",
                    "error_type": "system_error",
                    "validation_error": str(exc),
                })
                raise
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
    context_provider: Any = None,
    retriever: Any = None,
    cutoff_policy: Any = None,
    requested_manifest_id: str | None = None,
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

    if context_provider is None or retriever is None or cutoff_policy is None or not requested_manifest_id:
        raise AttributionDependencyError(
            "build_attribution_graph requires context_provider, retriever, cutoff_policy, and requested_manifest_id"
        )

    # Bind dependencies to nodes via partial application
    bound_context = _trace_node("context_builder", partial(context_builder_node, context_provider=context_provider, cutoff_policy=cutoff_policy))
    bound_miner = _trace_node("miner", partial(miner, retriever=retriever, cutoff_policy=cutoff_policy, requested_manifest_id=requested_manifest_id, table=table, embedding_fn=embedding_fn, reranker=reranker))
    bound_critic = _trace_node("critic", partial(critic, llm=llm))
    bound_router = _trace_node("decision_router", decision_router)
    bound_judge = _trace_node("judge", partial(judge, llm=llm))
    bound_validator = _trace_node("validator", partial(validator, llm=llm, cutoff_policy=cutoff_policy))
    bound_finalizer = _trace_node("finalizer", finalizer)
    bound_insufficient = _trace_node("insufficient_handler", insufficient_handler)
    bound_system_error = _trace_node("system_error_handler", system_error_handler)
    bound_baseline = _trace_node("baseline_prepare_evidence", baseline_prepare_evidence)
    bound_expand_macro = _trace_node("expand_macro", expand_macro_transition)

    graph = StateGraph(AttributionState)
    graph.add_node("context_builder", bound_context)
    graph.add_node("miner", bound_miner)
    graph.add_node("decision_router", bound_router)
    graph.add_node("expand_macro", bound_expand_macro)
    graph.add_node("judge", bound_judge)
    graph.add_node("validator", bound_validator)
    graph.add_node("finalizer", bound_finalizer)
    graph.add_node("insufficient_handler", bound_insufficient)
    graph.add_node("system_error_handler", bound_system_error)
    graph.add_node("baseline_prepare_evidence", bound_baseline)

    graph.set_entry_point("context_builder")
    graph.add_edge("context_builder", "miner")

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


# ---------------------------------------------------------------------------
# M4-8: authoritative V1.1 foundation graph (fixture mode)
# ---------------------------------------------------------------------------
# The foundation path wires observation_build -> research_policy ->
# research_execution -> evidence_state -> coverage_summary ->
# context_pack_build -> fixture Analyst boundary. It never runs
# Miner/Critic/Judge. The sealed MCJ graph above is BASELINE_ONLY and stays
# readable/compilable until the M5 comparison gate; nothing is deleted.

from dataclasses import dataclass
import hashlib
import time
from typing import Any, Callable

from catalyst_agents.attribution.context_pack import EvidenceAnalystContextPack
from catalyst_agents.attribution.context_pack_builder import (
    ContextPackBuilder,
    ContextPackFinalizer,
    PackedContextDraft,
    RenderMessage,
    canonical_context_pack_json,
)
from catalyst_agents.attribution.coverage import CoverageSummary
from catalyst_agents.attribution.coverage_builder import build_coverage_summary
from catalyst_agents.attribution.evidence_state import EvidenceState
from catalyst_agents.attribution.evidence_state_builder import (
    EvidenceStateBuildContext,
    build_evidence_state,
)
from catalyst_agents.attribution.context_builder import ObservationBuilder
from catalyst_agents.attribution.move_profile import MoveProfile
from catalyst_agents.attribution.provider import ContextProvider
from catalyst_agents.retrieval.execution import (
    ResearchDeadlineError,
    ResearchExecution,
    ResearchExecutor,
    ResearchRetriever,
)
from catalyst_agents.retrieval.policy import InitialResearchPolicy, ScenarioClassification
from catalyst_agents.runtime.manifest import ObservationPolicyConfig
from catalyst_agents.runtime.pack_persistence import (
    PackPersistence,
    PersistedPackPair,
    replay_asserts_pair,
)
from catalyst_agents.runtime.token_budget import UTF8ByteUpperBoundCounter
from catalyst_agents.state import FoundationGraphState, FoundationStage
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity


def _foundation_renderer(draft: PackedContextDraft) -> tuple[RenderMessage, ...]:
    """Deterministic fixture renderer; never includes context_pack_sha256."""
    return (
        RenderMessage(
            role="system",
            content="observation="
            + canonical_context_pack_json(
                draft.observation.model_dump(mode="json")
            ).decode("utf-8"),
        ),
        RenderMessage(
            role="system",
            content="inventory="
            + ",".join(item.evidence_id for item in draft.evidence_inventory),
        ),
        RenderMessage(
            role="system",
            content="coverage="
            + canonical_context_pack_json(
                draft.coverage_summary.model_dump(mode="json")
            ).decode("utf-8"),
        ),
    )


def _artifact_hash(value: Any) -> str:
    return hashlib.sha256(
        canonical_context_pack_json(value.model_dump(mode="json"))
    ).hexdigest()


@dataclass(frozen=True)
class FoundationRunResult:
    """Bounded fixture run result: thin state plus the immutable artifacts."""

    state: FoundationGraphState
    move_profile: MoveProfile
    classification: ScenarioClassification
    research_execution: ResearchExecution
    evidence_state: EvidenceState
    coverage_summary: CoverageSummary
    context_pack: EvidenceAnalystContextPack
    persisted_pair: PersistedPackPair | None
    analyst_result: Any | None


def build_foundation_graph(
    *,
    run_id: str,
    round: int,
    temporal_identity: TemporalIdentity,
    data_runtime_identity: DataRuntimeIdentity,
    ticker: str,
    cutoff: str,
    requested_manifest_id: str,
    observation_provider: ContextProvider,
    retriever: ResearchRetriever,
    policy_config: ObservationPolicyConfig,
    research_concurrency: int,
    research_stage_timeout_seconds: float,
    persistence: PackPersistence,
    packing_policy_version: str,
    template_bytes: bytes,
    template_version: str,
    analyst_boundary: Callable[[EvidenceAnalystContextPack], Any] | None = None,
    structured_provider: Any | None = None,
    run_deadline_epoch_ms: int | None = None,
) -> FoundationRunResult:
    """Run the V1.1 foundation pipeline in fixture mode (no Miner/Critic/Judge).

    The Analyst boundary is a fixture stub in M4; M5 replaces it with the
    production EvidenceAnalyst. ``run_deadline_epoch_ms`` is the absolute Unix
    epoch-millisecond run deadline from the runtime/manifest authority; the
    stage deadline may shorten but never extend it.
    """
    # Absolute deadlines are computed once, before the pipeline, in Unix epoch
    # ms (never time.monotonic(); FIX 3C). The effective stage deadline is the
    # minimum of the configured stage deadline and the injected run deadline:
    # the stage may shorten but never extend the run deadline, and a stage
    # timeout that exceeds the remaining run time is not an error in itself.
    now_epoch_ms = int(time.time() * 1000)
    configured_stage_deadline = now_epoch_ms + int(
        research_stage_timeout_seconds * 1000
    )
    if run_deadline_epoch_ms is not None:
        effective_deadline = min(configured_stage_deadline, run_deadline_epoch_ms)
    else:
        effective_deadline = configured_stage_deadline

    # 1. observation_build (TemporalIdentity-authoritative window)
    observation_builder = ObservationBuilder(
        provider=observation_provider, policy=policy_config
    )
    move_profile = observation_builder.build(
        ticker=ticker,
        session_date=temporal_identity.session_date,
        cutoff=temporal_identity.cutoff_at.isoformat(),
        temporal_identity=temporal_identity,
    )

    # 2. research_policy
    policy = InitialResearchPolicy(policy_config)
    classification = policy.classify(move_profile)

    # 3. research_execution. The executor still uses monotonic time internally,
    # but the stage budget is the remaining wall time up to effective_deadline
    # (never extends the persisted run deadline).
    current_epoch_ms = int(time.time() * 1000)
    remaining_seconds = (effective_deadline - current_epoch_ms) / 1000
    if remaining_seconds <= 0:
        raise ResearchDeadlineError(
            "research stage deadline expired before research execution"
        )
    executor = ResearchExecutor(
        retriever=retriever,
        concurrency=research_concurrency,
        stage_timeout_seconds=remaining_seconds,
        structured_provider=structured_provider,
    )
    execution = executor.execute(
        tasks=classification.tasks,
        run_id=run_id,
        round=round,
        temporal_identity=temporal_identity,
        data_runtime_identity=data_runtime_identity,
        research_policy_version=policy_config.scenario_policy_version,
        ticker=ticker,
        cutoff=cutoff,
        requested_manifest_id=requested_manifest_id,
    )

    # 4. evidence_state
    evidence_state = build_evidence_state(
        EvidenceStateBuildContext(
            run_id=run_id,
            round=round,
            temporal_identity=temporal_identity,
            data_runtime_identity=data_runtime_identity,
            research_policy_version=policy_config.scenario_policy_version,
            task_results=execution.task_results,
        )
    )

    # 5. coverage_summary
    coverage = build_coverage_summary(
        evidence_state,
        move_profile,
        execution.degradations,
        execution.capability_gaps,
    )

    # 6. context_pack_build
    counter = UTF8ByteUpperBoundCounter(provider="catalyst", model_id="analyst-default")
    builder = ContextPackBuilder(
        packing_policy_version=packing_policy_version,
        budget=_foundation_budget(),
        token_counter=counter,
    )
    draft = builder.build_draft(
        run_id=run_id,
        round=round,
        temporal_identity=temporal_identity,
        data_runtime_identity=data_runtime_identity,
        evidence_state=evidence_state,
        move_profile=move_profile,
        coverage_summary=coverage,
        research_history=classification.tasks,
    )
    finalizer = ContextPackFinalizer(
        template_bytes=template_bytes,
        template_version=template_version,
        renderer=_foundation_renderer,
        token_counter=counter,
    )
    pack = finalizer.finalize(draft)

    # 7. persistence + replay assertion (provider dispatch gate)
    messages = _foundation_renderer(draft)
    refs = persistence.persist_pack_and_render(
        run_id=run_id,
        pack=pack,
        pack_sha256=pack.context_pack_sha256,
        rendered_messages=messages,
        rendered_messages_sha256=pack.rendered_messages_sha256,
        prompt_template_version=template_version,
        prompt_template_sha256=pack.prompt_template_sha256,
    )
    persisted_pair = persistence.load_pair(run_id=run_id)
    replay_asserts_pair(
        refs,
        pack_sha256=pack.context_pack_sha256,
        rendered_messages=messages,
        rendered_messages_sha256=pack.rendered_messages_sha256,
        prompt_template_sha256=pack.prompt_template_sha256,
    )

    # 8. fixture Analyst boundary (M5 replaces this stub)
    analyst_result = analyst_boundary(pack) if analyst_boundary is not None else None

    state: FoundationGraphState = {
        "run_id": run_id,
        "stage": FoundationStage.ANALYST_BOUNDARY,
        "round": round,
        "attempt": 1,
        "deadline_epoch_ms": effective_deadline,
        "cancel_requested": False,
        "terminal_error": None,
        "observation_ref": f"artifact:observation:{run_id}:{round}",
        "observation_hash": _artifact_hash(move_profile),
        "research_tasks_ref": f"artifact:research_tasks:{run_id}:{round}",
        "research_results_ref": f"artifact:research_results:{run_id}:{round}",
        "evidence_state_ref": f"artifact:evidence_state:{run_id}:{round}",
        "evidence_state_hash": evidence_state.state_hash,
        "coverage_summary_ref": f"artifact:coverage_summary:{run_id}:{round}",
        "coverage_summary_hash": _artifact_hash(coverage),
        "context_pack_ref": refs.pack_artifact_id,
        "context_pack_hash": pack.context_pack_sha256,
        "rendered_messages_ref": refs.rendered_messages_artifact_id,
        "rendered_messages_hash": pack.rendered_messages_sha256,
        "prompt_template_version": template_version,
        "prompt_template_sha256": pack.prompt_template_sha256,
        "policy_version": policy_config.scenario_policy_version,
    }
    return FoundationRunResult(
        state=state,
        move_profile=move_profile,
        classification=classification,
        research_execution=execution,
        evidence_state=evidence_state,
        coverage_summary=coverage,
        context_pack=pack,
        persisted_pair=persisted_pair,
        analyst_result=analyst_result,
    )


def _foundation_budget():
    from catalyst_agents.attribution.context_pack import ContextBudget

    return ContextBudget(
        model_context_limit=8_000,
        reserved_output_tokens=300,
        reserved_system_instruction_tokens=200,
        observation_tokens=200,
        coverage_summary_tokens=200,
        research_history_tokens=100,
        inventory_tokens=200,
        evidence_payload_tokens=2_000,
        per_news_item_max_tokens=200,
        per_sec_chunk_max_tokens=250,
        lead_only_tokens=80,
        safety_margin_tokens=100,
    )


# ---------------------------------------------------------------------------
# M5-4: round-two EvidenceAnalyst over cumulative evidence
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CorrectiveRoundResult:
    """Bounded round-two result: thin state plus immutable artifacts."""

    state: FoundationGraphState
    research_execution: ResearchExecution
    evidence_state: EvidenceState
    coverage_summary: CoverageSummary
    context_pack: EvidenceAnalystContextPack
    persisted_pair: PersistedPackPair | None
    analyst_decision: Any
    assessment: Any
    analyst_logical_calls: int
    analyst_provider_attempts: int
    delta_evidence_ids: tuple[str, ...]


def run_corrective_round(
    *,
    run_id: str,
    round: int,
    temporal_identity: TemporalIdentity,
    data_runtime_identity: DataRuntimeIdentity,
    ticker: str,
    cutoff: str,
    requested_manifest_id: str,
    scenario: Any,
    move_profile: MoveProfile,
    prior_evidence_state: EvidenceState,
    prior_research_execution: ResearchExecution,
    prior_assessment: Any,
    research_policy_version: str,
    retriever: ResearchRetriever,
    research_concurrency: int,
    research_stage_timeout_seconds: float,
    persistence: PackPersistence,
    packing_policy_version: str,
    template_bytes: bytes,
    template_version: str,
    llm: Any,
    capability_registry: Any,
    query_builder: Callable[[Any, str], str] | None = None,
    structured_provider: Any | None = None,
    run_deadline_epoch_ms: int | None = None,
    policy: Any | None = None,
    normalization_policy_version: str = "assessment-normalizer-v1",
    prompt_template: str | None = None,
    research_history: tuple[Any, ...] = (),
) -> CorrectiveRoundResult:
    """Execute one corrective round over cumulative evidence (Frozen §6.4).

    FOLLOW_UP -> one CorrectiveResearchBatch -> ResearchExecutor -> cumulative
    EvidenceState (round-1 union round-2) -> recomputed CoverageSummary ->
    rebuilt round-2 ContextPack with prior assessment context -> second
    EvidenceAnalyst call -> round-2 EvidenceAssessment. There is no third
    Analyst call after round 2; a missing batch refuses to run and never calls
    the Analyst.
    """
    from catalyst_agents.attribution.assessment import normalize_decision
    from catalyst_agents.attribution.context_pack import PriorAssessmentContext
    from catalyst_agents.nodes.evidence_analyst import evidence_analyst
    from catalyst_agents.retrieval.corrective import (
        CorrectivePolicy,
        corrective_research_tasks,
    )

    if prior_assessment.corrective_batch is None:
        raise ValueError(
            "no corrective batch on the prior assessment; a second Analyst "
            "call must not run"
        )
    batch = prior_assessment.corrective_batch
    effective_policy = policy or CorrectivePolicy()

    now_epoch_ms = int(time.time() * 1000)
    configured_stage_deadline = now_epoch_ms + int(
        research_stage_timeout_seconds * 1000
    )
    effective_deadline = (
        min(configured_stage_deadline, run_deadline_epoch_ms)
        if run_deadline_epoch_ms is not None
        else configured_stage_deadline
    )
    current_epoch_ms = int(time.time() * 1000)
    remaining_seconds = (effective_deadline - current_epoch_ms) / 1000
    if remaining_seconds <= 0:
        raise ResearchDeadlineError(
            "research stage deadline expired before corrective execution"
        )

    tasks = corrective_research_tasks(
        batch.actions,
        run_id=run_id,
        round=round,
        scenario=scenario,
        retrieval_policy_id="corrective-v1",
    )
    full_research_history = tuple((*research_history, *tasks))
    executor = ResearchExecutor(
        retriever=retriever,
        concurrency=research_concurrency,
        stage_timeout_seconds=remaining_seconds,
        structured_provider=structured_provider,
        query_builder=query_builder,
    )
    corrective_execution = executor.execute(
        tasks=tasks,
        run_id=run_id,
        round=round,
        temporal_identity=temporal_identity,
        data_runtime_identity=data_runtime_identity,
        research_policy_version=research_policy_version,
        ticker=ticker,
        cutoff=cutoff,
        requested_manifest_id=requested_manifest_id,
    )

    # Cumulative evidence: round-1 union round-2, deduped by evidence/fact ID.
    evidence_state = build_evidence_state(
        EvidenceStateBuildContext(
            run_id=run_id,
            round=round,
            temporal_identity=temporal_identity,
            data_runtime_identity=data_runtime_identity,
            research_policy_version=research_policy_version,
            task_results=tuple(
                (*prior_research_execution.task_results, *corrective_execution.task_results)
            ),
            prior=prior_evidence_state,
        )
    )
    coverage = build_coverage_summary(
        evidence_state,
        move_profile,
        tuple((*prior_research_execution.degradations, *corrective_execution.degradations)),
        tuple((*prior_research_execution.capability_gaps, *corrective_execution.capability_gaps)),
    )

    prior_assessment_context = PriorAssessmentContext(
        prior_counter_evidence_ids=tuple(
            sorted(
                {
                    evidence_id
                    for hypothesis in prior_assessment.normalized_hypotheses
                    for evidence_id in hypothesis.contradicting_evidence_ids
                }
            )
        ),
        prior_semantic_conflicts=tuple(prior_assessment.normalized_conflicts),
        previously_supported_hypothesis_ids=tuple(
            hypothesis.hypothesis_id
            for hypothesis in prior_assessment.normalized_hypotheses
            if hypothesis.supporting_evidence_ids
        ),
        unresolved_gap_ids=tuple(
            gap.gap_id for gap in prior_assessment.validated_missing_evidence
        ),
        prior_evidence_assessment_ref=f"artifact:assessment:{run_id}:{round - 1}",
    )

    counter = UTF8ByteUpperBoundCounter(provider="catalyst", model_id="analyst-default")
    builder = ContextPackBuilder(
        packing_policy_version=packing_policy_version,
        budget=_foundation_budget(),
        token_counter=counter,
    )
    draft = builder.build_draft(
        run_id=run_id,
        round=round,
        temporal_identity=temporal_identity,
        data_runtime_identity=data_runtime_identity,
        evidence_state=evidence_state,
        move_profile=move_profile,
        coverage_summary=coverage,
        research_history=full_research_history,
        prior_assessment_context=prior_assessment_context,
    )
    finalizer = ContextPackFinalizer(
        template_bytes=template_bytes,
        template_version=template_version,
        renderer=_foundation_renderer,
        token_counter=counter,
    )
    pack = finalizer.finalize(draft)

    messages = _foundation_renderer(draft)
    refs = persistence.persist_pack_and_render(
        run_id=run_id,
        pack=pack,
        pack_sha256=pack.context_pack_sha256,
        rendered_messages=messages,
        rendered_messages_sha256=pack.rendered_messages_sha256,
        prompt_template_version=template_version,
        prompt_template_sha256=pack.prompt_template_sha256,
    )
    persisted_pair = persistence.load_pair(run_id=run_id)
    replay_asserts_pair(
        refs,
        pack_sha256=pack.context_pack_sha256,
        rendered_messages=messages,
        rendered_messages_sha256=pack.rendered_messages_sha256,
        prompt_template_sha256=pack.prompt_template_sha256,
    )

    analyst = evidence_analyst(
        {"run_id": run_id},
        llm=llm,
        persistence=persistence,
        prompt_template=prompt_template,
    )
    assessment = normalize_decision(
        analyst["analyst_decision"],
        pack,
        capability_registry,
        normalization_policy_version,
        policy=effective_policy,
    )

    state: FoundationGraphState = {
        "run_id": run_id,
        "stage": FoundationStage.ANALYST_BOUNDARY,
        "round": round,
        "attempt": 1,
        "deadline_epoch_ms": effective_deadline,
        "cancel_requested": False,
        "terminal_error": None,
        "observation_ref": f"artifact:observation:{run_id}:1",
        "observation_hash": _artifact_hash(move_profile),
        "research_tasks_ref": f"artifact:corrective_tasks:{run_id}:{round}",
        "research_results_ref": f"artifact:research_results:{run_id}:{round}",
        "evidence_state_ref": f"artifact:evidence_state:{run_id}:{round}",
        "evidence_state_hash": evidence_state.state_hash,
        "coverage_summary_ref": f"artifact:coverage_summary:{run_id}:{round}",
        "coverage_summary_hash": _artifact_hash(coverage),
        "context_pack_ref": refs.pack_artifact_id,
        "context_pack_hash": pack.context_pack_sha256,
        "rendered_messages_ref": refs.rendered_messages_artifact_id,
        "rendered_messages_hash": pack.rendered_messages_sha256,
        "prompt_template_version": template_version,
        "prompt_template_sha256": pack.prompt_template_sha256,
        "policy_version": research_policy_version,
    }
    return CorrectiveRoundResult(
        state=state,
        research_execution=corrective_execution,
        evidence_state=evidence_state,
        coverage_summary=coverage,
        context_pack=pack,
        persisted_pair=persisted_pair,
        analyst_decision=analyst["analyst_decision"],
        assessment=assessment,
        analyst_logical_calls=analyst["analyst_logical_calls"],
        analyst_provider_attempts=analyst["analyst_provider_attempts"],
        delta_evidence_ids=draft.delta_evidence_ids,
    )


# ---------------------------------------------------------------------------
# M5-9: V1.1 production graph over the normalized assessment
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V1RunResult:
    """Bounded V1.1 run result: thin state plus immutable artifacts."""

    state: FoundationGraphState
    move_profile: MoveProfile
    evidence_state: EvidenceState
    coverage_summary: CoverageSummary
    context_pack: EvidenceAnalystContextPack
    assessment: Any
    validated_claim_plan: Any
    answer: Any
    assurance_checks: tuple[Any, ...]
    terminal_envelope: dict
    logical_model_call_count: int
    analyst_logical_calls: int
    analyst_provider_attempts: int
    writer_logical_calls: int
    writer_provider_attempts: int
    corrective_rounds: int


def extract_answer_markers(answer_text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract emitted citation and claim markers from the answer text.

    The Writer prompt requires bounded markers so deterministic assurance can
    verify allowed subsets and same-run citation resolution.
    """
    import re

    citations = tuple(
        sorted(set(re.findall(r"\(([A-Za-z0-9:_-]+)\)", answer_text)))
    )
    claim_markers = tuple(
        sorted(set(re.findall(r"\[([A-Za-z0-9:_-]+)\]", answer_text)))
    )
    return citations, claim_markers


def _observed_move_text(ticker: str, move_profile: MoveProfile, session_date: str) -> str:
    target_return = getattr(move_profile, "target_return", None)
    if target_return is None:
        return f"{ticker} moved on {session_date}"
    return f"{ticker} {target_return:+.2f}% on {session_date}"


def run_v1_graph(
    *,
    run_id: str,
    temporal_identity: TemporalIdentity,
    data_runtime_identity: DataRuntimeIdentity,
    ticker: str,
    cutoff: str,
    requested_manifest_id: str,
    observation_provider: ContextProvider,
    retriever: ResearchRetriever,
    policy_config: ObservationPolicyConfig,
    research_concurrency: int,
    research_stage_timeout_seconds: float,
    persistence: PackPersistence,
    packing_policy_version: str,
    template_bytes: bytes,
    template_version: str,
    analyst_llm: Any,
    writer_llm: Any,
    capability_registry: Any,
    corrective_policy: Any | None = None,
    prompt_template: str | None = None,
    query_builder: Callable[[Any, str], str] | None = None,
    structured_provider: Any | None = None,
    run_deadline_epoch_ms: int | None = None,
    normalization_policy_version: str = "assessment-normalizer-v1",
    writer_sink: Any | None = None,
) -> V1RunResult:
    """Run the complete V1.1 semantic path (Frozen §6.1).

    RUN_ADMISSION -> QUERY_VALIDATION -> OBSERVATION_BUILD ->
    INITIAL_RESEARCH_POLICY -> INITIAL_RESEARCH_EXECUTION -> EVIDENCE_STATE ->
    COVERAGE_SUMMARY -> CONTEXT_PACK_BUILD -> EVIDENCE_ANALYST (routed
    READY/ABSTAIN/FOLLOW_UP) -> at most one corrective round -> CLAIM_PLAN_BUILD
    -> CLAIM_VALIDATION -> STREAMING_ANSWER_WRITER -> POST_STREAM_ASSURANCE ->
    FINALIZER. Logical call budget: 2 normal / 3 corrective; provider attempts
    per role <= 2 (one technical retry).
    """
    from catalyst_agents.attribution.assessment import normalize_decision
    from catalyst_agents.attribution.claims import (
        WriterFormatKind,
        WriterFormatStyleContract,
        WriterSection,
        build_claim_plan,
    )
    from catalyst_agents.nodes.writer import build_writer_input
    from catalyst_agents.attribution.claim_validation import validate_claim_plan
    from catalyst_agents.nodes.evidence_analyst import evidence_analyst
    from catalyst_agents.nodes.writer import writer_node
    from catalyst_agents.runtime.assurance.checks import run_structural_assurance
    from catalyst_agents.nodes.finalizer import thin_finalizer
    from catalyst_agents.runtime.delta_sink import InMemoryDeltaSink

    # RUN_ADMISSION / QUERY_VALIDATION
    if not run_id or not ticker or not cutoff:
        raise ValueError("run_v1_graph requires run_id, ticker, and cutoff")

    # OBSERVATION_BUILD .. CONTEXT_PACK_BUILD (reuse the M4 foundation spine)
    foundation = build_foundation_graph(
        run_id=run_id,
        round=1,
        temporal_identity=temporal_identity,
        data_runtime_identity=data_runtime_identity,
        ticker=ticker,
        cutoff=cutoff,
        requested_manifest_id=requested_manifest_id,
        observation_provider=observation_provider,
        retriever=retriever,
        policy_config=policy_config,
        research_concurrency=research_concurrency,
        research_stage_timeout_seconds=research_stage_timeout_seconds,
        persistence=persistence,
        packing_policy_version=packing_policy_version,
        template_bytes=template_bytes,
        template_version=template_version,
        analyst_boundary=None,
        structured_provider=structured_provider,
        run_deadline_epoch_ms=run_deadline_epoch_ms,
    )

    # EVIDENCE_ANALYST round 1
    analyst1 = evidence_analyst(
        foundation.state,
        llm=analyst_llm,
        persistence=persistence,
        prompt_template=prompt_template,
    )
    assessment = normalize_decision(
        analyst1["analyst_decision"],
        foundation.context_pack,
        capability_registry,
        normalization_policy_version,
        policy=corrective_policy,
        evidence_state_hash=foundation.state["evidence_state_hash"],
    )
    corrective_rounds = 0
    analyst_logical_calls = analyst1["analyst_logical_calls"]
    analyst_provider_attempts = analyst1["analyst_provider_attempts"]
    final_pack = foundation.context_pack

    # Route READY | FOLLOW_UP | ABSTAIN over the normalized assessment.
    from catalyst_agents.nodes.decision_router import route_assessment

    route = route_assessment(assessment)
    if route == "follow_up":
        corrective = run_corrective_round(
            run_id=run_id,
            round=2,
            temporal_identity=temporal_identity,
            data_runtime_identity=data_runtime_identity,
            ticker=ticker,
            cutoff=cutoff,
            requested_manifest_id=requested_manifest_id,
            scenario=foundation.classification.scenario,
            move_profile=foundation.move_profile,
            prior_evidence_state=foundation.evidence_state,
            prior_research_execution=foundation.research_execution,
            prior_assessment=assessment,
            research_policy_version=policy_config.scenario_policy_version,
            retriever=retriever,
            research_concurrency=research_concurrency,
            research_stage_timeout_seconds=research_stage_timeout_seconds,
            persistence=persistence,
            packing_policy_version=packing_policy_version,
            template_bytes=template_bytes,
            template_version=template_version,
            llm=analyst_llm,
            capability_registry=capability_registry,
            query_builder=query_builder,
            structured_provider=structured_provider,
            run_deadline_epoch_ms=run_deadline_epoch_ms,
            policy=corrective_policy,
            normalization_policy_version=normalization_policy_version,
            prompt_template=prompt_template,
            research_history=foundation.classification.tasks,
        )
        assessment = corrective.assessment
        final_pack = corrective.context_pack
        corrective_rounds = 1
        analyst_logical_calls += corrective.analyst_logical_calls
        analyst_provider_attempts += corrective.analyst_provider_attempts

    # CLAIM_PLAN_BUILD
    observed_move = _observed_move_text(
        ticker, foundation.move_profile, temporal_identity.session_date
    )
    plan = build_claim_plan(assessment, observed_move=observed_move)

    # CLAIM_VALIDATION
    validated = validate_claim_plan(
        plan,
        assessment,
        runtime_identity=_artifact_hash(data_runtime_identity),
        temporal_identity=temporal_identity,
        evidence_inventory=final_pack.evidence_inventory,
    )

    # STREAMING_ANSWER_WRITER
    if validated.status.value == "ABSTAIN":
        format_contract = WriterFormatStyleContract(
            format_kind=WriterFormatKind.FIXED_ABSTENTION,
            required_sections=(WriterSection.OBSERVED_MOVE, WriterSection.LIMITATIONS),
            style_instructions=(),
        )
    else:
        format_contract = WriterFormatStyleContract(
            format_kind=WriterFormatKind.CAUSAL,
            required_sections=(
                WriterSection.SUMMARY,
                WriterSection.CAUSAL_EXPLANATION,
                WriterSection.LIMITATIONS,
            ),
            style_instructions=("concise",),
        )
    writer_input = build_writer_input(
        validated,
        observed_move=observed_move,
        format_style_contract=format_contract,
    )
    sink = writer_sink or InMemoryDeltaSink()
    writer_result = writer_node(
        {"run_id": run_id},
        llm=writer_llm,
        writer_input=writer_input,
        sink=sink,
    )
    answer = writer_result["answer"]

    # POST_STREAM_ASSURANCE
    emitted_citations, emitted_claim_markers = extract_answer_markers(answer.text)
    assurance_artifacts = {
        "stream_complete": writer_result["stream_complete"],
        "answer_text": answer.text,
        "emitted_citations": emitted_citations,
        "emitted_claim_markers": emitted_claim_markers,
        "permitted_claim_ids": validated.permitted_claim_ids,
        "permitted_evidence_ids": validated.permitted_evidence_ids,
        "required_sections": tuple(
            section.value for section in format_contract.required_sections
        ),
        "required_limitations": validated.required_limitations,
        "emitted_status": validated.status.value,
        "emitted_attribution_type": validated.attribution_type.value,
        "validated_status": validated.status.value,
        "validated_attribution_type": validated.attribution_type.value,
        "input_hash": "i" * 64,
        "plan_hash": validated.plan_hash,
        "validated_plan_hash": validated.plan_hash,
        "evidence_state_hash": validated.evidence_state_hash,
        "runtime_identity": _artifact_hash(data_runtime_identity),
        "output_hash": answer.text_sha256,
        "cancellation_requested": bool(foundation.state.get("cancel_requested", False)),
        "timed_out": False,
        "input_tokens": 0,
        "output_tokens": 0,
        "completion_state": "completed",
    }
    assurance_checks = run_structural_assurance(run_id, assurance_artifacts)

    # FINALIZER
    terminal = thin_finalizer(
        {"run_id": run_id},
        answer_text=answer.text,
        validated_plan=validated,
        assurance_checks=assurance_checks,
        sink=sink,
    )

    state: FoundationGraphState = {
        **foundation.state,
        "stage": FoundationStage.TERMINAL,
        "round": 1 + corrective_rounds,
        "terminal_error": None,
    }
    if corrective_rounds:
        state["round"] = 2
        state["evidence_state_ref"] = f"artifact:evidence_state:{run_id}:2"
        state["context_pack_ref"] = f"artifact:context_pack:{run_id}:2"
        state["context_pack_hash"] = final_pack.context_pack_sha256

    logical_model_call_count = analyst_logical_calls + writer_result["writer_logical_calls"]
    return V1RunResult(
        state=state,
        move_profile=foundation.move_profile,
        evidence_state=corrective.evidence_state if corrective_rounds else foundation.evidence_state,
        coverage_summary=corrective.coverage_summary if corrective_rounds else foundation.coverage_summary,
        context_pack=final_pack,
        assessment=assessment,
        validated_claim_plan=validated,
        answer=answer,
        assurance_checks=tuple(assurance_checks),
        terminal_envelope=terminal["terminal_envelope"],
        logical_model_call_count=logical_model_call_count,
        analyst_logical_calls=analyst_logical_calls,
        analyst_provider_attempts=analyst_provider_attempts,
        writer_logical_calls=writer_result["writer_logical_calls"],
        writer_provider_attempts=writer_result["writer_provider_attempts"],
        corrective_rounds=corrective_rounds,
    )

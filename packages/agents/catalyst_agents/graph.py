"""V1.1 production graph - one selectable semantic path (M5).

The sealed MCJ graph was archived at M5-11 after the automated M5-10 gates
passed. The V1.1 graph is built by ``run_v1_graph`` (Frozen 6.1): observation
-> research -> EvidenceState -> CoverageSummary -> ContextPack ->
EvidenceAnalyst (routed READY/FOLLOW_UP/ABSTAIN) -> at most one corrective
round -> ClaimPlan -> ClaimValidator -> StreamingWriter -> structural
assurance -> thin Finalizer. ``build_foundation_graph`` and
``run_corrective_round`` are the M4/M5-4 building blocks.
"""

# M4-8: authoritative V1.1 foundation graph (fixture mode)
# ---------------------------------------------------------------------------
# The foundation path wires observation_build -> research_policy ->
# research_execution -> evidence_state -> coverage_summary ->
# context_pack_build -> fixture Analyst boundary. It never runs
# Miner/Critic/Judge (archived at M5-11).

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
from catalyst_agents.runtime.experiment import (
    ExperimentPolicyOverride,
    resolve_experiment_seams,
)
from catalyst_agents.runtime.control import (
    NoopRunControl,
    raise_if_control_expired,
)
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


def _critic_prefix_renderer(draft: PackedContextDraft) -> tuple[RenderMessage, ...]:
    """A1 raw critic-prefix block: one bounded 600-char prefix per included
    evidence unit (``critic_prefix_600chars_v1``). The Analyst sees a raw
    prefix block instead of the structured ContextPack render."""
    blocks: list[str] = []
    for item in draft.evidence_inventory:
        if item.evidence_id not in draft.included_evidence_ids:
            continue
        excerpt = item.excerpt_text or ""
        prefix = excerpt[:600]
        blocks.append(f"evidence:{item.evidence_id}:{prefix}")
    content = "critic_prefix=" + "|".join(blocks)
    return (RenderMessage(role="system", content=content),)


def _renderer_for_packing(
    draft: PackedContextDraft,
) -> Callable[[PackedContextDraft], tuple[RenderMessage, ...]]:
    """A1 behavioral seam: select the renderer for the draft's packing policy.

    ``evidence_context_pack_v1`` (the M6 production default) and every other
    version keep the full ContextPack render; ``critic_prefix_600chars_v1``
    renders the raw critic-prefix block. ``None`` never reaches this function
    because the draft always carries an explicit packing policy version.
    """
    if draft.packing_policy_version == "critic_prefix_600chars_v1":
        return _critic_prefix_renderer
    return _foundation_renderer


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
    observation_policy_version: str | None = None,
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
        observation_policy_version=observation_policy_version,
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
    renderer = _renderer_for_packing(draft)
    finalizer = ContextPackFinalizer(
        template_bytes=template_bytes,
        template_version=template_version,
        renderer=renderer,
        token_counter=counter,
    )
    pack = finalizer.finalize(draft)

    # 7. persistence + replay assertion (provider dispatch gate)
    messages = renderer(draft)
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
        "observation_policy_version": (
            observation_policy_version or "move_profile_v1"
        ),
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
    control: Any | None = None,
    hypothesis_policy_version: str | None = None,
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

    raise_if_control_expired(control)
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
    renderer = _renderer_for_packing(draft)
    finalizer = ContextPackFinalizer(
        template_bytes=template_bytes,
        template_version=template_version,
        renderer=renderer,
        token_counter=counter,
    )
    pack = finalizer.finalize(draft)

    messages = renderer(draft)
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

    raise_if_control_expired(control)
    analyst = evidence_analyst(
        {"run_id": run_id},
        llm=llm,
        persistence=persistence,
        prompt_template=prompt_template,
        pack_inventory_ids=pack.included_evidence_ids,
        hypothesis_policy_version=hypothesis_policy_version,
    )
    raise_if_control_expired(control)
    assessment = normalize_decision(
        analyst["analyst_decision"],
        pack,
        capability_registry,
        normalization_policy_version,
        policy=effective_policy,
        evidence_state_hash=evidence_state.state_hash,
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
    packing_policy_version: str = "evidence_context_pack_v1"
    hypothesis_policy_version: str = "bounded_competition_v1"
    observation_policy_version: str = "move_profile_v1"
    experiment_override_present: bool = False


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
    control: Any | None = None,
    experiment_override: ExperimentPolicyOverride | None = None,
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

    # A1-A5 experiment seams (M7-7): None preserves M6 behavior exactly.
    seams = resolve_experiment_seams(
        packing_policy_version=packing_policy_version,
        experiment_override=experiment_override,
    )
    effective_packing_version = str(seams["packing_policy_version"])
    effective_hypothesis_version = str(seams["hypothesis_policy_version"])
    effective_observation_version = str(seams["observation_policy_version"])
    max_corrective_rounds = int(seams["max_corrective_rounds"])
    # The Analyst semantic identity changes only under an explicit A2 arm;
    # None must reproduce the M6 identity byte-for-byte.
    analyst_hypothesis_version = (
        effective_hypothesis_version
        if experiment_override is not None
        and experiment_override.a2_hypothesis_policy_version is not None
        else None
    )

    # RUN_ADMISSION / QUERY_VALIDATION
    if not run_id or not ticker or not cutoff:
        raise ValueError("run_v1_graph requires run_id, ticker, and cutoff")

    # Cooperative boundary: observed before observation/retrieval starts.
    raise_if_control_expired(control)

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
        packing_policy_version=effective_packing_version,
        template_bytes=template_bytes,
        template_version=template_version,
        analyst_boundary=None,
        structured_provider=structured_provider,
        run_deadline_epoch_ms=run_deadline_epoch_ms,
        observation_policy_version=effective_observation_version,
    )

    # Cooperative boundary: observed after observation/retrieval and before
    # the round-one Analyst call.
    raise_if_control_expired(control)

    # EVIDENCE_ANALYST round 1
    analyst1 = evidence_analyst(
        foundation.state,
        llm=analyst_llm,
        persistence=persistence,
        prompt_template=prompt_template,
        pack_inventory_ids=foundation.context_pack.included_evidence_ids,
        hypothesis_policy_version=analyst_hypothesis_version,
    )
    raise_if_control_expired(control)
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
    # Cooperative boundary: observed before corrective dispatch.
    raise_if_control_expired(control)
    if route == "follow_up" and max_corrective_rounds >= 1:
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
            packing_policy_version=effective_packing_version,
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
            control=control,
            hypothesis_policy_version=analyst_hypothesis_version,
        )
        raise_if_control_expired(control)
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
        runtime_identity=data_runtime_identity,
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
    # Cooperative boundary: observed before the Writer starts; the Writer
    # additionally observes between stream chunks and stops accepting deltas.
    raise_if_control_expired(control)
    writer_result = writer_node(
        {"run_id": run_id},
        llm=writer_llm,
        writer_input=writer_input,
        sink=sink,
        control=control,
    )
    raise_if_control_expired(control)
    answer = writer_result["answer"]

    # POST_STREAM_ASSURANCE: the Writer surfaces real input/output hashes and
    # provider token/completion metadata; assurance recomputes and verifies
    # every identity against the authoritative WriterInput/ValidatedClaimPlan.
    emitted_citations, emitted_claim_markers = extract_answer_markers(answer.text)
    final_evidence_state = (
        corrective.evidence_state if corrective_rounds else foundation.evidence_state
    )
    assurance_artifacts = {
        "stream_complete": writer_result["stream_complete"],
        "answer_text": answer.text,
        "answer_text_sha256": answer.text_sha256,
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
        "writer_input": writer_input,
        "validated_plan": validated,
        "input_hash": writer_result["writer_input_hash"],
        "plan_hash": validated.plan_hash,
        "evidence_state_hash": validated.evidence_state_hash,
        "runtime_identity": _artifact_hash(data_runtime_identity),
        "bound_runtime_identity": _artifact_hash(
            final_evidence_state.data_runtime_identity
        ),
        "output_hash": answer.text_sha256,
        "cancellation_requested": writer_result["cancellation_requested"],
        "timed_out": writer_result["timed_out"],
        "input_tokens": writer_result["input_tokens"],
        "output_tokens": writer_result["output_tokens"],
        "completion_state": writer_result["completion_state"],
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
        packing_policy_version=effective_packing_version,
        hypothesis_policy_version=effective_hypothesis_version,
        observation_policy_version=effective_observation_version,
        experiment_override_present=experiment_override is not None,
    )


# ---------------------------------------------------------------------------
# M6 corrective: the app runtime composition invokes run_v1_graph directly;
# the legacy dependency-loader adapter below fails closed (retired surface).
# ---------------------------------------------------------------------------

class V1AppRuntimeNotWired(RuntimeError):
    """Retired adapter surface (M6 corrective).

    The production app runtime invokes ``run_v1_graph`` directly through the
    app-owned composition (``catalyst_app.runtime.composition``); this typed
    error remains only for the legacy dependency-loader adapter surface so it
    can never silently fall back to an archived graph.
    """


class V1GraphAdapter:
    """Legacy dependency-loader surface (M6 corrective).

    The production path is ``run_v1_graph`` invoked by the app runtime
    composition with real temporal identity, observation provider, persistence
    envelope, and LLM clients. This adapter is retained only for the legacy
    ``LiveRunService`` compatibility surface; ``invoke`` fails closed rather
    than executing a second graph path.
    """

    def __init__(self, *, model: Any = None, retriever: Any = None,
                 requested_manifest_id: str | None = None):
        self.model = model
        self.retriever = retriever
        self.requested_manifest_id = requested_manifest_id

    def invoke(self, state: dict, run_id: str | None = None) -> dict:
        del state, run_id
        raise V1AppRuntimeNotWired(
            "legacy V1GraphAdapter invoke is retired; production runs through "
            "run_v1_graph via the app runtime composition"
        )


def build_v1_graph_adapter(
    *,
    model: Any = None,
    retriever: Any = None,
    requested_manifest_id: str | None = None,
) -> V1GraphAdapter:
    """Return the legacy dependency-loader surface (M6 corrective)."""
    return V1GraphAdapter(
        model=model,
        retriever=retriever,
        requested_manifest_id=requested_manifest_id,
    )

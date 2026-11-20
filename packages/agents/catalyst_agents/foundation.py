"""V1.1 foundation graph: observation -> research -> EvidenceState -> CoverageSummary -> ContextPack.

Moved verbatim from graph.py (P3); graph.py re-exports these names.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Callable

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity, utc_iso_z

from catalyst_agents.attribution.context_pack import EvidenceAnalystContextPack
from catalyst_agents.attribution.context_pack_builder import (
    ContextPackBuilder,
    canonical_context_pack_json,
)
from catalyst_agents.attribution.coverage import CoverageSummary
from catalyst_agents.attribution.coverage_builder import build_coverage_summary
from catalyst_agents.attribution.evidence_state import EvidenceState
from catalyst_agents.attribution.evidence_state_builder import (
    EvidenceStateBuildContext,
    build_evidence_state,
)
from catalyst_agents.attribution.move_profile import MoveProfile
from catalyst_agents.attribution.observation_builder import ObservationBuilder
from catalyst_agents.attribution.pack_render import finalize_with_render
from catalyst_agents.attribution.provider import ContextProvider
from catalyst_agents.retrieval.execution import (
    ResearchDeadlineError,
    ResearchExecution,
    ResearchExecutor,
    ResearchRetriever,
)
from catalyst_agents.retrieval.policy import (
    InitialResearchPolicy,
    ScenarioClassification,
)
from catalyst_agents.runtime.manifest import ObservationPolicyConfig
from catalyst_agents.runtime.pack_persistence import (
    PackPersistence,
    PersistedPackPair,
    replay_asserts_pair,
)
from catalyst_agents.runtime.token_budget import UTF8ByteUpperBoundCounter
from catalyst_agents.state import FoundationGraphState


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
    on_facts_ready: Callable[[MoveProfile], None] | None = None,
    move_profile_override: MoveProfile | None = None,
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
    move_profile = move_profile_override or observation_builder.build(
        ticker=ticker,
        session_date=temporal_identity.session_date,
        cutoff=utc_iso_z(temporal_identity.cutoff_at),
        temporal_identity=temporal_identity,
        observation_policy_version=observation_policy_version,
    )
    if on_facts_ready is not None:
        on_facts_ready(move_profile)

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
    pack, messages = finalize_with_render(
        draft,
        template_bytes=template_bytes,
        template_version=template_version,
        token_counter=counter,
    )

    # 7. persistence + replay assertion (provider dispatch gate)
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
    """Return the foundation context budget in UTF-8 bytes, not tokens.

    Every value is counted by ``UTF8ByteUpperBoundCounter``: ``N`` means at
    most ``N`` UTF-8 bytes, which is a conservative upper bound on provider
    tokens.
    """
    from catalyst_agents.attribution.context_pack import ContextBudget

    # Derived against the Analyst role limit max_input_tokens=128_000. UTF-8
    # bytes are an upper bound on tokens (English runs ~4 bytes/token), so
    # 128_000 bytes stays well inside the provider limit. The evidence payload
    # was raised from 24_000 (about 12 SEC chunks) on 2026-09-29 so a filled
    # display of two tasks reaches the model without budget exclusions.
    return ContextBudget(
        model_context_limit=128_000,
        reserved_output_tokens=8_000,
        reserved_system_instruction_tokens=200,
        observation_tokens=200,
        coverage_summary_tokens=200,
        research_history_tokens=100,
        inventory_tokens=200,
        evidence_payload_tokens=64_000,
        per_news_item_max_tokens=1_200,
        per_sec_chunk_max_tokens=2_000,
        lead_only_tokens=400,
        safety_margin_tokens=1_000,
    )

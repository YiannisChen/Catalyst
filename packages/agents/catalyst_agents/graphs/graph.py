"""V1.1 production graph - one selectable semantic path (M5).

The sealed MCJ graph was archived at M5-11 after the automated M5-10 gates
passed. The V1.1 graph is built by ``run_v1_graph`` (Frozen 6.1): observation
-> research -> EvidenceState -> CoverageSummary -> ContextPack ->
EvidenceAnalyst (routed READY/FOLLOW_UP/ABSTAIN) -> ClaimPlan ->
ClaimValidator -> StreamingWriter -> structural assurance -> thin Finalizer.
``build_foundation_graph`` is the M4 building block. The corrective round was
removed; requesting it fails closed.
"""

# M4-8: authoritative V1.1 foundation graph (fixture mode)
# ---------------------------------------------------------------------------
# The foundation path wires observation_build -> research_policy ->
# research_execution -> evidence_state -> coverage_summary ->
# context_pack_build -> fixture Analyst boundary. It never runs
# Miner/Critic/Judge (archived at M5-11).

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Literal

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity, utc_iso_z
from catalyst_data.storage.runtime_paths import runtime_store_path

from catalyst_agents.attribution.context_pack import EvidenceAnalystContextPack
from catalyst_agents.attribution.coverage import CoverageSummary
from catalyst_agents.attribution.evidence_state import EvidenceState
from catalyst_agents.attribution.move_profile import MoveProfile
from catalyst_agents.attribution.provider import ContextProvider
from catalyst_agents.attribution.session_facts import (  # noqa: F401  (moved; re-exported)
    _COMOVE_REFERENCES,
    _observed_move_text,
    _session_facts,
)
from catalyst_agents.foundation import (  # noqa: F401  (moved; re-exported)
    FoundationRunResult,
    _artifact_hash,
    _foundation_budget,
    build_foundation_graph,
)
from catalyst_agents.graphs.facts_stage import emit_facts
from catalyst_agents.graphs.research_context import (
    FoundationGraphInputs,
    FoundationRetrievalStageInput,
    run_foundation_retrieval_stage,
)
from catalyst_agents.graphs.web_source_decision import web_source_decision
from catalyst_agents.observability.aggregation import build_retrieval_diagnostics
from catalyst_agents.observability.diagnostics import (
    CorrectiveRoundObservation,
    RetrievalDiagnostics,
)
from catalyst_agents.retrieval.execution import ResearchRetriever
from catalyst_agents.runtime.control import raise_if_control_expired
from catalyst_agents.runtime.earnings_timing import (
    earnings_timings_for_context_pack,
    with_earnings_timing_scope,
)
from catalyst_agents.runtime.experiment import (
    ExperimentPolicyOverride,
    resolve_experiment_seams,
)
from catalyst_agents.runtime.manifest import ObservationPolicyConfig
from catalyst_agents.runtime.pack_persistence import PackPersistence
from catalyst_agents.state import FoundationGraphState

# ---------------------------------------------------------------------------
# M5-4: round-two EvidenceAnalyst over cumulative evidence
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# M5-9: V1.1 production graph over the normalized assessment
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V1RunResult:
    """Common runtime surface for full and tier-only V1.1 results.

    Kept as the base class for compatibility with callers importing
    ``V1RunResult`` and using it for runtime checks.
    """

    state: FoundationGraphState | None
    move_profile: MoveProfile
    evidence_state: EvidenceState | None
    coverage_summary: CoverageSummary | None
    context_pack: EvidenceAnalystContextPack | None
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
    # Observed trajectory facts retained at their source (M7 Phase A). These
    # are recorded facts, not defaults: ``corrective_round_observations`` has
    # one entry per round actually executed.
    initial_task_ids: tuple[str, ...] = ()
    initial_task_fingerprints: tuple[str, ...] = ()
    corrective_round_observations: tuple[CorrectiveRoundObservation, ...] = ()
    # The runtime records its deterministic terminal status. It has no
    # machine-comparable refusal taxonomy, so refusal comparison stays
    # non-scorable and is never claimed as available.
    refusal_reason: str | None = None
    refusal_reason_available: bool = False
    # Observed retrieval facts retained at their source. ``None`` means the
    # run never reached retrieval, so retrieval metrics are non-scorable.
    retrieval_diagnostics: RetrievalDiagnostics | None = None
    # Genuinely reported provider token usage; ``None`` means the provider
    # reported none (never a claimed zero).
    reported_input_tokens: int | None = None
    reported_output_tokens: int | None = None
    packing_policy_version: str = "evidence_context_pack_v1"
    hypothesis_policy_version: str = "bounded_competition_v1"
    observation_policy_version: str = "move_profile_v1"
    experiment_override_present: bool = False
    # Same-event verifier calls (role ``primary_verifier``), counted in
    # ``logical_model_call_count`` and the reported token totals.
    verifier_logical_calls: int = 0
    verifier_provider_attempts: int = 0
    # What each stage decided and why (observability/decision_trace.py).
    decision_trace: dict | None = None
    answer_snapshot: dict | None = None
    tier_decision: Any | None = None
    tier_only: bool = False


@dataclass(frozen=True)
class V1FullRunResult(V1RunResult):
    """Run that completed retrieval and produced all evidence artifacts."""

    state: FoundationGraphState
    evidence_state: EvidenceState
    coverage_summary: CoverageSummary
    context_pack: EvidenceAnalystContextPack
    tier_only: Literal[False] = False


@dataclass(frozen=True)
class V1TierOnlyRunResult(V1RunResult):
    """Fast tier answer that intentionally skipped retrieval and artifacts."""

    state: None
    evidence_state: None
    coverage_summary: None
    context_pack: None
    tier_only: Literal[True] = True


def extract_answer_markers(answer_text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract emitted citation and claim markers from the answer text.

    The Writer prompt requires bounded markers so deterministic assurance can
    verify allowed subsets and same-run citation resolution. Production Writer
    lines are ``[ROLE] (claim_id: ID) ... [citations: e1,e2]``; ClaimRole
    brackets are labels, not claim IDs. The legacy fixture form
    ``[claim:1] (e1)`` remains accepted.
    """
    import re

    from catalyst_agents.attribution.claims import ClaimRole

    role_markers = {role.value for role in ClaimRole}
    claim_markers: set[str] = set(
        re.findall(r"\(claim_id:\s*([A-Za-z0-9:_-]+)\)", answer_text)
    )
    for raw in re.findall(r"\[([A-Za-z0-9:_-]+)\]", answer_text):
        if raw in role_markers or raw.lower() == "citations":
            continue
        claim_markers.add(raw)
    citations: set[str] = set()
    for block in re.findall(r"\[citations:\s*([^\]]*)\]", answer_text):
        for part in block.split(","):
            token = part.strip()
            if token:
                citations.add(token)
    citations.update(re.findall(r"\(([A-Za-z0-9:_-]+)\)", answer_text))
    return tuple(sorted(citations)), tuple(sorted(claim_markers))


def _aggregate_provider_usage(
    usages: tuple[tuple[int | None, int | None], ...],
) -> tuple[int | None, int | None]:
    """Sum whole-run usage only when every dispatched attempt is complete."""
    if not usages or any(
        input_tokens is None or output_tokens is None
        for input_tokens, output_tokens in usages
    ):
        return None, None
    return (
        sum(input_tokens for input_tokens, _ in usages),
        sum(output_tokens for _, output_tokens in usages),
    )


def _desk_context_payload(decomposition: Any | None) -> dict[str, Any] | None:
    if decomposition is None:
        return None
    best = getattr(decomposition, "best", None)
    return {
        "version": getattr(decomposition, "version", None),
        "verdict": getattr(decomposition, "verdict", None),
        "best": ({key: getattr(best, key, None) for key in (
            "kind", "symbol", "beta", "reference_return_pct", "expected_pct",
            "residual_pct", "explained_share",
        )} if best is not None else None),
        "sentence": decomposition.sentence() if callable(getattr(decomposition, "sentence", None)) else "",
    }


@with_earnings_timing_scope
def _issuer_name(ticker: str) -> str:
    """'Micron Technology' from the universe name tokens; the ticker when unknown."""
    from catalyst_data.retrieval.query_policy import ISSUER_NAMES

    words = [w for w in ISSUER_NAMES.get(ticker, ()) if w.casefold() != ticker.casefold()]
    return " ".join(w.capitalize() for w in words) or ticker


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
    capability_registry: Any,
    # Unused since Q1c (the answer is code-rendered); kept for callers.
    writer_llm: Any | None = None,
    corrective_policy: Any | None = None,
    prompt_template: str | None = None,
    query_builder: Callable[[Any, str], str] | None = None,
    structured_provider: Any | None = None,
    run_deadline_epoch_ms: int | None = None,
    normalization_policy_version: str = "assessment-normalizer-v1",
    writer_sink: Any | None = None,
    control: Any | None = None,
    experiment_override: ExperimentPolicyOverride | None = None,
    provider_budget: Any | None = None,
    provider_identity: str | None = None,
    analyst_model_identity: str | None = None,
    writer_model_identity: str | None = None,
    attribution_policy: Any | None = None,
    stage2_temporal_identity: TemporalIdentity | None = None,
    stage2_retriever: ResearchRetriever | None = None,
    desk_context_fn: Callable[..., Any] | None = None,
    investigator_deps: Any | None = None,
    on_step: Callable[[str, dict[str, Any]], None] | None = None,
    force_full: bool = False,
    web_source_title: str | None = None,
    web_source_company_name: str | None = None,
) -> V1FullRunResult | V1TierOnlyRunResult:
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
    from catalyst_agents.attribution.claim_validation import validate_claim_plan
    from catalyst_agents.attribution.claims import (
        WriterFormatKind,
        WriterFormatStyleContract,
        WriterSection,
        build_claim_plan,
    )
    from catalyst_agents.attribution.tiers import decide_tier
    from catalyst_agents.foundation import ObservationBuilder
    from catalyst_agents.nodes.evidence_analyst import evidence_analyst
    from catalyst_agents.nodes.finalizer import thin_finalizer
    from catalyst_agents.nodes.writer import build_writer_input, writer_node
    from catalyst_agents.runtime.assurance.checks import run_structural_assurance
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

    # Side record of what each stage decided (persisted as the decision_trace artifact).
    from catalyst_agents.observability.decision_trace import DecisionTrace

    def emit(event_type: str, payload: dict[str, Any]) -> None:
        if on_step is not None:
            on_step(event_type, payload)

    move_profile = ObservationBuilder(
        provider=observation_provider, policy=policy_config
    ).build(
        ticker=ticker,
        session_date=temporal_identity.session_date,
        cutoff=cutoff,
        temporal_identity=temporal_identity,
    )
    if temporal_identity.session_open_at >= temporal_identity.session_close_at:
        move_profile = move_profile.model_copy(update={
            "coverage_flags": tuple(sorted(set(move_profile.coverage_flags) | {"not_a_trading_session"}))
        })
    filings_in_window = 0
    count_filings = getattr(observation_provider, "issuer_filings_in_window", None)
    if callable(count_filings):
        filings_in_window = count_filings(
            ticker=ticker,
            information_window_start_at=utc_iso_z(temporal_identity.information_window_start_at),
            cutoff_at=utc_iso_z(temporal_identity.cutoff_at),
        )
    emit_facts(
        move_profile,
        ticker=ticker,
        temporal_identity=temporal_identity,
        on_step=on_step,
    )
    tier_decision = decide_tier(
        move_profile, filings_in_window=filings_in_window, policy=policy_config
    )
    emit("tier.decided", tier_decision.to_dict())

    desk_cascade = bool(attribution_policy is not None
                        and attribution_policy.evidence_window_policy == "desk_cascade_v1")
    desk_context = None
    if desk_cascade and desk_context_fn is not None:
        desk_context = desk_context_fn(
            ticker, temporal_identity.session_date, move_profile.sector_ticker,
            move_profile.commodity_ticker,
        )

    if (tier_decision.tier in ("T0", "T1")
            or (tier_decision.tier == "T2" and attribution_policy is not None
                and attribution_policy.evidence_window_policy in ("event_time_v1", "event_time_v2"))) and not force_full:
        from catalyst_agents.attribution.answer_snapshot import build_facts_snapshot
        from catalyst_agents.runtime.delta_sink import WRITER_ASSEMBLY_ORIGIN

        facts_text = _session_facts(
            move_profile, SimpleNamespace(evidence_inventory=()), temporal_identity
        )
        projection = build_facts_snapshot(
            ticker=ticker,
            move_profile=move_profile,
            temporal_identity=temporal_identity,
            facts_text=facts_text,
        )
        answer_text = (
            "OBSERVED_MOVE\n"
            f"{_observed_move_text(ticker, move_profile, temporal_identity.session_date)}"
            f"{'. ' + facts_text if facts_text else ''}\n\n"
            f"TIER\n{tier_decision.reason_text}"
        )
        answer_id = f"answer:stream:{run_id}"
        stream_id = f"tier:{run_id}"
        completed_at = datetime.now(timezone.utc).isoformat()
        sink = writer_sink or InMemoryDeltaSink()
        sink.commit_delta(
            stream_id=stream_id,
            ordinal=0,
            text=answer_text,
            cumulative_chars=len(answer_text),
            cumulative_bytes=len(answer_text.encode("utf-8")),
            origin=WRITER_ASSEMBLY_ORIGIN,
        )
        text_hash = hashlib.sha256(answer_text.encode("utf-8")).hexdigest()
        sink.commit_answer(
            answer_id=answer_id,
            stream_id=stream_id,
            text=answer_text,
            text_sha256=text_hash,
            completed_at=completed_at,
        )
        sink.commit_assured_envelope(
            answer_id=answer_id,
            final_status="ABSTAIN",
            attribution_type="EVIDENCE_BACKED_CAUSAL",
            checks=("tier_only_code_render",),
            completed_at=completed_at,
        )
        snapshot = {
            "schema_version": "answer_snapshot_v1",
            "run_id": run_id,
            "ticker": ticker,
            "session_date": temporal_identity.session_date,
            "outcome": "TIER_ONLY",
            "status": "ABSTAIN",
            **projection,
            "tier": tier_decision.tier,
            "tier_reason": tier_decision.reason_text,
            "governing_ref": (
                tier_decision.governing_ref
            ),
            "key_sentence": None,
            "also_sentence": None,
            "sources": [],
            "limitations": [],
        }
        if desk_cascade:
            snapshot["desk_context"] = _desk_context_payload(desk_context)
            snapshot["basis"] = None
            snapshot["cascade_stage"] = None
            snapshot["last_attempted_stage"] = None
            snapshot["published_stage"] = None
        if (attribution_policy is not None
                and attribution_policy.evidence_window_policy in ("event_time_v1", "event_time_v2")):
            snapshot["grade"] = (
                "ORDINARY" if tier_decision.tier == "T1"
                else "MOVED_WITH_GROUP" if tier_decision.tier == "T2"
                else "NO_CLEAR_CAUSE"
            )
            snapshot["grade_label"] = (
                "Ordinary close." if tier_decision.tier == "T1"
                else "Moved with the governing reference." if tier_decision.tier == "T2"
                else "No clear cause was established."
            )
        emit("answer.ready", {"answer_snapshot": snapshot})
        plan = SimpleNamespace(
            status=SimpleNamespace(value="ABSTAIN"),
            attribution_type=SimpleNamespace(value="EVIDENCE_BACKED_CAUSAL"),
            claims=(),
        )
        check = SimpleNamespace(check_name="tier_only_code_render")
        return V1TierOnlyRunResult(
            state=None,
            move_profile=move_profile,
            evidence_state=None,
            coverage_summary=None,
            context_pack=None,
            assessment=None,
            validated_claim_plan=plan,
            answer=sink.answer(),
            assurance_checks=(check,),
            terminal_envelope=sink.assured_envelope() or {},
            logical_model_call_count=0,
            analyst_logical_calls=0,
            analyst_provider_attempts=0,
            writer_logical_calls=0,
            writer_provider_attempts=0,
            corrective_rounds=0,
            retrieval_diagnostics=None,
            answer_snapshot=snapshot,
            tier_decision=tier_decision,
            tier_only=True,
        )

    trace = DecisionTrace(run_id)
    foundation_stage = run_foundation_retrieval_stage(FoundationRetrievalStageInput(
        foundation_inputs=FoundationGraphInputs(
            run_id=run_id, round=1, temporal_identity=temporal_identity,
            data_runtime_identity=data_runtime_identity, ticker=ticker, cutoff=cutoff,
            requested_manifest_id=requested_manifest_id, observation_provider=observation_provider,
            retriever=retriever, policy_config=policy_config, research_concurrency=research_concurrency,
            research_stage_timeout_seconds=research_stage_timeout_seconds, persistence=persistence,
            packing_policy_version=effective_packing_version, template_bytes=template_bytes,
            template_version=template_version, structured_provider=structured_provider,
            run_deadline_epoch_ms=run_deadline_epoch_ms,
            observation_policy_version=effective_observation_version, move_profile_override=move_profile,
        ),
        foundation_builder=build_foundation_graph, trace=trace, emit=emit,
        stage_name="research_and_context_pack", round=1, temporal_identity=temporal_identity,
        control=control,
        cascade_retrieval_stage=(1 if attribution_policy is not None
                                 and attribution_policy.evidence_window_policy == "desk_cascade_v1" else None),
    ))
    foundation = foundation_stage.foundation

    # Verifier on / corrective round off by default; the manifest records the
    # policy a run used (attribution/policy.py).
    if attribution_policy is None:
        from catalyst_agents.attribution.policy import (
            resolve_runtime_attribution_policy,
        )

        attribution_policy = resolve_runtime_attribution_policy()
    from catalyst_agents.engine.rules import V2Rules
    v2_rules = V2Rules.parse(attribution_policy.v2_rules)

    from catalyst_agents.attribution.analyst import (
        AnalystDecision,
        EventTimeAnalystDecision,
    )
    analyst_schema = (
        EventTimeAnalystDecision
        if attribution_policy.evidence_window_policy in ("event_time_v1", "event_time_v2")
        else AnalystDecision
    )

    # Same-event verifier: one short budgeted call on a published PRIMARY.
    primary_verifier = None
    if attribution_policy.verifier_enabled:
        from catalyst_agents.attribution.verifier import make_verifier

        primary_verifier = make_verifier(
            analyst_llm,
            budget=provider_budget,
            provider=provider_identity,
            model_id=analyst_model_identity,
            deadline_epoch_ms=run_deadline_epoch_ms,
            evidence_window_policy="pre_close_v1" if desk_cascade else attribution_policy.evidence_window_policy,
        )

    cascade = None
    if desk_cascade:
        # The desk cascade is a LangGraph graph (graphs/cascade.py); with every v2 rule off it reproduces A′.
        from catalyst_agents.graphs.cascade import CascadeInputs, run_desk_cascade

        cascade = run_desk_cascade(CascadeInputs(
            run_id=run_id, ticker=ticker, temporal_identity=temporal_identity,
            stage2_temporal_identity=stage2_temporal_identity, data_runtime_identity=data_runtime_identity,
            cutoff=cutoff, requested_manifest_id=requested_manifest_id, observation_provider=observation_provider,
            retriever=retriever, stage2_retriever=stage2_retriever, policy_config=policy_config,
            research_concurrency=research_concurrency, research_stage_timeout_seconds=research_stage_timeout_seconds,
            persistence=persistence, packing_policy_version=effective_packing_version, template_bytes=template_bytes,
            template_version=template_version, structured_provider=structured_provider,
            run_deadline_epoch_ms=run_deadline_epoch_ms, observation_policy_version=effective_observation_version,
            move_profile=move_profile, foundation=foundation, analyst_llm=analyst_llm, provider_budget=provider_budget,
            provider_identity=provider_identity, analyst_model_identity=analyst_model_identity,
            capability_registry=capability_registry, normalization_policy_version=normalization_policy_version,
            corrective_policy=corrective_policy, attribution_policy=attribution_policy,
            investigator_deps=investigator_deps, desk_context=desk_context, web_source_title=web_source_title,
            web_source_company_name=web_source_company_name, tier=tier_decision.tier, trace=trace, emit=emit,
            control=control, rules=v2_rules,
            foundation_builder=build_foundation_graph,
            market_db=runtime_store_path("market_db"),
        ))
        if v2_rules.any:
            trace.payload["v2_rules"] = v2_rules.to_dict()
        assessment, final_pack, foundation = cascade.assessment, cascade.final_pack, cascade.foundation
        temporal_identity, primary_verifier = cascade.temporal_identity, cascade.primary_verifier
        stage1_verifier_calls, stage2_verifier_calls = cascade.stage1_verifier_calls, cascade.stage2_verifier_calls
        analyst_logical_calls = cascade.analyst_logical_calls
        analyst_provider_attempts = cascade.analyst_provider_attempts
        analyst_attempt_usages = cascade.analyst_attempt_usages
        final_retriever = cascade.final_retriever
        web_source_filter_removed_all = cascade.web_source_filter_removed_all
        corrective_rounds = 0
    else:
        # EVIDENCE_ANALYST round 1
        stage = trace.begin("evidence_analyst", round=1)
        if desk_cascade:
            from catalyst_agents.nodes.desk_decision import desk_decide
            desk_result1 = desk_decide(
                foundation.context_pack, observation=foundation.context_pack.observation, temporal_identity=temporal_identity,
                quote_policy="pre_close_v1", llm=analyst_llm, provider_budget=provider_budget,
                provider_identity=provider_identity, model_identity=analyst_model_identity,
                run_deadline_epoch_ms=run_deadline_epoch_ms, company_name=web_source_company_name,
                market_facts={"session_close": temporal_identity.session_close_at},
            )
            persist_artifact = getattr(persistence, "persist_artifact", None)
            if callable(persist_artifact):
                persist_artifact(run_id=run_id, artifact_id=f"desk_decision:{run_id}:1",
                                 artifact_type="desk_decision",
                                 payload={**desk_result1.artifact, "cascade_stage": 1})
            analyst1 = {
                "analyst_decision": desk_result1.analyst_decision,
                "analyst_logical_calls": desk_result1.logical_calls,
                "analyst_provider_attempts": desk_result1.provider_attempts,
                "analyst_attempts": desk_result1.attempts,
                "analyst_attempt_usages": desk_result1.attempt_usages,
            }
        else:
            analyst1 = evidence_analyst(
                foundation.state, llm=analyst_llm, persistence=persistence,
                prompt_template=prompt_template, schema=analyst_schema,
                pack_inventory_ids=foundation.context_pack.included_evidence_ids,
                hypothesis_policy_version=analyst_hypothesis_version,
                provider_budget=provider_budget, provider_identity=provider_identity,
                model_identity=analyst_model_identity,
            )
        trace.end(stage)
        raw_decision = analyst1["analyst_decision"]
        web_source_filter_removed_all = False
        if web_source_title is not None:
            proposed_hypotheses = tuple(getattr(raw_decision, "candidate_hypotheses", ()))
            raw_decision = web_source_decision(
                raw_decision,
                title=web_source_title,
                ticker=ticker,
                company_name=web_source_company_name or ticker,
            )
            web_source_filter_removed_all = bool(proposed_hypotheses) and not raw_decision.candidate_hypotheses
        for decision in getattr(raw_decision, "evidence_decisions", ()):
            emit("analyst.decision", {
                "kind": "evidence",
                "evidence_id": decision.evidence_id,
                "disposition": str(getattr(decision.disposition, "value", decision.disposition)),
                "reason_code": decision.reason_code,
                "note": decision.note or None,
            })
        for hypothesis in getattr(raw_decision, "candidate_hypotheses", ()):
            emit("analyst.decision", {
                "kind": "candidate_cause",
                "ref": hypothesis.hypothesis_ref,
                "cause_type": str(getattr(hypothesis.cause_type, "value", hypothesis.cause_type)),
                "proposed_role": str(getattr(getattr(hypothesis, "proposed_role", None), "value", getattr(hypothesis, "proposed_role", None))),
            })
        emit("analyst.done", {
            "duration_ms": max(0, int(stage.get("duration_ms", 0))),
            "evidence_decisions": [
                {
                    "evidence_id": decision.evidence_id,
                    "disposition": str(getattr(decision.disposition, "value", decision.disposition)),
                    "reason_code": decision.reason_code,
                    "note": decision.note or None,
                }
                for decision in getattr(raw_decision, "evidence_decisions", ())
            ],
            "hypotheses": [
                {
                    "ref": hypothesis.hypothesis_ref,
                    "cause_type": str(getattr(hypothesis.cause_type, "value", hypothesis.cause_type)),
                    "proposed_role": str(getattr(getattr(hypothesis, "proposed_role", None), "value", getattr(hypothesis, "proposed_role", None))),
                }
                for hypothesis in getattr(raw_decision, "candidate_hypotheses", ())
            ],
            "research_decision": str(getattr(getattr(raw_decision, "research_decision", None), "value", getattr(raw_decision, "research_decision", "UNKNOWN"))),
        })
        raise_if_control_expired(control)
        stage = trace.begin("checks_and_verifier", round=1)
        assessment = normalize_decision(
            raw_decision,
            foundation.context_pack,
            capability_registry,
            normalization_policy_version,
            policy=corrective_policy,
            evidence_state_hash=foundation.state["evidence_state_hash"],
            quote_rules=True,
            evidence_window_policy="pre_close_v1" if desk_cascade else attribution_policy.evidence_window_policy,
            filing_rule=v2_rules.filing,
            release_timings=earnings_timings_for_context_pack(foundation.context_pack),
            primary_verifier=primary_verifier,
            trace=trace,
        )
        trace.end(stage)
        trace.outcome(1, assessment)
        if desk_cascade:
            trace.payload["rounds"][-1]["cascade_stage"] = 1
            if desk_result1 is not None:
                trace.payload["rounds"][-1]["desk_decision"] = {**desk_result1.artifact, "cascade_stage": 1}
        stage1_verifier_calls = dict(getattr(primary_verifier, "calls", None) or {})
        stage2_verifier_calls: dict[str, Any] = {}
        round_one_trace = trace.payload["rounds"][-1]
        checks = round_one_trace.get("code_checks", {})
        emit("checks.done", {
            "quotes": [
                {key: item[key] for key in ("hypothesis_ref", "evidence_id", "rule", "kept")}
                for item in checks.get("quotes", ())
            ],
            "gates": checks.get("gates", ()),
            "duration_ms": max(0, int(stage.get("duration_ms", 0))),
        })
        emit("gates.done", {
            "gates": checks.get("gates", ()),
            "duration_ms": max(0, int(stage.get("duration_ms", 0))),
        })
        verifier = round_one_trace.get("model_reported", {}).get("verifier", {})
        emit("verifier.done", {
            "called": bool(verifier.get("called", False)),
            "verdict": verifier.get("verdict"),
            "category": verifier.get("category"),
            "reason": verifier.get("reason"),
            "skipped_because": verifier.get("skipped_because"),
        })
        corrective_rounds = 0
        analyst_logical_calls = analyst1["analyst_logical_calls"]
        analyst_provider_attempts = analyst1["analyst_provider_attempts"]
        analyst_attempt_usages = tuple(analyst1["analyst_attempt_usages"])
        final_pack = foundation.context_pack
        final_retriever = retriever

        if (desk_cascade and getattr(assessment.final_status, "value", assessment.final_status) == "ABSTAIN"
                and attribution_policy.web_evidence == "discovery_v2"):
            if stage2_temporal_identity is None or stage2_retriever is None:
                raise ValueError("desk_cascade_v1 discovery_v2 requires stage-two temporal and retrieval inputs")
            if attribution_policy.investigator_mode is not None:
                raise ValueError("unknown investigator mode")
            stage = trace.begin("cascade_stage_2_research_and_context_pack", round=2, cascade_stage=2)
            foundation2 = build_foundation_graph(
                run_id=run_id, round=2, temporal_identity=stage2_temporal_identity,
                data_runtime_identity=data_runtime_identity, ticker=ticker, cutoff=cutoff,
                requested_manifest_id=requested_manifest_id, observation_provider=observation_provider,
                retriever=stage2_retriever, policy_config=policy_config,
                research_concurrency=research_concurrency,
                research_stage_timeout_seconds=research_stage_timeout_seconds, persistence=persistence,
                packing_policy_version=effective_packing_version, template_bytes=template_bytes,
                template_version=template_version, analyst_boundary=None,
                structured_provider=structured_provider, run_deadline_epoch_ms=run_deadline_epoch_ms,
                observation_policy_version=effective_observation_version, on_facts_ready=None,
                move_profile_override=move_profile,
            )
            trace.end(stage)
            trace.retrieval(
                evidence_state=foundation2.evidence_state,
                context_pack=foundation2.context_pack,
                research_tasks=getattr(getattr(foundation2, "research_execution", None), "tasks", ()),
            )
            trace.payload.setdefault("cascade_retrievals", []).append(
                {"cascade_stage": 2, **trace.payload["retrieval"]}
            )
            stage2_inventory = tuple(foundation2.context_pack.evidence_inventory)
            stage2_ids = set(foundation2.context_pack.included_evidence_ids)
            stage2_leads = {item.evidence_id for item in foundation2.context_pack.lead_only_evidence}
            emit("retrieval.done", {
                "inventory_count": len(stage2_inventory), "included_in_pack": len(stage2_ids),
                "lead_only": len(stage2_leads),
                "items": [{"evidence_id": item.evidence_id, "title": None,
                           "publisher": item.publisher or str(getattr(item.source_class, "value", item.source_class)),
                           "published_at": item.eligible_at.isoformat() if item.eligible_at else None,
                           "url": item.canonical_url,
                           "phase": "PRE_OPEN" if item.eligible_at <= stage2_temporal_identity.session_open_at else "SESSION",
                           "content_state": str(getattr(item.content_state, "value", item.content_state))}
                          for item in stage2_inventory[:40]],
                "duration_ms": max(0, int(stage.get("duration_ms", 0))),
            })
            raise_if_control_expired(control)
            from catalyst_agents.nodes.desk_decision import desk_decide
            stage = trace.begin("evidence_analyst", round=2, cascade_stage=2)
            desk_result2 = desk_decide(
                foundation2.context_pack, observation=foundation2.context_pack.observation, temporal_identity=stage2_temporal_identity,
                quote_policy="event_time_p1", llm=analyst_llm, provider_budget=provider_budget,
                provider_identity=provider_identity, model_identity=analyst_model_identity,
                run_deadline_epoch_ms=run_deadline_epoch_ms, company_name=web_source_company_name,
                market_facts={"session_close": stage2_temporal_identity.session_close_at},
            )
            trace.end(stage)
            if callable(persist_artifact):
                persist_artifact(run_id=run_id, artifact_id=f"desk_decision:{run_id}:2",
                                 artifact_type="desk_decision",
                                 payload={**desk_result2.artifact, "cascade_stage": 2})
            raw_decision2 = desk_result2.analyst_decision
            for d in raw_decision2.evidence_decisions:
                emit("analyst.decision", {"kind": "evidence", "evidence_id": d.evidence_id,
                                          "disposition": d.disposition.value, "reason_code": d.reason_code,
                                          "note": d.note or None})
            for h in raw_decision2.candidate_hypotheses:
                emit("analyst.decision", {"kind": "candidate_cause", "ref": h.hypothesis_ref,
                                          "cause_type": h.cause_type.value, "proposed_role": h.proposed_role.value})
            emit("analyst.done", {
                "duration_ms": max(0, int(stage.get("duration_ms", 0))),
                "evidence_decisions": [{"evidence_id": d.evidence_id, "disposition": d.disposition.value,
                                        "reason_code": d.reason_code, "note": d.note or None}
                                       for d in raw_decision2.evidence_decisions],
                "hypotheses": [{"ref": h.hypothesis_ref, "cause_type": h.cause_type.value,
                                "proposed_role": h.proposed_role.value} for h in raw_decision2.candidate_hypotheses],
                "research_decision": raw_decision2.research_decision.value,
            })
            stage = trace.begin("checks_and_verifier", round=2, cascade_stage=2)
            from catalyst_agents.attribution.verifier import make_verifier
            primary_verifier = make_verifier(
                analyst_llm, budget=provider_budget, provider=provider_identity,
                model_id=analyst_model_identity, deadline_epoch_ms=run_deadline_epoch_ms,
                evidence_window_policy="event_time_p1",
            ) if attribution_policy.verifier_enabled else None
            assessment = normalize_decision(
                raw_decision2, foundation2.context_pack, capability_registry,
                normalization_policy_version, policy=corrective_policy,
                evidence_state_hash=foundation2.state["evidence_state_hash"], quote_rules=True,
                evidence_window_policy="event_time_p1", filing_rule=v2_rules.filing,
                release_timings=earnings_timings_for_context_pack(foundation2.context_pack),
                primary_verifier=primary_verifier, trace=trace,
            )
            trace.end(stage)
            trace.outcome(2, assessment)
            trace.payload["rounds"][-1]["cascade_stage"] = 2
            trace.payload["rounds"][-1]["desk_decision"] = {**desk_result2.artifact, "cascade_stage": 2}
            stage2_verifier_calls = dict(getattr(primary_verifier, "calls", None) or {})
            stage2_trace = trace.payload["rounds"][-1]
            stage2_checks = stage2_trace.get("code_checks", {})
            emit("checks.done", {"quotes": [{key: item[key] for key in ("hypothesis_ref", "evidence_id", "rule", "kept")}
                                             for item in stage2_checks.get("quotes", ())],
                                  "gates": stage2_checks.get("gates", ()),
                                  "duration_ms": max(0, int(stage.get("duration_ms", 0)))})
            emit("gates.done", {"gates": stage2_checks.get("gates", ()),
                                "duration_ms": max(0, int(stage.get("duration_ms", 0)))})
            verifier2 = stage2_trace.get("model_reported", {}).get("verifier", {})
            emit("verifier.done", {"called": bool(verifier2.get("called", False)),
                                    "verdict": verifier2.get("verdict"), "category": verifier2.get("category"),
                                    "reason": verifier2.get("reason"), "skipped_because": verifier2.get("skipped_because")})
            analyst_logical_calls += desk_result2.logical_calls
            analyst_provider_attempts += desk_result2.provider_attempts
            analyst_attempt_usages += desk_result2.attempt_usages
            foundation = foundation2
            final_pack = foundation2.context_pack
            temporal_identity = stage2_temporal_identity
            final_retriever = stage2_retriever

    # Route READY | FOLLOW_UP | ABSTAIN over the normalized assessment.
    from catalyst_agents.nodes.decision_router import route_assessment

    route = route_assessment(assessment)
    # Cooperative boundary: observed before corrective dispatch.
    raise_if_control_expired(control)
    corrective_round_observations: tuple[CorrectiveRoundObservation, ...] = ()
    # The corrective round is off by default: on two fresh held-out sets (v3,
    # v4; plan doc §18–§19) skipping it was equal or better on every metric,
    # ~30% cheaper and removed the 60 s failures.
    if (not desk_cascade and route == "follow_up" and max_corrective_rounds >= 1
            and attribution_policy.corrective_enabled):
        raise ValueError("corrective round was removed")

    if attribution_policy.evidence_window_policy == "event_time_v1":
        from catalyst_agents.attribution.analyst import AttributionStatus
        from catalyst_agents.attribution.answer_snapshot import (
            grade_event_time_hypothesis,
        )

        grade_round = trace.payload["rounds"][-1]
        grade_reported = grade_round.get("model_reported", {})
        grade_checks = grade_round.get("code_checks", {})
        event_time_grade = grade_event_time_hypothesis(
            tier=tier_decision.tier,
            status=str(getattr(assessment.final_status, "value", assessment.final_status)),
            hypotheses=grade_reported.get("analyst", {}).get("hypotheses", ()),
            verifier=grade_reported.get("verifier", {}),
            checks=grade_checks.get("quotes", ()),
            gates=grade_checks.get("gates", ()),
            ceiling_codes=grade_checks.get("outcome", {}).get("ceiling_codes", ()),
        )
        trace.grade_decision(assessment.round, event_time_grade)
        if (event_time_grade["grade"] == "REPORTED"
                and assessment.final_status is AttributionStatus.ABSTAIN
                and any(code.startswith("REPORTED_CEILING_EXCEPTION:")
                        for code in event_time_grade.get("reason_codes", ()))):
            assessment = assessment.model_copy(update={
                "status_ceiling": AttributionStatus.PARTIAL,
                "final_status": AttributionStatus.PARTIAL,
            })
            grade_round["code_checks"].setdefault("outcome", {}).update({
                "status_ceiling": "PARTIAL",
                "final_status": "PARTIAL",
                "reported_ceiling_exception": True,
            })
        elif event_time_grade["grade"] == "NO_CLEAR_CAUSE" and assessment.final_status is not AttributionStatus.ABSTAIN:
            assessment = assessment.model_copy(update={
                "status_ceiling": AttributionStatus.ABSTAIN,
                "final_status": AttributionStatus.ABSTAIN,
            })
            grade_round["code_checks"].setdefault("outcome", {}).update({
                "status_ceiling": "ABSTAIN",
                "final_status": "ABSTAIN",
                "event_time_grade_ceiling": "NO_CLEAR_CAUSE",
            })
    elif attribution_policy.evidence_window_policy == "event_time_v2":
        from catalyst_agents.attribution.answer_snapshot import (
            grade_event_time_v2_hypothesis,
        )

        grade_round = trace.payload["rounds"][-1]
        grade_reported = grade_round.get("model_reported", {})
        grade_checks = grade_round.get("code_checks", {})
        grade = grade_event_time_v2_hypothesis(
            tier=tier_decision.tier,
            published=str(getattr(assessment.final_status, "value", assessment.final_status)) != "ABSTAIN",
            hypotheses=grade_reported.get("analyst", {}).get("hypotheses", ()),
            verifier=grade_reported.get("verifier", {}),
            checks=grade_checks.get("quotes", ()),
            gates=grade_checks.get("gates", ()),
            ceiling_codes=grade_checks.get("outcome", {}).get("ceiling_codes", ()),
        )
        trace.grade_decision(assessment.round, grade)

    # CLAIM_PLAN_BUILD
    stage = trace.begin("claims_and_render")
    observed_move = _observed_move_text(
        ticker, foundation.move_profile, temporal_identity.session_date
    )
    facts = _session_facts(foundation.move_profile, final_pack, temporal_identity)
    if facts:
        observed_move = f"{observed_move}. {facts}"
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
    abstain_context = ""
    abstention_data: dict[str, Any] = {"possible_explanation": None, "leads": []}
    if validated.status.value == "ABSTAIN":
        from catalyst_agents.attribution import abstain_context as _abstain
        from catalyst_agents.attribution.answer_snapshot import (
            possible_explanation_quotes,
        )

        abstention_data = _abstain.structured(
            primary_verifier,
            final_pack,
            ticker,
            possible_quotes=possible_explanation_quotes(trace.payload, final_pack),
        )
        abstain_context = _abstain.render_structured(abstention_data)
    writer_input = build_writer_input(
        validated,
        observed_move=observed_move,
        abstain_context=abstain_context,
        format_style_contract=format_contract,
        # Frozen-A4 §4: the assessment records whether the NO_MATERIAL gates
        # passed; the WriterInput gate reads that fact, never the type alone.
        no_material_gates_passed=bool(
            getattr(assessment, "no_material_gates_passed", False)
        ),
    )
    sink = writer_sink or InMemoryDeltaSink()
    # Cooperative boundary: observed before the Writer starts; the Writer
    # additionally observes between stream chunks and stops accepting deltas.
    raise_if_control_expired(control)
    # Q1c: the answer text is code-rendered from verified quotes; the Writer
    # LLM (``writer_llm``) is not called on the answer path.
    from catalyst_agents.nodes.quote_renderer import QuoteAnswerRenderer

    writer_result = writer_node(
        {"run_id": run_id},
        llm=QuoteAnswerRenderer(writer_input, basis=getattr(cascade, "basis", None)),
        writer_input=writer_input,
        sink=sink,
        control=control,
        provider_budget=None,
        provider_identity=provider_identity,
        model_identity=writer_model_identity,
    )
    # The renderer is not a provider: no logical call, attempt or usage.
    writer_result = {
        **writer_result,
        "writer_logical_calls": 0,
        "writer_provider_attempts": 0,
        "writer_attempt_usages": (),
    }
    raise_if_control_expired(control)
    answer = writer_result["answer"]

    # POST_STREAM_ASSURANCE: the Writer surfaces real input/output hashes and
    # provider token/completion metadata; assurance recomputes and verifies
    # every identity against the authoritative WriterInput/ValidatedClaimPlan.
    emitted_citations, emitted_claim_markers = extract_answer_markers(answer.text)
    final_evidence_state = foundation.evidence_state
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
        "round": (2 if desk_cascade and cascade.desk_result2 is not None else 1 + corrective_rounds),
        "terminal_error": None,
    }
    if corrective_rounds:
        state["round"] = 2
        state["evidence_state_ref"] = f"artifact:evidence_state:{run_id}:2"
        state["context_pack_ref"] = f"artifact:context_pack:{run_id}:2"
        state["context_pack_hash"] = final_pack.context_pack_sha256

    trace.end(stage)
    from catalyst_agents.attribution.reported_reason import run_reported_reason_stage
    reported_reason_result, reported_reason_status, calls = run_reported_reason_stage(
        trace=trace,
        enabled=(desk_cascade
                 and getattr(assessment.final_status, "value", assessment.final_status) == "ABSTAIN"
                 and os.environ.get("CATALYST_REPORTED_REASON", "1") != "0"),
        llm=analyst_llm, budget=provider_budget, provider=provider_identity,
        model_id=analyst_model_identity, deadline_epoch_ms=run_deadline_epoch_ms,
        profile=foundation.move_profile, ticker=ticker,
        company_name=web_source_company_name or _issuer_name(ticker),
        session_date=temporal_identity.session_date, items=final_pack.evidence_inventory,
    )
    verifier_calls = dict(getattr(primary_verifier, "calls", None) or {})
    if desk_cascade:
        verifier_calls = {
            "logical_calls": int(stage1_verifier_calls.get("logical_calls", 0)) + int(stage2_verifier_calls.get("logical_calls", 0)),
            "provider_attempts": int(stage1_verifier_calls.get("provider_attempts", 0)) + int(stage2_verifier_calls.get("provider_attempts", 0)),
            "attempt_usages": tuple(stage1_verifier_calls.get("attempt_usages", ())) + tuple(stage2_verifier_calls.get("attempt_usages", ())),
        }
    verifier_logical_calls = int(verifier_calls.get("logical_calls", 0))
    verifier_provider_attempts = int(verifier_calls.get("provider_attempts", 0))
    logical_model_call_count = (
        analyst_logical_calls + verifier_logical_calls + int(calls.get("logical_calls", 0))
        + writer_result["writer_logical_calls"]
    )
    retrieval_executions = [foundation.research_execution]
    retrieval_diagnostics = build_retrieval_diagnostics(
        data_runtime_identity=data_runtime_identity,
        executions=retrieval_executions,
    )
    provider_attempt_usages = (
        analyst_attempt_usages
        + tuple(verifier_calls.get("attempt_usages", ()))
        + tuple(calls.get("attempt_usages", ()))
        + tuple(writer_result["writer_attempt_usages"])
    )
    reported_input_tokens, reported_output_tokens = _aggregate_provider_usage(
        provider_attempt_usages
    )
    final_trace = trace.finish(
        final_status=assessment.final_status,
        calls={"analyst": analyst_logical_calls, "verifier": verifier_logical_calls,
               "writer": writer_result["writer_logical_calls"],
               # add-only: present only when the reported-reason call ran
               **({"reported_reason": int(calls["logical_calls"])} if calls.get("logical_calls") else {})},
    )
    from catalyst_agents.attribution.answer_snapshot import (
        ANSWER_BASIS_NOTE,
        build_answer_snapshot,
        derive_abstention_reason,
    )

    if getattr(assessment.final_status, "value", assessment.final_status) == "ABSTAIN":
        trace.abstention_reason(derive_abstention_reason(final_trace))

    final_quote_policy = ("event_time_p1" if desk_cascade and cascade.desk_result2 is not None
                          else "pre_close_v1" if desk_cascade
                          else attribution_policy.evidence_window_policy)
    answer_snapshot = build_answer_snapshot(
        run_id=run_id,
        ticker=ticker,
        move_profile=foundation.move_profile,
        temporal_identity=temporal_identity,
        facts_text=facts,
        assessment=assessment,
        validated_claim_plan=validated,
        context_pack=final_pack,
        decision_trace=final_trace,
        abstention_context=abstention_data,
        evidence_window_policy=final_quote_policy,
        governing_ref=tier_decision.governing_ref,
    )
    if answer_snapshot.get("outcome") == "ABSTAINED" and isinstance(reported_reason_result, dict):
        answer_snapshot["reported_reason"] = reported_reason_result
    answer_snapshot.update({
        "tier": tier_decision.tier,
        "tier_reason": tier_decision.reason_text,
        "governing_ref": (
            tier_decision.governing_ref
        ),
    })
    if desk_cascade:
        answer_snapshot["desk_context"] = _desk_context_payload(desk_context)
        answer_snapshot["basis"] = cascade.basis
        answer_snapshot["cascade_stage"] = cascade.last_attempted_stage
        answer_snapshot["last_attempted_stage"] = cascade.last_attempted_stage
        answer_snapshot["published_stage"] = cascade.published_stage
        if cascade.investigation_v2 is not None:
            answer_snapshot["investigation"] = {key: cascade.investigation_v2[key]
                                                for key in ("mode", "stop_reason", "steps", "published")}
        if attribution_policy.investigator_mode in ("checklist", "agent"):  # v2: v3's summary is no longer cleared
            answer_snapshot["investigation"] = (
                {key: cascade.investigation_payload[key] for key in ("mode", "stop_reason", "steps", "board")}
                if cascade.investigation_payload is not None else None)
        if cascade.earnings is not None:
            answer_snapshot["earnings_fallback"] = {k: v for k, v in cascade.earnings.items() if k != "quote"}
    if web_source_title is not None:
        answer_snapshot["evidence_origin"] = "web_search"
        answer_snapshot["label"] = "user-selected web source"
        from urllib.parse import urlsplit

        for source in answer_snapshot.get("sources", ()):
            source["domain"] = urlsplit(source.get("url") or "").hostname
    # Only quotes that the published answer actually contains (rendering may drop some). The answer renders every
    # statement through prose_channel (brackets and parentheses removed for marker safety), so a source is matched
    # the same way; matching the raw quote dropped every quote with a "(NYSE: X)" (v2 display fix).
    from catalyst_agents.nodes.writer import prose_channel

    published = answer.text
    published_flat = " ".join(published.split())

    def _published(text: Any) -> bool:
        return bool(text) and (text in published or " ".join(prose_channel(str(text)).split()) in published_flat)
    def _source_position(source: dict[str, Any]) -> int:
        needles = [source.get("evidence_id")]
        if source.get("text"):
            needles.append(" ".join(prose_channel(str(source["text"])).split()))
        positions = [
            position for needle in needles
            if needle and (position := published_flat.find(needle)) >= 0
        ]
        return min(positions, default=len(published_flat))

    answer_snapshot["sources"] = sorted(
        (q for q in answer_snapshot["sources"]
         if _published(q["text"]) or (
             (final_quote_policy in ("event_time_v1", "event_time_v2")
              or (desk_cascade and final_quote_policy == "event_time_p1"))
             and q.get("evidence_id") and q["evidence_id"] in published
         )),
        key=_source_position,
    )
    desk_key_text = None
    if desk_cascade:
        # The desk gate chose the attribution sentence; render order (by publication time) must not
        # demote it below the mechanism or event sentence it was published with.
        final_desk = cascade.final_desk
        primary = next((h for h in final_desk.analyst_decision.candidate_hypotheses
                        if getattr(h.proposed_role, "value", h.proposed_role) == "PRIMARY"), None)
        desk_key_text = primary.quotes[0].text if primary is not None and primary.quotes else None
        # v7 verifier: lead with the quote it says states the reason, not one that only describes the move
        # (MU 04-28 led with "down 2.6% at 10 a.m." while the OpenAI report sat in the next quote).
        verified = getattr(primary_verifier, "last", None) or {}
        cause_text = verified.get("cause_quote") if verified.get("verdict") is True else None
        if cause_text and any(q.get("text") == cause_text for q in answer_snapshot["sources"]):
            desk_key_text = cause_text
    for key, role in (("key_sentence", "PRIMARY"), ("also_sentence", "SECONDARY")):
        if desk_key_text and key == "key_sentence":
            first = next((q for q in answer_snapshot["sources"] if q.get("text") == desk_key_text), None)
            if first is not None:
                fields = ("text", "publisher", "publisher_family", "origin", "body_source", "published_at",
                          "event_time", "r4b_path", "url", "evidence_id", "roles")
                answer_snapshot[key] = {f: first.get(f) for f in fields}
                continue
        if ((final_quote_policy in ("event_time_v1", "event_time_v2")
             or (desk_cascade and final_quote_policy == "event_time_p1")) and key == "key_sentence"):
            grade_record = (final_trace.get("rounds", ())[-1].get("code_checks", {}).get("grade", {})
                            if final_trace.get("rounds") else {})
            wanted_id = grade_record.get("key_evidence_id")
            wanted_text = grade_record.get("key_quote_text")
            first = next((q for q in answer_snapshot["sources"]
                          if q.get("evidence_id") == wanted_id and q.get("text") == wanted_text), None)
            if first is None and (final_quote_policy == "event_time_v2"
                                  or (desk_cascade and final_quote_policy == "event_time_p1")):
                first = next((q for q in answer_snapshot["sources"] if q.get("role") == "PRIMARY"), None)
            fields = ("text", "publisher", "publisher_family", "origin", "body_source", "published_at", "event_time",
                      "r4b_path", "url", "evidence_id", "roles")
        else:
            first = next((q for q in answer_snapshot["sources"] if q["role"] == role), None)
            fields = ("text", "publisher", "published_at", "url", "evidence_id")
        answer_snapshot[key] = ({f: first.get(f) for f in fields} if first else None)
    if answer_snapshot["key_sentence"] is None and answer_snapshot["outcome"] == "ANSWERED":
        # No published PRIMARY quote: whatever the status label says, the reader got no cause.
        from catalyst_agents.attribution.answer_snapshot import abstention_block

        answer_snapshot["outcome"] = "ABSTAINED"
        answer_snapshot["abstention"] = abstention_block(final_trace, assessment, abstention_data)
    if web_source_title is not None:
        from catalyst_agents.attribution.answer_snapshot import (
            classify_web_source_snapshot,
        )

        answer_snapshot = classify_web_source_snapshot(
            answer_snapshot,
            final_trace,
            final_pack.evidence_inventory,
            filter_removed_all=web_source_filter_removed_all,
            evidence_window_policy=final_quote_policy,
        )
    answer_snapshot["basis_note"] = ANSWER_BASIS_NOTE if answer_snapshot["outcome"] == "ANSWERED" else None
    web_trace_getter = getattr(final_retriever, "web_trace", None)
    if callable(web_trace_getter):
        web_trace = web_trace_getter(used_evidence_ids=tuple(final_pack.included_evidence_ids))
        answer_snapshot["web_evidence"] = web_trace
        final_trace["web_evidence"] = web_trace
    emit("answer.ready", {"answer_snapshot": answer_snapshot})
    return V1FullRunResult(
        state=state,
        move_profile=foundation.move_profile,
        evidence_state=foundation.evidence_state,
        coverage_summary=foundation.coverage_summary,
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
        verifier_logical_calls=verifier_logical_calls,
        verifier_provider_attempts=verifier_provider_attempts,
        corrective_rounds=corrective_rounds,
        initial_task_ids=tuple(task.task_id for task in foundation.classification.tasks),
        initial_task_fingerprints=tuple(
            task.task_fingerprint for task in foundation.classification.tasks
        ),
        corrective_round_observations=corrective_round_observations,
        retrieval_diagnostics=retrieval_diagnostics,
        # ``None`` means the provider did not report verifiable usage; a real
        # reported zero remains a real zero and is never confused with absent
        # metadata.
        reported_input_tokens=reported_input_tokens,
        reported_output_tokens=reported_output_tokens,
        packing_policy_version=effective_packing_version,
        hypothesis_policy_version=effective_hypothesis_version,
        observation_policy_version=effective_observation_version,
        experiment_override_present=experiment_override is not None,
        decision_trace=final_trace,
        answer_snapshot=answer_snapshot,
        tier_decision=tier_decision,
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

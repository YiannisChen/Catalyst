"""M7 Batch-B corrective: A1/A2/A5 behavioral seams (not policy names).

Each seam must change observable behavior on the single run_v1_graph:
- A1: ``critic_prefix_600chars_v1`` renders a raw critic-prefix block while
  ``evidence_context_pack_v1`` renders the full ContextPack;
- A2: ``legacy_single_candidate_v1`` accepts exactly one candidate while
  ``bounded_competition_v1`` accepts the bounded competition set;
- A5: ``context_builder_v1_limited`` produces a limited observation while
  ``move_profile_v1`` produces the MoveProfile-aware observation.
``None`` must remain byte-identical to M6 production behavior.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from catalyst_agents.attribution.analyst import AnalystDecision
from catalyst_agents.runtime.experiment import (
    EVAL_CAPABILITY_TOKEN,
    ExperimentPolicyOverride,
    resolve_experiment_seams,
)


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


def _override(**seams) -> ExperimentPolicyOverride:
    return ExperimentPolicyOverride(
        eval_capability_token=EVAL_CAPABILITY_TOKEN, **seams
    )


def _decision_with_candidates(count: int) -> AnalystDecision:
    from catalyst_agents.attribution.analyst import (
        AttributionStatus,
        AttributionType,
        CandidateHypothesis,
        CauseType,
        EvidenceDecision,
        EvidenceDisposition,
        HypothesisRole,
        MagnitudeFit,
        ResearchDecision,
    )

    return AnalystDecision(
        schema_version="1.0",
        evidence_decisions=(
            EvidenceDecision(
                evidence_id="e1",
                disposition=EvidenceDisposition.SUPPORT,
                supports_hypothesis_refs=tuple(f"h{i}" for i in range(count)),
                reason_code="support",
            ),
        ),
        candidate_hypotheses=tuple(
            CandidateHypothesis(
                hypothesis_ref=f"h{i}",
                cause_type=CauseType.COMPANY_SPECIFIC_CATALYST,
                statement=f"candidate {i}",
                mechanism=None,
                supporting_evidence_ids=("e1",),
                contradicting_evidence_ids=(),
                magnitude_fit=MagnitudeFit.STRONG,
                proposed_role=HypothesisRole.PRIMARY if i == 0 else HypothesisRole.SECONDARY,
                unresolved_gap_refs=(),
            )
            for i in range(count)
        ),
        conflicts=(),
        proposed_missing_evidence=(),
        research_decision=ResearchDecision.READY,
        recommended_status=AttributionStatus.SUFFICIENT,
        proposed_attribution_type=AttributionType.EVIDENCE_BACKED_CAUSAL,
        proposed_corrective_intents=(),
    )


def test_resolve_seams_keeps_names_but_behavior_tests_prove_consumption():
    seams = resolve_experiment_seams(
        packing_policy_version="evidence_context_pack_v1",
        experiment_override=_override(a2_hypothesis_policy_version="legacy_single_candidate_v1"),
    )
    assert seams["hypothesis_policy_version"] == "legacy_single_candidate_v1"


# --- A2: hypothesis competition bound -------------------------------------


def test_a2_single_candidate_rejects_competition_decisions():
    from catalyst_agents.nodes.evidence_analyst import _validate_hypothesis_policy

    decision = _decision_with_candidates(2)
    assert _validate_hypothesis_policy(decision, "bounded_competition_v1") is None
    with pytest.raises(ValueError, match="single candidate"):
        _validate_hypothesis_policy(decision, "legacy_single_candidate_v1")
    single = _decision_with_candidates(1)
    assert _validate_hypothesis_policy(single, "legacy_single_candidate_v1") is None


def test_a2_none_preserves_m6_behavior():
    from catalyst_agents.nodes.evidence_analyst import _validate_hypothesis_policy

    decision = _decision_with_candidates(2)
    assert _validate_hypothesis_policy(decision, None) is None
    assert _validate_hypothesis_policy(decision, "bounded_competition_v1") is None


# --- A1: packing renderer behavior ----------------------------------------


def _temporal() -> "TemporalIdentity":
    from catalyst_data.canonical.temporal import TemporalIdentity

    return TemporalIdentity(
        session_date="2026-01-15",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-15T14:30:00Z"),
        session_close_at=_utc("2026-01-15T21:00:00Z"),
        information_window_start_at=_utc("2026-01-14T21:00:00Z"),
        cutoff_at=_utc("2026-01-15T21:00:00Z"),
    )


def _runtime() -> "DataRuntimeIdentity":
    from catalyst_data.canonical.identity import DataRuntimeIdentity

    return DataRuntimeIdentity(
        data_snapshot_id="s" * 64,
        corpus_manifest_id="m" * 64,
        fts_index_version="build:fts",
        dense_index_version="d" * 64,
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )


def _budget() -> "ContextBudget":
    from catalyst_agents.attribution.context_pack import ContextBudget

    return ContextBudget(
        model_context_limit=4_000,
        reserved_output_tokens=200,
        reserved_system_instruction_tokens=100,
        observation_tokens=100,
        coverage_summary_tokens=100,
        research_history_tokens=50,
        inventory_tokens=100,
        evidence_payload_tokens=1_000,
        per_news_item_max_tokens=120,
        per_sec_chunk_max_tokens=150,
        lead_only_tokens=60,
        safety_margin_tokens=50,
    )


def _coverage() -> "CoverageSummary":
    from catalyst_agents.attribution.coverage import CoverageSummary

    return CoverageSummary(
        eligible_item_count=1,
        eligible_asset_count=1,
        eligible_full_text_item_count=1,
        material_capable_item_count=1,
        material_capable_asset_count=1,
        primary_authority_asset_count=0,
        direct_primary_asset_count=1,
        reported_news_asset_count=1,
        commentary_lead_asset_count=0,
        unknown_role_asset_count=0,
        eligible_reported_news_group_count=1,
        unknown_independence_asset_count=0,
        known_duplicate_or_syndicated_asset_count=0,
        content_state_counts=(),
        parse_degraded_item_count=0,
    )


def _payload_item(
    evidence_id: str,
    excerpt: str | None = None,
    *,
    content_state: str = "FULL_TEXT",
    material_capability: str = "MATERIAL_CAPABLE",
    evidence_role: str = "DIRECT_PRIMARY",
) -> "EvidencePayloadItem":
    from catalyst_agents.attribution.context_pack import EvidencePayloadItem
    from catalyst_data.canonical.model import SourceClass

    return EvidencePayloadItem(
        evidence_id=evidence_id,
        canonical_asset_id=f"asset:{evidence_id}",
        canonical_content_version_id=f"version:{evidence_id}",
        corpus_document_id=f"doc:{evidence_id}",
        chunk_id=evidence_id,
        fact_id=None,
        section_key="body",
        chunk_ordinal=1,
        source_class=SourceClass.REPORTED_NEWS,
        evidence_role=evidence_role,
        eligible_at=_utc("2026-01-15T10:00:00Z"),
        content_state=content_state,
        material_capability=material_capability,
        independence_group_id=None,
        independence_status="UNKNOWN",
        content_hash="h" * 64,
        excerpt_text=excerpt,
    )


def _draft(packing_policy_version: str) -> "PackedContextDraft":
    from catalyst_agents.attribution.context_pack_builder import PackedContextDraft
    from catalyst_agents.attribution.move_profile import MoveProfile

    item = _payload_item("e1", "The company reported strong quarterly results " * 100)
    return PackedContextDraft(
        schema_version="context_pack_v1",
        packing_policy_version=packing_policy_version,
        run_id="run:1",
        round=1,
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        context_budget=_budget(),
        observation=MoveProfile(),
        coverage_summary=_coverage(),
        research_history=(),
        evidence_inventory=(item,),
        direct_primary_evidence=(item,),
        primary_authority_evidence=(),
        independent_reports=(),
        lead_only_evidence=(),
        structured_context=(),
        deterministic_conflict_signals=(),
        data_coverage_gaps=(),
        capability_gaps=(),
        retrieval_degradations=(),
        included_evidence_ids=("e1",),
        excluded_evidence_ids=(),
        truncation_metadata=(),
        delta_evidence_ids=(),
        prior_assessment_context=None,
    )


def test_a1_packing_changes_rendered_messages():
    """The two A1 versions render different analyst messages for the same
    evidence inventory: raw critic-prefix block vs full ContextPack."""
    from catalyst_agents.graph import (
        _foundation_renderer,
        _renderer_for_packing,
    )

    context_draft = _draft("evidence_context_pack_v1")
    critic_draft = _draft("critic_prefix_600chars_v1")
    context_messages = _renderer_for_packing(context_draft)(context_draft)
    critic_messages = _renderer_for_packing(critic_draft)(critic_draft)
    assert len(context_messages) == 3
    assert context_messages[0].content.startswith("observation=")
    assert len(critic_messages) == 1
    assert critic_messages[0].content.startswith("critic_prefix=")
    assert "evidence:e1:" in critic_messages[0].content
    assert context_messages != critic_messages
    # None/default remains the production ContextPack renderer.
    assert _renderer_for_packing(context_draft) is _foundation_renderer


def _draft_with_inventory(
    packing_policy_version: str,
    inventory: tuple,
    included: tuple[str, ...],
    excluded: tuple[str, ...],
) -> "PackedContextDraft":
    from catalyst_agents.attribution.context_pack_builder import PackedContextDraft
    from catalyst_agents.attribution.move_profile import MoveProfile

    return PackedContextDraft(
        schema_version="context_pack_v1",
        packing_policy_version=packing_policy_version,
        run_id="run:1",
        round=1,
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        context_budget=_budget(),
        observation=MoveProfile(),
        coverage_summary=_coverage(),
        research_history=(),
        evidence_inventory=inventory,
        direct_primary_evidence=(),
        primary_authority_evidence=(),
        independent_reports=(),
        lead_only_evidence=(),
        structured_context=(),
        deterministic_conflict_signals=(),
        data_coverage_gaps=(),
        capability_gaps=(),
        retrieval_degradations=(),
        included_evidence_ids=included,
        excluded_evidence_ids=excluded,
        truncation_metadata=(),
        delta_evidence_ids=(),
        prior_assessment_context=None,
    )


def test_foundation_renderer_inventory_lists_only_included_ids():
    """Citable inventory is included_evidence_ids, never METADATA_ONLY rows.

    The live c02 pack had a non-empty evidence_inventory of METADATA_ONLY
    identities and empty included_evidence_ids; labeling those IDs as
    ``inventory=`` made the model cite them and fail reference integrity.
    """
    from catalyst_agents.graph import _foundation_renderer

    included = _payload_item("e_full", "issuer exhibit body")
    meta = _payload_item(
        "e_meta",
        content_state="METADATA_ONLY",
        material_capability="NOT_CAPABLE",
        evidence_role="INDEPENDENT_REPORT",
    )
    draft = _draft_with_inventory(
        "evidence_context_pack_v1",
        inventory=(included, meta),
        included=("e_full",),
        excluded=("e_meta",),
    )
    messages = _foundation_renderer(draft)
    inventory_lines = [
        message.content
        for message in messages
        if message.content.startswith("inventory=")
    ]
    assert inventory_lines == ["inventory=e_full"]
    assert all("e_meta" not in line for line in inventory_lines)
    extra = [
        message.content
        for message in messages
        if message.content.startswith("metadata_only_identities=")
    ]
    assert extra == ["metadata_only_identities=e_meta"]
    assert not any("citable" in message.content.lower() and "e_meta" in message.content
                   for message in messages)


def test_foundation_renderer_empty_included_emits_inventory_none():
    """Empty citable set is explicit ``inventory=NONE``. METADATA_ONLY
    identities may appear as non-citable info, never as inventory."""
    from catalyst_agents.graph import _foundation_renderer

    meta_a = _payload_item(
        "meta:a",
        content_state="METADATA_ONLY",
        material_capability="NOT_CAPABLE",
        evidence_role="INDEPENDENT_REPORT",
    )
    meta_b = _payload_item(
        "meta:b",
        content_state="METADATA_ONLY",
        material_capability="NOT_CAPABLE",
        evidence_role="INDEPENDENT_REPORT",
    )
    draft = _draft_with_inventory(
        "evidence_context_pack_v1",
        inventory=(meta_a, meta_b),
        included=(),
        excluded=("meta:a", "meta:b"),
    )
    messages = _foundation_renderer(draft)
    inventory_lines = [
        message.content
        for message in messages
        if message.content.startswith("inventory=")
    ]
    assert inventory_lines == ["inventory=NONE"]
    joined_inventory = " ".join(inventory_lines)
    assert "meta:a" not in joined_inventory
    assert "meta:b" not in joined_inventory
    extra = [
        message.content
        for message in messages
        if message.content.startswith("metadata_only_identities=")
    ]
    assert extra == ["metadata_only_identities=meta:a,meta:b"]


def test_foundation_renderer_title_only_is_not_citable_inventory():
    """TITLE_ONLY identities may be shown as non-citable info, never inventory=."""
    from catalyst_agents.graph import _foundation_renderer

    title = _payload_item(
        "title:a",
        content_state="TITLE_ONLY",
        material_capability="LEAD_ONLY",
        evidence_role="INDEPENDENT_REPORT",
    )
    draft = _draft_with_inventory(
        "evidence_context_pack_v1",
        inventory=(title,),
        included=(),
        excluded=("title:a",),
    )
    messages = _foundation_renderer(draft)
    inventory_lines = [
        message.content
        for message in messages
        if message.content.startswith("inventory=")
    ]
    assert inventory_lines == ["inventory=NONE"]
    assert all("title:a" not in line for line in inventory_lines)
    extra = [
        message.content
        for message in messages
        if message.content.startswith("title_only_identities=")
    ]
    assert extra == ["title_only_identities=title:a"]


# --- A5: observation builder behavior --------------------------------------


def _context_inputs() -> "ContextInputs":
    from datetime import date

    from catalyst_agents.attribution.provider import ContextInputs

    return ContextInputs(
        ticker="AAPL",
        session_date=date(2026, 1, 15),
        cutoff="2026-01-15T21:00:00Z",
        target_close=110.0,
        previous_target_close=100.0,
        target_open=102.0,
        previous_2_target_close=90.0,
        target_volume=2200.0,
        expected_prior_sessions=("2026-01-14",),
        prior_volumes_by_session={"2026-01-14": 1000.0},
        benchmark_ticker="SPY",
        benchmark_return_pct=1.5,
        sector_ticker="XLK",
        sector_return_pct=2.0,
        peer_returns_by_ticker={"MSFT": 4.0, "NVDA": 6.0},
    )


class _RecordingProvider:
    def __init__(self, inputs):
        self._inputs = inputs

    def load_context_inputs(
        self,
        *,
        ticker: str,
        session_date: str,
        cutoff: str,
        information_window_start_at: str | None = None,
    ):
        return self._inputs


def _policy() -> "ObservationPolicyConfig":
    from catalyst_agents.runtime.manifest import ObservationPolicyConfig

    return ObservationPolicyConfig(
        material_target_return_pct=2.0,
        material_prior_return_pct=1.5,
        quiet_target_return_pct=0.5,
        flat_reference_return_pct=0.25,
        aligned_residual_pct=1.0,
        volume_elevated_ratio=1.5,
        volume_extreme_ratio=3.0,
        minimum_peer_count=3,
        require_sector_and_peer_for_broad_sector=True,
        scenario_policy_version="sp:v1",
    )


def test_a5_limited_observation_differs_from_move_profile():
    """context_builder_v1_limited drops peer/volume/comove context while
    move_profile_v1 keeps the full MoveProfile-aware observation."""
    from catalyst_agents.attribution.context_builder import ObservationBuilder

    provider = _RecordingProvider(_context_inputs())
    builder = ObservationBuilder(provider=provider, policy=_policy())
    full = builder.build(
        ticker="AAPL",
        session_date="2026-01-15",
        cutoff="2026-01-15T21:00:00Z",
        observation_policy_version="move_profile_v1",
    )
    limited = builder.build(
        ticker="AAPL",
        session_date="2026-01-15",
        cutoff="2026-01-15T21:00:00Z",
        observation_policy_version="context_builder_v1_limited",
    )
    assert full.peer_summary is not None
    assert full.volume_abnormality is not None
    assert limited.peer_summary is None
    assert limited.volume_abnormality is None
    assert limited.target_return == full.target_return
    # None/default is the MoveProfile-aware production behavior.
    default = builder.build(
        ticker="AAPL",
        session_date="2026-01-15",
        cutoff="2026-01-15T21:00:00Z",
    )
    assert default == full

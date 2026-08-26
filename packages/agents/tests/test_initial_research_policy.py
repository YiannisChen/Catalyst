"""M4-2: deterministic initial research policy (Frozen §6.2 precedence)."""
from __future__ import annotations

import pytest

from catalyst_agents.attribution.evidence_state import CapabilityGap
from catalyst_agents.attribution.move_profile import (
    AlignmentBand,
    AvailabilityState,
    DirectionRelation,
    MoveProfile,
    PeerSummary,
    SessionAlignment,
    VolumeAbnormality,
    VolumeBand,
)
from catalyst_agents.retrieval.policy import InitialResearchPolicy, ScenarioClassification
from catalyst_agents.retrieval.task import EvidenceNeed, ScenarioType, TimeScope
from catalyst_agents.runtime.manifest import ObservationPolicyConfig


def _policy(**overrides) -> ObservationPolicyConfig:
    base = dict(
        material_target_return_pct=2.0,
        material_prior_return_pct=1.5,
        quiet_target_return_pct=0.5,
        flat_reference_return_pct=0.25,
        aligned_residual_pct=1.0,
        volume_elevated_ratio=1.5,
        volume_extreme_ratio=3.0,
        minimum_peer_count=2,
        require_sector_and_peer_for_broad_sector=True,
        scenario_policy_version="sp:v1",
    )
    base.update(overrides)
    return ObservationPolicyConfig(**base)


def _aligned(kind: str = "MARKET") -> SessionAlignment:
    return SessionAlignment(
        schema_version="session_alignment_v1",
        reference_kind=kind,
        target_return_pct=3.0,
        reference_return_pct=2.9,
        residual_return_pct=0.1,
        direction_relation=DirectionRelation.SAME_DIRECTION,
        band=AlignmentBand.ALIGNED,
        availability=AvailabilityState.AVAILABLE,
    )


def _divergent(kind: str = "MARKET") -> SessionAlignment:
    return SessionAlignment(
        schema_version="session_alignment_v1",
        reference_kind=kind,
        target_return_pct=3.0,
        reference_return_pct=-1.0,
        residual_return_pct=4.0,
        direction_relation=DirectionRelation.OPPOSITE_DIRECTION,
        band=AlignmentBand.DIVERGENT,
        availability=AvailabilityState.AVAILABLE,
    )


def _unknown(kind: str = "MARKET") -> SessionAlignment:
    return SessionAlignment(
        schema_version="session_alignment_v1",
        reference_kind=kind,
        target_return_pct=None,
        reference_return_pct=None,
        residual_return_pct=None,
        direction_relation=DirectionRelation.UNKNOWN,
        band=AlignmentBand.UNKNOWN,
        availability=AvailabilityState.UNAVAILABLE,
        reason_codes=("alignment_reference_unavailable",),
    )


def _peers(median: float = 2.9, count: int = 2) -> PeerSummary:
    from catalyst_agents.attribution.move_profile import PeerReturn

    return PeerSummary(
        schema_version="peer_summary_v1",
        expected_peer_count=count,
        available_peer_count=count,
        peer_returns=tuple(
            PeerReturn(ticker=f"P{index}", return_pct=median)
            for index in range(count)
        ),
        median_return_pct=median,
        target_minus_median_pct=0.1,
        coverage_state=AvailabilityState.AVAILABLE,
    )


def _volume(band: VolumeBand = VolumeBand.NORMAL) -> VolumeAbnormality:
    return VolumeAbnormality(
        schema_version="volume_abnormality_v1",
        target_volume=1000.0,
        baseline_median_volume=1000.0,
        ratio=1.0,
        expected_session_count=20,
        valid_session_count=12,
        band=band,
        availability=AvailabilityState.AVAILABLE,
    )


def _profile(**overrides) -> MoveProfile:
    from catalyst_agents.attribution.move_profile import ScheduledMacroFlag

    base = dict(
        target_return=3.0,
        prior_session_return=2.0,
        gap_return=0.5,
        market_return=2.9,
        sector_return=2.8,
        peer_summary=_peers(),
        market_adjusted_return=0.1,
        sector_adjusted_return=0.2,
        volume_abnormality=_volume(),
        scheduled_macro_flags=(),
        market_comove=_aligned("MARKET"),
        sector_comove=_aligned("SECTOR"),
        peer_comove=_aligned("PEER_MEDIAN"),
        coverage_flags=(),
        degraded_fields=(),
    )
    base.update(overrides)
    return MoveProfile(**base)


def _macro_profile() -> MoveProfile:
    from catalyst_agents.attribution.move_profile import ScheduledMacroFlag

    return _profile(
        scheduled_macro_flags=(ScheduledMacroFlag(name="FOMC", scheduled_at=None),)
    )




def _full_policy():
    """Policy where every frozen evidence need has a supported backend."""
    from catalyst_agents.retrieval.task import EvidenceNeed

    return InitialResearchPolicy(
        _policy(),
        supported_evidence_needs=frozenset(EvidenceNeed),
    )


def _policy_for(scenario: ScenarioType, profile: MoveProfile):
    return InitialResearchPolicy(_policy()).classify(profile)


def _needs(classification: ScenarioClassification) -> list[tuple[EvidenceNeed, TimeScope]]:
    return [(task.evidence_need, task.time_scope) for task in classification.tasks]


def test_scheduled_macro_precedence_and_tasks():
    classification = _full_policy().classify(_macro_profile())
    assert classification.scenario == ScenarioType.SCHEDULED_MACRO
    assert classification.matched_predicate == "SCHEDULED_MACRO"
    assert _needs(classification) == [
        (EvidenceNeed.MACRO_EVENT, TimeScope.SESSION_INFORMATION_WINDOW),
        (EvidenceNeed.MACRO_SERIES, TimeScope.SESSION_INFORMATION_WINDOW),
        (EvidenceNeed.COMPANY_NEWS, TimeScope.SESSION_INFORMATION_WINDOW),
    ]
    assert [task.priority for task in classification.tasks] == [0, 1, 2]


def test_continuation_tasks_without_news_dependency():
    classification = _policy_for(ScenarioType.CONTINUATION, _profile())
    assert classification.scenario == ScenarioType.CONTINUATION
    assert _needs(classification) == [
        (EvidenceNeed.COMPANY_PRIMARY, TimeScope.PRIOR_SESSION),
        (EvidenceNeed.COMPANY_NEWS, TimeScope.PRIOR_SESSION),
        (EvidenceNeed.COMPANY_NEWS, TimeScope.SESSION_INFORMATION_WINDOW),
    ]


def test_broad_sector_tasks():
    classification = _full_policy().classify(
        _profile(prior_session_return=0.2),  # below material prior
    )
    assert classification.scenario == ScenarioType.BROAD_SECTOR
    assert _needs(classification) == [
        (EvidenceNeed.SECTOR_NEWS, TimeScope.SESSION_INFORMATION_WINDOW),
        (EvidenceNeed.MACRO_EVENT, TimeScope.SESSION_INFORMATION_WINDOW),
        (EvidenceNeed.COMPANY_NEWS, TimeScope.SESSION_INFORMATION_WINDOW),
    ]


def test_company_specific_tasks():
    classification = _policy_for(
        ScenarioType.COMPANY_SPECIFIC,
        _profile(
            prior_session_return=0.2,
            sector_comove=_divergent("SECTOR"),
            peer_comove=_divergent("PEER_MEDIAN"),
        ),
    )
    assert classification.scenario == ScenarioType.COMPANY_SPECIFIC
    assert _needs(classification) == [
        (EvidenceNeed.COMPANY_PRIMARY, TimeScope.SESSION_INFORMATION_WINDOW),
        (EvidenceNeed.COMPANY_NEWS, TimeScope.SESSION_INFORMATION_WINDOW),
    ]


def test_quiet_or_unclassified_tasks():
    classification = _policy_for(
        ScenarioType.QUIET_OR_UNCLASSIFIED,
        _profile(target_return=0.2),
    )
    assert classification.scenario == ScenarioType.QUIET_OR_UNCLASSIFIED
    assert _needs(classification) == [
        (EvidenceNeed.COMPANY_NEWS, TimeScope.SESSION_INFORMATION_WINDOW),
        (EvidenceNeed.COMPANY_PRIMARY, TimeScope.SESSION_INFORMATION_WINDOW),
    ]


def test_missing_target_return_falls_to_quiet():
    classification = _policy_for(
        ScenarioType.QUIET_OR_UNCLASSIFIED,
        _profile(target_return=None, prior_session_return=None),
    )
    assert classification.scenario == ScenarioType.QUIET_OR_UNCLASSIFIED


def test_overlapping_predicates_precedence_wins():
    """A profile satisfying macro + continuation + broad sector classifies as
    SCHEDULED_MACRO (Frozen §6.2 precedence)."""
    classification = _policy_for(ScenarioType.SCHEDULED_MACRO, _macro_profile())
    assert classification.scenario == ScenarioType.SCHEDULED_MACRO
    assert _needs(classification)[-1] == (
        EvidenceNeed.COMPANY_NEWS,
        TimeScope.SESSION_INFORMATION_WINDOW,
    )


def test_at_most_three_tasks_and_unique_fingerprints():
    for profile in (_macro_profile(), _profile(), _profile(prior_session_return=0.2)):
        classification = InitialResearchPolicy(_policy()).classify(profile)
        assert len(classification.tasks) <= 3
        fingerprints = [task.task_fingerprint for task in classification.tasks]
        assert len(fingerprints) == len(set(fingerprints))


def test_continuation_classify_takes_only_move_profile():
    """CONTINUATION must not depend on news/evidence: the classify signature
    accepts only the MoveProfile."""
    import inspect

    signature = inspect.signature(InitialResearchPolicy.classify)
    assert "evidence" not in signature.parameters
    assert "news" not in signature.parameters


def test_unsupported_sector_macro_emit_capability_gaps_not_widened_tasks():
    """Default M4 capabilities: SECTOR_NEWS/MACRO needs emit typed capability
    gaps; they never widen to generic company retrieval."""
    policy = InitialResearchPolicy(_policy())
    macro_classification = policy.classify(_macro_profile())
    gap_needs = {gap.evidence_need for gap in macro_classification.capability_gaps}
    assert EvidenceNeed.MACRO_EVENT in gap_needs
    assert EvidenceNeed.MACRO_SERIES in gap_needs
    assert all(
        task.evidence_need in {EvidenceNeed.COMPANY_PRIMARY, EvidenceNeed.COMPANY_NEWS}
        for task in macro_classification.tasks
    )

    broad = policy.classify(_profile(prior_session_return=0.2))
    broad_gap_needs = {gap.evidence_need for gap in broad.capability_gaps}
    assert EvidenceNeed.SECTOR_NEWS in broad_gap_needs
    assert EvidenceNeed.MACRO_EVENT in broad_gap_needs
    assert all(
        task.evidence_need in {EvidenceNeed.COMPANY_PRIMARY, EvidenceNeed.COMPANY_NEWS}
        for task in broad.tasks
    )


def test_market_structure_never_creates_retrieval_task():
    policy = InitialResearchPolicy(_policy())
    gap = policy.capability_gap_for(EvidenceNeed.MARKET_STRUCTURE)
    assert isinstance(gap, CapabilityGap)
    assert gap.evidence_need == EvidenceNeed.MARKET_STRUCTURE
    assert gap.recoverable_in_current_runtime is False
    for scenario in ScenarioType:
        profile = _macro_profile() if scenario is ScenarioType.SCHEDULED_MACRO else _profile()
        classification = policy.classify(profile)
        assert all(
            task.evidence_need is not EvidenceNeed.MARKET_STRUCTURE
            for task in classification.tasks
        )


def test_deterministic_task_order_and_id_shape():
    classification = _policy_for(ScenarioType.SCHEDULED_MACRO, _macro_profile())
    ids = [task.task_id for task in classification.tasks]
    assert ids == sorted(ids)
    for task in classification.tasks:
        assert task.task_id.startswith("research:1:")
        assert len(task.task_fingerprint) == 64
        assert task.round == 1
        assert task.scenario == ScenarioType.SCHEDULED_MACRO


def test_classification_carries_version_and_reason_codes():
    classification = _policy_for(ScenarioType.SCHEDULED_MACRO, _macro_profile())
    assert classification.scenario_policy_version == "sp:v1"
    assert classification.matched_predicate
    assert classification.input_field_names
    assert classification.reason_codes

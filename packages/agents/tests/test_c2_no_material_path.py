"""WP-C C2: the no-material path (Frozen-A4 Option X pairing; A3 §9 WP-C).

Deterministic synthetic-pack coverage for the frozen C2 predicate:

- the degraded-parse veto becomes an *in-pack, cutoff-valid, FULL_TEXT,
  material-capable* item with `parse_quality == "failed"`; `degraded` items
  are reported through coverage but never veto;
- point-in-time market/sector/peer context decides the pairing: adequate ->
  PARTIAL + NO_MATERIAL_PUBLIC_CATALYST, inadequate -> ABSTAIN +
  NO_MATERIAL_PUBLIC_CATALYST, with the inadequate context disclosed as a
  coverage gap;
- gate failure -> ABSTAIN + EVIDENCE_BACKED_CAUSAL + NO_MATERIAL_GATES_FAILED;
- an abstention whose only gate failure is the in-pack failed-parse veto is
  labelled `metadata_forced` (G3/G4 prereg §4.5.2).

No case ids, tickers, dates or gold fields appear here. Packs are synthetic.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from catalyst_agents.attribution.analyst import (
    AnalystDecision,
    AttributionStatus,
    AttributionType,
    ResearchDecision,
)
from catalyst_agents.attribution.assessment import (
    CONTEXT_ADEQUACY_FIELD,
    normalize_decision,
)
from catalyst_agents.attribution.coverage import CoverageSummary, DataCoverageGap
from catalyst_agents.attribution.move_profile import (
    AlignmentBand,
    AvailabilityState,
    DirectionRelation,
    SessionAlignment,
)
from catalyst_agents.attribution.no_material_gates import (
    NoMaterialOutcome,
    resolve_no_material_outcome,
)
from catalyst_agents.retrieval.task import EvidenceNeed, ResearchTask
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


def _h(part: str) -> str:
    return hashlib.sha256(f"c2:{part}".encode("utf-8")).hexdigest()


def _temporal() -> TemporalIdentity:
    return TemporalIdentity(
        session_date="2026-01-15",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-15T14:30:00Z"),
        session_close_at=_utc("2026-01-15T21:00:00Z"),
        information_window_start_at=_utc("2026-01-14T21:00:00Z"),
        cutoff_at=_utc("2026-01-15T21:00:00Z"),
    )


def _runtime() -> DataRuntimeIdentity:
    return DataRuntimeIdentity(
        data_snapshot_id=_h("snapshot"),
        corpus_manifest_id=_h("corpus"),
        fts_index_version="build:fts",
        dense_index_version=_h("dense"),
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )


class _Item:
    """Minimal in-pack item surface: content state, materiality, parse quality."""

    def __init__(
        self,
        *,
        evidence_id: str,
        content_state: str = "FULL_TEXT",
        material_capability: str = "MATERIAL_CAPABLE",
        parse_quality: str = "full",
        evidence_role: str = "DIRECT_PRIMARY",
        independence_status: str = "KNOWN_GROUP",
        independence_group_id: str | None = None,
        eligible_at: datetime | None = None,
    ) -> None:
        self.evidence_id = evidence_id
        self.content_state = content_state
        self.material_capability = material_capability
        self.parse_quality = parse_quality
        self.evidence_role = evidence_role
        self.independence_status = independence_status
        self.independence_group_id = independence_group_id
        self.eligible_at = eligible_at or _utc("2026-01-15T10:00:00Z")


class _Pack:
    def __init__(
        self,
        *,
        inventory: tuple[_Item, ...] = (),
        included: tuple[str, ...] | None = None,
        coverage: CoverageSummary | None = None,
        data_coverage_gaps: tuple[Any, ...] = (),
        capability_gaps: tuple[Any, ...] = (),
        retrieval_degradations: tuple[Any, ...] = (),
        research_history: tuple[ResearchTask, ...] = (),
        temporal: TemporalIdentity | None = None,
    ) -> None:
        self.run_id = "run:1"
        self.round = 1
        self.evidence_inventory = inventory
        self.included_evidence_ids = (
            tuple(item.evidence_id for item in inventory)
            if included is None
            else included
        )
        self.coverage_summary = coverage
        self.data_coverage_gaps = data_coverage_gaps
        self.capability_gaps = capability_gaps
        self.retrieval_degradations = retrieval_degradations
        self.research_history = research_history
        self.temporal_identity = temporal or _temporal()
        self.context_pack_sha256 = "a" * 64
        self.rendered_messages_sha256 = "b" * 64
        self.data_runtime_identity = _runtime()


class _Registry:
    def is_recoverable(self, need: EvidenceNeed) -> bool:
        return False


def _task(need: EvidenceNeed) -> ResearchTask:
    return ResearchTask(
        schema_version="1.0",
        task_id=f"task:{need.value}",
        round=1,
        priority=1,
        scenario="COMPANY_SPECIFIC",
        evidence_need=need,
        time_scope="SESSION_INFORMATION_WINDOW",
        retrieval_policy_id="rp:v1",
        task_fingerprint="f" * 64,
    )


def _both_tasks() -> tuple[ResearchTask, ...]:
    return (_task(EvidenceNeed.COMPANY_PRIMARY), _task(EvidenceNeed.COMPANY_NEWS))


def _alignment(kind: str, *, available: bool = True) -> SessionAlignment:
    if not available:
        return SessionAlignment(
            schema_version="session_alignment_v1",
            reference_kind=kind,
            direction_relation=DirectionRelation.UNKNOWN,
            band=AlignmentBand.UNKNOWN,
            availability=AvailabilityState.UNAVAILABLE,
            reason_codes=("alignment_reference_unavailable",),
        )
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


def _coverage(
    *,
    adequate: bool = False,
    parse_degraded_item_count: int = 0,
) -> CoverageSummary:
    return CoverageSummary(
        eligible_item_count=1,
        eligible_asset_count=1,
        eligible_full_text_item_count=1,
        material_capable_item_count=1,
        material_capable_asset_count=1,
        primary_authority_asset_count=0,
        direct_primary_asset_count=1,
        reported_news_asset_count=0,
        commentary_lead_asset_count=0,
        unknown_role_asset_count=0,
        eligible_reported_news_group_count=0,
        unknown_independence_asset_count=0,
        known_duplicate_or_syndicated_asset_count=0,
        content_state_counts={"FULL_TEXT": 1},
        parse_degraded_item_count=parse_degraded_item_count,
        market_alignment=_alignment("MARKET", available=adequate),
        sector_alignment=_alignment("SECTOR", available=adequate),
        peer_alignment=_alignment("PEER_MEDIAN", available=adequate),
    )


def _no_material_decision(
    *,
    evidence_id: str | None = None,
    recommended_status: str = AttributionStatus.ABSTAIN.value,
) -> AnalystDecision:
    hypotheses: tuple[dict[str, Any], ...] = ()
    decisions: tuple[dict[str, Any], ...] = ()
    if evidence_id is not None:
        decisions = (
            {
                "evidence_id": evidence_id,
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ("h1",),
                "reason_code": "material_support",
            },
        )
        hypotheses = (
            {
                "hypothesis_ref": "h1",
                "cause_type": "COMPANY_SPECIFIC_CATALYST",
                "statement": "A company-specific catalyst was disclosed.",
                "supporting_evidence_ids": (evidence_id,),
                "magnitude_fit": "STRONG",
                "proposed_role": "PRIMARY",
            },
        )
    return AnalystDecision.model_validate(
        {
            "schema_version": "1.0",
            "evidence_decisions": decisions,
            "candidate_hypotheses": hypotheses,
            "research_decision": ResearchDecision.READY.value,
            "recommended_status": recommended_status,
            "proposed_attribution_type": (
                AttributionType.NO_MATERIAL_PUBLIC_CATALYST.value
            ),
        }
    )


def _normalize(pack: _Pack, decision: AnalystDecision | None = None):
    return normalize_decision(
        decision or _no_material_decision(),
        context_pack=pack,
        capability_registry=_Registry(),
        policy_version="norm:v1",
        evidence_state_hash="e" * 64,
    )


# ---------------------------------------------------------------------------
# Predicate: empty pack, task completion
# ---------------------------------------------------------------------------


def test_empty_pack_with_both_tasks_is_abstain_no_material() -> None:
    pack = _Pack(coverage=_coverage(adequate=False), research_history=_both_tasks())
    result = _normalize(pack)
    assert result.no_material_gates_passed is True
    assert result.final_status is AttributionStatus.ABSTAIN
    assert result.final_attribution_type is AttributionType.NO_MATERIAL_PUBLIC_CATALYST


def test_no_material_outcome_encapsulates_inadequate_context_policy() -> None:
    pack = _Pack(coverage=_coverage(adequate=False), research_history=_both_tasks())
    outcome = resolve_no_material_outcome(
        _no_material_decision(), (), {}, pack
    )
    assert outcome == NoMaterialOutcome(
        passed=True,
        final_attribution_type=AttributionType.NO_MATERIAL_PUBLIC_CATALYST,
        status_ceiling=AttributionStatus.ABSTAIN,
        diagnostics=("NO_MATERIAL_CONTEXT_INADEQUATE",),
    )


def test_empty_pack_missing_one_task_is_not_no_material() -> None:
    pack = _Pack(
        coverage=_coverage(adequate=False),
        research_history=(_task(EvidenceNeed.COMPANY_PRIMARY),),
    )
    result = _normalize(pack)
    assert result.no_material_gates_passed is False
    assert result.final_attribution_type is AttributionType.EVIDENCE_BACKED_CAUSAL
    assert "NO_MATERIAL_GATES_FAILED" in result.normalization_diagnostics


# ---------------------------------------------------------------------------
# Predicate: in-pack parse quality
# ---------------------------------------------------------------------------


def test_in_pack_full_text_degraded_does_not_veto() -> None:
    item = _Item(evidence_id="e1", parse_quality="degraded")
    pack = _Pack(
        inventory=(item,),
        coverage=_coverage(adequate=False, parse_degraded_item_count=1),
        research_history=_both_tasks(),
    )
    result = _normalize(pack)
    assert result.no_material_gates_passed is True
    assert result.final_attribution_type is AttributionType.NO_MATERIAL_PUBLIC_CATALYST


def test_in_pack_full_text_failed_vetoes() -> None:
    item = _Item(evidence_id="e1", parse_quality="failed")
    pack = _Pack(
        inventory=(item,),
        coverage=_coverage(adequate=False),
        research_history=_both_tasks(),
    )
    result = _normalize(pack)
    assert result.no_material_gates_passed is False
    assert result.final_status is AttributionStatus.ABSTAIN
    assert result.final_attribution_type is AttributionType.EVIDENCE_BACKED_CAUSAL
    assert "NO_MATERIAL_GATES_FAILED" in result.normalization_diagnostics
    assert "metadata_forced" in result.normalization_diagnostics


def test_no_material_outcome_preserves_metadata_forced_failure_order() -> None:
    item = _Item(evidence_id="e1", parse_quality="failed")
    pack = _Pack(
        inventory=(item,),
        coverage=_coverage(adequate=False),
        research_history=_both_tasks(),
    )
    outcome = resolve_no_material_outcome(
        _no_material_decision(), (), {"e1": item}, pack
    )
    assert outcome.passed is False
    assert outcome.final_attribution_type is AttributionType.EVIDENCE_BACKED_CAUSAL
    assert outcome.status_ceiling is AttributionStatus.ABSTAIN
    assert outcome.diagnostics == ("NO_MATERIAL_GATES_FAILED", "metadata_forced")


def test_non_full_text_failed_does_not_veto() -> None:
    item = _Item(
        evidence_id="e1",
        content_state="TITLE_ONLY",
        parse_quality="failed",
        material_capability="LEAD_ONLY",
    )
    pack = _Pack(
        inventory=(item,),
        coverage=_coverage(adequate=False),
        research_history=_both_tasks(),
    )
    result = _normalize(pack)
    assert result.no_material_gates_passed is True


def test_post_cutoff_failed_item_does_not_veto() -> None:
    item = _Item(
        evidence_id="e1",
        parse_quality="failed",
        eligible_at=_utc("2026-01-16T10:00:00Z"),
    )
    pack = _Pack(
        inventory=(item,),
        coverage=_coverage(adequate=False),
        research_history=_both_tasks(),
    )
    result = _normalize(pack)
    assert result.no_material_gates_passed is True


def test_material_support_present_always_blocks_no_material() -> None:
    item = _Item(evidence_id="e1", parse_quality="degraded")
    pack = _Pack(
        inventory=(item,),
        coverage=_coverage(adequate=False),
        research_history=_both_tasks(),
    )
    result = _normalize(pack, _no_material_decision(evidence_id="e1"))
    assert result.no_material_gates_passed is False
    assert result.final_attribution_type is AttributionType.EVIDENCE_BACKED_CAUSAL


def test_retrieval_degradation_vetoes() -> None:
    class _Deg:
        task_id = "task:x"
        component = "retriever"
        reason_code = "timeout"
        requested_mode = "hybrid"
        served_mode = None

    pack = _Pack(
        coverage=_coverage(adequate=False),
        research_history=_both_tasks(),
        retrieval_degradations=(_Deg(),),
    )
    result = _normalize(pack)
    assert result.no_material_gates_passed is False
    assert result.final_attribution_type is AttributionType.EVIDENCE_BACKED_CAUSAL


# ---------------------------------------------------------------------------
# Pairing: context adequacy (Frozen-A4 Option X)
# ---------------------------------------------------------------------------


def test_adequate_context_yields_partial_no_material() -> None:
    # final = lower(model recommendation, ceiling); the adequate-context
    # ceiling is PARTIAL, so a PARTIAL recommendation delivers PARTIAL.
    pack = _Pack(coverage=_coverage(adequate=True), research_history=_both_tasks())
    result = _normalize(pack, _no_material_decision(recommended_status="PARTIAL"))
    assert result.no_material_gates_passed is True
    assert result.final_status is AttributionStatus.PARTIAL
    assert result.final_attribution_type is AttributionType.NO_MATERIAL_PUBLIC_CATALYST


def test_inadequate_context_yields_abstain_no_material() -> None:
    pack = _Pack(coverage=_coverage(adequate=False), research_history=_both_tasks())
    result = _normalize(pack)
    assert result.no_material_gates_passed is True
    assert result.final_status is AttributionStatus.ABSTAIN
    assert result.final_attribution_type is AttributionType.NO_MATERIAL_PUBLIC_CATALYST


def test_unavailable_non_context_gap_still_vetoes() -> None:
    gap = DataCoverageGap(
        gap_id="data:target_return",
        field="target_return",
        reason_code="target_return_unavailable",
        availability="UNAVAILABLE",
        source_artifact_id="move_profile",
    )
    pack = _Pack(
        coverage=_coverage(adequate=False),
        research_history=_both_tasks(),
        data_coverage_gaps=(gap,),
    )
    result = _normalize(pack)
    assert result.no_material_gates_passed is False
    assert result.final_attribution_type is AttributionType.EVIDENCE_BACKED_CAUSAL


def test_unavailable_context_gap_is_decided_not_vetoed() -> None:
    gaps = tuple(
        DataCoverageGap(
            gap_id=f"data:{field}",
            field=field,
            reason_code="context_unavailable",
            availability="UNAVAILABLE",
            source_artifact_id="move_profile",
        )
        for field in ("market_return", "sector_return", "peer_summary")
    )
    pack = _Pack(
        coverage=_coverage(adequate=False),
        research_history=_both_tasks(),
        data_coverage_gaps=gaps,
    )
    result = _normalize(pack)
    assert result.no_material_gates_passed is True
    assert result.final_status is AttributionStatus.ABSTAIN
    assert result.final_attribution_type is AttributionType.NO_MATERIAL_PUBLIC_CATALYST
    assert "NO_MATERIAL_GATES_FAILED" not in result.normalization_diagnostics


def test_context_adequacy_field_constant_names_the_three_context_fields() -> None:
    assert CONTEXT_ADEQUACY_FIELD == ("market_return", "sector_return", "peer_summary")

"""V1.1 EvidenceAnalystContextPack contract tests (M2-5).

Fields per Frozen §6.3.1; round-one invariant keeps the four prior_* semantic
fields empty; pack/render hashes are hex strings.
"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_agents.attribution.context_pack import EvidenceAnalystContextPack
from catalyst_agents.attribution.coverage import CoverageSummary
from catalyst_agents.attribution.evidence_state import EvidenceStateItem
from catalyst_agents.attribution.move_profile import MoveProfile
from catalyst_agents.retrieval.task import ResearchTask


def _evidence_item() -> EvidenceStateItem:
    return EvidenceStateItem(
        evidence_id="corpus:chunk:0001",
        first_seen_round=1,
        research_task_ids=("task-1",),
        state="accepted",
        canonical_asset_id="issuer:AAPL:news:0001",
        content_version_id="content:v1:0001",
        chunk_id="corpus:chunk:0001",
        fact_id=None,
        content_state="FULL_TEXT",
        source_class="issuer_disclosure",
        independence_group=None,
        dedup_cluster_id=None,
    )


def _coverage() -> CoverageSummary:
    return CoverageSummary(
        eligible_item_count=1,
        eligible_asset_count=1,
        eligible_full_text_item_count=1,
        material_capable_item_count=1,
        material_capable_asset_count=1,
        primary_authority_asset_count=0,
        direct_primary_asset_count=1,
        independent_report_asset_count=0,
        commentary_lead_asset_count=0,
        unknown_role_asset_count=0,
        eligible_reported_news_group_count=0,
        unknown_independence_asset_count=0,
        duplicate_or_syndicated_asset_count=0,
        content_state_counts={"FULL_TEXT": 1},
        parse_degraded_count=0,
        retrieval_degradations=(),
        data_coverage_gaps=(),
        capability_gaps=(),
        market_alignment=None,
        sector_alignment=None,
        peer_alignment=None,
        scheduled_macro_present=None,
    )


def _pack(**overrides: Any) -> dict[str, Any]:
    item = _evidence_item()
    base: dict[str, Any] = {
        "schema_version": "v1",
        "packing_policy_version": "p1",
        "round": 1,
        "token_budget": 4000,
        "hard_constraints": {"max_excerpt_chars": 600},
        "observation": MoveProfile(target_return=3.2),
        "coverage_summary": _coverage(),
        "research_history": (
            ResearchTask(
                task_id="task-1",
                evidence_need="COMPANY_PRIMARY",
                time_scope="SESSION_INFORMATION_WINDOW",
                retrieval_policy_id="qp:v1",
            ),
        ),
        "direct_primary_evidence": (item,),
        "primary_authority_evidence": (),
        "independent_reports": (),
        "lead_only_evidence": (),
        "deterministic_conflict_signals": ("dedup:cluster:g1",),
        "coverage_gaps": (),
        "included_evidence_ids": ("corpus:chunk:0001",),
        "excluded_evidence_ids": (),
        "truncation_metadata": {},
        "delta_evidence_ids": (),
        "prior_counter_evidence_ids": (),
        "prior_semantic_conflicts": (),
        "previously_supported_hypothesis_ids": (),
        "unresolved_gap_ids": (),
        "context_pack_sha256": "a" * 64,
        "rendered_messages_sha256": "b" * 64,
    }
    base.update(overrides)
    return base


def test_context_pack_fields_match_frozen_6_3_1() -> None:
    pack = EvidenceAnalystContextPack(**_pack())
    assert set(EvidenceAnalystContextPack.model_fields) == {
        "schema_version",
        "packing_policy_version",
        "round",
        "token_budget",
        "hard_constraints",
        "observation",
        "coverage_summary",
        "research_history",
        "direct_primary_evidence",
        "primary_authority_evidence",
        "independent_reports",
        "lead_only_evidence",
        "deterministic_conflict_signals",
        "coverage_gaps",
        "included_evidence_ids",
        "excluded_evidence_ids",
        "truncation_metadata",
        "delta_evidence_ids",
        "prior_counter_evidence_ids",
        "prior_semantic_conflicts",
        "previously_supported_hypothesis_ids",
        "unresolved_gap_ids",
        "context_pack_sha256",
        "rendered_messages_sha256",
    }


def test_context_pack_is_strict_and_frozen() -> None:
    pack = EvidenceAnalystContextPack(**_pack())
    with pytest.raises(ValidationError):
        pack.round = 2  # frozen
    with pytest.raises(ValidationError):
        EvidenceAnalystContextPack(**_pack(), unknown_field=True)  # extra forbidden


def test_context_pack_hashes_must_be_hex_strings() -> None:
    with pytest.raises(ValidationError):
        EvidenceAnalystContextPack(**_pack(context_pack_sha256="not-hex"))
    with pytest.raises(ValidationError):
        EvidenceAnalystContextPack(**_pack(rendered_messages_sha256="x" * 63))
    pack = EvidenceAnalystContextPack(**_pack())
    assert len(pack.context_pack_sha256) == 64
    assert len(pack.rendered_messages_sha256) == 64


def test_round_one_prior_semantic_fields_are_empty() -> None:
    with pytest.raises(ValidationError):
        EvidenceAnalystContextPack(
            **_pack(round=1, prior_counter_evidence_ids=("corpus:chunk:0002",))
        )
    with pytest.raises(ValidationError):
        EvidenceAnalystContextPack(
            **_pack(round=1, previously_supported_hypothesis_ids=("h-1",))
        )
    pack = EvidenceAnalystContextPack(**_pack(round=1))
    assert pack.prior_counter_evidence_ids == ()
    assert pack.prior_semantic_conflicts == ()
    assert pack.previously_supported_hypothesis_ids == ()
    assert pack.unresolved_gap_ids == ()


def test_round_two_may_carry_prior_semantic_references() -> None:
    pack = EvidenceAnalystContextPack(
        **_pack(
            round=2,
            prior_counter_evidence_ids=("corpus:chunk:0002",),
            prior_semantic_conflicts=("semantic:conflict:1",),
            previously_supported_hypothesis_ids=("h-1",),
            unresolved_gap_ids=("gap:1",),
            delta_evidence_ids=("corpus:chunk:0003",),
        )
    )
    assert pack.round == 2
    assert pack.prior_counter_evidence_ids == ("corpus:chunk:0002",)

"""V1.1 EvidenceAnalystContextPack contract tests (M2-5, corrective).

Phase 3 TSD §20 complete identity/hash contract. Role arrays are deterministic
views over one evidence inventory; one evidence ID cannot appear in multiple
role arrays; round-one prior assessment context is absent; pack/render/prompt
hashes are hex strings.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.trading_calendar import session_close_utc

from catalyst_agents.attribution.context_pack import (
    ContextBudget,
    EvidenceAnalystContextPack,
    EvidencePayloadItem,
    PriorAssessmentContext,
    TokenCountReport,
    TruncationRecord,
)
from catalyst_agents.attribution.coverage import CoverageSummary
from catalyst_agents.attribution.move_profile import MoveProfile
from catalyst_agents.retrieval.task import ResearchTask


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _temporal() -> TemporalIdentity:
    monday_close = _utc(session_close_utc("2026-01-05"))
    tuesday_close = _utc(session_close_utc("2026-01-06"))
    return TemporalIdentity(
        session_date="2026-01-06",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-06T14:30:00Z"),
        session_close_at=tuesday_close,
        information_window_start_at=monday_close,
        cutoff_at=tuesday_close,
    )


def _runtime() -> DataRuntimeIdentity:
    return DataRuntimeIdentity(
        data_snapshot_id="snapshot:7a004",
        corpus_manifest_id="c" * 64,
        fts_index_version="fts:v3",
        query_policy_version="qp:v1",
    )


def _payload_item(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "evidence_id": "corpus:chunk:0001",
        "canonical_asset_id": "issuer:AAPL:news:0001",
        "canonical_content_version_id": "content:v1:0001",
        "corpus_document_id": "corpus:doc:0001",
        "chunk_id": "corpus:chunk:0001",
        "section_key": None,
        "chunk_ordinal": None,
        "source_class": "issuer_disclosure",
        "evidence_role": "DIRECT_PRIMARY",
        "eligible_at": _utc("2026-01-05T21:05:00Z"),
        "content_state": "FULL_TEXT",
        "material_capability": "MATERIAL_CAPABLE",
        "independence_group_id": None,
        "independence_status": "UNKNOWN",
        "content_hash": "d" * 64,
        "excerpt_text": "AAPL reported record quarterly revenue.",
        "source_start_offset": 0,
        "source_end_offset": 44,
        "tokenizer_identity": "registered:test",
    }
    base.update(overrides)
    return base


def _budget() -> ContextBudget:
    return ContextBudget(
        model_context_limit=128_000,
        reserved_output_tokens=2_000,
        reserved_system_instruction_tokens=1_000,
        observation_tokens=300,
        coverage_summary_tokens=200,
        research_history_tokens=100,
        inventory_tokens=500,
        evidence_payload_tokens=60_000,
        per_news_item_max_tokens=800,
        per_sec_chunk_max_tokens=1_200,
        lead_only_tokens=1_000,
        safety_margin_tokens=2_000,
    )


def _token_report() -> TokenCountReport:
    return TokenCountReport(
        tokenizer_identity="registered:test",
        rendered_messages_tokens=5_000,
        reserved_output_tokens=2_000,
        safety_margin_tokens=2_000,
        remaining_payload_tokens=119_000,
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
        reported_news_asset_count=0,
        commentary_lead_asset_count=0,
        unknown_role_asset_count=0,
        eligible_reported_news_group_count=0,
        unknown_independence_asset_count=0,
        known_duplicate_or_syndicated_asset_count=0,
        content_state_counts={"FULL_TEXT": 1},
        parse_degraded_count=0,
        retrieval_degradations=(),
        data_coverage_gaps=(),
        capability_gaps=(),
        independence_groups=(),
        market_alignment=None,
        sector_alignment=None,
        peer_alignment=None,
        scheduled_macro_present=None,
    )


def _pack(**overrides: Any) -> dict[str, Any]:
    item = EvidencePayloadItem(**_payload_item())
    base: dict[str, Any] = {
        "schema_version": "v1",
        "packing_policy_version": "p1",
        "run_id": "run:1",
        "round": 1,
        "temporal_identity": _temporal(),
        "data_runtime_identity": _runtime(),
        "context_budget": _budget(),
        "token_count_report": _token_report(),
        "hard_constraints": ("materiality_gate", "cutoff_gate"),
        "observation": MoveProfile(target_return=3.2),
        "coverage_summary": _coverage(),
        "research_history": (
            ResearchTask(
                schema_version="1.0",
                task_id="research:1:1:a1b2c3",
                round=1,
                priority=1,
                scenario="COMPANY_SPECIFIC",
                evidence_need="COMPANY_PRIMARY",
                time_scope="SESSION_INFORMATION_WINDOW",
                ticker_scope=("AAPL",),
                source_classes=("issuer_disclosure",),
                evidence_types=("full_text",),
                query_hints=("AAPL guidance",),
                retrieval_policy_id="qp:v1",
                task_fingerprint="b" * 64,
            ),
        ),
        "evidence_inventory": (item,),
        "direct_primary_evidence": (item,),
        "primary_authority_evidence": (),
        "independent_reports": (),
        "lead_only_evidence": (),
        "structured_context": (),
        "deterministic_conflict_signals": (),
        "data_coverage_gaps": (),
        "capability_gaps": (),
        "retrieval_degradations": (),
        "included_evidence_ids": ("corpus:chunk:0001",),
        "excluded_evidence_ids": (),
        "truncation_metadata": (),
        "delta_evidence_ids": (),
        "prior_assessment_context": None,
        "context_pack_sha256": "a" * 64,
        "prompt_template_version": "pt:v1",
        "prompt_template_sha256": "f" * 64,
        "rendered_messages_sha256": "e" * 64,
    }
    base.update(overrides)
    return base


def test_context_pack_fields_match_phase_3_section_20() -> None:
    pack = EvidenceAnalystContextPack(**_pack())
    assert set(EvidenceAnalystContextPack.model_fields) == {
        "schema_version",
        "packing_policy_version",
        "run_id",
        "round",
        "temporal_identity",
        "data_runtime_identity",
        "context_budget",
        "token_count_report",
        "hard_constraints",
        "observation",
        "coverage_summary",
        "research_history",
        "evidence_inventory",
        "direct_primary_evidence",
        "primary_authority_evidence",
        "independent_reports",
        "lead_only_evidence",
        "structured_context",
        "deterministic_conflict_signals",
        "data_coverage_gaps",
        "capability_gaps",
        "retrieval_degradations",
        "included_evidence_ids",
        "excluded_evidence_ids",
        "truncation_metadata",
        "delta_evidence_ids",
        "prior_assessment_context",
        "context_pack_sha256",
        "prompt_template_version",
        "prompt_template_sha256",
        "rendered_messages_sha256",
    }
    assert pack.run_id == "run:1"
    assert pack.temporal_identity.session_date == "2026-01-06"
    assert pack.data_runtime_identity.data_snapshot_id == "snapshot:7a004"
    assert pack.prompt_template_version == "pt:v1"


def test_context_pack_is_strict_and_frozen() -> None:
    pack = EvidenceAnalystContextPack(**_pack())
    with pytest.raises(ValidationError):
        pack.round = 2  # frozen
    with pytest.raises(ValidationError):
        EvidenceAnalystContextPack(**_pack(), unknown_field=True)  # extra forbidden


def test_context_pack_hashes_must_be_hex_strings() -> None:
    with pytest.raises(ValidationError):
        EvidenceAnalystContextPack(**_pack(context_pack_sha256="not-a-hash"))
    with pytest.raises(ValidationError):
        EvidenceAnalystContextPack(**_pack(prompt_template_sha256="Z" * 64))
    with pytest.raises(ValidationError):
        EvidenceAnalystContextPack(**_pack(rendered_messages_sha256="short"))


def test_round_one_prior_assessment_context_is_absent() -> None:
    pack = EvidenceAnalystContextPack(**_pack())
    assert pack.prior_assessment_context is None
    with pytest.raises(ValidationError):
        EvidenceAnalystContextPack(
            **_pack(
                prior_assessment_context=PriorAssessmentContext(
                    prior_counter_evidence_ids=("corpus:chunk:0002",),
                    prior_semantic_conflicts=(),
                    previously_supported_hypothesis_ids=(),
                    unresolved_gap_ids=(),
                )
            )
        )


def test_round_two_may_carry_prior_assessment_context() -> None:
    prior = PriorAssessmentContext(
        prior_counter_evidence_ids=("corpus:chunk:0002",),
        prior_semantic_conflicts=("conflict:1",),
        previously_supported_hypothesis_ids=("hyp:1",),
        unresolved_gap_ids=("gap:1",),
    )
    pack = EvidenceAnalystContextPack(**_pack(round=2, prior_assessment_context=prior))
    assert pack.prior_assessment_context.prior_counter_evidence_ids == ("corpus:chunk:0002",)


def test_role_arrays_are_views_over_one_inventory() -> None:
    item = EvidencePayloadItem(**_payload_item())
    other = EvidencePayloadItem(
        **_payload_item(
            evidence_id="corpus:chunk:0002",
            chunk_id="corpus:chunk:0002",
            excerpt_text="Second chunk.",
        )
    )
    with pytest.raises(ValidationError):
        EvidenceAnalystContextPack(
            **_pack(
                evidence_inventory=(item,),
                direct_primary_evidence=(other,),  # not in inventory
            )
        )
    with pytest.raises(ValidationError):
        EvidenceAnalystContextPack(
            **_pack(
                evidence_inventory=(item, other),
                direct_primary_evidence=(item,),
                independent_reports=(item,),  # same ID in two role arrays
            )
        )


def test_structured_context_is_typed_fact_view() -> None:
    fact = EvidencePayloadItem(
        **_payload_item(
            evidence_id="fact:42",
            chunk_id=None,
            source_class="structured_market_data",
            evidence_role="STRUCTURED_CONTEXT",
            excerpt_text="AAPL P/E 32.1",
        )
    )
    pack = EvidenceAnalystContextPack(
        **_pack(
            evidence_inventory=(fact,),
            direct_primary_evidence=(),
            structured_context=(fact,),
        )
    )
    assert pack.structured_context[0].evidence_id == "fact:42"


def test_truncation_record_and_budget_are_typed() -> None:
    record = TruncationRecord(
        evidence_id="corpus:chunk:0001",
        action="INCLUDED_TRUNCATED",
        included_token_count=200,
        source_start_offset=0,
        source_end_offset=120,
        tokenizer_identity="registered:test",
        reason_code="per_item_cap",
    )
    assert record.action == "INCLUDED_TRUNCATED"
    with pytest.raises(ValidationError):
        TruncationRecord(
            evidence_id="corpus:chunk:0001",
            action="SLICED",  # not a closed action
            included_token_count=200,
            tokenizer_identity="registered:test",
            reason_code="x",
        )
    budget = _budget()
    assert budget.model_context_limit == 128_000
    with pytest.raises(ValidationError):
        ContextBudget(**_budget_dict(model_context_limit=-1))


def _budget_dict(**overrides: Any) -> dict[str, Any]:
    return {
        "model_context_limit": 128_000,
        "reserved_output_tokens": 2_000,
        "reserved_system_instruction_tokens": 1_000,
        "observation_tokens": 300,
        "coverage_summary_tokens": 200,
        "research_history_tokens": 100,
        "inventory_tokens": 500,
        "evidence_payload_tokens": 60_000,
        "per_news_item_max_tokens": 800,
        "per_sec_chunk_max_tokens": 1_200,
        "lead_only_tokens": 1_000,
        "safety_margin_tokens": 2_000,
        **overrides,
    }

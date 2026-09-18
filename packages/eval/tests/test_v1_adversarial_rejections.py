"""M2 corrective adversarial rejection gates (Phase 8).

Every reproduced false-accept case from the reconciliation amendment must fail
validation. eval is the offline consumer and may import production contracts
in tests; production packages never import eval (covered separately).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_agents.attribution.analyst import (
    AnalystDecision,
    CandidateHypothesis,
    EvidenceDecision,
)
from catalyst_agents.attribution.claims import (
    Claim,
    SupportingSnippet,
    SourceRoleIndependenceSummary,
    ValidatedClaimPlan,
    WriterFormatStyleContract,
    WriterInput,
)
from catalyst_agents.retrieval.corrective import (
    CorrectiveResearchAction,
    CorrectiveResearchBatch,
    MissingEvidence,
)
from catalyst_agents.attribution.context_pack import ContextBudget
from catalyst_agents.runtime.manifest import (
    ObservationPolicyConfig,
    RunManifest,
    RuntimeConfiguration,
)
from catalyst_app.api_dto import RunDTO
from catalyst_app.events import PublicRunEvent, RunAcceptedPayload
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.trading_calendar import session_close_utc
from catalyst_eval.v1_1.manifest import EvalManifest, EvalOutcome


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


def _runtime_config() -> RuntimeConfiguration:
    return RuntimeConfiguration(
        observation_policy=ObservationPolicyConfig(
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
        ),
        context_budget=ContextBudget(
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
        ),
    )


def _manifest(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "run:1",
        "request_hash": "e" * 64,
        "temporal_identity": _temporal(),
        "data_runtime_identity_ref": "runtime-id:7a004",
        "data_runtime_identity_hash": "f" * 64,
        "code_revision": "17693fa",
        "workflow_version": "v1.1",
        "policy_version": "p1",
        "analyst_model_id": "model-analyst",
        "analyst_prompt_hash": "a" * 64,
        "writer_model_id": "model-writer",
        "writer_prompt_hash": "b" * 64,
        "context_pack_schema_version": "v1",
        "packing_policy_version": "p1",
        "context_token_budget": 4000,
        "tokenizer_policy": "registered-bge-m3",
        "hypothesis_schema_version": "v1",
        "claim_schema_version": "v1",
        "max_corrective_rounds": 1,
        "max_actions_per_batch": 1,
        "run_timeout_seconds": 60,
        "provider_capability_revision": "cap:v1",
        "runtime_configuration": _runtime_config(),
    }
    base.update(overrides)
    return base


def _decision_base() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "evidence_decisions": (),
        "candidate_hypotheses": (),
        "conflicts": (),
        "proposed_missing_evidence": (),
        "research_decision": "READY",
        "recommended_status": "ABSTAIN",
        "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
        "proposed_corrective_intents": (),
    }


def test_raw_analyst_decision_with_hypothesis_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AnalystDecision(
            **_decision_base(),
            candidate_hypotheses=(
                CandidateHypothesis(
                    hypothesis_id="hyp:1",
                    cause_type="COMPANY_SPECIFIC_CATALYST",
                    statement="AAPL rose on guidance.",
                    magnitude_fit="PLAUSIBLE",
                    proposed_role="PRIMARY",
                ),
            ),
        )


def test_analyst_decision_without_schema_version_is_rejected() -> None:
    data = _decision_base()
    del data["schema_version"]
    with pytest.raises(ValidationError):
        AnalystDecision(**data)


def test_recoverable_market_structure_gap_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MissingEvidence(
            gap_id="gap:9",
            evidence_need="MARKET_STRUCTURE",
            time_scope="SESSION_INFORMATION_WINDOW",
            expected_information="positioning data",
            reason_code="MARKET_STRUCTURE_UNSUPPORTED",
            recoverable=True,
        )


def test_market_structure_corrective_action_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CorrectiveResearchAction(
            action_id="action:9",
            gap_id="gap:9",
            evidence_need="MARKET_STRUCTURE",
            time_scope="SESSION_INFORMATION_WINDOW",
            query_hints=("short interest",),
            research_fingerprint="fp:9",
        )


def test_empty_corrective_batch_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CorrectiveResearchBatch(
            batch_id="batch:9",
            run_id="run:1",
            round=1,
            actions=(),
            shared_deadline=_utc("2026-01-06T21:00:00Z"),
            internal_deadline=_utc("2026-01-06T21:00:00Z"),
            total_result_budget=20,
            policy_version="cp:v1",
        )


def _canonical_validated_plan() -> ValidatedClaimPlan:
    """Canonical Phase 4 §27 plan used by the Writer bypass reproductions."""
    return ValidatedClaimPlan(
        status="PARTIAL",
        attribution_type="EVIDENCE_BACKED_CAUSAL",
        claims=(
            Claim(
                claim_id="claim-1",
                role="PRIMARY",
                statement="AAPL rose on guidance.",
                support_evidence_ids=("corpus:chunk:0001",),
                citation_evidence_ids=("corpus:chunk:0001",),
                source_hypothesis_id="hyp:1",
                magnitude_fit="STRONG",
                order_index=0,
            ),
        ),
        assessment_hash="a" * 64,
        context_pack_sha256="b" * 64,
        evidence_state_hash="c" * 64,
        required_limitations=("magnitude coverage is partial",),
        citation_map={"claim-1": ("corpus:chunk:0001",)},
        permitted_claim_ids=("claim-1",),
        permitted_evidence_ids=("corpus:chunk:0001",),
        source_role_independence_summary=SourceRoleIndependenceSummary(),
        ordering_policy_version="op:v1",
        plan_hash="d" * 64,
    )


def _canonical_writer_input(**overrides) -> dict:
    base = dict(
        final_status="PARTIAL",
        attribution_type="EVIDENCE_BACKED_CAUSAL",
        observed_move="AAPL +3.2%",
        validated_claim_plan=_canonical_validated_plan(),
        narrowly_bound_supporting_snippets=(
            SupportingSnippet(
                evidence_id="corpus:chunk:0001", snippet_text="guidance",
                content_sha256="a" * 64, source_start_offset=0, source_end_offset=8,
            ),
        ),
        citation_map={"claim-1": ("corpus:chunk:0001",)},
        required_limitations=("magnitude coverage is partial",),
        format_style_contract=WriterFormatStyleContract(
            required_sections=("SUMMARY",),
            style_instructions=("neutral tone",),
        ),
    )
    base.update(overrides)
    return base


def test_abstain_with_primary_causal_claim_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WriterInput(
            final_status="ABSTAIN",
            attribution_type="EVIDENCE_BACKED_CAUSAL",
            observed_move="AAPL +3.2%",
            validated_claim_plan=ValidatedClaimPlan(
                status="ABSTAIN",
                attribution_type="EVIDENCE_BACKED_CAUSAL",
                claims=(
                    Claim(
                        claim_id="claim-1",
                        role="PRIMARY",
                        statement="AAPL rose on guidance.",
                        support_evidence_ids=("corpus:chunk:0001",),
                        citation_evidence_ids=("corpus:chunk:0001",),
                        source_hypothesis_id="hyp:1",
                        magnitude_fit="STRONG",
                        order_index=0,
                    ),
                ),
                assessment_hash="a" * 64,
                context_pack_sha256="b" * 64,
                evidence_state_hash="c" * 64,
                required_limitations=(),
                citation_map={"claim-1": ("corpus:chunk:0001",)},
                permitted_claim_ids=("claim-1",),
                permitted_evidence_ids=("corpus:chunk:0001",),
                source_role_independence_summary=SourceRoleIndependenceSummary(),
                ordering_policy_version="op:v1",
                plan_hash="d" * 64,
            ),
            narrowly_bound_supporting_snippets={
                "corpus:chunk:0001": "guidance"
            },
            citation_map={"claim-1": ("corpus:chunk:0001",)},
            required_limitations=(),
            format_style_contract=WriterFormatStyleContract(
                required_sections=("limitations",),
                style_instructions=("neutral tone",),
            ),
        )


def test_writer_public_surface_bypasses_are_rejected() -> None:
    # Unbound snippet key outside the validated permitted/citation surface.
    with pytest.raises(ValidationError):
        WriterInput(
            **_canonical_writer_input(
                narrowly_bound_supporting_snippets={
                    "corpus:chunk:0001": "guidance",
                    "corpus:chunk:9999": "unbound text",
                }
            )
        )
    # Writer limitations differ from the validated plan limitations.
    with pytest.raises(ValidationError):
        WriterInput(
            **_canonical_writer_input(
                required_limitations=("a different limitation",)
            )
        )
    # Writer omits the required format/style contract.
    writer = _canonical_writer_input()
    del writer["format_style_contract"]
    with pytest.raises(ValidationError):
        WriterInput(**writer)
    # Citation-map relation differs from the plan/claim citation relation.
    with pytest.raises(ValidationError):
        WriterInput(
            **_canonical_writer_input(
                citation_map={"claim-1": ()}
            )
        )


def test_event_payload_with_api_key_or_raw_provider_response_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PublicRunEvent(
            schema_version="v1",
            run_id="run:1",
            sequence=1,
            event_type="run.accepted",
            emitted_at=_utc("2026-01-06T14:00:00Z"),
            stage="ADMISSION",
            payload={
                "request_identity_digest": "a" * 64,
                "ticker": "AAPL",
                "trade_date": "2026-01-06",
                "workflow_version": "v1.1",
                "model_provider_label": "t",
                "stream_url": "/api/live-runs/run:1/stream",
                "api_key": "sk-secret",
            },
        )
    with pytest.raises(ValidationError):
        PublicRunEvent(
            schema_version="v1",
            run_id="run:1",
            sequence=1,
            event_type="run.accepted",
            emitted_at=_utc("2026-01-06T14:00:00Z"),
            stage="ADMISSION",
            payload={
                "request_identity_digest": "a" * 64,
                "ticker": "AAPL",
                "trade_date": "2026-01-06",
                "workflow_version": "v1.1",
                "model_provider_label": "t",
                "stream_url": "/api/live-runs/run:1/stream",
                "raw_provider_response": {"choices": []},
            },
        )


def test_event_payload_larger_than_64_kib_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PublicRunEvent(
            schema_version="v1",
            run_id="run:1",
            sequence=1,
            event_type="run.accepted",
            emitted_at=_utc("2026-01-06T14:00:00Z"),
            stage="ADMISSION",
            payload=RunAcceptedPayload(
                request_identity_digest="z" * 70_000,
                ticker="AAPL",
                trade_date="2026-01-06",
                workflow_version="v1.1",
                model_provider_label="t",
                stream_url="/api/live-runs/run:1/stream",
            ),
        )


def test_failed_run_dto_with_sufficient_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RunDTO(
            run_id="run:1",
            lifecycle_status="FAILED",
            attribution_status="SUFFICIENT",
            created_at=_utc("2026-01-06T14:00:00Z"),
        )


def test_run_manifest_with_99_corrective_rounds_and_actions_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(max_corrective_rounds=99))
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(max_actions_per_batch=99))


def test_run_manifest_negative_timeout_and_token_budget_are_rejected() -> None:
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(run_timeout_seconds=-60))
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(context_token_budget=-4000))


def test_second_eval_manifest_outcome_append_is_rejected() -> None:
    from catalyst_eval.v1_1.manifest import (
        LatencyTokensCost,
    )

    manifest = EvalManifest(
        evaluation_identity={
            "eval_id": "eval:stage1:v1",
            "schema_version": "v1",
            "dataset_id": "catalyst-stage1",
            "dataset_version": "v1",
            "stage": "stage1",
            "split": "dev",
            "ordered_case_ids": ("stage1-001",),
            "case_list_sha256": "a" * 64,
        },
        code_provider_identity={
            "code_git_sha": "17693fa",
            "harness_revision": "h:v1",
            "random_seed": 42,
            "models_by_role": (),
        },
        data_identity={
            "data_runtime_identity_ref": "runtime-id:7a004",
            "data_runtime_identity_hash": "d" * 64,
        },
        run_artifact_identity={
            "run_manifest_bindings": (),
            "context_pack_refs": (),
            "claim_plan_refs": (),
            "assurance_refs": (),
        },
        retrieval_policy={
            "arm_names": ("fts5",),
            "arm_order": ("fts5",),
            "top_k": 8,
            "candidate_pool_id": "pool:1",
            "dedup_policy_version": "dedup:v1",
            "independence_policy_version": "ind:v1",
            "reranker_policy_version": "rr:v1",
        },
        agent_policy={
            "observation_policy_version": "move_profile_v1",
            "context_pack_policy_version": "evidence_context_pack_v1",
            "analyst_policy_version": "bounded_competition_v1",
            "writer_policy_version": "writer_v1",
            "a1_policy_version": "a1:v1",
            "a2_policy_version": "a2:v1",
            "a3_policy_version": "a3:v1",
            "a4_policy_version": "a4:v1",
            "a5_policy_version": "a5:v1",
        },
        metric_contract={
            "metric_spec_version": "ms:v1",
            "definitions": (),
        },
        eligible_experiments=(),
        outcome=None,
    )
    outcome = EvalOutcome(
        completed_at=_utc("2026-01-08T09:00:00Z"),
        observed_run_artifact_identity={
            "run_manifest_bindings": (
                {
                    "run_manifest_id": "manifest:run:1",
                    "run_manifest_hash": "b" * 64,
                },
            ),
            "context_pack_refs": (),
            "claim_plan_refs": (),
            "assurance_refs": (),
        },
        per_case_result_refs=(
            {
                "case_id": "stage1-001",
                "run_manifest_id": "manifest:run:1",
                "run_manifest_hash": "b" * 64,
                "result_artifact_id": "result:run:1",
            },
        ),
        aggregate_metrics=(),
        latency_tokens_cost=LatencyTokensCost(
            total_latency_ms=1,
            total_tokens=1,
            total_cost=0.0,
        ),
        gate_results=(),
    )
    completed = manifest.append_outcome(outcome)
    assert completed.outcome is not None
    with pytest.raises(ValueError):
        completed.append_outcome(outcome)

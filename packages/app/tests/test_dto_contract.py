"""V1.1 public DTO boundary + idempotency hash contract tests (M2-9, corrective).

Phase 5 TSD §21/§23: the sole public DTO boundary must not expose LangGraph
state, raw internal semantic fields, provider objects, credentials, or
internal node names. Lifecycle/result invariants, typed artifact refs,
chunk/fact identity, capability fields, health readiness, and request-hash
secret exclusion are enforced.
"""
from __future__ import annotations

import inspect
import re
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_app.api_dto import (
    ArtifactDTO,
    ArtifactRefDTO,
    CancelResponse,
    CapabilityModelDTO,
    CapabilityResponse,
    ClaimDetailDTO,
    EvidenceDetailDTO,
    HealthDTO,
    PublicRunEventDTO,
    RunAcceptedResponse,
    RunDTO,
    RunFailureDTO,
    WorkbenchProjectionDTO,
    compute_request_hash,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_agents.attribution.analyst import (
    AttributionStatus,
    AttributionType,
)


def _accepted_response(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "run:1",
        "status": "ACCEPTED",
        "stream_url": "/api/live-runs/run:1/stream",
        "request_hash_prefix": "a" * 8,
        "model_capability_label": "streaming-v1",
    }
    base.update(overrides)
    return base


def _artifact_ref(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "artifact_id": "artifact:1",
        "artifact_type": "evidence_state",
        "schema_version": "v1",
        "content_sha256": "c" * 64,
        "run_id": "run:1",
    }
    base.update(overrides)
    return base


def _evidence_detail(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "evidence_id": "corpus:chunk:0001",
        "canonical_asset_id": "issuer:AAPL:news:0001",
        "content_version_id": "content:v1:0001",
        "chunk_id": "corpus:chunk:0001",
        "fact_id": None,
        "excerpt": "AAPL reported record quarterly revenue.",
        "source_class": "reported_news",
        "content_state": "FULL_TEXT",
        "eligible_at": datetime(2026, 1, 5, 21, 5, tzinfo=timezone.utc),
        "ticker_scope": ("AAPL",),
        "provider": "polygon",
        "publisher": "example-news",
        "dedup_cluster_id": None,
    }
    base.update(overrides)
    return base


def _claim_detail(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "claim_id": "claim-1",
        "role": "PRIMARY",
        "statement": "AAPL rose on record guidance.",
        "mechanism": None,
        "support_evidence_ids": ("corpus:chunk:0001",),
        "counter_evidence_ids": (),
        "limitations": (),
        "citation_evidence_ids": ("corpus:chunk:0001",),
        "validation_status": "validated",
        "validation_codes": (),
    }
    base.update(overrides)
    return base


ALL_DTO_CLASSES = [
    RunAcceptedResponse,
    RunDTO,
    CancelResponse,
    PublicRunEventDTO,
    ArtifactRefDTO,
    ArtifactDTO,
    EvidenceDetailDTO,
    ClaimDetailDTO,
    WorkbenchProjectionDTO,
    CapabilityResponse,
    CapabilityModelDTO,
    HealthDTO,
    RunFailureDTO,
]


def test_run_accepted_response_contract() -> None:
    response = RunAcceptedResponse(**_accepted_response())
    assert response.status == "ACCEPTED"
    assert response.stream_url.startswith("/api/live-runs/")
    assert response.request_hash_prefix == "a" * 8
    with pytest.raises(ValidationError):
        RunAcceptedResponse(**_accepted_response(status="QUEUED"))
    with pytest.raises(ValidationError):
        RunAcceptedResponse(**_accepted_response(), unknown_field=True)


def test_dto_models_are_strict() -> None:
    for cls in ALL_DTO_CLASSES:
        assert cls.model_config.get("extra") == "forbid"


def test_dtos_do_not_expose_internal_semantic_fields() -> None:
    forbidden = {
        "retrieved_chunks",
        "reranked_chunks",
        "critic_reasoning",
        "causes",
        "summary_md",
        "api_key",
        "credentials",
        "secret",
        "raw_llm_response",
        "state_snapshot",
        "headers",
    }
    for cls in ALL_DTO_CLASSES:
        fields = set(cls.model_fields)
        overlap = fields & forbidden
        assert not overlap, f"{cls.__name__} exposes forbidden fields {overlap}"


def test_evidence_detail_identity_invariants() -> None:
    evidence = EvidenceDetailDTO(**_evidence_detail())
    assert evidence.evidence_id == "corpus:chunk:0001"
    with pytest.raises(ValidationError):
        EvidenceDetailDTO(**{**_evidence_detail(), "fact_id": "fact:1"})  # both chunk+fact
    with pytest.raises(ValidationError):
        EvidenceDetailDTO(
            **_evidence_detail(
                evidence_id="corpus:chunk:9999",
            )
        )  # text evidence_id must equal chunk_id
    with pytest.raises(ValidationError):
        EvidenceDetailDTO(**_evidence_detail(), critic_reasoning="hidden")


def test_artifact_refs_retain_version_hash_and_same_run_metadata() -> None:
    ref = ArtifactRefDTO(**_artifact_ref())
    assert ref.schema_version == "v1"
    assert ref.content_sha256 == "c" * 64
    assert ref.run_id == "run:1"
    with pytest.raises(ValidationError):
        ArtifactRefDTO(**_artifact_ref(content_sha256="not-hex"))
    with pytest.raises(ValidationError):
        ArtifactRefDTO(**{k: v for k, v in _artifact_ref().items() if k != "schema_version"})
    with pytest.raises(ValidationError):
        ArtifactRefDTO(**{k: v for k, v in _artifact_ref().items() if k != "content_sha256"})
    artifact = ArtifactDTO(
        artifact_id="artifact:1",
        artifact_type="evidence_state",
        stage="INITIAL_RESEARCH",
        created_at=datetime(2026, 1, 6, 14, 0, tzinfo=timezone.utc),
        ref=ArtifactRefDTO(**_artifact_ref()),
    )
    assert artifact.ref.schema_version == "v1"
    with pytest.raises(ValidationError):
        ArtifactDTO(
            artifact_id="artifact:other", artifact_type="evidence_state",
            ref=ArtifactRefDTO(**_artifact_ref()),
        )


def test_claim_detail_retains_authoritative_relations() -> None:
    claim = ClaimDetailDTO(**_claim_detail())
    assert claim.support_evidence_ids == ("corpus:chunk:0001",)
    assert claim.citation_evidence_ids == ("corpus:chunk:0001",)
    assert claim.validation_status == "validated"


def test_workbench_projection_has_no_raw_graph_state() -> None:
    projection = WorkbenchProjectionDTO(
        run_id="run:1",
        lifecycle_status="COMPLETED",
        attribution_status="PARTIAL",
        attribution_type="EVIDENCE_BACKED_CAUSAL",
        claims=(ClaimDetailDTO(**_claim_detail()),),
        evidence=(EvidenceDetailDTO(**_evidence_detail()),),
        answer="AAPL rose on record guidance.",
        limitations=("magnitude coverage is partial",),
        artifact_refs=(ArtifactRefDTO(**_artifact_ref()),),
    )
    assert projection.lifecycle_status == "COMPLETED"
    fields = set(WorkbenchProjectionDTO.model_fields)
    assert "retrieved_chunks" not in fields
    assert "critic_reasoning" not in fields


def test_run_dto_non_completed_states_cannot_carry_attribution() -> None:
    with pytest.raises(ValidationError):
        RunDTO(
            run_id="run:1",
            lifecycle_status="RUNNING",
            attribution_status="PARTIAL",
            created_at=datetime(2026, 1, 6, 14, 0, tzinfo=timezone.utc),
        )
    with pytest.raises(ValidationError):
        RunDTO(
            run_id="run:1",
            lifecycle_status="FAILED",
            attribution_status="SUFFICIENT",
            created_at=datetime(2026, 1, 6, 14, 0, tzinfo=timezone.utc),
        )
    with pytest.raises(ValidationError):
        WorkbenchProjectionDTO(
            run_id="run:1",
            lifecycle_status="RUNNING",
            attribution_status="PARTIAL",
        )
    valid = RunDTO(
        run_id="run:1",
        lifecycle_status="COMPLETED",
        attribution_status="ABSTAIN",
        attribution_type="EVIDENCE_BACKED_CAUSAL",
        created_at=datetime(2026, 1, 6, 14, 0, tzinfo=timezone.utc),
    )
    assert valid.attribution_status is AttributionStatus.ABSTAIN


def test_run_dto_terminal_artifact_and_failure_contract() -> None:
    with pytest.raises(ValidationError):
        RunDTO(
            run_id="run:1", lifecycle_status="COMPLETED", created_at=datetime.now(timezone.utc),
            failure=RunFailureDTO(code="unexpected"),
        )


def test_run_failure_message_uses_safe_public_text_contract() -> None:
    unsafe = "provider failure api_key=sk-abcdefghijklmnopqrstuvwxyz012345"
    with pytest.raises(ValidationError) as exc_info:
        RunFailureDTO(code="provider_failure", message=unsafe)
    assert unsafe not in str(exc_info.value)


def test_workbench_projection_artifact_refs_must_belong_to_run() -> None:
    with pytest.raises(ValidationError):
        WorkbenchProjectionDTO(
            run_id="run:1", lifecycle_status="COMPLETED",
            artifact_refs=(ArtifactRefDTO(**_artifact_ref(run_id="run:other")),),
        )
    with pytest.raises(ValidationError):
        RunDTO(
            run_id="run:1", lifecycle_status="FAILED", created_at=datetime.now(timezone.utc),
            duration_ms=-1,
        )
    with pytest.raises(ValidationError):
        RunDTO(
            run_id="run:1", lifecycle_status="COMPLETED", created_at=datetime.now(timezone.utc),
            terminal_artifact_refs=(ArtifactRefDTO(**_artifact_ref(run_id="run:other")),),
        )


def test_capability_model_uses_exact_phase_5_23_2_fields() -> None:
    capability = CapabilityModelDTO(
        provider="anthropic",
        model_id="claude-x",
        executable=True,
        writer_streaming=True,
        analyst_structured_output=True,
        cancellation="supported",
        token_usage="provider_reported",
        timeout_seconds=120,
        offline_only=False,
        readiness="ready",
        reason_code=None,
        capability_probe_at=datetime(2026, 1, 6, 14, 0, tzinfo=timezone.utc),
        revision="cap:v1",
    )
    assert capability.writer_streaming is True
    assert capability.analyst_structured_output is True
    assert capability.revision == "cap:v1"
    fields = set(CapabilityModelDTO.model_fields)
    assert fields == {
        "provider",
        "model_id",
        "executable",
        "writer_streaming",
        "analyst_structured_output",
        "cancellation",
        "token_usage",
        "timeout_seconds",
        "offline_only",
        "readiness",
        "reason_code",
        "capability_probe_at",
        "revision",
    }
    response = CapabilityResponse(models=(capability,))
    assert response.models[0].readiness == "ready"


def test_health_dto_types_process_executor_runtime_db_and_event_store() -> None:
    health = HealthDTO(
        status="ready",
        process="ready",
        executor="ready",
        runtime_db="ready",
        event_store="ready",
    )
    assert health.status == "ready"
    assert health.event_store == "ready"
    with pytest.raises(ValidationError):
        HealthDTO(status="unknown", process="ready", executor="ready", runtime_db="ready", event_store="ready")
    with pytest.raises(ValidationError):
        HealthDTO(status="ready", process="ready", executor="ready", runtime_db="ready", event_store="nope")


def test_compute_request_hash_is_stable_sha256_over_canonical_json() -> None:
    kwargs = dict(
        ticker="AAPL",
        session_date="2026-01-06",
        normalized_question="why did AAPL move?",
        provider="anthropic",
        model_id="claude-x",
        normalized_base_url="https://api.example.com",
        credential_source_identifier="local:env",
        workflow_version="v1.1",
        config_version="p1",
    )
    first = compute_request_hash(**kwargs)
    second = compute_request_hash(**kwargs)
    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first)


def test_request_hash_excludes_secrets_idempotency_key_and_transport() -> None:
    params = set(inspect.signature(compute_request_hash).parameters)
    assert "api_key" not in params
    assert "credentials" not in params
    assert "idempotency_key" not in params
    assert "headers" not in params
    kwargs = dict(
        ticker="AAPL",
        session_date="2026-01-06",
        normalized_question="why did AAPL move?",
        provider="anthropic",
        model_id="claude-x",
        normalized_base_url="https://api.example.com",
        credential_source_identifier="local:env",
        workflow_version="v1.1",
        config_version="p1",
    )
    with pytest.raises(TypeError):
        compute_request_hash(**kwargs, api_key="super-secret")  # type: ignore[call-arg]


def test_request_hash_changes_when_semantic_input_changes() -> None:
    base = dict(
        ticker="AAPL",
        session_date="2026-01-06",
        normalized_question="why did AAPL move?",
        provider="anthropic",
        model_id="claude-x",
        normalized_base_url="https://api.example.com",
        credential_source_identifier="local:env",
        workflow_version="v1.1",
        config_version="p1",
    )
    assert compute_request_hash(**base) != compute_request_hash(
        **{**base, "normalized_question": "why did MSFT move?"}
    )
    assert compute_request_hash(**base) != compute_request_hash(
        **{**base, "ticker": "MSFT"}
    )

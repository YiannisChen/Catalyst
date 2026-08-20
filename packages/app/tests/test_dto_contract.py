"""V1.1 public DTO boundary + idempotency hash contract tests (M2-9).

Final Migration TSD §20/§21 and Phase 5 TSD §21: the sole public DTO boundary
must not expose LangGraph state, raw internal semantic fields, provider
objects, credentials, or internal node names. compute_request_hash is a
canonical-JSON SHA-256 over the documented fields only.
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
    }
    for cls in ALL_DTO_CLASSES:
        fields = set(cls.model_fields)
        overlap = fields & forbidden
        assert not overlap, f"{cls.__name__} exposes forbidden fields {overlap}"


def test_evidence_detail_and_claim_detail_are_bounded_projections() -> None:
    evidence = EvidenceDetailDTO(**_evidence_detail())
    assert evidence.evidence_id == "corpus:chunk:0001"
    claim = ClaimDetailDTO(**_claim_detail())
    assert claim.support_evidence_ids == ("corpus:chunk:0001",)
    with pytest.raises(ValidationError):
        EvidenceDetailDTO(**_evidence_detail(), critic_reasoning="hidden")


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
        artifact_refs=("artifact:1",),
    )
    assert projection.lifecycle_status == "COMPLETED"
    fields = set(WorkbenchProjectionDTO.model_fields)
    assert "retrieved_chunks" not in fields
    assert "critic_reasoning" not in fields


def test_capability_and_health_dtos() -> None:
    capability = CapabilityModelDTO(
        provider="anthropic",
        model_id="claude-x",
        streaming=True,
        structured_output=True,
        cancellation=True,
        token_accounting=True,
        readiness=True,
    )
    response = CapabilityResponse(models=(capability,))
    assert response.models[0].streaming is True
    health = HealthDTO(status="ready", components={"sqlite": "ready"})
    assert health.status == "ready"
    with pytest.raises(ValidationError):
        HealthDTO(status="unknown", components={})


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

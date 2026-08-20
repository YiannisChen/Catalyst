"""V1.1 RunManifest contract tests (M2-7, corrective).

Agents-owned production run identity. It references DataRuntimeIdentity by
ref/hash, never duplicates an independently recomputed data identity, enforces
the production corrective bounds (1 round, 1 action), enforces the initial
60-second run deadline, rejects zero/negative token/deadline/budget values,
and uses a typed runtime configuration (Final Migration TSD §14–15).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_agents.runtime.manifest import (
    ContextBudgetPolicy,
    ObservationPolicyConfig,
    RunManifest,
    RuntimeConfiguration,
)
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.trading_calendar import session_close_utc


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
        context_budget=ContextBudgetPolicy(
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


def test_run_manifest_fields() -> None:
    manifest = RunManifest(**_manifest())
    assert set(RunManifest.model_fields) == {
        "run_id",
        "request_hash",
        "temporal_identity",
        "data_runtime_identity_ref",
        "data_runtime_identity_hash",
        "code_revision",
        "workflow_version",
        "policy_version",
        "analyst_model_id",
        "analyst_prompt_hash",
        "writer_model_id",
        "writer_prompt_hash",
        "context_pack_schema_version",
        "packing_policy_version",
        "context_token_budget",
        "tokenizer_policy",
        "hypothesis_schema_version",
        "claim_schema_version",
        "max_corrective_rounds",
        "max_actions_per_batch",
        "run_timeout_seconds",
        "provider_capability_revision",
        "runtime_configuration",
    }
    assert manifest.temporal_identity.session_date == "2026-01-06"


def test_run_manifest_production_freeze_bounds_are_enforced() -> None:
    assert RunManifest(**_manifest()).max_corrective_rounds == 1
    assert RunManifest(**_manifest()).max_actions_per_batch == 1
    assert RunManifest(**_manifest()).run_timeout_seconds == 60
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(max_corrective_rounds=99))
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(max_actions_per_batch=2))
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(run_timeout_seconds=300))


def test_run_manifest_rejects_zero_or_negative_deadline_budget_values() -> None:
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(run_timeout_seconds=0))
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(run_timeout_seconds=-60))
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(context_token_budget=0))
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(context_token_budget=-1))


def test_run_manifest_uses_typed_runtime_configuration() -> None:
    manifest = RunManifest(**_manifest())
    assert isinstance(manifest.runtime_configuration, RuntimeConfiguration)
    assert manifest.runtime_configuration.observation_policy.material_target_return_pct == 2.0
    assert manifest.runtime_configuration.context_budget.model_context_limit == 128_000
    with pytest.raises(ValidationError):
        RunManifest(
            **_manifest(
                runtime_configuration={"admission_slots": 4}  # untyped dict rejected
            )
        )


def test_typed_runtime_configuration_rejects_negative_policy_values() -> None:
    with pytest.raises(ValidationError):
        ObservationPolicyConfig(
            material_target_return_pct=-1.0,
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
    with pytest.raises(ValidationError):
        ContextBudgetPolicy(
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
            safety_margin_tokens=-5,
        )


def test_run_manifest_hashes_are_hex_strings() -> None:
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(request_hash="not-hex"))
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(analyst_prompt_hash="x" * 63))
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(data_runtime_identity_hash="y" * 65))


def test_run_manifest_does_not_duplicate_data_runtime_identity() -> None:
    fields = set(RunManifest.model_fields)
    for duplicated in (
        "data_snapshot_id",
        "corpus_manifest_id",
        "fts_index_version",
        "dense_index_version",
        "embedding_model_revision",
        "reranker_revision",
        "query_policy_version",
    ):
        assert duplicated not in fields
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(data_snapshot_id="snapshot:7a004"))


def test_run_manifest_is_strict_and_frozen() -> None:
    manifest = RunManifest(**_manifest())
    with pytest.raises(ValidationError):
        manifest.run_id = "run:2"  # frozen
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(), unknown_field=True)  # extra forbidden

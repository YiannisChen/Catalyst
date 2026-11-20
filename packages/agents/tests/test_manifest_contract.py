"""V1.1 RunManifest contract tests (M2-7, corrective).

Agents-owned production run identity. It references DataRuntimeIdentity by
ref/hash, never duplicates an independently recomputed data identity, enforces
the production corrective bounds (1 round, 1 action), enforces the initial
60-second run deadline, rejects zero/negative token/deadline/budget values,
and uses a typed runtime configuration (Final Migration TSD §14–15).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from catalyst_agents.attribution.context_pack import ContextBudget
from catalyst_agents.runtime.manifest import (
    ObservationPolicyConfig,
    RunManifest,
    RuntimeConfiguration,
)
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.trading_calendar import session_close_utc
from pydantic import ValidationError


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
        "run_timeout_seconds": 120,
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
        "stage2_temporal_identity",
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
        "parent_run_id",
        "web_search_lead_id",
        "web_cache_rows_sha256",
        "retrieval_profile",
        "query_policy_version",
        "candidate_depth",
        "display_top_k",
        "max_chunks_per_document",
        "retrieval_policy_hash",
        "attribution_policy",
        "press_release_archive",
        "filing_units",
    }
    assert manifest.temporal_identity.session_date == "2026-01-06"
    # Optional web-source metadata is absent from ordinary run manifests, so
    # their frozen serialized identity remains byte-compatible.
    assert "parent_run_id" not in manifest.model_dump(mode="json")
    assert "web_search_lead_id" not in manifest.model_dump(mode="json")
    assert "press_release_archive" not in manifest.model_dump(mode="json")


@pytest.mark.parametrize("policy", ["pre_close_v1", "event_time_p1", "event_time_v1", "event_time_v2"])
def test_historical_manifest_policy_values_still_decode(policy: str) -> None:
    from catalyst_agents.attribution.policy import AttributionPolicy

    attribution = AttributionPolicy(
        verifier_enabled=True, corrective_enabled=False, quote_rules_version="legacy-q1",
        evidence_window_policy=policy,
    )
    manifest = RunManifest(**_manifest(attribution_policy=attribution))
    restored = RunManifest.model_validate_json(manifest.model_dump_json())
    assert restored.attribution_policy.evidence_window_policy == policy


def test_web_cache_manifest_hash_is_opt_in():
    default = RunManifest(**_manifest()).model_dump(mode="json")
    enabled = RunManifest(**_manifest(web_cache_rows_sha256="c" * 64)).model_dump(mode="json")
    assert "web_cache_rows_sha256" not in default
    assert enabled["web_cache_rows_sha256"] == "c" * 64


def test_run_manifest_production_freeze_bounds_are_enforced() -> None:
    assert RunManifest(**_manifest()).max_corrective_rounds == 1
    assert RunManifest(**_manifest()).max_actions_per_batch == 1
    assert RunManifest(**_manifest()).run_timeout_seconds == 120
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
        ContextBudget(
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


def test_run_manifest_rejects_invalid_nested_context_budget() -> None:
    """The same invalid context-budget configuration must be rejected when it
    appears inside RunManifest.runtime_configuration (supervisor Task 2)."""
    from catalyst_agents.attribution.context_pack import ContextBudget

    with pytest.raises(ValidationError):
        RunManifest(
            **_manifest(
                runtime_configuration=RuntimeConfiguration(
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
                        model_context_limit=0,  # zero context is impossible
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
            )
        )
    with pytest.raises(ValidationError):
        RunManifest(
            **_manifest(
                runtime_configuration=RuntimeConfiguration(
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
                        safety_margin_tokens=-5,
                    ),
                )
            )
        )


def test_runtime_configuration_research_limits_are_typed_and_bounded():
    """M4-2 amendment §3: typed research stage timeout/concurrency in
    RuntimeConfiguration, strictly bounded by the run deadline."""
    from catalyst_agents.runtime.manifest import (
        INITIAL_RUN_TIMEOUT_SECONDS,
        RuntimeConfiguration,
    )

    base = _runtime_config()
    cfg = RuntimeConfiguration(
        observation_policy=base.observation_policy,
        context_budget=base.context_budget,
        research_stage_timeout_seconds=30,
        max_initial_research_concurrency=2,
    )
    assert cfg.research_stage_timeout_seconds == 30
    assert cfg.max_initial_research_concurrency == 2
    with pytest.raises(ValidationError):
        RuntimeConfiguration(
            observation_policy=base.observation_policy,
            context_budget=base.context_budget,
            research_stage_timeout_seconds=INITIAL_RUN_TIMEOUT_SECONDS,  # must be strictly below deadline
            max_initial_research_concurrency=2,
        )
    with pytest.raises(ValidationError):
        RuntimeConfiguration(
            observation_policy=base.observation_policy,
            context_budget=base.context_budget,
            research_stage_timeout_seconds=INITIAL_RUN_TIMEOUT_SECONDS + 1,
            max_initial_research_concurrency=2,
        )
    with pytest.raises(ValidationError):
        RuntimeConfiguration(
            observation_policy=base.observation_policy,
            context_budget=base.context_budget,
            research_stage_timeout_seconds=0,
            max_initial_research_concurrency=2,
        )
    with pytest.raises(ValidationError):
        RuntimeConfiguration(
            observation_policy=base.observation_policy,
            context_budget=base.context_budget,
            research_stage_timeout_seconds=30,
            max_initial_research_concurrency=0,
        )
    with pytest.raises(ValidationError):
        RuntimeConfiguration(
            observation_policy=base.observation_policy,
            context_budget=base.context_budget,
            research_stage_timeout_seconds=30,
            max_initial_research_concurrency=4,  # > max initial task count
        )


def test_run_manifest_persists_research_limits_in_runtime_configuration():
    manifest = RunManifest(**_manifest())
    assert manifest.runtime_configuration.research_stage_timeout_seconds < manifest.run_timeout_seconds
    assert 1 <= manifest.runtime_configuration.max_initial_research_concurrency <= 3


# --- Frozen-A3 §2.1 / WP-B B5: the RunManifest retrieval limits -------------


def _retrieval_limits(**overrides: Any) -> dict[str, Any]:
    limits: dict[str, Any] = {
        "retrieval_profile": "lexical_display_v1",
        "query_policy_version": "qp:v1",
        "candidate_depth": 100,
        "display_top_k": 8,
        "max_chunks_per_document": 3,
        "retrieval_policy_hash": "a" * 64,
    }
    limits.update(overrides)
    return limits


def test_run_manifest_retrieval_limits_default_to_none_and_never_serialize() -> None:
    """A historical manifest keeps its exact dump: the six fields are absent.

    The admission hash is ``payload_sha256(manifest.model_dump(mode="json"))``,
    so omitting unset fields is what keeps a historical manifest's hash
    byte-identical (Frozen-A3 §2.1; WP-B B5).
    """
    manifest = RunManifest(**_manifest())
    for field in (
        "retrieval_profile",
        "query_policy_version",
        "candidate_depth",
        "display_top_k",
        "max_chunks_per_document",
        "retrieval_policy_hash",
    ):
        assert getattr(manifest, field) is None
        assert field not in manifest.model_dump()
        assert field not in manifest.model_dump(mode="json")


def test_run_manifest_retrieval_limits_round_trip_when_set() -> None:
    manifest = RunManifest(**_manifest(**_retrieval_limits()))
    assert manifest.retrieval_profile == "lexical_display_v1"
    assert manifest.query_policy_version == "qp:v1"
    assert manifest.candidate_depth == 100
    assert manifest.display_top_k == 8
    assert manifest.max_chunks_per_document == 3
    dumped = manifest.model_dump(mode="json")
    assert dumped["candidate_depth"] == 100
    assert dumped["display_top_k"] == 8
    assert dumped["max_chunks_per_document"] == 3
    assert dumped["retrieval_policy_hash"] == "a" * 64


@pytest.mark.parametrize(
    "field", ("candidate_depth", "display_top_k", "max_chunks_per_document")
)
@pytest.mark.parametrize("value", (0, -1))
def test_run_manifest_retrieval_limits_reject_non_positive_ints(field, value) -> None:
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(**_retrieval_limits(**{field: value})))


@pytest.mark.parametrize(
    "bad_hash",
    ("abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65),
)
def test_run_manifest_retrieval_policy_hash_requires_lowercase_sha256(
    bad_hash,
) -> None:
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(**_retrieval_limits(retrieval_policy_hash=bad_hash)))

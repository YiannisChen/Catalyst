"""Frozen-A3 §2.1 / WP-B B5: the RunManifest retrieval limits.

The six retrieval-limit fields are added *directly* on ``RunManifest`` and are
optional. A historical manifest (one that sets none of them) must still dump
and hash exactly as it did before the fields existed, because admission binds
``payload_sha256(manifest.model_dump(mode="json"))``
(``catalyst_app/runtime/admission.py:200-202``). The production factory fills
all six from the frozen data-core constants, never from string literals.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from catalyst_agents.attribution.context_pack import ContextBudget
from catalyst_agents.runtime.manifest import (
    ObservationPolicyConfig,
    RunManifest,
    RuntimeConfiguration,
)
from catalyst_app.persistence.events import canonical_json, payload_sha256
from catalyst_app.runtime.admission import AdmissionRequest
from catalyst_app.runtime.composition import build_default_manifest_factory
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.retrieval.lexical_display import (
    CANDIDATE_DEPTH_CAP,
    DISPLAY_TOP_K,
    LEXICAL_DISPLAY_POLICY_HASH,
    MAX_CHUNKS_PER_DOCUMENT,
    RETRIEVAL_PROFILE,
    V1_QUERY_POLICY_VERSION,
)
from catalyst_data.trading_calendar import session_close_utc

# The pre-Frozen-A3 manifest dump and admission hash, captured on the branch
# before the six fields were added. Adding the fields with a None default and
# an omitting model_serializer must not move either byte.
_HISTORICAL_MANIFEST_JSON = (
    '{"analyst_model_id":"model-analyst","analyst_prompt_hash":"' + "a" * 64 + '",'
    '"claim_schema_version":"v1","code_revision":"17693fa",'
    '"context_pack_schema_version":"v1","context_token_budget":4000,'
    '"data_runtime_identity_hash":"' + "f" * 64 + '",'
    '"data_runtime_identity_ref":"runtime-id:7a004",'
    '"hypothesis_schema_version":"v1","max_actions_per_batch":1,'
    '"max_corrective_rounds":1,"packing_policy_version":"p1",'
    '"policy_version":"p1","provider_capability_revision":"cap:v1",'
    '"request_hash":"' + "e" * 64 + '","run_id":"run:1","run_timeout_seconds":60,'
    '"runtime_configuration":{"context_budget":{"coverage_summary_tokens":200,'
    '"evidence_payload_tokens":60000,"inventory_tokens":500,"lead_only_tokens":1000,'
    '"model_context_limit":128000,"observation_tokens":300,'
    '"per_news_item_max_tokens":800,"per_sec_chunk_max_tokens":1200,'
    '"research_history_tokens":100,"reserved_output_tokens":2000,'
    '"reserved_system_instruction_tokens":1000,"safety_margin_tokens":2000},'
    '"max_initial_research_concurrency":2,"observation_policy":{'
    '"aligned_residual_pct":1.0,"flat_reference_return_pct":0.25,'
    '"material_prior_return_pct":1.5,"material_target_return_pct":2.0,'
    '"minimum_peer_count":3,"quiet_target_return_pct":0.5,'
    '"require_sector_and_peer_for_broad_sector":true,'
    '"scenario_policy_version":"sp:v1","volume_elevated_ratio":1.5,'
    '"volume_extreme_ratio":3.0},"research_stage_timeout_seconds":30},'
    '"temporal_identity":{"cutoff_at":"2026-01-06T21:00:00Z",'
    '"information_window_start_at":"2026-01-05T21:00:00Z",'
    '"market_timezone":"America/New_York",'
    '"session_close_at":"2026-01-06T21:00:00Z",'
    '"session_date":"2026-01-06",'
    '"session_open_at":"2026-01-06T14:30:00Z"},'
    '"tokenizer_policy":"registered-bge-m3","workflow_version":"v1.1",'
    '"writer_model_id":"model-writer","writer_prompt_hash":"' + "b" * 64 + '"}'
)
# payload_sha256(_HISTORICAL_MANIFEST_JSON)
_HISTORICAL_MANIFEST_SHA256 = (
    "07d6f2c80a4db4a5373bdeac0f05458075e6253bbe59fadadb3291ac04356a7d"
)


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _historical_manifest() -> RunManifest:
    return RunManifest(
        run_id="run:1",
        request_hash="e" * 64,
        temporal_identity=TemporalIdentity(
            session_date="2026-01-06",
            market_timezone="America/New_York",
            session_open_at=_utc("2026-01-06T14:30:00Z"),
            session_close_at=_utc(session_close_utc("2026-01-06")),
            information_window_start_at=_utc(session_close_utc("2026-01-05")),
            cutoff_at=_utc(session_close_utc("2026-01-06")),
        ),
        data_runtime_identity_ref="runtime-id:7a004",
        data_runtime_identity_hash="f" * 64,
        code_revision="17693fa",
        workflow_version="v1.1",
        policy_version="p1",
        analyst_model_id="model-analyst",
        analyst_prompt_hash="a" * 64,
        writer_model_id="model-writer",
        writer_prompt_hash="b" * 64,
        context_pack_schema_version="v1",
        packing_policy_version="p1",
        context_token_budget=4000,
        tokenizer_policy="registered-bge-m3",
        hypothesis_schema_version="v1",
        claim_schema_version="v1",
        max_corrective_rounds=1,
        max_actions_per_batch=1,
        run_timeout_seconds=60,
        provider_capability_revision="cap:v1",
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
                safety_margin_tokens=2_000,
            ),
        ),
    )


def test_historical_run_manifest_dump_and_hash_are_byte_identical() -> None:
    """A manifest without the six fields dumps and hashes exactly as before."""
    dumped = _historical_manifest().model_dump(mode="json")
    assert canonical_json(dumped) == _HISTORICAL_MANIFEST_JSON
    assert payload_sha256(dumped) == _HISTORICAL_MANIFEST_SHA256
    for field in (
        "retrieval_profile",
        "query_policy_version",
        "candidate_depth",
        "display_top_k",
        "max_chunks_per_document",
        "retrieval_policy_hash",
    ):
        assert field not in dumped


class _FakeRuntimeDependencyLoader:
    """External-boundary loader: identity-bound runtime deps, no SQLite conn."""

    def __init__(self) -> None:
        self._runtime = DataRuntimeIdentity(
            data_snapshot_id="s" * 64,
            corpus_manifest_id="m" * 64,
            fts_index_version="build:fts",
            dense_index_version=None,
            embedding_model_revision=None,
            reranker_revision=None,
            query_policy_version=V1_QUERY_POLICY_VERSION,
        )

    def get_dependencies(self, *, force_reload: bool = False):
        from catalyst_agents.runtime.dependencies import RuntimeDependencies

        return RuntimeDependencies(
            sqlite_db_path=Path("/tmp/catalyst-test.db"),
            health={"status": "ready"},
            retriever=None,
            data_runtime_identity=self._runtime,
            requested_manifest_id="m" * 64,
        )


def _admission_request() -> AdmissionRequest:
    return AdmissionRequest(
        ticker="AAPL",
        session_date="2026-01-15",
        query="Why did AAPL move?",
        provider="openai",
        model_id="gpt-4.1-mini",
        base_url=None,
        credential_source_identifier="server_env",
        workflow_version="v1.1",
        config_version="v1.1",
    )


def test_production_manifest_factory_fills_the_frozen_retrieval_limits() -> None:
    factory = build_default_manifest_factory(
        dependency_loader=_FakeRuntimeDependencyLoader()
    )
    manifest = factory(_admission_request(), "run:limits", "c" * 64)
    assert manifest.retrieval_profile == RETRIEVAL_PROFILE
    assert manifest.query_policy_version == V1_QUERY_POLICY_VERSION
    assert manifest.candidate_depth == CANDIDATE_DEPTH_CAP
    assert manifest.display_top_k == DISPLAY_TOP_K
    assert manifest.max_chunks_per_document == MAX_CHUNKS_PER_DOCUMENT
    assert manifest.retrieval_policy_hash == LEXICAL_DISPLAY_POLICY_HASH
    # And they are actually serialized, so admission binds them.
    dumped = manifest.model_dump(mode="json")
    assert dumped["retrieval_profile"] == RETRIEVAL_PROFILE
    assert dumped["candidate_depth"] == CANDIDATE_DEPTH_CAP
    assert dumped["retrieval_policy_hash"] == LEXICAL_DISPLAY_POLICY_HASH


def test_production_manifest_omits_retired_archive_and_decodes_legacy_off(monkeypatch) -> None:
    factory = build_default_manifest_factory(
        dependency_loader=_FakeRuntimeDependencyLoader()
    )

    manifest = factory(_admission_request(), "run:archive-off", "c" * 64)

    dumped = manifest.model_dump(mode="json")
    assert "press_release_archive" not in dumped
    assert RunManifest.model_validate({**dumped, "press_release_archive": "off"}).press_release_archive == "off"


def test_manifest_records_explicitly_waived_filing_units(monkeypatch) -> None:
    monkeypatch.setenv("CATALYST_FILING_UNITS", "off")
    factory = build_default_manifest_factory(
        dependency_loader=_FakeRuntimeDependencyLoader()
    )

    manifest = factory(_admission_request(), "run:filing-units-off", "c" * 64)

    assert manifest.model_dump(mode="json")["filing_units"] == "waived"


def test_production_manifest_records_the_attribution_policy(monkeypatch) -> None:
    """Verifier on / corrective off by default, bound into the manifest."""
    from catalyst_agents.attribution.policy import resolve_attribution_policy

    monkeypatch.delenv("CATALYST_PRIMARY_VERIFIER", raising=False)
    monkeypatch.delenv("CATALYST_SKIP_CORRECTIVE", raising=False)
    monkeypatch.setenv("CATALYST_EVIDENCE_WINDOW_POLICY", "desk_cascade_v1")
    monkeypatch.setenv("CATALYST_WEB_EVIDENCE", "off")
    factory = build_default_manifest_factory(
        dependency_loader=_FakeRuntimeDependencyLoader()
    )
    manifest = factory(_admission_request(), "run:policy", "c" * 64)
    policy = manifest.attribution_policy
    assert policy == resolve_attribution_policy({
        "CATALYST_EVIDENCE_WINDOW_POLICY": "desk_cascade_v1",
        "CATALYST_WEB_EVIDENCE": "off",
    })
    assert policy.verifier_enabled and not policy.corrective_enabled
    assert policy.verifier_version and len(policy.verifier_prompt_sha256) == 64
    dumped = manifest.model_dump(mode="json")
    assert dumped["attribution_policy"]["verifier_version"] == policy.verifier_version
    assert "attribution_policy" not in _historical_manifest().model_dump(mode="json")

"""Shared V1.1 API-surface test helpers (M6-9 migration)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI

from catalyst_app.main import create_app
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import ArtifactPayload, EventRepository
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.admission import AdmissionController, AdmissionRequest
from catalyst_app.runtime.executor import RunExecutor
from catalyst_agents.attribution.context_pack import ContextBudget
from catalyst_agents.runtime.manifest import (
    ObservationPolicyConfig,
    RunManifest,
    RuntimeConfiguration,
)
from catalyst_data.canonical.temporal import TemporalIdentity


def utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def build_manifest(request: AdmissionRequest, run_id: str, request_hash: str) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        request_hash=request_hash,
        temporal_identity=TemporalIdentity(
            session_date="2026-01-06",
            market_timezone="America/New_York",
            session_open_at=utc("2026-01-06T14:30:00Z"),
            session_close_at=utc("2026-01-06T21:00:00Z"),
            information_window_start_at=utc("2026-01-05T21:00:00Z"),
            cutoff_at=utc("2026-01-06T21:00:00Z"),
        ),
        data_runtime_identity_ref="runtime-id:test",
        data_runtime_identity_hash="f" * 64,
        code_revision="m6-test",
        workflow_version=request.workflow_version,
        policy_version="p1",
        analyst_model_id=request.model_id,
        analyst_prompt_hash="a" * 64,
        writer_model_id=request.model_id,
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


def fixture_db(db_path: Path) -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.commit()


def make_v1_app(
    db_path: Path,
    *,
    admission_slots: int = 4,
    max_workers: int = 2,
    run_adapter=None,
    failure_handler=None,
) -> FastAPI:
    """Build a V1.1 app with an injectable run adapter."""
    repo = EventRepository(db_path=db_path)
    executor = RunExecutor(
        admission_slots=admission_slots,
        max_workers=max_workers,
        run_adapter=run_adapter or (lambda run_id, t: {"ok": True}),
        failure_handler=failure_handler,
        shutdown_grace_seconds=0.1,
    )
    controller = AdmissionController(
        db_path=db_path,
        executor=executor,
        events=repo,
        manifest_factory=build_manifest,
    )
    return create_app(admission_controller=controller, db_path=db_path)


def terminalize_with_attribution(
    db_path: Path,
    run_id: str,
    *,
    attribution_status: str = "SUFFICIENT",
    attribution_type: str = "EVIDENCE_BACKED_CAUSAL",
) -> None:
    """Persist stage.started + terminal run.completed + attribution artifact."""
    
    from catalyst_app.events import (
        AssuranceCompletedPayload,
        RunCompletedPayload,
        RunEventType,
        StageStartedPayload,
    )
    from catalyst_app.lifecycle import RunLifecycleStatus
    from catalyst_app.persistence.events import ArtifactPayload, EventRepository

    repo = EventRepository(db_path=db_path)
    repo.append(
        run_id=run_id,
        event_type=RunEventType.STAGE_STARTED,
        payload=StageStartedPayload(stage="observation_build"),
        lifecycle_update=(RunLifecycleStatus.ACCEPTED, RunLifecycleStatus.RUNNING),
    )
    seqs = repo.append_terminal(
        run_id=run_id,
        assurance_payload=AssuranceCompletedPayload(
            valid=True, final_result_status=attribution_status
        ),
        terminal_event_type=RunEventType.RUN_COMPLETED,
        terminal_payload=RunCompletedPayload(
            result_status=attribution_status,
            final_output_artifact_ref=f"answer:{run_id}",
            total_latency_ms=10,
            runtime_identity_ref="runtime:test",
        ),
        artifact_payloads=[
            ArtifactPayload(
                artifact_id=f"attribution:{run_id}",
                artifact_type="attribution_result",
                payload={
                    "attribution_status": attribution_status,
                    "attribution_type": attribution_type,
                },
            )
        ],
        lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.COMPLETED),
    )
    del seqs

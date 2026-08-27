"""FastAPI lifespan startup/shutdown ordering contract (M6-3).

Final Migration TSD §15/§11: startup recovery completes before admission
reopens; shutdown stops new admission, cancels pending work, waits one bounded
grace period, and leaves unresolved rows for the same startup-recovery policy.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient

from catalyst_app.main import create_app
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import EventRepository
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.admission import (
    AdmissionController,
    AdmissionRequest,
)
from catalyst_app.runtime.executor import RunExecutor
from catalyst_agents.attribution.context_pack import ContextBudget
from catalyst_agents.runtime.manifest import (
    ObservationPolicyConfig,
    RunManifest,
    RuntimeConfiguration,
)
from catalyst_data.canonical.temporal import TemporalIdentity


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def manifest_for_lifespan(request: AdmissionRequest, run_id: str, request_hash: str) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        request_hash=request_hash,
        temporal_identity=TemporalIdentity(
            session_date="2026-01-06",
            market_timezone="America/New_York",
            session_open_at=_utc("2026-01-06T14:30:00Z"),
            session_close_at=_utc("2026-01-06T21:00:00Z"),
            information_window_start_at=_utc("2026-01-05T21:00:00Z"),
            cutoff_at=_utc("2026-01-06T21:00:00Z"),
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


def _fixture_db(db_path: Path) -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.commit()


def _request(**overrides: object) -> AdmissionRequest:
    base: dict[str, object] = {
        "ticker": "AAPL",
        "session_date": "2026-01-06",
        "query": "Why did AAPL move today?",
        "provider": "openai",
        "model_id": "gpt-4.1-mini",
        "base_url": None,
        "credential_source_identifier": "server_env",
        "workflow_version": "v1.1",
        "config_version": "v1.1",
        "idempotency_key": None,
    }
    base.update(overrides)
    return AdmissionRequest(**base)


def test_startup_recovery_runs_before_admission_reopens(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    with open_rw(db_path) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES ('stranded:1', 'RUNNING', NULL, 'a'*64, 'm:1', 'b'*64, 0, 't', 't')"
        )
        conn.commit()

    submitted: list[str] = []
    executor = RunExecutor(
        admission_slots=4,
        max_workers=1,
        run_adapter=lambda run_id, t: submitted.append(run_id) or {"ok": True},
        shutdown_grace_seconds=0.1,
    )
    controller = AdmissionController(
        db_path=db_path,
        executor=executor,
        events=EventRepository(db_path=db_path),
        manifest_factory=manifest_for_lifespan,
    )

    with TestClient(create_app(admission_controller=controller)) as client:
        with open_rw(db_path) as conn:
            row = conn.execute(
                "SELECT lifecycle_status FROM runs WHERE run_id = 'stranded:1'"
            ).fetchone()
        assert row["lifecycle_status"] == "FAILED"
        r1 = controller.admit(_request())
        assert r1.kind == "accepted"
        assert client.get("/api/health/runtime").status_code == 200

    assert executor.is_closed
    assert executor.try_reserve_slot() is False


def test_shutdown_ordering_stops_admission_and_waits_bounded(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)

    started = threading.Event()
    release = threading.Event()

    def slow_adapter(run_id: str, timeout_seconds: float) -> dict[str, object]:
        started.set()
        release.wait(timeout=5)
        return {"run_id": run_id, "status": "COMPLETED"}

    executor = RunExecutor(
        admission_slots=2,
        max_workers=1,
        run_adapter=slow_adapter,
        shutdown_grace_seconds=0.5,
    )
    controller = AdmissionController(
        db_path=db_path,
        executor=executor,
        events=EventRepository(db_path=db_path),
        manifest_factory=manifest_for_lifespan,
    )

    with TestClient(create_app(admission_controller=controller)) as client:
        assert client.get("/api/health/runtime").status_code == 200
        outcome = controller.admit(_request())
        assert outcome.kind == "accepted"
        assert started.wait(timeout=5)

    release.set()
    deadline = time.monotonic() + 5
    while executor.active_count > 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert executor.active_count == 0
    assert executor.is_closed
    assert controller.admit(_request()).kind == "unavailable"

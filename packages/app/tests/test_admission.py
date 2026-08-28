"""Admission + idempotency + capacity reservation contract (M6-3).

Final Migration TSD §11; Grok RUNTIME-01. Durable ACCEPTED admission, one
process-wide bounded executor, SQLite-backed idempotency, non-blocking slot
reservation, atomic run+event+manifest publication, submission-failure
terminalization, startup recovery, and the capacity invariant
ACCEPTED + RUNNING + CANCEL_REQUESTED <= admission_slots.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import threading
from uuid import uuid4

import pytest

from catalyst_app.events import (
    AssuranceCompletedPayload,
    RunAcceptedPayload,
    RunCompletedPayload,
    RunEventType,
    RunFailedPayload,
    StageStartedPayload,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import (
    ArtifactPayload,
    EventRepository,
    payload_sha256,
)
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.admission import (
    AdmissionController,
    AdmissionOutcome,
    AdmissionRequest,
)
from catalyst_app.runtime.executor import (
    ExecutorConfigError,
    RunExecutor,
)
from catalyst_agents.attribution.context_pack import ContextBudget
from catalyst_agents.runtime.manifest import (
    ObservationPolicyConfig,
    RunManifest,
    RuntimeConfiguration,
)
from catalyst_data.canonical.temporal import TemporalIdentity


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _temporal() -> TemporalIdentity:
    return TemporalIdentity(
        session_date="2026-01-06",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-06T14:30:00Z"),
        session_close_at=_utc("2026-01-06T21:00:00Z"),
        information_window_start_at=_utc("2026-01-05T21:00:00Z"),
        cutoff_at=_utc("2026-01-06T21:00:00Z"),
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


def _manifest(
    request: AdmissionRequest, run_id: str, request_hash: str
) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        request_hash=request_hash,
        temporal_identity=_temporal(),
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
        runtime_configuration=_runtime_config(),
    )


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


def _fixture_db(db_path: Path) -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.commit()


def _events(db_path: Path) -> EventRepository:
    return EventRepository(db_path=db_path)


def _adapter_factory(db_path: Path, repo: EventRepository, *, fail_code: str | None = None):
    """A fake agents run adapter simulating claim -> run -> terminal commit."""

    def adapter(run_id: str, timeout_seconds: float) -> dict[str, object]:
        if fail_code == "TIMEOUT":
            with open_rw(db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                repo.append(
                    run_id=run_id,
                    event_type=RunEventType.STAGE_STARTED,
                    payload=StageStartedPayload(stage="research_execution"),
                    lifecycle_update=(RunLifecycleStatus.ACCEPTED, RunLifecycleStatus.RUNNING),
                    conn=conn,
                )
                repo.append_terminal(
                    run_id=run_id,
                    assurance_payload=AssuranceCompletedPayload(
                        valid=False, violations=("run_timeout",), final_result_status=None
                    ),
                    terminal_event_type=RunEventType.RUN_FAILED,
                    terminal_payload=RunFailedPayload(
                        failure_code="TIMEOUT", stage="research_execution", retryable=False
                    ),
                    lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.FAILED),
                    conn=conn,
                )
                conn.commit()
            return {"run_id": run_id, "status": "FAILED_SYSTEM", "sub_reason": "timeout"}

        with open_rw(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            repo.append(
                run_id=run_id,
                event_type=RunEventType.STAGE_STARTED,
                payload=StageStartedPayload(stage="observation_build"),
                lifecycle_update=(RunLifecycleStatus.ACCEPTED, RunLifecycleStatus.RUNNING),
                conn=conn,
            )
            conn.commit()
        with open_rw(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            repo.append_terminal(
                run_id=run_id,
                assurance_payload=AssuranceCompletedPayload(
                    valid=True, final_result_status="SUFFICIENT"
                ),
                terminal_event_type=RunEventType.RUN_COMPLETED,
                terminal_payload=RunCompletedPayload(
                    result_status="SUFFICIENT",
                    final_output_artifact_ref=f"answer:{run_id}",
                    total_latency_ms=10,
                    runtime_identity_ref="runtime-id:test",
                ),
                lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.COMPLETED),
                conn=conn,
            )
            conn.commit()
        return {"run_id": run_id, "status": "COMPLETED"}

    return adapter


def _controller(
    db_path: Path,
    *,
    admission_slots: int = 4,
    max_workers: int = 2,
    run_adapter=None,
    manifest_factory=_manifest,
    repo: EventRepository | None = None,
) -> tuple[AdmissionController, RunExecutor, EventRepository]:
    repo = repo or _events(db_path)
    adapter = run_adapter or _adapter_factory(db_path, repo)
    executor = RunExecutor(
        admission_slots=admission_slots,
        max_workers=max_workers,
        run_adapter=adapter,
        shutdown_grace_seconds=0.2,
    )
    controller = AdmissionController(
        db_path=db_path,
        executor=executor,
        events=repo,
        manifest_factory=manifest_factory,
    )
    return controller, executor, repo


def _wait_until(predicate, timeout: float = 5.0) -> None:
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return
        _time.sleep(0.05)
    raise TimeoutError("condition not met within timeout")


def test_admit_persists_accepted_run_event_and_manifest(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    release = threading.Event()
    base_adapter = _adapter_factory(db_path, _events(db_path))

    def blocking_adapter(run_id: str, timeout_seconds: float) -> dict[str, object]:
        release.wait(timeout=5)
        return base_adapter(run_id, timeout_seconds)

    controller, executor, _ = _controller(db_path, run_adapter=blocking_adapter)

    outcome = controller.admit(_request())

    assert outcome.kind == "accepted"
    assert outcome.run_id
    assert outcome.stream_url == f"/api/live-runs/{outcome.run_id}/stream"
    assert executor.reserved_count == 1

    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT lifecycle_status, request_hash, run_manifest_id, manifest_hash, capacity_slot "
            "FROM runs WHERE run_id = ?",
            (outcome.run_id,),
        ).fetchone()
        assert row["lifecycle_status"] == "ACCEPTED"
        assert row["request_hash"] == outcome.request_hash
        assert row["run_manifest_id"] == f"manifest:{outcome.run_id}"
        assert row["capacity_slot"] >= 0

        event = conn.execute(
            "SELECT seq, event_type FROM run_events WHERE run_id = ? ORDER BY seq",
            (outcome.run_id,),
        ).fetchall()
        assert [(e["seq"], e["event_type"]) for e in event] == [(1, "run.accepted")]

        manifest = conn.execute(
            "SELECT payload_json, payload_hash FROM run_artifacts WHERE run_id = ?",
            (outcome.run_id,),
        ).fetchone()
    payload = json.loads(manifest["payload_json"])
    assert payload["run_timeout_seconds"] == 60
    assert manifest["payload_hash"] == payload_sha256(json.loads(manifest["payload_json"]))

    release.set()
    _wait_until(lambda: executor.active_count == 0)
    with open_rw(db_path) as conn:
        lifecycle = conn.execute(
            "SELECT lifecycle_status FROM runs WHERE run_id = ?", (outcome.run_id,)
        ).fetchone()[0]
    assert lifecycle == "COMPLETED"
    assert executor.reserved_count == 0


def test_duplicate_idempotency_key_same_hash_returns_existing(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    submitted: list[str] = []
    controller, executor, _ = _controller(
        db_path,
        run_adapter=lambda run_id, t: submitted.append(run_id) or {"ok": True},
    )

    first = controller.admit(_request(idempotency_key="key-1"))
    second = controller.admit(_request(idempotency_key="key-1"))

    assert first.kind == "accepted"
    assert second.kind == "duplicate"
    assert second.run_id == first.run_id
    assert submitted == [first.run_id]  # exactly one executor task, no second slot
    with open_rw(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        events = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id = ?", (first.run_id,)
        ).fetchone()[0]
    assert count == 1
    assert events == 1


def test_same_key_different_hash_conflict(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    controller, _, _ = _controller(db_path)

    controller.admit(_request(idempotency_key="key-1"))
    conflict = controller.admit(
        _request(idempotency_key="key-1", query="A completely different question")
    )

    assert conflict.kind == "conflict"
    with open_rw(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert count == 1


def test_no_key_identical_active_request_dedupes(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    submitted: list[str] = []
    controller, executor, _ = _controller(
        db_path,
        run_adapter=lambda run_id, t: submitted.append(run_id) or {"ok": True},
    )

    first = controller.admit(_request())
    second = controller.admit(_request())

    assert second.kind == "duplicate"
    assert second.run_id == first.run_id
    assert submitted == [first.run_id]  # no second executor task
    with open_rw(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert count == 1


def test_no_key_terminal_historical_allows_new_run(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    controller, executor, repo = _controller(db_path)

    first = controller.admit(_request())
    _wait_until(lambda: executor.active_count == 0)  # first run completes
    second = controller.admit(_request())

    assert second.kind == "accepted"
    assert second.run_id != first.run_id
    with open_rw(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert count == 2


def test_capacity_exceeded_returns_capacity_outcome(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    controller, executor, _ = _controller(db_path, admission_slots=2, max_workers=1, run_adapter=lambda run_id, t: {"ok": True})

    first = controller.admit(_request(ticker="AAPL"))
    second = controller.admit(_request(ticker="MSFT"))
    third = controller.admit(_request(ticker="NVDA"))

    assert first.kind == "accepted"
    assert second.kind == "accepted"
    assert third.kind == "capacity_exceeded"
    with open_rw(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE lifecycle_status IN "
            "('ACCEPTED','RUNNING','CANCEL_REQUESTED')"
        ).fetchone()[0]
    assert count == 2
    assert active == 2 <= executor.admission_slots
    assert executor.reserved_count == 0  # adapters returned without terminalization
    assert controller.admit(_request(ticker="TSLA")).kind == "capacity_exceeded"


def test_transaction_failure_releases_reserved_slot(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)

    def failing_manifest(request, run_id, request_hash):
        raise RuntimeError("manifest build failed")

    controller, executor, _ = _controller(db_path, manifest_factory=failing_manifest)

    with pytest.raises(RuntimeError):
        controller.admit(_request())

    assert executor.reserved_count == 0  # slot released on pre-visibility rollback
    with open_rw(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert count == 0


def test_submission_failure_terminalizes_failed_and_releases_slot(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    controller, executor, _ = _controller(db_path)

    def failing_submit(run_id: str, timeout_seconds: float):
        raise RuntimeError("executor submit failed")

    executor.submit = failing_submit  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        controller.admit(_request())

    assert executor.reserved_count == 0
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT lifecycle_status, failure_code FROM runs"
        ).fetchone()
        assert row["lifecycle_status"] == "FAILED"
        assert row["failure_code"] == "SUBMISSION_FAILURE"
        terminal = conn.execute(
            "SELECT event_type FROM run_events WHERE event_type = 'run.failed'"
        ).fetchone()
        assert terminal is not None


def test_admission_slots_and_worker_invariants(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    repo = _events(db_path)
    adapter = _adapter_factory(db_path, repo)

    with pytest.raises(ExecutorConfigError):
        RunExecutor(admission_slots=1, max_workers=1, run_adapter=adapter)
    with pytest.raises(ExecutorConfigError):
        RunExecutor(admission_slots=5, max_workers=2, run_adapter=adapter)
    with pytest.raises(ExecutorConfigError):
        RunExecutor(admission_slots=2, max_workers=3, run_adapter=adapter)

    for slots in (2, 3, 4):
        executor = RunExecutor(
            admission_slots=slots,
            max_workers=min(2, slots),
            run_adapter=adapter,
        )
        assert executor.admission_slots == slots
        assert executor.max_workers <= executor.admission_slots
        executor.shutdown(grace_seconds=0)


def test_timeout_publishes_failed_and_releases_capacity(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    repo = _events(db_path)
    controller, executor, _ = _controller(
        db_path,
        run_adapter=_adapter_factory(db_path, repo, fail_code="TIMEOUT"),
        repo=repo,
    )

    outcome = controller.admit(_request())
    _wait_until(lambda: executor.active_count == 0)

    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT lifecycle_status, failure_code FROM runs WHERE run_id = ?",
            (outcome.run_id,),
        ).fetchone()
        failed_event = conn.execute(
            "SELECT event_type FROM run_events WHERE run_id = ? AND event_type = 'run.failed'",
            (outcome.run_id,),
        ).fetchone()
        failed_payload = conn.execute(
            "SELECT payload_json FROM run_events WHERE run_id = ? AND event_type = 'run.failed'",
            (outcome.run_id,),
        ).fetchone()
    assert row["lifecycle_status"] == "FAILED"
    assert row["failure_code"] == "TIMEOUT"
    assert failed_event is not None
    assert json.loads(failed_payload["payload_json"])["failure_code"] == "TIMEOUT"
    assert executor.reserved_count == 0


def test_startup_recovery_resubmits_accepted_and_fails_stranded_running(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    repo = _events(db_path)

    # Pre-seed one ACCEPTED run with its manifest artifact and one stranded RUNNING run.
    with open_rw(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES ('recover:accepted', 'ACCEPTED', NULL, ?, 'manifest:recover:accepted', ?, 0, 't', 't')",
            ("a" * 64, "b" * 64),
        )
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES ('recover:stranded', 'RUNNING', NULL, ?, 'manifest:recover:stranded', ?, 1, 't', 't')",
            ("c" * 64, "d" * 64),
        )
        manifest = _manifest(_request(), "recover:accepted", "a" * 64)
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version)"
            " VALUES ('recover:accepted', 1, 't', 'run.accepted', 'ADMISSION', ?, 'v1')",
            (json.dumps({"seq": 1}),),
        )
        conn.execute(
            "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json, optional)"
            " VALUES ('manifest:recover:accepted', 'recover:accepted', 1, 'run_manifest', ?, ?, 0)",
            (
                payload_sha256(manifest.model_dump(mode="json")),
                json.dumps(manifest.model_dump(mode="json"), sort_keys=True),
            ),
        )
        conn.commit()

    submitted: list[str] = []

    def recording_adapter(run_id: str, timeout_seconds: float) -> dict[str, object]:
        submitted.append(run_id)
        return {"run_id": run_id}

    executor = RunExecutor(
        admission_slots=4,
        max_workers=2,
        run_adapter=recording_adapter,
        shutdown_grace_seconds=0.1,
    )
    controller = AdmissionController(
        db_path=db_path,
        executor=executor,
        events=repo,
        manifest_factory=_manifest,
    )
    controller.startup_recovery()

    assert submitted == ["recover:accepted"]
    assert executor.reserved_count == 1
    with open_rw(db_path) as conn:
        stranded = conn.execute(
            "SELECT lifecycle_status, failure_code FROM runs WHERE run_id = 'recover:stranded'"
        ).fetchone()
        assert stranded["lifecycle_status"] == "FAILED"
        assert stranded["failure_code"] == "PROCESS_RECOVERY"
        failed = conn.execute(
            "SELECT event_type FROM run_events WHERE run_id = 'recover:stranded' AND event_type = 'run.failed'"
        ).fetchone()
        assert failed is not None


def test_startup_recovery_reconciles_slots_before_admission_reopens(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    repo = _events(db_path)

    with open_rw(db_path) as conn:
        for i in range(2):
            run_id = f"recover:{i}"
            request_hash = f"{i}a" + "a" * 62
            conn.execute(
                "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
                " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
                " VALUES (?, 'ACCEPTED', NULL, ?, 'manifest:' || ?, 'b'*64, ?, 't', 't')",
                (run_id, request_hash, run_id, i),
            )
            conn.execute(
                "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version)"
                " VALUES (?, 1, 't', 'run.accepted', 'ADMISSION', '{}', 'v1')",
                (run_id,),
            )
            manifest = _manifest(_request(), run_id, request_hash)
            conn.execute(
                "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json, optional)"
                " VALUES (?, ?, 1, 'run_manifest', ?, ?, 0)",
                (
                    f"manifest:{run_id}",
                    run_id,
                    payload_sha256(manifest.model_dump(mode="json")),
                    json.dumps(manifest.model_dump(mode="json"), sort_keys=True),
                ),
            )
        conn.commit()

    submitted: list[str] = []
    executor = RunExecutor(
        admission_slots=2,
        max_workers=1,
        run_adapter=lambda run_id, t: submitted.append(run_id) or {"ok": True},
        shutdown_grace_seconds=0.1,
    )
    controller = AdmissionController(
        db_path=db_path,
        executor=executor,
        events=repo,
        manifest_factory=_manifest,
    )
    controller.startup_recovery()

    assert sorted(submitted) == ["recover:0", "recover:1"]
    assert executor.reserved_count == 2
    # Admission must not reopen with extra capacity after recovery.
    outcome = controller.admit(_request())
    assert outcome.kind == "capacity_exceeded"


# ── M6 corrective: capacity release ownership (Finding F) ──────────────────

def test_release_slot_is_keyed_by_run_id_and_exactly_once(tmp_path: Path) -> None:
    """Duplicate release must not decrement another run's reservation."""
    from catalyst_app.persistence.connect import open_rw
    from catalyst_app.persistence.schema import init_runtime_db

    db_path = tmp_path / "capacity.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)

    executor = RunExecutor(
        admission_slots=4,
        max_workers=2,
        run_adapter=lambda run_id, t: {"ok": True},
        shutdown_grace_seconds=0.1,
    )
    assert executor.try_reserve_slot("run:a") is True
    assert executor.try_reserve_slot("run:b") is True
    assert executor.reserved_count == 2

    # Release run:a exactly once; run:b must keep its reservation.
    executor.release_slot("run:a")
    assert executor.reserved_count == 1
    # Duplicate release of run:a is a no-op and cannot touch run:b.
    executor.release_slot("run:a")
    assert executor.reserved_count == 1
    executor.release_slot("run:b")
    assert executor.reserved_count == 0
    executor.shutdown(grace_seconds=0)


def test_release_only_after_terminal_commit_or_rollback(tmp_path: Path) -> None:
    """An adapter returning without terminalizing cannot release capacity."""
    from catalyst_app.persistence.connect import open_rw
    from catalyst_app.persistence.schema import init_runtime_db

    db_path = tmp_path / "capacity.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES ('run:no-term', 'RUNNING', NULL, 'a'*64, 'm', 'b'*64, 0, 't', 't')"
        )
        conn.commit()

    def is_terminal(run_id: str) -> bool:
        with open_rw(db_path) as conn:
            row = conn.execute(
                "SELECT lifecycle_status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return row is not None and row["lifecycle_status"] in {
            "COMPLETED", "FAILED", "CANCELLED",
        }

    executor = RunExecutor(
        admission_slots=4,
        max_workers=1,
        run_adapter=lambda run_id, t: {"ok": True, "not_terminalized": True},
        terminal_check=is_terminal,
        shutdown_grace_seconds=0.1,
    )
    assert executor.try_reserve_slot("run:no-term") is True
    executor.submit("run:no-term", 60.0)
    _wait_until(lambda: executor.active_count == 0)
    # The adapter returned without terminalizing: capacity stays held by the
    # stranded durable row instead of being released "as if it succeeded".
    assert executor.reserved_count == 1
    executor.shutdown(grace_seconds=0)


def test_release_after_durable_terminal_commit(tmp_path: Path) -> None:
    """Release happens only after the durable terminal commit."""
    from catalyst_app.persistence.connect import open_rw
    from catalyst_app.persistence.schema import init_runtime_db

    db_path = tmp_path / "capacity.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES ('run:term', 'ACCEPTED', NULL, 'a'*64, 'm', 'b'*64, 0, 't', 't')"
        )
        conn.commit()

    def is_terminal(run_id: str) -> bool:
        with open_rw(db_path) as conn:
            row = conn.execute(
                "SELECT lifecycle_status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return row is not None and row["lifecycle_status"] in {
            "COMPLETED", "FAILED", "CANCELLED",
        }

    def terminalizing_adapter(run_id: str, timeout_seconds: float) -> dict:
        from catalyst_app.events import (
            AssuranceCompletedPayload,
            RunCompletedPayload,
            RunEventType,
            StageStartedPayload,
        )
        from catalyst_app.lifecycle import RunLifecycleStatus
        from catalyst_app.persistence.events import EventRepository

        repo = EventRepository(db_path=db_path)
        repo.append(
            run_id=run_id,
            event_type=RunEventType.STAGE_STARTED,
            payload=StageStartedPayload(stage="observation_build"),
            lifecycle_update=(RunLifecycleStatus.ACCEPTED, RunLifecycleStatus.RUNNING),
        )
        repo.append_terminal(
            run_id=run_id,
            assurance_payload=AssuranceCompletedPayload(valid=True, final_result_status="SUFFICIENT"),
            terminal_event_type=RunEventType.RUN_COMPLETED,
            terminal_payload=RunCompletedPayload(
                result_status="SUFFICIENT",
                final_output_artifact_ref=f"answer:{run_id}",
                total_latency_ms=1,
                runtime_identity_ref="runtime:test",
            ),
            lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.COMPLETED),
        )
        return {"run_id": run_id, "status": "COMPLETED"}

    executor = RunExecutor(
        admission_slots=4,
        max_workers=1,
        run_adapter=terminalizing_adapter,
        terminal_check=is_terminal,
        shutdown_grace_seconds=0.1,
    )
    assert executor.try_reserve_slot("run:term") is True
    executor.submit("run:term", 60.0)
    _wait_until(lambda: executor.active_count == 0)
    assert executor.reserved_count == 0
    executor.shutdown(grace_seconds=0)


def test_process_local_reservation_reconciles_with_durable_rows(tmp_path: Path) -> None:
    """Finding F: reservation count reconciles with durable capacity rows."""
    from catalyst_app.persistence.connect import open_rw
    from catalyst_app.persistence.schema import init_runtime_db

    db_path = tmp_path / "capacity.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        for idx, status in enumerate(("ACCEPTED", "RUNNING", "CANCEL_REQUESTED", "COMPLETED")):
            conn.execute(
                "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
                " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
                " VALUES (?, ?, NULL, ?, ?, ?, ?, 't', 't')",
                (f"run:{idx}", status, f"{idx}" * 64, f"m:{idx}", f"{idx+1}" * 64, idx),
            )
        conn.commit()

    executor = RunExecutor(
        admission_slots=4,
        max_workers=2,
        run_adapter=lambda run_id, t: {"ok": True},
        shutdown_grace_seconds=0.1,
    )
    executor.reconcile_slots(
        ["run:0", "run:1", "run:2"],
    )
    assert executor.reserved_count == 3
    # A 4th reservation is still allowed (ACCEPTED+RUNNING+CANCEL_REQUESTED=3).
    assert executor.try_reserve_slot("run:4") is True
    assert executor.reserved_count == 4
    assert executor.try_reserve_slot("run:5") is False
    executor.shutdown(grace_seconds=0)


class _SyncExecutor:
    """Deterministic admission executor: submit runs the adapter synchronously.

    Mirrors the RunExecutor capacity surface (admission_slots,
    try_reserve_slot, reserved_count, release_slot, submit) so admission
    ordering is testable without thread scheduling.
    """

    def __init__(self, adapter, *, admission_slots: int = 4, fail: bool = False) -> None:
        self.adapter = adapter
        self.admission_slots = admission_slots
        self.fail = fail
        self._reserved: set[str] = set()
        self.submitted: list[str] = []

    def try_reserve_slot(self, run_id: str | None = None) -> bool:
        key = run_id or "__anon__:sync"
        if key in self._reserved:
            return True
        if len(self._reserved) >= self.admission_slots:
            return False
        self._reserved.add(key)
        return True

    @property
    def reserved_count(self) -> int:
        return len(self._reserved)

    def release_slot(self, run_id: str | None = None) -> bool:
        if run_id is not None:
            return run_id in self._reserved and self._reserved.discard(run_id) or False
        return False

    def submit(self, run_id: str, timeout_seconds: float) -> dict[str, object]:
        self.submitted.append(run_id)
        if self.fail:
            raise RuntimeError("synchronous submission failure")
        return self.adapter(run_id, timeout_seconds)


def test_pre_submit_credential_visible_to_synchronous_submit(tmp_path: Path) -> None:
    """Finding I deterministic race test: the volatile credential is
    registered after run_id allocation and before executor submission; with a
    synchronous submit the adapter observes it inline — no sleep/poll."""
    from catalyst_app.runtime_credential_store import RuntimeCredentialStore

    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    repo = _events(db_path)
    store = RuntimeCredentialStore()
    SECRET = "sk-byok-race-0123456789abcdef"
    observed: dict[str, object] = {}

    def adapter(run_id: str, timeout_seconds: float) -> dict[str, object]:
        credential = store.get(run_id)
        observed["run_id"] = run_id
        observed["api_key"] = credential.api_key if credential is not None else None
        return {"run_id": run_id, "status": "OK"}

    executor = _SyncExecutor(adapter)
    controller = AdmissionController(
        db_path=db_path,
        executor=executor,
        events=repo,
        manifest_factory=_manifest,
        credential_store=store,
    )
    outcome = controller.admit(
        _request(credential_source_identifier="browser_key"),
        pre_submit=lambda run_id: store.register(run_id, api_key=SECRET),
    )

    assert outcome.kind == "accepted"
    assert executor.submitted == [outcome.run_id]
    # submit ran synchronously inside admit; the adapter already saw it.
    assert observed["run_id"] == outcome.run_id
    assert observed["api_key"] == SECRET
    assert store.get(outcome.run_id) is not None


def test_pre_submit_credential_removed_on_submission_failure(tmp_path: Path) -> None:
    """Finding I: submission failure cleans up the registered credential."""
    from catalyst_app.runtime_credential_store import RuntimeCredentialStore

    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    repo = _events(db_path)
    store = RuntimeCredentialStore()
    SECRET = "sk-byok-submission-failure-0123456789ab"
    executor = _SyncExecutor(adapter=lambda run_id, t: {"ok": True}, fail=True)
    controller = AdmissionController(
        db_path=db_path,
        executor=executor,
        events=repo,
        manifest_factory=_manifest,
        credential_store=store,
    )

    with pytest.raises(RuntimeError, match="synchronous submission failure"):
        controller.admit(
            _request(),
            pre_submit=lambda run_id: store.register(run_id, api_key=SECRET),
        )

    # The failed run was terminalized FAILED and its volatile key removed.
    assert store.active_count == 0
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT lifecycle_status FROM runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert row["lifecycle_status"] == "FAILED"

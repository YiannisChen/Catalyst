"""Cooperative cancellation protocol contract (M6-8).

Final TSD §11: ACCEPTED cancellation is terminal; RUNNING persists
CANCEL_REQUESTED and signals the control token; CANCELLED is reached only when
work stops or late output is safely discarded; late completion loses the
conditional update; CANCEL_REQUESTED -> FAILED on integrity failure; no events
or artifacts after a terminal CANCELLED; repeated cancels are idempotent.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from catalyst_app.events import (
    AssuranceCompletedPayload,
    RunCancelledPayload,
    RunCompletedPayload,
    RunEventType,
    RunFailedPayload,
    StageStartedPayload,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import EventRepository, TerminalRunError
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.cancel import (
    CancelOutcome,
    CancellationController,
    CancellationTokenRegistry,
)
from catalyst_app.runtime.claim import RunClaimer


def _seed_run(db_path: Path, run_id: str = "run:1", status: str = "ACCEPTED") -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES (?, ?, NULL, ?, ?, ?, 0, 't', 't')",
            (run_id, status, "d" * 64, f"manifest:{run_id}", "e" * 64),
        )
        conn.commit()


def _controller(db_path: Path) -> CancellationController:
    events = EventRepository(db_path=db_path)
    return CancellationController(
        db_path=db_path,
        events=events,
        claimer=RunClaimer(db_path=db_path, events=events),
    )


def _seed_accepted_event(db_path: Path, run_id: str = "run:1") -> None:
    from catalyst_app.events import RunAcceptedPayload

    EventRepository(db_path=db_path).append(
        run_id=run_id,
        event_type=RunEventType.RUN_ACCEPTED,
        stage="ADMISSION",
        payload=RunAcceptedPayload(
            request_identity_digest="a" * 64,
            ticker="AAPL",
            trade_date="2026-01-06",
            workflow_version="v1.1",
            model_provider_label="test/model",
            stream_url=f"/api/live-runs/{run_id}/stream",
        ),
    )


def _lifecycle(db_path: Path, run_id: str = "run:1") -> str:
    with open_rw(db_path) as conn:
        return conn.execute(
            "SELECT lifecycle_status FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()[0]


def test_cancel_accepted_is_terminal(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="ACCEPTED")
    _seed_accepted_event(db_path)
    controller = _controller(db_path)

    outcome = controller.request_cancel("run:1")

    assert outcome.acknowledged is True
    assert outcome.status == RunLifecycleStatus.CANCELLED
    assert _lifecycle(db_path) == "CANCELLED"
    with open_rw(db_path) as conn:
        cancelled = conn.execute(
            "SELECT event_type FROM run_events WHERE run_id='run:1' AND event_type='run.cancelled'"
        ).fetchone()
        assert cancelled is not None


def test_cancel_running_persists_cancel_requested_and_signals_token(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    controller = _controller(db_path)

    outcome = controller.request_cancel("run:1")

    assert outcome.acknowledged is True
    assert outcome.status == RunLifecycleStatus.CANCEL_REQUESTED
    assert _lifecycle(db_path) == "CANCEL_REQUESTED"
    token = controller.tokens.token("run:1")
    assert token.requested is True


def test_repeated_cancel_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    controller = _controller(db_path)

    first = controller.request_cancel("run:1")
    second = controller.request_cancel("run:1")
    third = controller.request_cancel("run:1")

    assert first.acknowledged is True
    assert second.acknowledged is True
    assert third.acknowledged is True
    assert _lifecycle(db_path) == "CANCEL_REQUESTED"


def test_acknowledge_cancellation_reaches_cancelled(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    _seed_accepted_event(db_path)
    controller = _controller(db_path)
    controller.request_cancel("run:1")

    outcome = controller.acknowledge_cancellation("run:1")

    assert outcome.status == RunLifecycleStatus.CANCELLED
    assert _lifecycle(db_path) == "CANCELLED"
    with open_rw(db_path) as conn:
        cancelled = conn.execute(
            "SELECT event_type FROM run_events WHERE run_id='run:1' AND event_type='run.cancelled'"
        ).fetchone()
        assert cancelled is not None


def test_late_completion_after_cancel_is_discarded(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    _seed_accepted_event(db_path)
    controller = _controller(db_path)
    controller.request_cancel("run:1")

    claimer = RunClaimer(db_path=db_path, events=EventRepository(db_path=db_path))
    with pytest.raises(ValueError):
        claimer.terminalize(
            run_id="run:1",
            assurance_payload=AssuranceCompletedPayload(valid=True, final_result_status="SUFFICIENT"),
            terminal_event_type=RunEventType.RUN_COMPLETED,
            terminal_payload=RunCompletedPayload(
                result_status="SUFFICIENT",
                final_output_artifact_ref="a",
                total_latency_ms=1,
                runtime_identity_ref="r",
            ),
            lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.COMPLETED),
        )

    assert _lifecycle(db_path) == "CANCEL_REQUESTED"
    controller.acknowledge_cancellation("run:1")
    with open_rw(db_path) as conn:
        completed = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id='run:1' AND event_type='run.completed'"
        ).fetchone()[0]
        assert completed == 0
        cancelled = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id='run:1' AND event_type='run.cancelled'"
        ).fetchone()[0]
        assert cancelled == 1


def test_no_events_after_cancelled_terminal(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    _seed_accepted_event(db_path)
    controller = _controller(db_path)
    controller.request_cancel("run:1")
    controller.acknowledge_cancellation("run:1")

    repo = EventRepository(db_path=db_path)
    with pytest.raises(TerminalRunError):
        repo.append(
            run_id="run:1",
            event_type=RunEventType.STAGE_STARTED,
            payload=StageStartedPayload(stage="late"),
        )


def test_cancel_requested_can_fail_on_integrity_failure(tmp_path: Path) -> None:
    """CANCEL_REQUESTED -> FAILED is the cancellation-handling integrity path."""
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    _seed_accepted_event(db_path)
    controller = _controller(db_path)
    controller.request_cancel("run:1")

    claimer = RunClaimer(db_path=db_path, events=EventRepository(db_path=db_path))
    claimer.terminalize(
        run_id="run:1",
        assurance_payload=AssuranceCompletedPayload(valid=False, violations=("cancel_integrity",)),
        terminal_event_type=RunEventType.RUN_FAILED,
        terminal_payload=RunFailedPayload(
            failure_code="CANCELLATION_INTEGRITY_FAILURE",
            stage="STREAMING_ANSWER",
            retryable=False,
        ),
        lifecycle_update=(RunLifecycleStatus.CANCEL_REQUESTED, RunLifecycleStatus.FAILED),
    )

    assert _lifecycle(db_path) == "FAILED"
    with open_rw(db_path) as conn:
        failed = conn.execute(
            "SELECT failure_code FROM runs WHERE run_id='run:1'"
        ).fetchone()[0]
        assert failed == "CANCELLATION_INTEGRITY_FAILURE"


def test_cancellation_token_is_threadsafe_boundary_check(tmp_path: Path) -> None:
    """The control token is the cooperative boundary the run adapter observes."""
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    controller = _controller(db_path)
    token = controller.tokens.token("run:1")
    assert token.requested is False

    controller.request_cancel("run:1")
    assert token.requested is True
    assert token.wait(timeout=1.0) is True

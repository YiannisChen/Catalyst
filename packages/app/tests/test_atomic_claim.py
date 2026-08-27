"""Atomic executor claim + terminal commit contract (M6-4).

Final Migration TSD §11/§17: claim_run conditionally transitions
ACCEPTED -> RUNNING with owner/task token under one transaction (row count
must equal one); terminal state + required terminal artifacts + terminal
event commit together; a late completion loses the conditional update and is
discarded; stranded RUNNING is failed at recovery, never resumed mid-graph.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
import threading

import pytest

from catalyst_app.events import (
    AssuranceCompletedPayload,
    RunCompletedPayload,
    RunEventType,
    RunFailedPayload,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import ArtifactPayload, EventRepository
from catalyst_app.persistence.schema import init_runtime_db
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


def _claimer(db_path: Path) -> RunClaimer:
    return RunClaimer(db_path=db_path, events=EventRepository(db_path=db_path))


def test_claim_run_transitions_accepted_to_running_with_token(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path)
    claimer = _claimer(db_path)

    claimed = claimer.claim_run("run:1", owner="executor-1", task_token="task-1")

    assert claimed is True
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT lifecycle_status, owner, task_token FROM runs WHERE run_id='run:1'"
        ).fetchone()
    assert row["lifecycle_status"] == "RUNNING"
    assert row["owner"] == "executor-1"
    assert row["task_token"] == "task-1"


def test_claim_run_requires_exact_accepted_state(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    claimer = _claimer(db_path)

    assert claimer.claim_run("run:1", owner="o", task_token="t") is False

    _seed_run(db_path, run_id="run:done", status="COMPLETED")
    assert claimer.claim_run("run:done", owner="o", task_token="t") is False


def test_competing_claims_exactly_one_winner(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path)
    claimer = _claimer(db_path)
    results: list[bool] = []
    lock = threading.Lock()

    def claimant(name: str) -> None:
        ok = claimer.claim_run("run:1", owner=name, task_token=f"task-{name}")
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=claimant, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert results.count(True) == 1
    assert results.count(False) == 3


def test_terminal_commit_persists_state_artifact_and_event_together(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    claimer = _claimer(db_path)

    claimer.terminalize(
        run_id="run:1",
        assurance_payload=AssuranceCompletedPayload(valid=True, final_result_status="SUFFICIENT"),
        terminal_event_type=RunEventType.RUN_COMPLETED,
        terminal_payload=RunCompletedPayload(
            result_status="SUFFICIENT",
            final_output_artifact_ref="artifact:answer",
            total_latency_ms=100,
            runtime_identity_ref="runtime:1",
        ),
        required_artifact_payloads=[
            ArtifactPayload(
                artifact_id="artifact:answer",
                artifact_type="answer",
                payload={"text": "final"},
            )
        ],
        lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.COMPLETED),
    )

    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT lifecycle_status FROM runs WHERE run_id='run:1'"
        ).fetchone()
        assert row["lifecycle_status"] == "COMPLETED"
        events = [
            (r["seq"], r["event_type"])
            for r in conn.execute(
                "SELECT seq, event_type FROM run_events WHERE run_id='run:1' ORDER BY seq"
            ).fetchall()
        ]
        assert events[-1] == (2, "run.completed")
        artifact = conn.execute(
            "SELECT payload_json FROM run_artifacts WHERE artifact_id='artifact:answer'"
        ).fetchone()
        assert artifact is not None


def test_terminal_commit_fault_rolls_back_everything(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    claimer = _claimer(db_path)
    # Pre-insert the colliding artifact so the terminal commit fails.
    with open_rw(db_path) as conn:
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, payload_json, schema_version)"
            " VALUES ('run:1', 1, 't', 'stage.started', '{}', 'v1')"
        )
        conn.execute(
            "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json, optional)"
            " VALUES ('artifact:dup', 'run:1', 1, 'x', ?, '{}', 0)",
            ("f" * 64,),
        )
        conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        claimer.terminalize(
            run_id="run:1",
            assurance_payload=AssuranceCompletedPayload(valid=True),
            terminal_event_type=RunEventType.RUN_COMPLETED,
            terminal_payload=RunCompletedPayload(
                result_status="SUFFICIENT",
                final_output_artifact_ref="artifact:answer",
                total_latency_ms=1,
                runtime_identity_ref="r",
            ),
            required_artifact_payloads=[ArtifactPayload(artifact_id="artifact:dup", artifact_type="x", payload={})],
            lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.COMPLETED),
        )

    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT lifecycle_status FROM runs WHERE run_id='run:1'"
        ).fetchone()
        assert row["lifecycle_status"] == "RUNNING"  # no partial terminal state
        count = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE event_type='run.completed'"
        ).fetchone()[0]
        assert count == 0


def test_late_completion_loses_conditional_update_and_is_discarded(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    claimer = _claimer(db_path)
    # The run fails first (e.g. timeout).
    claimer.terminalize(
        run_id="run:1",
        assurance_payload=AssuranceCompletedPayload(valid=False, violations=("timeout",)),
        terminal_event_type=RunEventType.RUN_FAILED,
        terminal_payload=RunFailedPayload(failure_code="TIMEOUT", stage="research_execution"),
        lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.FAILED),
    )

    # A late RUNNING -> COMPLETED must lose the conditional update.
    with pytest.raises(ValueError):
        claimer.terminalize(
            run_id="run:1",
            assurance_payload=AssuranceCompletedPayload(valid=True),
            terminal_event_type=RunEventType.RUN_COMPLETED,
            terminal_payload=RunCompletedPayload(
                result_status="SUFFICIENT",
                final_output_artifact_ref="artifact:late",
                total_latency_ms=1,
                runtime_identity_ref="r",
            ),
            lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.COMPLETED),
        )

    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT lifecycle_status FROM runs WHERE run_id='run:1'"
        ).fetchone()
        assert row["lifecycle_status"] == "FAILED"
        count = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE event_type='run.completed'"
        ).fetchone()[0]
        assert count == 0


def test_stranded_running_is_failed_never_resumed(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="RUNNING")
    claimer = _claimer(db_path)

    claimer.fail_stranded("run:1", failure_code="PROCESS_RECOVERY")

    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT lifecycle_status, failure_code FROM runs WHERE run_id='run:1'"
        ).fetchone()
        assert row["lifecycle_status"] == "FAILED"
        assert row["failure_code"] == "PROCESS_RECOVERY"
        failed = conn.execute(
            "SELECT event_type FROM run_events WHERE run_id='run:1' AND event_type='run.failed'"
        ).fetchone()
        assert failed is not None


def test_terminal_commit_rejects_illegal_prior_state(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, status="ACCEPTED")
    claimer = _claimer(db_path)

    with pytest.raises(ValueError):
        claimer.terminalize(
            run_id="run:1",
            assurance_payload=AssuranceCompletedPayload(valid=True),
            terminal_event_type=RunEventType.RUN_COMPLETED,
            terminal_payload=RunCompletedPayload(
                result_status="SUFFICIENT",
                final_output_artifact_ref="a",
                total_latency_ms=1,
                runtime_identity_ref="r",
            ),
            lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.COMPLETED),
        )

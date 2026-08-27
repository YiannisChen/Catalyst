"""EventRepository contract (M6-5).

Final Migration TSD §17 + SSE-01 lock: DB-allocated per-run sequence under
BEGIN IMMEDIATE, UNIQUE(run_id, seq), atomic event+artifact+lifecycle
publication, fault-injection rollback, terminal two-event batch, and an
after-commit notifier that fires only after a successful commit. The
repository imports the sealed M2 taxonomy and never defines a second one.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
import threading

import pytest

from catalyst_app.events import (
    AssuranceCompletedPayload,
    EvidenceRetrievedPayload,
    RunAcceptedPayload,
    RunCompletedPayload,
    RunEventType,
    StageStartedPayload,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import ArtifactPayload, EventRepository
from catalyst_app.persistence.schema import init_runtime_db


def _accepted_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "request_identity_digest": "a" * 64,
        "ticker": "AAPL",
        "trade_date": "2026-01-06",
        "workflow_version": "v1.1",
        "model_provider_label": "test-provider/model",
        "stream_url": "/api/live-runs/run:1/stream",
    }
    base.update(overrides)
    return base


def _prepare_db(db_path: Path, run_id: str = "run:1") -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES (?, 'ACCEPTED', NULL, ?, ?, ?, 0, 't', 't')",
            (run_id, "d" * 64, f"manifest:{run_id}", "e" * 64),
        )
        conn.commit()


def _repo(db_path: Path, run_id: str = "run:1", notifier=None) -> EventRepository:
    return EventRepository(db_path=db_path, notifier=notifier)


def _artifact(artifact_id: str, artifact_type: str = "evidence_state", **overrides: object) -> ArtifactPayload:
    payload: dict[str, object] = {"ref": artifact_id}
    payload.update(overrides)
    return ArtifactPayload(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        payload=payload,
    )


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, run_id: str) -> None:
        self.calls.append(run_id)


def test_append_allocates_db_sequence_starting_at_one(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    repo = _repo(db_path)

    seq = repo.append(
        run_id="run:1",
        event_type=RunEventType.RUN_ACCEPTED,
        payload=RunAcceptedPayload(**_accepted_payload()),
        stage="ADMISSION",
    )

    assert seq == 1
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT seq, event_type, schema_version FROM run_events WHERE run_id='run:1'"
        ).fetchone()
        assert row["seq"] == 1
        assert row["event_type"] == "run.accepted"


def test_append_increments_sequence_per_run(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    repo = _repo(db_path)

    seq1 = repo.append(
        run_id="run:1",
        event_type=RunEventType.RUN_ACCEPTED,
        payload=RunAcceptedPayload(**_accepted_payload()),
    )
    seq2 = repo.append(
        run_id="run:1",
        event_type=RunEventType.STAGE_STARTED,
        payload=StageStartedPayload(stage="observation_build"),
    )

    assert (seq1, seq2) == (1, 2)


def test_concurrent_appends_produce_distinct_contiguous_sequences(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    repo = _repo(db_path)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def writer(prefix: str) -> None:
        try:
            for i in range(2):
                repo.append(
                    run_id="run:1",
                    event_type=RunEventType.STAGE_STARTED,
                    payload=StageStartedPayload(stage=f"{prefix}-{i}"),
                )
        except BaseException as exc:  # pragma: no cover - failure path
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    with open_rw(db_path) as conn:
        rows = conn.execute(
            "SELECT seq FROM run_events WHERE run_id='run:1' ORDER BY seq ASC"
        ).fetchall()
        seqs = [r["seq"] for r in rows]
    assert seqs == [1, 2, 3, 4]  # run.accepted + four concurrent stage events


def test_unknown_event_type_fails_before_opening_connection(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    opened: list[bool] = []

    def spying_open(*args, **kwargs):
        opened.append(True)
        from catalyst_app.persistence.connect import open_rw
        return open_rw(*args, **kwargs)

    repo = EventRepository(db_path=db_path, open_fn=spying_open)
    with pytest.raises(ValueError):
        repo.append(
            run_id="run:1",
            event_type="unknown.event",  # type: ignore[arg-type]
            payload=RunAcceptedPayload(**_accepted_payload()),
        )
    assert opened == []


def test_payload_type_mismatch_fails_before_opening_connection(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    opened: list[bool] = []

    def spying_open(*args, **kwargs):
        opened.append(True)
        from catalyst_app.persistence.connect import open_rw
        return open_rw(*args, **kwargs)

    repo = EventRepository(db_path=db_path, open_fn=spying_open)
    with pytest.raises(ValueError):
        repo.append(
            run_id="run:1",
            event_type=RunEventType.STAGE_STARTED,
            payload=RunAcceptedPayload(**_accepted_payload()),
        )
    assert opened == []


def test_fault_injection_artifact_failure_rolls_back_event(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    with open_rw(db_path) as conn:
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, payload_json, schema_version)"
            " VALUES ('run:1', 1, 't', 'run.accepted', '{}', 'v1')"
        )
        conn.execute(
            "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json, optional)"
            " VALUES ('artifact:dup', 'run:1', 1, 'evidence_state', ?, '{}', 0)",
            ("f" * 64,),
        )
        conn.commit()

    notifier = RecordingNotifier()
    repo = _repo(db_path, notifier=notifier)
    with pytest.raises(sqlite3.IntegrityError):
        repo.append(
            run_id="run:1",
            event_type=RunEventType.EVIDENCE_RETRIEVED,
            payload=EvidenceRetrievedPayload(task_id="task:1", candidate_count=0),
            artifact_payloads=[_artifact("artifact:dup")],
        )

    with open_rw(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id='run:1' AND event_type='evidence.retrieved'"
        ).fetchone()[0]
        assert count == 0  # no dangling event row
        lifecycle = conn.execute(
            "SELECT lifecycle_status FROM runs WHERE run_id='run:1'"
        ).fetchone()[0]
        assert lifecycle == "ACCEPTED"
    assert notifier.calls == []  # no notify without commit


def test_after_commit_notifier_fires_only_after_successful_commit(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    notifier = RecordingNotifier()
    repo = _repo(db_path, notifier=notifier)

    repo.append(
        run_id="run:1",
        event_type=RunEventType.STAGE_STARTED,
        payload=StageStartedPayload(stage="research_execution"),
    )

    assert notifier.calls == ["run:1"]


def test_artifact_payload_hash_matches_canonical_json(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    repo = _repo(db_path)

    repo.append(
        run_id="run:1",
        event_type=RunEventType.EVIDENCE_RETRIEVED,
        payload=EvidenceRetrievedPayload(task_id="task:1", candidate_count=2),
        artifact_payloads=[_artifact("artifact:1", counts=2)],
    )

    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT payload_hash, payload_json, optional FROM run_artifacts WHERE artifact_id='artifact:1'"
        ).fetchone()
    expected = hashlib.sha256(
        json.dumps({"ref": "artifact:1", "counts": 2}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert row["payload_hash"] == expected
    assert row["optional"] == 0


def test_optional_artifact_marked_optional(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    repo = _repo(db_path)

    repo.append(
        run_id="run:1",
        event_type=RunEventType.EVIDENCE_RETRIEVED,
        payload=EvidenceRetrievedPayload(task_id="task:1", candidate_count=0),
        artifact_payloads=[
            ArtifactPayload(artifact_id="artifact:dbg", artifact_type="debug", payload={"x": 1}, optional=True)
        ],
    )

    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT optional FROM run_artifacts WHERE artifact_id='artifact:dbg'"
        ).fetchone()
    assert row["optional"] == 1


def test_terminal_batch_allocates_two_contiguous_events(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    repo = _repo(db_path)
    repo.append(
        run_id="run:1",
        event_type=RunEventType.RUN_ACCEPTED,
        payload=RunAcceptedPayload(**_accepted_payload()),
    )
    repo.append(
        run_id="run:1",
        event_type=RunEventType.STAGE_STARTED,
        payload=StageStartedPayload(stage="observation_build"),
        lifecycle_update=(RunLifecycleStatus.ACCEPTED, RunLifecycleStatus.RUNNING),
    )

    assurance_seq, terminal_seq = repo.append_terminal(
        run_id="run:1",
        assurance_payload=AssuranceCompletedPayload(valid=True, final_result_status="SUFFICIENT"),
        terminal_event_type=RunEventType.RUN_COMPLETED,
        terminal_payload=RunCompletedPayload(
            result_status="SUFFICIENT",
            final_output_artifact_ref="artifact:answer",
            total_latency_ms=100,
            runtime_identity_ref="runtime:1",
        ),
        lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.COMPLETED),
    )

    assert terminal_seq == assurance_seq + 1
    with open_rw(db_path) as conn:
        rows = conn.execute(
            "SELECT seq, event_type FROM run_events WHERE run_id='run:1' ORDER BY seq ASC"
        ).fetchall()
        assert [(r["seq"], r["event_type"]) for r in rows] == [
            (1, "run.accepted"),
            (2, "stage.started"),
            (3, "assurance.completed"),
            (4, "run.completed"),
        ]
        lifecycle = conn.execute(
            "SELECT lifecycle_status FROM runs WHERE run_id='run:1'"
        ).fetchone()[0]
        assert lifecycle == "COMPLETED"


def test_lifecycle_update_conditioned_on_legal_prior_state(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    repo = _repo(db_path)

    with pytest.raises(ValueError):
        repo.append(
            run_id="run:1",
            event_type=RunEventType.STAGE_STARTED,
            payload=StageStartedPayload(stage="x"),
            lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.COMPLETED),
        )

    seq = repo.append(
        run_id="run:1",
        event_type=RunEventType.STAGE_STARTED,
        payload=StageStartedPayload(stage="observation_build"),
        lifecycle_update=(RunLifecycleStatus.ACCEPTED, RunLifecycleStatus.RUNNING),
    )
    assert seq == 1
    with open_rw(db_path) as conn:
        lifecycle = conn.execute(
            "SELECT lifecycle_status FROM runs WHERE run_id='run:1'"
        ).fetchone()[0]
        assert lifecycle == "RUNNING"


def test_terminal_batch_rejects_non_terminal_event_type(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _prepare_db(db_path)
    repo = _repo(db_path)

    with pytest.raises(ValueError):
        repo.append_terminal(
            run_id="run:1",
            assurance_payload=AssuranceCompletedPayload(valid=True),
            terminal_event_type=RunEventType.STAGE_STARTED,
            terminal_payload=StageStartedPayload(stage="x"),
        )


def test_append_can_run_inside_caller_transaction(tmp_path: Path) -> None:
    """Admission inserts the run and sequence-1 event in one transaction."""
    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES ('run:tx', 'ACCEPTED', NULL, 'x'*64, 'manifest:run:tx', 'y'*64, 0, 't', 't')"
        )
        repo = EventRepository(db_path=db_path)
        seq = repo.append(
            run_id="run:tx",
            event_type=RunEventType.RUN_ACCEPTED,
            payload=RunAcceptedPayload(**_accepted_payload()),
            conn=conn,
        )
        assert seq == 1
        with open_rw(db_path) as other:
            count = other.execute(
                "SELECT COUNT(*) FROM run_events WHERE run_id='run:tx'"
            ).fetchone()[0]
            assert count == 0
        conn.commit()
    with open_rw(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id='run:tx'"
        ).fetchone()[0]
        assert count == 1

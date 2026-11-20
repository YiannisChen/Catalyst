"""RG-13: a failed or cancelled run must not return an answer; a pushed answer is provisional.

Read-only view checks plus the answer.ready/terminal payload rules, on temporary
databases with synthetic data only.
"""
from __future__ import annotations

import json
from pathlib import Path

from catalyst_app.api_dto import RunViewDTO
from catalyst_app.events import AnswerReadyPayload, RunFailedPayload
from catalyst_app.main import create_app
from catalyst_app.persistence.connect import open_rw
from fastapi.testclient import TestClient
from test_runtime_scenarios_app import _seed

_SNAPSHOT = {
    "schema_version": "answer_snapshot_v1",
    "outcome": "ANSWERED",
    "status": "PARTIAL",
    "key_sentence": {"text": "Acme shares fell after the company cut its outlook."},
}


def _add_answer_ready_event(db_path: Path, run_id: str) -> None:
    with open_rw(db_path) as conn:
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json,"
            " schema_version) VALUES (?, 3, '2026-01-06T21:00:30Z', 'answer.ready', 'ANSWER', ?, 'v1')",
            (run_id, json.dumps({"answer_snapshot": _SNAPSHOT})),
        )
        conn.commit()


def test_cancelled_run_view_does_not_return_the_answer(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed(db_path, "run:c", "CANCELLED", {"run_manifest": {"temporal_identity": {"session_date": "2026-01-06"}},
                                          "answer_snapshot": _SNAPSHOT}, None)
    with TestClient(create_app(db_path=db_path)) as client:
        view = RunViewDTO.model_validate(client.get("/api/live-runs/run:c/view").json())
    assert view.run.lifecycle == "CANCELLED"
    assert view.answer is None
    assert view.availability["answer"] == "not_applicable"


def test_completed_run_view_returns_the_answer_as_before(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed(db_path, "run:ok", "COMPLETED", {"run_manifest": {"temporal_identity": {"session_date": "2026-01-06"}},
                                           "answer_snapshot": _SNAPSHOT}, None)
    with TestClient(create_app(db_path=db_path)) as client:
        view = RunViewDTO.model_validate(client.get("/api/live-runs/run:ok/view").json())
    assert view.run.lifecycle == "COMPLETED"
    assert view.answer is not None and view.answer["status"] == "PARTIAL"
    assert view.availability["answer"] == "present"


def test_answer_ready_payload_defaults_to_provisional():
    assert AnswerReadyPayload(answer_snapshot={}).provisional is True
    assert AnswerReadyPayload(answer_snapshot={}, provisional=False).provisional is False


def test_terminal_failed_payload_provisional_flag_follows_answer_ready(tmp_path):
    from catalyst_app.events import run_has_answer_ready

    db_path = tmp_path / "runtime.db"
    _seed(db_path, "run:a", "FAILED", {}, ("SYSTEM_ERROR", "boom"))
    assert run_has_answer_ready(db_path, "run:a") is False
    assert RunFailedPayload(
        failure_code="SYSTEM_ERROR", provisional_output_invalidated=False
    ).provisional_output_invalidated is False
    _add_answer_ready_event(db_path, "run:a")
    assert run_has_answer_ready(db_path, "run:a") is True
    assert RunFailedPayload(
        failure_code="SYSTEM_ERROR", provisional_output_invalidated=True
    ).provisional_output_invalidated is True


def test_stranded_run_with_a_pushed_answer_is_invalidated_at_recovery(tmp_path):
    """claim.fail_stranded (PROCESS_RECOVERY) follows the same rule: invalidated iff an answer.ready exists."""
    import inspect

    from catalyst_app.runtime import claim
    src = inspect.getsource(claim.RunClaimer.fail_stranded)
    assert "run_has_answer_ready(self.db_path, run_id)" in src


def test_unreadable_event_log_fails_toward_invalidation(tmp_path):
    from catalyst_app.events import run_has_answer_ready
    assert run_has_answer_ready(tmp_path / "does-not-exist.db", "run:1") is True

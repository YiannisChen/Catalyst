"""Fault-injection checks for docs/design/runtime_scenarios.md, app side (admission, run view, executor).

Read-only behaviour checks of the frozen code; temporary databases and stubs only.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from catalyst_app.api_dto import RunViewDTO
from catalyst_app.main import create_app
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.admission import AdmissionController, AdmissionRequest
from catalyst_app.runtime.executor import RunExecutor
from catalyst_app.runtime.manifest_factory import (
    RuntimeUnavailableError,
    build_temporal_identity,
)
from fastapi.testclient import TestClient


def _request(ticker: str, day: str) -> AdmissionRequest:
    return AdmissionRequest(ticker=ticker, session_date=day, query=None, provider="deepseek", model_id="deepseek-chat",
                            base_url=None, credential_source_identifier="server_env")


def test_admission_accepts_any_ticker_text_and_any_iso_date():
    """S0: request validation only trims and upper-cases the ticker and parses the date; it does not check the ticker
    against a universe, the calendar, or whether D has closed (later steps decide)."""
    for ticker, day in (("zzzz.l", "2026-10-18"), ("NOTREAL", "2031-03-03"), (" brk-b ", "2026-01-06")):
        normalized = AdmissionController._validate_request(None, _request(ticker, day))  # pure: no self state
        assert normalized["ticker"] == ticker.strip().upper() and normalized["session_date"] == day
    with pytest.raises(ValueError):
        AdmissionController._validate_request(None, _request("AAPL", "2026/01/06"))


def test_non_trading_day_fails_closed_at_the_manifest_but_a_future_session_does_not():
    """S0: a weekend/holiday raises RuntimeUnavailableError in the manifest factory (HTTP 503 runtime_unavailable)
    before a run exists; a future trading day builds a manifest (the run later fails when D has no close). The
    calendar covers 2024-2027 only: a date outside it is rejected the same way as a holiday."""
    with pytest.raises(RuntimeUnavailableError, match="not_a_trading_session"):
        build_temporal_identity("2026-10-18")  # a Sunday
    future = build_temporal_identity("2027-03-03")
    assert future.session_date == "2027-03-03" and future.cutoff_at.year == 2027
    with pytest.raises(RuntimeUnavailableError, match="not_a_trading_session"):
        build_temporal_identity("2028-03-01")  # a Wednesday, outside CALENDAR_YEARS


def _seed(db_path: Path, run_id: str, lifecycle: str, artifacts: dict, failure: tuple[str, str] | None) -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, request_hash, run_manifest_id, manifest_hash, capacity_slot,"
            " created_at, updated_at, ticker, provider, failure_code, failure_message)"
            " VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'AAPL', 'deepseek', ?, ?)",
            (run_id, lifecycle, "a" * 64, f"manifest:{run_id}", "b" * 64, "2026-01-06T21:00:00Z",
             "2026-01-06T21:01:00Z", failure[0] if failure else None, failure[1] if failure else None))
        conn.execute("INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json,"
                     " schema_version) VALUES (?, 1, '2026-01-06T21:00:00Z', 'run.accepted', 'ADMISSION', '{}', 'v1')",
                     (run_id,))
        conn.execute("INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json,"
                     " schema_version) VALUES (?, 2, '2026-01-06T21:01:00Z', 'run.failed', 'TERMINAL', '{}', 'v1')",
                     (run_id,))
        for index, (artifact_type, payload) in enumerate(artifacts.items()):
            conn.execute("INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash,"
                         " payload_json, optional) VALUES (?, ?, 1, ?, ?, ?, 0)",
                         (f"{artifact_type}:{run_id}:{index}", run_id, artifact_type, "c" * 64, json.dumps(payload)))
        conn.commit()


def test_failed_run_view_does_not_return_the_answer_written_before_the_failure(tmp_path):
    """RG-13: a failed run must not return an answer. The snapshot written with the evidence-assessed event, before
    the terminal commit, is provisional only; the view does not expose it once the run FAILED."""
    db_path = tmp_path / "runtime.db"
    snapshot = {"schema_version": "answer_snapshot_v1", "outcome": "ANSWERED", "status": "PARTIAL",
                "key_sentence": {"text": "Acme shares fell after the company cut its outlook."}}
    _seed(db_path, "run:x", "FAILED", {"run_manifest": {"temporal_identity": {"session_date": "2026-01-06"}},
                                       "answer_snapshot": snapshot}, ("SYSTEM_ERROR", "OperationalError: locked"))
    with TestClient(create_app(db_path=db_path)) as client:
        view = RunViewDTO.model_validate(client.get("/api/live-runs/run:x/view").json())
    assert view.run.lifecycle == "FAILED" and view.failure.code == "SYSTEM_ERROR"
    assert view.answer is None
    assert view.availability["answer"] == "not_applicable"


def test_failure_handler_that_cannot_write_leaves_the_run_running_until_restart(tmp_path):
    """S13: when the crash handler's own write fails (database locked past the busy timeout), the error is swallowed,
    the row stays RUNNING and its slot stays reserved; startup recovery later fails it with PROCESS_RECOVERY."""
    def crashed(run_id: str, timeout_seconds: float) -> dict:
        raise RuntimeError("graph boom")

    def locked(run_id: str, code: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    executor = RunExecutor(admission_slots=2, max_workers=1, run_adapter=crashed, failure_handler=locked,
                           terminal_check=lambda run_id: False, shutdown_grace_seconds=0.1)
    assert executor.try_reserve_slot("run:1")
    future = executor.submit("run:1", 60.0)
    with pytest.raises(RuntimeError, match="graph boom"):
        future.result(timeout=5)
    deadline = time.monotonic() + 5
    while executor.active_count > 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert executor.reserved_count == 1  # not released: no durable terminal row
    executor.shutdown(grace_seconds=0)

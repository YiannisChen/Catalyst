from catalyst_agents.runtime.status import (
    build_failure_payload,
    failure_payload_from_rows,
    is_retryable,
    normalize_run_status,
)
from catalyst_agents.state import OutputStatus


def test_normalize_run_status_maps_internal_outputs_to_frontend_statuses():
    assert normalize_run_status(OutputStatus.SUFFICIENT) == {
        "status": "SUCCEEDED",
        "sub_reason": None,
    }
    assert normalize_run_status("PARTIAL") == {"status": "PARTIAL", "sub_reason": None}
    assert normalize_run_status("INSUFFICIENT") == {"status": "INSUFFICIENT", "sub_reason": None}
    assert normalize_run_status("SYSTEM_ERROR") == {"status": "FAILED_SYSTEM", "sub_reason": "system_error"}


def test_non_material_move_and_inconclusive_remain_sub_reasons():
    assert normalize_run_status("INSUFFICIENT", sub_reason="NON_MATERIAL_MOVE") == {
        "status": "INSUFFICIENT",
        "sub_reason": "NON_MATERIAL_MOVE",
    }
    assert normalize_run_status("PARTIAL", sub_reason="INCONCLUSIVE") == {
        "status": "PARTIAL",
        "sub_reason": "INCONCLUSIVE",
    }


def test_failed_request_status_for_pre_run_validation_failure():
    payload = build_failure_payload(
        status="FAILED_REQUEST",
        sub_reason="unsupported_ticker",
        message="Ticker is not supported.",
        source="request_validation",
    )

    assert payload == {
        "status": "FAILED_REQUEST",
        "sub_reason": "unsupported_ticker",
        "message": "Ticker is not supported.",
        "source": "request_validation",
        "node": None,
        "retryable": False,
    }


def test_retryable_rules_for_lifecycle_and_terminal_statuses():
    assert is_retryable("QUEUED") is False
    assert is_retryable("RUNNING") is False
    assert is_retryable("FAILED_SYSTEM", sub_reason="system_error") is True
    assert is_retryable("FAILED_REQUEST", sub_reason="unsupported_ticker") is False
    assert is_retryable("INSUFFICIENT", sub_reason="NON_MATERIAL_MOVE") is False
    assert is_retryable("PARTIAL", sub_reason="INCONCLUSIVE") is True
    assert is_retryable("SUCCEEDED") is False


def test_failure_payload_from_run_row_prefers_run_level_failure():
    run_row = {
        "run_id": "r1",
        "status": "SYSTEM_ERROR",
        "error_type": "system_error",
        "error_message": "runtime failed",
    }

    payload = failure_payload_from_rows(run_row=run_row, trace_event_row=None)

    assert payload["status"] == "FAILED_SYSTEM"
    assert payload["sub_reason"] == "system_error"
    assert payload["message"] == "runtime failed"
    assert payload["source"] == "agent_runs"
    assert payload["node"] is None
    assert payload["retryable"] is True


def test_failure_payload_from_trace_event_row_includes_node_context():
    run_row = {"run_id": "r1", "status": "RUNNING", "error_type": None, "error_message": None}
    trace_event_row = {
        "node": "critic",
        "error_type": "model_timeout",
        "error_message": "upstream timeout",
    }

    payload = failure_payload_from_rows(run_row=run_row, trace_event_row=trace_event_row)

    assert payload == {
        "status": "FAILED_SYSTEM",
        "sub_reason": "model_timeout",
        "message": "upstream timeout",
        "source": "trace_events",
        "node": "critic",
        "retryable": True,
    }


def test_failure_payload_from_sqlite_row_objects():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE agent_runs (run_id TEXT, status TEXT, error_type TEXT, error_message TEXT)")
    conn.execute("CREATE TABLE trace_events (run_id TEXT, node TEXT, error_type TEXT, error_message TEXT)")
    conn.execute("INSERT INTO agent_runs VALUES (?, ?, ?, ?)", ("r1", "SYSTEM_ERROR", "system_error", "run failed"))
    conn.execute("INSERT INTO trace_events VALUES (?, ?, ?, ?)", ("r1", "critic", "model_timeout", "timeout"))
    run_row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", ("r1",)).fetchone()
    event_row = conn.execute("SELECT * FROM trace_events WHERE run_id = ?", ("r1",)).fetchone()

    run_payload = failure_payload_from_rows(run_row=run_row, trace_event_row=None)
    event_payload = failure_payload_from_rows(run_row=run_row, trace_event_row=event_row)

    assert run_payload["status"] == "FAILED_SYSTEM"
    assert run_payload["sub_reason"] == "system_error"
    assert run_payload["message"] == "run failed"
    assert event_payload["status"] == "FAILED_SYSTEM"
    assert event_payload["sub_reason"] == "model_timeout"
    assert event_payload["node"] == "critic"

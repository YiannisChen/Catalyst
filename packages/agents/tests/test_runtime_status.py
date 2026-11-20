from catalyst_agents.runtime.status import (
    build_failure_payload,
    failure_payload_from_rows,
    is_retryable,
    normalize_run_status,
)


def test_normalize_run_status_maps_internal_outputs_to_frontend_statuses():
    assert normalize_run_status("SUFFICIENT") == {
        "status": "SUCCEEDED",
        "sub_reason": None,
    }
    assert normalize_run_status("PARTIAL") == {"status": "PARTIAL", "sub_reason": None}
    assert normalize_run_status("ABSTAIN") == {"status": "ABSTAIN", "sub_reason": None}
    assert normalize_run_status("INSUFFICIENT") == {"status": "ABSTAIN", "sub_reason": None}
    assert normalize_run_status("SYSTEM_ERROR") == {"status": "FAILED_SYSTEM", "sub_reason": "system_error"}


def test_non_material_move_and_inconclusive_remain_sub_reasons():
    assert normalize_run_status("INSUFFICIENT", sub_reason="NON_MATERIAL_MOVE") == {
        "status": "ABSTAIN",
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
    assert is_retryable("ABSTAIN", sub_reason="NON_MATERIAL_MOVE") is False
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


def test_service_get_run_normalizes_persisted_insufficient(tmp_path):
    from catalyst_agents.runtime.service import LiveRunService

    service = LiveRunService(db_path=tmp_path / "trace.db", graph_factory=lambda **_: None)
    conn = service._connect()
    conn.execute(
        """INSERT INTO agent_runs
        (run_id, trace_id, ticker, trade_date, status, queued_at, config, error_type)
        VALUES ('legacy', 'trace-legacy', 'AAPL', '2026-01-15', 'INSUFFICIENT',
                '2026-01-15T21:00:00Z', '{}', 'no_evidence')"""
    )
    conn.commit()
    conn.close()

    run = service.get_run("legacy")
    assert run["status"] == "ABSTAIN"
    assert run["sub_reason"] == "no_evidence"


def test_service_get_events_normalizes_historical_insufficient(tmp_path):
    from catalyst_agents.runtime.service import LiveRunService

    service = LiveRunService(db_path=tmp_path / "trace.db", graph_factory=lambda **_: None)
    conn = service._connect()
    conn.execute(
        "INSERT INTO agent_runs (run_id, trace_id, status) VALUES ('legacy', 'trace-legacy', 'INSUFFICIENT')"
    )
    conn.execute(
        """INSERT INTO trace_events
        (run_id, trace_id, event_seq, node, started_at, ended_at, latency_ms, status_before, status_after)
        VALUES ('legacy', 'trace-legacy', 1, 'finalizer', '2026-01-15T00:00:00Z',
                '2026-01-15T00:00:01Z', 1, 'PARTIAL', 'INSUFFICIENT')"""
    )
    conn.commit()
    conn.close()

    event = service.get_events("legacy")[0]
    assert event["status_before"] == "PARTIAL"
    assert event["status_after"] == "ABSTAIN"


def test_service_losing_run_claim_does_not_invoke_graph_or_remove_credential(tmp_path, monkeypatch):
    import sqlite3

    from catalyst_agents.runtime.runner import LiveRunRunner
    from catalyst_agents.runtime.service import LiveRunService

    db_path = tmp_path / "trace.db"
    graph_calls = []

    class CredentialStore:
        def __init__(self):
            self.removed = []

        def remove(self, run_id):
            self.removed.append(run_id)

    credentials = CredentialStore()
    service = LiveRunService(
        db_path=db_path,
        graph_factory=lambda **kwargs: graph_calls.append(kwargs),
        credential_store=credentials,
    )
    conn = service._connect()
    conn.execute(
        """INSERT INTO agent_runs
        (run_id, trace_id, ticker, trade_date, status, queued_at, config)
        VALUES ('contended', 'trace-contended', 'ACME', '2026-01-15', 'QUEUED', 'now', '{}')"""
    )
    conn.commit()
    conn.close()

    original_claim = LiveRunRunner._mark_running

    def competing_claim(self, run_id):
        # Simulate another executor winning after run_one's read, before its CAS.
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE agent_runs SET status = 'RUNNING' WHERE run_id = ? AND status = 'QUEUED'",
            (run_id,),
        )
        conn.commit()
        conn.close()
        return original_claim(self, run_id)

    monkeypatch.setattr(LiveRunRunner, "_mark_running", competing_claim)

    result = service.run_one("contended")

    assert result == {
        "ok": False,
        "run_id": "contended",
        "status": "RUNNING",
        "failure": {"sub_reason": "run_not_queued", "message": "Run is not in QUEUED state."},
    }
    assert graph_calls == []
    assert credentials.removed == []


def test_runner_setup_failure_terminalizes_claim_and_removes_credential(tmp_path):
    from catalyst_agents.runtime.service import LiveRunService

    class CredentialStore:
        def __init__(self):
            self.removed = []

        def remove(self, run_id):
            self.removed.append(run_id)

    credentials = CredentialStore()
    graph_calls = []
    service = LiveRunService(
        db_path=tmp_path / "trace.db",
        graph_factory=lambda **kwargs: graph_calls.append(kwargs),
        credential_store=credentials,
    )
    conn = service._connect()
    conn.execute(
        """INSERT INTO agent_runs
        (run_id, trace_id, ticker, trade_date, status, queued_at, config)
        VALUES ('bad-config', 'trace-bad-config', 'ACME', '2026-01-15', 'QUEUED', 'now', '{')"""
    )
    conn.commit()
    conn.close()

    result = service.run_one("bad-config")

    assert result == {
        "ok": True,
        "run_id": "bad-config",
        "status": "FAILED_SYSTEM",
        "sub_reason": "system_error",
    }
    assert graph_calls == []
    assert credentials.removed == ["bad-config"]
    conn = service._connect()
    row = conn.execute(
        "SELECT status, error_type, error_message FROM agent_runs WHERE run_id = 'bad-config'"
    ).fetchone()
    conn.close()
    assert row["status"] == "FAILED_SYSTEM"
    assert row["error_type"] == "system_error"
    assert row["error_message"]

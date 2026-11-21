from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest
from catalyst_app.events import run_has_answer_ready
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import EventRepository
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.budget import BudgetAdmission, BudgetLedger
from catalyst_app.runtime.claim import RunClaimer
from catalyst_app.runtime.composition import (
    _default_failure_handler,
    _failure_code_for_exception,
    _sanitized_failure_message,
    store_read_failure_count,
)
from catalyst_data.storage.connect import SQLiteReadTimeoutError
from catalyst_data.storage.errors import StoreReadError


@pytest.mark.parametrize("status", [401, 402, 403])
def test_provider_account_error_fails_run_and_refunds_caller(tmp_path, caplog, status):
    import logging

    from catalyst_app.runtime.composition import provider_account_error_count
    class Response:
        status_code = status
    class APIStatusError(Exception):
        response = Response()
        provider, model, role = "deepseek", "deepseek-chat", "analyst"

    error = APIStatusError("account denied")
    db_path = tmp_path / "provider-failure.sqlite3"
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id,lifecycle_status,request_hash,run_manifest_id,manifest_hash,"
            "capacity_slot,created_at,updated_at,access_tier) "
            "VALUES ('provider-failure','RUNNING','hash','manifest','manifest-hash',0,?,?, 'anonymous')",
            (now.isoformat(), now.isoformat()),
        )
        ledger = BudgetLedger(anonymous_daily_limit=1)
        ledger.reserve(conn, "provider-failure", BudgetAdmission(
            tier="anonymous", caller_key="caller", max_cost_usd=0.05
        ), now=now)
        conn.commit()
    events = EventRepository(db_path=db_path)
    claimer = RunClaimer(db_path=db_path, events=events)
    code = _failure_code_for_exception("SYSTEM_ERROR", error)
    assert code == "PROVIDER_UNAVAILABLE"
    assert _sanitized_failure_message(error) == "Service is temporarily unavailable"
    caplog.set_level(logging.ERROR, logger="catalyst_app.runtime.composition")
    before = provider_account_error_count()
    _default_failure_handler(events, claimer)("provider-failure", code, error)
    assert provider_account_error_count() == before + 1
    alert = next(record for record in caplog.records
                 if getattr(record, "event_name", None) == "provider_account_error")
    assert (alert.provider, alert.model, alert.role, alert.status_code) == (
        "deepseek", "deepseek-chat", "analyst", status
    )
    with open_rw(db_path) as conn:
        row = conn.execute("SELECT lifecycle_status,failure_code FROM runs WHERE run_id='provider-failure'").fetchone()
        ledger.settle_terminal_runs(conn)
        reservation = conn.execute(
            "SELECT quota_charged FROM run_budget_reservations WHERE run_id='provider-failure'"
        ).fetchone()
    assert tuple(row) == ("FAILED", "PROVIDER_UNAVAILABLE")
    assert reservation[0] == 0
    assert run_has_answer_ready(db_path, "provider-failure") is False


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (StoreReadError(
            "private/store.sqlite3", "read sec_exhibit_units",
            "no such table: sec_exhibit_units token=synthetic-secret",
        ), "DATA_UNAVAILABLE"),
        (SQLiteReadTimeoutError("SQLite live read remained locked"), "DATA_UNAVAILABLE"),
    ],
    ids=["store-read-error", "sqlite-read-timeout"],
)
@pytest.mark.parametrize(("cost_usd", "expected_cost_micros"), [(0, 0), (0.01, 10_000)])
def test_store_read_failure_fails_run_refunds_caller_and_emits_alert(
    tmp_path, caplog, error, expected_code, cost_usd, expected_cost_micros
):
    db_path = tmp_path / "runtime.sqlite3"
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id,lifecycle_status,request_hash,run_manifest_id,manifest_hash,"
            "capacity_slot,created_at,updated_at,access_tier) "
            "VALUES ('store-failure','RUNNING','hash','manifest','manifest-hash',0,?,?, 'anonymous')",
            (now.isoformat(), now.isoformat()),
        )
        ledger = BudgetLedger(anonymous_daily_limit=1)
        ledger.reserve(
            conn,
            "store-failure",
            BudgetAdmission(tier="anonymous", caller_key="caller-hash", max_cost_usd=0.05),
            now=now,
        )
        conn.execute(
            "INSERT INTO run_events (run_id,seq,occurred_at,event_type,stage,payload_json,schema_version) "
            "VALUES ('store-failure',1,?,'stage.started','PROVIDER_ACCOUNTING','{}','v1')",
            (now.isoformat(),),
        )
        accounting = json.dumps({"provider": {"cost_usd": cost_usd}})
        conn.execute(
            "INSERT INTO run_artifacts (artifact_id,run_id,event_seq,artifact_type,payload_hash,payload_json) "
            "VALUES ('accounting','store-failure',1,'run_provider_accounting','hash',?)",
            (accounting,),
        )
        conn.commit()

    events = EventRepository(db_path=db_path)
    claimer = RunClaimer(db_path=db_path, events=events)
    failure = _default_failure_handler(events, claimer)
    caplog.set_level(logging.ERROR, logger="catalyst_app.runtime.composition")
    before = store_read_failure_count()

    code = _failure_code_for_exception("SYSTEM_ERROR", error)
    failure("store-failure", code, error)

    assert code == expected_code
    assert _sanitized_failure_message(error) == "Data is temporarily unavailable"
    assert store_read_failure_count() == before + 1
    alert = next(
        record for record in caplog.records
        if getattr(record, "event_name", None) == "data_store_read_failed"
    )
    assert alert.store_path == getattr(error, "store_path", "unknown")
    assert alert.operation == getattr(error, "operation", "sqlite read")
    assert alert.exception_class == type(error).__name__
    expected_exception_message = (
        "no such table: sec_exhibit_units token=[REDACTED]"
        if isinstance(error, StoreReadError)
        else str(error)
    )
    assert alert.exception_message == expected_exception_message
    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT lifecycle_status,failure_code,failure_message FROM runs WHERE run_id='store-failure'"
        ).fetchone()
        ledger.settle_terminal_runs(conn)
        reservation = conn.execute(
            "SELECT settled_cost_micros,quota_charged FROM run_budget_reservations WHERE run_id='store-failure'"
        ).fetchone()
        request = BudgetAdmission(tier="anonymous", caller_key="caller-hash", max_cost_usd=0.05)
        assert ledger.check(conn, request, now=now) is None
        diagnostics = conn.execute(
            "SELECT payload_json FROM run_artifacts "
            "WHERE run_id='store-failure' AND artifact_type='run_failure_diagnostics'"
        ).fetchone()
    assert tuple(row) == ("FAILED", "DATA_UNAVAILABLE", "Data is temporarily unavailable")
    assert tuple(reservation) == (expected_cost_micros, 0)
    assert run_has_answer_ready(db_path, "store-failure") is False
    assert diagnostics is not None
    diagnostic = json.loads(diagnostics[0])
    assert diagnostic["store_path"] == getattr(error, "store_path", "unknown")
    assert diagnostic["operation"] == getattr(error, "operation", "sqlite read")
    assert diagnostic["exception_class"] == type(error).__name__
    assert diagnostic["exception_message"] == expected_exception_message
    assert "synthetic-secret" not in json.dumps(diagnostic)
    assert "document text" not in json.dumps(diagnostic).lower()


from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import EventRepository
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.budget import BudgetAdmission, BudgetLedger
from catalyst_app.runtime.claim import RunClaimer
from catalyst_app.runtime.composition import (
    _default_failure_handler,
    _failure_code_for_exception,
    store_read_failure_count,
)
from catalyst_data.storage.errors import StoreReadError


def test_earnings_timing_store_failure_fails_run_refunds_quota_and_alerts(tmp_path, caplog):
    db_path = tmp_path / "runtime.sqlite3"
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id,lifecycle_status,request_hash,run_manifest_id,manifest_hash,"
            "capacity_slot,created_at,updated_at,access_tier) "
            "VALUES ('d-e-failure','RUNNING','hash','manifest','manifest-hash',0,?,?, 'anonymous')",
            (now.isoformat(), now.isoformat()),
        )
        ledger = BudgetLedger(anonymous_daily_limit=1)
        ledger.reserve(
            conn, "d-e-failure",
            BudgetAdmission(tier="anonymous", caller_key="caller-hash", max_cost_usd=0.05),
            now=now,
        )
        conn.execute(
            "INSERT INTO run_events (run_id,seq,occurred_at,event_type,stage,payload_json,schema_version) "
            "VALUES ('d-e-failure',1,?,'stage.started','EXECUTION','{}','v1')",
            (now.isoformat(),),
        )
        conn.execute(
            "INSERT INTO run_artifacts (artifact_id,run_id,event_seq,artifact_type,payload_hash,payload_json) "
            "VALUES ('accounting','d-e-failure',1,'run_provider_accounting','hash',?)",
            (json.dumps({"provider": {"cost_usd": 0}}),),
        )
        conn.commit()

    events = EventRepository(db_path=db_path)
    claimer = RunClaimer(db_path=db_path, events=events)
    failure = _default_failure_handler(events, claimer)
    error = StoreReadError("/data/news.sqlite3", "read earnings_release_timing", "database is malformed")
    before = store_read_failure_count()
    caplog.set_level(logging.ERROR, logger="catalyst_app.runtime.composition")

    code = _failure_code_for_exception("SYSTEM_ERROR", error)
    failure("d-e-failure", code, error)

    assert code == "DATA_UNAVAILABLE"
    assert store_read_failure_count() == before + 1
    alert = next(record for record in caplog.records if getattr(record, "event_name", None) == "data_store_read_failed")
    assert alert.store_path == "/data/news.sqlite3"
    assert alert.operation == "read earnings_release_timing"
    assert alert.exception_class == "StoreReadError"
    with open_rw(db_path) as conn:
        run = conn.execute(
            "SELECT lifecycle_status,failure_code,failure_message FROM runs WHERE run_id='d-e-failure'"
        ).fetchone()
        ledger.settle_terminal_runs(conn)
        reservation = conn.execute(
            "SELECT settled_cost_micros,quota_charged FROM run_budget_reservations WHERE run_id='d-e-failure'"
        ).fetchone()
        artifact = conn.execute(
            "SELECT payload_json FROM run_artifacts WHERE run_id='d-e-failure' "
            "AND artifact_type='run_failure_diagnostics'"
        ).fetchone()
    assert tuple(run) == ("FAILED", "DATA_UNAVAILABLE", "Data is temporarily unavailable")
    assert tuple(reservation) == (0, 0)
    assert artifact is not None
    diagnostic = json.loads(artifact[0])
    assert diagnostic["store_path"] == "/data/news.sqlite3"
    assert diagnostic["operation"] == "read earnings_release_timing"
    assert diagnostic["exception_class"] == "StoreReadError"
    assert diagnostic["exception_message"] == "database is malformed"

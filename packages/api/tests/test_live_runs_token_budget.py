from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.budget import BudgetAdmission, BudgetLedger


def test_own_key_admission_persists_token_limit_without_dollar_reservation():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_runtime_db(conn)
    now = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO runs (run_id, lifecycle_status, request_hash, run_manifest_id, "
        "manifest_hash, capacity_slot, created_at, updated_at) "
        "VALUES ('byok', 'ACCEPTED', ?, 'manifest', ?, 0, ?, ?)",
        ("a" * 64, "b" * 64, now.isoformat(), now.isoformat()),
    )
    request = BudgetAdmission(
        tier="admin",
        caller_key=None,
        max_cost_usd=0,
        global_budget=False,
        max_tokens=300_000,
    )

    BudgetLedger().reserve(conn, "byok", request, now=now)
    row = conn.execute(
        "SELECT max_run_cost_micros, max_run_tokens, reserved_cost_micros, "
        "global_budget_charged FROM run_budget_reservations WHERE run_id='byok'"
    ).fetchone()

    assert row["max_run_cost_micros"] == 0
    assert row["max_run_tokens"] == 300_000
    assert row["reserved_cost_micros"] == 0
    assert row["global_budget_charged"] == 0


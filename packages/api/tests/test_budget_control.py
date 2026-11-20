from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.budget import BudgetAdmission, BudgetLedger


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    init_runtime_db(conn)
    return conn


def _ledger(*, daily_budget: float = 2.00, anonymous_limit: int = 5) -> BudgetLedger:
    return BudgetLedger(
        daily_model_budget_usd=daily_budget,
        max_run_cost_usd=0.05,
        anonymous_daily_limit=anonymous_limit,
        invite_daily_limit=50,
    )


def _reserve(ledger, conn, run_id, request, now):
    conn.execute(
        "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
        " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
        " VALUES (?, 'ACCEPTED', NULL, ?, ?, ?, 0, ?, ?)",
        (run_id, run_id * 64, f"manifest:{run_id}", "f" * 64, now.isoformat(), now.isoformat()),
    )
    ledger.reserve(conn, run_id, request, now=now)


def _admission(
    *,
    tier: str = "anonymous",
    caller_key: str = "ip-hash-a",
    max_cost: float = 0.05,
    global_budget: bool = True,
) -> BudgetAdmission:
    return BudgetAdmission(
        tier=tier,
        caller_key=caller_key,
        max_cost_usd=max_cost,
        global_budget=global_budget,
    )


@pytest.mark.parametrize("max_run_cost", [float("nan"), float("inf")])
def test_ledger_rejects_nonfinite_per_run_cost_configuration(max_run_cost: float):
    with pytest.raises(ValueError, match="budget limits must be finite and positive"):
        BudgetLedger(max_run_cost_usd=max_run_cost)


def test_default_quota_limits_match_the_owner_decision(monkeypatch):
    for name in (
        "CATALYST_DAILY_MODEL_BUDGET_USD",
        "CATALYST_ANONYMOUS_DAILY_RUN_LIMIT",
        "CATALYST_INVITE_DAILY_RUN_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)

    ledger = BudgetLedger.from_environment()

    assert ledger.daily_model_budget_micros == 2_000_000
    assert ledger.anonymous_daily_limit == 5
    assert ledger.invite_daily_limit == 50

    constructed = BudgetLedger()
    assert constructed.daily_model_budget_micros == 2_000_000
    assert constructed.anonymous_daily_limit == 5
    assert constructed.invite_daily_limit == 50


def test_anonymous_sixth_run_from_same_ip_is_refused_while_another_ip_runs():
    conn = _db()
    ledger = BudgetLedger()
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    same_ip = _admission(caller_key="ip-hash-a")
    other_ip = _admission(caller_key="ip-hash-b")

    for index in range(5):
        assert ledger.check(conn, same_ip, now=now) is None
        _reserve(ledger, conn, f"same-ip-{index}", same_ip, now)

    assert ledger.check(conn, same_ip, now=now) == "rate_limited_ip"
    assert ledger.check(conn, other_ip, now=now) is None


def test_zero_anonymous_limit_refuses_server_key_but_allows_byok():
    conn = _db()
    ledger = BudgetLedger(anonymous_daily_limit=0)
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    assert ledger.check(conn, _admission(), now=now) == "rate_limited_ip"
    assert ledger.check(conn, _admission(global_budget=False), now=now) is None
    assert ledger.remaining(conn, _admission(), now=now) == 0


def test_anonymous_own_key_runs_are_counted_apart_from_the_free_demo():
    conn = _db()
    ledger = BudgetLedger(anonymous_daily_limit=1, anonymous_byok_daily_limit=2)
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    demo, own = _admission(), _admission(global_budget=False)
    _reserve(ledger, conn, "demo-1", demo, now)
    assert ledger.check(conn, demo, now=now) == "rate_limited_ip"
    assert ledger.check(conn, own, now=now) is None
    _reserve(ledger, conn, "own-1", own, now)
    _reserve(ledger, conn, "own-2", own, now)
    assert ledger.check(conn, own, now=now) == "rate_limited_ip"
    assert ledger.remaining(conn, demo, now=now) == 0


def test_caller_quota_counts_only_accepted_reservations_and_refunds_service_failures():
    conn = _db()
    ledger = _ledger(anonymous_limit=1)
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    first = _admission()

    assert ledger.check(conn, first, now=now) is None
    _reserve(ledger, conn, "run-1", first, now)
    assert ledger.check(conn, first, now=now) == "rate_limited_ip"

    ledger.settle(conn, "run-1", actual_cost_usd=0, refund_caller=True)
    assert ledger.check(conn, first, now=now) is None


def test_invite_quota_is_counted_per_token():
    conn = _db()
    ledger = BudgetLedger(invite_daily_limit=2)
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    invite = _admission(tier="invite", caller_key="token-a")

    assert ledger.check(conn, invite, now=now) is None
    _reserve(ledger, conn, "invite-1", invite, now)
    assert ledger.check(conn, invite, now=now) is None
    _reserve(ledger, conn, "invite-2", invite, now)
    assert ledger.check(conn, invite, now=now) == "rate_limited_token"
    assert ledger.check(
        conn, _admission(tier="invite", caller_key="token-b"), now=now
    ) is None


def test_global_budget_reserves_then_settles_actual_cost_before_next_admission():
    conn = _db()
    ledger = _ledger(daily_budget=0.08)
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    first = _admission(max_cost=0.05)
    second = _admission(caller_key="ip-hash-b", max_cost=0.05)

    assert ledger.check(conn, first, now=now) is None
    _reserve(ledger, conn, "run-1", first, now)
    assert ledger.check(conn, second, now=now) == "daily_budget_exhausted"

    ledger.settle(conn, "run-1", actual_cost_usd=0.02)
    assert ledger.check(conn, second, now=now) is None


def test_byok_and_admin_do_not_reserve_public_global_budget():
    conn = _db()
    ledger = _ledger(daily_budget=0.05, anonymous_limit=1)
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    byok = _admission(global_budget=False)
    admin = _admission(tier="admin", caller_key="", global_budget=False)
    anonymous = _admission(caller_key="ip-hash-b")

    assert ledger.check(conn, byok, now=now) is None
    _reserve(ledger, conn, "byok", byok, now)
    # own-key runs are counted apart from the one-run free demo (CATALYST_ANONYMOUS_BYOK_DAILY_RUN_LIMIT)
    assert ledger.check(conn, byok, now=now) is None
    assert ledger.check(conn, _admission(), now=now) is None
    assert ledger.check(conn, admin, now=now) is None
    _reserve(ledger, conn, "admin", admin, now)
    assert ledger.check(conn, anonymous, now=now) is None


@pytest.mark.parametrize(
    "value", [0, -0.01, float("nan"), float("inf"), True, "0.01"]
)
def test_per_run_limit_rejects_invalid_values(value):
    from types import SimpleNamespace

    from catalyst_app.routers.live_runs import _budget_max_cost
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        _budget_max_cost(SimpleNamespace(max_cost_usd=value), _ledger())
    assert error.value.status_code == 400
    assert error.value.detail == "budget_invalid"


def test_per_run_limit_is_lowerable_and_capped_at_service_limit():
    from types import SimpleNamespace

    from catalyst_app.routers.live_runs import _budget_max_cost

    ledger = _ledger()
    assert _budget_max_cost(SimpleNamespace(max_cost_usd=0.02), ledger) == 0.02
    assert _budget_max_cost(SimpleNamespace(max_cost_usd=0.10), ledger) == 0.05


def test_unavailable_actual_cost_is_charged_at_reserved_bound():
    conn = _db()
    ledger = _ledger(daily_budget=0.08)
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    first = _admission(max_cost=0.05)

    assert ledger.check(conn, first, now=now) is None
    _reserve(ledger, conn, "run-1", first, now)
    ledger.settle(conn, "run-1", actual_cost_usd=None)

    assert ledger.check(conn, _admission(caller_key="ip-hash-b", max_cost=0.05), now=now) == "daily_budget_exhausted"


@pytest.mark.parametrize(
    "diagnostics_payload",
    [
        {"provider": {"cost_usd": 0}},
        {"provider": {}},
        {},
    ],
)
def test_terminal_recovery_uses_provider_accounting_not_later_diagnostics(diagnostics_payload):
    conn = _db()
    ledger = _ledger()
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    request = _admission()
    _reserve(ledger, conn, "run-1", request, now)
    conn.execute(
        "UPDATE runs SET lifecycle_status='COMPLETED' WHERE run_id='run-1'"
    )
    payload = json.dumps({"provider": {"cost_usd": 0.012}})
    conn.execute(
        "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version) "
        "VALUES ('run-1', 1, ?, 'stage.started', 'PROVIDER_ACCOUNTING', '{}', 'v1')",
        (now.isoformat(),),
    )
    conn.execute(
        "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json) "
        "VALUES ('accounting-1', 'run-1', 1, 'run_provider_accounting', 'hash', ?)",
        (payload,),
    )
    conn.execute(
        "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json) "
        "VALUES ('diagnostics-1', 'run-1', 1, 'run_diagnostics', 'hash', ?)",
        (json.dumps(diagnostics_payload),),
    )

    ledger.settle_terminal_runs(conn)
    ledger.settle_terminal_runs(conn)

    row = conn.execute(
        "SELECT settled_cost_micros, quota_charged FROM run_budget_reservations WHERE run_id='run-1'"
    ).fetchone()
    assert row["settled_cost_micros"] == 12_000
    assert row["quota_charged"] == 1


def test_terminal_recovery_keeps_reservation_and_quota_when_accounting_is_missing():
    conn = _db()
    ledger = _ledger(anonymous_limit=1)
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    request = _admission()
    _reserve(ledger, conn, "run-1", request, now)
    conn.execute(
        "UPDATE runs SET lifecycle_status='FAILED', failure_code='SYSTEM_ERROR' "
        "WHERE run_id='run-1'"
    )
    conn.execute(
        "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version) "
        "VALUES ('run-1', 1, ?, 'stage.started', 'DIAGNOSTICS', '{}', 'v1')",
        (now.isoformat(),),
    )
    conn.execute(
        "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json) "
        "VALUES ('diagnostics-zero-only', 'run-1', 1, 'run_diagnostics', 'hash', ?)",
        (json.dumps({"provider": {"cost_usd": 0}}),),
    )

    ledger.settle_terminal_runs(conn)
    ledger.settle_terminal_runs(conn)

    row = conn.execute(
        "SELECT settled_cost_micros, quota_charged FROM run_budget_reservations WHERE run_id='run-1'"
    ).fetchone()
    assert row["settled_cost_micros"] == 50_000
    assert row["quota_charged"] == 1
    assert ledger.check(conn, request, now=now) == "rate_limited_ip"


@pytest.mark.parametrize("cost_usd", [0, 0.01])
@pytest.mark.parametrize("failure_code", ["DATA_UNAVAILABLE", "PROVIDER_UNAVAILABLE"])
def test_provider_unavailable_refund_matches_data_unavailable_with_authoritative_cost(
    cost_usd, failure_code
):
    conn = _db()
    ledger = _ledger(anonymous_limit=1)
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    request = _admission()
    _reserve(ledger, conn, "run-1", request, now)
    conn.execute(
        "UPDATE runs SET lifecycle_status='FAILED', failure_code=? WHERE run_id='run-1'",
        (failure_code,),
    )
    conn.execute(
        "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version) "
        "VALUES ('run-1', 1, ?, 'stage.started', 'PROVIDER_ACCOUNTING', '{}', 'v1')",
        (now.isoformat(),),
    )
    conn.execute(
        "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json) "
        "VALUES ('accounting', 'run-1', 1, 'run_provider_accounting', 'hash', ?)",
        (json.dumps({"provider": {"cost_usd": cost_usd}}),),
    )
    ledger.settle_terminal_runs(conn)
    row = conn.execute(
        "SELECT quota_charged FROM run_budget_reservations WHERE run_id='run-1'"
    ).fetchone()
    assert row["quota_charged"] == 0
    assert ledger.check(conn, request, now=now) is None


def test_service_failure_refunds_caller_only_when_authoritative_cost_is_zero():
    conn = _db()
    ledger = _ledger(anonymous_limit=1)
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    request = _admission()
    _reserve(ledger, conn, "run-1", request, now)

    ledger.settle(conn, "run-1", actual_cost_usd=0.01, refund_caller=True)
    assert ledger.check(conn, request, now=now) == "rate_limited_ip"

    _reserve(ledger, conn, "run-2", request, now)
    conn.execute(
        "UPDATE runs SET lifecycle_status='FAILED', failure_code='MODEL_PRICE_UNAVAILABLE' "
        "WHERE run_id='run-2'"
    )
    conn.execute(
        "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version) "
        "VALUES ('run-2', 1, ?, 'stage.started', 'PROVIDER_ACCOUNTING', '{}', 'v1')",
        (now.isoformat(),),
    )
    conn.execute(
        "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json) "
        "VALUES ('accounting-zero', 'run-2', 1, 'run_provider_accounting', 'hash', ?)",
        (json.dumps({"provider": {"cost_usd": 0}}),),
    )
    ledger.settle_terminal_runs(conn)
    row = conn.execute(
        "SELECT quota_charged FROM run_budget_reservations WHERE run_id='run-2'"
    ).fetchone()
    assert row["quota_charged"] == 1
    assert ledger.check(conn, request, now=now) == "rate_limited_ip"

    # A genuine service failure is refundable only after authoritative zero cost.
    conn = _db()
    ledger = _ledger(anonymous_limit=1)
    _reserve(ledger, conn, "service-failure", request, now)
    conn.execute(
        "UPDATE runs SET lifecycle_status='FAILED', failure_code='PROVIDER_UNAVAILABLE' "
        "WHERE run_id='service-failure'"
    )
    conn.execute(
        "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version) "
        "VALUES ('service-failure', 1, ?, 'stage.started', 'PROVIDER_ACCOUNTING', '{}', 'v1')",
        (now.isoformat(),),
    )
    conn.execute(
        "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json) "
        "VALUES ('accounting-zero-service', 'service-failure', 1, 'run_provider_accounting', 'hash', ?)",
        (json.dumps({"provider": {"cost_usd": 0}}),),
    )
    ledger.settle_terminal_runs(conn)
    assert ledger.check(conn, request, now=now) is None


def test_provider_price_failure_code_is_non_refundable():
    from catalyst_app.runtime.budget import _SERVICE_FAILURES_REFUNDING_CALLER

    assert "MODEL_PRICE_UNAVAILABLE" not in _SERVICE_FAILURES_REFUNDING_CALLER

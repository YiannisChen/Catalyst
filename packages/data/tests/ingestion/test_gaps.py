from __future__ import annotations

import sqlite3

import pytest
from catalyst_data.ingestion.gaps import GapLedger


def ledger():
    return GapLedger(sqlite3.connect(":memory:"))


def base(**changes):
    values = {
        "session_date": "2026-10-23",
        "ticker": "AAPL",
        "source_id": "alpaca_news:news:poll",
        "scope_kind": "target",
        "scope_id": "AAPL",
        "status": "OK",
        "requested_from": "2026-10-23T13:30:00Z",
        "requested_until": "2026-10-23T20:00:00Z",
        "coverage_from": "2026-10-23T13:30:00Z",
        "coverage_until": "2026-10-23T20:00:00Z",
        "cursor": "cursor-2",
        "pagination_complete": True,
    }
    return values | changes


def test_records_category_from_explicit_scope_and_mapping_version():
    row = ledger().record(**base())

    assert row.category == "target_news"
    assert row.source_id == "alpaca_news:news:poll"
    assert row.mapping_version == 1
    assert row.scope_kind == "target"
    assert row.scope_id == "AAPL"


def test_mapping_rejects_a_category_that_conflicts_with_registry_route():
    with pytest.raises(ValueError, match="category"):
        ledger().record(**base(category="peer_news"))


def test_coverage_and_pagination_are_recorded_separately_from_request_status():
    api = ledger()
    complete = api.record(**base())
    partial = api.record(
        **base(
            source_id="daily:sec",
            scope_kind="reference",
            scope_id="AAPL",
            status="OK",
            coverage_until="2026-10-23T18:00:00Z",
            cursor="page-2",
            pagination_complete=False,
        )
    )

    assert complete.status == "OK"
    assert complete.coverage_until == complete.requested_until
    assert complete.pagination_complete is True
    assert partial.status == "OK"
    assert partial.coverage_until < partial.requested_until
    assert partial.pagination_complete is False


@pytest.mark.parametrize(
    ("status", "reason"),
    [("EMPTY", None), ("ERROR", "ERROR"), ("NOT_FETCHED", "DISABLED")],
)
def test_missing_disabled_and_error_states_are_retained(status, reason):
    row = ledger().record(**base(status=status, reason=reason))
    assert (row.status, row.reason) == (status, reason)


def test_reason_must_match_the_contract_reason_classes():
    with pytest.raises(ValueError, match="reason"):
        ledger().record(**base(status="ERROR", reason="arbitrary prose"))
    with pytest.raises(ValueError, match="reason"):
        ledger().record(**base(status="NOT_FETCHED", reason="timeout"))
    with pytest.raises(ValueError, match="require NOT_FETCHED"):
        ledger().record(**base(status="OK", reason="DISABLED"))
    with pytest.raises(ValueError, match="require NOT_FETCHED"):
        ledger().record(**base(status="EMPTY", reason="breaker"))


def test_backfill_task_updates_are_idempotent_and_keep_logical_identity():
    api = ledger()
    first = api.record(
        **base(
            progress_state="queued",
            logical_task_id="task-1",
            target_count=3,
            completed_count=0,
        )
    )
    same = api.record(
        **base(
            progress_state="queued",
            logical_task_id="task-1",
            target_count=3,
            completed_count=0,
        )
    )
    running = api.record(
        **base(
            progress_state="running",
            logical_task_id="task-1",
            target_count=3,
            completed_count=1,
        )
    )

    assert first.id == same.id == running.id
    assert running.progress_state == "running"
    assert running.target_count == 3
    assert running.completed_count == 1
    assert len(api.list_rows()) == 1


def test_backfill_transition_cannot_regress_or_exceed_target_count():
    api = ledger()
    api.record(
        **base(
            progress_state="running",
            logical_task_id="task-1",
            target_count=2,
            completed_count=1,
        )
    )
    with pytest.raises(ValueError, match="progress"):
        api.record(
            **base(
                progress_state="queued",
                logical_task_id="task-1",
                target_count=2,
                completed_count=0,
            )
        )
    with pytest.raises(ValueError, match="completed_count"):
        api.record(
            **base(
                progress_state="completed",
                logical_task_id="task-1",
                target_count=2,
                completed_count=3,
            )
        )


def test_provider_identity_is_internal_and_public_category_is_contract_category():
    api = ledger()
    api.record(**base())
    row = api.list_rows(category="target_news")[0]
    public = row.to_public_dict()

    assert public["category"] == "target_news"
    assert "provider" not in public
    assert "source_id" not in public


def test_public_rows_can_filter_by_session_category_and_ticker():
    api = ledger()
    api.record(**base())
    api.record(**base(ticker="MSFT", scope_id="MSFT", session_date="2026-10-24"))
    api.record(**base(ticker="AAPL", scope_id="AAPL", session_date="2026-10-24", source_id="daily:sec",
                      scope_kind="reference", status="EMPTY", reason=None))

    rows = api.public_rows(category="target_news", session_date="2026-10-23", ticker="AAPL")

    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAPL"
    assert rows[0]["category"] == "target_news"
    assert "source_id" not in rows[0]


def test_unknown_source_or_ambiguous_news_scope_fails_closed():
    with pytest.raises(ValueError, match="source"):
        ledger().record(**base(source_id="unknown:provider"))
    with pytest.raises(ValueError, match="scope"):
        ledger().record(**base(scope_kind="reference"))


def test_supplied_connection_keeps_transaction_control_with_caller():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sentinel (value TEXT)")
    conn.execute("INSERT INTO sentinel VALUES ('uncommitted')")
    api = GapLedger(conn)
    api.record(**base())

    conn.rollback()

    assert conn.execute("SELECT COUNT(*) FROM sentinel").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='category_gaps'").fetchone()[0] == 0

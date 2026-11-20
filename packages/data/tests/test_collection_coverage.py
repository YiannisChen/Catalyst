from __future__ import annotations

import sqlite3

import pytest
from catalyst_data.ingestion import coverage

NOW = "2026-10-26T10:00:00Z"
START = "2026-10-26T00:00:00Z"
END = "2026-10-27T00:00:00Z"


def db():
    return sqlite3.connect(":memory:")


def start(conn, *, attempt_id="attempt-1", query_spec=None, **kwargs):
    values = dict(logical_task_id="task-1", source_id="provider-a", product="full_text",
                  subject_kind="ticker", subject_id="ACME",
                  query_spec={"symbols": ["ACME"]} if query_spec is None else query_spec,
                  coverage_policy_version="policy-v1", requested_from=START, requested_until=END)
    values.update(kwargs)
    return coverage.start_attempt(conn, attempt_id=attempt_id, **values)


def event(conn, *, coverage_id="event-1", attempt_id="attempt-1", **kwargs):
    values = dict(status="OK", recorded_at=NOW,
                  expected_targets=("https://news.example/a?utm_source=x",),
                  acquired_targets=("https://news.example/a",))
    values.update(kwargs)
    return coverage.record_coverage(conn, coverage_id=coverage_id, attempt_id=attempt_id, **values)


def test_schema_is_additive_and_remains_in_the_callers_transaction():
    conn = db()
    conn.execute("BEGIN")
    coverage.ensure_schema(conn)
    assert conn.in_transaction
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"source_coverage_attempts", "source_coverage_facts", "source_coverage_targets"} <= names
    columns = {row[1] for row in conn.execute("PRAGMA table_info(source_coverage_facts)")}
    assert not columns & {"case_id", "ticker", "target", "peer", "category"}
    conn.rollback()
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='source_coverage_facts'").fetchone() is None


def test_start_attempt_allocates_sequence_per_stable_partition_and_latest_snapshot():
    conn = db()
    coverage.ensure_schema(conn)
    first = start(conn)
    second = start(conn, attempt_id="attempt-2")
    other = start(conn, attempt_id="attempt-3", query_spec={"symbols": ["OTHER"]})
    assert first.partition_id == second.partition_id
    assert (first.attempt_seq, second.attempt_seq, other.attempt_seq) == (1, 2, 1)
    replay = start(conn, query_spec={"symbols": ["ACME"]},
                   requested_from="2026-10-26T00:00:00+00:00",
                   requested_until="2026-10-27T00:00:00+00:00")
    assert replay == first
    first_fact = event(conn)
    second_fact = event(conn, attempt_id="attempt-2", coverage_id="event-2", status="EMPTY",
                        expected_targets=(), acquired_targets=(), enumeration_complete=True)
    assert first_fact.state_seq == 1
    assert second_fact.state_seq == 1
    latest = coverage.latest_coverage(conn)
    selected = {snapshot.attempt.partition_id: snapshot for snapshot in latest}
    assert selected[first.partition_id].fact.coverage_id == "event-2"
    assert selected[first.partition_id].attempt.attempt_seq == 2
    assert selected[other.partition_id].fact is None


def test_exact_replay_is_idempotent_and_conflicting_replay_fails():
    conn = db()
    coverage.ensure_schema(conn)
    start(conn)
    first = event(conn)
    assert event(conn) == first
    with pytest.raises(coverage.CoverageError, match="conflict"):
        event(conn, status="EMPTY")
    assert coverage.list_facts(conn)[0] == first


def test_event_and_memberships_are_append_only_even_under_replace():
    conn = db()
    coverage.ensure_schema(conn)
    start(conn)
    event(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE source_coverage_facts SET status='EMPTY' WHERE coverage_id='event-1'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM source_coverage_facts WHERE coverage_id='event-1'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT OR REPLACE INTO source_coverage_facts SELECT * FROM source_coverage_facts WHERE coverage_id='event-1'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM source_coverage_targets")


def test_full_text_unknown_counts_remain_unknown_until_enumerated():
    conn = db()
    coverage.ensure_schema(conn)
    start(conn)
    fact = coverage.record_coverage(conn, coverage_id="event-unknown", attempt_id="attempt-1",
                                    status="OK", recorded_at=NOW)
    assert fact.expected_target_count is None
    assert fact.acquired_target_count is None
    partial = coverage.record_coverage(conn, coverage_id="event-partial", attempt_id="attempt-1",
                                       status="OK", recorded_at=NOW,
                                       expected_targets=("article-a",))
    assert partial.expected_target_count == 1
    assert partial.acquired_target_count is None
    assert coverage.list_targets(conn, coverage_id="event-partial")[0].acquired is None


def test_full_text_target_identity_deduplication_and_counts():
    conn = db()
    coverage.ensure_schema(conn)
    start(conn)
    fact = coverage.record_coverage(
        conn, coverage_id="event-1", attempt_id="attempt-1", status="OK", recorded_at=NOW,
        expected_targets=("https://news.example/a?utm_source=x", "https://news.example/a", "provider-article-7"),
        acquired_targets=("https://news.example/a", "provider-article-7", "provider-article-7"),
    )
    assert (fact.expected_target_count, fact.acquired_target_count) == (2, 2)
    assert len(coverage.list_targets(conn, coverage_id="event-1")) == 2
    assert {row.identity for row in coverage.list_targets(conn, coverage_id="event-1") if row.acquired} == {
        "https://news.example/a", "provider:provider-a:provider-article-7"
    }


def test_latest_snapshot_does_not_fall_back_across_tasks_or_empty_attempts():
    conn = db()
    coverage.ensure_schema(conn)
    first = start(conn)
    event(conn)
    second = coverage.start_attempt(
        conn, logical_task_id="task-2", attempt_id="attempt-2", source_id="provider-a",
        product="full_text", subject_kind="ticker", subject_id="ACME",
        query_spec={"symbols": ["ACME"]}, coverage_policy_version="policy-v1",
        requested_from=START, requested_until=END,
    )
    assert second.partition_id == first.partition_id
    assert coverage.latest_coverage(conn, logical_task_id="task-1") == []
    snapshots = coverage.latest_coverage(conn, logical_task_id="task-2")
    assert len(snapshots) == 1 and snapshots[0].fact is None
    assert snapshots[0].targets == ()


def test_target_memberships_reject_unknown_acquired_identity():
    conn = db()
    coverage.ensure_schema(conn)
    start(conn)
    with pytest.raises(coverage.CoverageError, match="expected"):
        coverage.record_coverage(conn, coverage_id="event-1", attempt_id="attempt-1", status="OK",
                                 recorded_at=NOW, expected_targets=("article-a",), acquired_targets=("article-b",))


def test_non_full_text_counts_and_state_fields_are_validated():
    conn = db()
    coverage.ensure_schema(conn)
    attempt = coverage.start_attempt(
        conn, logical_task_id="task", attempt_id="bars-1", source_id="provider-a", product="bars",
        subject_kind="instrument", subject_id="CUSIP:1", query_spec={"interval": "1d"},
        coverage_policy_version="v1", requested_from=START, requested_until=END,
    )
    assert attempt.attempt_seq == 1
    with pytest.raises(coverage.CoverageError):
        coverage.record_coverage(conn, coverage_id="bad-count", attempt_id="bars-1", status="OK",
                                 recorded_at=NOW, expected_target_count=True, acquired_target_count=0)
    with pytest.raises(coverage.CoverageError):
        coverage.record_coverage(conn, coverage_id="bad-range", attempt_id="bars-1", status="OK",
                                 recorded_at=NOW, coverage_from=END, coverage_until=START)
    with pytest.raises(coverage.CoverageError):
        coverage.record_coverage(conn, coverage_id="bad-reason", attempt_id="bars-1", status="NOT_FETCHED",
                                 reason="ERROR", recorded_at=NOW)
    with pytest.raises(coverage.CoverageError):
        coverage.start_attempt(conn, logical_task_id="task", attempt_id="bad-product", source_id="provider-a",
                               product="prices", subject_kind="instrument", subject_id="CUSIP:1",
                               query_spec={}, coverage_policy_version="v1", requested_from=START,
                               requested_until=END)


def test_invalid_scopes_timestamps_and_unsafe_queries_are_rejected():
    conn = db()
    coverage.ensure_schema(conn)
    base = dict(logical_task_id="task", source_id="provider-a", product="search", subject_kind="query",
                subject_id="earnings", query_spec={}, coverage_policy_version="v1",
                requested_from=START, requested_until=END)
    for changes in (
        {"subject_kind": "category"},
        {"requested_from": "2026-10-26T00:00:00"},
        {"requested_until": START},
        {"query_spec": {"api_key": "secret"}},
        {"query_spec": "ticker = 'ACME'; DROP TABLE"},
    ):
        with pytest.raises(coverage.CoverageError):
            coverage.start_attempt(conn, attempt_id=f"bad-{len(changes)}-{list(changes)[0]}", **(base | changes))


def test_latest_attempt_and_state_order_are_monotonic_and_task_scoped():
    conn = db()
    coverage.ensure_schema(conn)
    start(conn)
    event(conn, coverage_id="event-1", cursor="page-1")
    event(conn, coverage_id="event-2", cursor="page-2")
    with pytest.raises(coverage.CoverageError, match="sequence"):
        coverage.record_coverage(conn, coverage_id="event-0", attempt_id="attempt-1", status="OK",
                                 recorded_at="2026-10-26T09:00:00Z")
    start(conn, attempt_id="attempt-2")
    event(conn, coverage_id="event-3", attempt_id="attempt-2", status="NOT_FETCHED", reason="breaker",
          expected_targets=(), acquired_targets=())
    assert coverage.latest_coverage(conn, logical_task_id="task-1")[0].fact.coverage_id == "event-3"
    assert coverage.latest_coverage(conn, logical_task_id="missing") == []


def test_helpers_never_commit_callers_work_on_success_or_failure():
    conn = db()
    conn.execute("CREATE TABLE owner_data(value TEXT)")
    conn.commit()
    conn.execute("INSERT INTO owner_data VALUES('outer')")
    coverage.ensure_schema(conn)
    start(conn)
    with pytest.raises(coverage.CoverageError):
        event(conn, status="UNKNOWN")
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM owner_data").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='source_coverage_facts'").fetchone() is None


def test_fractional_window_timestamps_preserve_distinct_instants():
    conn = db()
    coverage.ensure_schema(conn)
    first = start(conn, requested_from="2026-10-26T00:00:00.123456Z",
                   requested_until="2026-10-27T00:00:00.987654Z")
    second = start(conn, attempt_id="attempt-fraction-2",
                   requested_from="2026-10-26T00:00:00.654321Z",
                   requested_until="2026-10-27T00:00:00.987654Z")
    assert first.partition_id != second.partition_id
    assert first.requested_from.endswith("00:00:00.123456+00:00")
    assert second.requested_from.endswith("00:00:00.654321+00:00")
    fact = coverage.record_coverage(
        conn, coverage_id="fractional-event", attempt_id="attempt-1", status="OK",
        recorded_at="2026-10-26T10:00:00.123456Z",
        coverage_from="2026-10-26T00:00:00.123456Z",
        coverage_until="2026-10-26T12:00:00.654321Z",
    )
    assert fact.recorded_at.endswith("10:00:00.123456+00:00")
    assert fact.coverage_from.endswith("00:00:00.123456+00:00")
    assert fact.coverage_until.endswith("12:00:00.654321+00:00")


def test_latest_snapshot_carries_explicit_full_text_target_memberships():
    conn = db()
    coverage.ensure_schema(conn)
    start(conn)
    event(conn, expected_targets=("https://news.example/story/amp?amp=1", "article-7"),
          acquired_targets=("https://news.example/story",))
    snapshot = coverage.latest_coverage(conn)[0]
    assert [(target.identity, target.acquired) for target in snapshot.targets] == [
        ("https://news.example/story", True),
        ("provider:provider-a:article-7", False),
    ]


def test_coverage_bounds_must_be_complete_ordered_and_inside_requested_window():
    conn = db()
    coverage.ensure_schema(conn)
    start(conn)
    for changes in (
        {"coverage_from": "2026-10-26T01:00:00Z"},
        {"coverage_until": "2026-10-26T02:00:00Z"},
        {"coverage_from": "2026-10-26T03:00:00Z", "coverage_until": "2026-10-26T02:00:00Z"},
        {"coverage_from": "2026-10-25T23:59:59Z", "coverage_until": "2026-10-26T02:00:00Z"},
        {"coverage_from": "2026-10-26T23:00:00Z", "coverage_until": "2026-10-27T00:00:01Z"},
    ):
        with pytest.raises(coverage.CoverageError):
            coverage.record_coverage(conn, coverage_id=f"bad-bounds-{len(changes)}-{list(changes)[0]}",
                                     attempt_id="attempt-1", status="OK", recorded_at=NOW, **changes)


def test_unhashable_product_and_subject_kind_raise_coverage_error():
    conn = db()
    coverage.ensure_schema(conn)
    for field, value in (("product", []), ("subject_kind", [])):
        args = dict(logical_task_id="task", attempt_id=f"bad-{field}", source_id="provider-a",
                    product="search", subject_kind="query", subject_id="query-a", query_spec={},
                    coverage_policy_version="v1", requested_from=START, requested_until=END)
        args[field] = value
        with pytest.raises(coverage.CoverageError):
            coverage.start_attempt(conn, **args)


def test_full_text_counts_require_explicit_target_identities():
    conn = db()
    coverage.ensure_schema(conn)
    start(conn)
    for suffix, expected_count, acquired_count in (("zero", 0, 0), ("nonzero", 1, 0)):
        with pytest.raises(coverage.CoverageError):
            coverage.record_coverage(
                conn, coverage_id=f"count-only-{suffix}", attempt_id="attempt-1", status="EMPTY",
                recorded_at=NOW, expected_target_count=expected_count,
                acquired_target_count=acquired_count,
            )
    unknown = coverage.record_coverage(conn, coverage_id="count-unknown", attempt_id="attempt-1",
                                       status="OK", recorded_at=NOW)
    assert unknown.expected_target_count is None
    assert unknown.acquired_target_count is None

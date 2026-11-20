"""Accounting cohorts describe logical work, never a sum of unlike counters."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from catalyst_data.ingestion import fetch_accounting as A

T = datetime(2026, 10, 23, tzinfo=timezone.utc)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    A.ensure_schema(conn)
    yield conn
    conn.close()


def task(db, ident="t1", source="alpaca", at=T):
    A.register_task(db, source_id=source, task_id=ident, at=at)


def request(db, ident="r1", owner="t1", source="alpaca", at=T):
    A.register_request(db, source_id=source, task_id=owner, request_id=ident, at=at)


def attempt(db, ident="a1", owner="r1", outcome="success", source="alpaca", at=T):
    A.record_attempt(db, source_id=source, request_id=owner, attempt_id=ident,
                     outcome=outcome, at=at)


def report(db, cutoff=T + timedelta(hours=1), sources=("alpaca",)):
    return A.report(db, start=T, end=T + timedelta(hours=1), as_of=cutoff,
                    expected_sources=sources)["sources"]["alpaca"]


def test_multiple_requests_and_retry_failures_do_not_inflate_task_completion(db):
    task(db)
    request(db)
    request(db, "r2")
    attempt(db, outcome="failure")
    attempt(db, "a2", at=T + timedelta(seconds=1))
    attempt(db, "a3", owner="r2")
    A.set_task_state(db, source_id="alpaca", task_id="t1", event_id="done",
                     state="succeeded", at=T + timedelta(seconds=2))
    r = report(db)
    assert r["requests"]["attempts"] == 3
    assert r["requests"]["successes"] == 2
    assert r["requests"]["rate"] == pytest.approx(2 / 3)
    assert r["tasks"]["registered"] == r["tasks"]["succeeded"] == 1
    assert r["tasks"]["rate"] == 1


def test_repeated_registration_and_event_replay_are_idempotent(db):
    task(db)
    task(db)
    request(db)
    request(db)
    attempt(db)
    attempt(db)
    r = report(db)
    assert r["tasks"]["registered"] == 1
    assert r["tasks"]["pending"] == 1
    assert r["requests"]["attempts"] == 1


def test_expected_target_denominator_is_registered_before_acquisition(db):
    task(db)
    task(db, "t2")
    for owner, target_id in (("t1", "article1"), ("t2", "article1"), ("t1", "article2")):
        A.register_target(db, source_id="alpaca", task_id=owner, target_id=target_id, at=T)
    A.set_target_state(db, source_id="alpaca", target_id="article1", event_id="body1",
                       state="acquired", at=T)
    r = report(db)["targets"]
    assert r["registered"] == 2
    assert r["acquired"] == r["pending"] == 1
    assert r["rate"] == 0.5
    assert r["terminal"] == 1
    assert r["terminal_rate"] == 1


def test_lead_only_source_has_no_fabricated_body_success_rate(db):
    task(db)
    r = report(db)["targets"]
    assert r["registered"] == 0
    assert r["rate"] is None
    assert r["terminal_rate"] is None


def test_pending_failed_and_terminal_denominators_are_explicit(db):
    task(db)
    task(db, "t2")
    A.set_task_state(db, source_id="alpaca", task_id="t1", event_id="failure",
                     state="failed", at=T)
    r = report(db)["tasks"]
    assert (r["registered"], r["pending"], r["failed"], r["terminal"]) == (2, 1, 1, 1)
    assert r["rate"] == r["terminal_rate"] == 0


def test_breaker_skip_is_not_a_physical_http_attempt(db):
    task(db)
    request(db)
    attempt(db, outcome="skipped_breaker")
    r = report(db)["requests"]
    assert r["logical_requests"] == r["skipped_breaker"] == 1
    assert r["attempts"] == 0
    assert r["rate"] is None


def test_historical_cutoff_excludes_later_success_and_later_registration(db):
    task(db)
    request(db)
    A.register_target(db, source_id="alpaca", task_id="t1", target_id="body", at=T)
    later = T + timedelta(minutes=20)
    attempt(db, at=later)
    A.set_task_state(db, source_id="alpaca", task_id="t1", event_id="done",
                     state="succeeded", at=later)
    A.set_target_state(db, source_id="alpaca", target_id="body", event_id="bodydone",
                       state="acquired", at=later)
    task(db, "t2", at=later)
    r = report(db, T + timedelta(minutes=10))
    assert r["tasks"]["registered"] == r["tasks"]["pending"] == 1
    assert r["tasks"]["succeeded"] == r["targets"]["acquired"] == 0
    assert r["requests"]["attempts"] == 0


def test_half_open_cohort_excludes_end_boundary_and_old_tasks(db):
    task(db, "old", at=T - timedelta(seconds=1))
    task(db)
    task(db, "next", at=T + timedelta(hours=1))
    assert report(db)["tasks"]["registered"] == 1


def test_target_identity_is_unique_per_source_not_global(db):
    task(db)
    task(db, "other", source="other")
    for source, owner in (("alpaca", "t1"), ("other", "other")):
        A.register_target(db, source_id=source, task_id=owner, target_id="same", at=T)
    assert report(db)["targets"]["registered"] == 1


def test_conflicting_ids_source_mismatch_and_unknown_owner_are_rejected(db):
    task(db)
    request(db)
    attempt(db)
    with pytest.raises(A.AccountingError):
        task(db, source="other")
    with pytest.raises(A.AccountingError):
        request(db, owner="missing")
    with pytest.raises(A.AccountingError):
        attempt(db, outcome="failure")
    with pytest.raises(A.AccountingError):
        attempt(db, "a2", source="other")
    with pytest.raises(A.AccountingError):
        A.register_target(db, source_id="other", task_id="t1", target_id="body", at=T)


def test_terminal_transition_and_pre_registration_event_are_rejected(db):
    task(db)
    A.set_task_state(db, source_id="alpaca", task_id="t1", event_id="done",
                     state="succeeded", at=T)
    with pytest.raises(A.AccountingError):
        A.set_task_state(db, source_id="alpaca", task_id="t1", event_id="late",
                         state="failed", at=T + timedelta(seconds=1))
    with pytest.raises(A.AccountingError):
        request(db, "tooearly", at=T - timedelta(seconds=1))


def test_missing_instrumentation_is_unavailable_not_success(db):
    r = report(db)
    assert r["available"] is False
    assert r["tasks"]["rate"] is None
    task(db)
    r = report(db)
    assert r["requests"]["available"] is False
    assert r["requests"]["rate"] is None


def test_failed_multirow_target_registration_leaves_no_partial_target(db):
    task(db)
    with pytest.raises(A.AccountingError):
        A.register_target(db, source_id="alpaca", task_id="missing", target_id="body", at=T)
    with pytest.raises(A.AccountingError):
        A.set_target_state(db, source_id="alpaca", target_id="body", event_id="orphan",
                           state="acquired", at=T)
    assert report(db)["targets"]["registered"] == 0


def test_helpers_preserve_callers_transaction_ownership(db):
    db.commit()
    db.execute("BEGIN")
    task(db)
    db.rollback()
    assert report(db)["available"] is False


def test_naive_times_and_invalid_reporting_bounds_fail(db):
    with pytest.raises(A.AccountingError):
        task(db, at=T.replace(tzinfo=None))
    with pytest.raises(A.AccountingError):
        A.report(db, start=T, end=T, as_of=T)


def test_an_accounting_write_holds_the_write_lock_from_its_first_read(tmp_path):
    """Deferred BEGIN made read-then-write fail instantly with 'database is locked' when another writer committed in
    between, whatever the busy timeout. The write lock must be taken first so the timeout can queue the writers."""
    path = tmp_path / "accounting.sqlite3"
    first = sqlite3.connect(path, timeout=0)
    first.execute("PRAGMA journal_mode=WAL")
    A.ensure_schema(first)
    first.commit()
    other = sqlite3.connect(path, timeout=0)
    with A._atomic(first):
        first.execute("SELECT count(*) FROM fa_tasks").fetchone()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")
    first.commit()
    other.execute("BEGIN IMMEDIATE")  # free again once the first writer is done
    other.rollback()

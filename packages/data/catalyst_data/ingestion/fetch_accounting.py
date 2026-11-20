"""Three independent fetch denominators, with explicit IDs and historical cutoffs.

No connections or files are opened here. Callers own the supplied connection and
commit/rollback. IDs must be opaque identifiers, never URLs or credentials. A
target identifies a required body version, not merely an article whose body may
change; callers must include version identity when registering revised content.
Task failures are final: queued retries leave the original task pending/retrying.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


class AccountingError(ValueError):
    """Conflicting identity, invalid chronology, or invalid accounting state."""


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS fa_tasks (task_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, registered_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS fa_requests (request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES fa_tasks(task_id), source_id TEXT NOT NULL, registered_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS fa_attempts (attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES fa_requests(request_id), source_id TEXT NOT NULL, outcome TEXT NOT NULL, occurred_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS fa_task_states (event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES fa_tasks(task_id), source_id TEXT NOT NULL, state TEXT NOT NULL, occurred_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS fa_targets (source_id TEXT NOT NULL, target_id TEXT NOT NULL, registered_at TEXT NOT NULL, PRIMARY KEY(source_id,target_id))",
    "CREATE TABLE IF NOT EXISTS fa_task_targets (task_id TEXT NOT NULL REFERENCES fa_tasks(task_id), source_id TEXT NOT NULL, target_id TEXT NOT NULL, registered_at TEXT NOT NULL, PRIMARY KEY(task_id,source_id,target_id), FOREIGN KEY(source_id,target_id) REFERENCES fa_targets(source_id,target_id))",
    "CREATE TABLE IF NOT EXISTS fa_target_states (event_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, target_id TEXT NOT NULL, state TEXT NOT NULL, occurred_at TEXT NOT NULL, FOREIGN KEY(source_id,target_id) REFERENCES fa_targets(source_id,target_id))",
    "CREATE INDEX IF NOT EXISTS fa_tasks_cohort ON fa_tasks(source_id,registered_at)",
    "CREATE INDEX IF NOT EXISTS fa_requests_task ON fa_requests(task_id,registered_at)",
    "CREATE INDEX IF NOT EXISTS fa_attempts_request ON fa_attempts(request_id,occurred_at)",
    "CREATE INDEX IF NOT EXISTS fa_task_states_owner ON fa_task_states(task_id,occurred_at)",
    "CREATE INDEX IF NOT EXISTS fa_target_states_owner ON fa_target_states(source_id,target_id,occurred_at)",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Add isolated tables; never use executescript (which commits caller work)."""
    with _atomic(conn):
        for statement in _SCHEMA:
            conn.execute(statement)


@contextmanager
def _atomic(conn):
    # A lone SAVEPOINT would commit on RELEASE. Start an outer transaction so
    # successful helpers always remain subject to the caller's commit/rollback.
    # IMMEDIATE takes the write lock up front, so the connection's busy timeout applies. A deferred BEGIN that reads
    # first and writes later fails at once with "database is locked" when another writer commits in between (WAL).
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("SAVEPOINT fa_operation")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK TO fa_operation")
        conn.execute("RELEASE fa_operation")
        raise
    else:
        conn.execute("RELEASE fa_operation")


def _stamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AccountingError("timestamps must be timezone-aware datetimes")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _ident(*values):
    if any(not isinstance(v, str) or not v or len(v) > 512 or any(ord(c) < 32 for c in v)
           or "://" in v for v in values):
        raise AccountingError("identifiers must be nonempty opaque strings")


def _append(conn, table, columns, values, key_columns):
    """Exact replay is harmless; reusing an ID with different facts is an error."""
    where = " AND ".join(f"{c}=?" for c in key_columns)
    keys = [values[columns.index(c)] for c in key_columns]
    existing = conn.execute(f"SELECT {','.join(columns)} FROM {table} WHERE {where}", keys).fetchone()
    if existing is not None:
        if tuple(existing) != tuple(values):
            raise AccountingError("identity conflicts with an existing accounting record")
        return False
    conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)
    return True


def _task(conn, source, task, at):
    row = conn.execute("SELECT source_id,registered_at FROM fa_tasks WHERE task_id=?", (task,)).fetchone()
    if row is None or row[0] != source:
        raise AccountingError("unknown task or source mismatch")
    if at < row[1]:
        raise AccountingError("event precedes task registration")


def register_task(conn, *, source_id, task_id, at):
    _ident(source_id, task_id)
    with _atomic(conn):
        _append(conn, "fa_tasks", ("task_id", "source_id", "registered_at"),
                (task_id, source_id, _stamp(at)), ("task_id",))


def register_request(conn, *, source_id, task_id, request_id, at):
    _ident(source_id, task_id, request_id)
    stamp = _stamp(at)
    with _atomic(conn):
        _task(conn, source_id, task_id, stamp)
        _append(conn, "fa_requests", ("request_id", "task_id", "source_id", "registered_at"),
                (request_id, task_id, source_id, stamp), ("request_id",))


def record_attempt(conn, *, source_id, request_id, attempt_id, outcome, at):
    """Record each physical attempt; breaker skips are events, never HTTP attempts."""
    _ident(source_id, request_id, attempt_id)
    if outcome not in {"success", "failure", "skipped_breaker"}:
        raise AccountingError("invalid attempt outcome")
    stamp = _stamp(at)
    with _atomic(conn):
        row = conn.execute("SELECT source_id,registered_at FROM fa_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None or row[0] != source_id:
            raise AccountingError("unknown request or source mismatch")
        if stamp < row[1]:
            raise AccountingError("attempt precedes request registration")
        _append(conn, "fa_attempts", ("attempt_id", "request_id", "source_id", "outcome", "occurred_at"),
                (attempt_id, request_id, source_id, outcome, stamp), ("attempt_id",))


def _state(conn, *, table, columns, values, owner_where, owner_args, terminal):
    event = conn.execute(f"SELECT {','.join(columns)} FROM {table} WHERE event_id=?", (values[0],)).fetchone()
    if event is not None:
        if tuple(event) != tuple(values):
            raise AccountingError("event identity conflicts with an existing record")
        return
    previous = conn.execute(f"SELECT state,occurred_at FROM {table} WHERE {owner_where} ORDER BY occurred_at DESC,rowid DESC LIMIT 1", owner_args).fetchone()
    if previous and (previous[0] in terminal or values[-1] < previous[1]):
        raise AccountingError("terminal transition or out-of-order state event")
    _append(conn, table, columns, values, ("event_id",))


def set_task_state(conn, *, source_id, task_id, event_id, state, at):
    _ident(source_id, task_id, event_id)
    if state not in {"pending", "retrying", "succeeded", "failed"}:
        raise AccountingError("invalid task state")
    stamp = _stamp(at)
    with _atomic(conn):
        _task(conn, source_id, task_id, stamp)
        _state(conn, table="fa_task_states", columns=("event_id", "task_id", "source_id", "state", "occurred_at"),
               values=(event_id, task_id, source_id, state, stamp), owner_where="task_id=?",
               owner_args=(task_id,), terminal={"succeeded", "failed"})


def register_target(conn, *, source_id, task_id, target_id, at):
    """Register an expected body before acquisition, even if acquisition will fail."""
    _ident(source_id, task_id, target_id)
    stamp = _stamp(at)
    with _atomic(conn):
        _task(conn, source_id, task_id, stamp)
        existing = conn.execute("SELECT registered_at FROM fa_targets WHERE source_id=? AND target_id=?", (source_id, target_id)).fetchone()
        if existing is None:
            _append(conn, "fa_targets", ("source_id", "target_id", "registered_at"),
                    (source_id, target_id, stamp), ("source_id", "target_id"))
        elif stamp < existing[0]:
            raise AccountingError("target association predates its first registration")
        _append(conn, "fa_task_targets", ("task_id", "source_id", "target_id", "registered_at"),
                (task_id, source_id, target_id, stamp), ("task_id", "source_id", "target_id"))


def set_target_state(conn, *, source_id, target_id, event_id, state, at):
    _ident(source_id, target_id, event_id)
    if state not in {"pending", "retrying", "acquired", "unavailable"}:
        raise AccountingError("invalid target state")
    stamp = _stamp(at)
    with _atomic(conn):
        row = conn.execute("SELECT registered_at FROM fa_targets WHERE source_id=? AND target_id=?", (source_id, target_id)).fetchone()
        if row is None or stamp < row[0]:
            raise AccountingError("unknown target or event precedes registration")
        _state(conn, table="fa_target_states", columns=("event_id", "source_id", "target_id", "state", "occurred_at"),
               values=(event_id, source_id, target_id, state, stamp), owner_where="source_id=? AND target_id=?",
               owner_args=(source_id, target_id), terminal={"acquired"})


def _summary(states, success, failure):
    count = len(states)
    good = states.count(success)
    bad = states.count(failure)
    terminal = good + bad
    return {"registered": count, success: good, failure: bad,
            "pending": states.count("pending"), "retrying": states.count("retrying"),
            "terminal": terminal, "rate": good / count if count else None,
            "terminal_rate": good / terminal if terminal else None}


def _latest(conn, table, where, args, cutoff):
    row = conn.execute(f"SELECT state FROM {table} WHERE {where} AND occurred_at<=? ORDER BY occurred_at DESC,rowid DESC LIMIT 1", (*args, cutoff)).fetchone()
    return row[0] if row else "pending"


def report(conn, *, start, end, as_of, expected_sources=()):
    """Registration cohorts [start,end), state/attempts through inclusive as_of.

    Request attempts belong to tasks registered in the cohort. Targets have
    their own first-registration cohort, independent of repeated task links.
    rate includes pending work in its denominator; terminal_rate excludes it.
    Missing instrumentation is exposed explicitly; zero denominators yield null.
    This module cannot detect calls which bypass instrumentation entirely.
    """
    begin, finish, cutoff = _stamp(start), _stamp(end), _stamp(as_of)
    if begin >= finish:
        raise AccountingError("report start must precede end")
    sources = set(expected_sources)
    for source in sources:
        _ident(source)
    sources.update(r[0] for r in conn.execute("SELECT DISTINCT source_id FROM fa_tasks WHERE registered_at>=? AND registered_at<? AND registered_at<=?", (begin, finish, cutoff)))
    result = {}
    for source in sorted(sources):
        tasks = [r[0] for r in conn.execute("SELECT task_id FROM fa_tasks WHERE source_id=? AND registered_at>=? AND registered_at<? AND registered_at<=?", (source, begin, finish, cutoff))]
        states, outcomes, request_count, missing = [], [], 0, 0
        for task in tasks:
            states.append(_latest(conn, "fa_task_states", "task_id=?", (task,), cutoff))
            requests = [r[0] for r in conn.execute("SELECT request_id FROM fa_requests WHERE task_id=? AND registered_at<=?", (task, cutoff))]
            request_count += len(requests)
            missing += not bool(requests)
            for request in requests:
                outcomes.extend(r[0] for r in conn.execute("SELECT outcome FROM fa_attempts WHERE request_id=? AND occurred_at<=?", (request, cutoff)))
        success, failure = outcomes.count("success"), outcomes.count("failure")
        attempts = success + failure
        targets = [r[0] for r in conn.execute("SELECT target_id FROM fa_targets WHERE source_id=? AND registered_at>=? AND registered_at<? AND registered_at<=?", (source, begin, finish, cutoff))]
        target_states = [_latest(conn, "fa_target_states", "source_id=? AND target_id=?", (source, target), cutoff) for target in targets]
        result[source] = {
            "available": bool(tasks),
            "issues": (["no_task_instrumentation"] if not tasks else []) + (["tasks_without_request_instrumentation"] if missing else []),
            "tasks": {"available": bool(tasks), **_summary(states, "succeeded", "failed")},
            "requests": {"available": bool(tasks) and not missing, "logical_requests": request_count,
                         "tasks_without_requests": missing, "attempts": attempts,
                         "successes": success, "failures": failure,
                         "skipped_breaker": outcomes.count("skipped_breaker"),
                         "rate": success / attempts if attempts and not missing else None},
            "targets": _summary(target_states, "acquired", "unavailable"),
        }
    return {"schema_version": 1, "start": begin, "end": finish, "as_of": cutoff,
            "cohort": "registration_time", "sources": result}

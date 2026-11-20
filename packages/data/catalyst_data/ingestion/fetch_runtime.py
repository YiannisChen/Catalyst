"""Task-scoped runtime bridge to fetch_accounting; no provider payloads are stored."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import uuid4

from catalyst_data.ingestion import fetch_accounting as A

_CURRENT = ContextVar("fetch_task", default=None)
_LEGACY_QUEUE = ContextVar("legacy_fetch_queue", default=False)
COVERAGE = {"complete", "partial", "unmeasured"}


def ensure_schema(conn):
    A.ensure_schema(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS fa_coverage (task_id TEXT NOT NULL, at TEXT NOT NULL, "
                 "request_coverage TEXT NOT NULL, discovery_complete INTEGER, PRIMARY KEY(task_id,at))")
    conn.execute("CREATE TABLE IF NOT EXISTS fa_queue_links (kind TEXT NOT NULL, queue_key TEXT NOT NULL, "
                 "first_failed_at TEXT NOT NULL, task_id TEXT NOT NULL, source_id TEXT NOT NULL, "
                 "PRIMARY KEY(kind,queue_key))")


def _write(work, function, **kwargs):
    conn = work.connect()
    if conn is None:
        raise RuntimeError("fetch accounting store unavailable")
    try:
        with conn:
            ensure_schema(conn)
            return function(conn, **kwargs)
    finally:
        conn.close()


@dataclass
class Work:
    source_id: str
    task_id: str
    connect: object
    clock: object
    coverage: str
    statuses: list[str] = field(default_factory=list)
    queued: bool = False
    finalized: bool = False
    discovery: bool | None = None
    expected: set[str] = field(default_factory=set)

    def finish(self, status):
        if status not in {"OK", "EMPTY", "ERROR", "DISABLED"}:
            raise ValueError("invalid logical task outcome")
        self.statuses.append(status)

    def _coverage(self):
        def save(conn):
            conn.execute("INSERT OR REPLACE INTO fa_coverage VALUES (?,?,?,?)",
                         (self.task_id, self.clock().isoformat(timespec="microseconds"), self.coverage, self.discovery))
        _write(self, save)

    def discovery_complete(self, complete):
        if not isinstance(complete, bool):
            raise ValueError("discovery completeness must be boolean")
        self.discovery = complete
        self._coverage()

    def expect_target(self, target_id):
        if target_id in self.expected:
            return
        def register(conn):
            at = self.clock()
            A.register_target(conn, source_id=self.source_id, task_id=self.task_id,
                              target_id=target_id, at=at)
            latest = conn.execute("SELECT state FROM fa_target_states WHERE source_id=? AND target_id=? "
                                  "ORDER BY occurred_at DESC,rowid DESC LIMIT 1", (self.source_id, target_id)).fetchone()
            if latest and latest[0] == "unavailable":
                A.set_target_state(conn, source_id=self.source_id, target_id=target_id,
                                   event_id=uuid4().hex, state="pending", at=at)
        _write(self, register)
        self.expected.add(target_id)

    def persist_final_state(self, conn):
        """Caller may commit queue resolution and accounting together."""
        queue_states = [r[0] for r in conn.execute(
            "SELECT q.status FROM queue q JOIN fa_queue_links l ON q.kind=l.kind AND q.key=l.queue_key "
            "AND q.first_failed_at=l.first_failed_at WHERE l.task_id=?", (self.task_id,))]
        if "PENDING" in queue_states:
            state = "retrying"
        elif "FAILED" in queue_states or "ERROR" in self.statuses or "DISABLED" in self.statuses:
            state = "failed"
        elif self.statuses:
            state = "succeeded"
        else:
            state = "pending"
        at = self.clock()
        A.set_task_state(conn, source_id=self.source_id, task_id=self.task_id,
                         event_id=uuid4().hex, state=state, at=at)
        for target in self.expected:
            latest = conn.execute("SELECT state FROM fa_target_states WHERE source_id=? AND target_id=? "
                                  "ORDER BY occurred_at DESC,rowid DESC LIMIT 1", (self.source_id, target)).fetchone()
            if latest and latest[0] == "acquired":
                continue
            owners = [r[0] for r in conn.execute(
                "SELECT COALESCE((SELECT state FROM fa_task_states WHERE task_id=l.task_id "
                "ORDER BY occurred_at DESC,rowid DESC LIMIT 1),'pending') FROM fa_task_targets l "
                "WHERE l.source_id=? AND l.target_id=?", (self.source_id, target))]
            target_state = "retrying" if "retrying" in owners else "pending" if "pending" in owners else "unavailable"
            A.set_target_state(conn, source_id=self.source_id, target_id=target,
                               event_id=uuid4().hex, state=target_state, at=at)

    def acquire_target(self, target_id):
        if target_id not in self.expected:
            raise ValueError("body target was not registered by this task")
        def acquire(conn):
            latest = conn.execute("SELECT state FROM fa_target_states WHERE source_id=? AND target_id=? "
                                  "ORDER BY occurred_at DESC,rowid DESC LIMIT 1", (self.source_id, target_id)).fetchone()
            if latest and latest[0] == "acquired":
                return
            A.set_target_state(conn, source_id=self.source_id, target_id=target_id,
                               event_id=uuid4().hex, state="acquired", at=self.clock())
        _write(self, acquire)


@contextmanager
def task(source_id, *, connect, clock, task_id=None, request_coverage="partial"):
    if request_coverage not in COVERAGE:
        raise ValueError("invalid request coverage")
    if _LEGACY_QUEUE.get():
        class LegacyWork:
            def finish(self, status):
                pass
            def expect_target(self, target):
                pass
            def acquire_target(self, target):
                pass
            def discovery_complete(self, complete):
                pass
        yield LegacyWork()
        return
    existing = _CURRENT.get()
    if existing is not None:
        if existing.source_id != source_id or (task_id and task_id != existing.task_id):
            raise ValueError("nested task source/identity mismatch")
        yield existing
        return
    work = Work(source_id, task_id or uuid4().hex, connect, clock, request_coverage)
    def register(conn):
        row = conn.execute("SELECT source_id FROM fa_tasks WHERE task_id=?", (work.task_id,)).fetchone()
        if row is None:
            A.register_task(conn, source_id=source_id, task_id=work.task_id, at=clock())
        elif row[0] != source_id:
            raise ValueError("restored task source mismatch")
        else:
            latest = conn.execute("SELECT state FROM fa_task_states WHERE task_id=? "
                                  "ORDER BY occurred_at DESC,rowid DESC LIMIT 1", (work.task_id,)).fetchone()
            if latest and latest[0] in {"succeeded", "failed"}:
                raise ValueError("cannot resume a terminal task")
            work.expected.update(r[0] for r in conn.execute(
                "SELECT target_id FROM fa_task_targets WHERE task_id=?", (work.task_id,)))
    _write(work, register)
    work._coverage()
    token = _CURRENT.set(work)
    try:
        yield work
    except Exception:
        work.finish("ERROR")
        work.discovery_complete(False)
        raise
    finally:
        _CURRENT.reset(token)
        if not work.finalized:
            _write(work, work.persist_final_state)


def begin_request():
    work = _CURRENT.get()
    if work is None:
        return None
    request_id = uuid4().hex
    _write(work, A.register_request, source_id=work.source_id, task_id=work.task_id,
           request_id=request_id, at=work.clock())
    return work, request_id


def record_request(handle, outcome):
    if handle is None:
        return
    work, request_id = handle
    _write(work, A.record_attempt, source_id=work.source_id, request_id=request_id,
           attempt_id=uuid4().hex, outcome=outcome, at=work.clock())


def link_queue(conn, kind, key, first_failed_at, *, is_new):
    work = _CURRENT.get()
    if work is None:
        return
    ensure_schema(conn)
    if is_new:
        conn.execute("INSERT INTO fa_queue_links VALUES (?,?,?,?,?)",
                     (kind, key, first_failed_at, work.task_id, work.source_id))
        work.queued = True
    else:
        row = conn.execute("SELECT task_id FROM fa_queue_links WHERE kind=? AND queue_key=? AND first_failed_at=?",
                           (kind, key, first_failed_at)).fetchone()
        work.queued = bool(row and row[0] == work.task_id)


def queue_owner(conn, kind, key, first_failed_at):
    ensure_schema(conn)
    return conn.execute("SELECT task_id,source_id FROM fa_queue_links WHERE kind=? AND queue_key=? AND first_failed_at=?",
                        (kind, key, first_failed_at)).fetchone()


@contextmanager
def legacy_queue():
    token = _LEGACY_QUEUE.set(True)
    try:
        yield None
    finally:
        _LEGACY_QUEUE.reset(token)


def report(conn, *, start, end, as_of, expected_sources=()):
    ensure_schema(conn)
    result = A.report(conn, start=start, end=end, as_of=as_of, expected_sources=expected_sources)
    for source, item in result["sources"].items():
        rows = conn.execute("SELECT c.request_coverage,c.discovery_complete FROM fa_coverage c JOIN fa_tasks t ON t.task_id=c.task_id "
                            "WHERE t.source_id=? AND t.registered_at>=? AND t.registered_at<? AND t.registered_at<=? "
                            "AND c.at<=?",
                            (source, result["start"], result["end"], result["as_of"], result["as_of"])).fetchall()
        coverages = {r[0] for r in rows}
        coverage = "complete" if coverages == {"complete"} else "partial" if coverages - {"unmeasured"} else "unmeasured"
        item["requests"]["coverage"] = coverage
        if coverage != "complete":
            item["requests"]["available"] = False
            item["requests"]["observed_attempt_rate"] = item["requests"]["rate"]
            item["requests"]["rate"] = None
            item["issues"].append("transport_not_fully_instrumented")
        item["targets"]["discovery_incomplete_tasks"] = conn.execute("SELECT COUNT(*) FROM fa_tasks t JOIN fa_coverage c ON t.task_id=c.task_id "
            "WHERE t.source_id=? AND t.registered_at>=? AND t.registered_at<? AND t.registered_at<=? "
            "AND c.at=(SELECT MAX(at) FROM fa_coverage WHERE task_id=t.task_id AND at<=?) "
            "AND c.discovery_complete=0", (source, result["start"], result["end"], result["as_of"], result["as_of"])).fetchone()[0]
        item["targets"]["denominator_scope"] = "discovered_expected_body_versions_only"
    return result

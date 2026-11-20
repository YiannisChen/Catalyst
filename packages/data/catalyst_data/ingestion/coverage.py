"""Append-only, role-neutral facts about source collection coverage.

Call ``ensure_schema`` before use. The supplied SQLite connection and its
transaction belong to the caller; helpers never commit. Attempts partition work
by source, product, subject, safe query, policy version, and requested window.
Coverage events are immutable snapshots within an attempt. This module records
collection evidence only; it does not decide whether a requested role is covered.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from catalyst_data.dedup.links import normalize_identity_url
from catalyst_data.ingestion.time import timefields


class CoverageError(ValueError):
    """Invalid coverage evidence, chronology, or conflicting identity."""


@dataclass(frozen=True)
class Attempt:
    logical_task_id: str
    attempt_id: str
    partition_id: str
    attempt_seq: int
    source_id: str
    product: str
    subject_kind: str
    subject_id: str
    query_spec_hash: str
    query_spec_json: str
    coverage_policy_version: str
    requested_from: str
    requested_until: str


@dataclass(frozen=True)
class CoverageFact:
    coverage_id: str
    logical_task_id: str
    attempt_id: str
    partition_id: str
    attempt_seq: int
    state_seq: int
    source_id: str
    product: str
    subject_kind: str
    subject_id: str
    query_spec_hash: str
    query_spec_json: str
    coverage_policy_version: str
    requested_from: str
    requested_until: str
    coverage_from: str | None
    coverage_until: str | None
    enumeration_complete: bool | None
    cursor: str | None
    expected_target_count: int | None
    acquired_target_count: int | None
    status: str
    reason: str | None
    recorded_at: str


@dataclass(frozen=True)
class CoverageTarget:
    identity: str
    acquired: bool | None


@dataclass(frozen=True)
class CoverageSnapshot:
    attempt: Attempt
    fact: CoverageFact | None
    targets: tuple["CoverageTarget", ...] = ()


_ATTEMPT_COLUMNS = (
    "logical_task_id", "attempt_id", "partition_id", "attempt_seq", "source_id",
    "product", "subject_kind", "subject_id", "query_spec_hash", "query_spec_json",
    "coverage_policy_version", "requested_from", "requested_until",
)
_FACT_COLUMNS = (
    "coverage_id", "logical_task_id", "attempt_id", "partition_id", "attempt_seq", "state_seq",
    "source_id", "product", "subject_kind", "subject_id", "query_spec_hash", "query_spec_json",
    "coverage_policy_version", "requested_from", "requested_until", "coverage_from", "coverage_until",
    "enumeration_complete", "cursor", "expected_target_count", "acquired_target_count",
    "status", "reason", "recorded_at",
)

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS source_coverage_attempts (
        logical_task_id TEXT NOT NULL, attempt_id TEXT PRIMARY KEY, partition_id TEXT NOT NULL,
        attempt_seq INTEGER NOT NULL CHECK (attempt_seq > 0), source_id TEXT NOT NULL,
        product TEXT NOT NULL CHECK (product IN ('headlines','full_text','bars','search')),
        subject_kind TEXT NOT NULL CHECK (subject_kind IN ('ticker','instrument','feed','query')),
        subject_id TEXT NOT NULL, query_spec_hash TEXT NOT NULL, query_spec_json TEXT NOT NULL,
        coverage_policy_version TEXT NOT NULL, requested_from TEXT NOT NULL, requested_until TEXT NOT NULL,
        UNIQUE (partition_id, attempt_seq)
    )""",
    "CREATE INDEX IF NOT EXISTS source_coverage_attempts_partition ON source_coverage_attempts(partition_id, attempt_seq)",
    """CREATE TABLE IF NOT EXISTS source_coverage_facts (
        coverage_id TEXT PRIMARY KEY, logical_task_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL REFERENCES source_coverage_attempts(attempt_id), partition_id TEXT NOT NULL,
        attempt_seq INTEGER NOT NULL CHECK (attempt_seq > 0), state_seq INTEGER NOT NULL CHECK (state_seq > 0),
        source_id TEXT NOT NULL, product TEXT NOT NULL CHECK (product IN ('headlines','full_text','bars','search')),
        subject_kind TEXT NOT NULL CHECK (subject_kind IN ('ticker','instrument','feed','query')),
        subject_id TEXT NOT NULL, query_spec_hash TEXT NOT NULL, query_spec_json TEXT NOT NULL,
        coverage_policy_version TEXT NOT NULL, requested_from TEXT NOT NULL, requested_until TEXT NOT NULL,
        coverage_from TEXT, coverage_until TEXT,
        enumeration_complete INTEGER CHECK (enumeration_complete IN (0,1) OR enumeration_complete IS NULL),
        cursor TEXT, expected_target_count INTEGER CHECK (expected_target_count >= 0 OR expected_target_count IS NULL),
        acquired_target_count INTEGER CHECK (acquired_target_count >= 0 OR acquired_target_count IS NULL),
        status TEXT NOT NULL CHECK (status IN ('OK','EMPTY','ERROR','NOT_FETCHED')),
        reason TEXT CHECK (reason IN ('ERROR','DISABLED','deferred','breaker') OR reason IS NULL),
        recorded_at TEXT NOT NULL, UNIQUE (attempt_id, state_seq),
        CHECK (acquired_target_count IS NULL OR expected_target_count IS NULL OR acquired_target_count <= expected_target_count)
    )""",
    "CREATE INDEX IF NOT EXISTS source_coverage_facts_latest ON source_coverage_facts(attempt_id, state_seq)",
    """CREATE TABLE IF NOT EXISTS source_coverage_targets (
        coverage_id TEXT NOT NULL REFERENCES source_coverage_facts(coverage_id),
        identity TEXT NOT NULL, acquired INTEGER CHECK (acquired IN (0,1) OR acquired IS NULL),
        PRIMARY KEY (coverage_id, identity)
    )""",
    """CREATE TRIGGER IF NOT EXISTS source_coverage_attempts_no_update BEFORE UPDATE ON source_coverage_attempts
       BEGIN SELECT RAISE(ABORT, 'coverage attempts are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS source_coverage_attempts_no_delete BEFORE DELETE ON source_coverage_attempts
       BEGIN SELECT RAISE(ABORT, 'coverage attempts are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS source_coverage_attempts_no_replace BEFORE INSERT ON source_coverage_attempts
       WHEN EXISTS (SELECT 1 FROM source_coverage_attempts WHERE attempt_id=NEW.attempt_id)
       BEGIN SELECT RAISE(ABORT, 'existing coverage attempts cannot be replaced'); END""",
    """CREATE TRIGGER IF NOT EXISTS source_coverage_facts_no_update BEFORE UPDATE ON source_coverage_facts
       BEGIN SELECT RAISE(ABORT, 'coverage facts are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS source_coverage_facts_no_delete BEFORE DELETE ON source_coverage_facts
       BEGIN SELECT RAISE(ABORT, 'coverage facts are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS source_coverage_facts_no_replace BEFORE INSERT ON source_coverage_facts
       WHEN EXISTS (SELECT 1 FROM source_coverage_facts WHERE coverage_id=NEW.coverage_id)
       BEGIN SELECT RAISE(ABORT, 'existing coverage facts cannot be replaced'); END""",
    """CREATE TRIGGER IF NOT EXISTS source_coverage_targets_no_update BEFORE UPDATE ON source_coverage_targets
       BEGIN SELECT RAISE(ABORT, 'coverage targets are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS source_coverage_targets_no_delete BEFORE DELETE ON source_coverage_targets
       BEGIN SELECT RAISE(ABORT, 'coverage targets are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS source_coverage_targets_no_replace BEFORE INSERT ON source_coverage_targets
       WHEN EXISTS (SELECT 1 FROM source_coverage_targets WHERE coverage_id=NEW.coverage_id AND identity=NEW.identity)
       BEGIN SELECT RAISE(ABORT, 'existing coverage targets cannot be replaced'); END""",
)


@contextmanager
def _atomic(conn: sqlite3.Connection):
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT source_coverage_operation")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK TO source_coverage_operation")
        conn.execute("RELEASE source_coverage_operation")
        raise
    else:
        conn.execute("RELEASE source_coverage_operation")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create additive schema and append-only guards without committing caller work."""
    with _atomic(conn):
        for statement in _SCHEMA:
            conn.execute(statement)


def _identity(value: Any, field: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip() or len(value) > 512
            or any(ord(char) < 32 for char in value) or "://" in value):
        raise CoverageError(f"{field} must be a nonempty opaque identifier")
    return value


def _timestamp(value: Any, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        raise CoverageError(f"{field} must be an ISO timestamp string")
    try:
        return timefields.stored(value).isoformat(timespec="microseconds")
    except (TypeError, ValueError, OverflowError) as exc:
        raise CoverageError(f"{field} must be a timezone-aware ISO timestamp") from exc


def _safe_query_spec(value: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        raise CoverageError("query_spec must be a mapping of equality filters")
    safe: dict[str, Any] = {}
    for key, item in value.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", key)
                or re.search(r"password|passwd|secret|token|api[_-]?key|authorization|cookie", key, re.I)):
            raise CoverageError("query_spec contains an unsafe filter name")
        values = item if isinstance(item, (list, tuple)) else (item,)
        if any(not (entry is None or isinstance(entry, (str, int, float, bool))) for entry in values):
            raise CoverageError("query_spec values must be safe JSON primitives or lists of primitives")
        if any(isinstance(entry, float) and (entry != entry or abs(entry) == float("inf")) for entry in values):
            raise CoverageError("query_spec numbers must be finite")
        if any(isinstance(entry, str) and re.search(r"(?i)bearer\s+|(?:api[_-]?key|password|passwd|secret|token)\s*=|authorization\s*:", entry) for entry in values):
            raise CoverageError("query_spec must not contain credential material")
        safe[key] = list(item) if isinstance(item, tuple) else item
    encoded = json.dumps(safe, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return digest, encoded


def _attempt_from_row(row) -> Attempt:
    return Attempt(*row)


def _fact_from_row(row) -> CoverageFact:
    values = list(row)
    if values[17] is not None:
        values[17] = bool(values[17])
    return CoverageFact(*values)


def start_attempt(conn: sqlite3.Connection, *, logical_task_id: str, attempt_id: str,
                  source_id: str, product: str, subject_kind: str, subject_id: str,
                  query_spec: Mapping[str, Any], coverage_policy_version: str,
                  requested_from: str, requested_until: str) -> Attempt:
    """Append an attempt and allocate its sequence within the stable partition."""
    logical_task_id = _identity(logical_task_id, "logical_task_id")
    attempt_id = _identity(attempt_id, "attempt_id")
    source_id = _identity(source_id, "source_id")
    subject_id = _identity(subject_id, "subject_id")
    coverage_policy_version = _identity(coverage_policy_version, "coverage_policy_version")
    if not isinstance(product, str) or product not in {"headlines", "full_text", "bars", "search"}:
        raise CoverageError("invalid product")
    if not isinstance(subject_kind, str) or subject_kind not in {"ticker", "instrument", "feed", "query"}:
        raise CoverageError("invalid subject_kind")
    begin, end = _timestamp(requested_from, "requested_from"), _timestamp(requested_until, "requested_until")
    if begin >= end:
        raise CoverageError("requested_from must precede requested_until")
    query_hash, query_json = _safe_query_spec(query_spec)
    scope = (source_id, product, subject_kind, subject_id, query_hash, query_json,
             coverage_policy_version, begin, end)
    partition_id = hashlib.sha256(json.dumps(scope, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    with _atomic(conn):
        existing = conn.execute(f"SELECT {','.join(_ATTEMPT_COLUMNS)} FROM source_coverage_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if existing:
            row = _attempt_from_row(existing)
            existing_scope = (row.source_id, row.product, row.subject_kind, row.subject_id,
                              row.query_spec_hash, row.query_spec_json, row.coverage_policy_version,
                              row.requested_from, row.requested_until)
            if row.logical_task_id != logical_task_id or row.partition_id != partition_id or existing_scope != scope:
                raise CoverageError("attempt identity conflicts with an existing record")
            return row
        previous = conn.execute("SELECT MAX(attempt_seq) FROM source_coverage_attempts WHERE partition_id=?", (partition_id,)).fetchone()[0]
        values = (logical_task_id, attempt_id, partition_id, (previous or 0) + 1, *scope)
        conn.execute(f"INSERT INTO source_coverage_attempts ({','.join(_ATTEMPT_COLUMNS)}) VALUES ({','.join('?' for _ in values)})", values)
    return Attempt(*values)


def _target_identity(source_id: str, raw: Any) -> str:
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise CoverageError("target identities must be nonempty strings")
    if raw.lower().startswith(("http://", "https://")):
        normalized = normalize_identity_url(raw)
        if normalized is None:
            raise CoverageError("target URL cannot be normalized")
        return normalized
    return f"provider:{source_id}:{raw}"


def _target_sets(source_id: str, expected: Sequence[str] | None,
                 acquired: Sequence[str] | None) -> tuple[dict[str, bool | None], int | None, int | None]:
    for values in (expected, acquired):
        if values is not None and isinstance(values, (str, bytes)):
            raise CoverageError("target identities must be supplied as sequences")
    expected_ids = {_target_identity(source_id, value) for value in expected} if expected is not None else set()
    acquired_ids = {_target_identity(source_id, value) for value in acquired} if acquired is not None else set()
    if acquired is not None and expected is not None and not acquired_ids <= expected_ids:
        raise CoverageError("every acquired target must also be expected")
    if acquired is not None and expected is None:
        raise CoverageError("expected targets are required when acquired targets are known")
    memberships = {identity: (None if acquired is None else identity in acquired_ids)
                   for identity in expected_ids}
    return (memberships, len(expected_ids) if expected is not None else None,
            len(acquired_ids) if acquired is not None else None)


def _validate_state(status: str, reason: str | None) -> None:
    if not isinstance(status, str) or status not in {"OK", "EMPTY", "ERROR", "NOT_FETCHED"}:
        raise CoverageError("invalid coverage status")
    allowed = {"OK": {None}, "EMPTY": {None}, "ERROR": {"ERROR"},
               "NOT_FETCHED": {"DISABLED", "deferred", "breaker"}}
    if reason is not None and not isinstance(reason, str):
        raise CoverageError("invalid coverage reason")
    if reason not in allowed[status]:
        raise CoverageError("reason does not match coverage status")


def _fact_values(attempt: Attempt, state_seq: int, coverage_id: str, coverage_begin: str | None,
                 coverage_end: str | None, enumeration_complete: bool | None, cursor: str | None,
                 expected_count: int | None, acquired_count: int | None, status: str,
                 reason: str | None, recorded: str) -> tuple:
    return (coverage_id, attempt.logical_task_id, attempt.attempt_id, attempt.partition_id,
            attempt.attempt_seq, state_seq, attempt.source_id, attempt.product, attempt.subject_kind,
            attempt.subject_id, attempt.query_spec_hash, attempt.query_spec_json,
            attempt.coverage_policy_version, attempt.requested_from, attempt.requested_until,
            coverage_begin, coverage_end, None if enumeration_complete is None else int(enumeration_complete),
            cursor, expected_count, acquired_count, status, reason, recorded)


def record_coverage(conn: sqlite3.Connection, *, coverage_id: str, attempt_id: str, status: str,
                     recorded_at: str, reason: str | None = None, coverage_from: str | None = None,
                     coverage_until: str | None = None, enumeration_complete: bool | None = None,
                     cursor: str | None = None, expected_target_count: int | None = None,
                     acquired_target_count: int | None = None, expected_targets: Sequence[str] | None = None,
                     acquired_targets: Sequence[str] | None = None) -> CoverageFact:
    """Append an immutable state snapshot; full-text counts derive from identities."""
    coverage_id = _identity(coverage_id, "coverage_id")
    attempt_id = _identity(attempt_id, "attempt_id")
    _validate_state(status, reason)
    recorded = _timestamp(recorded_at, "recorded_at")
    coverage_begin = _timestamp(coverage_from, "coverage_from", optional=True)
    coverage_end = _timestamp(coverage_until, "coverage_until", optional=True)
    if (coverage_begin is None) != (coverage_end is None):
        raise CoverageError("coverage_from and coverage_until must be supplied together")
    if coverage_begin is not None and coverage_end is not None and coverage_begin > coverage_end:
        raise CoverageError("coverage_from must not follow coverage_until")
    if enumeration_complete is not None and not isinstance(enumeration_complete, bool):
        raise CoverageError("enumeration_complete must be boolean or null")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096 or any(ord(c) < 32 for c in cursor)
            or re.search(r"(?i)bearer\s+|(?:api[_-]?key|password|passwd|secret|token)\s*=|authorization\s*:", cursor)):
        raise CoverageError("cursor must be a safe string or null")
    for value, field in ((expected_target_count, "expected_target_count"), (acquired_target_count, "acquired_target_count")):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise CoverageError(f"{field} must be a nonnegative integer or null")
    with _atomic(conn):
        attempt_row = conn.execute(f"SELECT {','.join(_ATTEMPT_COLUMNS)} FROM source_coverage_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if attempt_row is None:
            raise CoverageError("unknown attempt")
        attempt = _attempt_from_row(attempt_row)
        if coverage_begin is not None and (coverage_begin < attempt.requested_from or coverage_end > attempt.requested_until):
            raise CoverageError("coverage bounds must lie within the requested window")
        memberships: dict[str, bool | None] = {}
        if attempt.product == "full_text":
            if expected_target_count is not None and expected_targets is None:
                raise CoverageError("full_text expected counts require explicit target identities")
            if acquired_target_count is not None and acquired_targets is None:
                raise CoverageError("full_text acquired counts require explicit target identities")
            memberships, expected_count, acquired_count = _target_sets(attempt.source_id, expected_targets, acquired_targets)
            if expected_count is not None and expected_target_count not in (None, expected_count):
                raise CoverageError("full_text expected count must match deduplicated target identities")
            if acquired_count is not None and acquired_target_count not in (None, acquired_count):
                raise CoverageError("full_text acquired count must match deduplicated target identities")
            expected_target_count = expected_count if expected_count is not None else expected_target_count
            acquired_target_count = acquired_count if acquired_count is not None else acquired_target_count
        elif expected_targets is not None or acquired_targets is not None:
            raise CoverageError("explicit target identities are reserved for full_text coverage")
        if expected_target_count is not None and acquired_target_count is not None and acquired_target_count > expected_target_count:
            raise CoverageError("acquired target count exceeds expected target count")
        existing = conn.execute(f"SELECT {','.join(_FACT_COLUMNS)} FROM source_coverage_facts WHERE coverage_id=?", (coverage_id,)).fetchone()
        if existing:
            fact = _fact_from_row(existing)
            expected_facts = _fact_values(attempt, fact.state_seq, coverage_id, coverage_begin, coverage_end,
                                          enumeration_complete, cursor, expected_target_count,
                                          acquired_target_count, status, reason, recorded)
            if tuple(existing) != expected_facts:
                raise CoverageError("coverage identity conflicts with an existing fact")
            old_targets = {row[0]: None if row[1] is None else bool(row[1]) for row in conn.execute(
                "SELECT identity,acquired FROM source_coverage_targets WHERE coverage_id=?", (coverage_id,))}
            if old_targets != memberships:
                raise CoverageError("coverage identity conflicts with existing target identities")
            return fact
        previous = conn.execute("SELECT state_seq,recorded_at FROM source_coverage_facts WHERE attempt_id=? ORDER BY state_seq DESC LIMIT 1", (attempt_id,)).fetchone()
        state_seq = (previous[0] if previous else 0) + 1
        if previous and recorded < previous[1]:
            raise CoverageError("recorded_at precedes prior state sequence")
        values = _fact_values(attempt, state_seq, coverage_id, coverage_begin, coverage_end,
                              enumeration_complete, cursor, expected_target_count,
                              acquired_target_count, status, reason, recorded)
        conn.execute(f"INSERT INTO source_coverage_facts ({','.join(_FACT_COLUMNS)}) VALUES ({','.join('?' for _ in values)})", values)
        for identity, acquired in sorted(memberships.items()):
            conn.execute("INSERT INTO source_coverage_targets (coverage_id,identity,acquired) VALUES (?,?,?)",
                         (coverage_id, identity, None if acquired is None else int(acquired)))
    return _fact_from_row(values)


def list_attempts(conn: sqlite3.Connection) -> list[Attempt]:
    """Return all immutable attempts in insertion order."""
    rows = conn.execute(f"SELECT {','.join(_ATTEMPT_COLUMNS)} FROM source_coverage_attempts ORDER BY rowid")
    return [_attempt_from_row(row) for row in rows]


def list_facts(conn: sqlite3.Connection) -> list[CoverageFact]:
    """Return all immutable coverage facts in attempt/state order."""
    rows = conn.execute(f"SELECT {','.join(_FACT_COLUMNS)} FROM source_coverage_facts ORDER BY attempt_id,state_seq")
    return [_fact_from_row(row) for row in rows]


def list_targets(conn: sqlite3.Connection, *, coverage_id: str) -> list[CoverageTarget]:
    """Return the deduplicated expected full-text targets for one fact."""
    _identity(coverage_id, "coverage_id")
    rows = conn.execute("SELECT identity,acquired FROM source_coverage_targets WHERE coverage_id=? ORDER BY identity", (coverage_id,))
    return [CoverageTarget(identity=row[0], acquired=None if row[1] is None else bool(row[1])) for row in rows]


def latest_coverage(conn: sqlite3.Connection, *, logical_task_id: str | None = None) -> list[CoverageSnapshot]:
    """Return latest attempt per partition and its latest event, if available.

    Partition selection happens before optional task filtering. A newer attempt
    without an event remains visible as ``fact=None`` and cannot expose a stale
    successful event from an older retry.
    """
    if logical_task_id is not None:
        _identity(logical_task_id, "logical_task_id")
    rows = conn.execute(
        f"SELECT {','.join('a.' + column for column in _ATTEMPT_COLUMNS)} FROM source_coverage_attempts a "
        "WHERE a.attempt_seq=(SELECT MAX(b.attempt_seq) FROM source_coverage_attempts b WHERE b.partition_id=a.partition_id) "
        "ORDER BY a.partition_id"
    )
    snapshots = []
    for row in rows:
        attempt = _attempt_from_row(row)
        if logical_task_id is not None and attempt.logical_task_id != logical_task_id:
            continue
        fact_row = conn.execute(f"SELECT {','.join(_FACT_COLUMNS)} FROM source_coverage_facts WHERE attempt_id=? ORDER BY state_seq DESC LIMIT 1", (attempt.attempt_id,)).fetchone()
        fact = _fact_from_row(fact_row) if fact_row else None
        targets = tuple(list_targets(conn, coverage_id=fact.coverage_id)) if fact else ()
        snapshots.append(CoverageSnapshot(attempt, fact, targets))
    return snapshots

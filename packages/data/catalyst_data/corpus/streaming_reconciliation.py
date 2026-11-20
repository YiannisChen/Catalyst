"""Reconciliation phases for streaming corpus publication."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

from catalyst_data.corpus.streaming_publication import (
    ReconciliationSummary,
    ResumableResourceStop,
    _canonical_bytes,
    _table_columns,
    ensure_streaming_publication_schema,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReconciliationDeadlineStop(RuntimeError):
    """A reconciliation phase stopped because the progress deadline elapsed."""


_RECON_BASE_FIELDS = (
    "chunk_id",
    "document_id",
    "chunk_profile_version",
    "content_hash",
    "metadata_hash",
    "dedup_cluster_id",
)

# L6: phase SQL keeps the drafted IS NOT / != predicates verbatim, but reads the
# previous served snapshot from temp_previous_served instead of the served view.
_RECON_TO_EMBED_SQL = """
INSERT INTO corpus_build_deltas
    (build_id, delta_kind, chunk_id, document_id,
     previous_content_hash, previous_metadata_hash)
SELECT ?, 'to_embed', n.chunk_id, n.document_id,
       p.content_hash, p.metadata_hash
FROM corpus_build_chunks n
LEFT JOIN temp_previous_served p ON p.chunk_id=n.chunk_id
WHERE n.build_id=? AND n.eligibility='eligible'
  AND (p.chunk_id IS NULL OR p.content_hash != n.content_hash
       OR p.dedup_cluster_id IS NOT n.dedup_cluster_id)
"""

_RECON_CLUSTER_TOMBSTONE_SQL = """
INSERT INTO corpus_build_deltas
    (build_id, delta_kind, chunk_id, document_id, reason,
     previous_content_hash, previous_metadata_hash)
SELECT ?, 'tombstone', n.chunk_id, n.document_id,
       'dedup_cluster_reassigned', p.content_hash, p.metadata_hash
FROM corpus_build_chunks n
JOIN temp_previous_served p ON p.chunk_id=n.chunk_id
WHERE n.build_id=? AND n.eligibility='eligible'
  AND p.dedup_cluster_id IS NOT n.dedup_cluster_id
"""

_RECON_METADATA_UPDATE_SQL = """
INSERT INTO corpus_build_deltas
    (build_id, delta_kind, chunk_id, document_id,
     previous_content_hash, previous_metadata_hash)
SELECT ?, 'metadata_update', n.chunk_id, n.document_id,
       p.content_hash, p.metadata_hash
FROM corpus_build_chunks n
JOIN temp_previous_served p ON p.chunk_id=n.chunk_id
WHERE n.build_id=? AND n.eligibility='eligible'
  AND p.content_hash=n.content_hash
  AND p.metadata_hash != n.metadata_hash
  AND p.dedup_cluster_id IS n.dedup_cluster_id
"""

# Removed tombstones: read the bounded temp_tombstone_survivors (previous
# chunks absent from the new build) instead of anti-joining the full
# previous-served snapshot. Ordinary 1:1 LEFT JOINs only; replacement_chunk_id
# comes from the temp_profile_replacements lookup and is filled regardless of
# reason. No GROUP BY, no correlated EXISTS/MIN over the previous-served rows.
_RECON_REMOVED_TOMBSTONE_SQL = """
INSERT INTO corpus_build_deltas
    (build_id, delta_kind, chunk_id, document_id, reason,
     previous_content_hash, previous_metadata_hash, replacement_chunk_id)
SELECT ?, 'tombstone', p.chunk_id, p.document_id,
       CASE
         WHEN ineligible.document_id IS NOT NULL THEN 'eligibility_lost'
         WHEN profile_replacement.replacement_chunk_id IS NOT NULL
              THEN 'profile_version_replaced'
         WHEN survivor.document_id IS NOT NULL THEN 'disappeared_child'
         ELSE 'document_removed'
       END,
       p.content_hash, p.metadata_hash,
       profile_replacement.replacement_chunk_id
FROM temp_tombstone_survivors p
LEFT JOIN (
    SELECT DISTINCT document_id FROM corpus_build_source_documents
    WHERE build_id=? AND eligibility='ineligible'
) ineligible ON ineligible.document_id=p.document_id
LEFT JOIN temp_profile_replacements profile_replacement
  ON profile_replacement.document_id=p.document_id
 AND profile_replacement.chunk_profile_version=p.chunk_profile_version
LEFT JOIN (
    SELECT DISTINCT document_id FROM corpus_build_chunks WHERE build_id=?
) survivor ON survivor.document_id=p.document_id
"""

_RECON_STATUSES_PENDING_SQL = """
UPDATE corpus_build_chunks SET status='pending_embedding'
WHERE build_id=? AND chunk_id IN (
    SELECT chunk_id FROM corpus_build_deltas
    WHERE build_id=? AND delta_kind='to_embed')
"""

_RECON_STATUSES_METADATA_SQL = """
UPDATE corpus_build_chunks SET status='metadata_only'
WHERE build_id=? AND chunk_id IN (
    SELECT chunk_id FROM corpus_build_deltas
    WHERE build_id=? AND delta_kind='metadata_update')
"""

_RECON_FINALIZE_VALIDATIONS = (
    (
        "to_embed_and_metadata_in_target",
        """SELECT COUNT(*) FROM corpus_build_deltas d
           LEFT JOIN corpus_build_chunks n
             ON n.build_id=? AND n.chunk_id=d.chunk_id
           WHERE d.build_id=? AND d.delta_kind IN ('to_embed','metadata_update')
             AND n.chunk_id IS NULL""",
    ),
    (
        "dedup_cluster_reassigned_in_target",
        """SELECT COUNT(*) FROM corpus_build_deltas d
           LEFT JOIN corpus_build_chunks n
             ON n.build_id=? AND n.chunk_id=d.chunk_id
           WHERE d.build_id=? AND d.delta_kind='tombstone'
             AND d.reason='dedup_cluster_reassigned'
             AND n.chunk_id IS NULL""",
    ),
    (
        "removed_not_in_target",
        """SELECT COUNT(*) FROM corpus_build_deltas d
           JOIN corpus_build_chunks n
             ON n.build_id=? AND n.chunk_id=d.chunk_id
           WHERE d.build_id=? AND d.delta_kind='tombstone'
             AND d.reason IN ('eligibility_lost','profile_version_replaced',
                              'disappeared_child','document_removed')""",
    ),
    (
        "to_embed_pending",
        """SELECT COUNT(*) FROM corpus_build_deltas d
           LEFT JOIN corpus_build_chunks n
             ON n.build_id=? AND n.chunk_id=d.chunk_id
           WHERE d.build_id=? AND d.delta_kind='to_embed'
             AND n.status != 'pending_embedding'""",
    ),
    (
        "metadata_update_metadata_only",
        """SELECT COUNT(*) FROM corpus_build_deltas d
           LEFT JOIN corpus_build_chunks n
             ON n.build_id=? AND n.chunk_id=d.chunk_id
           WHERE d.build_id=? AND d.delta_kind='metadata_update'
             AND n.status != 'metadata_only'""",
    ),
)

# 1:1 lookup: GROUP BY MIN only over DISTINCT survivor profiles x
# temp_new_doc_profiles, never over the 295k-row previous-served table and
# never by re-joining the target chunks.
_RECON_PROFILE_REPLACEMENTS_SQL = """
CREATE TEMP TABLE temp_profile_replacements AS
SELECT p.document_id, p.chunk_profile_version,
       MIN(n.min_chunk_id COLLATE BINARY) AS replacement_chunk_id
FROM (SELECT DISTINCT document_id, chunk_profile_version
      FROM temp_tombstone_survivors) p
JOIN temp_new_doc_profiles n
  ON n.document_id = p.document_id
 AND n.chunk_profile_version != p.chunk_profile_version
GROUP BY p.document_id, p.chunk_profile_version
"""


def _recon_checkpoint_read(conn: sqlite3.Connection, build_id: str) -> str | None:
    columns = _table_columns(conn, "corpus_publication_builds")
    if "reconciliation_checkpoint" not in columns:
        return None
    row = conn.execute(
        "SELECT reconciliation_checkpoint FROM corpus_publication_builds "
        "WHERE build_id=?",
        (build_id,),
    ).fetchone()
    return None if row is None else row[0]


def _recon_flags(operator_interrupt: dict[str, bool] | None) -> dict[str, bool]:
    if operator_interrupt is None:
        return {"deadline": False, "operator_interrupt": False}
    if "operator_interrupt" not in operator_interrupt:
        operator_interrupt["operator_interrupt"] = False
    if "deadline" not in operator_interrupt:
        operator_interrupt["deadline"] = False
    return operator_interrupt


def _run_recon_transaction(
    conn: sqlite3.Connection,
    *,
    statements: Callable[[], None],
    checkpoint: str | None,
    build_id: str,
    now: str | None,
    deadline: float,
    flags: dict[str, bool],
) -> None:
    """L3 explicit rollback contract for reconciliation phases.

    Installs a progress handler that aborts on deadline or operator interrupt,
    BEGINs, runs statements, writes the durable checkpoint, and COMMITs. Any
    abort rolls back; the handler is cleared AFTER rollback. A bare
    OperationalError without an abort flag propagates unchanged.
    """
    started = time.monotonic()

    def handler() -> int:
        try:
            if time.monotonic() - started >= deadline:
                flags["deadline"] = True
                return 1
            if flags.get("operator_interrupt"):
                return 1
        except Exception:
            pass
        return 0

    conn.set_progress_handler(handler, 1000)
    try:
        try:
            conn.execute("BEGIN")
            statements()
            if checkpoint is not None:
                conn.execute(
                    """UPDATE corpus_publication_builds
                       SET reconciliation_checkpoint=?, updated_at=?
                       WHERE build_id=?""",
                    (checkpoint, now, build_id),
                )
            conn.commit()
        except BaseException as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if isinstance(exc, sqlite3.OperationalError) and flags.get("deadline"):
                raise ReconciliationDeadlineStop(
                    f"reconciliation deadline exceeded before checkpoint {checkpoint!r}"
                ) from exc
            if isinstance(exc, sqlite3.OperationalError) and flags.get("operator_interrupt"):
                raise ResumableResourceStop(
                    "operator_interrupt",
                    f"reconciliation interrupted before checkpoint {checkpoint!r}",
                ) from exc
            raise
    finally:
        conn.set_progress_handler(None, 0)


def _reconciliation_base(conn: sqlite3.Connection) -> tuple[str, str | None, str | None, int | None]:
    """Fail-closed resolver for the previous-served reconciliation base.

    Returns (kind, build_id, manifest_id, published_chunk_count). Exactly one
    current manifest is required. If exactly one published build owns that
    manifest it is the base, but only when no legacy corpus_chunks rows exist
    for the SAME current manifest (a published build AND legacy rows is
    ambiguous and fails closed). Otherwise the current manifest's legacy rows
    are the base. Never silently picks a winner with ORDER BY/LIMIT.
    """
    current_rows = conn.execute(
        "SELECT manifest_id FROM corpus_manifest WHERE is_current=1"
    ).fetchmany(2)
    if len(current_rows) != 1:
        raise ValueError(
            "reconciliation requires exactly one current corpus manifest"
        )
    current_manifest_id = str(current_rows[0][0])
    published_rows = conn.execute(
        """SELECT build_id, manifest_id, chunk_count
           FROM corpus_publication_builds
           WHERE status='published' AND manifest_id=?""",
        (current_manifest_id,),
    ).fetchmany(2)
    if len(published_rows) > 1:
        raise ValueError(
            "reconciliation base is ambiguous: multiple published builds "
            "own the current manifest"
        )
    if len(published_rows) == 1:
        legacy = conn.execute(
            "SELECT 1 FROM corpus_chunks WHERE manifest_id=? LIMIT 1",
            (current_manifest_id,),
        ).fetchone()
        if legacy is not None:
            raise ValueError(
                "ambiguous served source: published build AND legacy rows "
                "for the same current manifest"
            )
        return (
            "published_build",
            published_rows[0][0],
            published_rows[0][1],
            int(published_rows[0][2] or 0),
        )
    return "legacy", None, current_manifest_id, None


def _reconciliation_temp_digest(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    cursor = conn.execute(
        """SELECT chunk_id, document_id, chunk_profile_version, content_hash,
                  metadata_hash, dedup_cluster_id
           FROM temp_previous_served ORDER BY chunk_id COLLATE BINARY"""
    )
    while True:
        page = cursor.fetchmany(500)
        if not page:
            break
        for row in page:
            digest.update(_canonical_bytes(dict(zip(_RECON_BASE_FIELDS, row))))
            digest.update(b"\n")
    return digest.hexdigest()


def _materialize_reconciliation_temp(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    deadline: float,
    flags: dict[str, bool],
) -> dict[str, object]:
    """L5/L6: rebuild the TEMP snapshot outside phase transactions.

    The materialize runs its own explicit transaction with no checkpoint write;
    an abort rolls back so no half-populated TEMP remains and no checkpoint
    advances. Count and reconciliation-only digest are recomputed and fail
    closed against the bound base values on resume.
    """
    base_kind, base_build_id, base_manifest_id, published_chunk_count = (
        _reconciliation_base(conn)
    )

    def statements() -> None:
        conn.execute("DROP TABLE IF EXISTS temp_previous_served")
        conn.execute("DROP TABLE IF EXISTS temp_tombstone_survivors")
        conn.execute("DROP TABLE IF EXISTS temp_profile_replacements")
        conn.execute("DROP TABLE IF EXISTS temp_new_doc_profiles")
        conn.execute(
            """CREATE TEMP TABLE temp_previous_served (
                chunk_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                chunk_profile_version TEXT NOT NULL,
                content_hash TEXT,
                metadata_hash TEXT,
                dedup_cluster_id TEXT
            )"""
        )
        if base_kind == "published_build":
            conn.execute(
                """INSERT INTO temp_previous_served
                   SELECT chunk_id, document_id, chunk_profile_version,
                          content_hash, metadata_hash, dedup_cluster_id
                   FROM corpus_build_chunks WHERE build_id=?""",
                (base_build_id,),
            )
        elif base_manifest_id is not None:
            conn.execute(
                """INSERT INTO temp_previous_served
                   SELECT chunk_id, document_id, chunk_profile_version,
                          content_hash, metadata_hash, dedup_cluster_id
                   FROM corpus_chunks WHERE manifest_id=?""",
                (base_manifest_id,),
            )
        conn.execute(
            "CREATE INDEX idx_temp_previous_served_chunk ON temp_previous_served(chunk_id)"
        )
        conn.execute(
            "CREATE INDEX idx_temp_previous_served_document ON temp_previous_served(document_id)"
        )
        conn.execute(
            """CREATE TEMP TABLE temp_tombstone_survivors (
                chunk_id TEXT COLLATE BINARY PRIMARY KEY,
                document_id TEXT NOT NULL,
                chunk_profile_version TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                metadata_hash TEXT NOT NULL
            ) WITHOUT ROWID"""
        )
        conn.execute(
            """INSERT INTO temp_tombstone_survivors
               SELECT p.chunk_id, p.document_id, p.chunk_profile_version,
                      p.content_hash, p.metadata_hash
               FROM temp_previous_served p
               LEFT JOIN corpus_build_chunks n
                 ON n.build_id=? AND n.chunk_id=p.chunk_id
               WHERE n.chunk_id IS NULL""",
            (build_id,),
        )
        conn.execute(
            """CREATE INDEX idx_temp_tombstone_survivors_document
               ON temp_tombstone_survivors(document_id, chunk_profile_version)"""
        )
        conn.execute(
            """CREATE TEMP TABLE temp_new_doc_profiles AS
               SELECT document_id, chunk_profile_version,
                      MIN(chunk_id COLLATE BINARY) AS min_chunk_id
               FROM corpus_build_chunks WHERE build_id=?
               GROUP BY document_id, chunk_profile_version""",
            (build_id,),
        )
        conn.execute(
            """CREATE INDEX idx_temp_new_doc_profiles_lookup
               ON temp_new_doc_profiles(document_id, chunk_profile_version)"""
        )
        conn.execute(_RECON_PROFILE_REPLACEMENTS_SQL)
        conn.execute(
            """CREATE INDEX idx_temp_profile_replacements_lookup
               ON temp_profile_replacements(document_id, chunk_profile_version)"""
        )

    _run_recon_transaction(
        conn,
        statements=statements,
        checkpoint=None,
        build_id=build_id,
        now=None,
        deadline=deadline,
        flags=flags,
    )
    count = int(
        conn.execute("SELECT COUNT(*) FROM temp_previous_served").fetchone()[0]
    )
    digest = _reconciliation_temp_digest(conn)
    # A durable checkpoint means initialize already bound the base: verify the
    # recomputed manifest, count, and digest fail closed even when the bound
    # base manifest is NULL (fresh-DB first build had no previous served).
    checkpoint = _recon_checkpoint_read(conn, build_id)
    if checkpoint is not None:
        bound = conn.execute(
            """SELECT reconciliation_base_manifest_id,
                      reconciliation_base_chunk_count,
                      reconciliation_base_inventory_digest
               FROM corpus_publication_builds WHERE build_id=?""",
            (build_id,),
        ).fetchone()
        if bound is None:
            raise ValueError(
                "reconciliation base bound values missing on resume"
            )
        if base_manifest_id != bound[0]:
            raise ValueError(
                "reconciliation base manifest changed; refusing to resume"
            )
        if count != int(bound[1] or 0):
            raise ValueError(
                "reconciliation base chunk count changed; refusing to resume"
            )
        if digest != bound[2]:
            raise ValueError(
                "reconciliation base inventory digest changed; refusing to resume"
            )
    return {
        "kind": base_kind,
        "manifest_id": base_manifest_id,
        "published_chunk_count": published_chunk_count,
        "count": count,
        "digest": digest,
    }


def _validate_reconciliation_finalize(conn: sqlite3.Connection, build_id: str) -> None:
    for name, sql in _RECON_FINALIZE_VALIDATIONS:
        count = conn.execute(sql, (build_id, build_id)).fetchone()[0]
        if int(count) != 0:
            raise ValueError(f"reconciliation finalize validation failed: {name}={count}")


def _recon_phase_initialize(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    now: str,
    deadline: float,
    flags: dict[str, bool],
    failure_injector: Callable[[str], None] | None,
    state: dict[str, object],
) -> None:
    base = state["base"]
    if base["kind"] == "published_build" and int(base["count"]) != int(
        base["published_chunk_count"]
    ):
        raise ValueError(
            "reconciliation base chunk count does not match published build"
        )

    def statements() -> None:
        conn.execute(
            """UPDATE corpus_publication_builds
               SET reconciliation_base_manifest_id=?,
                   reconciliation_base_chunk_count=?,
                   reconciliation_base_inventory_digest=?
               WHERE build_id=?""",
            (base["manifest_id"], base["count"], base["digest"], build_id),
        )

    _run_recon_transaction(
        conn,
        statements=statements,
        checkpoint="initialized",
        build_id=build_id,
        now=now,
        deadline=deadline,
        flags=flags,
    )
    if failure_injector is not None:
        failure_injector("after_recon_initialized")


def _recon_phase_to_embed(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    now: str,
    deadline: float,
    flags: dict[str, bool],
    failure_injector: Callable[[str], None] | None,
    state: dict[str, object],
) -> None:
    def statements() -> None:
        conn.execute(
            "DELETE FROM corpus_build_deltas WHERE build_id=? AND delta_kind='to_embed'",
            (build_id,),
        )
        conn.execute(_RECON_TO_EMBED_SQL, (build_id, build_id))

    _run_recon_transaction(
        conn,
        statements=statements,
        checkpoint="to_embed_done",
        build_id=build_id,
        now=now,
        deadline=deadline,
        flags=flags,
    )
    if failure_injector is not None:
        failure_injector("after_recon_to_embed_done")


def _recon_phase_cluster_tombstones(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    now: str,
    deadline: float,
    flags: dict[str, bool],
    failure_injector: Callable[[str], None] | None,
    state: dict[str, object],
) -> None:
    def statements() -> None:
        conn.execute(
            """DELETE FROM corpus_build_deltas
               WHERE build_id=? AND delta_kind='tombstone'
                 AND reason='dedup_cluster_reassigned'""",
            (build_id,),
        )
        conn.execute(_RECON_CLUSTER_TOMBSTONE_SQL, (build_id, build_id))

    _run_recon_transaction(
        conn,
        statements=statements,
        checkpoint="cluster_tombstones_done",
        build_id=build_id,
        now=now,
        deadline=deadline,
        flags=flags,
    )
    if failure_injector is not None:
        failure_injector("after_recon_cluster_tombstones_done")


def _recon_phase_metadata_updates(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    now: str,
    deadline: float,
    flags: dict[str, bool],
    failure_injector: Callable[[str], None] | None,
    state: dict[str, object],
) -> None:
    def statements() -> None:
        conn.execute(
            """DELETE FROM corpus_build_deltas
               WHERE build_id=? AND delta_kind='metadata_update'""",
            (build_id,),
        )
        conn.execute(_RECON_METADATA_UPDATE_SQL, (build_id, build_id))

    _run_recon_transaction(
        conn,
        statements=statements,
        checkpoint="metadata_updates_done",
        build_id=build_id,
        now=now,
        deadline=deadline,
        flags=flags,
    )
    if failure_injector is not None:
        failure_injector("after_recon_metadata_updates_done")


def _recon_phase_removed_tombstones(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    now: str,
    deadline: float,
    flags: dict[str, bool],
    failure_injector: Callable[[str], None] | None,
    state: dict[str, object],
) -> None:
    def statements() -> None:
        conn.execute(
            """DELETE FROM corpus_build_deltas
               WHERE build_id=? AND delta_kind='tombstone'
                 AND reason IN ('eligibility_lost','profile_version_replaced',
                                'disappeared_child','document_removed')""",
            (build_id,),
        )
        conn.execute(_RECON_REMOVED_TOMBSTONE_SQL, (build_id, build_id, build_id))

    _run_recon_transaction(
        conn,
        statements=statements,
        checkpoint="removed_tombstones_done",
        build_id=build_id,
        now=now,
        deadline=deadline,
        flags=flags,
    )
    if failure_injector is not None:
        failure_injector("after_recon_removed_tombstones_done")


def _recon_phase_statuses(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    now: str,
    deadline: float,
    flags: dict[str, bool],
    failure_injector: Callable[[str], None] | None,
    state: dict[str, object],
) -> None:
    def statements() -> None:
        conn.execute(_RECON_STATUSES_PENDING_SQL, (build_id, build_id))
        conn.execute(_RECON_STATUSES_METADATA_SQL, (build_id, build_id))

    _run_recon_transaction(
        conn,
        statements=statements,
        checkpoint="statuses_done",
        build_id=build_id,
        now=now,
        deadline=deadline,
        flags=flags,
    )
    if failure_injector is not None:
        failure_injector("after_recon_statuses_done")


def _recon_phase_finalize(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    now: str,
    deadline: float,
    flags: dict[str, bool],
    failure_injector: Callable[[str], None] | None,
    state: dict[str, object],
) -> None:
    def statements() -> None:
        _validate_reconciliation_finalize(conn, build_id)
        conn.execute(
            """UPDATE corpus_publication_builds
               SET status='reconciliation_ready', reconciliation_ready=1
               WHERE build_id=?""",
            (build_id,),
        )

    _run_recon_transaction(
        conn,
        statements=statements,
        checkpoint="reconciliation_ready",
        build_id=build_id,
        now=now,
        deadline=deadline,
        flags=flags,
    )
    if failure_injector is not None:
        failure_injector("after_recon_finalize")


_RECON_PHASES = {
    None: _recon_phase_initialize,
    "initialized": _recon_phase_to_embed,
    "to_embed_done": _recon_phase_cluster_tombstones,
    "cluster_tombstones_done": _recon_phase_metadata_updates,
    "metadata_updates_done": _recon_phase_removed_tombstones,
    "removed_tombstones_done": _recon_phase_statuses,
    "statuses_done": _recon_phase_finalize,
}


def _backfill_reconciliation_ready(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    now: str,
    deadline: float,
    flags: dict[str, bool],
) -> None:
    def statements() -> None:
        pass

    _run_recon_transaction(
        conn,
        statements=statements,
        checkpoint="reconciliation_ready",
        build_id=build_id,
        now=now,
        deadline=deadline,
        flags=flags,
    )


def _reconciliation_phase(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    now: str,
    failure_injector: Callable[[str], None] | None = None,
    deadline: float = 900.0,
    operator_interrupt: dict[str, bool] | None = None,
) -> ReconciliationSummary:
    """Run remaining reconciliation phases until skip_all, deadline, or interrupt.

    One call completes every remaining phase. TEMP is rebuilt on this
    connection before dispatch; after each durable checkpoint COMMIT the
    checkpoint is re-read and the next phase dispatched.
    """
    ensure_streaming_publication_schema(conn)
    row = conn.execute(
        "SELECT status, reconciliation_ready FROM corpus_publication_builds "
        "WHERE build_id=?",
        (build_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"reconciliation build not found: {build_id}")
    status = str(row[0])
    ready_flag = int(row[1] or 0)
    checkpoint = _recon_checkpoint_read(conn, build_id)
    if checkpoint == "reconciliation_ready":
        if status != "reconciliation_ready" or ready_flag != 1:
            raise ValueError(
                "inconsistent reconciliation state: reconciliation_ready checkpoint "
                "without a flipped build status"
            )
        return _reconciliation_summary(conn, build_id)
    if status != "manifest_ready":
        raise ValueError(f"reconciliation requires manifest_ready, got status={status!r}")
    if ready_flag != 0:
        raise ValueError(
            "inconsistent reconciliation state: reconciliation_ready flag set "
            "before status flip"
        )
    flags = _recon_flags(operator_interrupt)
    base = _materialize_reconciliation_temp(
        conn, build_id=build_id, deadline=deadline, flags=flags
    )
    state: dict[str, object] = {"base": base}
    while checkpoint != "reconciliation_ready":
        if checkpoint not in _RECON_PHASES:
            raise ValueError(f"unknown reconciliation checkpoint: {checkpoint!r}")
        phase = _RECON_PHASES[checkpoint]
        phase(
            conn,
            build_id=build_id,
            now=now,
            deadline=deadline,
            flags=flags,
            failure_injector=failure_injector,
            state=state,
        )
        checkpoint = _recon_checkpoint_read(conn, build_id)
    if failure_injector is not None:
        failure_injector("after_reconciliation")
    return _reconciliation_summary(conn, build_id)


def explain_reconciliation_dml(
    conn: sqlite3.Connection, *, build_id: str
) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Dry-run helper: create empty TEMP schema and EXPLAIN each delta DML.

    Never populates the previous-served snapshot, never INSERTs deltas, never
    advances a checkpoint, and never writes evidence. TEMP DDL is not
    SAVEPOINT+ROLLBACKed (a rollback would drop TEMP); closing the connection
    drops the TEMP tables.
    """
    conn.execute("DROP TABLE IF EXISTS temp_previous_served")
    conn.execute("DROP TABLE IF EXISTS temp_tombstone_survivors")
    conn.execute("DROP TABLE IF EXISTS temp_profile_replacements")
    conn.execute("DROP TABLE IF EXISTS temp_new_doc_profiles")
    conn.execute(
        """CREATE TEMP TABLE temp_previous_served (
            chunk_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            chunk_profile_version TEXT NOT NULL,
            content_hash TEXT,
            metadata_hash TEXT,
            dedup_cluster_id TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX idx_temp_previous_served_chunk ON temp_previous_served(chunk_id)"
    )
    conn.execute(
        "CREATE INDEX idx_temp_previous_served_document ON temp_previous_served(document_id)"
    )
    conn.execute(
        """CREATE TEMP TABLE temp_tombstone_survivors (
            chunk_id TEXT COLLATE BINARY PRIMARY KEY,
            document_id TEXT NOT NULL,
            chunk_profile_version TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            metadata_hash TEXT NOT NULL
        ) WITHOUT ROWID"""
    )
    conn.execute(
        """CREATE INDEX idx_temp_tombstone_survivors_document
           ON temp_tombstone_survivors(document_id, chunk_profile_version)"""
    )
    conn.execute(
        """CREATE TEMP TABLE temp_new_doc_profiles (
            document_id TEXT NOT NULL,
            chunk_profile_version TEXT NOT NULL,
            min_chunk_id TEXT
        )"""
    )
    conn.execute(
        """CREATE INDEX idx_temp_new_doc_profiles_lookup
           ON temp_new_doc_profiles(document_id, chunk_profile_version)"""
    )
    conn.execute(
        """CREATE TEMP TABLE temp_profile_replacements (
            document_id TEXT NOT NULL,
            chunk_profile_version TEXT NOT NULL,
            replacement_chunk_id TEXT
        )"""
    )
    conn.execute(
        """CREATE INDEX idx_temp_profile_replacements_lookup
           ON temp_profile_replacements(document_id, chunk_profile_version)"""
    )
    dml = (
        ("to_embed", _RECON_TO_EMBED_SQL, (build_id, build_id)),
        ("cluster_tombstones", _RECON_CLUSTER_TOMBSTONE_SQL, (build_id, build_id)),
        ("metadata_updates", _RECON_METADATA_UPDATE_SQL, (build_id, build_id)),
        (
            "removed_tombstones",
            _RECON_REMOVED_TOMBSTONE_SQL,
            (build_id, build_id, build_id),
        ),
    )
    plans: dict[str, tuple[tuple[object, ...], ...]] = {}
    for name, sql, params in dml:
        cursor = conn.execute("EXPLAIN QUERY PLAN " + sql, params)
        plans[name] = tuple(tuple(row) for row in cursor)
    return plans


def reconciliation_resume_state(
    conn: sqlite3.Connection, *, build_id: str
) -> tuple[str, str | None]:
    """L1 fail-closed ready-state matrix over status and checkpoint."""
    row = conn.execute(
        "SELECT status, reconciliation_ready FROM corpus_publication_builds "
        "WHERE build_id=?",
        (build_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"reconciliation build not found: {build_id}")
    status = str(row[0])
    ready_flag = int(row[1] or 0)
    checkpoint = _recon_checkpoint_read(conn, build_id)
    if status == "reconciliation_ready":
        if ready_flag != 1:
            raise ValueError(
                "inconsistent reconciliation state: reconciliation_ready flag is not set"
            )
        if checkpoint is None or checkpoint == "reconciliation_ready":
            return status, checkpoint
        raise ValueError(
            "inconsistent reconciliation state: "
            f"status={status!r} checkpoint={checkpoint!r}"
        )
    if status == "manifest_ready":
        if ready_flag != 0:
            raise ValueError(
                "inconsistent reconciliation state: reconciliation_ready flag set "
                "before status flip"
            )
        if checkpoint == "reconciliation_ready":
            raise ValueError(
                "inconsistent reconciliation state: status=manifest_ready "
                "with reconciliation_ready checkpoint"
            )
        return status, checkpoint
    raise ValueError(f"reconciliation resume not applicable to status={status!r}")


def resume_candidate_reconciliation(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    now: str | None = None,
    failure_injector: Callable[[str], None] | None = None,
    deadline: float = 900.0,
    operator_interrupt: dict[str, bool] | None = None,
) -> ReconciliationSummary:
    """Resume reconciliation for one pinned build_id (L1 + dispatch wrapper)."""
    if now is None:
        now = _utc_now()
    ensure_streaming_publication_schema(conn)
    status, checkpoint = reconciliation_resume_state(conn, build_id=build_id)
    if status == "reconciliation_ready":
        _validate_reconciliation_finalize(conn, build_id)
        if checkpoint is None:
            flags = _recon_flags(operator_interrupt)
            _backfill_reconciliation_ready(
                conn, build_id=build_id, now=now, deadline=deadline, flags=flags
            )
        return _reconciliation_summary(conn, build_id)
    return _reconciliation_phase(
        conn,
        build_id=build_id,
        now=now,
        failure_injector=failure_injector,
        deadline=deadline,
        operator_interrupt=operator_interrupt,
    )


def _reconciliation_summary(
    conn: sqlite3.Connection, build_id: str
) -> ReconciliationSummary:
    def count(kind: str) -> int:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM corpus_build_deltas WHERE build_id=? AND delta_kind=?",
                (build_id, kind),
            ).fetchone()[0]
        )

    return ReconciliationSummary(
        to_embed_count=count("to_embed"),
        metadata_update_count=count("metadata_update"),
        tombstone_count=count("tombstone"),
    )


def iter_reconciliation_pages(
    conn: sqlite3.Connection,
    build_id: str,
    *,
    page_size: int = 500,
) -> Iterator[tuple[tuple[object, ...], ...]]:
    if not 1 <= page_size <= 500:
        raise ValueError("page_size must be between 1 and 500")
    cursor = conn.execute(
        """SELECT delta_kind, chunk_id, document_id, reason,
                  previous_content_hash, previous_metadata_hash,
                  replacement_chunk_id
           FROM corpus_build_deltas WHERE build_id=?
           ORDER BY delta_kind COLLATE BINARY, chunk_id COLLATE BINARY""",
        (build_id,),
    )
    while True:
        page = cursor.fetchmany(page_size)
        if not page:
            break
        yield tuple(tuple(row) for row in page)

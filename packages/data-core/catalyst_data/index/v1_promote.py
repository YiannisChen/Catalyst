"""Reversible V1.1 generation promotion (M3-12A; execution-lock §I).

``promote_v1_generation`` is the sole pointer-promotion owner: it writes a
durable ``m3_promotion_journal_v2``, flips corpus+FTS SQLite pointers in one
``BEGIN IMMEDIATE``, atomically replaces the dense ``active_generation.json``
pointer, verifies the activated generation, and journals every state
transition. Any activation or post-activation verification failure triggers the
single locked rollback rule (dense-first restore, then one-transaction SQLite
restore, then mandatory prior verification). ``rollback_v1_generation`` restores
the prior generation from the journal.

Runtime admission is allowed only for ``COMMITTED``, or for the prior generation
after a verified ``ROLLED_BACK``. ``SQLITE_COMMITTED`` alone (SQLite-new /
dense-old) is never admissible and forces rollback.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalyst_data.corpus.streaming_publication import (
    _cutover,
    served_chunks_relation,
)
from catalyst_data.index.v1_staging import (
    InactiveDenseCandidate,
    validate_dense_candidate,
)
from catalyst_data.retrieval.gpu_contract import _atomic_json

JOURNAL_SCHEMA = "m3_promotion_journal_v2"
STATES = (
    "PREPARED",
    "SQLITE_COMMITTED",
    "COMMITTED",
    "ROLLING_BACK",
    "ROLLED_BACK",
    "FAILED",
)
ACTIVE_GENERATION_SCHEMA = "active_generation_v1"

_REQUIRED_JOURNAL_FIELDS = (
    "schema_version",
    "state",
    "promotion_id",
    "build_id",
    "corpus_manifest_id",
    "lexical_generation_id",
    "lexical_digest",
    "dense_index_manifest_id",
    "active_generation_path",
    "created_at",
    "updated_at",
    "prior",
)


@dataclass(frozen=True)
class PromotionResult:
    state: str
    promotion_id: str
    build_id: str
    corpus_manifest_id: str
    lexical_generation_id: str
    dense_index_manifest_id: str
    journal_path: Path
    admitted: bool

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"unknown promotion state: {self.state!r}")
        if self.admitted is not (self.state == "COMMITTED"):
            raise ValueError(
                "PromotionResult admitted must be True only for COMMITTED"
            )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_journal(path: Path, payload: dict[str, Any]) -> None:
    """Atomically persist the journal with file + directory fsync."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    try:
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def _read_journal(path: Path) -> dict[str, Any]:
    """Read and validate the promotion journal; fail closed on corruption."""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("promotion journal unreadable or malformed") from exc
    if not isinstance(payload, dict):
        raise ValueError("promotion journal must be a JSON object")
    if payload.get("schema_version") != JOURNAL_SCHEMA:
        raise ValueError("promotion journal schema mismatch")
    if payload.get("state") not in STATES:
        raise ValueError("promotion journal state is invalid")
    for field in _REQUIRED_JOURNAL_FIELDS:
        if field not in payload:
            raise ValueError(f"promotion journal missing {field}")
    if not isinstance(payload["prior"], dict):
        raise ValueError("promotion journal prior block must be an object")
    return payload


def _read_dense_pointer(path: Path) -> dict[str, Any] | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("active_generation.json unreadable or malformed") from exc
    if not isinstance(payload, dict):
        raise ValueError("active_generation.json must be a JSON object")
    return payload


def _read_current_manifest(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT manifest_id, manifest_json, is_current FROM corpus_manifest "
        "WHERE is_current=1"
    ).fetchone()
    if row is None:
        return None
    return {
        "corpus_manifest_id": str(row[0]),
        "corpus_manifest_json": str(row[1]),
    }


def _read_lexical_state(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT schema_version, corpus_manifest_id, mode_served,
                  fallback_reason, row_count, built_at, lexical_generation_id,
                  lexical_digest
           FROM lexical_index_state WHERE singleton_id=1"""
    ).fetchone()
    if row is None:
        return None
    return {
        "schema_version": row[0],
        "corpus_manifest_id": row[1],
        "mode_served": row[2],
        "fallback_reason": row[3],
        "row_count": int(row[4] or 0),
        "built_at": row[5],
        "lexical_generation_id": row[6],
        "lexical_digest": row[7],
    }


def _live_fts_row_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='corpus_chunks_fts'"
    ).fetchone()
    if row is None:
        return 0
    return int(conn.execute("SELECT COUNT(*) FROM corpus_chunks_fts").fetchone()[0])


def _served_chunk_count(conn: sqlite3.Connection) -> int:
    relation = served_chunks_relation(conn)
    return int(conn.execute(f"SELECT COUNT(*) FROM {relation}").fetchone()[0])


def _flip_sqlite_pointers(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    manifest_id: str,
    now: str,
) -> None:
    """Atomically flip corpus+FTS SQLite pointers in one BEGIN IMMEDIATE."""
    row = conn.execute(
        "SELECT certified_snapshot_identity FROM corpus_publication_builds "
        "WHERE build_id=?",
        (build_id,),
    ).fetchone()
    if row is None or not row[0]:
        raise ValueError("candidate build missing certified snapshot identity")
    _cutover(
        conn,
        build_id=build_id,
        manifest_id=manifest_id,
        certified_snapshot_identity=str(row[0]),
        now=now,
        failure_injector=None,
    )


def _sqlite_pointer_is_new(conn: sqlite3.Connection, corpus_manifest_id: str) -> bool:
    row = conn.execute(
        "SELECT manifest_id FROM corpus_manifest WHERE is_current=1"
    ).fetchone()
    return row is not None and str(row[0]) == corpus_manifest_id


def _dense_pointer_payload(
    dense_candidate: InactiveDenseCandidate, certified_snapshot_identity: str
) -> dict[str, Any]:
    return {
        "schema_version": ACTIVE_GENERATION_SCHEMA,
        "index_manifest_id": dense_candidate.index_manifest_id,
        "table_name": dense_candidate.table_name,
        "source_bundle_id": dense_candidate.source_bundle_id,
        "snapshot_id": certified_snapshot_identity,
        "corpus_manifest_id": dense_candidate.corpus_manifest_id,
        "chunk_count": dense_candidate.chunk_count,
    }


def _replace_dense_pointer(path: Path, payload: dict[str, Any]) -> None:
    _atomic_json(path, payload)


def _restore_dense_pointer(path: Path, prior_dense: dict[str, Any] | None) -> None:
    if prior_dense is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    _atomic_json(path, prior_dense)


def _restore_sqlite_pointers(
    conn: sqlite3.Connection, prior: dict[str, Any]
) -> None:
    """Restore prior corpus+FTS pointers in one BEGIN IMMEDIATE transaction."""
    lex = prior["lexical_index_state"]
    build_id = prior.get("build_id") or ""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE corpus_manifest SET is_current=0 WHERE is_current=1")
        conn.execute(
            "UPDATE corpus_manifest SET is_current=1, manifest_json=? "
            "WHERE manifest_id=?",
            (prior["corpus_manifest_json"], prior["corpus_manifest_id"]),
        )
        conn.execute("DELETE FROM lexical_index_state")
        conn.execute(
            """INSERT INTO lexical_index_state
               (singleton_id, schema_version, corpus_manifest_id, mode_served,
                fallback_reason, row_count, built_at, lexical_generation_id,
                lexical_digest)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                lex.get("schema_version") or "1.0.0",
                prior["corpus_manifest_id"],
                lex.get("mode_served") or "fts5",
                lex.get("fallback_reason"),
                int(lex.get("row_count") or 0),
                lex.get("built_at") or "",
                lex.get("lexical_generation_id"),
                lex.get("lexical_digest"),
            ),
        )
        if build_id:
            conn.execute(
                "UPDATE corpus_publication_builds SET status=?, published_at=NULL "
                "WHERE build_id=?",
                (prior.get("build_status") or "lexical_ready", build_id),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _verify_active_generation(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    corpus_manifest_id: str,
    lexical_digest: str,
    dense_candidate: InactiveDenseCandidate,
    active_generation_path: Path,
) -> None:
    manifest = conn.execute(
        "SELECT manifest_id FROM corpus_manifest WHERE is_current=1"
    ).fetchone()
    if manifest is None or str(manifest[0]) != corpus_manifest_id:
        raise ValueError("corpus pointer mismatch after promotion")
    lex = conn.execute(
        """SELECT corpus_manifest_id, mode_served, row_count,
                  lexical_generation_id, lexical_digest
           FROM lexical_index_state WHERE singleton_id=1"""
    ).fetchone()
    if (
        lex is None
        or str(lex[0]) != corpus_manifest_id
        or str(lex[3]) != build_id
        or (lex[4] or "") != lexical_digest
    ):
        raise ValueError("lexical pointer mismatch after promotion")
    served = _served_chunk_count(conn)
    fts = int(
        conn.execute(
            "SELECT COUNT(*) FROM corpus_build_chunks_fts WHERE build_id=?",
            (build_id,),
        ).fetchone()[0]
    )
    if served != fts or fts != dense_candidate.chunk_count:
        raise ValueError(
            "served/FTS/dense count parity failure after promotion"
        )
    dense = _read_dense_pointer(active_generation_path)
    if dense is None:
        raise ValueError("active_generation.json missing after promotion")
    if dense.get("index_manifest_id") != dense_candidate.index_manifest_id:
        raise ValueError("dense pointer index_manifest_id mismatch")
    if dense.get("corpus_manifest_id") != corpus_manifest_id:
        raise ValueError("dense pointer corpus_manifest_id mismatch")
    if dense.get("chunk_count") != dense_candidate.chunk_count:
        raise ValueError("dense pointer chunk_count mismatch")


def _verify_prior_generation(
    conn: sqlite3.Connection,
    prior: dict[str, Any],
    active_generation_path: Path,
) -> None:
    manifest = _read_current_manifest(conn)
    if (
        manifest is None
        or manifest["corpus_manifest_id"] != prior["corpus_manifest_id"]
        or manifest["corpus_manifest_json"] != prior["corpus_manifest_json"]
    ):
        raise ValueError("prior corpus manifest restore mismatch")
    lex = _read_lexical_state(conn)
    expected_lex = prior["lexical_index_state"]
    if lex is None or {
        key: lex.get(key) for key in (
            "schema_version", "corpus_manifest_id", "mode_served",
            "fallback_reason", "row_count", "built_at", "lexical_generation_id",
            "lexical_digest",
        )
    } != {
        key: expected_lex.get(key) for key in (
            "schema_version", "corpus_manifest_id", "mode_served",
            "fallback_reason", "row_count", "built_at", "lexical_generation_id",
            "lexical_digest",
        )
    }:
        raise ValueError("prior lexical pointer restore mismatch")
    prior_dense = prior.get("dense_pointer")
    if prior_dense is None:
        if active_generation_path.exists():
            raise ValueError("prior dense pointer was absent but file exists")
    else:
        dense = _read_dense_pointer(active_generation_path)
        if dense != prior_dense:
            raise ValueError("prior dense pointer restore mismatch")
    served = _served_chunk_count(conn)
    if served != int(prior.get("served_chunk_count") or 0):
        raise ValueError("prior served count mismatch")
    fts = _live_fts_row_count(conn)
    if fts != int(prior.get("fts_row_count") or 0):
        raise ValueError("prior FTS count mismatch")
    build_id = prior.get("build_id") or ""
    if build_id:
        row = conn.execute(
            "SELECT status FROM corpus_publication_builds WHERE build_id=?",
            (build_id,),
        ).fetchone()
        if row is None or str(row[0]) != (prior.get("build_status") or "lexical_ready"):
            raise ValueError("prior build status mismatch")


def _build_prior_block(conn: sqlite3.Connection, active_generation_path: Path) -> dict[str, Any]:
    manifest = _read_current_manifest(conn)
    if manifest is None:
        raise ValueError("no current corpus manifest to back up")
    lex = _read_lexical_state(conn)
    if lex is None:
        raise ValueError("no current lexical_index_state to back up")
    return {
        "corpus_manifest_id": manifest["corpus_manifest_id"],
        "corpus_manifest_json": manifest["corpus_manifest_json"],
        "build_status": "lexical_ready",
        "lexical_index_state": lex,
        "dense_pointer": _read_dense_pointer(active_generation_path),
        "served_chunk_count": _served_chunk_count(conn),
        "fts_row_count": _live_fts_row_count(conn),
        "dense_chunk_count": None,
    }


def _perform_rollback(
    conn: sqlite3.Connection,
    journal: dict[str, Any],
    journal_path: Path,
    active_generation_path: Path,
) -> PromotionResult:
    """Single locked rollback rule: dense-first, then one-transaction SQLite."""
    prior = journal["prior"]
    prior_with_build = dict(prior)
    prior_with_build["build_id"] = journal["build_id"]
    if journal["state"] != "ROLLING_BACK":
        _write_journal(
            journal_path,
            {**journal, "state": "ROLLING_BACK", "updated_at": _utc_now()},
        )
    try:
        # 1. Restore the prior dense pointer first.
        _restore_dense_pointer(active_generation_path, prior.get("dense_pointer"))
        # 2. Restore prior corpus+FTS pointers in one BEGIN IMMEDIATE.
        _restore_sqlite_pointers(conn, prior_with_build)
        # 3. Verify prior identities/counts before ROLLED_BACK.
        _verify_prior_generation(conn, prior_with_build, active_generation_path)
    except Exception:
        # Block runtime: journal stays ROLLING_BACK; no admission.
        raise
    _write_journal(
        journal_path,
        {**journal, "state": "ROLLED_BACK", "updated_at": _utc_now()},
    )
    return PromotionResult(
        state="ROLLED_BACK",
        promotion_id=journal["promotion_id"],
        build_id=journal["build_id"],
        corpus_manifest_id=journal["corpus_manifest_id"],
        lexical_generation_id=journal["lexical_generation_id"],
        dense_index_manifest_id=journal["dense_index_manifest_id"],
        journal_path=journal_path,
        admitted=False,
    )


def _journal_payload(
    *,
    promotion_id: str,
    build_id: str,
    corpus_manifest_id: str,
    lexical_digest: str,
    dense_candidate: InactiveDenseCandidate,
    active_generation_path: Path,
    prior: dict[str, Any],
    now: str,
    state: str,
) -> dict[str, Any]:
    return {
        "schema_version": JOURNAL_SCHEMA,
        "state": state,
        "promotion_id": promotion_id,
        "build_id": build_id,
        "corpus_manifest_id": corpus_manifest_id,
        "lexical_generation_id": build_id,
        "lexical_digest": lexical_digest,
        "dense_index_manifest_id": dense_candidate.index_manifest_id,
        "active_generation_path": str(active_generation_path),
        "created_at": now,
        "updated_at": now,
        "prior": prior,
    }


def _verify_requested_identities(
    journal: dict[str, Any],
    *,
    build_id: str,
    corpus_manifest_id: str,
    lexical_digest: str,
    dense_candidate: InactiveDenseCandidate,
) -> None:
    if journal["build_id"] != build_id:
        raise ValueError("promotion journal build_id mismatch")
    if journal["corpus_manifest_id"] != corpus_manifest_id:
        raise ValueError("promotion journal corpus_manifest_id mismatch")
    if journal["lexical_digest"] != lexical_digest:
        raise ValueError("promotion journal lexical_digest mismatch")
    if journal["dense_index_manifest_id"] != dense_candidate.index_manifest_id:
        raise ValueError("promotion journal dense_index_manifest_id mismatch")


def promote_v1_generation(
    conn: sqlite3.Connection,
    *,
    journal_path: Path,
    build_id: str,
    corpus_manifest_id: str,
    lexical_digest: str,
    dense_candidate: InactiveDenseCandidate,
    active_generation_path: Path,
) -> PromotionResult:
    """Promote one inactive generation; journaled, reversible, fail-closed."""
    if not isinstance(dense_candidate, InactiveDenseCandidate):
        raise TypeError("dense_candidate must be an InactiveDenseCandidate")
    validate_dense_candidate(
        dense_candidate, expected_chunk_count=dense_candidate.chunk_count
    )
    journal_path = Path(journal_path)
    active_generation_path = Path(active_generation_path)

    if journal_path.exists():
        journal = _read_journal(journal_path)
        state = journal["state"]
        if state == "COMMITTED":
            _verify_requested_identities(
                journal,
                build_id=build_id,
                corpus_manifest_id=corpus_manifest_id,
                lexical_digest=lexical_digest,
                dense_candidate=dense_candidate,
            )
            _verify_active_generation(
                conn,
                build_id=build_id,
                corpus_manifest_id=corpus_manifest_id,
                lexical_digest=lexical_digest,
                dense_candidate=dense_candidate,
                active_generation_path=active_generation_path,
            )
            return PromotionResult(
                state="COMMITTED",
                promotion_id=journal["promotion_id"],
                build_id=build_id,
                corpus_manifest_id=corpus_manifest_id,
                lexical_generation_id=build_id,
                dense_index_manifest_id=dense_candidate.index_manifest_id,
                journal_path=journal_path,
                admitted=True,
            )
        if state == "SQLITE_COMMITTED":
            # SQLite-new/dense-old is never admissible: mandatory rollback.
            return _perform_rollback(
                conn, journal, journal_path, active_generation_path
            )
        if state == "ROLLING_BACK":
            return _perform_rollback(
                conn, journal, journal_path, active_generation_path
            )
        if state == "PREPARED":
            if _sqlite_pointer_is_new(conn, corpus_manifest_id):
                # Crash after SQLite flip, before SQLITE_COMMITTED: roll forward.
                _verify_requested_identities(
                    journal,
                    build_id=build_id,
                    corpus_manifest_id=corpus_manifest_id,
                    lexical_digest=lexical_digest,
                    dense_candidate=dense_candidate,
                )
                _write_journal(
                    journal_path,
                    {**journal, "state": "SQLITE_COMMITTED", "updated_at": _utc_now()},
                )
                return _finish_after_sqlite(
                    conn,
                    journal=journal,
                    journal_path=journal_path,
                    build_id=build_id,
                    corpus_manifest_id=corpus_manifest_id,
                    lexical_digest=lexical_digest,
                    dense_candidate=dense_candidate,
                    active_generation_path=active_generation_path,
                )
            # Crash before the flip: pointers untouched; continue fresh.
            _verify_prior_generation(
                conn, {**journal["prior"], "build_id": journal["build_id"]},
                active_generation_path,
            )
            # fall through to fresh promotion below (overwrites journal)
        elif state == "ROLLED_BACK":
            _verify_prior_generation(
                conn, {**journal["prior"], "build_id": journal["build_id"]},
                active_generation_path,
            )
            # Retry of the same build is allowed after a verified rollback.
        elif state == "FAILED":
            _verify_prior_generation(
                conn, {**journal["prior"], "build_id": journal["build_id"]},
                active_generation_path,
            )
            # FAILED is legal only before pointer mutation; fresh retry allowed.
        else:  # pragma: no cover - _read_journal validates state
            raise ValueError("promotion journal state is invalid")

    # ---- fresh promotion ----
    now = _utc_now()
    promotion_id = uuid.uuid4().hex
    prior = _build_prior_block(conn, active_generation_path)
    journal = _journal_payload(
        promotion_id=promotion_id,
        build_id=build_id,
        corpus_manifest_id=corpus_manifest_id,
        lexical_digest=lexical_digest,
        dense_candidate=dense_candidate,
        active_generation_path=active_generation_path,
        prior=prior,
        now=now,
        state="PREPARED",
    )
    _write_journal(journal_path, journal)

    try:
        _flip_sqlite_pointers(
            conn, build_id=build_id, manifest_id=corpus_manifest_id, now=now
        )
    except Exception:
        if _sqlite_pointer_is_new(conn, corpus_manifest_id):
            # Crash after SQLite flip, before SQLITE_COMMITTED: roll forward.
            journal = _read_journal(journal_path)
            _write_journal(
                journal_path,
                {**journal, "state": "SQLITE_COMMITTED", "updated_at": _utc_now()},
            )
            return _finish_after_sqlite(
                conn,
                journal=journal,
                journal_path=journal_path,
                build_id=build_id,
                corpus_manifest_id=corpus_manifest_id,
                lexical_digest=lexical_digest,
                dense_candidate=dense_candidate,
                active_generation_path=active_generation_path,
            )
        _write_journal(
            journal_path,
            {**journal, "state": "FAILED", "updated_at": _utc_now()},
        )
        return PromotionResult(
            state="FAILED",
            promotion_id=promotion_id,
            build_id=build_id,
            corpus_manifest_id=corpus_manifest_id,
            lexical_generation_id=build_id,
            dense_index_manifest_id=dense_candidate.index_manifest_id,
            journal_path=journal_path,
            admitted=False,
        )

    _write_journal(
        journal_path,
        {**journal, "state": "SQLITE_COMMITTED", "updated_at": _utc_now()},
    )
    return _finish_after_sqlite(
        conn,
        journal=journal,
        journal_path=journal_path,
        build_id=build_id,
        corpus_manifest_id=corpus_manifest_id,
        lexical_digest=lexical_digest,
        dense_candidate=dense_candidate,
        active_generation_path=active_generation_path,
    )


def _finish_after_sqlite(
    conn: sqlite3.Connection,
    *,
    journal: dict[str, Any],
    journal_path: Path,
    build_id: str,
    corpus_manifest_id: str,
    lexical_digest: str,
    dense_candidate: InactiveDenseCandidate,
    active_generation_path: Path,
) -> PromotionResult:
    """Replace the dense pointer, verify, and journal COMMITTED (or roll back)."""
    try:
        certified = conn.execute(
            "SELECT certified_snapshot_identity FROM corpus_publication_builds "
            "WHERE build_id=?",
            (build_id,),
        ).fetchone()
        if certified is None or not certified[0]:
            raise ValueError("candidate build missing certified snapshot identity")
        _replace_dense_pointer(
            active_generation_path,
            _dense_pointer_payload(dense_candidate, str(certified[0])),
        )
        _verify_active_generation(
            conn,
            build_id=build_id,
            corpus_manifest_id=corpus_manifest_id,
            lexical_digest=lexical_digest,
            dense_candidate=dense_candidate,
            active_generation_path=active_generation_path,
        )
    except Exception:
        current = _read_journal(journal_path)
        return _perform_rollback(
            conn, current, journal_path, active_generation_path
        )
    _write_journal(
        journal_path,
        {**journal, "state": "COMMITTED", "updated_at": _utc_now()},
    )
    return PromotionResult(
        state="COMMITTED",
        promotion_id=journal["promotion_id"],
        build_id=build_id,
        corpus_manifest_id=corpus_manifest_id,
        lexical_generation_id=build_id,
        dense_index_manifest_id=dense_candidate.index_manifest_id,
        journal_path=journal_path,
        admitted=True,
    )


def rollback_v1_generation(
    conn: sqlite3.Connection,
    *,
    journal_path: Path,
    active_generation_path: Path,
) -> PromotionResult:
    """Restore the prior generation from the journal (fail closed)."""
    journal_path = Path(journal_path)
    active_generation_path = Path(active_generation_path)
    if not journal_path.exists():
        raise ValueError("promotion journal missing; nothing to roll back")
    journal = _read_journal(journal_path)
    state = journal["state"]
    if state == "ROLLED_BACK":
        _verify_prior_generation(
            conn,
            {**journal["prior"], "build_id": journal["build_id"]},
            active_generation_path,
        )
        return PromotionResult(
            state="ROLLED_BACK",
            promotion_id=journal["promotion_id"],
            build_id=journal["build_id"],
            corpus_manifest_id=journal["corpus_manifest_id"],
            lexical_generation_id=journal["lexical_generation_id"],
            dense_index_manifest_id=journal["dense_index_manifest_id"],
            journal_path=journal_path,
            admitted=False,
        )
    if state == "FAILED":
        _verify_prior_generation(
            conn,
            {**journal["prior"], "build_id": journal["build_id"]},
            active_generation_path,
        )
        return PromotionResult(
            state="FAILED",
            promotion_id=journal["promotion_id"],
            build_id=journal["build_id"],
            corpus_manifest_id=journal["corpus_manifest_id"],
            lexical_generation_id=journal["lexical_generation_id"],
            dense_index_manifest_id=journal["dense_index_manifest_id"],
            journal_path=journal_path,
            admitted=False,
        )
    return _perform_rollback(conn, journal, journal_path, active_generation_path)


__all__ = ["PromotionResult", "promote_v1_generation", "rollback_v1_generation"]

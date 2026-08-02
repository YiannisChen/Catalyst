"""Audited recovery of rebuildable Pre-B6 candidate state.

This module deliberately limits recovery to derived corpus and retrieval
tables. Provider payloads, normalized entities, provenance, and filing source
documents are fingerprinted before and after the transaction and are never
updated by the reset.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from catalyst_data.migrations import CORPUS_CHUNKS_FTS_DDL


EXPECTED_USER_VERSION = 13

SOURCE_TABLES = (
    "raw_assets",
    "provider_request_attempts",
    "normalized_provenance",
    "articles",
    "article_tickers",
    "filings",
    "filing_documents",
)

# Delete dependants before their manifests. FTS shadow tables are maintained by
# SQLite and must never be edited directly.
DERIVED_TABLES = (
    "corpus_chunks_fts",
    "lexical_index_state",
    "index_state",
    "corpus_tombstones",
    "corpus_chunks",
    "corpus_manifest",
    "index_manifests",
)

DERIVED_TABLES_DELETE_ORDER = tuple(
    table for table in DERIVED_TABLES if table != "corpus_chunks_fts"
)

FTS_SHADOW_TABLES = (
    "corpus_chunks_fts_data",
    "corpus_chunks_fts_idx",
    "corpus_chunks_fts_content",
    "corpus_chunks_fts_docsize",
    "corpus_chunks_fts_config",
)

STREAMING_DERIVED_TABLES_DROP_ORDER = (
    "corpus_build_fts_batches",
    "corpus_build_deltas",
    "corpus_build_chunks",
    "corpus_build_source_documents",
    "corpus_build_documents",
    "corpus_publication_builds",
)

EMPTY_FTS_ROWS = {
    "corpus_chunks_fts": 0,
    "corpus_chunks_fts_data": 2,
    "corpus_chunks_fts_idx": 0,
    "corpus_chunks_fts_content": 0,
    "corpus_chunks_fts_docsize": 0,
    "corpus_chunks_fts_config": 1,
}

class CandidateRecoveryError(RuntimeError):
    """The candidate does not satisfy the recovery safety contract."""


@dataclass(frozen=True)
class CandidateResetResult:
    schema_version: str
    status: str
    database_path: str
    user_version: int
    reset_at: str
    source_tables_before: Mapping[str, Mapping[str, Any]]
    source_tables_after: Mapping[str, Mapping[str, Any]]
    derived_rows_before: Mapping[str, int]
    derived_rows_after: Mapping[str, int]
    fts_shadow_rows_before: Mapping[str, int]
    fts_shadow_rows_after: Mapping[str, int]
    integrity_check: str
    foreign_key_violations: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (table,),
    ).fetchone() is not None


def _require_tables(conn: sqlite3.Connection, tables: Sequence[str]) -> None:
    missing = [table for table in tables if not _table_exists(conn, table)]
    if missing:
        raise CandidateRecoveryError(
            "candidate is missing required tables: " + ", ".join(missing)
        )


def _fingerprint_projection(
    conn: sqlite3.Connection, table: str
) -> tuple[list[str], list[str]]:
    columns = conn.execute(
        f"PRAGMA table_info({_quote_identifier(table)})"
    ).fetchall()
    if not columns:
        raise CandidateRecoveryError(f"cannot inspect source table: {table}")

    columns = sorted(columns, key=lambda row: row[0])
    selected = [row[1] for row in columns]
    projection = [_quote_identifier(column) for column in selected]

    primary_key = [
        row[1]
        for row in sorted(columns, key=lambda item: item[5])
        if row[5] > 0
    ]
    if not primary_key:
        raise CandidateRecoveryError(f"source table has no primary key: {table}")
    return projection, primary_key


def _canonical_sqlite_value(value: Any) -> list[Any]:
    if value is None:
        return ["null", None]
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, int):
        return ["integer", str(value)]
    if isinstance(value, float):
        return ["real", value.hex()]
    raise CandidateRecoveryError(
        f"unsupported SQLite value type in source fingerprint: {type(value)!r}"
    )


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def fingerprint_source_tables(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    """Return deterministic logical fingerprints for protected source tables."""
    _require_tables(conn, SOURCE_TABLES)
    result: dict[str, dict[str, Any]] = {}
    for table in SOURCE_TABLES:
        projection, primary_key = _fingerprint_projection(conn, table)
        order_by = ", ".join(_quote_identifier(column) for column in primary_key)
        query = (
            f"SELECT {', '.join(projection)} FROM {_quote_identifier(table)} "
            f"ORDER BY {order_by}"
        )
        digest = hashlib.sha256()
        digest.update(_canonical_json_bytes(["columns", projection]))
        digest.update(b"\n")
        row_count = 0
        for row in conn.execute(query):
            digest.update(
                _canonical_json_bytes(
                    [_canonical_sqlite_value(value) for value in row]
                )
            )
            digest.update(b"\n")
            row_count += 1
        result[table] = {
            "row_count": row_count,
            "logical_sha256": digest.hexdigest(),
        }
    return result


def _derived_counts(conn: sqlite3.Connection) -> dict[str, int]:
    _require_tables(conn, DERIVED_TABLES)
    return {
        table: int(
            conn.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
            ).fetchone()[0]
        )
        for table in DERIVED_TABLES
    }


def _fts_rows(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ("corpus_chunks_fts", *FTS_SHADOW_TABLES)
    _require_tables(conn, tables)
    return {
        table: int(
            conn.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
            ).fetchone()[0]
        )
        for table in tables
    }


def _validate_candidate_path(path: Path) -> None:
    if path.parent.name != "candidates" or path.suffix != ".db":
        raise CandidateRecoveryError(
            "recovery is restricted to a .db file inside a candidates directory"
        )
    if not path.is_file():
        raise CandidateRecoveryError(f"candidate database does not exist: {path}")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def reset_pre_b6_candidate(
    db_path: str | Path,
    *,
    audit_path: str | Path,
    clock: Callable[[], str] = _utc_now,
) -> CandidateResetResult:
    """Transactionally clear only rebuildable corpus and retrieval state."""
    path = Path(db_path).expanduser().resolve()
    report_path = Path(audit_path).expanduser().resolve()
    _validate_candidate_path(path)
    if report_path.exists() and report_path.is_dir():
        raise CandidateRecoveryError(f"audit path is a directory: {report_path}")

    conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if user_version != EXPECTED_USER_VERSION:
            raise CandidateRecoveryError(
                f"expected user_version={EXPECTED_USER_VERSION}, got {user_version}"
            )
        _require_tables(
            conn,
            SOURCE_TABLES + DERIVED_TABLES + FTS_SHADOW_TABLES,
        )

        source_before = fingerprint_source_tables(conn)
        derived_before = _derived_counts(conn)
        fts_before = _fts_rows(conn)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DROP VIEW IF EXISTS corpus_served_chunks")
            conn.execute("DROP TABLE IF EXISTS corpus_build_chunks_fts")
            for table in STREAMING_DERIVED_TABLES_DROP_ORDER:
                conn.execute(f"DROP TABLE IF EXISTS {_quote_identifier(table)}")
            conn.execute("DROP TABLE corpus_chunks_fts")
            conn.execute(CORPUS_CHUNKS_FTS_DDL)
            for table in DERIVED_TABLES_DELETE_ORDER:
                conn.execute(f"DELETE FROM {_quote_identifier(table)}")

            source_after = fingerprint_source_tables(conn)
            if source_after != source_before:
                raise CandidateRecoveryError(
                    "protected source table fingerprint changed during reset"
                )
            foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_rows:
                raise CandidateRecoveryError(
                    f"foreign_key_check found {len(foreign_key_rows)} violations"
                )
            derived_after = _derived_counts(conn)
            if any(derived_after.values()):
                raise CandidateRecoveryError(
                    f"derived reset left rows behind: {derived_after}"
                )
            fts_after = _fts_rows(conn)
            if fts_after != EMPTY_FTS_ROWS:
                raise CandidateRecoveryError(
                    f"FTS reset did not reach empty physical state: {fts_after}"
                )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise

        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise CandidateRecoveryError(f"integrity_check failed: {integrity}")

        result = CandidateResetResult(
            schema_version="1.1.0",
            status="complete_for_rebuild",
            database_path=str(path),
            user_version=user_version,
            reset_at=clock(),
            source_tables_before=source_before,
            source_tables_after=source_after,
            derived_rows_before=derived_before,
            derived_rows_after=derived_after,
            fts_shadow_rows_before=fts_before,
            fts_shadow_rows_after=fts_after,
            integrity_check=integrity,
            foreign_key_violations=0,
        )
        _atomic_write_json(report_path, result.to_dict())
        return result
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m catalyst_data.candidate_recovery"
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--audit-report", required=True)
    args = parser.parse_args(argv)
    try:
        result = reset_pre_b6_candidate(
            args.db,
            audit_path=args.audit_report,
        )
    except (CandidateRecoveryError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

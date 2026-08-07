#!/usr/bin/env python3
"""B6-L production LanceDB import operator CLI.

Subcommands:

- ``preflight``       zero-write verification of every identity and input gate.
- ``execute``         bounded import into a unique staging table, then atomic
                      active-generation pointer switch after persisted
                      validation.
- ``execute --resume`` continue an interrupted import from its checkpoint.

There is intentionally no ``--force`` or identity-bypass argument. The CLI
never writes to the SQLite database, source bundle, or embedding-artifact
payload files (vectors.npy / chunk_ids.json).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT / "packages" / "data-core") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "packages" / "data-core"))

import numpy as np  # noqa: E402

from catalyst_data.retrieval.git_revision import resolve_git_revision  # noqa: E402
from catalyst_data.retrieval.gpu_contract import (  # noqa: E402
    IMPORT_BATCH_SIZE,
    import_vectors_to_lancedb,
    production_lancedb_schema,
    validate_embedding_import,
    verify_source_bundle,
)
from catalyst_data.retrieval.import_metadata import (  # noqa: E402
    iter_import_metadata,
)
from catalyst_data.retrieval.index_manifest import IndexManifest  # noqa: E402

EXPECTED_DB_SCHEMA_VERSION = 13
STAGING_PREFIX = "chunks__staging__"
REPORT_SCHEMA_VERSION = "lancedb_import_report_v1"
STAGING_STATE_SCHEMA_VERSION = "lancedb_staging_state_v1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass




_SECRET_WARNING_PATTERN = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|credential|authorization|private[_-]?key)"
    r"\s*[=:]\s*\S+"
)


def _sanitize_warning(text: str) -> str:
    """Strip credential-like material and absolute paths from warning text."""
    cleaned = _SECRET_WARNING_PATTERN.sub(r"\1=<redacted>", str(text))
    cleaned = re.sub(r"(?i)\bbearer\s+\S+", "bearer <redacted>", cleaned)
    cleaned = re.sub(r"file://\S+", "<path>", cleaned)
    cleaned = re.sub(r"(?<![\w:])/[\w./~+-]+", "<path>", cleaned)
    return cleaned


def _committed_generation_match(
    active_pointer_path: Path,
    state_identity: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the active pointer payload when it already names a committed
    generation for this exact import identity, else None.

    This is the duplicate-generation guard: a retry after a post-commit
    warning must not create a second staging table for the same generation.
    """
    active = _read_active_pointer(active_pointer_path, allow_missing=True)
    if active is None:
        return None
    for key in ("index_manifest_id", "source_bundle_id", "snapshot_id", "corpus_manifest_id"):
        if active.get(key) != state_identity.get(key):
            return None
    return active


def _exception_committed_state(
    active_pointer_path: Path,
    artifact_path: Path,
    table_name: str,
    expected_chunk_count: int,
) -> dict[str, Any] | None:
    """Re-read the pointer immediately after an import exception.

    Returns None when there is no evidence this run's table was committed.
    Otherwise returns ``{"matched": bool, "manifest": dict}`` where
    ``matched`` is True only when the pointer fully matches the committed
    identity (table_name, index_manifest_id, source_bundle_id, snapshot_id,
    corpus_manifest_id, chunk_count) and the artifact is in the
    ``lancedb_imported`` state.
    """
    pointer = _read_active_pointer(active_pointer_path, allow_missing=True)
    if pointer is None or pointer.get("table_name") != table_name:
        return None
    try:
        raw_manifest = json.loads(
            (artifact_path / "index_manifest.json").read_text(encoding="utf-8")
        )
    except (ValueError, OSError):
        return None
    if raw_manifest.get("artifact_state") != "lancedb_imported":
        return None
    index_manifest = IndexManifest.from_dict(
        {key: value for key, value in raw_manifest.items() if key != "non_production"}
    )
    expected = {
        "index_manifest_id": index_manifest.index_manifest_id,
        "source_bundle_id": raw_manifest.get("source_bundle_id"),
        "snapshot_id": raw_manifest.get("snapshot_id"),
        "corpus_manifest_id": raw_manifest.get("corpus_manifest_id"),
        "chunk_count": expected_chunk_count,
    }
    matched = all(pointer.get(key) == value for key, value in expected.items())
    return {"matched": matched, "manifest": raw_manifest, "index_manifest": index_manifest}


class _CommittedReportIdentity:
    """Report identity for the ambiguous-commit path (no final_manifest)."""

    def __init__(self, raw_manifest: dict[str, Any], index_manifest_id: str):
        self.source_bundle_id = raw_manifest["source_bundle_id"]
        self.snapshot_id = raw_manifest["snapshot_id"]
        self.corpus_manifest_id = raw_manifest["corpus_manifest_id"]
        self.probe_report_id = raw_manifest["probe_report_id"]
        self.postbuild_readiness_id = raw_manifest["postbuild_readiness_id"]
        self.index_manifest_id = index_manifest_id
        self.vector_count = int(raw_manifest["vector_count"])


def _open_db(path: Path) -> sqlite3.Connection:
    """Open the frozen snapshot DB strictly read-only (never touches WAL)."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


def _verify_snapshot_pointer(
    pointer_path: Path, *, expected_snapshot_id: str, expected_db_sha: str
) -> dict[str, Any]:
    if not pointer_path.is_file():
        raise ValueError(f"active snapshot pointer missing: {pointer_path}")
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    if pointer.get("schema_version") != "1.0.0":
        raise ValueError("active snapshot pointer schema mismatch")
    if pointer.get("snapshot_id") != expected_snapshot_id:
        raise ValueError("active snapshot pointer snapshot_id mismatch")
    db_path = Path(pointer.get("db_path") or "")
    if not db_path.is_file():
        raise ValueError(f"active snapshot database missing: {db_path}")
    actual_sha = _sha256_file(db_path)
    if actual_sha != expected_db_sha or pointer.get("sha256") != expected_db_sha:
        raise ValueError("active snapshot DB SHA mismatch")
    return {
        "db_path": str(db_path),
        "snapshot_id": expected_snapshot_id,
        "sha256": actual_sha,
    }


def _verify_db(conn: sqlite3.Connection, expected_corpus_manifest_id: str) -> dict[str, Any]:
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if user_version != EXPECTED_DB_SCHEMA_VERSION:
        raise ValueError(
            f"SQLite schema version mismatch: {user_version} != {EXPECTED_DB_SCHEMA_VERSION}"
        )
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise ValueError(f"SQLite integrity_check failed: {integrity}")
    foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_rows:
        raise ValueError(
            f"SQLite foreign_key_check found {len(foreign_key_rows)} violations"
        )
    row = conn.execute(
        "SELECT manifest_id FROM corpus_manifest WHERE is_current = 1"
    ).fetchone()
    if row is None:
        raise ValueError("corpus has no current manifest")
    if row[0] != expected_corpus_manifest_id:
        raise ValueError(
            "current corpus manifest does not match expected corpus_manifest_id"
        )
    return {
        "user_version": user_version,
        "integrity": integrity,
        "foreign_key_violations": len(foreign_key_rows),
        "current_manifest_id": row[0],
    }


def _verify_import_inputs(
    args: argparse.Namespace, *, code_revision: str
) -> tuple[Any, Any]:
    bundle = verify_source_bundle(
        Path(args.source_bundle),
        expected_source_bundle_id=args.expected_source_bundle_id,
        expected_snapshot_id=args.expected_snapshot_id,
        expected_corpus_manifest_id=args.expected_corpus_manifest_id,
        expected_probe_report_id=args.expected_probe_report_id,
        expected_postbuild_readiness_id=args.expected_postbuild_readiness_id,
    )
    artifact_path = Path(args.embedding_artifact)
    raw_manifest = json.loads((artifact_path / "index_manifest.json").read_text(encoding="utf-8"))
    lancedb_table = None
    if raw_manifest.get("artifact_state") == "lancedb_imported":
        if getattr(args, "resume", False):
            # Interrupt during persisted validation leaves the artifact
            # lancedb_imported while the pointer is not yet committed.
            # Validate against the staging table named by staging_state; a
            # missing state means there is no recoverable staging work.
            state_path = Path(args.lancedb_dir) / "staging_state.json"
            if not state_path.is_file():
                raise ValueError("resume requires an existing staging_state.json")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            table_name = state.get("table_name")
        else:
            # The artifact was already imported by a prior run (for example a
            # committed_with_warning retry). Validate it against the table
            # named by the active pointer; a missing or corrupt pointer is an
            # inconsistent state and fails closed before any new write.
            active = _read_active_pointer(args.active_generation_pointer, allow_missing=False)
            table_name = active.get("table_name")
        if not isinstance(table_name, str) or not table_name:
            raise ValueError("table identity missing for imported artifact")
        import lancedb

        lancedb_table = lancedb.connect(str(args.lancedb_dir)).open_table(table_name)
    manifest = validate_embedding_import(
        Path(args.source_bundle), artifact_path,
        allow_non_production=False,
        lancedb_table=lancedb_table,
    )
    if manifest.code_revision != code_revision:
        raise ValueError("embedding artifact code_revision mismatch")
    return bundle, manifest


def _verify_metadata_stream(
    conn: sqlite3.Connection, corpus_manifest_id: str, expected_count: int, *, page_size: int
) -> dict[str, Any]:
    count = 0
    first_chunk_id = last_chunk_id = None
    for row in iter_import_metadata(
        conn, corpus_manifest_id=corpus_manifest_id, page_size=page_size
    ):
        count += 1
        if first_chunk_id is None:
            first_chunk_id = row.chunk_id
        last_chunk_id = row.chunk_id
    if count != expected_count:
        raise ValueError(f"metadata stream count mismatch: {count} != {expected_count}")
    return {
        "count": count,
        "first_chunk_id": first_chunk_id,
        "last_chunk_id": last_chunk_id,
    }


def _check_disk_capacity(lancedb_dir: Path, chunk_count: int) -> dict[str, Any]:
    target = lancedb_dir if lancedb_dir.exists() else lancedb_dir.parent
    required = max(10 * 1024**3, chunk_count * (1024 * 4 + 2048) * 2)
    free = shutil.disk_usage(target).free
    if free < required:
        raise ValueError(
            f"target disk free {free} < required {required} at {target}"
        )
    return {"free_bytes": free, "required_bytes": required, "target": str(target)}


def _pointer_status(
    lancedb_dir: Path, active_pointer_path: Path
) -> dict[str, Any]:
    active = {}
    if active_pointer_path.is_file():
        raw = json.loads(active_pointer_path.read_text(encoding="utf-8"))
        active = {
            "exists": True,
            "table_name": raw.get("table_name"),
            "index_manifest_id": raw.get("index_manifest_id"),
            "chunk_count": raw.get("chunk_count"),
        }
    else:
        active = {"exists": False, "table_name": None, "index_manifest_id": None, "chunk_count": None}
    staging_tables: list[str] = []
    if lancedb_dir.is_dir():
        staging_tables = sorted(
            path.name for path in lancedb_dir.iterdir()
            if path.is_dir() and path.name.startswith(STAGING_PREFIX)
        )
    return {
        "active_generation": active,
        "staging_tables": staging_tables,
        "checkpoint_exists": (lancedb_dir / "import_checkpoint.json").is_file(),
        "staging_state_exists": (lancedb_dir / "staging_state.json").is_file(),
    }


def _read_active_pointer(
    active_pointer_path: Path, *, allow_missing: bool = False
) -> dict[str, Any] | None:
    """Read and validate the active-generation pointer.

    Fails closed when the pointer exists but is corrupt. When ``allow_missing``
    is True a missing pointer is treated as "no active generation" (used by
    cleanup, where there is no active table to protect).
    """
    if not active_pointer_path.is_file():
        if allow_missing:
            return None
        raise ValueError(f"active generation pointer missing: {active_pointer_path}")
    try:
        raw = json.loads(active_pointer_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(
            f"active generation pointer corrupt: {active_pointer_path}"
        ) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("table_name"), str):
        raise ValueError(f"active generation pointer corrupt: {active_pointer_path}")
    return raw


def _active_table_name(active_pointer_path: Path) -> str | None:
    active = _read_active_pointer(active_pointer_path, allow_missing=True)
    return active.get("table_name") if active is not None else None


def _create_staging_table(lancedb_dir: Path, active_table_name: str | None) -> Any:
    import lancedb

    db = lancedb.connect(str(lancedb_dir))
    if hasattr(db, "list_tables"):
        response = db.list_tables()
        existing = set(response.tables)
    elif hasattr(db, "table_names"):
        existing = set(db.table_names())
    else:
        existing = set()
    while True:
        name = f"{STAGING_PREFIX}{uuid.uuid4().hex[:16]}"
        if name != active_table_name and name not in existing:
            break
    return db.create_table(name, data=[], schema=production_lancedb_schema())


def _load_staging_table(
    lancedb_dir: Path,
    active_pointer_path: Path,
    *,
    expected_identity: dict[str, str],
    artifact_consumed: bool = False,
) -> tuple[Any, Path]:
    """Validate and open the resume staging table.

    Every stable identity bound at state-write time must still match the
    current invocation; the state table must never equal the
    active-generation table; a corrupt active pointer fails closed. When
    ``artifact_consumed`` is True the embedding artifact has already been
    mutated to ``lancedb_imported`` (interrupt during persisted validation),
    so the state's pre-mutation index_manifest_id/artifact_checksum bindings
    no longer compare equal; content is still bound by the import checkpoint
    and the final persisted validation stamps/checks the table rows.
    """
    state_path = lancedb_dir / "staging_state.json"
    if not state_path.is_file():
        raise ValueError("resume requires an existing staging_state.json")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("schema_version") != STAGING_STATE_SCHEMA_VERSION:
        raise ValueError("staging_state schema mismatch")
    table_name = state.get("table_name")
    if not isinstance(table_name, str) or not table_name.startswith(STAGING_PREFIX):
        raise ValueError("staging_state table_name is invalid")
    for key, expected in expected_identity.items():
        if artifact_consumed and key in ("index_manifest_id", "artifact_checksum"):
            continue
        if state.get(key) != expected:
            raise ValueError(f"staging_state {key} mismatch")
    active = _read_active_pointer(active_pointer_path, allow_missing=True)
    if active is not None and active.get("table_name") == table_name:
        raise ValueError(
            "staging_state points at the active table; refusing resume"
        )
    import lancedb

    db = lancedb.connect(str(lancedb_dir))
    return db.open_table(table_name), state_path


def _drop_staging_table(
    lancedb_dir: Path, table_name: str, active_pointer_path: Path
) -> None:
    """Drop exactly one staging table created/opened by this run.

    The active pointer is re-read immediately before dropping so a concurrent
    switch cannot cause the active table to be deleted: if the pointer names
    this table, the drop is refused. A corrupt pointer fails closed; a missing
    pointer means there is no active generation to protect. Other staging
    tables are never touched.
    """
    active = _read_active_pointer(active_pointer_path, allow_missing=True)
    if active is not None and active.get("table_name") == table_name:
        raise ValueError(f"refusing to drop active table {table_name}")
    import lancedb

    db = lancedb.connect(str(lancedb_dir))
    if hasattr(db, "list_tables"):
        tables = set(db.list_tables().tables)
    elif hasattr(db, "table_names"):
        tables = set(db.table_names())
    else:
        tables = set()
    if table_name not in tables:
        return
    db.drop_table(table_name)


def _add_common_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--active-snapshot-pointer", type=Path, required=True)
    sub.add_argument("--source-bundle", type=Path, required=True)
    sub.add_argument("--embedding-artifact", type=Path, required=True)
    sub.add_argument("--lancedb-dir", type=Path, required=True)
    sub.add_argument("--active-generation-pointer", type=Path, required=True)
    sub.add_argument("--expected-source-bundle-id", required=True)
    sub.add_argument("--expected-snapshot-id", required=True)
    sub.add_argument("--expected-corpus-manifest-id", required=True)
    sub.add_argument("--expected-probe-report-id", required=True)
    sub.add_argument("--expected-postbuild-readiness-id", required=True)
    sub.add_argument("--expected-db-sha", required=True)
    sub.add_argument("--code-revision", required=True)
    sub.add_argument("--import-batch-size", type=int, default=500)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    _add_common_args(preflight)
    execute = subparsers.add_parser("execute")
    execute.add_argument("--resume", action="store_true", help="resume an interrupted execute")
    _add_common_args(execute)
    return parser


def _preflight(args: argparse.Namespace) -> int:
    code_revision = resolve_git_revision(
        repo_root=REPO_ROOT, expected=args.code_revision, require_clean=True,
    )
    pointer_info = _verify_snapshot_pointer(
        args.active_snapshot_pointer,
        expected_snapshot_id=args.expected_snapshot_id,
        expected_db_sha=args.expected_db_sha,
    )
    conn = _open_db(Path(pointer_info["db_path"]))
    try:
        db_info = _verify_db(conn, args.expected_corpus_manifest_id)
    finally:
        conn.close()
    bundle, manifest = _verify_import_inputs(args, code_revision=code_revision)
    conn = _open_db(Path(pointer_info["db_path"]))
    try:
        metadata_info = _verify_metadata_stream(
            conn,
            corpus_manifest_id=args.expected_corpus_manifest_id,
            expected_count=bundle.chunk_count,
            page_size=args.import_batch_size,
        )
    finally:
        conn.close()
    disk_info = _check_disk_capacity(Path(args.lancedb_dir), bundle.chunk_count)
    status = _pointer_status(Path(args.lancedb_dir), args.active_generation_pointer)
    report = {
        "schema_version": "lancedb_preflight_v1",
        "ok": True,
        "subcommand": "preflight",
        "git_clean": True,
        "code_revision": code_revision,
        "snapshot_pointer": pointer_info,
        "db": db_info,
        "source_bundle": {
            "source_bundle_id": bundle.source_bundle_id,
            "chunk_count": bundle.chunk_count,
            "snapshot_id": bundle.snapshot_id,
            "corpus_manifest_id": bundle.corpus_manifest_id,
            "probe_report_id": bundle.probe_report_id,
            "postbuild_readiness_id": bundle.postbuild_readiness_id,
        },
        "embedding_artifact": {
            "model_name": manifest.model_name,
            "model_revision": manifest.model_revision,
            "dtype": manifest.dtype,
            "dimension": manifest.dimension,
            "vector_count": manifest.vector_count,
            "code_revision": manifest.code_revision,
            "artifact_state": manifest.artifact_state,
        },
        "metadata": metadata_info,
        "disk": disk_info,
        "pointer_status": status,
        "chunk_count": bundle.chunk_count,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


def _metadata_row_iter(args: argparse.Namespace, db_path: Path) -> Iterator[dict[str, Any]]:
    conn = _open_db(Path(db_path))
    try:
        for row in iter_import_metadata(
            conn,
            corpus_manifest_id=args.expected_corpus_manifest_id,
            page_size=args.import_batch_size,
        ):
            yield row.to_dict()
    finally:
        conn.close()




def _base_execute_report(
    args: argparse.Namespace,
    final_manifest: Any,
    *,
    table_name: str,
    db_info: dict[str, Any],
    resume: bool,
    status: str,
    written_at_utc: str | None = None,
) -> dict[str, Any]:
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "ok": True,
        "status": status,
        "subcommand": "execute",
        "resumed": resume,
        "source_bundle_id": final_manifest.source_bundle_id,
        "snapshot_id": final_manifest.snapshot_id,
        "corpus_manifest_id": final_manifest.corpus_manifest_id,
        "probe_report_id": final_manifest.probe_report_id,
        "postbuild_readiness_id": final_manifest.postbuild_readiness_id,
        "index_manifest_id": final_manifest.index_manifest_id,
        "table_name": table_name,
        "chunk_count": final_manifest.vector_count,
        "import_batch_size": args.import_batch_size,
        "db": db_info,
        "written_at_utc": written_at_utc or datetime.now(timezone.utc).isoformat(),
    }
    return report


def _emit_report(report: dict[str, Any], *, lancedb_dir: Path) -> int:
    """Write the report; on failure emit structured committed_with_warning to
    stderr and return the ops exit code 2. Never rolls back the commit."""
    try:
        _atomic_json(lancedb_dir / "import_report.json", report)
    except Exception as exc:  # noqa: BLE001 - committed; warning only
        warning = {
            "code": "import_report_write_failed",
            "message": _sanitize_warning(str(exc)),
        }
        report["status"] = "committed_with_warning"
        report.setdefault("warnings", []).append(warning)
        print(
            json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report.get("status") in ("committed", "already_committed") else 2


def _post_commit(
    *,
    lancedb_dir: Path,
    args: argparse.Namespace,
    final_manifest: Any,
    table_name: str,
    db_info: dict[str, Any],
    resume: bool,
    payload_before: dict[str, str],
    state_path: Path | None,
) -> int:
    """Post-commit state machine (COMMITTED). Every failure here is a
    committed_with_warning: the active generation is preserved, never rolled
    back, never cleared, and never dropped."""
    warnings: list[dict[str, str]] = []
    active_pointer_path = Path(args.active_generation_pointer)

    # Re-read the pointer and verify the exact committed identity before any
    # cleanup touches post-commit state.
    pointer_identity_ok = False
    try:
        pointer = _read_active_pointer(active_pointer_path)
        expected = {
            "table_name": table_name,
            "index_manifest_id": final_manifest.index_manifest_id,
            "source_bundle_id": final_manifest.source_bundle_id,
            "snapshot_id": final_manifest.snapshot_id,
            "corpus_manifest_id": final_manifest.corpus_manifest_id,
        }
        for key, expected_value in expected.items():
            if pointer.get(key) != expected_value:
                raise ValueError(
                    f"committed pointer {key} mismatch: "
                    f"pointer={pointer.get(key)!r} expected={expected_value!r}"
                )
        pointer_identity_ok = True
    except Exception as exc:  # noqa: BLE001 - committed; warning only
        warnings.append({
            "code": "pointer_identity_mismatch",
            "message": _sanitize_warning(str(exc)),
        })

    # staging_state is only removed after the full pointer identity is
    # verified. When the pointer is missing, corrupt, or mismatched, the
    # recovery state is preserved for investigation and a later retry.
    if pointer_identity_ok and state_path is not None:
        try:
            _remove_if_exists(state_path)
        except Exception as exc:  # noqa: BLE001 - committed; warning only
            warnings.append({
                "code": "staging_state_cleanup_failed",
                "message": _sanitize_warning(str(exc)),
            })

    artifact_path = Path(args.embedding_artifact)
    try:
        payload_after = {
            "vectors.npy": _sha256_file(artifact_path / "vectors.npy"),
            "chunk_ids.json": _sha256_file(artifact_path / "chunk_ids.json"),
        }
        if payload_after != payload_before:
            raise ValueError("embedding artifact payload changed during import")
    except Exception as exc:  # noqa: BLE001 - committed; warning only
        warnings.append({
            "code": "artifact_payload_drift",
            "message": _sanitize_warning(str(exc)),
        })

    status = "committed_with_warning" if warnings else "committed"
    report = _base_execute_report(
        args, final_manifest, table_name=table_name, db_info=db_info,
        resume=resume, status=status,
    )
    if warnings:
        report["warnings"] = warnings
    return _emit_report(report, lancedb_dir=lancedb_dir)


def _already_committed(
    lancedb_dir: Path,
    args: argparse.Namespace,
    bundle: Any,
    manifest: Any,
    db_info: dict[str, Any],
    pointer: dict[str, Any],
) -> int:
    """A retry detected that this exact generation is already active. Fail
    closed if the committed table is missing or row count drifted; otherwise
    recover the missing report idempotently without a duplicate import."""
    table_name = pointer.get("table_name")
    if not isinstance(table_name, str):
        raise ValueError("active pointer has no table_name during already-committed check")
    try:
        import lancedb

        db = lancedb.connect(str(lancedb_dir))
        table = db.open_table(table_name)
        row_count = table.count_rows()
    except Exception as exc:
        raise ValueError(
            f"active generation table unavailable during already-committed check: {exc}"
        ) from exc
    if row_count != bundle.chunk_count:
        raise ValueError(
            f"active table row count mismatch during already-committed check: "
            f"{row_count} != {bundle.chunk_count}"
        )
    # The generation is confirmed committed; remove stale recovery artifacts
    # left by an interrupted post-commit run (a future --resume would refuse
    # them anyway). Failures never block report recovery.
    for stale in ("import_checkpoint.json", "staging_state.json"):
        try:
            _remove_if_exists(lancedb_dir / stale)
        except Exception:  # noqa: BLE001 - committed; recovery is best-effort
            pass
    report = _base_execute_report(
        args, manifest, table_name=table_name, db_info=db_info,
        resume=False, status="already_committed",
    )
    report["note"] = "generation already committed; no import performed"
    return _emit_report(report, lancedb_dir=lancedb_dir)


def _execute(args: argparse.Namespace, *, resume: bool) -> int:
    code_revision = resolve_git_revision(
        repo_root=REPO_ROOT, expected=args.code_revision, require_clean=True,
    )
    pointer_info = _verify_snapshot_pointer(
        args.active_snapshot_pointer,
        expected_snapshot_id=args.expected_snapshot_id,
        expected_db_sha=args.expected_db_sha,
    )
    conn = _open_db(Path(pointer_info["db_path"]))
    try:
        db_info = _verify_db(conn, args.expected_corpus_manifest_id)
    finally:
        conn.close()
    bundle, manifest = _verify_import_inputs(args, code_revision=code_revision)

    lancedb_dir = Path(args.lancedb_dir).resolve()
    checkpoint_path = lancedb_dir / "import_checkpoint.json"
    artifact_path = Path(args.embedding_artifact)
    payload_before = {
        "vectors.npy": _sha256_file(artifact_path / "vectors.npy"),
        "chunk_ids.json": _sha256_file(artifact_path / "chunk_ids.json"),
    }

    staging_table = None
    state_path = None
    state_identity = {
        "source_bundle_id": args.expected_source_bundle_id,
        "snapshot_id": args.expected_snapshot_id,
        "corpus_manifest_id": args.expected_corpus_manifest_id,
        "index_manifest_id": manifest.index_manifest_id,
        "code_revision": code_revision,
        "artifact_checksum": _sha256_file(artifact_path / "checksums.sha256"),
    }
    active_pointer_path = Path(args.active_generation_pointer)
    try:
        if resume:
            staging_table, state_path = _load_staging_table(
                lancedb_dir, active_pointer_path, expected_identity=state_identity,
                artifact_consumed=(manifest.artifact_state == "lancedb_imported"),
            )
        else:
            committed = _committed_generation_match(active_pointer_path, state_identity)
            if committed is not None:
                return _already_committed(
                    lancedb_dir,
                    args,
                    bundle,
                    manifest,
                    db_info,
                    committed,
                )
            if checkpoint_path.is_file():
                raise ValueError(
                    "import checkpoint exists; pass --resume to continue the interrupted import"
                )
            state_path = lancedb_dir / "staging_state.json"
            if state_path.is_file():
                raise ValueError(
                    "staging_state exists; a generation is mid-flight or its "
                    "pointer identity is uncertain; resolve the active pointer "
                    "or use --resume before a fresh execute"
                )
            staging_table = _create_staging_table(
                lancedb_dir, _active_table_name(active_pointer_path)
            )
            _atomic_json(state_path, {
                "schema_version": STAGING_STATE_SCHEMA_VERSION,
                "table_name": staging_table.name,
                **state_identity,
            })

        final_manifest = import_vectors_to_lancedb(
            Path(args.source_bundle),
            artifact_path,
            staging_table,
            metadata_rows=_metadata_row_iter(args, pointer_info["db_path"]),
            allow_non_production=False,
            import_batch_size=args.import_batch_size,
            checkpoint_path=checkpoint_path,
            resume=resume,
            active_pointer_path=active_pointer_path,
        )
    except KeyboardInterrupt:
        # Staging rows, checkpoint, and staging_state are intentionally
        # preserved so `execute --resume` can continue from the last batch.
        raise
    except Exception as exc:
        # Activation commit ambiguity: the pointer's atomic writer may have
        # completed os.replace and then raised. Re-read the pointer now; if it
        # fully matches the committed identity (and the artifact is
        # lancedb_imported), the generation is COMMITTED. No pre-commit
        # cleanup, no drop/clear of the active table, no deletion of recovery
        # state; surface committed_with_warning with exit code 2.
        if staging_table is not None:
            committed_state = _exception_committed_state(
                active_pointer_path,
                artifact_path,
                staging_table.name,
                bundle.chunk_count,
            )
            if committed_state is not None:
                raw_manifest = committed_state["manifest"]
                index_manifest = committed_state["index_manifest"]
                warnings = [{
                    "code": "post_commit_exception",
                    "message": _sanitize_warning(str(exc)),
                }]
                if not committed_state["matched"]:
                    warnings.append({
                        "code": "pointer_identity_mismatch",
                        "message": (
                            "committed pointer identity does not fully match "
                            "the committed generation; recovery state preserved"
                        ),
                    })
                report = _base_execute_report(
                    args,
                    _CommittedReportIdentity(
                        raw_manifest, index_manifest.index_manifest_id,
                    ),
                    table_name=staging_table.name,
                    db_info=db_info,
                    resume=resume,
                    status="committed_with_warning",
                )
                report["warnings"] = warnings
                return _emit_report(report, lancedb_dir=lancedb_dir)
        # Ordinary pre-commit failure: drop only the staging table
        # created/opened by this run, remove this run's checkpoint and
        # staging_state, and restore the embedding artifact (gpu_contract
        # already restores manifest/checksums). The active table and active
        # pointer bytes are never touched. Cleanup failures are attached as
        # notes and never mask the import exception.
        cleanup_errors: list[str] = []
        if staging_table is not None:
            try:
                _drop_staging_table(
                    lancedb_dir, staging_table.name, active_pointer_path,
                )
                _remove_if_exists(checkpoint_path)
            except Exception as cleanup_exc:  # noqa: BLE001 - preserve import error
                cleanup_errors.append(f"staging cleanup failed: {cleanup_exc}")
        if state_path is not None:
            try:
                _remove_if_exists(state_path)
            except Exception as cleanup_exc:  # noqa: BLE001 - preserve import error
                cleanup_errors.append(f"staging_state cleanup failed: {cleanup_exc}")
        if cleanup_errors:
            for note in cleanup_errors:
                try:
                    exc.add_note(note)
                except AttributeError:
                    pass
        raise
    # The active pointer is committed. From here on, failures are
    # committed_with_warning: no rollback, no drop of the active table.
    return _post_commit(
        lancedb_dir=lancedb_dir,
        args=args,
        final_manifest=final_manifest,
        table_name=staging_table.name,
        db_info=db_info,
        resume=resume,
        payload_before=payload_before,
        state_path=state_path,
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not (1 <= args.import_batch_size <= IMPORT_BATCH_SIZE):
        parser.error(f"--import-batch-size must be between 1 and {IMPORT_BATCH_SIZE}")
    if args.command == "preflight":
        return _preflight(args)
    return _execute(args, resume=bool(args.resume))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(
            "import interrupted; staging rows and checkpoint preserved. "
            "Resume with: execute --resume <same arguments>",
            file=sys.stderr,
        )
        raise SystemExit(130)
    except Exception as exc:  # noqa: BLE001 - operator CLI boundary
        print(f"import failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

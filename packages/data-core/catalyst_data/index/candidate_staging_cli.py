"""Pointer-free inactive candidate dense staging CLI core (Q-011).

Wraps ``catalyst_data.index.v1_staging.stage_dense`` so an operator can stage
an *inactive* dense candidate for the Q-011 corpus build without ever calling
``promote_v1_generation`` or creating/replacing/modifying active pointers.

The core verifies, before any mutation, that:
- the embedding artifact carries a valid ``IndexManifest`` whose model,
  dimension, corpus/source/snapshot/probe/postbuild identities and (when
  supplied) code revision are consistent;
- the derivative row for the candidate build is a lexical-ready candidate
  whose ``corpus_manifest.is_current=0``;
- the candidate LanceDB manifest directory does not alias a protected active
  generation path or active LanceDB directory;
- the optional source bundle verifies against the same identities.

``preflight`` performs only validation.  ``execute`` calls ``stage_dense`` and
confirms the active-generation pointer bytes are unchanged on success.
``stage_dense`` idempotent resume is preserved: an existing matching
``candidate_generation.json`` is returned without re-mutation.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from catalyst_data.config import BGE_M3_DIMENSION, BGE_M3_MODEL, BGE_M3_REVISION
from catalyst_data.retrieval.gpu_contract import verify_source_bundle
from catalyst_data.retrieval.index_manifest import IndexManifest

_HEX64 = "0123456789abcdef"


class CandidateDenseStagingError(RuntimeError):
    """Fail-closed operator error for candidate dense staging."""


@dataclass(frozen=True)
class CandidateDenseStagingInputs:
    derivative: Path
    build_id: str
    embedding_artifact_dir: Path
    candidate_manifest_dir: Path
    source_bundle: Path | None = None
    active_lancedb_dir: Path | None = None
    active_generation_pointer: Path | None = None
    expected_index_manifest_id: str | None = None
    code_revision: str | None = None


def _require_hex64(value: str, *, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        c not in _HEX64 for c in value
    ):
        raise CandidateDenseStagingError(f"{label} must be a lowercase SHA-256")
    return value


def _open_derivative_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise CandidateDenseStagingError(f"derivative DB not found: {db_path}")
    uri = f"{db_path.resolve().as_uri()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _load_artifact_manifest(artifact_dir: Path) -> IndexManifest:
    path = Path(artifact_dir) / "index_manifest.json"
    if not path.is_file():
        raise CandidateDenseStagingError(f"embedding artifact index_manifest.json missing: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise CandidateDenseStagingError("index_manifest.json must be an object")
    manifest = IndexManifest.from_dict(raw)
    for name, actual, expected in (
        ("model_name", manifest.model_name, BGE_M3_MODEL),
        ("model_revision", manifest.model_revision, BGE_M3_REVISION),
        ("dimension", manifest.dimension, BGE_M3_DIMENSION),
    ):
        if actual != expected:
            raise CandidateDenseStagingError(
                f"IndexManifest {name} mismatch: {actual!r} != {expected!r}"
            )
    return manifest


def _verify_derivative_candidate(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    corpus_manifest_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT is_current FROM corpus_manifest WHERE manifest_id=?",
        (corpus_manifest_id,),
    ).fetchone()
    if row is None:
        raise CandidateDenseStagingError(
            f"corpus_manifest {corpus_manifest_id} not found on derivative"
        )
    if int(row[0] or 0) != 0:
        raise CandidateDenseStagingError(
            f"corpus_manifest {corpus_manifest_id} is_current=1; "
            "candidate staging requires an inactive corpus candidate"
        )
    build = conn.execute(
        "SELECT status, lexical_ready, chunk_count, manifest_id "
        "FROM corpus_publication_builds WHERE build_id=?",
        (build_id,),
    ).fetchone()
    if build is None:
        raise CandidateDenseStagingError(f"candidate build {build_id} not found")
    status, lexical_ready, chunk_count, manifest_id = build
    if str(manifest_id or "") != corpus_manifest_id:
        raise CandidateDenseStagingError(
            f"candidate build {build_id} manifest_id {manifest_id!r} != "
            f"{corpus_manifest_id}"
        )
    if str(status or "") != "lexical_ready" and int(lexical_ready or 0) != 1:
        raise CandidateDenseStagingError(
            f"candidate build {build_id} is not lexical-ready "
            f"(status={status!r}, lexical_ready={lexical_ready!r})"
        )
    return {
        "status": status,
        "lexical_ready": int(lexical_ready or 0),
        "chunk_count": int(chunk_count or 0),
        "manifest_id": manifest_id,
    }


def _verify_expected_manifest_id(
    manifest: IndexManifest, expected_index_manifest_id: str | None
) -> None:
    if expected_index_manifest_id is not None:
        _require_hex64(expected_index_manifest_id, label="expected_index_manifest_id")
        if manifest.index_manifest_id != expected_index_manifest_id:
            raise CandidateDenseStagingError(
                "index manifest id mismatch: "
                f"loaded={manifest.index_manifest_id} "
                f"expected={expected_index_manifest_id}"
            )


def _verify_source_bundle_identity(
    manifest: IndexManifest,
    source_bundle: Path | None,
    *,
    expected_chunk_count: int,
) -> int:
    if source_bundle is None:
        return expected_chunk_count
    verified = verify_source_bundle(
        source_bundle,
        expected_source_bundle_id=manifest.source_bundle_id,
        expected_snapshot_id=manifest.snapshot_id,
        expected_corpus_manifest_id=manifest.corpus_manifest_id,
        expected_probe_report_id=manifest.probe_report_id,
        expected_postbuild_readiness_id=manifest.postbuild_readiness_id,
    )
    if verified.chunk_count != expected_chunk_count:
        raise CandidateDenseStagingError(
            "source bundle chunk_count mismatch: "
            f"bundle={verified.chunk_count} index={expected_chunk_count}"
        )
    return verified.chunk_count


def _protected_alias_error(label: str, left: Path, right: Path) -> str:
    return (
        f"{label}: {left} aliases protected active path {right}; "
        "candidate staging must be pointer-free"
    )


def _verify_no_active_alias(
    *,
    candidate_manifest_dir: Path,
    active_lancedb_dir: Path | None,
    active_generation_pointer: Path | None,
) -> None:
    candidate = candidate_manifest_dir.resolve()
    if active_lancedb_dir is not None:
        active = active_lancedb_dir.resolve()
        if candidate == active or candidate.is_relative_to(active) or active.is_relative_to(candidate):
            raise CandidateDenseStagingError(
                _protected_alias_error(
                    "candidate LanceDB manifest directory", candidate, active
                )
            )
    if active_generation_pointer is not None:
        pointer = active_generation_pointer.resolve()
        if candidate == pointer or pointer.is_relative_to(candidate):
            raise CandidateDenseStagingError(
                _protected_alias_error(
                    "candidate manifest directory", candidate, pointer
                )
            )


def _resolve_code_revision(code_revision: str | None) -> str | None:
    if code_revision is None:
        return None
    import subprocess

    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
    except Exception as exc:  # pragma: no cover - environment dependent
        raise CandidateDenseStagingError(
            "unable to resolve git HEAD for --code-revision"
        ) from exc
    if code_revision != head:
        raise CandidateDenseStagingError(
            f"--code-revision {code_revision} != git HEAD {head}"
        )
    return code_revision


def preflight_stage_candidate_dense(
    inputs: CandidateDenseStagingInputs,
) -> dict[str, Any]:
    """Validate identities and path safety without mutating anything."""
    _require_hex64(inputs.build_id, label="build_id")
    manifest = _load_artifact_manifest(inputs.embedding_artifact_dir)
    _verify_expected_manifest_id(manifest, inputs.expected_index_manifest_id)
    _verify_no_active_alias(
        candidate_manifest_dir=inputs.candidate_manifest_dir,
        active_lancedb_dir=inputs.active_lancedb_dir,
        active_generation_pointer=inputs.active_generation_pointer,
    )
    if inputs.code_revision is not None and inputs.code_revision != manifest.code_revision:
        raise CandidateDenseStagingError(
            "--code-revision must equal the embedding artifact code_revision"
        )
    _resolve_code_revision(inputs.code_revision)
    expected_chunk_count = manifest.vector_count
    conn = _open_derivative_readonly(inputs.derivative)
    try:
        build = _verify_derivative_candidate(
            conn, build_id=inputs.build_id, corpus_manifest_id=manifest.corpus_manifest_id
        )
    finally:
        conn.close()
    _verify_source_bundle_identity(
        manifest, inputs.source_bundle, expected_chunk_count=expected_chunk_count
    )
    return {
        "schema_version": "candidate_dense_staging_preflight_v1",
        "mode": "preflight",
        "derivative": str(inputs.derivative.resolve()),
        "build_id": inputs.build_id,
        "index_manifest_id": manifest.index_manifest_id,
        "corpus_manifest_id": manifest.corpus_manifest_id,
        "source_bundle_id": manifest.source_bundle_id,
        "expected_chunk_count": expected_chunk_count,
        "derivative_build": build,
        "code_revision": inputs.code_revision,
    }


def _read_pointer_bytes(pointer: Path | None) -> bytes | None:
    if pointer is None:
        return None
    if pointer.is_file():
        return pointer.read_bytes()
    return b""


def execute_stage_candidate_dense(
    inputs: CandidateDenseStagingInputs,
) -> dict[str, Any]:
    """Run pointer-free ``stage_dense`` for an inactive candidate build."""
    from catalyst_data.index.v1_staging import (
        InactiveDenseCandidate,
        stage_dense,
        validate_dense_candidate,
    )

    preflight = preflight_stage_candidate_dense(inputs)
    expected_chunk_count = preflight["expected_chunk_count"]
    pointer_before = _read_pointer_bytes(inputs.active_generation_pointer)
    active_dir_snapshot: dict[Path, bytes] = {}
    if inputs.active_lancedb_dir is not None:
        active_root = inputs.active_lancedb_dir.resolve()
        if active_root.is_dir():
            for path in sorted(active_root.rglob("*")):
                if path.is_file():
                    active_dir_snapshot[path] = path.read_bytes()

    manifest = _load_artifact_manifest(inputs.embedding_artifact_dir)
    conn = _open_derivative_readonly(inputs.derivative)
    try:
        candidate = stage_dense(
            inputs.candidate_manifest_dir,
            embedding_artifact_dir=inputs.embedding_artifact_dir,
            new_index_manifest=manifest,
            expected_chunk_count=expected_chunk_count,
            source_conn=conn,
            source_build_id=inputs.build_id,
        )
    finally:
        conn.close()

    if not isinstance(candidate, InactiveDenseCandidate):
        raise CandidateDenseStagingError("stage_dense returned an invalid candidate")
    validate_dense_candidate(candidate, expected_chunk_count=expected_chunk_count)

    # Pointer immutability on success: active-generation pointer bytes and the
    # active LanceDB directory content must be unchanged.
    if inputs.active_generation_pointer is not None:
        if _read_pointer_bytes(inputs.active_generation_pointer) != pointer_before:
            raise CandidateDenseStagingError(
                "active-generation pointer changed during candidate staging"
            )
    if inputs.active_lancedb_dir is not None:
        active_root = inputs.active_lancedb_dir.resolve()
        if active_root.is_dir():
            after = {
                path: path.read_bytes()
                for path in sorted(active_root.rglob("*"))
                if path.is_file()
            }
            if after != active_dir_snapshot:
                raise CandidateDenseStagingError(
                    "active LanceDB directory changed during candidate staging"
                )
    payload = json.loads(candidate.candidate_generation_path.read_text(encoding="utf-8"))
    return {
        "schema_version": "candidate_dense_staging_execute_v1",
        "mode": "execute",
        "status": payload.get("status"),
        "table_name": candidate.table_name,
        "index_manifest_id": candidate.index_manifest_id,
        "corpus_manifest_id": candidate.corpus_manifest_id,
        "source_bundle_id": candidate.source_bundle_id,
        "chunk_count": candidate.chunk_count,
        "candidate_generation_path": str(candidate.candidate_generation_path),
        "candidate_generation_sha256": hashlib.sha256(
            candidate.candidate_generation_path.read_bytes()
        ).hexdigest(),
    }


__all__ = [
    "CandidateDenseStagingError",
    "CandidateDenseStagingInputs",
    "execute_stage_candidate_dense",
    "preflight_stage_candidate_dense",
]

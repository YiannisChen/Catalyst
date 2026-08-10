"""Runtime index identity binding from actual execution files (AMEND-2 P2).

The production runner must verify identity from the real LanceDB directory
(active_generation.json, import_report.json), the clean import
index_manifest.json (validated through the production ``IndexManifest``
contract), the actually opened LanceDB table, and the frozen DB — against an
explicit ``ApprovedFrozenIdentities`` contract — before model load / embedding /
retrieval / artifact write. Hardcoded approved values are never treated as
verified facts; the approved contract is compared against actuals.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from catalyst_data.retrieval.git_revision import resolve_git_revision
from catalyst_data.retrieval.gpu_contract import validate_persisted_lancedb_table
from catalyst_data.retrieval.index_manifest import IndexManifest

HEX64 = frozenset("0123456789abcdef")


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and set(value) <= HEX64


def _is_hex40(value: str) -> bool:
    return len(value) == 40 and set(value) <= HEX64


def _load_json(path: Path, label: str) -> dict:
    if not path.is_file():
        raise ValueError(f"{label} missing: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} invalid: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return data


def _require_str(payload: dict, key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}.{key} missing or invalid")
    return value


def _require_hex64(payload: dict, key: str, label: str) -> str:
    value = _require_str(payload, key, label)
    if not _is_hex64(value):
        raise ValueError(f"{label}.{key} must be a 64-char SHA-256")
    return value


@dataclass(frozen=True)
class ApprovedFrozenIdentities:
    """Explicit approved identity contract for production execution."""

    snapshot_id: str
    corpus_manifest_id: str
    source_bundle_id: str
    probe_report_id: str
    postbuild_readiness_id: str
    index_manifest_id: str
    code_revision: str  # index build revision
    model_name: str
    model_revision: str
    tokenizer_revision: str
    dimension: int
    dtype: str
    normalization_mode: str
    table_name: str
    vector_count: int
    db_sha256: str
    db_user_version: int
    db_fk_violations: int

    def __post_init__(self) -> None:
        for name in (
            "snapshot_id", "corpus_manifest_id", "source_bundle_id",
            "probe_report_id", "postbuild_readiness_id", "index_manifest_id",
            "db_sha256",
        ):
            if not _is_hex64(getattr(self, name)):
                raise ValueError(f"approved {name} must be lowercase SHA-256")
        if not _is_hex40(self.code_revision):
            raise ValueError("approved code_revision must be a 40-char SHA")
        if self.dimension <= 0 or self.vector_count <= 0:
            raise ValueError("approved dimension/vector_count must be positive")
        if self.db_user_version < 0 or self.db_fk_violations < 0:
            raise ValueError("approved DB facts must be non-negative")


@dataclass(frozen=True)
class ResolvedRuntimeIdentity:
    lancedb_dir: Path
    active_table_name: str
    snapshot_id: str
    corpus_manifest_id: str
    source_bundle_id: str
    index_manifest_id: str
    probe_report_id: str
    postbuild_readiness_id: str
    code_revision: str  # index build revision from clean import index_manifest
    git_head: str  # runtime repository HEAD
    model_name: str
    model_revision: str
    tokenizer_revision: str
    dimension: int
    dtype: str
    normalization_mode: str
    vector_count: int
    db_path: Path
    db_sha256: str
    db_user_version: int
    db_foreign_key_violations: int
    lancedb_row_count: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_lancedb_schema(table: object, *, expected_dimension: int) -> None:
    """Validate the actual LanceDB table vector schema/dimension."""
    try:
        schema = table.schema
    except Exception as exc:  # pragma: no cover - defensive
        raise ValueError(f"cannot read LanceDB table schema: {exc}") from exc
    vector_field = None
    for field in schema:
        if field.name == "vector":
            vector_field = field
            break
    if vector_field is None:
        raise ValueError("LanceDB table schema missing vector field")
    vector_type = getattr(vector_field, "type", None)
    try:
        actual_dimension = int(vector_type.list_size)
    except Exception as exc:
        raise ValueError(
            f"LanceDB vector schema dimension mismatch: {vector_type}"
        ) from exc
    if actual_dimension != expected_dimension:
        raise ValueError(
            f"LanceDB vector schema dimension mismatch: {actual_dimension} != {expected_dimension}"
        )
    import pyarrow as pa

    value_type = getattr(vector_type, "value_type", None)
    if value_type is None or not pa.types.is_float32(value_type):
        raise ValueError(
            f"LanceDB vector value type must be float32: {value_type}"
        )


def resolve_runtime_identity(
    *,
    lancedb_dir: Path,
    index_manifest_path: Path,
    db_path: Path,
    repo_root: Path,
    expected: ApprovedFrozenIdentities,
    git_runner: Callable[..., str] | None = None,
    require_clean: bool = True,
) -> ResolvedRuntimeIdentity:
    """Load and cross-validate all execution identities before any model/artifact work."""
    lancedb_dir = Path(lancedb_dir)
    if not lancedb_dir.is_dir():
        raise ValueError(f"lancedb dir does not exist: {lancedb_dir}")

    pointer = _load_json(lancedb_dir / "active_generation.json", "active_generation")
    import_report = _load_json(lancedb_dir / "import_report.json", "import_report")
    index_manifest_raw = _load_json(index_manifest_path, "index_manifest")

    # --- active_generation pointer vs approved contract ---
    pointer_table = _require_str(pointer, "table_name", "active_generation")
    pointer_chunk_count = pointer.get("chunk_count")
    pointer_snapshot = _require_hex64(pointer, "snapshot_id", "active_generation")
    pointer_corpus = _require_hex64(pointer, "corpus_manifest_id", "active_generation")
    pointer_source = _require_hex64(pointer, "source_bundle_id", "active_generation")
    pointer_index = _require_hex64(pointer, "index_manifest_id", "active_generation")

    if pointer_table != expected.table_name:
        raise ValueError(
            f"active pointer table {pointer_table} != approved {expected.table_name}"
        )
    if not isinstance(pointer_chunk_count, int) or pointer_chunk_count != expected.vector_count:
        raise ValueError(
            f"active pointer chunk_count {pointer_chunk_count} != approved {expected.vector_count}"
        )
    if pointer_snapshot != expected.snapshot_id:
        raise ValueError("active pointer snapshot_id mismatch")
    if pointer_corpus != expected.corpus_manifest_id:
        raise ValueError("active pointer corpus_manifest_id mismatch")
    if pointer_source != expected.source_bundle_id:
        raise ValueError("active pointer source_bundle_id mismatch")
    if pointer_index != expected.index_manifest_id:
        raise ValueError("active pointer index_manifest_id mismatch")

    # --- import_report vs approved contract + DB facts ---
    if import_report.get("status") != "committed" or import_report.get("ok") is not True:
        raise ValueError("import_report is not committed/ok")
    if import_report.get("table_name") != pointer_table:
        raise ValueError("import_report table_name does not match active pointer")
    if import_report.get("chunk_count") != pointer_chunk_count:
        raise ValueError("import_report chunk_count does not match active pointer")
    if import_report.get("chunk_count") != expected.vector_count:
        raise ValueError(
            f"import_report chunk_count {import_report.get('chunk_count')} != approved {expected.vector_count}"
        )
    for key, label in (
        ("corpus_manifest_id", "corpus_manifest_id"),
        ("index_manifest_id", "index_manifest_id"),
        ("snapshot_id", "snapshot_id"),
        ("source_bundle_id", "source_bundle_id"),
    ):
        if import_report.get(key) != pointer.get(key):
            raise ValueError(f"import_report {label} mismatch")
    probe_report_id = _require_hex64(import_report, "probe_report_id", "import_report")
    postbuild_readiness_id = _require_hex64(
        import_report, "postbuild_readiness_id", "import_report"
    )
    if probe_report_id != expected.probe_report_id:
        raise ValueError("import_report probe_report_id mismatch")
    if postbuild_readiness_id != expected.postbuild_readiness_id:
        raise ValueError("import_report postbuild_readiness_id mismatch")

    import_db = import_report.get("db")
    if not isinstance(import_db, dict):
        raise ValueError("import_report.db missing")
    if import_db.get("current_manifest_id") != expected.corpus_manifest_id:
        raise ValueError("import_report.db.current_manifest_id mismatch")
    if import_db.get("user_version") != expected.db_user_version:
        raise ValueError(
            f"import_report.db.user_version mismatch: {import_db.get('user_version')}"
        )
    if import_db.get("foreign_key_violations") != expected.db_fk_violations:
        raise ValueError("import_report.db.foreign_key_violations mismatch")
    if import_db.get("integrity") != "ok":
        raise ValueError("import_report.db.integrity is not ok")

    # --- clean import index_manifest via production IndexManifest validation ---
    manifest = IndexManifest.from_dict(index_manifest_raw)
    if manifest.code_revision != expected.code_revision:
        raise ValueError("index_manifest.code_revision does not match approved index build revision")
    if manifest.index_manifest_id != expected.index_manifest_id:
        raise ValueError("index_manifest.index_manifest_id does not match approved")
    manifest.assert_approved_identities(
        source_bundle_id=expected.source_bundle_id,
        snapshot_id=expected.snapshot_id,
        corpus_manifest_id=expected.corpus_manifest_id,
        probe_report_id=expected.probe_report_id,
        postbuild_readiness_id=expected.postbuild_readiness_id,
    )
    if manifest.vector_count != expected.vector_count:
        raise ValueError("index_manifest.vector_count does not match approved")
    code_revision = manifest.code_revision
    model_name = manifest.model_name
    model_revision = manifest.model_revision
    tokenizer_revision = manifest.tokenizer_revision
    dimension = manifest.dimension
    dtype = manifest.dtype
    normalization_mode = manifest.normalization_mode
    vector_count = manifest.vector_count
    index_manifest_id = manifest.index_manifest_id

    # --- Frozen DB identity ---
    db_path = Path(db_path)
    db_sha256 = _sha256_file(db_path)
    if db_sha256 != expected.db_sha256:
        raise ValueError("frozen DB sha256 mismatch")
    uri = f"{db_path.resolve().as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        db_user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        conn.close()
    if db_user_version != expected.db_user_version:
        raise ValueError(f"frozen DB user_version mismatch: {db_user_version}")
    if fk_violations != expected.db_fk_violations:
        raise ValueError(
            f"frozen DB foreign_key violations {fk_violations} != approved {expected.db_fk_violations}"
        )

    # --- Actual LanceDB table ---
    import lancedb

    lancedb_db = lancedb.connect(str(lancedb_dir))
    actual_table_names = list(lancedb_db.list_tables().tables)
    if pointer_table not in actual_table_names:
        raise ValueError(f"active table {pointer_table} not found in lancedb dir")
    table = lancedb_db.open_table(pointer_table)
    lancedb_row_count = int(table.count_rows())
    if lancedb_row_count != expected.vector_count:
        raise ValueError(
            f"actual LanceDB row count {lancedb_row_count} != approved {expected.vector_count}"
        )
    if lancedb_row_count != vector_count:
        raise ValueError("actual LanceDB row count does not match index_manifest vector_count")
    _validate_lancedb_schema(table, expected_dimension=expected.dimension)
    validate_persisted_lancedb_table(
        table,
        chunk_ids_path=index_manifest_path.parent / "chunk_ids.json",
        chunk_count=vector_count,
        dimension=dimension,
        index_manifest_id=index_manifest_id,
        expected_identities={
            "corpus_manifest_id": manifest.corpus_manifest_id,
            "source_bundle_id": manifest.source_bundle_id,
            "snapshot_id": manifest.snapshot_id,
            "probe_report_id": manifest.probe_report_id,
            "postbuild_readiness_id": manifest.postbuild_readiness_id,
        },
        expected_hash=manifest.artifact_hashes["lancedb_table"],
    )

    # --- Runtime git HEAD (dirty worktree fails closed for production execution;
    # preparation evidence may record the actual HEAD without a clean gate). ---
    git_head = resolve_git_revision(
        repo_root=repo_root,
        require_clean=require_clean,
        git_runner=git_runner,
    )

    return ResolvedRuntimeIdentity(
        lancedb_dir=lancedb_dir.resolve(),
        active_table_name=pointer_table,
        snapshot_id=pointer_snapshot,
        corpus_manifest_id=pointer_corpus,
        source_bundle_id=pointer_source,
        index_manifest_id=index_manifest_id,
        probe_report_id=probe_report_id,
        postbuild_readiness_id=postbuild_readiness_id,
        code_revision=code_revision,
        git_head=git_head,
        model_name=model_name,
        model_revision=model_revision,
        tokenizer_revision=tokenizer_revision,
        dimension=dimension,
        dtype=dtype,
        normalization_mode=normalization_mode,
        vector_count=vector_count,
        db_path=db_path.resolve(),
        db_sha256=db_sha256,
        db_user_version=db_user_version,
        db_foreign_key_violations=fk_violations,
        lancedb_row_count=lancedb_row_count,
    )


__all__ = [
    "ApprovedFrozenIdentities", "ResolvedRuntimeIdentity", "resolve_runtime_identity",
]

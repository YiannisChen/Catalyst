"""AMEND-2 P2: runtime index identity bound to approved frozen identities.

``resolve_runtime_identity`` must validate the three identity files AND every
field against an explicit ``ApprovedFrozenIdentities`` contract, reuse the
production ``IndexManifest`` validation, verify import_report DB facts, actual
DB FK violations, LanceDB vector schema/dimension, and table identity columns.
Failures must occur before model load and artifact writes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from catalyst_eval.post_import.index_identity import (
    ApprovedFrozenIdentities,
    resolve_runtime_identity,
)

POINTER = {
    "chunk_count": 6,
    "corpus_manifest_id": "3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc",
    "index_manifest_id": "61c9876760882478f86a919376a63f663fec0c9a7fafc30d8f6c93db14d9988c",
    "schema_version": "active_generation_v1",
    "snapshot_id": "7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49",
    "source_bundle_id": "8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2",
    "table_name": "chunks__staging__b3761f4b943542a8",
}
IMPORT = {
    "chunk_count": 6,
    "corpus_manifest_id": POINTER["corpus_manifest_id"],
    "db": {"current_manifest_id": POINTER["corpus_manifest_id"], "foreign_key_violations": 0, "integrity": "ok", "user_version": 13},
    "index_manifest_id": POINTER["index_manifest_id"],
    "ok": True,
    "postbuild_readiness_id": "9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b",
    "probe_report_id": "25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23",
    "schema_version": "lancedb_import_report_v1",
    "snapshot_id": POINTER["snapshot_id"],
    "source_bundle_id": POINTER["source_bundle_id"],
    "status": "committed",
    "table_name": POINTER["table_name"],
}
INDEX_MANIFEST = {
    "artifact_hashes": {
        "chunk_ids.json": "c1d4938560714eadca90e4d42019414606fa36c34481e1d0891abce7ae840231",
        "lancedb_table": "0fcddf2ba84f2868327ce802b0677ac22826fe29327c645a02408352176e4c21",
        "vectors.npy": "c5445312b985529cdb40332fd8f18683e05667f3e555779ac4b5b1d2def7e49a",
    },
    "artifact_state": "lancedb_imported",
    "code_revision": "bb43ebe20f29a13ef426e0a1a7c3aefc6d15ffd8",
    "corpus_manifest_id": POINTER["corpus_manifest_id"],
    "dimension": 1024,
    "dtype": "float32",
    "index_manifest_id": POINTER["index_manifest_id"],
    "model_name": "BAAI/bge-m3",
    "model_revision": "5617a9f61b028005a4858fdac845db406aefb181",
    "normalization_mode": "l2",
    "postbuild_readiness_id": IMPORT["postbuild_readiness_id"],
    "probe_report_id": IMPORT["probe_report_id"],
    "schema_version": "1.0.0",
    "snapshot_id": POINTER["snapshot_id"],
    "source_bundle_id": POINTER["source_bundle_id"],
    "tokenizer_revision": "5617a9f61b028005a4858fdac845db406aefb181",
    "vector_count": 6,
}

APPROVED = ApprovedFrozenIdentities(
    snapshot_id=POINTER["snapshot_id"],
    corpus_manifest_id=POINTER["corpus_manifest_id"],
    source_bundle_id=POINTER["source_bundle_id"],
    probe_report_id=IMPORT["probe_report_id"],
    postbuild_readiness_id=IMPORT["postbuild_readiness_id"],
    index_manifest_id=POINTER["index_manifest_id"],
    code_revision=INDEX_MANIFEST["code_revision"],
    model_name="BAAI/bge-m3",
    model_revision="5617a9f61b028005a4858fdac845db406aefb181",
    tokenizer_revision="5617a9f61b028005a4858fdac845db406aefb181",
    dimension=1024,
    dtype="float32",
    normalization_mode="l2",
    table_name=POINTER["table_name"],
    vector_count=6,
    db_sha256="0" * 64,  # replaced with the actual fake-DB sha inside _call()
    db_user_version=13,
    db_fk_violations=0,
)


def _canonical_digest(rows: list[dict]) -> str:
    """Fixture-side canonical digest matching the persisted-table hash contract."""
    payload = [
        {key: value for key, value in row.items() if key != "index_manifest_id"}
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _fixture_schema(*, vector_dim: int = 1024, vector_value_type: str = "float32"):
    import pyarrow as pa

    from catalyst_data.retrieval.gpu_contract import production_lancedb_schema

    schema = production_lancedb_schema()
    pa_type = pa.float32() if vector_value_type == "float32" else pa.float64()
    fields = [field for field in schema if field.name != "vector"]
    fields.append(pa.field("vector", pa.list_(pa_type, vector_dim), nullable=False))
    return pa.schema(fields)


def _write_lancedb_identity_files(
    tmp_path: Path,
    *,
    pointer=None,
    import_report=None,
    index_manifest=None,
    with_table: bool = True,
    vector_dim: int = 1024,
    vector_value_type: str = "float32",
    chunk_ids: list[str] | None = None,
    table_hash_override: str | None = None,
    row_identity_override: dict | None = None,
) -> Path:
    import lancedb

    lancedb_dir = tmp_path / "gold"
    lancedb_dir.mkdir(exist_ok=True)
    pointer = pointer or POINTER
    import_report = import_report or IMPORT
    manifest_raw = json.loads(json.dumps(index_manifest or INDEX_MANIFEST))
    (lancedb_dir / "active_generation.json").write_text(json.dumps(pointer, sort_keys=True))
    (lancedb_dir / "import_report.json").write_text(json.dumps(import_report, sort_keys=True))
    if with_table:
        db = lancedb.connect(str(lancedb_dir))
        rows = []
        for idx in range(6):
            row = {
                "chunk_id": f"c{idx}",
                "document_id": f"doc{idx}",
                "content_text": "text",
                "content_hash": "a" * 64,
                "metadata_hash": "b" * 64,
                "available_at": "2025-01-01T00:00:00Z",
                "ticker_associations": ["AAPL"],
                "source_class": "reported_news",
                "chunk_profile_version": "news_v2",
                "status": "active",
                "eligibility": "eligible",
                "dedup_cluster_id": None,
                "cluster_first_available_at": None,
                "representative_document_id": None,
                "corpus_manifest_id": pointer["corpus_manifest_id"],
                "source_bundle_id": pointer["source_bundle_id"],
                "snapshot_id": pointer["snapshot_id"],
                "probe_report_id": import_report["probe_report_id"],
                "postbuild_readiness_id": import_report["postbuild_readiness_id"],
                "index_manifest_id": pointer["index_manifest_id"],
                "vector": [0.0] * vector_dim,
            }
            if row_identity_override:
                row.update(row_identity_override)
            rows.append(row)
        ids = chunk_ids if chunk_ids is not None else [f"c{idx}" for idx in range(6)]
        (lancedb_dir / "chunk_ids.json").write_text(
            json.dumps(ids, separators=(",", ":")) + "\n"
        )
        manifest_raw.setdefault("artifact_hashes", {})
        if table_hash_override is not None:
            manifest_raw["artifact_hashes"]["lancedb_table"] = table_hash_override
        else:
            manifest_raw["artifact_hashes"]["lancedb_table"] = _canonical_digest(rows)
        from catalyst_data.retrieval.index_manifest import IndexManifest
        if index_manifest is None:
            raw_for_id = {key: value for key, value in manifest_raw.items()
                          if key != "index_manifest_id"}
            manifest_id = IndexManifest.from_dict(raw_for_id).index_manifest_id
            manifest_raw["index_manifest_id"] = manifest_id
        else:
            try:
                raw_for_id = {key: value for key, value in manifest_raw.items()
                              if key != "index_manifest_id"}
                manifest_id = IndexManifest.from_dict(raw_for_id).index_manifest_id
            except (ValueError, TypeError):
                manifest_id = manifest_raw.get("index_manifest_id")
        if pointer is None:
            pointer = {**POINTER, "index_manifest_id": manifest_id}
        elif pointer.get("index_manifest_id") == POINTER["index_manifest_id"]:
            pointer = {**pointer, "index_manifest_id": manifest_id}
        if import_report is None:
            import_report = {**IMPORT, "index_manifest_id": manifest_id}
        elif import_report.get("index_manifest_id") == IMPORT["index_manifest_id"]:
            import_report = {**import_report, "index_manifest_id": manifest_id}
        for row in rows:
            row["index_manifest_id"] = manifest_id
        table = db.create_table(
            POINTER["table_name"], data=[], schema=_fixture_schema(
                vector_dim=vector_dim, vector_value_type=vector_value_type,
            ),
        )
        table.add(rows)
    (lancedb_dir / "active_generation.json").write_text(json.dumps(pointer, sort_keys=True))
    (lancedb_dir / "import_report.json").write_text(json.dumps(import_report, sort_keys=True))
    (lancedb_dir / "index_manifest.json").write_text(json.dumps(manifest_raw, sort_keys=True))
    return lancedb_dir


def _write_fake_db(tmp_path: Path, *, fk_violations: bool = False) -> Path:
    import sqlite3

    db = tmp_path / "frozen.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 13")
    if fk_violations:
        conn.executescript(
            """
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (id INTEGER PRIMARY KEY, pid INTEGER REFERENCES parent(id));
            INSERT INTO child (id, pid) VALUES (1, 999);
            """
        )
    else:
        conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    return db


def _git_runner(*args, cwd):
    if args[0] == "rev-parse":
        return "8dd9ee9b5f04e848e3d8248dad6470189af79573\n"
    if args[0] == "status":
        return ""
    raise AssertionError(f"unexpected git args: {args}")


def _fixture_manifest_id(tmp_path: Path) -> str:
    from catalyst_data.retrieval.index_manifest import IndexManifest

    raw = json.loads((tmp_path / "gold" / "index_manifest.json").read_text())
    try:
        return IndexManifest.from_dict(raw).index_manifest_id
    except (ValueError, TypeError):
        return raw.get("index_manifest_id") or APPROVED.index_manifest_id


def _call(tmp_path, *, expected=None, **kwargs):
    db_path = kwargs.pop("db_path", _write_fake_db(tmp_path))
    if expected is None:
        # Bind the approved contract to the actual fake-DB SHA and the
        # fixture's actual (recomputed) index manifest ID.
        expected = ApprovedFrozenIdentities(**{
            **APPROVED.__dict__,
            "index_manifest_id": _fixture_manifest_id(tmp_path),
            "db_sha256": hashlib.sha256(Path(db_path).read_bytes()).hexdigest(),
        })
    defaults = dict(
        lancedb_dir=tmp_path / "gold",
        index_manifest_path=tmp_path / "gold" / "index_manifest.json",
        db_path=db_path,
        repo_root=tmp_path,
        expected=expected,
        git_runner=_git_runner,
    )
    defaults.update(kwargs)
    return resolve_runtime_identity(**defaults)


def test_resolve_runtime_identity_reads_actual_files(tmp_path, monkeypatch):
    _write_lancedb_identity_files(tmp_path)
    resolved = _call(tmp_path)
    assert resolved.active_table_name == POINTER["table_name"]
    assert resolved.corpus_manifest_id == POINTER["corpus_manifest_id"]
    assert resolved.index_manifest_id == _fixture_manifest_id(tmp_path)
    assert resolved.snapshot_id == POINTER["snapshot_id"]
    assert resolved.source_bundle_id == POINTER["source_bundle_id"]
    assert resolved.probe_report_id == IMPORT["probe_report_id"]
    assert resolved.postbuild_readiness_id == IMPORT["postbuild_readiness_id"]
    assert resolved.code_revision == INDEX_MANIFEST["code_revision"]
    assert resolved.git_head == "8dd9ee9b5f04e848e3d8248dad6470189af79573"
    assert resolved.model_name == "BAAI/bge-m3"
    assert resolved.model_revision == "5617a9f61b028005a4858fdac845db406aefb181"
    assert resolved.tokenizer_revision == "5617a9f61b028005a4858fdac845db406aefb181"
    assert resolved.dimension == 1024
    assert resolved.dtype == "float32"
    assert resolved.normalization_mode == "l2"
    assert resolved.vector_count == 6
    assert resolved.lancedb_row_count == 6


def test_resolve_runtime_identity_fails_on_missing_lancedb_dir(tmp_path):
    db = _write_fake_db(tmp_path)
    with pytest.raises(ValueError, match="lancedb"):
        resolve_runtime_identity(
            lancedb_dir=tmp_path / "missing",
            index_manifest_path=tmp_path / "missing" / "index_manifest.json",
            db_path=db,
            repo_root=tmp_path,
            expected=APPROVED,
            git_runner=_git_runner,
        )


def test_resolve_runtime_identity_fails_on_missing_active_pointer(tmp_path):
    lancedb_dir = tmp_path / "gold"
    lancedb_dir.mkdir()
    db = _write_fake_db(tmp_path)
    with pytest.raises(ValueError, match="active_generation"):
        resolve_runtime_identity(
            lancedb_dir=lancedb_dir,
            index_manifest_path=lancedb_dir / "index_manifest.json",
            db_path=db,
            repo_root=tmp_path,
            expected=APPROVED,
            git_runner=_git_runner,
        )


def test_resolve_runtime_identity_fails_on_pointer_import_table_mismatch(tmp_path):
    bad_import = dict(IMPORT)
    bad_import["table_name"] = "different_table"
    _write_lancedb_identity_files(tmp_path, import_report=bad_import)
    with pytest.raises(ValueError, match="table"):
        _call(tmp_path)


def test_resolve_runtime_identity_fails_on_index_manifest_mismatch(tmp_path):
    bad_manifest = dict(INDEX_MANIFEST)
    bad_manifest["index_manifest_id"] = "f" * 64
    _write_lancedb_identity_files(tmp_path, index_manifest=bad_manifest)
    with pytest.raises(ValueError, match="index_manifest_id"):
        _call(tmp_path)


def test_resolve_runtime_identity_fails_on_db_sha_mismatch(tmp_path):
    _write_lancedb_identity_files(tmp_path)
    with pytest.raises(ValueError, match="sha"):
        _call(
            tmp_path,
            expected=ApprovedFrozenIdentities(**{
                **APPROVED.__dict__,
                "index_manifest_id": _fixture_manifest_id(tmp_path),
                "db_sha256": "1" * 64,
            }),
        )


def test_resolve_runtime_identity_fails_on_dirty_worktree(tmp_path):
    _write_lancedb_identity_files(tmp_path)
    db = _write_fake_db(tmp_path)

    def dirty_runner(*args, cwd):
        if args[0] == "rev-parse":
            return "8dd9ee9b5f04e848e3d8248dad6470189af79573\n"
        if args[0] == "status":
            return "M file.py\n"
        raise AssertionError(f"unexpected git args: {args}")

    expected = ApprovedFrozenIdentities(**{
        **APPROVED.__dict__,
        "index_manifest_id": _fixture_manifest_id(tmp_path),
        "db_sha256": hashlib.sha256(db.read_bytes()).hexdigest(),
    })
    with pytest.raises(RuntimeError, match="dirty"):
        resolve_runtime_identity(
            lancedb_dir=tmp_path / "gold",
            index_manifest_path=tmp_path / "gold" / "index_manifest.json",
            db_path=db,
            repo_root=tmp_path,
            expected=expected,
            git_runner=dirty_runner,
        )


# ---------------------------------------------------------------------------
# AMEND-2 P2: approved frozen identity contract
# ---------------------------------------------------------------------------


def _tamper_expected(field, value):
    return ApprovedFrozenIdentities(**{**APPROVED.__dict__, field: value})


def test_synced_tamper_of_all_three_identity_files_still_fails(tmp_path):
    """All three files tampered consistently must still fail (approved contract)."""
    new_corpus = "e" * 64
    pointer = dict(POINTER)
    pointer["corpus_manifest_id"] = new_corpus
    imp = dict(IMPORT)
    imp["corpus_manifest_id"] = new_corpus
    imp["db"] = dict(imp["db"])
    imp["db"]["current_manifest_id"] = new_corpus
    manifest = dict(INDEX_MANIFEST)
    manifest["corpus_manifest_id"] = new_corpus
    _write_lancedb_identity_files(tmp_path, pointer=pointer, import_report=imp, index_manifest=manifest)
    with pytest.raises(ValueError, match="corpus_manifest_id"):
        _call(tmp_path)


@pytest.mark.parametrize("field,value", [
    ("model_name", "BAAI/bge-large"),
    ("model_revision", "0" * 40),
    ("tokenizer_revision", "0" * 40),
    ("dimension", 512),
    ("dtype", "float64"),
    ("normalization_mode", "none"),
])
def test_wrong_index_contract_fields_fail(tmp_path, field, value):
    manifest = dict(INDEX_MANIFEST)
    manifest[field] = value
    _write_lancedb_identity_files(tmp_path, index_manifest=manifest)
    with pytest.raises(ValueError):
        _call(tmp_path)


@pytest.mark.parametrize("field", [
    "snapshot_id", "corpus_manifest_id", "source_bundle_id", "index_manifest_id",
])
def test_wrong_frozen_id_in_pointer_fails(tmp_path, field):
    pointer = dict(POINTER)
    pointer[field] = "e" * 64
    _write_lancedb_identity_files(tmp_path, pointer=pointer)
    with pytest.raises(ValueError, match=field):
        _call(tmp_path)


@pytest.mark.parametrize("field", ["probe_report_id", "postbuild_readiness_id"])
def test_wrong_frozen_id_in_import_report_fails(tmp_path, field):
    imp = dict(IMPORT)
    imp[field] = "e" * 64
    _write_lancedb_identity_files(tmp_path, import_report=imp)
    with pytest.raises(ValueError, match=field):
        _call(tmp_path)


def test_wrong_index_build_revision_fails(tmp_path):
    manifest = dict(INDEX_MANIFEST)
    manifest["code_revision"] = "0" * 40
    manifest.pop("index_manifest_id", None)
    _write_lancedb_identity_files(tmp_path, index_manifest=manifest)
    with pytest.raises(ValueError, match="code_revision|revision"):
        _call(tmp_path)


def test_wrong_import_report_db_facts_fail(tmp_path):
    imp = dict(IMPORT)
    imp["db"] = dict(imp["db"])
    imp["db"]["user_version"] = 12
    _write_lancedb_identity_files(tmp_path, import_report=imp)
    with pytest.raises(ValueError, match="user_version"):
        _call(tmp_path)


def test_wrong_import_report_current_manifest_fails(tmp_path):
    imp = dict(IMPORT)
    imp["db"] = dict(imp["db"])
    imp["db"]["current_manifest_id"] = "e" * 64
    _write_lancedb_identity_files(tmp_path, import_report=imp)
    with pytest.raises(ValueError, match="current_manifest_id"):
        _call(tmp_path)


def test_wrong_import_report_integrity_fails(tmp_path):
    imp = dict(IMPORT)
    imp["db"] = dict(imp["db"])
    imp["db"]["integrity"] = "corrupt"
    _write_lancedb_identity_files(tmp_path, import_report=imp)
    with pytest.raises(ValueError, match="integrity"):
        _call(tmp_path)


def test_actual_fk_violations_nonzero_fails(tmp_path):
    _write_lancedb_identity_files(tmp_path)
    db = _write_fake_db(tmp_path, fk_violations=True)
    expected = ApprovedFrozenIdentities(**{
        **APPROVED.__dict__,
        "index_manifest_id": _fixture_manifest_id(tmp_path),
        "db_sha256": hashlib.sha256(db.read_bytes()).hexdigest(),
    })
    with pytest.raises(ValueError, match="foreign_key|FK"):
        resolve_runtime_identity(
            lancedb_dir=tmp_path / "gold",
            index_manifest_path=tmp_path / "gold" / "index_manifest.json",
            db_path=db,
            repo_root=tmp_path,
            expected=expected,
            git_runner=_git_runner,
        )


def test_lancedb_vector_schema_dimension_mismatch_fails(tmp_path):
    _write_lancedb_identity_files(tmp_path, vector_dim=512)
    with pytest.raises(ValueError, match="dimension|vector"):
        _call(tmp_path)


# ---------------------------------------------------------------------------
# AMEND-3: runtime table binding (chunk counts, float32 vector type,
# persisted artifact hash, chunk_ids order, per-row identity columns)
# ---------------------------------------------------------------------------


def test_pointer_chunk_count_mismatch_approved_vector_count_fails(tmp_path):
    pointer = dict(POINTER)
    pointer["chunk_count"] = 5
    _write_lancedb_identity_files(tmp_path, pointer=pointer)
    with pytest.raises(ValueError, match="chunk_count|vector_count"):
        _call(tmp_path)


def test_import_report_chunk_count_mismatch_fails(tmp_path):
    imp = dict(IMPORT)
    imp["chunk_count"] = 5
    _write_lancedb_identity_files(tmp_path, import_report=imp)
    with pytest.raises(ValueError, match="chunk_count"):
        _call(tmp_path)


def test_lancedb_vector_value_type_mismatch_fails(tmp_path):
    _write_lancedb_identity_files(tmp_path, vector_value_type="float64")
    with pytest.raises(ValueError, match="float32|value type|vector"):
        _call(tmp_path)


def test_persisted_table_hash_mismatch_fails(tmp_path):
    _write_lancedb_identity_files(tmp_path, table_hash_override="f" * 64)
    with pytest.raises(ValueError, match="hash"):
        _call(tmp_path)


def test_persisted_chunk_ids_order_drift_fails(tmp_path):
    _write_lancedb_identity_files(tmp_path, chunk_ids=["c1", "c0", "c2", "c3", "c4", "c5"])
    with pytest.raises(ValueError, match="order|drift|chunk_id"):
        _call(tmp_path)


def test_persisted_row_identity_column_mismatch_fails(tmp_path):
    _write_lancedb_identity_files(
        tmp_path, row_identity_override={"corpus_manifest_id": "e" * 64},
    )
    with pytest.raises(ValueError, match="corpus_manifest_id"):
        _call(tmp_path)


def test_missing_chunk_ids_file_fails(tmp_path):
    _write_lancedb_identity_files(tmp_path)
    (tmp_path / "gold" / "chunk_ids.json").unlink()
    with pytest.raises((ValueError, OSError), match="chunk_ids|missing|No such file"):
        _call(tmp_path)


def test_dirty_git_fails_before_lancedb_or_db_scan(tmp_path, monkeypatch):
    """Dirty Git must fail before any LanceDB open or DB SHA scan."""
    import sys
    import types

    import catalyst_eval.post_import.index_identity as index_identity

    _write_lancedb_identity_files(tmp_path)
    db = _write_fake_db(tmp_path)
    expected = ApprovedFrozenIdentities(**{
        **APPROVED.__dict__,
        "index_manifest_id": _fixture_manifest_id(tmp_path),
        "db_sha256": hashlib.sha256(Path(db).read_bytes()).hexdigest(),
    })
    called = {"sha256_file": False, "lancedb_connect": False}
    expected_db_sha = hashlib.sha256(Path(db).read_bytes()).hexdigest()

    def record_sha(path):
        called["sha256_file"] = True
        return expected_db_sha

    monkeypatch.setattr(index_identity, "_sha256_file", record_sha)
    fake_lancedb = types.ModuleType("lancedb")

    def bad_connect(*args, **kwargs):
        called["lancedb_connect"] = True
        raise AssertionError("lancedb.connect must not run on a dirty tree")

    fake_lancedb.connect = bad_connect
    monkeypatch.setitem(sys.modules, "lancedb", fake_lancedb)

    def dirty_runner(*args, cwd):
        if args[0] == "rev-parse":
            return "8dd9ee9b5f04e848e3d8248dad6470189af79573\n"
        if args[0] == "status":
            return "M file.py\n"
        raise AssertionError(f"unexpected git args: {args}")

    with pytest.raises(RuntimeError, match="dirty"):
        resolve_runtime_identity(
            lancedb_dir=tmp_path / "gold",
            index_manifest_path=tmp_path / "gold" / "index_manifest.json",
            db_path=db,
            repo_root=tmp_path,
            expected=expected,
            git_runner=dirty_runner,
        )
    assert called["sha256_file"] is False
    assert called["lancedb_connect"] is False

"""M3-10: inactive dense staging from fixture vectors (never GPU)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from catalyst_data.config import BGE_M3_DIMENSION, BGE_M3_MODEL, BGE_M3_REVISION
from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION
from catalyst_data.retrieval.index_manifest import IndexManifest


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_dir(tmp_path: Path, *, count: int = 2) -> tuple[Path, list[str], np.ndarray]:
    chunk_ids = [f"chunk:{index:04d}" for index in range(count)]
    vectors = np.zeros((count, BGE_M3_DIMENSION), dtype=np.float32)
    for index in range(count):
        vectors[index, index] = 1.0
    root = tmp_path / "embedding-artifact"
    root.mkdir()
    np.save(root / "vectors.npy", vectors)
    (root / "chunk_ids.json").write_text(json.dumps(chunk_ids), encoding="utf-8")
    texts = [f"fixture {index}" for index in range(count)]
    lines = []
    for chunk_id, text in zip(chunk_ids, texts):
        record = {
            "chunk_id": chunk_id,
            "document_id": f"doc:{chunk_id}",
            "content_text": text,
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "metadata_hash": "b" * 64,
            "available_at": "2026-08-01T00:00:00Z",
            "ticker_associations": '["AAPL"]',
            "corpus_manifest_id": "c" * 64,
            "chunk_profile_version": "news_v2",
        }
        lines.append(json.dumps(record, sort_keys=True, separators=(",", ":")))
    (root / "chunks.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    checksums = {
        "vectors.npy": _sha(root / "vectors.npy"),
        "chunk_ids.json": _sha(root / "chunk_ids.json"),
    }
    (root / "checksums.sha256").write_text(
        "\n".join(f"{value}  {name}" for name, value in sorted(checksums.items())) + "\n",
        encoding="utf-8",
    )
    return root, chunk_ids, vectors


def _manifest(artifact: Path, *, vector_count: int, source_bundle_id: str = "1" * 64) -> IndexManifest:
    hashes = {
        "vectors.npy": _sha(artifact / "vectors.npy"),
        "chunk_ids.json": _sha(artifact / "chunk_ids.json"),
        "lancedb_table": "c" * 64,
    }
    return IndexManifest(
        model_name=BGE_M3_MODEL,
        model_revision=BGE_M3_REVISION,
        tokenizer_revision=TOKENIZER_REVISION,
        normalization_mode="l2",
        dtype="float32",
        dimension=BGE_M3_DIMENSION,
        corpus_manifest_id="c" * 64,
        source_bundle_id=source_bundle_id,
        snapshot_id="6" * 64,
        probe_report_id="7" * 64,
        postbuild_readiness_id="8" * 64,
        artifact_hashes=hashes,
        code_revision="a" * 40,
        vector_count=vector_count,
        artifact_state="vectors_staged",
    )


def test_stage_dense_missing_api():
    from catalyst_data.index.v1_staging import (
        InactiveDenseCandidate,
        stage_dense,
        validate_dense_candidate,
    )

    assert callable(stage_dense)
    assert callable(validate_dense_candidate)
    assert InactiveDenseCandidate is not None


def test_stage_dense_writes_inactive_candidate_and_preserves_active(tmp_path):
    from catalyst_data.index.v1_staging import stage_dense, validate_dense_candidate

    artifact, _chunk_ids, _vectors = _artifact_dir(tmp_path)
    source_bundle_id = "1" * 64
    manifest = _manifest(artifact, vector_count=2, source_bundle_id=source_bundle_id)
    gold = tmp_path / "lancedb_gold" / source_bundle_id
    gold.mkdir(parents=True)
    active = gold / "active_generation.json"
    active.write_text(
        json.dumps({"schema_version": "active_generation_v1", "table_name": "live_table"}),
        encoding="utf-8",
    )
    before = active.read_bytes()
    import sqlite3

    db, build_id = _db_for_artifact(tmp_path, chunk_ids=_chunk_ids)
    source_conn = sqlite3.connect(db)
    manifest_dir = gold / "candidates" / manifest.index_manifest_id
    first = stage_dense(
        manifest_dir,
        embedding_artifact_dir=artifact,
        new_index_manifest=manifest,
        expected_chunk_count=2,
        source_conn=source_conn,
        source_build_id=build_id,
    )
    validate_dense_candidate(first, expected_chunk_count=2)
    payload = json.loads(first.candidate_generation_path.read_text())
    assert payload["schema_version"] == "candidate_generation_v1"
    assert payload["status"] == "inactive"
    assert payload["table_name"] != "live_table"
    assert payload["chunk_count"] == 2
    assert payload["embedding_model"] == BGE_M3_MODEL
    assert payload["embedding_revision"] == BGE_M3_REVISION
    assert payload["embedding_dimension"] == BGE_M3_DIMENSION
    assert active.read_bytes() == before
    second = stage_dense(
        manifest_dir,
        embedding_artifact_dir=artifact,
        new_index_manifest=manifest,
        expected_chunk_count=2,
        source_conn=source_conn,
        source_build_id=build_id,
    )
    assert second.table_name == first.table_name
    assert json.loads(second.candidate_generation_path.read_text()) == payload
    (manifest_dir / "candidate_generation.json").write_text(
        json.dumps({**payload, "table_name": "other_table"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        stage_dense(
            manifest_dir,
            embedding_artifact_dir=artifact,
            new_index_manifest=manifest,
            expected_chunk_count=2,
            source_conn=source_conn,
            source_build_id=build_id,
        )
    source_conn.close()




def _db_for_artifact(tmp_path: Path, *, chunk_ids: list[str], build_id: str = "b" * 64) -> tuple[Path, str]:
    """Fixture derivative with corpus_build_chunks rows matching _artifact_dir."""
    import sqlite3

    db = tmp_path / "derivative.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE corpus_build_chunks (
            build_id TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            chunk_profile_version TEXT NOT NULL,
            section_key TEXT NOT NULL,
            ordinal TEXT NOT NULL,
            content_text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            metadata_hash TEXT NOT NULL,
            source_class TEXT NOT NULL,
            dedup_cluster_id TEXT,
            cluster_first_available_at TEXT,
            representative_document_id TEXT,
            available_at TEXT NOT NULL,
            ticker_associations TEXT NOT NULL,
            eligibility TEXT NOT NULL,
            status TEXT NOT NULL,
            boundary_kind TEXT NOT NULL,
            body_token_start INTEGER NOT NULL,
            body_token_end INTEGER NOT NULL,
            body_overlap_tokens INTEGER NOT NULL,
            prefix_token_count INTEGER NOT NULL,
            prefix_truncated INTEGER NOT NULL,
            section_parse_degraded INTEGER NOT NULL,
            source_kind TEXT NOT NULL,
            provider TEXT,
            source_type TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            canonical_asset_id TEXT,
            content_version_id TEXT,
            corpus_document_id TEXT,
            content_state TEXT,
            independence_group_id TEXT,
            parse_quality TEXT
        )
        """
    )
    rows = []
    for index, chunk_id in enumerate(chunk_ids):
        text = f"fixture {index}"
        rows.append(
            (
                build_id,
                chunk_id,
                f"doc:{chunk_id}",
                "news_v2",
                "body",
                "0001",
                text,
                hashlib.sha256(text.encode()).hexdigest(),
                "b" * 64,
                "reported_news" if index % 2 == 0 else "sec_filing",
                f"v1:dedup:cluster{index}",
                "2026-08-01T00:00:00Z",
                f"doc:{chunk_id}",
                "2026-08-01T00:00:00Z",
                '["AAPL"]',
                "eligible",
                "pending_embedding",
                "served",
                0,
                10,
                10,
                0,
                0,
                0,
                "news" if index % 2 == 0 else "filing",
                "provider",
                "type",
                "2026-08-01T00:00:00Z",
                "2026-08-01T00:00:00Z",
                None,
                None,
                None,
                "METADATA_ONLY",
                None,
                "not_applicable",
            )
        )
    conn.executemany(
        "INSERT INTO corpus_build_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db, build_id


# ---------------------------------------------------------------------------
# M3 corrective regression: stage_dense must never synthesize production rows
# from placeholder/default metadata. The real GPU artifact does NOT contain
# chunks.jsonl; per-row metadata must come from the derivative
# corpus_build_chunks (authoritative) or a complete artifact chunks.jsonl.
# ---------------------------------------------------------------------------

def _db_with_build_rows(tmp_path: Path, *, build_id: str = "b" * 64) -> tuple[Path, str]:
    """Create a fixture derivative containing corpus_build_chunks for build_id.

    Mirrors the production column set stage_dense reads; rows are returned in
    chunk_id COLLATE BINARY order exactly like the export path.
    """
    import sqlite3

    db = tmp_path / "derivative.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE corpus_build_chunks (
            build_id TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            chunk_profile_version TEXT NOT NULL,
            section_key TEXT NOT NULL,
            ordinal TEXT NOT NULL,
            content_text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            metadata_hash TEXT NOT NULL,
            source_class TEXT NOT NULL,
            dedup_cluster_id TEXT,
            cluster_first_available_at TEXT,
            representative_document_id TEXT,
            available_at TEXT NOT NULL,
            ticker_associations TEXT NOT NULL,
            eligibility TEXT NOT NULL,
            status TEXT NOT NULL,
            boundary_kind TEXT NOT NULL,
            body_token_start INTEGER NOT NULL,
            body_token_end INTEGER NOT NULL,
            body_overlap_tokens INTEGER NOT NULL,
            prefix_token_count INTEGER NOT NULL,
            prefix_truncated INTEGER NOT NULL,
            section_parse_degraded INTEGER NOT NULL,
            source_kind TEXT NOT NULL,
            provider TEXT,
            source_type TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            canonical_asset_id TEXT,
            content_version_id TEXT,
            corpus_document_id TEXT,
            content_state TEXT,
            independence_group_id TEXT,
            parse_quality TEXT
        )
        """
    )
    texts = {0: "db fixture zero", 1: "db fixture one"}
    rows = []
    for index in range(2):
        text = texts[index]
        rows.append(
            (
                build_id,
                f"chunk:{index:04d}",
                f"doc:{index}",
                "news_v2",
                "body",
                "0001",
                text,
                hashlib.sha256(text.encode()).hexdigest(),
                f"m{index:064x}",
                "reported_news" if index % 2 == 0 else "sec_filing",
                f"v1:dedup:cluster{index}",
                "2026-08-01T00:00:00Z",
                f"doc:{index}",
                "2026-08-01T00:00:00Z",
                '["AAPL"]' if index % 2 == 0 else '["MSFT","NVDA"]',
                "eligible",
                "pending_embedding",
                "served",
                0,
                10,
                10,
                0,
                0,
                0,
                "news" if index % 2 == 0 else "filing",
                "provider",
                "type",
                "2026-08-01T00:00:00Z",
                "2026-08-01T00:00:00Z",
                None,
                None,
                None,
                "METADATA_ONLY",
                None,
                "not_applicable",
            )
        )
    conn.executemany(
        "INSERT INTO corpus_build_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db, build_id


def test_stage_dense_fails_closed_without_authoritative_metadata(tmp_path):
    """A real GPU artifact has no chunks.jsonl and no derivative source:
    stage_dense must fail closed instead of synthesizing placeholder rows."""
    from catalyst_data.index.v1_staging import stage_dense

    artifact, chunk_ids, _vectors = _artifact_dir(tmp_path, count=2)
    (artifact / "chunks.jsonl").unlink()  # real GPU driver never writes this
    manifest = _manifest(artifact, vector_count=2, source_bundle_id="1" * 64)
    manifest_dir = tmp_path / "candidates" / manifest.index_manifest_id
    with pytest.raises(ValueError):
        stage_dense(
            manifest_dir,
            embedding_artifact_dir=artifact,
            new_index_manifest=manifest,
            expected_chunk_count=2,
        )


def test_stage_dense_uses_derivative_metadata_for_real_gpu_artifact(tmp_path):
    """A real GPU artifact (no chunks.jsonl) staged with the derivative
    corpus_build_chunks source must persist the authoritative metadata, never
    placeholders."""
    import sqlite3

    from catalyst_data.index.v1_staging import stage_dense

    artifact, chunk_ids, _vectors = _artifact_dir(tmp_path, count=2)
    (artifact / "chunks.jsonl").unlink()  # real GPU driver never writes this
    db, build_id = _db_with_build_rows(tmp_path, build_id="1" * 64)
    manifest = _manifest(artifact, vector_count=2, source_bundle_id="1" * 64)
    # _manifest defaults corpus_manifest_id to "c"*64; keep consistent with DB.
    conn = sqlite3.connect(db)
    try:
        candidate = stage_dense(
            tmp_path / "candidates" / manifest.index_manifest_id,
            embedding_artifact_dir=artifact,
            new_index_manifest=manifest,
            expected_chunk_count=2,
            source_conn=conn,
            source_build_id=build_id,
        )
    finally:
        conn.close()
    assert candidate.chunk_count == 2
    assert candidate.table_name.startswith("candidate_")

    import lancedb

    table = lancedb.connect(str(tmp_path / "candidates" / manifest.index_manifest_id)).open_table(
        candidate.table_name
    )
    rows = sorted(
        table.to_arrow().to_pylist(),
        key=lambda row: row["chunk_id"],
    )
    assert len(rows) == 2
    first = rows[0]
    assert first["chunk_id"] == "chunk:0000"
    assert first["document_id"] == "doc:0"
    assert first["content_text"] == "db fixture zero"
    assert first["content_hash"] == hashlib.sha256(b"db fixture zero").hexdigest()
    assert first["metadata_hash"] == "m0000000000000000000000000000000000000000000000000000000000000000"
    assert first["available_at"] == "2026-08-01T00:00:00Z"
    assert first["ticker_associations"] == ["AAPL"]
    assert first["source_class"] == "reported_news"
    assert first["chunk_profile_version"] == "news_v2"
    assert first["status"] == "pending_embedding"
    assert first["eligibility"] == "eligible"
    assert first["dedup_cluster_id"] == "v1:dedup:cluster0"
    assert first["cluster_first_available_at"] == "2026-08-01T00:00:00Z"
    assert first["representative_document_id"] == "doc:0"
    assert first["corpus_manifest_id"] == manifest.corpus_manifest_id
    assert first["index_manifest_id"] == manifest.index_manifest_id
    assert first["source_bundle_id"] == manifest.source_bundle_id
    second = rows[1]
    assert second["ticker_associations"] == ["MSFT", "NVDA"]
    assert second["source_class"] == "sec_filing"
    assert second["content_text"] == "db fixture one"


def test_stage_dense_rejects_chunks_jsonl_missing_required_fields(tmp_path):
    """An artifact chunks.jsonl that omits required metadata must fail closed,
    not fall back to defaults."""
    from catalyst_data.index.v1_staging import stage_dense

    artifact, chunk_ids, _vectors = _artifact_dir(tmp_path, count=2)
    # _artifact_dir's chunks.jsonl intentionally lacks source_class/status/
    # eligibility/dedup fields -> must be rejected as incomplete.
    manifest = _manifest(artifact, vector_count=2, source_bundle_id="1" * 64)
    with pytest.raises(ValueError):
        stage_dense(
            tmp_path / "candidates" / manifest.index_manifest_id,
            embedding_artifact_dir=artifact,
            new_index_manifest=manifest,
            expected_chunk_count=2,
        )


# ---------------------------------------------------------------------------
# M3 corrective regression (supervisor Finding 1): stage_dense must complete a
# bounded validation pass BEFORE any LanceDB table mutation. A malformed late
# row (past the first 512-row staging batch), a short source, a trailing
# source row, or a late order/hash failure must leave no candidate table and
# no candidate_generation.json.
# ---------------------------------------------------------------------------

_DB_DDL = """
CREATE TABLE corpus_build_chunks (
    build_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    chunk_profile_version TEXT NOT NULL,
    section_key TEXT NOT NULL,
    ordinal TEXT NOT NULL,
    content_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata_hash TEXT NOT NULL,
    source_class TEXT NOT NULL,
    dedup_cluster_id TEXT,
    cluster_first_available_at TEXT,
    representative_document_id TEXT,
    available_at TEXT NOT NULL,
    ticker_associations TEXT NOT NULL,
    eligibility TEXT NOT NULL,
    status TEXT NOT NULL,
    boundary_kind TEXT NOT NULL,
    body_token_start INTEGER NOT NULL,
    body_token_end INTEGER NOT NULL,
    body_overlap_tokens INTEGER NOT NULL,
    prefix_token_count INTEGER NOT NULL,
    prefix_truncated INTEGER NOT NULL,
    section_parse_degraded INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    provider TEXT,
    source_type TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    canonical_asset_id TEXT,
    content_version_id TEXT,
    corpus_document_id TEXT,
    content_state TEXT,
    independence_group_id TEXT,
    parse_quality TEXT
)
"""


def _db_with_many_build_rows(
    tmp_path: Path,
    *,
    count: int,
    build_id: str = "1" * 64,
    malformed_ticker_index: int | None = None,
    bad_hash_index: int | None = None,
    bad_order_index: int | None = None,
    extra_rows: int = 0,
) -> tuple[Path, str]:
    """Fixture derivative with `count` corpus_build_chunks rows matching
    _artifact_dir chunk_ids (chunk:0000..). Optional per-row mutations inject
    late failures after the first 512-row staging batch."""
    import sqlite3

    db = tmp_path / "derivative.db"
    conn = sqlite3.connect(db)
    conn.execute(_DB_DDL)
    rows = []
    for index in range(count):
        text = f"fixture {index}"
        chunk_id = f"chunk:{index:04d}"
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        ticker = '["AAPL"]'
        if malformed_ticker_index == index:
            ticker = "not-json"
        if bad_hash_index == index:
            content_hash = "0" * 64
        if bad_order_index == index and index > 0:
            chunk_id = f"chunk:{index - 1:04d}"
        rows.append(
            (
                build_id, chunk_id, f"doc:{index}", "news_v2", "body", "0001",
                text, content_hash, f"m{index:064x}", "reported_news",
                f"v1:dedup:cluster{index}", "2026-08-01T00:00:00Z", f"doc:{index}",
                "2026-08-01T00:00:00Z", ticker, "eligible", "pending_embedding",
                "served", 0, 10, 0, 0, 0, 0, "news", "provider", "type",
                "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z", None, None, None,
                "METADATA_ONLY", None, "not_applicable",
            )
        )
    for index in range(extra_rows):
        text = f"fixture extra {index}"
        chunk_id = f"chunk:extra{index:04d}"
        rows.append(
            (
                build_id, chunk_id, f"doc-extra:{index}", "news_v2", "body", "0001",
                text, hashlib.sha256(text.encode()).hexdigest(),
                f"e{index:064x}", "reported_news", None, "2026-08-01T00:00:00Z",
                f"doc-extra:{index}", "2026-08-01T00:00:00Z", '["AAPL"]',
                "eligible", "pending_embedding", "served", 0, 10, 0, 0, 0, 0,
                "news", "provider", "type", "2026-08-01T00:00:00Z",
                "2026-08-01T00:00:00Z", None, None, None, "METADATA_ONLY", None,
                "not_applicable",
            )
        )
    conn.executemany(
        "INSERT INTO corpus_build_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db, build_id


def _preserved_active_root(tmp_path: Path, source_bundle_id: str = "1" * 64) -> tuple[Path, Path, Path]:
    """Create a gold root with an active pointer plus a real prior table."""
    import lancedb

    gold = tmp_path / "lancedb_gold" / source_bundle_id
    gold.mkdir(parents=True)
    active = gold / "active_generation.json"
    active.write_text(
        json.dumps({"schema_version": "active_generation_v1", "table_name": "live_table"}),
        encoding="utf-8",
    )
    db = lancedb.connect(str(gold))
    db.create_table(
        "live_table",
        data=[{"chunk_id": "prior:0000", "vector": [0.0] * BGE_M3_DIMENSION}],
    )
    return gold, active, db.open_table("live_table")


def _assert_no_partial_candidate(manifest_dir: Path, active: Path, before: bytes, prior_table: Any) -> None:
    assert not (manifest_dir / "candidate_generation.json").exists()
    assert not list(Path(manifest_dir).glob("*.lance"))
    assert active.read_bytes() == before
    assert prior_table.count_rows() == 1


def test_stage_dense_late_malformed_row_fails_before_table_mutation(tmp_path):
    """A malformed metadata row after the first 512-row staging batch must
    raise before any LanceDB table is created."""
    from catalyst_data.index.v1_staging import stage_dense
    import sqlite3

    count = 1024
    artifact, chunk_ids, _vectors = _artifact_dir(tmp_path, count=count)
    (artifact / "chunks.jsonl").unlink()  # real GPU artifact has no chunks.jsonl
    db, build_id = _db_with_many_build_rows(
        tmp_path, count=count, malformed_ticker_index=600
    )
    manifest = _manifest(artifact, vector_count=count, source_bundle_id="1" * 64)
    gold, active, prior_table = _preserved_active_root(tmp_path)
    before = active.read_bytes()
    manifest_dir = gold / "candidates" / manifest.index_manifest_id
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(ValueError):
            stage_dense(
                manifest_dir,
                embedding_artifact_dir=artifact,
                new_index_manifest=manifest,
                expected_chunk_count=count,
                source_conn=conn,
                source_build_id=build_id,
            )
    finally:
        conn.close()
    _assert_no_partial_candidate(manifest_dir, active, before, prior_table)


def test_stage_dense_fewer_rows_fails_before_table_mutation(tmp_path):
    from catalyst_data.index.v1_staging import stage_dense
    import sqlite3

    count = 1024
    artifact, chunk_ids, _vectors = _artifact_dir(tmp_path, count=count)
    (artifact / "chunks.jsonl").unlink()
    db, build_id = _db_with_many_build_rows(tmp_path, count=count - 512)  # short source
    manifest = _manifest(artifact, vector_count=count, source_bundle_id="1" * 64)
    gold, active, prior_table = _preserved_active_root(tmp_path)
    before = active.read_bytes()
    manifest_dir = gold / "candidates" / manifest.index_manifest_id
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(ValueError):
            stage_dense(
                manifest_dir,
                embedding_artifact_dir=artifact,
                new_index_manifest=manifest,
                expected_chunk_count=count,
                source_conn=conn,
                source_build_id=build_id,
            )
    finally:
        conn.close()
    _assert_no_partial_candidate(manifest_dir, active, before, prior_table)


def test_stage_dense_trailing_row_fails_before_table_mutation(tmp_path):
    from catalyst_data.index.v1_staging import stage_dense
    import sqlite3

    count = 1024
    artifact, chunk_ids, _vectors = _artifact_dir(tmp_path, count=count)
    (artifact / "chunks.jsonl").unlink()
    db, build_id = _db_with_many_build_rows(tmp_path, count=count, extra_rows=1)
    manifest = _manifest(artifact, vector_count=count, source_bundle_id="1" * 64)
    gold, active, prior_table = _preserved_active_root(tmp_path)
    before = active.read_bytes()
    manifest_dir = gold / "candidates" / manifest.index_manifest_id
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(ValueError):
            stage_dense(
                manifest_dir,
                embedding_artifact_dir=artifact,
                new_index_manifest=manifest,
                expected_chunk_count=count,
                source_conn=conn,
                source_build_id=build_id,
            )
    finally:
        conn.close()
    _assert_no_partial_candidate(manifest_dir, active, before, prior_table)


def test_stage_dense_late_hash_and_order_failures_fail_before_table_mutation(tmp_path):
    from catalyst_data.index.v1_staging import stage_dense
    import sqlite3

    for iteration, mutation in enumerate((dict(bad_hash_index=900), dict(bad_order_index=700))):
        root = tmp_path / f"iter_{iteration}"
        root.mkdir()
        artifact, chunk_ids, _vectors = _artifact_dir(root, count=1024)
        (artifact / "chunks.jsonl").unlink()
        db, build_id = _db_with_many_build_rows(root, count=1024, **mutation)
        manifest = _manifest(artifact, vector_count=1024, source_bundle_id="1" * 64)
        gold, active, prior_table = _preserved_active_root(root)
        before = active.read_bytes()
        manifest_dir = gold / "candidates" / manifest.index_manifest_id
        conn = sqlite3.connect(db)
        try:
            with pytest.raises(ValueError):
                stage_dense(
                    manifest_dir,
                    embedding_artifact_dir=artifact,
                    new_index_manifest=manifest,
                    expected_chunk_count=1024,
                    source_conn=conn,
                    source_build_id=build_id,
                )
        finally:
            conn.close()
        _assert_no_partial_candidate(manifest_dir, active, before, prior_table)

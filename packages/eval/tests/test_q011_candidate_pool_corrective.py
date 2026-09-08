"""Q-011 candidate pool corrective contracts (RED/GREEN).

Locks the fail-closed corrections for the pointer-free evidence-pool runner:

- B1 execute requires explicit active-generation protection.
- B2 candidate_generation.json is mandatory and the identity chain
  (generation record + authoritative index_manifest.json + derivative
  corpus manifest) must agree; manually supplied substitute identities are
  rejected.
- B4 every successful pool must contain genuine, non-degraded, non-empty
  fts5/dense/hybrid/reranked arms with a real reranker-served reranked arm.
- B5 pool source_artifact_id is the persisted canonical arm-artifact ID.
- B6 typed temporal eligibility: equivalent ISO-8601 forms compare equal and
  malformed/naive timestamps are rejected.
- B7 bounded annotation packets carry repository-backed evidence fields.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from catalyst_data.config import BGE_M3_DIMENSION, BGE_M3_MODEL, BGE_M3_REVISION
from catalyst_data.retrieval.pool import load_union_pool
from tests.post_import_fixtures import make_result, make_result_set

from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION
from catalyst_data.retrieval.index_manifest import IndexManifest

# Self-contained fixture helpers (mirror original suite).
def _hex(value: str = "a") -> str:
    return (value * 64)[:64]


def _identities() -> dict[str, str]:
    return {
        "build_id": _hex("b"),
        "corpus_manifest_id": _hex("c"),
        "source_bundle_id": _hex("d"),
        "snapshot_id": _hex("e"),
        "probe_report_id": _hex("f"),
        "postbuild_readiness_id": _hex("0"),
        "index_manifest_id": _hex("1"),
    }


def _packet(tmp_path: Path, *, unsigned: bool = True, count: int = 12) -> Path:
    path = tmp_path / "q011_packet.json"
    cases = []
    for index in range(1, count + 1):
        slot = f"c{index:02d}"
        cases.append(
            {
                "slot": slot,
                "ticker": "AAPL",
                "session_date": "2025-05-12",
                "question": f"Why did {slot} move?",
                "cutoff_utc": "2025-05-12T20:00:00Z",
            }
        )
    path.write_text(
        json.dumps(
            {
                "schema_version": "q011_final_12_case_packet_v1",
                "unsigned_q011": bool(unsigned),
                "non_authoritative": bool(unsigned),
                "cases": cases,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _default_chunk_records(build_id: str, count: int = 12) -> list[dict[str, Any]]:
    """Metadata records for every fixture arm chunk used by the 12-case retriever."""
    records: list[dict[str, Any]] = []
    for index in range(1, count + 1):
        slot = f"c{index:02d}"
        for suffix in ("a", "b", "c", "d"):
            chunk_id = f"{slot}-{suffix}"
            content_text = f"evidence text {chunk_id}"
            records.append(
                {
                    "chunk_id": chunk_id,
                    "document_id": f"doc:{chunk_id}",
                    "content_hash": hashlib.sha256(content_text.encode()).hexdigest(),
                    "metadata_hash": "b" * 64,
                    "chunk_profile_version": "news_v2",
                    "source_class": "reported_news",
                    "dedup_cluster_id": f"dedup:{chunk_id}",
                    "cluster_first_available_at": "2025-05-01T00:00:00Z",
                    "representative_document_id": f"doc:{chunk_id}",
                    "canonical_asset_id": f"asset:{chunk_id}",
                    "content_version_id": f"content_version:{chunk_id}",
                    "corpus_document_id": f"doc:{chunk_id}",
                    "content_state": "live",
                    "independence_group_id": f"group:{chunk_id}",
                    "parse_quality": "full",
                    "section_parse_degraded": 0,
                    "available_at": "2025-05-01T00:00:00Z",
                }
            )
    return records


def _derivative(
    tmp_path: Path,
    *,
    name: str = "derivative.db",
    is_current: int = 0,
    build_id: str | None = "b" * 64,
    build_manifest_id: str | None = None,
    status: str = "lexical_ready",
    lexical_ready: int = 1,
    chunk_count: int = 2,
    chunk_rows: list[dict[str, Any]] | None = None,
    create_chunks: bool = True,
    chunk_table_unique: bool = True,
) -> Path:
    db = tmp_path / name
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE corpus_manifest (manifest_id TEXT PRIMARY KEY, "
        "manifest_json TEXT, is_current INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current) "
        "VALUES (?, ?, ?)",
        (build_manifest_id or _hex("c"), json.dumps({}), is_current),
    )
    if build_id is not None:
        conn.execute(
            "CREATE TABLE corpus_publication_builds (build_id TEXT PRIMARY KEY, "
            "manifest_id TEXT NOT NULL, status TEXT NOT NULL, lexical_ready INTEGER "
            "NOT NULL DEFAULT 0, chunk_count INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO corpus_publication_builds "
            "(build_id, manifest_id, status, lexical_ready, chunk_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                build_id,
                build_manifest_id or _hex("c"),
                status,
                int(lexical_ready),
                chunk_count,
            ),
        )
    if build_id is not None and create_chunks:
        unique = "PRIMARY KEY (build_id, chunk_id)" if chunk_table_unique else ""
        conn.execute(
            f"""
            CREATE TABLE corpus_build_chunks (
                build_id TEXT NOT NULL,
                chunk_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                metadata_hash TEXT NOT NULL,
                chunk_profile_version TEXT NOT NULL,
                source_class TEXT NOT NULL,
                dedup_cluster_id TEXT,
                cluster_first_available_at TEXT,
                representative_document_id TEXT,
                canonical_asset_id TEXT,
                content_version_id TEXT,
                corpus_document_id TEXT,
                content_state TEXT,
                independence_group_id TEXT,
                parse_quality TEXT,
                section_parse_degraded INTEGER NOT NULL,
                available_at TEXT NOT NULL
                {("," + unique) if unique else ""}
            )
            """
        )
        records = (
            chunk_rows
            if chunk_rows is not None
            else _default_chunk_records(build_id)
        )
        for record in records:
            conn.execute(
                "INSERT INTO corpus_build_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    build_id,
                    record["chunk_id"],
                    record["document_id"],
                    record["content_hash"],
                    record["metadata_hash"],
                    record["chunk_profile_version"],
                    record["source_class"],
                    record.get("dedup_cluster_id"),
                    record.get("cluster_first_available_at"),
                    record.get("representative_document_id"),
                    record.get("canonical_asset_id"),
                    record.get("content_version_id"),
                    record.get("corpus_document_id"),
                    record.get("content_state"),
                    record.get("independence_group_id"),
                    record.get("parse_quality"),
                    int(record.get("section_parse_degraded", 0)),
                    record["available_at"],
                ),
            )
    conn.commit()
    conn.close()
    return db


def _candidate_dir_with_records(tmp_path: Path, identities: dict[str, str], *, count: int = 2) -> Path:
    """Candidate dir with generation + index manifest + real LanceDB table."""
    import numpy as np
    from catalyst_data.config import BGE_M3_DIMENSION
    from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION
    from catalyst_data.retrieval.index_manifest import IndexManifest

    candidate_dir = tmp_path / "candidate_lancedb"
    candidate_dir.mkdir(exist_ok=True)
    vectors = np.zeros((count, BGE_M3_DIMENSION), dtype=np.float32)
    for index in range(count):
        vectors[index, 0] = 1.0 + index
    checksums = {
        "vectors.npy": hashlib.sha256(b"v").hexdigest(),
        "chunk_ids.json": hashlib.sha256(b"c").hexdigest(),
        "lancedb_table": hashlib.sha256(b"l").hexdigest(),
    }
    manifest = IndexManifest(
        model_name=BGE_M3_MODEL,
        model_revision=BGE_M3_REVISION,
        tokenizer_revision=TOKENIZER_REVISION,
        normalization_mode="l2",
        dtype="float32",
        dimension=BGE_M3_DIMENSION,
        corpus_manifest_id=identities["corpus_manifest_id"],
        source_bundle_id=identities["source_bundle_id"],
        snapshot_id=identities["snapshot_id"],
        probe_report_id=identities["probe_report_id"],
        postbuild_readiness_id=identities["postbuild_readiness_id"],
        artifact_hashes=checksums,
        code_revision="a" * 40,
        vector_count=count,
        artifact_state="vectors_staged",
    )
    (candidate_dir / "index_manifest.json").write_text(
        json.dumps(manifest.to_dict(), sort_keys=True), encoding="utf-8"
    )
    table_name = f"candidate_{manifest.index_manifest_id[:16]}"
    payload = {
        "schema_version": "candidate_generation_v1",
        "status": "inactive",
        "corpus_manifest_id": identities["corpus_manifest_id"],
        "index_manifest_id": manifest.index_manifest_id,
        "table_name": table_name,
        "chunk_count": count,
        "source_bundle_id": identities["source_bundle_id"],
        "embedding_model": BGE_M3_MODEL,
        "embedding_revision": BGE_M3_REVISION,
        "embedding_dimension": BGE_M3_DIMENSION,
    }
    (candidate_dir / "candidate_generation.json").write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )
    try:
        import lancedb
        import pyarrow as pa

        table = lancedb.connect(str(candidate_dir)).create_table(
            table_name,
            data=[
                {
                    "chunk_id": f"chunk:{index:04d}",
                    "vector": vectors[index].tolist(),
                }
                for index in range(count)
            ],
            mode="overwrite",
        )
    except Exception:
        pass
    identities["table_name"] = table_name
    identities["index_manifest_id"] = manifest.index_manifest_id
    return candidate_dir




def _make_case_retriever():
    """Deterministic HybridRetrievalResult-shaped fixture per case.

    Each arm returns exactly one genuine result carrying the field values the
    persisted arm artifact requires, proving all four arms were served.
    """
    def _hybrid(case):
        import types

        fts5 = make_result_set("lexical", "fts5", (f"{case.case_id}-a",), ticker=case.ticker)
        dense = make_result_set("dense", "dense", (f"{case.case_id}-b",), ticker=case.ticker)
        fusion = (
            make_result(
                f"{case.case_id}-c",
                ticker=case.ticker,
                available_at="2025-05-01T00:00:00Z",
                mode_requested="hybrid",
                mode_served="hybrid",
                fusion_rank=1,
                fusion_score=0.5,
                lexical_rank=1,
                lexical_raw_score=1.0,
            ),
        )
        final = (
            make_result(
                f"{case.case_id}-d",
                ticker=case.ticker,
                available_at="2025-05-01T00:00:00Z",
                mode_requested="reranked",
                mode_served="reranked",
                reranker_rank=1,
                reranker_score=9.0,
                fusion_rank=1,
                fusion_score=0.5,
            ),
        )
        # normalize fixture result cutoff to the case cutoff
        def _fix_result_set(rs, mode, served):
            fixed = []
            for item in rs.results:
                fixed.append(
                    make_result(
                        item.chunk_id,
                        ticker=case.ticker,
                        available_at=item.available_at,
                        mode_requested=mode,
                        mode_served=served,
                        lexical_rank=item.lexical_rank,
                        dense_rank=item.dense_rank,
                        lexical_raw_score=item.lexical_raw_score,
                        dense_score=item.dense_score,
                    )
                )
            return fixed

        fts5_fixed = tuple(_fix_result_set(fts5, "lexical", "fts5"))
        dense_fixed = tuple(_fix_result_set(dense, "dense", "dense"))
        return types.SimpleNamespace(
            lexical_results=types.SimpleNamespace(
                results=fts5_fixed,
                mode_served="fts5",
                trace=types.SimpleNamespace(total_ms=1.0),
            ),
            dense_results=types.SimpleNamespace(
                results=dense_fixed,
                mode_served="dense",
                trace=types.SimpleNamespace(total_ms=1.0),
            ),
            fusion_results=fusion,
            final_results=final,
            mode_requested="reranked",
            mode_served="reranked",
            degradation_reasons=(),
            latency_ms=1.0,
        )

    return _hybrid


def _candidate(tmp_path, identities):
    return _candidate_dir_with_records(tmp_path, identities)


def _identity(tmp_path, identities, *, derivative=None, candidate=None):
    return load_candidate_identity(
        run_id="q011-corrective",
        packet_path=_packet(tmp_path),
        derivative=derivative or _derivative(tmp_path),
        build_id=identities["build_id"],
        corpus_manifest_id=identities["corpus_manifest_id"],
        source_bundle_id=identities["source_bundle_id"],
        snapshot_id=identities["snapshot_id"],
        probe_report_id=identities["probe_report_id"],
        postbuild_readiness_id=identities["postbuild_readiness_id"],
        lancedb_dir=candidate or _candidate(tmp_path, identities),
        index_manifest_id=identities.get("index_manifest_id"),
        table_name=identities.get("table_name"),
    )


from catalyst_eval.v1_1.candidate_pool import (
    CandidatePoolError,
    _cutoff_decision,
    load_candidate_identity,
    run_candidate_pools,
    validate_four_arm_served,
)


def _active(tmp_path) -> tuple[Path, Path]:
    active = tmp_path / "active_lancedb"
    active.mkdir(exist_ok=True)
    pointer = active / "active_generation.json"
    if not pointer.exists():
        pointer.write_text(
            json.dumps({"schema_version": "active_generation_v1"}),
            encoding="utf-8",
        )
    return active, pointer


def _run(tmp_path, identities, *, retriever=None, remove_generation=False):
    candidate = _candidate(tmp_path, identities)
    if remove_generation:
        (candidate / "candidate_generation.json").unlink()
    active, pointer = _active(tmp_path)
    out = tmp_path / "out"
    ident = _identity(tmp_path, identities, candidate=candidate)
    run_candidate_pools(
        identity=ident,
        packet_path=_packet(tmp_path),
        output_dir=out,
        retrieve_case=retriever or _make_case_retriever(),
        active_lancedb_dir=active,
        active_generation_pointer=pointer,
        embedding_mode="mock_unit_test",
    )
    return out


def test_run_requires_active_paths(tmp_path):
    """B1: execute without explicit active dir/pointer fails closed."""
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    ident = _identity(tmp_path, identities, candidate=candidate)
    with pytest.raises(CandidatePoolError, match="active-generation protection"):
        run_candidate_pools(
            identity=ident,
            packet_path=_packet(tmp_path),
            output_dir=tmp_path / "out",
            retrieve_case=_make_case_retriever(),
            active_lancedb_dir=None,
            active_generation_pointer=None,
        )


def test_missing_candidate_generation_file_rejected(tmp_path):
    """B2: no silently supplied substitute identities when the file is absent."""
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    (candidate / "candidate_generation.json").unlink()
    with pytest.raises(CandidatePoolError, match="candidate_generation.json is required"):
        _identity(tmp_path, identities, candidate=candidate)


def test_active_status_generation_rejected(tmp_path):
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    payload = json.loads((candidate / "candidate_generation.json").read_text())
    payload["status"] = "active"
    (candidate / "candidate_generation.json").write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )
    with pytest.raises(CandidatePoolError, match="not inactive"):
        _identity(tmp_path, identities, candidate=candidate)


def test_spoofed_index_manifest_disagreement_rejected(tmp_path):
    """B2: candidate_generation/IndexManifest disagreement rejects."""
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    manifest = json.loads((candidate / "index_manifest.json").read_text())
    manifest["vector_count"] = manifest["vector_count"] + 5
    (candidate / "index_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    with pytest.raises(CandidatePoolError, match="IndexManifest|disagreement|count"):
        _identity(tmp_path, identities, candidate=candidate)


def test_spoofed_generation_chunk_count_rejected(tmp_path):
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    payload = json.loads((candidate / "candidate_generation.json").read_text())
    payload["chunk_count"] = int(payload["chunk_count"]) + 3
    (candidate / "candidate_generation.json").write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )
    with pytest.raises(CandidatePoolError, match="disagreement|count"):
        _identity(tmp_path, identities, candidate=candidate)


def test_mismatched_table_name_rejected(tmp_path):
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    payload = json.loads((candidate / "candidate_generation.json").read_text())
    payload["table_name"] = "not_the_candidate_table"
    (candidate / "candidate_generation.json").write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )
    with pytest.raises(CandidatePoolError, match="table_name"):
        _identity(tmp_path, identities, candidate=candidate)


def test_mismatched_source_bundle_rejected(tmp_path):
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    payload = json.loads((candidate / "candidate_generation.json").read_text())
    payload["source_bundle_id"] = _hex("e")
    (candidate / "candidate_generation.json").write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )
    with pytest.raises(CandidatePoolError, match="disagreement|source_bundle"):
        _identity(tmp_path, identities, candidate=candidate)


def test_mismatched_snapshot_probe_postbuild_rejected(tmp_path):
    """Supplied snapshot/probe/postbuild identities must equal the IndexManifest."""
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    manifest = json.loads((candidate / "index_manifest.json").read_text())
    manifest["snapshot_id"] = _hex("f")
    (candidate / "index_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    with pytest.raises(CandidatePoolError, match="disagrees|snapshot|valid IndexManifest"):
        _identity(tmp_path, identities, candidate=candidate)


def test_cli_snapshot_disagrees_with_valid_manifest_rejected(tmp_path):
    """B2: a manually supplied snapshot that disagrees with the record rejects."""
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    identities = dict(identities)
    identities["snapshot_id"] = _hex("9")
    with pytest.raises(CandidatePoolError, match="disagrees|snapshot"):
        _identity(tmp_path, identities, candidate=candidate)


def test_active_corpus_manifest_rejected(tmp_path):
    """Active corpus manifest (is_current=1) is never an inactive candidate."""
    identities = _identities()
    derivative = _derivative(tmp_path, is_current=1)
    with pytest.raises(CandidatePoolError, match="is_current=1"):
        _identity(tmp_path, identities, derivative=derivative)


def test_run_rejects_degraded_reranked_mode(tmp_path):
    """B4: a degraded/hybrid-fallback result cannot be labeled reranked."""
    identities = _identities()

    def _degraded(case):
        import types

        def _shape(chunk_id, mode, served):
            return make_result(
                chunk_id,
                ticker=case.ticker,
                available_at="2025-05-01T00:00:00Z",
                mode_requested=mode,
                mode_served=served,
                reranker_rank=1 if served == "reranked" else None,
                reranker_score=9.0 if served == "reranked" else None,
            )

        fts5 = make_result_set("lexical", "fts5", (f"{case.case_id}-a",), ticker=case.ticker)
        dense = make_result_set("dense", "dense", (f"{case.case_id}-b",), ticker=case.ticker)
        return types.SimpleNamespace(
            lexical_results=types.SimpleNamespace(results=fts5.results, mode_served="fts5"),
            dense_results=types.SimpleNamespace(results=dense.results, mode_served="dense"),
            fusion_results=(_shape(f"{case.case_id}-c", "hybrid", "hybrid"),),
            final_results=(_shape(f"{case.case_id}-d", "reranked", "reranked"),),
            mode_requested="reranked",
            mode_served="hybrid",  # degraded: reranker did not serve
            degradation_reasons=("reranker_timeout",),
            latency_ms=2.0,
        )

    with pytest.raises(CandidatePoolError, match="reranked|degraded"):
        _run(tmp_path, identities, retriever=_degraded)


def test_run_rejects_empty_arm(tmp_path):
    """B4: an empty arm is not a successful four-arm pool run."""
    identities = _identities()

    def _empty_dense(case):
        import types

        fts5 = make_result_set("lexical", "fts5", (f"{case.case_id}-a",), ticker=case.ticker)
        final = (
            make_result(
                f"{case.case_id}-d",
                ticker=case.ticker,
                available_at="2025-05-01T00:00:00Z",
                mode_requested="reranked",
                mode_served="reranked",
                reranker_rank=1,
                reranker_score=9.0,
            ),
        )
        return types.SimpleNamespace(
            lexical_results=types.SimpleNamespace(results=fts5.results, mode_served="fts5"),
            dense_results=types.SimpleNamespace(results=(), mode_served="dense"),
            fusion_results=(),
            final_results=final,
            mode_requested="reranked",
            mode_served="reranked",
            degradation_reasons=(),
            latency_ms=2.0,
        )

    with pytest.raises(CandidatePoolError, match="empty|not a genuine success"):
        _run(tmp_path, identities, retriever=_empty_dense)


def test_run_rejects_reranked_without_scores(tmp_path):
    """B4: final results without finite reranker scores are not reranked."""
    identities = _identities()

    def _no_scores(case):
        import types

        fts5 = make_result_set("lexical", "fts5", (f"{case.case_id}-a",), ticker=case.ticker)
        dense = make_result_set("dense", "dense", (f"{case.case_id}-b",), ticker=case.ticker)
        bad = make_result(
            f"{case.case_id}-d",
            ticker=case.ticker,
            available_at="2025-05-01T00:00:00Z",
            mode_requested="reranked",
            mode_served="reranked",
        )
        return types.SimpleNamespace(
            lexical_results=types.SimpleNamespace(results=fts5.results, mode_served="fts5"),
            dense_results=types.SimpleNamespace(results=dense.results, mode_served="dense"),
            fusion_results=(),
            final_results=(bad,),
            mode_requested="reranked",
            mode_served="reranked",
            degradation_reasons=(),
            latency_ms=2.0,
        )

    with pytest.raises(CandidatePoolError, match="finite reranker|not a genuine success"):
        _run(tmp_path, identities, retriever=_no_scores)


def test_success_manifest_records_real_arm_source_ids(tmp_path):
    """B5: pool source_artifact_id is the canonical arm-artifact id (not union hash)."""
    from catalyst_data.retrieval.artifacts import load_arm_artifact

    identities = _identities()
    out = _run(tmp_path, identities)
    assert (out / "arms").is_dir(), (out / "arms")
    arm_files = sorted((out / "arms").glob("c*.json"))
    assert len(arm_files) == 12, [p.name for p in (out / "arms").iterdir()]
    for pool_path in sorted((out / "pools").glob("c*.json")):
        pool = load_union_pool(pool_path)
        arm_path = out / "arms" / f"{pool.case_id}.json"
        artifact = load_arm_artifact(arm_path)
        assert pool.source_artifact_id == artifact.artifact_id
        assert set(pool.per_arm_chunk_ids) == {"fts5", "dense", "hybrid", "reranked"}


def test_typed_temporal_equivalences():
    """B6: equivalent ISO-8601 forms are equal under typed UTC comparison."""
    assert _cutoff_decision("2025-05-12T20:00:00Z", "2025-05-12T20:00:00+00:00") == "eligible"
    assert _cutoff_decision("2025-05-12T19:59:59.500Z", "2025-05-12T20:00:00Z") == "eligible"
    assert _cutoff_decision("2025-05-12T20:00:00.001+00:00", "2025-05-12T20:00:00Z") == "post_cutoff_excluded"
    assert _cutoff_decision("2025-05-12T21:00:00Z", "2025-05-12T20:00:00Z") == "post_cutoff_excluded"


def test_typed_temporal_rejects_naive_and_malformed():
    with pytest.raises(CandidatePoolError, match="timezone-aware|ISO-8601"):
        _cutoff_decision("2025-05-12T20:00:00", "2025-05-12T20:00:00Z")
    with pytest.raises(CandidatePoolError, match="ISO-8601"):
        _cutoff_decision("not-a-date", "2025-05-12T20:00:00Z")
    with pytest.raises(CandidatePoolError, match="timezone-aware|ISO-8601"):
        _cutoff_decision("2025-05-12T20:00:00Z", "2025-05-12")


def test_packet_rows_carry_evidence_metadata_fields(tmp_path):
    """B7: packet rows include available repository-backed evidence fields."""
    identities = _identities()
    out = _run(tmp_path, identities)
    packet = json.loads((out / "packets" / "c01.json").read_text())
    assert packet["rows"]
    required = {
        "evidence_id", "document_id", "available_at", "case_cutoff_utc",
        "temporal_status", "excerpt", "excerpt_bounded", "ticker",
        "ticker_associations", "arm", "rank", "score",
        "corpus_manifest_id", "index_manifest_id", "table_name",
        "build_id", "source_bundle_id", "snapshot_id", "probe_report_id",
        "postbuild_readiness_id",
    }
    row = packet["rows"][0]
    for key in required:
        assert key in row, key
    assert set(row) <= {
        "case_id", "evidence_id", "canonical_asset_id", "content_version_id",
        "document_id", "source_class", "available_at", "case_cutoff_utc",
        "temporal_status", "excerpt", "excerpt_bounded", "ticker",
        "ticker_associations", "dedup_cluster_id", "cluster_first_available_at",
        "representative_document_id", "content_hash", "metadata_hash",
        "chunk_profile_version", "independence_group_id", "parse_quality",
        "content_state", "section_parse_degraded", "arm", "rank", "score",
        "arm_ranks", "arm_scores", "corpus_manifest_id", "index_manifest_id",
        "table_name", "build_id", "source_bundle_id", "snapshot_id",
        "probe_report_id", "postbuild_readiness_id",
    }


def test_candidate_remains_inactive_and_pointer_unchanged(tmp_path):
    """After a pool run the candidate stays inactive and active fingerprints hold."""
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    active, pointer = _active(tmp_path)
    pointer_before = pointer.read_bytes()
    _run(tmp_path, identities)
    payload = json.loads((candidate / "candidate_generation.json").read_text())
    assert payload["status"] == "inactive"
    assert pointer.read_bytes() == pointer_before


# ---------------------------------------------------------------------------
# Final narrow corrective pass: build-row identity, output-dir protection,
# metadata binding, real chunk_profile_version, artifact identity chain,
# offline reranker contract.
# ---------------------------------------------------------------------------


def test_missing_build_row_rejected(tmp_path):
    """Item 1: a derivative without corpus_publication_builds fails closed."""
    import catalyst_eval.v1_1.candidate_pool as pool_mod

    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    # No corpus_publication_builds table on this derivative.
    derivative = _derivative(tmp_path, build_id=None)
    with pytest.raises(CandidatePoolError, match="corpus_publication_builds|not found"):
        pool_mod.load_candidate_identity(
            run_id="q011-corrective",
            packet_path=_packet(tmp_path),
            derivative=derivative,
            build_id=identities["build_id"],
            corpus_manifest_id=identities["corpus_manifest_id"],
            source_bundle_id=identities["source_bundle_id"],
            snapshot_id=identities["snapshot_id"],
            probe_report_id=identities["probe_report_id"],
            postbuild_readiness_id=identities["postbuild_readiness_id"],
            lancedb_dir=candidate,
        )


def test_arbitrary_build_id_rejected(tmp_path):
    """Item 1: build_id with no matching build row is rejected."""
    import catalyst_eval.v1_1.candidate_pool as pool_mod

    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    derivative = _derivative(tmp_path, build_id=_hex("9"))  # table row for other id
    with pytest.raises(CandidatePoolError, match="not found"):
        pool_mod.load_candidate_identity(
            run_id="q011-corrective",
            packet_path=_packet(tmp_path),
            derivative=derivative,
            build_id=identities["build_id"],
            corpus_manifest_id=identities["corpus_manifest_id"],
            source_bundle_id=identities["source_bundle_id"],
            snapshot_id=identities["snapshot_id"],
            probe_report_id=identities["probe_report_id"],
            postbuild_readiness_id=identities["postbuild_readiness_id"],
            lancedb_dir=candidate,
        )


def test_wrong_build_ownership_rejected(tmp_path):
    """Item 1: build manifest_id must equal the candidate corpus manifest."""
    import catalyst_eval.v1_1.candidate_pool as pool_mod

    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    derivative = _derivative(tmp_path)
    conn = sqlite3.connect(derivative)
    conn.execute(
        "UPDATE corpus_publication_builds SET manifest_id=? WHERE build_id=?",
        (_hex("9"), identities["build_id"]),
    )
    conn.commit()
    conn.close()
    with pytest.raises(CandidatePoolError, match="manifest_id"):
        pool_mod.load_candidate_identity(
            run_id="q011-corrective",
            packet_path=_packet(tmp_path),
            derivative=derivative,
            build_id=identities["build_id"],
            corpus_manifest_id=identities["corpus_manifest_id"],
            source_bundle_id=identities["source_bundle_id"],
            snapshot_id=identities["snapshot_id"],
            probe_report_id=identities["probe_report_id"],
            postbuild_readiness_id=identities["postbuild_readiness_id"],
            lancedb_dir=candidate,
        )


def test_contradictory_build_readiness_rejected(tmp_path):
    """Item 1: status lexical_ready with lexical_ready=0 is incoherent."""
    import catalyst_eval.v1_1.candidate_pool as pool_mod

    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    derivative = _derivative(
        tmp_path, status="lexical_ready", lexical_ready=0, chunk_count=2
    )
    with pytest.raises(CandidatePoolError, match="lexical-ready|lexical_ready"):
        pool_mod.load_candidate_identity(
            run_id="q011-corrective",
            packet_path=_packet(tmp_path),
            derivative=derivative,
            build_id=identities["build_id"],
            corpus_manifest_id=identities["corpus_manifest_id"],
            source_bundle_id=identities["source_bundle_id"],
            snapshot_id=identities["snapshot_id"],
            probe_report_id=identities["probe_report_id"],
            postbuild_readiness_id=identities["postbuild_readiness_id"],
            lancedb_dir=candidate,
        )


def test_build_chunk_count_mismatch_rejected(tmp_path):
    """Item 1: build chunk_count must equal the generation/vector count."""
    import catalyst_eval.v1_1.candidate_pool as pool_mod

    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    derivative = _derivative(
        tmp_path, status="lexical_ready", lexical_ready=1, chunk_count=9
    )
    with pytest.raises(CandidatePoolError, match="chunk_count mismatch"):
        pool_mod.load_candidate_identity(
            run_id="q011-corrective",
            packet_path=_packet(tmp_path),
            derivative=derivative,
            build_id=identities["build_id"],
            corpus_manifest_id=identities["corpus_manifest_id"],
            source_bundle_id=identities["source_bundle_id"],
            snapshot_id=identities["snapshot_id"],
            probe_report_id=identities["probe_report_id"],
            postbuild_readiness_id=identities["postbuild_readiness_id"],
            lancedb_dir=candidate,
        )


def _identity_for(
    pool_mod,
    tmp_path: Path,
    identities: dict[str, str],
    *,
    candidate: Path,
    derivative: Path,
) -> None:
    pool_mod.load_candidate_identity(
        run_id="q011-corrective",
        packet_path=_packet(tmp_path),
        derivative=derivative,
        build_id=identities["build_id"],
        corpus_manifest_id=identities["corpus_manifest_id"],
        source_bundle_id=identities["source_bundle_id"],
        snapshot_id=identities["snapshot_id"],
        probe_report_id=identities["probe_report_id"],
        postbuild_readiness_id=identities["postbuild_readiness_id"],
        lancedb_dir=candidate,
    )


def test_missing_lancedb_table_rejected(tmp_path):
    """Item 1: missing candidate LanceDB table must fail closed."""
    import catalyst_eval.v1_1.candidate_pool as pool_mod

    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    import lancedb

    db = lancedb.connect(str(candidate))
    names = set(db.table_names())
    for name in names:
        db.drop_table(name)
    derivative = _derivative(tmp_path)
    with pytest.raises(CandidatePoolError, match="missing from|LanceDB table"):
        _identity_for(pool_mod, tmp_path, identities, candidate=candidate, derivative=derivative)


def test_unreadable_lancedb_table_rejected(tmp_path, monkeypatch):
    """Item 1: an unreadable LanceDB connection must fail closed (not None)."""
    import types
    import catalyst_eval.v1_1.candidate_pool as pool_mod

    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    derivative = _derivative(tmp_path)

    def connect_boom(*args, **kwargs):
        raise OSError("unreadable lancedb dir")

    stub = types.ModuleType("lancedb")
    stub.connect = connect_boom
    monkeypatch.setitem(sys.modules, "lancedb", stub)
    with pytest.raises(CandidatePoolError, match="unreadable|lancedb"):
        _identity_for(pool_mod, tmp_path, identities, candidate=candidate, derivative=derivative)


def test_output_dir_beneath_active_lancedb_rejected_before_retrieval(tmp_path):
    """Item 2: output under the active LanceDB dir fails before any retrieval."""
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    ident = _identity(tmp_path, identities, candidate=candidate)
    active = tmp_path / "active_lancedb"
    active.mkdir()
    pointer = active / "active_generation.json"
    pointer.write_text(
        json.dumps({"schema_version": "active_generation_v1"}),
        encoding="utf-8",
    )
    out = active / "pool_out"
    called: list[str] = []

    def boom(case):
        called.append(case.case_id)
        raise AssertionError("retriever must not be called")

    with pytest.raises(CandidatePoolError, match="output dir .* aliases protected active"):
        run_candidate_pools(
            identity=ident,
            packet_path=_packet(tmp_path),
            output_dir=out,
            retrieve_case=boom,
            active_lancedb_dir=active,
            active_generation_pointer=pointer,
        )
    assert called == []
    assert not out.exists()


def test_missing_metadata_row_rejected(tmp_path):
    """Item 3: exactly one metadata row per union chunk is required."""
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    active, pointer = _active(tmp_path)
    records = _default_chunk_records(identities["build_id"])
    records = [record for record in records if record["chunk_id"] != "c01-a"]
    derivative = _derivative(tmp_path, name="meta-missing.db", chunk_rows=records)
    ident = _identity(tmp_path, identities, candidate=candidate, derivative=derivative)
    with pytest.raises(CandidatePoolError, match="metadata row missing"):
        run_candidate_pools(
            identity=ident,
            packet_path=_packet(tmp_path),
            output_dir=tmp_path / "out",
            retrieve_case=_make_case_retriever(),
            active_lancedb_dir=active,
            active_generation_pointer=pointer,
        )


def test_duplicate_metadata_row_rejected(tmp_path):
    """Item 3: duplicate build metadata rows are rejected."""
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    active, pointer = _active(tmp_path)
    records = _default_chunk_records(identities["build_id"])
    duplicate = dict(records[0])
    duplicate["document_id"] = "dup-doc"
    derivative = _derivative(
        tmp_path, name="meta-dup.db",
        chunk_rows=records + [duplicate], chunk_table_unique=False,
    )
    ident = _identity(tmp_path, identities, candidate=candidate, derivative=derivative)
    with pytest.raises(CandidatePoolError, match="duplicate metadata row"):
        run_candidate_pools(
            identity=ident,
            packet_path=_packet(tmp_path),
            output_dir=tmp_path / "out",
            retrieve_case=_make_case_retriever(),
            active_lancedb_dir=active,
            active_generation_pointer=pointer,
        )


def test_metadata_sqlite_error_fails_closed(tmp_path):
    """Item 3: schema/SQLite errors are not converted into an empty dict."""
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    active, pointer = _active(tmp_path)
    # Build table exists but corpus_build_chunks is absent -> query error.
    derivative = _derivative(tmp_path, name="meta-no-chunks.db", create_chunks=False)
    ident = _identity(tmp_path, identities, candidate=candidate, derivative=derivative)
    with pytest.raises(CandidatePoolError, match="metadata lookup failed"):
        run_candidate_pools(
            identity=ident,
            packet_path=_packet(tmp_path),
            output_dir=tmp_path / "out",
            retrieve_case=_make_case_retriever(),
            active_lancedb_dir=active,
            active_generation_pointer=pointer,
        )


def test_packet_uses_real_chunk_profile_version(tmp_path):
    """Item 4: no invented candidate_chunk_profile_v1 fallback."""
    identities = _identities()
    out = _run(tmp_path, identities)
    for packet_path in sorted((out / "packets").glob("c*.json")):
        text = packet_path.read_text(encoding="utf-8")
        assert "candidate_chunk_profile_v1" not in text
        packet = json.loads(text)
        assert packet["rows"]
        for row in packet["rows"]:
            assert row["chunk_profile_version"] == "news_v2"


def test_arm_artifact_filters_carry_complete_identity_chain(tmp_path):
    """Item 5: persisted arm filters include the full candidate identity chain."""
    from catalyst_data.retrieval.artifacts import load_arm_artifact

    identities = _identities()
    out = _run(tmp_path, identities)
    arm_files = sorted((out / "arms").glob("c*.json"))
    assert arm_files
    for arm_path in arm_files:
        artifact = load_arm_artifact(arm_path)
        filters = artifact.filters
        expected = {
            "build_id": identities["build_id"],
            "corpus_manifest_id": identities["corpus_manifest_id"],
            "index_manifest_id": identities["index_manifest_id"],
            "source_bundle_id": identities["source_bundle_id"],
            "snapshot_id": identities["snapshot_id"],
            "probe_report_id": identities["probe_report_id"],
            "postbuild_readiness_id": identities["postbuild_readiness_id"],
            "table_name": identities["table_name"],
            "model_name": BGE_M3_MODEL,
            "model_revision": BGE_M3_REVISION,
            "reranker_model": "BAAI/bge-reranker-v2-m3",
        }
        for key, value in expected.items():
            assert filters[key] == value, key
        assert filters["embedding_dimension"] == BGE_M3_DIMENSION


def test_production_reranker_missing_fails_before_run(tmp_path):
    """Item 7: a None reranker aborts before the 12-case run; no fallback."""
    from catalyst_eval.v1_1.candidate_pool import build_production_retriever

    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    table_name = identities["table_name"]
    ident = _identity(tmp_path, identities, candidate=candidate)

    class _EmbedderFactory:
        def create(self, model_name):
            assert model_name == BGE_M3_MODEL
            return _FakeEmbedder()

    class _FakeEmbedder:
        def embed_query(self, text):
            return [0.0] * BGE_M3_DIMENSION

    with pytest.raises(CandidatePoolError, match="reranker"):
        build_production_retriever(
            derivative=Path(ident.derivative),
            lancedb_dir=str(candidate),
            table_name=table_name,
            index_manifest_id=ident.index_manifest_id,
            corpus_manifest_id=ident.corpus_manifest_id,
            reranker_timeout=2.0,
            cuda_available=lambda: True,
            embedder_factory=_EmbedderFactory(),
            reranker_loader=lambda: None,
        )


def test_production_embedder_missing_fails_before_run(tmp_path):
    """Item 7: a missing offline embedder aborts before the 12-case run."""
    from catalyst_eval.v1_1.candidate_pool import build_production_retriever

    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    ident = _identity(tmp_path, identities, candidate=candidate)

    class _EmbedderFactory:
        def create(self, model_name):
            raise RuntimeError("offline embedder cache missing")

    with pytest.raises(Exception, match="embedder|offline|cache|RuntimeError"):
        build_production_retriever(
            derivative=Path(ident.derivative),
            lancedb_dir=str(candidate),
            table_name=ident.table_name,
            index_manifest_id=ident.index_manifest_id,
            corpus_manifest_id=ident.corpus_manifest_id,
            reranker_timeout=2.0,
            cuda_available=lambda: True,
            embedder_factory=_EmbedderFactory(),
            reranker_loader=lambda: object(),
        )


# ---------------------------------------------------------------------------
# Cloud-readiness: exact-build evidence identities, exact arm mode_served,
# and retriever cleanup on preflight/output-path failures.
# ---------------------------------------------------------------------------


def _null_one_field(records: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    out = []
    for record in records:
        copied = dict(record)
        if copied["chunk_id"] == "c01-a":
            copied[field] = None
        out.append(copied)
    return out


@pytest.mark.parametrize(
    "field",
    (
        "canonical_asset_id",
        "content_version_id",
        "independence_group_id",
        "content_state",
        "parse_quality",
    ),
)
def test_null_exact_build_identity_is_rejected(tmp_path, field):
    """Q-011 evidence rows must carry real exact-build values; null is unpublished."""
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    active, pointer = _active(tmp_path)
    records = _null_one_field(_default_chunk_records(identities["build_id"]), field)
    derivative = _derivative(tmp_path, name=f"meta-null-{field}.db", chunk_rows=records)
    ident = _identity(tmp_path, identities, candidate=candidate, derivative=derivative)
    with pytest.raises(CandidatePoolError, match=field):
        run_candidate_pools(
            identity=ident,
            packet_path=_packet(tmp_path),
            output_dir=tmp_path / "out",
            retrieve_case=_make_case_retriever(),
            active_lancedb_dir=active,
            active_generation_pointer=pointer,
        )


def _hybrid_with_served(*, fts5="fts5", dense="dense", hybrid="hybrid", reranked="reranked"):
    import types

    def _item(chunk_id, requested, served, **scores):
        return make_result(
            chunk_id,
            ticker="AAPL",
            available_at="2025-05-01T00:00:00Z",
            mode_requested=requested,
            mode_served=served,
            **scores,
        )

    fts5_item = _item("c01-a", "lexical", fts5, lexical_rank=1, lexical_raw_score=1.0)
    dense_item = _item("c01-b", "dense", dense, dense_rank=1, dense_score=0.9)
    hybrid_item = _item("c01-c", "hybrid", hybrid, fusion_rank=1, fusion_score=0.5)
    reranked_item = _item(
        "c01-d", "reranked", reranked, reranker_rank=1, reranker_score=9.0
    )
    return types.SimpleNamespace(
        lexical_results=types.SimpleNamespace(
            results=(fts5_item,), mode_served=fts5, is_degraded=False
        ),
        dense_results=types.SimpleNamespace(
            results=(dense_item,), mode_served=dense, is_degraded=False
        ),
        fusion_results=(hybrid_item,),
        final_results=(reranked_item,),
        mode_requested="reranked",
        mode_served="reranked",
        degradation_reasons=(),
        latency_ms=1.0,
    )


def test_fts5_arm_must_be_served_as_fts5():
    hybrid = _hybrid_with_served(fts5="sql_like")
    with pytest.raises(CandidatePoolError, match="fts5 arm .*fts5|served as 'sql_like'"):
        validate_four_arm_served(hybrid, case_id="c01")


def test_dense_arm_must_be_served_as_dense():
    hybrid = _hybrid_with_served(dense="hybrid")
    with pytest.raises(CandidatePoolError, match="dense arm .*dense|served as 'hybrid'"):
        validate_four_arm_served(hybrid, case_id="c01")


def test_hybrid_arm_must_be_served_as_hybrid():
    hybrid = _hybrid_with_served(hybrid="reranked")
    with pytest.raises(CandidatePoolError, match="hybrid arm .*hybrid|served as 'reranked'"):
        validate_four_arm_served(hybrid, case_id="c01")


def test_reranked_arm_must_be_served_as_reranked():
    hybrid = _hybrid_with_served(reranked="hybrid")
    with pytest.raises(CandidatePoolError, match="reranked arm .*reranked|served as 'hybrid'"):
        validate_four_arm_served(hybrid, case_id="c01")


def test_retriever_closed_on_nonempty_output_dir(tmp_path):
    """Cleanup must run for output-path failures after the retriever exists."""
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    ident = _identity(tmp_path, identities, candidate=candidate)
    active, pointer = _active(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "stale.json").write_text("{}", encoding="utf-8")
    closed = {"n": 0}

    def close():
        closed["n"] += 1

    with pytest.raises(CandidatePoolError, match="empty"):
        run_candidate_pools(
            identity=ident,
            packet_path=_packet(tmp_path),
            output_dir=out,
            retrieve_case=_make_case_retriever(),
            active_lancedb_dir=active,
            active_generation_pointer=pointer,
            retriever_close=close,
        )
    assert closed["n"] == 1


def test_retriever_closed_on_output_alias_failure(tmp_path):
    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    ident = _identity(tmp_path, identities, candidate=candidate)
    active, pointer = _active(tmp_path)
    closed = {"n": 0}

    def close():
        closed["n"] += 1

    with pytest.raises(CandidatePoolError, match="aliases protected active"):
        run_candidate_pools(
            identity=ident,
            packet_path=_packet(tmp_path),
            output_dir=active / "pool_out",
            retrieve_case=_make_case_retriever(),
            active_lancedb_dir=active,
            active_generation_pointer=pointer,
            retriever_close=close,
        )
    assert closed["n"] == 1


def test_production_retriever_closes_sqlite_when_lancedb_open_fails(tmp_path, monkeypatch):
    """SQLite opened during retriever construction must close if LanceDB fails."""
    import types
    import catalyst_eval.v1_1.candidate_pool as pool_mod

    identities = _identities()
    candidate = _candidate(tmp_path, identities)
    ident = _identity(tmp_path, identities, candidate=candidate)

    class _EmbedderFactory:
        def create(self, model_name):
            return types.SimpleNamespace(embed_query=lambda text: [0.0] * BGE_M3_DIMENSION)

    created: list[Any] = []

    class _Conn:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    def fake_connect(*args, **kwargs):
        conn = _Conn()
        created.append(conn)
        return conn

    class _BoomDB:
        def open_table(self, name):
            raise OSError("table open failed")

    stub = types.ModuleType("lancedb")
    stub.connect = lambda *args, **kwargs: _BoomDB()
    monkeypatch.setitem(__import__("sys").modules, "lancedb", stub)
    monkeypatch.setattr(pool_mod.sqlite3, "connect", fake_connect)

    with pytest.raises(CandidatePoolError, match="unreadable|table|LanceDB|open"):
        pool_mod.build_production_retriever(
            derivative=Path(ident.derivative),
            lancedb_dir=str(candidate),
            table_name=ident.table_name,
            index_manifest_id=ident.index_manifest_id,
            corpus_manifest_id=ident.corpus_manifest_id,
            reranker_timeout=2.0,
            cuda_available=lambda: True,
            embedder_factory=_EmbedderFactory(),
            reranker_loader=lambda: object(),
        )
    assert created, "sqlite connection was never opened"
    assert created[0].closed is True
